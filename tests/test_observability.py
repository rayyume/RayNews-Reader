import json
import logging
import os
import sqlite3

import pytest

import fetcher
import refresh_server
import web_server
from observability import record_phase, timed_phase


@pytest.mark.parametrize('value,expected', [('180',180),('900',900),('86400',86400),('179',900),('86401',900),('bad',900),('-1',900)])
def test_refresh_interval_keeps_default_and_validates_bounds(monkeypatch,value,expected):
    monkeypatch.setenv('REFRESH_INTERVAL_SECONDS',value)
    assert refresh_server._refresh_interval_seconds() == expected


def test_phase_records_only_allowlisted_content_free_fields(caplog):
    logger = logging.getLogger('phase-test')
    with caplog.at_level(logging.INFO):
        record_phase(logger,'stream_commit',0,job_id='job',count=2,password='do-not-log',body='private content')
    event = json.loads(caplog.records[-1].message)
    assert event['job_id'] == 'job' and event['count'] == 2
    assert 'do-not-log' not in caplog.text and 'private content' not in caplog.text


def test_failed_phase_still_records_duration(caplog):
    @timed_phase('article_download')
    def fail():
        raise TimeoutError()
    with caplog.at_level(logging.INFO), pytest.raises(TimeoutError):
        fail()
    assert json.loads(caplog.records[-1].message)['phase'] == 'article_download'


def test_progress_phase_changes_preserve_committed_ids(tmp_path,monkeypatch):
    monkeypatch.setattr(fetcher,'OUTPUT_DIR',tmp_path)
    monkeypatch.setattr(fetcher,'PROGRESS_FILE',tmp_path/'progress.json')
    fetcher.write_fetch_progress(2,5,[1,2])
    fetcher._set_fetch_phase('json_mirror')
    data = json.loads(fetcher.PROGRESS_FILE.read_text())
    assert data['stage'] == 'json_mirror'
    assert data['inserted_ids'] == [1,2] and data['inserted'] == 2


def test_request_id_validation_timing_and_private_result_errors():
    client = web_server.app.test_client()
    response = client.get('/ai/result/1',headers={'X-Request-ID':'qa-request-1'})
    assert response.headers['X-Request-ID'] == 'qa-request-1'
    assert response.headers['Server-Timing'].startswith('app;dur=')
    assert response.headers['Cache-Control'] == 'private, no-store'
    invalid = client.get('/ai/result/1',headers={'X-Request-ID':'not a valid id'})
    assert invalid.headers['X-Request-ID'] != 'not a valid id'


def test_repeated_image_warning_is_bounded(caplog,monkeypatch):
    import observability
    monkeypatch.setattr(observability,'_warning_times',{})
    logger=logging.getLogger('image-warning-test')
    with caplog.at_level(logging.WARNING):
        for _ in range(100):
            observability.warn_rate_limited(logger,'unavailable','Image cache unavailable')
    assert len(caplog.records)==1
