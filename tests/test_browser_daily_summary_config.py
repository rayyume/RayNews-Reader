"""Browser plugin not available: exercise the admin digest form in Chromium."""
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
USER = {'id':7, 'role':'admin', 'nickname':'Admin', 'email':'admin@example.test'}


@pytest.fixture
def browser():
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        yield browser
        browser.close()


def setup_page(browser, width):
    context = browser.new_context(viewport={'width':width,'height':900}, service_workers='block',
                                  timezone_id='America/Los_Angeles', locale='zh-CN', color_scheme='dark')
    context.add_init_script("localStorage.setItem('raynews_token','mock');localStorage.setItem('raynews_user',JSON.stringify(%s));" % json.dumps(USER))
    state = {'config':{'enabled':True, 'generate_at':'08:45', 'timezone':'Pacific/Auckland',
                       'delivery_method':'both', 'email_configured':False},
             'saved':[], 'failure':False, 'hold':False, 'pending':[]}
    def route(r):
        path = urlsplit(r.request.url).path
        if path in ('/','/index.html'):
            r.fulfill(body=(ROOT/'frontend/index.html').read_text(), content_type='text/html');return
        asset = ROOT/'frontend'/path.lstrip('/')
        if path.endswith(('.png','.ico')) and asset.is_file():
            r.fulfill(body=asset.read_bytes(),content_type='image/png');return
        data = {}
        if path == '/auth/me':data = {'user':USER}
        elif path == '/settings':
            data = {'access':{'permissions':{'daily_digest':True},'services':{},'view_daily_digest':True},
                    'notification_config':{},'theme_preference':'dark'}
        elif path == '/api/news':data = {'items':[],'total':0,'total_pages':1}
        elif path in ('/sources','/api/sources'):data = {'sources':[],'categories':[]}
        elif path == '/notifications':data = {'items':[],'unread':0}
        elif path == '/auth/users':data = {'users':[USER]}
        elif path == '/admin/daily-summary-config':
            assert r.request.headers['authorization'] == 'Bearer mock'
            if r.request.method == 'PUT':
                if state['failure']:
                    r.fulfill(status=400,json={'error':'保存失败，请稍后重试'});return
                state['saved'].append(r.request.post_data_json)
                state['config'].update(r.request.post_data_json)
            elif state['hold']:
                state['pending'].append(r);return
            data = state['config']
        elif path == '/ai/daily-summary/today':
            data = {'status':'scheduled' if state['config']['enabled'] else 'server_disabled',
                    'generate_at':state['config']['generate_at'], 'timezone':state['config']['timezone']}
        elif path == '/ai/result-updates':data = {'items':[],'cursor':'test|0','has_more':False}
        r.fulfill(json=data)
    context.route('**/*', route)
    page = context.new_page()
    errors, logs = [], []
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('console',lambda message:logs.append(message.text) if message.type in {'warning','error'} else None)
    page.goto('http://raynews.test/')
    page.wait_for_function('userAutoSettings?.view_daily_digest && dailySummaryState.timezone')
    return context,page,state,errors,logs


def open_config(page):
    page.evaluate("openAdminSettings();switchAdminTab('ai');switchAdminAiTab('digest')")
    expect(page.locator('#adminDigestSave')).to_be_enabled()


@pytest.mark.parametrize('width',[390,1280])
def test_admin_can_save_all_delivery_modes_time_and_master_switch(browser,width,tmp_path):
    context,page,state,errors,logs = setup_page(browser,width)
    try:
        assert page.title() == 'RayNews' and urlsplit(page.url).netloc == 'raynews.test'
        open_config(page)
        expect(page.locator('#adminDigestEnabled')).to_be_checked()
        expect(page.locator('#adminDigestTime')).to_have_value('08:45')
        expect(page.locator('#adminDigestTimezone')).to_have_text('Pacific/Auckland')
        expect(page.locator('#adminDigestEmailHint')).to_be_visible()
        for method in ('inapp','email','both'):
            page.locator('#adminDigestDelivery').select_option(method)
            page.locator('#adminDigestTime').fill('06:35')
            page.locator('#adminDigestSave').click()
            expect(page.locator('#adminDigestStatus')).to_have_text('每日总结设置已保存。')
            assert state['saved'][-1] == {'enabled':True,'generate_at':'06:35','delivery_method':method}
            if method == 'inapp':expect(page.locator('#adminDigestEmailHint')).to_be_hidden()
            else:expect(page.locator('#adminDigestEmailHint')).to_be_visible()
        page.locator('#adminDigestEnabled').uncheck()
        page.locator('#adminDigestSave').click()
        expect(page.locator('#adminDigestStatus')).to_have_text('每日总结设置已保存。')
        assert state['saved'][-1]['enabled'] is False
        page.evaluate('closeAdminSettings()')
        page.locator('#dailySummaryBtn').click()
        expect(page.locator('#dailySummaryBody')).to_contain_text('服务器已暂停每日总结')
        page.evaluate('closeDailySummary()')
        open_config(page)
        expect(page.locator('#adminDigestEnabled')).not_to_be_checked()
        expect(page.locator('#adminDigestTime')).to_have_value('06:35')
        expect(page.locator('#adminDigestDelivery')).to_have_value('both')
        page.locator('#adminDigestEnabled').check()
        page.locator('#adminDigestSave').click()
        expect(page.locator('#adminDigestStatus')).to_have_text('每日总结设置已保存。')
        screenshot_dir = Path(os.environ.get('RAYNEWS_QA_SCREENSHOT_DIR',str(tmp_path)))
        screenshot_dir.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(screenshot_dir/f'admin-digest-{width}.png'),animations='disabled')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        page.evaluate('closeAdminSettings()')
        page.locator('#dailySummaryBtn').click()
        expect(page.locator('#dailySummaryBody')).to_contain_text('06:35（Pacific/Auckland）')
        expect(page.locator('#dailySummaryBody')).not_to_contain_text('北京时间')
        assert page.evaluate("formatDailySummaryTime('2026-10-21 00:00:00')") == '13:00'
        assert errors == [] and logs == []
    finally:context.close()


def test_failed_save_keeps_edits_and_allows_retry(browser):
    context,page,state,errors,logs = setup_page(browser,390)
    try:
        open_config(page)
        page.locator('#adminDigestTime').fill('23:59')
        page.locator('#adminDigestDelivery').select_option('email')
        state['failure'] = True
        page.locator('#adminDigestSave').click()
        expect(page.locator('#adminDigestStatus')).to_have_text('保存失败，请稍后重试')
        expect(page.locator('#adminDigestSave')).to_be_enabled()
        expect(page.locator('#adminDigestTime')).to_have_value('23:59')
        assert state['saved'] == []
        state['failure'] = False
        page.locator('#adminDigestSave').click()
        expect(page.locator('#adminDigestStatus')).to_have_text('每日总结设置已保存。')
        assert state['saved'] == [{'enabled':True,'generate_at':'23:59','delivery_method':'email'}]
        assert errors == []
        assert all('400 (Bad Request)' in message for message in logs)
    finally:context.close()


def test_delayed_configuration_does_not_apply_after_logout(browser):
    context,page,state,errors,_ = setup_page(browser,390)
    try:
        state['hold'] = True
        page.evaluate("openAdminSettings();switchAdminTab('ai');switchAdminAiTab('digest')")
        for _ in range(500):
            if state['pending']:break
            page.wait_for_timeout(10)
        assert state['pending']
        expect(page.locator('#adminDigestSave')).to_be_disabled()
        page.evaluate('logout()')
        for pending in state['pending']:pending.fulfill(json=state['config'])
        expect(page.locator('#adminDigestTimezone')).to_have_text('读取中…')
        expect(page.locator('#adminDigestSave')).to_be_disabled()
        assert errors == []
    finally:context.close()
