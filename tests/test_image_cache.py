import io
import sqlite3
import tempfile
import sys
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import image_cache
from image_validation import detect_image_content_type
import network_safety
import refresh_server


SSPAI_INNER = (
    "https://cdnfile.sspai.com/6/12/2026/article/"
    "e88eb0d5-16e0-8817-03f2-a79df52905cc.jpg"
)
SSPAI_WSRV = "https://wsrv.nl/?url=" + SSPAI_INNER


class RemoteImageCandidateTests(unittest.TestCase):
    def test_sspai_wsrv_falls_back_to_rssfile_then_original(self):
        candidates = image_cache._remote_image_candidates(SSPAI_WSRV)

        self.assertEqual(candidates[0], SSPAI_WSRV)
        self.assertEqual(
            candidates[1],
            SSPAI_INNER.replace("cdnfile.sspai.com", "rssfile.sspai.com"),
        )
        self.assertEqual(candidates[2], SSPAI_INNER)

    def test_non_sspai_wsrv_only_unwraps_original(self):
        inner = "https://example.com/path/image.jpg"
        wrapped = "https://wsrv.nl/?url=" + inner

        self.assertEqual(
            image_cache._remote_image_candidates(wrapped),
            [wrapped, inner],
        )

    def test_invalid_wsrv_inner_url_is_ignored(self):
        wrapped = "https://wsrv.nl/?url=file%3A%2F%2F%2Fetc%2Fpasswd"

        self.assertEqual(image_cache._remote_image_candidates(wrapped), [wrapped])

    def test_fetch_uses_sspai_fallback_after_wsrv_failure(self):
        failed = mock.Mock()
        failed.raise_for_status.side_effect = RuntimeError("404")
        succeeded = mock.Mock()
        succeeded.headers = {"Content-Type": "image/jpeg"}
        succeeded.raise_for_status.return_value = None
        succeeded.iter_content.return_value = [b"\xff\xd8\xffvalid-jpeg"]

        with mock.patch.object(
            network_safety,
            "_send_bound_request",
            side_effect=[failed, succeeded],
        ) as request_get:
            body, content_type = image_cache.fetch_remote_image(SSPAI_WSRV)

        self.assertEqual(body, b"\xff\xd8\xffvalid-jpeg")
        self.assertEqual(content_type, "image/jpeg")
        self.assertEqual(
            request_get.call_args_list[1].args[0],
            SSPAI_INNER.replace("cdnfile.sspai.com", "rssfile.sspai.com"),
        )

    def test_fetch_uses_referer_for_sspai_cdn_after_rssfile_failure(self):
        failed_wsrv = mock.Mock()
        failed_wsrv.raise_for_status.side_effect = RuntimeError("wsrv 404")
        failed_rss = mock.Mock()
        failed_rss.raise_for_status.side_effect = RuntimeError("rss 404")
        succeeded = mock.Mock()
        succeeded.headers = {"Content-Type": "image/jpeg"}
        succeeded.raise_for_status.return_value = None
        succeeded.iter_content.return_value = [b"\xff\xd8\xffvalid-jpeg"]

        with mock.patch.object(
            network_safety,
            "_send_bound_request",
            side_effect=[failed_wsrv, failed_rss, succeeded],
        ) as request_get:
            body, content_type = image_cache.fetch_remote_image(SSPAI_WSRV)

        self.assertEqual((body, content_type), (b"\xff\xd8\xffvalid-jpeg", "image/jpeg"))
        final_call = request_get.call_args_list[2]
        self.assertEqual(final_call.args[0], SSPAI_INNER)
        self.assertEqual(
            final_call.kwargs["headers"]["Referer"],
            "https://cdnfile.sspai.com/",
        )


class StartupWarmupTests(unittest.TestCase):
    def test_startup_warmup_only_queues_today_articles_with_wsrv_images(self):
        beijing = timezone(timedelta(hours=8))
        today = datetime.now(beijing).strftime("%Y-%m-%d")
        yesterday = (datetime.now(beijing) - timedelta(days=1)).strftime("%Y-%m-%d")

        db_path = ROOT / f"tmp-image-cache-test-{uuid.uuid4().hex}.db"
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE articles (
                    id INTEGER PRIMARY KEY,
                    date TEXT,
                    thumb TEXT,
                    body_html TEXT,
                    timestamp INTEGER DEFAULT 0
                )
                """
            )
            conn.executemany(
                "INSERT INTO articles (id, date, thumb, body_html) VALUES (?, ?, ?, ?)",
                [
                    (1, today, SSPAI_WSRV, "<p>today cover</p>"),
                    (2, today, "", f'<img src="{SSPAI_WSRV}"><img src="https://example.com/other.jpg">'),
                    (3, today, "https://example.com/ok.jpg", "<p>no wsrv</p>"),
                    (4, yesterday, SSPAI_WSRV, "<p>old</p>"),
                ],
            )
            conn.commit()
            conn.close()

            with (
                mock.patch.object(refresh_server, "DB_FILE", db_path),
                mock.patch.object(
                    refresh_server,
                    "enqueue_article_image_prefetch",
                    return_value=2,
                ) as enqueue,
            ):
                result = refresh_server.enqueue_today_wsrv_article_images()
        finally:
            for suffix in ("", "-journal", "-wal", "-shm"):
                Path(str(db_path) + suffix).unlink(missing_ok=True)

        self.assertEqual(result, {"articles": 2, "queued": 4})
        self.assertEqual([call.args[0] for call in enqueue.call_args_list], [1, 2])
        for call in enqueue.call_args_list:
            self.assertIsNone(call.kwargs["body_limit"])

class CacheInitializationLifecycleTests(unittest.TestCase):
    def _install_recording_connections(self, cache_db):
        calls = {"execute": 0, "script": 0, "commit": 0}

        class RecordingConnection:
            def execute(self, statement, *args):
                if statement.startswith("PRAGMA"):
                    calls["execute"] += 1

            def executescript(self, script):
                calls["script"] += 1

            def commit(self):
                calls["commit"] += 1

            def close(self):
                pass

        def connect(*args, **kwargs):
            cache_db.touch(exist_ok=True)
            return RecordingConnection()

        return calls, connect

    def test_concurrent_init_for_same_existing_path_runs_schema_setup_once(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory) / "image_cache"
            cache_db = cache_dir / "cache.db"
            calls, connect = self._install_recording_connections(cache_db)
            start = threading.Barrier(2)
            with (
                mock.patch.object(image_cache, "CACHE_DIR", cache_dir),
                mock.patch.object(image_cache, "DB_FILE", cache_db),
                mock.patch.object(image_cache, "_initialized_cache_paths", set(), create=True),
                mock.patch.object(image_cache.sqlite3, "connect", side_effect=connect),
                ThreadPoolExecutor(max_workers=2) as workers,
            ):
                list(workers.map(lambda _unused: (start.wait(), image_cache.init_cache()), range(2)))

        self.assertEqual(calls["execute"], 1)
        self.assertEqual(calls["script"], 1)
        self.assertEqual(calls["commit"], 1)

    def test_db_file_change_initializes_each_path_only_once(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            first_dir = Path(directory) / "first"
            second_dir = Path(directory) / "second"
            first_db = first_dir / "cache.db"
            second_db = second_dir / "cache.db"
            calls, connect = self._install_recording_connections(first_db)

            def connect_for_current_path(path, *args, **kwargs):
                Path(path).touch(exist_ok=True)
                return connect(path, *args, **kwargs)

            with (
                mock.patch.object(image_cache, "CACHE_DIR", first_dir),
                mock.patch.object(image_cache, "DB_FILE", first_db),
                mock.patch.object(image_cache, "_initialized_cache_paths", set(), create=True),
                mock.patch.object(image_cache.sqlite3, "connect", side_effect=connect_for_current_path),
            ):
                image_cache.init_cache()
                image_cache.init_cache()
                with (
                    mock.patch.object(image_cache, "CACHE_DIR", second_dir),
                    mock.patch.object(image_cache, "DB_FILE", second_db),
                ):
                    image_cache.init_cache()
                    image_cache.init_cache()

        self.assertEqual(calls["script"], 2)
        self.assertEqual(calls["commit"], 2)

    def test_deleted_cache_database_is_reinitialized(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory) / "image_cache"
            cache_db = cache_dir / "cache.db"
            with (
                mock.patch.object(image_cache, "CACHE_DIR", cache_dir),
                mock.patch.object(image_cache, "DB_FILE", cache_db),
                mock.patch.object(image_cache, "_initialized_cache_paths", set(), create=True),
            ):
                image_cache.init_cache()
                cache_db.unlink()
                image_cache.init_cache()

            self.assertTrue(cache_db.exists())
            conn = sqlite3.connect(cache_db)
            try:
                self.assertIsNotNone(
                    conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'image_cache_entries'"
                    ).fetchone()
                )
            finally:
                conn.close()

    def test_failed_schema_setup_is_not_marked_initialized(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory) / "image_cache"
            cache_db = cache_dir / "cache.db"
            calls = {"script": 0}

            class FailingOnceConnection:
                def execute(self, statement, *args):
                    pass

                def executescript(self, script):
                    calls["script"] += 1
                    if calls["script"] == 1:
                        raise sqlite3.OperationalError("setup failed")

                def commit(self):
                    pass

                def close(self):
                    pass

            def connect(*args, **kwargs):
                cache_db.touch(exist_ok=True)
                return FailingOnceConnection()

            with (
                mock.patch.object(image_cache, "CACHE_DIR", cache_dir),
                mock.patch.object(image_cache, "DB_FILE", cache_db),
                mock.patch.object(image_cache, "_initialized_cache_paths", set(), create=True),
                mock.patch.object(image_cache.sqlite3, "connect", side_effect=connect),
            ):
                with self.assertRaisesRegex(sqlite3.OperationalError, "setup failed"):
                    image_cache.init_cache()
                image_cache.init_cache()

        self.assertEqual(calls["script"], 2)


class ImageContentValidationTests(unittest.TestCase):
    JPEG = b"\xff\xd8\xffvalid-jpeg"
    PNG = b"\x89PNG\r\n\x1a\nvalid-png"
    GIF = b"GIF89avalid-gif"
    WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 valid-webp"

    def test_detects_supported_image_magic_bytes(self):
        cases = {
            self.JPEG: "image/jpeg",
            self.PNG: "image/png",
            self.GIF: "image/gif",
            self.WEBP: "image/webp",
        }

        for data, content_type in cases.items():
            with self.subTest(content_type=content_type):
                self.assertEqual(detect_image_content_type(data), content_type)

    def test_fetch_uses_detected_type_when_declared_type_is_wrong(self):
        response = mock.Mock()
        response.headers = {"Content-Type": "image/jpeg"}
        response.raise_for_status.return_value = None
        response.iter_content.return_value = [self.PNG]

        with (
            mock.patch.object(image_cache, "_remote_image_candidates", return_value=["https://public.example/image"]),
            mock.patch.object(image_cache, "safe_get", return_value=response),
        ):
            body, content_type = image_cache.fetch_remote_image("https://public.example/image")

        self.assertEqual((body, content_type), (self.PNG, "image/png"))
        response.close.assert_called_once_with()

    def test_cache_uses_detected_type_for_path_and_record(self):
        response = mock.Mock()
        response.headers = {"Content-Type": "image/jpeg"}
        response.raise_for_status.return_value = None
        response.iter_content.return_value = [self.PNG]

        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory) / "image_cache"
            db_file = cache_dir / "cache.db"
            with (
                mock.patch.object(image_cache, "CACHE_DIR", cache_dir),
                mock.patch.object(image_cache, "DB_FILE", db_file),
                mock.patch.object(image_cache, "_initialized_cache_paths", set()),
                mock.patch.object(image_cache, "_remote_image_candidates", return_value=["https://public.example/image"]),
                mock.patch.object(image_cache, "safe_get", return_value=response),
            ):
                path, content_type = image_cache.cache_image("https://public.example/image")
                self.assertEqual((path.suffix, content_type), (".png", "image/png"))
                self.assertEqual(path.read_bytes(), self.PNG)
                conn = sqlite3.connect(db_file)
                try:
                    self.assertEqual(
                        conn.execute("SELECT content_type FROM image_cache_entries").fetchone()[0],
                        "image/png",
                    )
                finally:
                    conn.close()

    def test_non_image_body_is_not_cached_or_recorded(self):
        response = mock.Mock()
        response.headers = {"Content-Type": "image/jpeg"}
        response.raise_for_status.return_value = None
        response.iter_content.return_value = [b"<html>not an image</html>"]

        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory) / "image_cache"
            db_file = cache_dir / "cache.db"
            with (
                mock.patch.object(image_cache, "CACHE_DIR", cache_dir),
                mock.patch.object(image_cache, "DB_FILE", db_file),
                mock.patch.object(image_cache, "_initialized_cache_paths", set()),
                mock.patch.object(image_cache, "_remote_image_candidates", return_value=["https://public.example/image"]),
                mock.patch.object(image_cache, "safe_get", return_value=response),
                self.assertRaisesRegex(ValueError, "unsupported image content"),
            ):
                image_cache.cache_image("https://public.example/image")

            self.assertFalse(list(cache_dir.rglob("*.jpg")))
            self.assertFalse(list(cache_dir.rglob("*.png")))
            conn = sqlite3.connect(db_file)
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM image_cache_entries").fetchone()[0], 0)
            finally:
                conn.close()

    def test_fetch_tries_next_safe_candidate_after_non_image_body(self):
        rejected = mock.Mock()
        rejected.headers = {"Content-Type": "image/jpeg"}
        rejected.raise_for_status.return_value = None
        rejected.iter_content.return_value = [b"<html>not an image</html>"]
        succeeded = mock.Mock()
        succeeded.headers = {"Content-Type": "text/plain"}
        succeeded.raise_for_status.return_value = None
        succeeded.iter_content.return_value = [self.GIF]

        with (
            mock.patch.object(
                image_cache,
                "_remote_image_candidates",
                return_value=["https://public.example/first", "https://public.example/second"],
            ),
            mock.patch.object(image_cache, "safe_get", side_effect=[rejected, succeeded]),
        ):
            self.assertEqual(
                image_cache.fetch_remote_image("https://public.example/first"),
                (self.GIF, "image/gif"),
            )

        rejected.close.assert_called_once_with()
        succeeded.close.assert_called_once_with()

    def _handler(self):
        handler = refresh_server.Handler.__new__(refresh_server.Handler)
        handler.wfile = io.BytesIO()
        handler.statuses = []
        handler.headers_sent = []
        handler.send_response = handler.statuses.append
        handler.send_header = lambda name, value: handler.headers_sent.append((name, value))
        handler.end_headers = lambda: None
        return handler

    def test_img_cache_returns_502_for_non_image_fetch_failure(self):
        handler = self._handler()
        with (
            mock.patch.object(refresh_server, "open_cached_image", return_value=None),
            mock.patch.object(refresh_server, "cache_image", side_effect=ValueError("unsupported image content")),
        ):
            handler._handle_img_cache({"url": ["https://public.example/image"]})

        self.assertEqual(handler.statuses, [502])

    def test_img_cache_success_responses_include_nosniff(self):
        body = self.JPEG
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cached.jpg"
            path.write_bytes(body)
            for cached in (True, False):
                with self.subTest(cached=cached):
                    handler = self._handler()
                    with (
                        mock.patch.object(refresh_server, "open_cached_image", return_value=(path.open('rb'), "image/jpeg") if cached else None),
                        mock.patch.object(refresh_server, "cache_image", return_value=None),
                        mock.patch.object(refresh_server, "fetch_remote_image", return_value=(body, "image/jpeg")),
                    ):
                        handler._handle_img_cache({"url": ["https://public.example/image"]})
                    self.assertEqual(handler.statuses, [200])
                    self.assertEqual(handler.wfile.getvalue(), body)
                    self.assertIn(("X-Content-Type-Options", "nosniff"), handler.headers_sent)



if __name__ == "__main__":
    unittest.main()
