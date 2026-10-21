"""Browser plugin not available: verify the maintenance panel in Chromium.

Flow: admin settings -> system maintenance -> formatted statistics and polling.
"""
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from test_browser_daily_summary_config import browser, setup_page


def statistics(memory_used=64 * 1024**2):
    return {
        'storage': {'news_db': 2 * 1024**2, 'app_db': 1024**2,
                    'image_cache_files': 4 * 1024**2, 'image_cache_db': 0,
                    'avatars': 0, 'misc': 1024**2, 'data_dir_total': 8 * 1024**2},
        'disk': {'used': 2 * 1024**3, 'total': 8 * 1024**3, 'free': 6 * 1024**3},
        'image_cache': {'used_bytes': 4 * 1024**2, 'max_bytes': 16 * 1024**2, 'enabled': True},
        'counts': {'articles': 1234, 'cached_images': 70, 'favorites': 35, 'users': 12},
        'container': {'cpu_percent': 12.5, 'cpu_count': 2,
                      'mem_used_bytes': memory_used, 'mem_limit_bytes': 256 * 1024**2},
        'server_date': '2026-10-21',
    }


def open_maintenance(page):
    page.locator('.header-avatar').click()
    page.locator('#adminSettingsBtn').click()
    page.locator('#adminSettingsBody [data-admin-tab="server"]').click()


@pytest.mark.parametrize('width', [390, 1280])
def test_stats_render_and_refresh_with_formatted_memory_disk_and_storage(browser, width, tmp_path):
    context, page, _, errors, logs = setup_page(browser, width)
    payload = statistics()
    calls = []

    def route(request):
        assert request.request.headers['authorization'] == 'Bearer mock'
        calls.append(request.request.url)
        request.fulfill(json=payload)

    context.route('**/admin/server-stats', route)
    try:
        assert page.title() == 'RayNews' and urlsplit(page.url).netloc == 'raynews.test'
        open_maintenance(page)
        panel = page.locator('#serverStatsBody')
        expect(panel).to_have_attribute('data-loaded', '1')
        expect(panel).to_contain_text('64.0 MB / 256.0 MB')
        expect(panel).to_contain_text('2.0 GB / 8.0 GB')
        expect(panel).to_contain_text('8.0 MB / 8.0 GB')
        expect(panel).to_contain_text('图片缓存用量 4.0 MB / 16.0 MB (25%)')
        expect(panel.locator('.admin-stat-num')).to_have_text(['1234', '70', '35', '12'])
        expect(panel).not_to_contain_text('加载中')
        assert page.evaluate('serverTodayDate') == '2026-10-21'

        payload['container']['mem_used_bytes'] = 128 * 1024**2
        expect(panel).to_contain_text('128.0 MB / 256.0 MB', timeout=5000)
        assert len(calls) >= 2
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.locator('#adminSettingsBody').evaluate('el => el.scrollWidth <= el.clientWidth')
        screenshot_dir = Path(os.environ.get('RAYNEWS_QA_SCREENSHOT_DIR', str(tmp_path)))
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshot_dir / f'server-stats-{width}.png'), animations='disabled')
        page.evaluate('closeAdminSettings()')
        assert page.evaluate('serverStatsTimer') is None
        assert errors == [] and logs == []
    finally:
        context.close()


def test_stats_request_failure_is_visible_and_polling_recovers(browser):
    context, page, _, errors, logs = setup_page(browser, 390)
    state = {'failed': True}

    def route(request):
        if state['failed']:
            request.fulfill(body='<html>upstream unavailable</html>', content_type='text/html')
        else:
            payload = statistics()
            payload['container'] = {'cpu_percent': None, 'mem_limit_bytes': None}
            payload['disk'] = {'used': None, 'total': None, 'free': None}
            request.fulfill(json=payload)

    context.route('**/admin/server-stats', route)
    try:
        open_maintenance(page)
        panel = page.locator('#serverStatsBody')
        expect(panel).to_contain_text('加载失败')
        expect(panel).not_to_contain_text('加载中')
        state['failed'] = False
        expect(panel).to_have_attribute('data-loaded', '1', timeout=5000)
        expect(panel).to_contain_text('N/A / N/A')
        expect(panel).not_to_contain_text('NaN')
        expect(panel).not_to_contain_text('加载失败')
        page.evaluate('closeAdminSettings()')
        assert errors == [] and logs == []
    finally:
        context.close()
