"""Bounded, disposable TTS spool. No audio or job metadata enters the data volume."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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

    def request(self, article_id, user_id, config, text, has_summary, namespace="", valid=None, selection=None):
        self.start()
        selection = selection or {}
        digest = hashlib.sha256((namespace + "\0" + config["revision"] + "\0" + text + "\0" +
                                 json.dumps(selection, sort_keys=True)).encode()).hexdigest()
        with self.lock:
            self.cleanup()
            old = self.jobs.get(self.keys.get(digest))
            if old and old["article_id"] == article_id and not old.get("invalid"):
                old["last_used"] = time.time()
                if old["status"] != "failed":
                    return dict(old)
                self._discard(old)
            if any(j["owner"] == user_id and j["status"] in {"queued", "generating"} for j in self.jobs.values()):
                raise TTSError("TTS_BUSY", "已有语音正在生成，请稍后再试", 429)
            if not self.slots.acquire(blocking=False):
                raise TTSError("TTS_BUSY", "语音生成繁忙，请稍后重试", 429)
            job_id = uuid.uuid4().hex
            job = {"id": job_id, "key": digest, "article_id": article_id, "owner": user_id,
                   "has_summary": has_summary, "revision": config["revision"],
                   "selection": selection,
                   "text_hash": hashlib.sha256(text.encode()).hexdigest(),
                   "status": "queued", "created_at": time.time(), "last_used": time.time(),
                   "leases": 0, "published_bytes": 0}
            self.jobs[job_id] = job
            self.keys[digest] = job_id
            try:
                self.pool.submit(self._generate, job, config.copy(), text, valid)
            except Exception:
                self._discard(job)
                self.slots.release()
                raise
            return dict(job)

    def _generate(self, job, config, text, valid):
        part = self.directory / (job["id"] + ".part")
        allowance = min(self.max_bytes, 64 * 1024 * 1024)
        reserved = False
        try:
            with self.lock:
                if self.jobs.get(job["id"]) is not job or job.get("invalid") or (valid and not valid()):
                    raise TTSError("TTS_EXPIRED", "文章已删除，请选择其他文章", 410)
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
                        raise TTSError("TTS_EXPIRED", "文章已更新或删除，请重新生成语音", 410)
                    job["published_bytes"] = size
                    self.changed.notify_all()

            synthesize(config, text, part, max_bytes=allowance, on_progress=publish)
            with self.lock:
                if self.jobs.get(job["id"]) is not job or job.get("invalid") or (valid and not valid()):
                    raise TTSError("TTS_EXPIRED", "文章已更新或删除，请重新生成语音", 410)
                size = part.stat().st_size
                if size > allowance:
                    raise TTSError("TTS_STORAGE_FULL", "语音临时空间繁忙，请稍后重试", 503)
                part.replace(self.directory / (job["id"] + ".mp3"))
                self.reserved_bytes -= allowance
                reserved = False
                job.update(status="ready", bytes=size)
                self.changed.notify_all()
        except Exception as exc:
            error = exc if isinstance(exc, TTSError) else TTSError("TTS_STORAGE_ERROR", "语音生成失败，请稍后重试", 503)
            with self.lock:
                job.update(status="failed", error=str(error), code=error.code, http_status=error.status,
                           admin_detail=error.admin_detail)
                self.changed.notify_all()
        finally:
            try:
                part.unlink(missing_ok=True)
            finally:
                if reserved:
                    with self.lock:
                        self.reserved_bytes -= allowance
                self.slots.release()

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
