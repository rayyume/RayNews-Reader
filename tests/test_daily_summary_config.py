"""Admin scheduling, local cutoff dates, and global delivery gates."""
import datetime as dt
import json
import sqlite3
from zoneinfo import ZoneInfo

import pytest

import ai_access
import daily_summary_config
import models
import web_server


@pytest.fixture
def app(tmp_path, monkeypatch):
    models.close_db()
    monkeypatch.setattr(models, 'DB_FILE', tmp_path/'accounts.db')
    monkeypatch.setattr(web_server, 'NEWS_DB', str(tmp_path/'news.db'))
    monkeypatch.setenv('TZ', 'Asia/Shanghai')
    monkeypatch.delenv('RESEND_API_KEY', raising=False)
    sqlite3.connect(web_server.NEWS_DB).close()
    admin = models.create_user('admin@digest.test', 'pw', 'Admin', 'admin')['id']
    user = models.create_user('reader@digest.test', 'pw', 'Reader')['id']
    ai_access.set_permissions(user, {'daily_digest':True})
    for uid in (admin, user):
        models.set_user_settings(uid, view_daily_digest=1)
    monkeypatch.setattr(web_server, '_daily_summary_now',
                        lambda:dt.datetime(2026, 10, 21, 21, 1, tzinfo=ZoneInfo('Asia/Shanghai')))
    headers = lambda uid, role: {'Authorization':'Bearer '+web_server.create_token(uid, role)}
    yield web_server.app.test_client(), headers(admin, 'admin'), headers(user, 'user')
    models.close_db()


def test_settings_are_admin_only_persistent_and_follow_deployment_timezone(app, monkeypatch):
    client, admin, reader = app
    path = '/admin/daily-summary-config'
    assert client.get(path).status_code == 401
    assert client.get(path, headers=reader).status_code == 403
    assert client.put(path, headers=reader, json={'enabled':False}).status_code == 403
    response = client.get(path, headers=admin)
    assert response.headers['Cache-Control'] == 'private, no-store'
    assert response.json == {'enabled':True, 'generate_at':'21:00', 'delivery_method':'both',
                             'timezone':'Asia/Shanghai', 'email_configured':False}
    payload = {'enabled':False, 'generate_at':'08:45', 'delivery_method':'inapp'}
    assert client.put(path, headers=admin, json=payload).status_code == 200
    models.close_db()  # A fresh connection still sees the saved settings.
    saved = client.get(path, headers=admin).json
    assert all(saved[key] == value for key, value in payload.items())
    monkeypatch.setenv('TZ', 'Pacific/Auckland')
    monkeypatch.setenv('RESEND_API_KEY', 'test-key')
    saved = client.get(path, headers=admin).json
    assert saved['timezone'] == 'Pacific/Auckland' and saved['email_configured']
    assert 'test-key' not in str(saved)
    assert client.put(path, headers=admin, json={'enabled':True}).json['generate_at'] == '08:45'


@pytest.mark.parametrize('payload', [None, [], {'unknown':True}, {'enabled':1}, {'enabled':'false'},
    {'generate_at':'24:00'}, {'generate_at':'8:30'}, {'generate_at':'12:60'}, {'generate_at':'00:00\n'},
    {'generate_at':None}, {'delivery_method':'none'}, {'delivery_method':[]}, {'timezone':'UTC'}])
def test_invalid_settings_do_not_modify_saved_configuration(app, payload):
    client, admin, _ = app
    path = '/admin/daily-summary-config'
    before = client.get(path, headers=admin).json
    assert client.put(path, headers=admin, json=payload).status_code == 400
    assert client.get(path, headers=admin).json == before


def _capture_generation(monkeypatch):
    calls = []
    def generate(day):
        calls.append(('generate', day))
        return {'summary':'今日要闻', 'stats':{}, 'article_count':1}
    monkeypatch.setattr(web_server, '_generate_daily_summary_global', generate)
    monkeypatch.setattr(web_server, '_deliver_daily_summary_inapp',
                        lambda day, result:(calls.append(('inapp', day)), {'status':'ok', 'recipients':1})[1])
    monkeypatch.setattr(web_server, '_deliver_daily_summary_email',
                        lambda day, result, force=False:(calls.append(('email', day)), {'status':'ok', 'sent':1})[1])
    return calls


@pytest.mark.parametrize('method,channels', [('inapp',['inapp']), ('email',['email']), ('both',['inapp','email'])])
def test_scheduler_uses_saved_time_local_date_and_selected_channels(app, monkeypatch, method, channels):
    monkeypatch.setenv('TZ', 'Pacific/Auckland')
    daily_summary_config.save_config({'generate_at':'08:45', 'delivery_method':method})
    calls = _capture_generation(monkeypatch)
    monkeypatch.setattr(web_server, '_daily_summary_now',
                        lambda:dt.datetime(2026, 10, 22, 8, 44, tzinfo=ZoneInfo('Pacific/Auckland')))
    assert web_server._send_daily_summaries()['status'] == 'skipped'
    assert calls == []
    monkeypatch.setattr(web_server, '_daily_summary_now',
                        lambda:dt.datetime(2026, 10, 22, 8, 46, tzinfo=ZoneInfo('Pacific/Auckland')))
    assert web_server._send_daily_summaries()['status'] == 'ok'
    assert calls == [(channel,'2026-10-22') for channel in ['generate',*channels]]
    start, cutoff = web_server._digest_window('2026-10-22')
    assert cutoff == int(dt.datetime(2026, 10, 22, 8, 45, tzinfo=ZoneInfo('Pacific/Auckland')).timestamp())
    assert cutoff-start == 86400


def test_disabled_service_blocks_generation_retries_and_both_delivery_helpers(app, monkeypatch):
    client, admin, _ = app
    failure = web_server._record_daily_summary_failure('2026-10-21', 'temporary failure')
    daily_summary_config.save_config({'enabled':False})
    monkeypatch.setattr(web_server, '_generate_daily_summary_global', lambda day:pytest.fail('Generation must be paused'))
    assert web_server._send_daily_summaries()['reason'] == 'daily summary disabled'
    assert web_server._broadcast_daily_summary(force=True)['status'] == 'skipped'
    assert web_server._broadcast_daily_summary(bypass_window=True)['status'] == 'skipped'
    assert web_server._deliver_daily_summary_inapp('2026-10-21', {})['status'] == 'skipped'
    assert web_server._deliver_daily_summary_email('2026-10-21', {})['status'] == 'skipped'
    response = client.post('/ai/daily-summary/retry', headers=admin)
    assert response.status_code == 409 and response.json['status'] == 'server_disabled'
    assert web_server._get_daily_summary_failure('2026-10-21')['attempts'] == failure['attempts']


def test_disabling_during_generation_prevents_delivery(app, monkeypatch):
    calls = _capture_generation(monkeypatch)
    def generate(day):
        daily_summary_config.save_config({'enabled':False})
        return {'summary':'已生成的内容', 'stats':{}, 'article_count':1}
    monkeypatch.setattr(web_server, '_generate_daily_summary_global', generate)
    outcome = web_server._broadcast_daily_summary()
    assert calls == [] and outcome['inapp']['status'] == 'skipped'


@pytest.mark.parametrize('method', ['inapp','email','both'])
def test_real_delivery_helpers_apply_global_channels_and_user_subscriptions(app, monkeypatch, method):
    import notifier
    user = models.get_user_by_email('reader@digest.test')['id']
    models.set_user_settings(user, daily_summary_enabled=1, daily_summary_inapp_enabled=1,
                             notification_config=json.dumps({'resend':{'to_email':'reader@digest.test'}}))
    monkeypatch.setenv('RESEND_API_KEY', 'test-key')
    sent = []
    monkeypatch.setattr(notifier, 'send_daily_summary_email', lambda *args, **kw:sent.append((args, kw)))
    daily_summary_config.save_config({'delivery_method':method})
    result = {'summary':'今日要闻', 'stats':{}, 'article_count':1}
    for _ in range(2):
        web_server._deliver_daily_summary_inapp('2026-10-21', result)
        web_server._deliver_daily_summary_email('2026-10-21', result)
    assert len(models.list_notifications(user)) == (0 if method == 'email' else 1)
    assert len(sent) == (0 if method == 'inapp' else 1)
    if sent:
        assert sent[0][0][1] == 'reader@digest.test'
        assert sent[0][1]['idempotency_key'] == f'daily-2026-10-21-{user}'
    # Personal opt-outs remain effective when the global channels are enabled.
    models.set_user_settings(user, daily_summary_enabled=0, daily_summary_inapp_enabled=0)
    before = len(sent)
    web_server._deliver_daily_summary_inapp('2026-10-22', result)
    web_server._deliver_daily_summary_email('2026-10-22', result)
    assert len(models.list_notifications(user)) == (0 if method == 'email' else 1)
    assert len(sent) == before


def test_reader_receives_real_schedule_and_server_pause_preserves_completed_content(app):
    client, admin, reader = app
    client.put('/admin/daily-summary-config', headers=admin,
               json={'generate_at':'22:30', 'delivery_method':'inapp'})
    response = client.get('/ai/daily-summary/today', headers=reader)
    assert response.json['generate_at'] == '22:30'
    assert response.json['timezone'] == 'Asia/Shanghai'
    assert response.json['status'] == 'scheduled'
    client.put('/admin/daily-summary-config', headers=admin, json={'enabled':False})
    assert client.get('/ai/daily-summary/today', headers=reader).json['status'] == 'server_disabled'
    # Before 22:30 the most recent scheduled task is still the previous day's.
    assert web_server._save_daily_summary_global_cache('2026-10-20', '已生成的内容', 1, {})
    data = client.get('/ai/daily-summary/today', headers=reader).json
    assert data['status'] == 'completed' and data['summary'] == '已生成的内容'
    assert data['date'] == '2026-10-20'
    assert data['server_enabled'] is False
    status = client.get('/admin/scheduler/status', headers=admin).json
    assert status['daily_summary_send_time'] == '22:30'
    assert status['timezone_hint'] == 'Asia/Shanghai'
    assert status['daily_summary_enabled'] is False


def test_midnight_window_and_retries_keep_the_original_digest_date(app, monkeypatch):
    monkeypatch.setenv('TZ', 'UTC')
    daily_summary_config.save_config({'generate_at':'23:58'})
    clock = {'now':dt.datetime(2026, 10, 22, 0, 1, tzinfo=dt.timezone.utc), 'epoch':1000}
    monkeypatch.setattr(web_server, '_daily_summary_now', lambda:clock['now'])
    monkeypatch.setattr(web_server, '_epoch_now', lambda:clock['epoch'])
    calls = _capture_generation(monkeypatch)
    web_server._send_daily_summaries()
    assert calls[0] == ('generate','2026-10-21')
    clock['now'] = clock['now'].replace(minute=12)
    assert web_server._broadcast_daily_summary()['status'] == 'skipped'
    web_server._record_daily_summary_failure('2026-10-21', 'temporary failure')
    clock['epoch'] += web_server.DAILY_SUMMARY_RETRY_INTERVAL_SECONDS + 1
    web_server._send_daily_summaries()
    assert calls[-3] == ('generate','2026-10-21')
    assert web_server._get_daily_summary_failure('2026-10-21') is None


@pytest.mark.parametrize('day,hours', [('2026-03-08',23), ('2026-11-01',25)])
def test_cutoff_windows_follow_daylight_saving_time(app, monkeypatch, day, hours):
    monkeypatch.setenv('TZ', 'America/New_York')
    start, end = web_server._digest_window(day)
    assert end-start == hours*3600


@pytest.mark.parametrize('new_time', ['08:00','23:45'])
def test_time_changes_connect_to_the_previous_actual_cutoff(app, new_time):
    previous_end = int(dt.datetime(2026, 10, 20, 21, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp())
    assert web_server._save_daily_summary_global_cache('2026-10-20', '昨日总结', 1, {'window_end':previous_end})
    daily_summary_config.save_config({'generate_at':new_time})
    start, end = web_server._digest_window('2026-10-21')
    assert start == previous_end
    assert end == int(web_server._daily_summary_cutoff(dt.date(2026,10,21)).timestamp())


def test_repeated_dst_hour_does_not_create_a_second_send_window(app, monkeypatch):
    monkeypatch.setenv('TZ', 'America/New_York')
    daily_summary_config.save_config({'generate_at':'01:30'})
    calls = _capture_generation(monkeypatch)
    for fold in (0,1):
        monkeypatch.setattr(web_server, '_daily_summary_now',
            lambda fold=fold:dt.datetime(2026, 11, 1, 1, 31, tzinfo=ZoneInfo('America/New_York'), fold=fold))
        web_server._send_daily_summaries()
    assert [call for call in calls if call[0]=='generate'] == [('generate','2026-11-01')]


@pytest.fixture(params=['UTC', 'Asia/Shanghai'])
def midnight_clock(app, monkeypatch, request):
    monkeypatch.setenv('TZ', request.param)
    daily_summary_config.save_config({'generate_at':'23:58'})
    clock = {'now':dt.datetime(2026, 10, 22, 0, 1, tzinfo=ZoneInfo(request.param))}
    monkeypatch.setattr(web_server, '_daily_summary_now', lambda:clock['now'])
    monkeypatch.setattr(web_server, '_epoch_now', lambda:int(clock['now'].timestamp()))
    return clock


def test_midnight_status_tracks_the_scheduler_while_generating_and_after_completion(
        app, midnight_clock, monkeypatch):
    client, admin, reader = app
    calls = _capture_generation(monkeypatch)
    generate = web_server._generate_daily_summary_global

    def generate_and_save(day):
        for headers in (admin, reader):
            response = client.get('/ai/daily-summary/today', headers=headers)
            assert response.status_code == 200
            assert response.json['date'] == day == '2026-10-21'
            assert response.json['status'] == 'generating'
        result = generate(day)
        assert web_server._save_daily_summary_global_cache(
            day, result['summary'], result['article_count'], result['stats'])
        return result

    monkeypatch.setattr(web_server, '_generate_daily_summary_global', generate_and_save)
    assert web_server._send_daily_summaries()['status'] == 'ok'
    assert calls == [(channel, '2026-10-21') for channel in ('generate', 'inapp', 'email')]
    for headers in (admin, reader):
        response = client.get('/ai/daily-summary/today', headers=headers)
        assert response.json['status'] == 'completed'
        assert response.json['date'] == '2026-10-21'
        assert response.json['summary'] == '今日要闻'
    assert client.get('/admin/daily-digest/events', headers=admin).json['date'] == '2026-10-21'
    web_server._record_daily_summary_send('2026-10-21', 42, 'sent@digest.test', 'sent')
    status = client.get('/admin/scheduler/status', headers=admin).json
    assert status['today'] == '2026-10-21'
    assert status['daily_summary_sent_user_ids_today'] == [42]


@pytest.mark.parametrize('given_up', [False, True])
def test_midnight_failed_status_and_manual_retry_use_the_original_task_date(
        app, midnight_clock, monkeypatch, given_up):
    client, admin, reader = app
    for _ in range(1 + (web_server.DAILY_SUMMARY_MAX_RETRIES if given_up else 0)):
        web_server._record_daily_summary_failure('2026-10-21', 'provider temporarily unavailable')
    failure = web_server._get_daily_summary_failure('2026-10-21')
    admin_status = client.get('/ai/daily-summary/today', headers=admin).json
    assert admin_status['date'] == '2026-10-21'
    assert admin_status['status'] == ('failed' if given_up else 'retrying')
    assert admin_status['can_retry'] is True
    assert admin_status['attempts'] == failure['attempts']
    reader_status = client.get('/ai/daily-summary/today', headers=reader).json
    assert reader_status['date'] == '2026-10-21'
    assert reader_status['status'] == ('unavailable' if given_up else 'generating')
    assert 'error' not in reader_status and 'can_retry' not in reader_status
    assert client.post('/ai/daily-summary/retry', headers=reader).status_code == 403
    calls = _capture_generation(monkeypatch)
    response = client.post('/ai/daily-summary/retry', headers=admin)
    assert response.status_code == 200
    assert response.json['status'] == 'completed'
    assert response.json['date'] == '2026-10-21'
    assert calls == [(channel, '2026-10-21') for channel in ('generate', 'inapp', 'email')]
    assert web_server._get_daily_summary_failure('2026-10-21') is None
    assert web_server._get_daily_summary_failure('2026-10-22') is None


def test_midnight_failed_manual_retry_remains_available_to_the_automatic_retry_chain(
        app, midnight_clock, monkeypatch):
    client, admin, _ = app
    web_server._record_daily_summary_failure('2026-10-21', 'initial failure')
    def failing(day):
        assert day == '2026-10-21'
        web_server._set_daily_summary_error('provider still unavailable')
        return None
    monkeypatch.setattr(web_server, '_generate_daily_summary_global', failing)
    response = client.post('/ai/daily-summary/retry', headers=admin)
    assert response.status_code == 502
    assert response.json['date'] == '2026-10-21'
    assert response.json['status'] == 'retrying' and response.json['attempts'] == 1
    assert web_server._get_daily_summary_failure('2026-10-22') is None
    midnight_clock['now'] = midnight_clock['now'].replace(minute=12)
    status = client.get('/ai/daily-summary/today', headers=admin).json
    assert status['date'] == '2026-10-21' and status['status'] == 'retrying'
    calls = _capture_generation(monkeypatch)
    assert web_server._send_daily_summaries()['status'] == 'ok'
    assert calls[0] == ('generate', '2026-10-21')
    assert web_server._get_daily_summary_failure('2026-10-21') is None


def test_task_date_rolls_over_at_the_next_scheduled_time_not_at_midnight(
        app, midnight_clock, monkeypatch):
    client, admin, _ = app
    failure = web_server._record_daily_summary_failure('2026-10-21', 'previous run failed')
    midnight_clock['now'] = midnight_clock['now'].replace(hour=23, minute=57, second=59)
    assert client.get('/ai/daily-summary/today', headers=admin).json['date'] == '2026-10-21'
    midnight_clock['now'] += dt.timedelta(seconds=1)
    status = client.get('/ai/daily-summary/today', headers=admin).json
    assert status['date'] == '2026-10-22' and status['status'] == 'generating'
    response = client.post('/ai/daily-summary/retry', headers=admin)
    assert response.status_code == 409 and response.json['status'] == 'not_failed'
    assert response.json['date'] == '2026-10-22'
    assert web_server._get_daily_summary_failure('2026-10-21') == failure
    calls = _capture_generation(monkeypatch)
    assert web_server._send_daily_summaries()['status'] == 'ok'
    assert calls[0] == ('generate', '2026-10-22')


def test_scheduler_keeps_one_task_date_when_a_tick_straddles_the_cutoff(app, monkeypatch):
    daily_summary_config.save_config({'generate_at':'23:58'})
    before = dt.datetime(2026, 10, 22, 23, 57, 59, tzinfo=ZoneInfo('Asia/Shanghai'))
    after = before + dt.timedelta(seconds=1)
    moments = iter((before, after))
    monkeypatch.setattr(web_server, '_daily_summary_now', lambda:next(moments, after))
    calls = _capture_generation(monkeypatch)
    # This tick selected the previous task. It must not silently switch to a
    # new date after checking the previous task's failure state.
    assert web_server._send_daily_summaries()['reason'] == 'outside daily send window'
    assert calls == []
    # The following tick may select and generate the newly due task.
    assert web_server._send_daily_summaries()['status'] == 'ok'
    assert calls[0] == ('generate', '2026-10-22')


def test_postponing_the_schedule_keeps_todays_completed_summary(app, monkeypatch):
    client, admin, reader = app
    monkeypatch.setattr(web_server, '_daily_summary_now',
        lambda:dt.datetime(2026, 10, 21, 22, tzinfo=ZoneInfo('Asia/Shanghai')))
    assert web_server._save_daily_summary_global_cache('2026-10-20', '昨日总结', 1, {})
    assert web_server._save_daily_summary_global_cache('2026-10-21', '今日总结', 2, {})
    assert client.put('/admin/daily-summary-config', headers=admin,
                      json={'generate_at':'23:00'}).status_code == 200
    for headers in (admin, reader):
        data = client.get('/ai/daily-summary/today', headers=headers).json
        assert data['date'] == '2026-10-21' and data['status'] == 'completed'
        assert data['summary'] == '今日总结'
    assert client.get('/admin/daily-digest/events', headers=admin).json['date'] == '2026-10-21'
    assert client.get('/admin/scheduler/status', headers=admin).json['today'] == '2026-10-21'


@pytest.mark.parametrize('manual', [False, True])
def test_postponing_the_schedule_keeps_failed_tasks_retryable_with_their_original_window(
        app, monkeypatch, manual):
    client, admin, _ = app
    clock = {'now':dt.datetime(2026, 10, 21, 21, 1, tzinfo=ZoneInfo('Asia/Shanghai'))}
    monkeypatch.setattr(web_server, '_daily_summary_now', lambda:clock['now'])
    monkeypatch.setattr(web_server, '_epoch_now', lambda:int(clock['now'].timestamp()))
    original_window = web_server._digest_window('2026-10-21')

    def failing(day):
        web_server._set_daily_summary_error('provider temporarily unavailable')
        return None

    monkeypatch.setattr(web_server, '_generate_daily_summary_global', failing)
    assert web_server._send_daily_summaries()['status'] == 'error'
    clock['now'] = clock['now'].replace(hour=22, minute=0)
    assert client.put('/admin/daily-summary-config', headers=admin,
                      json={'generate_at':'23:00'}).status_code == 200
    models.close_db()  # The task and its original window survive new connections.
    status = client.get('/ai/daily-summary/today', headers=admin).json
    assert status['date'] == '2026-10-21' and status['can_retry']
    assert web_server._digest_window('2026-10-21') == original_window
    calls = _capture_generation(monkeypatch)
    if manual:
        response = client.post('/ai/daily-summary/retry', headers=admin)
        assert response.status_code == 200 and response.json['date'] == '2026-10-21'
    else:
        assert web_server._send_daily_summaries()['status'] == 'ok'
    assert calls[0] == ('generate', '2026-10-21')
    assert web_server._get_daily_summary_failure('2026-10-21') is None


@pytest.mark.parametrize('new_time', ['08:00', '23:00'])
def test_changing_the_schedule_during_generation_keeps_the_running_task_visible(app, monkeypatch, new_time):
    client, admin, reader = app
    calls = _capture_generation(monkeypatch)

    def generate(day):
        original_window = web_server._digest_window(day)
        assert client.put('/admin/daily-summary-config', headers=admin,
                          json={'generate_at':new_time}).status_code == 200
        for headers in (admin, reader):
            data = client.get('/ai/daily-summary/today', headers=headers).json
            assert data['date'] == day == '2026-10-21'
            assert data['status'] == 'generating'
        assert web_server._digest_window(day) == original_window
        return {'summary':'今日总结', 'stats':{}, 'article_count':1}

    monkeypatch.setattr(web_server, '_generate_daily_summary_global', generate)
    assert web_server._send_daily_summaries()['status'] == 'ok'
    assert calls == [(channel, '2026-10-21') for channel in ('inapp', 'email')]


def test_postponed_task_does_not_prevent_the_next_days_rollover(app, monkeypatch):
    client, admin, _ = app
    monkeypatch.setattr(web_server, '_generate_daily_summary_global', lambda day:None)
    web_server._send_daily_summaries()
    daily_summary_config.save_config({'generate_at':'23:00'})
    monkeypatch.setattr(web_server, '_daily_summary_now',
        lambda:dt.datetime(2026, 10, 22, 23, tzinfo=ZoneInfo('Asia/Shanghai')))
    assert client.get('/ai/daily-summary/today', headers=admin).json['date'] == '2026-10-22'
    calls = _capture_generation(monkeypatch)
    assert web_server._send_daily_summaries()['status'] == 'ok'
    assert calls[0] == ('generate', '2026-10-22')


def test_existing_completed_digests_keep_their_saved_window_after_a_schedule_change(app):
    original_window = web_server._digest_window('2026-10-21')
    assert web_server._save_daily_summary_global_cache('2026-10-21', '今日总结', 1,
        dict(zip(('window_start', 'window_end'), original_window)))
    daily_summary_config.save_config({'generate_at':'23:00'})
    assert web_server._digest_window('2026-10-21') == original_window


def test_next_window_connects_to_the_actual_cutoff_even_when_the_previous_task_failed(app, monkeypatch):
    previous_end = web_server._digest_window('2026-10-21')[1]
    monkeypatch.setattr(web_server, '_generate_daily_summary_global', lambda day:None)
    assert web_server._send_daily_summaries()['status'] == 'error'
    daily_summary_config.save_config({'generate_at':'23:00'})
    start, end = web_server._digest_window('2026-10-22')
    assert start == previous_end
    assert end == int(dt.datetime(2026, 10, 22, 23, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp())
