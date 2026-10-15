"""Small, content-free phase records; no telemetry backend or dynamic labels."""
import json
import logging
import os
import time
import threading
from functools import wraps


def record_phase(logger, phase, started, *, job_id='', **counts):
    payload = {'event': 'phase', 'phase': phase, 'duration_ms': round((time.monotonic()-started)*1000, 3)}
    if job_id:
        payload['job_id'] = str(job_id)[:64]
    for key in ('count', 'inserted', 'failed'):
        if key in counts:
            payload[key] = int(counts[key])
    logger.info(json.dumps(payload, separators=(',', ':')))
    return payload['duration_ms']


def timed_phase(phase):
    def decorate(function):
        @wraps(function)
        def timed(*args, **kwargs):
            started = time.monotonic()
            try:
                return function(*args, **kwargs)
            finally:
                record_phase(logging.getLogger(function.__module__), phase, started,
                             job_id=os.environ.get('FETCH_JOB_ID', ''))
        return timed
    return decorate


_warning_lock = threading.Lock()
_warning_times = {}


def warn_rate_limited(logger, key, message, *, interval=60):
    """Bound repeated operational warnings with a small, fixed-category map."""
    now = time.monotonic()
    category = (logger.name, key)
    with _warning_lock:
        previous = _warning_times.get(category)
        if previous is not None and now-previous < interval:
            return
        if category not in _warning_times and len(_warning_times) >= 32:
            _warning_times.pop(min(_warning_times, key=_warning_times.get))
        _warning_times[category] = now
    logger.warning(message)
