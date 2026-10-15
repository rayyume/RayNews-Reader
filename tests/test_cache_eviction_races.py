"""Exercise filesystem ownership and download coordination, not source spelling."""
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import threading
import time

import pytest
import image_cache as cache

PNG = b'\x89PNG\r\n\x1a\n' + b'x' * 100


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(cache, 'CACHE_DIR', tmp_path / 'images')
    monkeypatch.setattr(cache, 'DB_FILE', tmp_path / 'images' / 'cache.db')
    monkeypatch.setattr(cache, 'IMAGE_CACHE_ENABLED', True)
    monkeypatch.setattr(cache, 'MAX_CACHE_BYTES', 100000)
    monkeypatch.setattr(cache, 'fetch_remote_image', lambda url: (PNG, 'image/png'))
    cache.init_cache()


def read(url):
    reader, kind = cache.cache_image(url, _reader=True)
    with reader:
        return reader.read(), kind


def test_new_file_evicted_before_response_stays_readable(isolated_cache, monkeypatch):
    monkeypatch.setattr(cache, 'MAX_CACHE_BYTES', 1)
    assert read('https://example.test/a') == (PNG, 'image/png')
    assert cache.cache_stats()['used_bytes'] == 0
    assert not list(cache.CACHE_DIR.rglob('.image-*'))


def test_open_reader_survives_concurrent_pruning(isolated_cache, monkeypatch):
    cache.cache_image('https://example.test/a')
    handle, _ = cache.open_cached_image('https://example.test/a')
    monkeypatch.setattr(cache, 'MAX_CACHE_BYTES', 1)
    cache.prune_cache()
    with handle:
        assert handle.read() == PNG


def test_same_url_threads_download_once(isolated_cache, monkeypatch):
    calls = []
    def fetch(url):
        calls.append(url)
        time.sleep(.02)
        return PNG, 'image/png'
    monkeypatch.setattr(cache, 'fetch_remote_image', fetch)
    with ThreadPoolExecutor(max_workers=12) as pool:
        assert all(value == (PNG, 'image/png') for value in pool.map(read, ['https://example.test/a'] * 24))
    assert len(calls) == 1


def test_different_downloads_are_not_globally_serialized(isolated_cache, monkeypatch):
    barrier = threading.Barrier(2)
    def fetch(url):
        barrier.wait(timeout=3)
        return PNG, 'image/png'
    monkeypatch.setattr(cache, 'fetch_remote_image', fetch)
    urls = ['https://example.test/a', 'https://example.test/b']
    assert len({int(cache._url_hash(url)[:4], 16) % 256 for url in urls}) == 2
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(value == (PNG, 'image/png') for value in pool.map(read, urls))


def test_continuous_eviction_with_parallel_readers(isolated_cache, monkeypatch):
    monkeypatch.setattr(cache, 'MAX_CACHE_BYTES', len(PNG) * 2)
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(read, ['https://example.test/' + str(i % 10) for i in range(100)]))
    assert all(value == (PNG, 'image/png') for value in values)
    assert cache.cache_stats()['used_bytes'] <= len(PNG) * 2
    assert not list(cache.CACHE_DIR.rglob('.image-*'))


def test_same_url_processes_download_once(isolated_cache, monkeypatch):
    ctx = multiprocessing.get_context('fork')
    count = ctx.Value('i', 0)
    output = ctx.Queue()
    def fetch(url):
        with count.get_lock():
            count.value += 1
        time.sleep(.03)
        return PNG, 'image/png'
    def worker():
        output.put(read('https://example.test/shared'))
    monkeypatch.setattr(cache, 'fetch_remote_image', fetch)
    processes = [ctx.Process(target=worker) for _ in range(6)]
    try:
        for process in processes:
            process.start()
        assert all(output.get(timeout=5) == (PNG, 'image/png') for _ in processes)
        for process in processes:
            process.join(timeout=5)
            assert process.exitcode == 0
        assert count.value == 1
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
            process.join()
        output.close()


def test_upstream_failure_does_not_publish_partial_file(isolated_cache, monkeypatch):
    def fetch(url):
        raise TimeoutError('upstream timeout')
    monkeypatch.setattr(cache, 'fetch_remote_image', fetch)
    with pytest.raises(TimeoutError):
        read('https://example.test/timeout')
    assert cache.cache_stats()['count'] == 0
    assert not list(cache.CACHE_DIR.rglob('.image-*'))
