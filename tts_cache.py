"""Bounded, disposable TTS spool. No audio or job metadata enters the data volume."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from pathlib import Path

from tts_audio import MP3Stream
from tts_preparation import WAIT_SECONDS, speech_preparation, speech_segments
from tts_service import TTSError, synthesize


def _env_limit(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if minimum <= value <= maximum else default


class TTSCache:
    def __init__(self, directory=None, *, ttl=3600, max_bytes=None, workers=None):
        self.directory = Path(directory or os.environ.get("TTS_TEMP_DIR") or
                              (Path(tempfile.gettempdir()) / ("raynews-tts-" + str(os.getuid()))))
        if max_bytes is None:
            max_bytes = _env_limit("TTS_CACHE_MAX_MB", 256, 64, 2048) * 1024 * 1024
        if workers is None:
            workers = _env_limit("TTS_WORKERS", 2, 1, 8)
        self.ttl, self.max_bytes = ttl, max_bytes
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.jobs, self.keys = {}, {}
        self.cancelled_listeners = OrderedDict()
        self.reserved_bytes = 0
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tts")
        self.slots = threading.BoundedSemaphore(16)
        self.started = False

    def start(self):
        with self.lock:
            if self.started:
                return
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.directory.chmod(0o700)
            # Registry is process-local by design. Remove our own orphaned spool
            # files on restart, including old audio formats and incomplete downloads.
            for pattern in ("*.mp3", "*.opus", "*.flac", "*.wav", "*.part"):
                for path in self.directory.glob(pattern):
                    if len(path.stem) == 32 and all(c in "0123456789abcdef" for c in path.stem):
                        path.unlink(missing_ok=True)
            self.started = True
            threading.Thread(target=self._clean_loop, daemon=True, name="tts-cleanup").start()

    def _clean_loop(self):
        while True:
            time.sleep(600)
            try:
                self.cleanup()
            except OSError:
                pass  # The next request also performs cleanup and reports storage failure.

    def _discard(self, job):
        if job.get("leases"):
            job["invalid"] = True
            return
        self.jobs.pop(job["id"], None)
        if self.keys.get(job["key"]) == job["id"]:
            self.keys.pop(job["key"], None)
        for suffix in ("mp3", "part"):
            # Active workers own their .part files until they finish.
            if suffix == "part" and job["status"] in {"queued", "generating"}:
                continue
            (self.directory / (job["id"] + "." + suffix)).unlink(missing_ok=True)

    def cleanup(self):
        with self.lock:
            now = time.time()
            for job in list(self.jobs.values()):
                expired = now - job["created_at"] > (300 if job["status"] == "failed" else self.ttl)
                if job.get("invalid") or (expired and job["status"] not in {"queued", "generating"}):
                    self._discard(job)
            completed = sorted((j for j in self.jobs.values() if j["status"] == "ready"), key=lambda j: j["last_used"])
            total = sum(j.get("bytes", 0) for j in completed)
            for job in completed:
                if total <= self.max_bytes:
                    break
                if not job.get("leases"):
                    total -= job.get("bytes", 0)
                    self._discard(job)

    def stats(self):
        with self.lock:
            completed = [job for job in self.jobs.values() if job["status"] == "ready"]
            generating = [job for job in self.jobs.values() if job["status"] == "generating"]
            return {
                "used_bytes": sum(job["bytes"] for job in completed)
                + sum(job["published_bytes"] for job in generating),
                "max_bytes": self.max_bytes,
                "count": len(completed),
            }

    def stop_listener(self, user_id, listener):
        claim = (user_id, listener)
        aborts = []
        with self.changed:
            now = time.monotonic()
            self.cancelled_listeners[claim] = now
            self.cancelled_listeners.move_to_end(claim)
            # A stop can arrive before its aborted POST reaches the server.
            while self.cancelled_listeners and (
                len(self.cancelled_listeners) > 2048 or next(iter(self.cancelled_listeners.values())) < now - 60
            ):
                self.cancelled_listeners.popitem(last=False)
            for job in list(self.jobs.values()):
                if claim not in job["listeners"]:
                    continue
                job["listeners"].remove(claim)
                if job["status"] in {"queued", "generating"} and not job["listeners"] and not job["legacy_users"]:
                    job["invalid"] = True
                    if job.get("abort"):
                        aborts.append(job["abort"])
                    future = job.pop("future", None)
                    if future is not None and future.cancel():
                        self.slots.release()
                    self._discard(job)
            self.changed.notify_all()
        for abort in aborts:
            abort()

    @contextmanager
    def _cancellable_request(self, job, abort):
        with self.lock:
            if job.get("invalid"):
                raise TTSError("TTS_EXPIRED", "朗读已停止", 410)
            job["abort"] = abort
        try:
            yield
        finally:
            with self.lock:
                job.pop("abort", None)

    def request(self, article_id, user_id, config, text, has_summary, namespace="", valid=None,
                selection=None, speech_config=None, listener=None):
        self.start()
        selection = selection or {}
        language = config.get("lang_code")
        speech_key = speech_preparation.key(namespace, text, speech_config, language)
        segments = speech_segments(text) if speech_key and config.get("stream", True) else [(text, "")]
        plan = [
            (f"{speech_key}:{index}" if len(segments) > 1 else speech_key, segment, context)
            for index, (segment, context) in enumerate(segments)
        ]
        digest = hashlib.sha256((namespace + "\0" + config["revision"] + "\0" + text + "\0" +
                                 json.dumps(selection, sort_keys=True) + "\0" + (speech_key or "")).encode()).hexdigest()
        with self.lock:
            self.cleanup()
            claim = (user_id, listener)
            if listener and time.monotonic() - self.cancelled_listeners.get(claim, -60) < 60:
                raise TTSError("TTS_EXPIRED", "朗读已停止", 410)
            old = self.jobs.get(self.keys.get(digest))
            if old and old["article_id"] == article_id and not old.get("invalid"):
                old["last_used"] = time.time()
                if old["status"] in {"queued", "generating"}:
                    listeners = old["listeners"] if listener else old["legacy_users"]
                    interest = claim if listener else user_id
                    if len(listeners) >= 64 and interest not in listeners:
                        raise TTSError("TTS_BUSY", "语音生成繁忙，请稍后重试", 429)
                    listeners.add(interest)
                    return dict(old)
                if old["status"] == "ready":
                    ready = [speech_preparation.ready(key) for key, _, _ in plan]
                    improved = any(value is not None and hashlib.sha256(value.encode()).hexdigest() != spoken
                                   for value, spoken in zip(ready, old["spoken_hashes"]))
                    if not improved:
                        pending = [index for index in old["unprepared_segments"] if ready[index] is None]
                        for index in pending[:2]:
                            key, segment, context = plan[index]
                            speech_preparation.submit(key, segment, speech_config, language, context)
                        return dict(old)
                if old["status"] == "failed":
                    self._discard(old)
            if any(not j.get("invalid") and j["status"] in {"queued", "generating"} and
                   (user_id in j["legacy_users"] or any(user == user_id for user, _ in j["listeners"]))
                   for j in self.jobs.values()):
                raise TTSError("TTS_BUSY", "已有语音正在生成，请稍后再试", 429)
            if not self.slots.acquire(blocking=False):
                raise TTSError("TTS_BUSY", "语音生成繁忙，请稍后重试", 429)
            job_id = uuid.uuid4().hex
            job = {"id": job_id, "key": digest, "article_id": article_id,
                   "listeners": {claim} if listener else set(), "legacy_users": set() if listener else {user_id},
                   "has_summary": has_summary, "revision": config["revision"],
                   "selection": selection, "segmented": len(plan) > 1,
                   "text_hash": hashlib.sha256(text.encode()).hexdigest(),
                   "status": "queued", "created_at": time.time(), "last_used": time.time(),
                   "leases": 0, "published_bytes": 0}
            self.jobs[job_id] = job
            self.keys[digest] = job_id
            try:
                first_preparation = speech_preparation.submit(plan[0][0], plan[0][1], speech_config, language, plan[0][2])
                job["future"] = self.pool.submit(self._generate, job, config.copy(), plan, valid, speech_config, first_preparation)
            except Exception:
                self._discard(job)
                self.slots.release()
                raise
            return dict(job)

    def _generate(self, job, config, plan, valid, speech_config, first_preparation):
        part = self.directory / (job["id"] + ".part")
        allowance = min(self.max_bytes, 64 * 1024 * 1024)
        reserved = False
        try:
            with self.lock:
                job.pop("future", None)
                if self.jobs.get(job["id"]) is not job or job.get("invalid") or (valid and not valid()):
                    raise TTSError("TTS_EXPIRED", "朗读内容已失效，请重新选择", 410)
                # Account for in-progress downloads as well as completed audio.
                # Reservations prevent two workers temporarily exceeding the
                # spool budget before their finished files can be evicted.
                ready = sorted((j for j in self.jobs.values() if j["status"] == "ready"), key=lambda j: j["last_used"])
                total = sum(j.get("bytes", 0) for j in ready)
                for old in ready:
                    if total + self.reserved_bytes + allowance <= self.max_bytes:
                        break
                    if not old.get("leases"):
                        total -= old.get("bytes", 0)
                        self._discard(old)
                if total + self.reserved_bytes + allowance > self.max_bytes:
                    raise TTSError("TTS_STORAGE_FULL", "语音临时空间繁忙，请稍后重试", 503)
                self.reserved_bytes += allowance
                reserved = True
                job["status"] = "generating"
            def publish(size):
                with self.changed:
                    if self.jobs.get(job["id"]) is not job or job.get("invalid"):
                        raise TTSError("TTS_EXPIRED", "朗读内容已更新，请重新生成语音", 410)
                    job["published_bytes"] = size
                    self.changed.notify_all()

            self._synthesize_plan(job, config, plan, speech_config, first_preparation, part, allowance, publish)
            with self.lock:
                if self.jobs.get(job["id"]) is not job or job.get("invalid") or (valid and not valid()):
                    raise TTSError("TTS_EXPIRED", "朗读内容已更新，请重新生成语音", 410)
                size = part.stat().st_size
                if size > allowance:
                    raise TTSError("TTS_STORAGE_FULL", "语音临时空间繁忙，请稍后重试", 503)
                part.replace(self.directory / (job["id"] + ".mp3"))
                self.reserved_bytes -= allowance
                reserved = False
                job.update(status="ready", bytes=size)
                job["listeners"].clear()
                job["legacy_users"].clear()
                self.changed.notify_all()
        except Exception as exc:
            error = exc if isinstance(exc, TTSError) else TTSError("TTS_STORAGE_ERROR", "语音生成失败，请稍后重试", 503)
            with self.lock:
                job.update(status="failed", error=str(error), code=error.code, http_status=error.status,
                           admin_detail=error.admin_detail)
                job["listeners"].clear()
                job["legacy_users"].clear()
                self.changed.notify_all()
        finally:
            try:
                part.unlink(missing_ok=True)
            finally:
                if reserved:
                    with self.lock:
                        self.reserved_bytes -= allowance
                self.slots.release()

    def _synthesize_plan(self, job, config, plan, speech_config, first_preparation, part, allowance, publish):
        preparations = {0: first_preparation}
        spoken_hashes = []
        unprepared = []
        optimized = 0
        segmented = len(plan) > 1
        language = config.get("lang_code")
        def cancel_scope(abort):
            return self._cancellable_request(job, abort)

        def prepare_ahead(start):
            for index in range(start, min(start + 2, len(plan))):
                if preparations.get(index) is None:
                    key, text, context = plan[index]
                    preparations[index] = speech_preparation.submit(key, text, speech_config, language, context)

        def begin_segment(index, key, text):
            nonlocal optimized
            prepare_ahead(index)
            future = preparations.pop(index)
            prepared = None
            if future is not None:
                try:
                    prepared = future.result(timeout=WAIT_SECONDS if index == 0 else 0)
                except FutureTimeout:
                    pass
            if key is not None and prepared is None:
                unprepared.append(index)
            spoken = prepared or text
            spoken_hashes.append(hashlib.sha256(spoken.encode()).hexdigest())
            optimized += spoken != text
            with self.lock:
                if self.jobs.get(job["id"]) is not job or job.get("invalid"):
                    raise TTSError("TTS_EXPIRED", "朗读内容已更新，请重新生成语音", 410)
                job["preprocessing"] = "ai" if optimized else "rules"
            # The following segments prepare in parallel with this Kokoro request.
            prepare_ahead(index + 1)
            return spoken

        if segmented:
            with part.open("wb") as output:
                audio = MP3Stream(output, allowance, publish)
                for index, (key, text, _) in enumerate(plan):
                    spoken = begin_segment(index, key, text)
                    audio.start_segment()
                    synthesize(config, spoken, None, max_bytes=allowance, write_audio=audio.write,
                               cancel_scope=cancel_scope)
                    audio.finish_segment()
        else:
            spoken = begin_segment(0, plan[0][0], plan[0][1])
            synthesize(config, spoken, part, max_bytes=allowance, on_progress=publish,
                       cancel_scope=cancel_scope)
        with self.lock:
            job.update(spoken_hashes=spoken_hashes, unprepared_segments=unprepared)

    def get(self, job_id):
        with self.lock:
            self.cleanup()
            job = self.jobs.get(job_id)
            if not job or job.get("invalid"):
                raise TTSError("TTS_EXPIRED", "语音已过期，请重新生成", 410)
            job["last_used"] = time.time()
            return dict(job)

    def acquire(self, job_id):
        with self.lock:
            job = self.get(job_id)
            if job["status"] != "ready":
                raise TTSError("TTS_NOT_READY", "语音尚未生成完成", 409)
            path = self.directory / (job_id + ".mp3")
            if not path.is_file():
                self._discard(self.jobs[job_id])
                raise TTSError("TTS_EXPIRED", "语音已过期，请重新生成", 410)
            self.jobs[job_id]["leases"] += 1
            return path

    def stream(self, job_id):
        """Tail only published bytes; a lease protects both the spool and rename."""
        source = None
        leased = False
        position = 0
        try:
            with self.changed:
                self.get(job_id)
                job = self.jobs[job_id]
                job["leases"] += 1
                leased = True
            while True:
                with self.changed:
                    if job.get("invalid"):
                        raise TTSError("TTS_EXPIRED", "语音已过期，请重新生成", 410)
                    if job["status"] == "failed":
                        raise TTSError(job["code"], job["error"], job["http_status"], job.get("admin_detail", ""))
                    complete = job["status"] == "ready"
                    available = job["bytes"] if complete else job["published_bytes"]
                    if position >= available:
                        if complete:
                            return
                        self.changed.wait(timeout=1)
                        continue
                    if source is None:
                        source = (self.directory / (job_id + (".mp3" if complete else ".part"))).open("rb")
                    chunk = source.read(min(16384, available - position))
                    if not chunk:
                        raise TTSError("TTS_STORAGE_ERROR", "语音临时存储不可用，请稍后重试", 503)
                    position += len(chunk)
                yield chunk
        finally:
            if source:
                source.close()
            if leased:
                self.release(job_id)

    def release(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job:
                job["leases"] = max(0, job["leases"] - 1)
                if job.get("invalid"):
                    self._discard(job)

    def invalidate_articles(self, article_ids):
        with self.lock:
            for job in list(self.jobs.values()):
                if job["article_id"] in article_ids:
                    job["invalid"] = True
                    self._discard(job)
            self.changed.notify_all()


cache = TTSCache()
