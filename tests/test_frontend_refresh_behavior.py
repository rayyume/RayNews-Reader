import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")


def test_review_hardening_frontend_contracts():
    assert HTML.count('data-admin-tab="server"') == 1
    assert "detail === 'refresh failed'" in HTML
    assert "global_article_count" in source_between(
        "function isStartupInitializationResponse(",
        "function renderColdStartInitializing",
    )
    assert "startStartupEmptyRevalidation" in source_between(
        "function applyNewsPage(",
        "async function fetchNewsPage",
    )
    assert "await delay(" in source_between("function retryArticleDetail(", "function usesMobileArticleNavigation")
    assert "retryArticleDetail" in source_between("function onReturnToForeground()", "document.addEventListener('visibilitychange'")


def test_logo_refresh_state_is_declared_before_click_handler_uses_it():
    assert "let logoRefreshInProgress = false;" in HTML


def test_is_transient_refresh_error_recognizes_browser_and_tagged_timeouts():
    source = source_between("function isTransientRefreshError(", "async function retryTransientRefreshRequest(")
    run_node(
        source,
        """
assert.equal(context.isTransientRefreshError(null), false);
assert.equal(context.isTransientRefreshError(Object.assign(new Error('cancelled'), { name: 'AbortError' })), false);
assert.equal(context.isTransientRefreshError(new TypeError('Load failed')), true);
assert.equal(context.isTransientRefreshError(new TypeError('Failed to fetch')), true);
assert.equal(context.isTransientRefreshError(new Error('NetworkError when attempting to fetch resource')), true);
assert.equal(context.isTransientRefreshError(Object.assign(new Error('slow'), { name: 'RefreshStatusTimeoutError' })), true);
assert.equal(context.isTransientRefreshError(new Error('刷新失败，请稍后重试')), false);
// A real 502/504 collapses its body into a code-less message ("refresh service
// unavailable", nginx HTML) — classification must fall back to the numeric status.
assert.equal(context.isTransientRefreshError(Object.assign(new Error('refresh service unavailable'), { status: 502 })), true);
assert.equal(context.isTransientRefreshError(Object.assign(new Error('刷新接口返回了 HTML 页面'), { status: 504 })), true);
assert.equal(context.isTransientRefreshError(Object.assign(new Error('bad request'), { status: 400 })), false);
""",
    )


def test_parse_refresh_response_preserves_numeric_status_on_error():
    source = source_between("async function parseRefreshResponse(", "function isTransientRefreshError(")
    run_node(
        source,
        """
context.refreshErrorMessage = (data) => (data && data.error) || 'refresh failed';
// A real 502 JSON body from web_server.py's status endpoint.
const jsonResp = {
  ok: false,
  status: 502,
  text: async () => JSON.stringify({ error: 'refresh service unavailable' }),
};
await assert.rejects(
  context.parseRefreshResponse(jsonResp),
  error => error.status === 502 && error.message === 'refresh service unavailable',
);
// An nginx HTML 504 page must also carry the status through.
const htmlResp = {
  ok: false,
  status: 504,
  text: async () => '<html><body>504 Gateway Time-out</body></html>',
};
await assert.rejects(
  context.parseRefreshResponse(htmlResp),
  error => error.status === 504,
);
""",
    )


def test_bump_content_epoch_retains_memory_buffer_for_stale_downgrade():
    source = source_between("function bumpContentEpoch(", "function rememberBufferedPage(")
    run_node(
        source,
        """
context.contentEpoch = 3;
context.pagePrefetchPromises = new Map([['2:all', Promise.resolve()]]);
let cleared = false;
context.clearCachedNewsPages = () => { cleared = true; };
// A snapshot buffered under the current epoch.
context.pageMemoryBuffer = new Map([['2:all', { data: { items: [] }, epoch: 3 }]]);
context.bumpContentEpoch();
assert.equal(context.contentEpoch, 4);
// Prefetch dedup slots + IndexedDB are hygiene-cleared...
assert.equal(context.pagePrefetchPromises.size, 0);
assert.equal(cleared, true);
// ...but the in-memory buffer is deliberately retained so peekStaleBufferedPage()
// still has a snapshot to downgrade to after a bump (correctness is upheld by the
// per-record epoch check in readBufferedPage(), not by clearing).
assert.equal(context.pageMemoryBuffer.size, 1);
""",
    )


def test_refresh_error_message_normalizes_raw_browser_network_errors():
    source = source_between("function refreshErrorMessage(", "async function parseRefreshResponse(")
    run_node(
        source,
        """
context.compactRefreshDetail = value => String(value || '').trim();
// Must construct with the vm realm's own Error/TypeError — refreshErrorMessage's
// `instanceof Error` check fails across realms, so an outer-realm Error would be
// silently misclassified as a data object instead.
const loadFailed = vm.runInContext("new TypeError('Load failed')", context);
const failedToFetch = vm.runInContext("new TypeError('Failed to fetch')", context);
const networkError = vm.runInContext("new Error('NetworkError when attempting to fetch resource')", context);
const other = vm.runInContext("new Error('something unexpected')", context);
assert.equal(context.refreshErrorMessage(loadFailed), '网络连接失败，请检查网络后重试');
assert.equal(context.refreshErrorMessage(failedToFetch), '网络连接失败，请检查网络后重试');
assert.equal(context.refreshErrorMessage(networkError), '网络连接失败，请检查网络后重试');
assert.equal(context.refreshErrorMessage(other), 'something unexpected');
""",
    )


def test_status_request_tags_per_request_timeout_as_transient():
    source = source_between("async function requestRefreshStatusOnce(", "async function pollRefreshJob(")
    run_node(
        source,
        """
context.authToken = 'token';
context.parseRefreshResponse = async () => ({ status: 'running' });
context.setTimeout = (callback) => { callback(); return 1; };
context.clearTimeout = () => {};
context.AbortController = class {
  constructor() { this.signal = { aborted: false }; }
  abort() { this.signal.aborted = true; }
};
context.fetch = () => new Promise((resolve, reject) => {
  reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
});
await assert.rejects(
  context.requestRefreshStatusOnce('job-1', 10),
  error => error.name === 'RefreshStatusTimeoutError',
);
""",
    )


def test_prepare_page_navigation_must_be_fresh_uses_network_within_budget():
    block = source_between("function peekStaleBufferedPage(", "// Full snapshot compatibility wrapper")
    run_node(
        block,
        """
context.pageRequestSequence = 0;
context.pageRequestController = null;
context.contentEpoch = 1;
context.AbortController = class { constructor() { this.signal = {}; } abort() {} };
context.setTimeout = () => 1;
context.clearTimeout = () => {};
context.fetchNewsPage = async () => ({ items: [{ id: 1 }], total: 1 });
context.writeCachedNewsPage = async () => {};
context.rememberBufferedPage = () => {};
context.pendingRelevantCount = () => 1; // mustBeFresh
context.withPromiseTimeout = async promise => promise; // network answers "immediately"
context.showToast = () => { throw new Error('should not toast'); };
context.applyPageCalibrationWhenActive = () => { throw new Error('should not calibrate'); };
context.pageMemoryBuffer = new Map();
const data = await context.preparePageNavigation(2, 'all');
assert.deepEqual(data, { items: [{ id: 1 }], total: 1 });
""",
    )


def test_prepare_page_navigation_falls_back_to_stale_snapshot_past_budget():
    block = source_between("function peekStaleBufferedPage(", "// Full snapshot compatibility wrapper")
    run_node(
        block,
        """
context.pageRequestSequence = 0;
context.pageRequestController = null;
context.contentEpoch = 5;
context.AbortController = class { constructor() { this.signal = {}; } abort() {} };
context.setTimeout = () => 1;
context.clearTimeout = () => {};
let resolveNetwork;
context.fetchNewsPage = () => new Promise(resolve => { resolveNetwork = resolve; });
context.writeCachedNewsPage = async () => {};
context.rememberBufferedPage = () => {};
context.pendingRelevantCount = () => 1;
// Simulates withPromiseTimeout's real race semantics losing to the timeout,
// without needing a real timer in the test.
context.withPromiseTimeout = async () => null;
context.showToast = () => { throw new Error('should not toast'); };
context.newsPageCacheKey = (page, filter) => page + ':' + filter;
// A snapshot buffered under a now-stale epoch — still usable as a downgrade.
context.pageMemoryBuffer = new Map([['2:all', { data: { items: [{ id: 'stale' }] }, epoch: 1 }]]);
let calibrated = null;
context.applyPageCalibrationWhenActive = data => { calibrated = data; };
const data = await context.preparePageNavigation(2, 'all');
assert.deepEqual(data, { items: [{ id: 'stale' }] });
resolveNetwork({ items: [{ id: 'fresh' }] });
await new Promise(resolve => setTimeout(resolve, 20));
assert.deepEqual(calibrated, { items: [{ id: 'fresh' }] });
""",
    )


def test_prepare_page_navigation_waits_out_network_when_no_stale_snapshot_exists():
    block = source_between("function peekStaleBufferedPage(", "// Full snapshot compatibility wrapper")
    run_node(
        block,
        """
context.pageRequestSequence = 0;
context.pageRequestController = null;
context.contentEpoch = 5;
context.AbortController = class { constructor() { this.signal = {}; } abort() {} };
context.setTimeout = () => 1;
context.clearTimeout = () => {};
context.fetchNewsPage = async () => ({ items: [{ id: 'fresh' }] });
context.writeCachedNewsPage = async () => {};
context.rememberBufferedPage = () => {};
context.pendingRelevantCount = () => 1;
context.withPromiseTimeout = async () => null; // budget always "expires" in this test
context.showToast = () => {};
context.newsPageCacheKey = (page, filter) => page + ':' + filter;
context.pageMemoryBuffer = new Map(); // nothing buffered — must fall through to network
const data = await context.preparePageNavigation(2, 'all');
assert.deepEqual(data, { items: [{ id: 'fresh' }] });
""",
    )


def test_is_sw_fallback_response_reads_the_marker_header():
    source = source_between("function isSwFallbackResponse(", "function swFallbackError(")
    run_node(
        source,
        """
const headers = new Map([['X-SW-Fallback', '1']]);
assert.equal(context.isSwFallbackResponse({ headers: { get: k => headers.get(k) } }), true);
assert.equal(context.isSwFallbackResponse({ headers: { get: () => null } }), false);
assert.equal(context.isSwFallbackResponse({}), false);
assert.equal(context.isSwFallbackResponse(null), false);
""",
    )


def test_fetch_news_page_rejects_sw_fallback_responses():
    source = source_between("function isSwFallbackResponse(", "function warmPageCoverImages")
    run_node(
        source,
        """
context.buildNewsPageParams = () => new URLSearchParams();
context.fetch = async () => ({
  ok: true,
  headers: { get: name => (name === 'X-SW-Fallback' ? '1' : null) },
  json: async () => ({ items: [{ id: 1 }] }),
});
await assert.rejects(
  context.fetchNewsPage(1, 'all'),
  error => error.name === 'SwFallbackError',
);
""",
    )


def test_load_since_treats_sw_fallback_as_a_failed_check_not_zero_new_articles():
    incremental = source_between("async function loadSince(", "function rebuildSourceFilterGroups")
    run_node(
        incremental,
        """
context.refreshInProgress = false;
context.INCREMENTAL_FETCH_LIMIT = 200;
context.seenArticleIds = new Set();
context.latestKnownTimestamp = 100;
context.pendingNewArticleCount = 0;
context.pendingNewItems = [];
context.fetch = async () => ({
  headers: { get: name => (name === 'X-SW-Fallback' ? '1' : null) },
  json: async () => { throw new Error('must not parse a fallback response as data'); },
});
context.setTimeout = () => 1;
context.clearTimeout = () => {};
const added = await context.loadSince(100);
// -1 (not 0): a stale SW-cache fallback must read as "couldn't check", not as
// "checked, confirmed zero new articles" — see checkForNewArticlesAfterForegroundResume().
assert.equal(added, -1);
assert.equal(context.pendingNewItems.length, 0);
""",
    )


def test_foreground_resume_check_retries_only_on_failed_checks():
    helper = source_between(
        "async function checkForNewArticlesAfterForegroundResume(",
        "let lastForegroundSyncAt = 0;",
    )
    run_node(
        helper,
        """
context.document = { hidden: false };
context.delay = async () => {};
context.latestKnownTimestamp = 0;
const calls = [];
context.loadSince = async cursor => { calls.push(cursor); return 0; };
await context.checkForNewArticlesAfterForegroundResume(100);
assert.deepEqual(calls, [100]); // confirmed zero -> no retry
""",
    )


def test_foreground_resume_check_retries_up_to_twice_on_failure_then_gives_up():
    helper = source_between(
        "async function checkForNewArticlesAfterForegroundResume(",
        "let lastForegroundSyncAt = 0;",
    )
    run_node(
        helper,
        """
context.document = { hidden: false };
const delays = [];
context.delay = async ms => { delays.push(ms); };
context.latestKnownTimestamp = 0;
const calls = [];
context.loadSince = async cursor => { calls.push(cursor); return -1; };
await context.checkForNewArticlesAfterForegroundResume(100);
assert.deepEqual(calls, [100, 100, 100]);
assert.deepEqual(delays, [1500, 4000]);
""",
    )


def test_foreground_resume_check_stops_retrying_once_backgrounded_again():
    helper = source_between(
        "async function checkForNewArticlesAfterForegroundResume(",
        "let lastForegroundSyncAt = 0;",
    )
    run_node(
        helper,
        """
context.document = { hidden: false };
context.delay = async () => { context.document.hidden = true; };
context.latestKnownTimestamp = 0;
const calls = [];
context.loadSince = async cursor => { calls.push(cursor); return -1; };
await context.checkForNewArticlesAfterForegroundResume(100);
// One initial attempt, one retry attempt scheduled — but backgrounded during the
// delay before that retry's loadSince() call, so it's never made.
assert.deepEqual(calls, [100]);
""",
    )


def test_foreground_resume_check_uses_the_latest_known_timestamp_on_retry():
    helper = source_between(
        "async function checkForNewArticlesAfterForegroundResume(",
        "let lastForegroundSyncAt = 0;",
    )
    run_node(
        helper,
        """
context.document = { hidden: false };
context.delay = async () => {};
context.latestKnownTimestamp = 0;
const calls = [];
context.loadSince = async cursor => {
  calls.push(cursor);
  context.latestKnownTimestamp = 999; // simulates a concurrent successful check
  return -1;
};
await context.checkForNewArticlesAfterForegroundResume(100);
assert.deepEqual(calls, [100, 999, 999]);
""",
    )


def test_refresh_running_state_has_no_dot_overlay_but_keeps_sweep():
    assert ".refresh-btn.refresh-running::after" not in HTML
    assert ".refresh-btn.refresh-running::before" in HTML
    assert "refreshSweep" in HTML


def test_mobile_article_header_has_a_dedicated_double_tap_top_scroll_zone():
    assert 'id="articleTopScrollZone"' in HTML
    assert "function scrollArticleDetailToTop()" in HTML
    zone = source_between("const articleTopScrollZone", "document.getElementById('lb').addEventListener")
    assert "usesMobileArticleNavigation()" in zone
    assert "touchend" in zone
    assert "scrollArticleDetailToTop()" in zone


def test_service_worker_rethrows_network_errors_without_a_cached_response():
    source = (ROOT / "frontend" / "sw.js").read_text(encoding="utf-8")
    # Article detail is network-first: on a network failure it serves a cached
    # copy only if one exists, otherwise it rethrows (never silently swallows).
    article_detail_block = source[
        source.index("// Article detail: network-first"):
        source.index("// List / other API: network-first")
    ]
    assert "if (cached) return withSwFallbackMarker(cached);" in article_detail_block
    assert "throw error;" in article_detail_block


def test_service_worker_tags_list_api_fallback_responses():
    source = (ROOT / "frontend" / "sw.js").read_text(encoding="utf-8")
    assert "function withSwFallbackMarker(cached)" in source
    assert "headers.set('X-SW-Fallback', '1');" in source
    list_api_block = source[
        source.index("// List / other API: network-first"):
        source.index("// ── Static assets: cache-first ──")
    ]
    assert "if (cached) return withSwFallbackMarker(cached);" in list_api_block
    # Article detail is network-first too: its cached copy is only ever served
    # as an offline failure fallback (never as a fresh hit), so — like the list —
    # it must be tagged so callers can tell it apart from a live response.
    article_detail_block = source[
        source.index("// Article detail: network-first"):
        source.index("// List / other API: network-first")
    ]
    assert "if (cached) return withSwFallbackMarker(cached);" in article_detail_block


def source_between(start, end):
    return HTML[HTML.index(start):HTML.index(end, HTML.index(start))]


def run_node(source, body):
    script = f"""
const assert = require('assert');
const vm = require('vm');
const context = {{ console, URLSearchParams, AbortController }};
// Resume-recovery collaborators (index.html, next to renderColdStartError) sit
// outside most extracted ranges. Default them to no-ops here; a test that
// asserts on the recovery chain overrides them in its own body.
context.cancelNetworkRecoveryRetry = () => {{}};
context.scheduleNetworkRecoveryRetry = () => {{}};
context.listLoadDegraded = false;
context.activeRefreshDiscoverySink = null;
vm.createContext(context);
vm.runInContext({json.dumps(source)}, context);
(async () => {{
{body}
}})().catch(error => {{
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
}});
"""
    result = subprocess.run(
        ["node", "-e", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def select_filter_source():
    return source_between("function applyFilterSelectionState(", "function filteredNews()")


def select_filter_runtime_setup():
    return """
context.filter = 'cat:Old';
context.currentPage = 1;
context.pageNavigationSequence = 0;
context.pageNavigationPending = false;
context.programmaticScrollUntil = 0;
context.CATEGORY_ORDER = [];
context.sourceFilterGroups = {};
context.localStorage = { setItem: () => {} };
context.document = {
  documentElement: { scrollTop: 0 },
  getElementById: () => ({
    querySelectorAll: () => [],
    querySelector: () => null,
  }),
};
context.window = {
  scrollY: 480,
  scrollTo: (_x, y) => { context.window.scrollY = y; context.scrollPositions.push(y); },
  matchMedia: () => ({ matches: false }),
};
context.scrollPositions = [];
context.cancelCalls = 0;
context.cancelViewBoundRefreshWork = () => { context.cancelCalls++; };
context.rememberCurrentListHistory = () => { context.rememberCalls++; };
context.rememberCalls = 0;
context.renderTopCatBar = () => {};
context.renderFilters = () => { context.renderFilterCalls++; };
context.renderFilterCalls = 0;
context.loadNewsPage = async () => { context.legacyLoadCalls++; return true; };
context.legacyLoadCalls = 0;
context.consumePendingNewArticles = value => context.consumed.push(value);
context.consumed = [];
context.syncListUrl = options => context.synced.push(options);
context.synced = [];
context.closeSidebar = () => { context.closeCalls++; };
context.closeCalls = 0;
context.setPageNavigationPending = value => context.pendingStates.push(value);
context.pendingStates = [];
context.stabilizePageTop = () => { context.stabilizeCalls++; };
context.stabilizeCalls = 0;
context.scheduleAdjacentPagePrefetch = (...args) => context.prefetchCalls.push(args);
context.prefetchCalls = [];
context.applyPageDuringScroll = (data, page, activeFilter) => {
  context.applied.push([data.marker, page, activeFilter]);
  return { marker: 'transition-list' };
};
context.applied = [];
context.releasePageTransitionLock = list => context.released.push(list && list.marker);
context.released = [];
"""


def test_top_category_bar_enables_scroll_navigation_mode():
    top_bar = source_between("function renderTopCatBar()", "function toggleCategoryExpansion")
    assert "selectFilter(btn.dataset.f, { scrollToTop: true });" in top_bar


def test_active_top_category_on_first_page_only_scrolls_without_loading():
    run_node(
        select_filter_source(),
        select_filter_runtime_setup()
        + """
context.scrollCalls = 0;
context.scrollPageToTop = async () => { context.scrollCalls++; context.window.scrollY = 0; return true; };
context.prepareCalls = 0;
context.preparePageNavigation = async () => { context.prepareCalls++; return { marker: 'unexpected' }; };

await context.selectFilter('cat:Old', { scrollToTop: true });

assert.equal(context.scrollCalls, 1);
assert.equal(context.cancelCalls, 0);
assert.equal(context.pageNavigationSequence, 0);
assert.equal(context.prepareCalls, 0);
assert.equal(context.legacyLoadCalls, 0);
assert.deepEqual(context.applied, []);
assert.equal(context.filter, 'cat:Old');
assert.equal(context.currentPage, 1);
assert.equal(context.stabilizeCalls, 1);
assert.deepEqual(context.synced, [{ replace: true }]);
""",
    )


def test_active_top_category_scroll_invalidates_only_an_existing_page_navigation():
    run_node(
        select_filter_source(),
        select_filter_runtime_setup()
        + """
context.pageNavigationSequence = 7;
context.pageNavigationPending = true;
context.scrollPageToTop = async () => true;
context.preparePageNavigation = async () => { throw new Error('must not prepare'); };

await context.selectFilter('cat:Old', { scrollToTop: true });

assert.equal(context.cancelCalls, 0);
assert.equal(context.pageNavigationSequence, 8);
assert.deepEqual(context.pendingStates, [false]);
assert.equal(context.filter, 'cat:Old');
assert.equal(context.currentPage, 1);
""",
    )


def test_top_category_change_prepares_and_scrolls_in_parallel_then_applies_near_top():
    run_node(
        select_filter_source(),
        select_filter_runtime_setup()
        + """
let resolveData;
context.prepareStarted = false;
context.preparePageNavigation = (page, activeFilter) => {
  context.prepareStarted = true;
  assert.equal(page, 1);
  assert.equal(activeFilter, 'cat:New');
  return new Promise(resolve => { resolveData = resolve; });
};
context.scrollStarted = false;
context.scrollPageToTop = async ({ onNearTop }) => {
  context.scrollStarted = true;
  assert.equal(context.prepareStarted, true);
  onNearTop();
  return true;
};

const switching = context.selectFilter('cat:New', { scrollToTop: true });
await Promise.resolve();
assert.equal(context.scrollStarted, true);
assert.equal(context.filter, 'cat:Old');
assert.deepEqual(context.applied, []);
resolveData({ marker: 'new', total: 45 });
await switching;

assert.equal(context.filter, 'cat:New');
assert.equal(context.currentPage, 1);
assert.deepEqual(context.applied, [['new', 1, 'cat:New']]);
assert.deepEqual(context.consumed, ['cat:New']);
assert.deepEqual(context.synced, [{ push: true }]);
assert.deepEqual(context.prefetchCalls, [[1, 'cat:New', 45]]);
assert.equal(context.stabilizeCalls, 1);
assert.deepEqual(context.pendingStates, [true, false]);
assert.deepEqual(context.released, ['transition-list']);
""",
    )


def test_failed_top_category_change_keeps_list_state_and_restores_scroll_offset():
    run_node(
        select_filter_source(),
        select_filter_runtime_setup()
        + """
context.currentPage = 3;
context.preparePageNavigation = async () => null;
context.scrollPageToTop = async ({ onNearTop }) => {
  context.window.scrollY = 0;
  context.scrollPositions.push(0);
  onNearTop();
  return true;
};

await context.selectFilter('cat:New', { scrollToTop: true });

assert.equal(context.filter, 'cat:Old');
assert.equal(context.currentPage, 3);
assert.deepEqual(context.applied, []);
assert.equal(context.scrollPositions.at(-1), 480);
assert.deepEqual(context.consumed, []);
assert.deepEqual(context.synced, []);
assert.deepEqual(context.prefetchCalls, []);
assert.deepEqual(context.pendingStates, [true, false]);
""",
    )


def test_later_top_category_click_invalidates_an_older_inflight_transition():
    run_node(
        select_filter_source(),
        select_filter_runtime_setup()
        + """
const resolvers = {};
context.preparePageNavigation = (_page, activeFilter) => new Promise(resolve => {
  resolvers[activeFilter] = resolve;
});
context.scrollPageToTop = async ({ onNearTop }) => { onNearTop(); return true; };

const older = context.selectFilter('cat:Older', { scrollToTop: true });
await Promise.resolve();
const newer = context.selectFilter('cat:Newer', { scrollToTop: true });
await Promise.resolve();
resolvers['cat:Newer']({ marker: 'newer', total: 1 });
await newer;
resolvers['cat:Older']({ marker: 'older', total: 1 });
await older;

assert.equal(context.filter, 'cat:Newer');
assert.deepEqual(context.applied, [['newer', 1, 'cat:Newer']]);
assert.deepEqual(context.consumed, ['cat:Newer']);
assert.deepEqual(context.synced, [{ push: true }]);
""",
    )


def test_changed_notification_retry_requires_explicit_new_broadcast_id():
    source = source_between("let notifPubBroadcastId", "function renderNotifPreview")
    run_node(
        source,
        """
const ids = ['broadcast-1', 'broadcast-2'];
context.window = { crypto: { randomUUID: () => ids.shift() } };
context.crypto = context.window.crypto;
let confirmResult = false;
let confirmCalls = 0;
context.confirm = () => { confirmCalls++; return confirmResult; };

const first = context.notificationBroadcastPayloadSignature('title', 'body', 'plain', false);
const changed = context.notificationBroadcastPayloadSignature('title', 'edited', 'plain', false);
assert.equal(context.prepareNotificationBroadcast(first), 'broadcast-1');
assert.equal(context.prepareNotificationBroadcast(first), 'broadcast-1');
assert.equal(confirmCalls, 0);

// A changed payload must never silently reuse the ambiguous request's id.
assert.equal(context.prepareNotificationBroadcast(changed), null);
assert.equal(confirmCalls, 1);

// Explicit confirmation creates a genuinely new broadcast id.
confirmResult = true;
assert.equal(context.prepareNotificationBroadcast(changed), 'broadcast-2');
assert.equal(confirmCalls, 2);
""",
    )


def test_notification_refresh_exposes_a_failed_load_instead_of_an_empty_list():
    notification_source = source_between("// ═══ In-App Notifications", "// ═══ Admin Panel")
    notification_source += "\nglobalThis.__notificationLoadState = () => notifLoadState;"
    run_node(
        notification_source,
        """
const classes = new Set();
context.document = {
  querySelector: () => ({ classList: { toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name) } }),
  getElementById: () => ({ style: {}, textContent: '' }),
};
context.authToken = 'token';
context.apiFetch = async () => { throw new Error('offline'); };

assert.equal(await context.refreshNotifStatus(), false);
assert.equal(context.__notificationLoadState(), 'error');
""",
    )


def test_mark_notification_read_updates_the_list_before_the_network_returns():
    notification_source = "function esc(value) { return String(value); }\n" + source_between(
        "// ═══ In-App Notifications", "// ═══ Admin Panel"
    )
    notification_source += """
globalThis.__setNotifTestState = (items, unread) => {
  notifItems = items;
  notifUnread = unread;
  notifLoadState = 'ready';
};
globalThis.__getNotifTestState = () => ({
  items: notifItems.map(item => ({ ...item })),
  unread: notifUnread,
});
"""
    run_node(
        notification_source,
        """
const body = { innerHTML: '' };
const menuBadge = { style: {}, textContent: '' };
context.document = {
  querySelector: () => ({ classList: { toggle: () => {} } }),
  getElementById: id => id === 'notifBody' ? body : menuBadge,
};
context.authToken = 'token';
let finishPost;
let postStarted = false;
context.apiFetch = (url, options) => {
  assert.equal(url, '/notifications/7/read');
  assert.equal(options.method, 'POST');
  postStarted = true;
  return new Promise(resolve => { finishPost = resolve; });
};
context.__setNotifTestState([
  { id: 7, title: '最新通知', created_at: '2026-07-29T00:00:00', read_at: null },
], 1);

const request = context.markNotifRead(7);
assert.equal(postStarted, true);
assert.notEqual(context.__getNotifTestState().items[0].read_at, null);
assert.equal(context.__getNotifTestState().unread, 0);
assert.doesNotMatch(body.innerHTML, /read-btn/);

finishPost({ ok: true, unread: 0 });
await request;
""",
    )


def test_pending_notification_read_survives_a_stale_menu_refresh():
    notification_source = "function esc(value) { return String(value); }\n" + source_between(
        "// ═══ In-App Notifications", "// ═══ Admin Panel"
    )
    notification_source += """
globalThis.__setNotifTestState = (items, unread) => {
  notifItems = items;
  notifUnread = unread;
  notifLoadState = 'ready';
};
globalThis.__getNotifTestState = () => ({
  items: notifItems.map(item => ({ ...item })),
  unread: notifUnread,
});
"""
    run_node(
        notification_source,
        """
const body = { innerHTML: '' };
const menuBadge = { style: {}, textContent: '' };
context.document = {
  querySelector: () => ({ classList: { toggle: () => {} } }),
  getElementById: id => id === 'notifBody' ? body : menuBadge,
};
context.authToken = 'token';
let finishPost;
context.apiFetch = (url) => {
  if (url.endsWith('/read')) {
    return new Promise(resolve => { finishPost = resolve; });
  }
  return Promise.resolve({
    items: [
      { id: 7, title: '最新通知', created_at: '2026-07-29T00:00:00', read_at: null },
    ],
    unread: 1,
  });
};
context.__setNotifTestState([
  { id: 7, title: '最新通知', created_at: '2026-07-29T00:00:00', read_at: null },
], 1);

const request = context.markNotifRead(7);
await context.refreshNotifStatus();
assert.notEqual(context.__getNotifTestState().items[0].read_at, null);
assert.equal(context.__getNotifTestState().unread, 0);

finishPost({ ok: true, unread: 0 });
await request;
""",
    )


def notification_source_with_action_state_helpers():
    source = "function esc(value) { return String(value); }\n" + source_between(
        "// ═══ In-App Notifications", "// ═══ Admin Panel"
    )
    return source + """
globalThis.__setNotifTestState = (items, unread) => {
  notifItems = items;
  notifUnread = unread;
  notifLoadState = 'ready';
};
globalThis.__getNotifTestState = () => ({
  items: notifItems.map(item => ({ ...item })),
  unread: notifUnread,
});
"""


def test_notification_list_uses_read_then_red_delete_without_view_button():
    source = notification_source_with_action_state_helpers()
    run_node(
        source,
        """
const body = { innerHTML: '' };
const menuBadge = { style: {}, textContent: '' };
context.document = {
  querySelector: () => ({ classList: { toggle: () => {} } }),
  getElementById: id => id === 'notifBody' ? body : menuBadge,
};
context.__setNotifTestState([
  { id: 7, title: '公告', created_at: '2026-07-29T00:00:00', read_at: null },
], 1);
context.renderNotifList();
assert.match(body.innerHTML, /markNotifRead\(7\)/);
assert.match(body.innerHTML, /deleteNotif\(7\)/);
assert.ok(body.innerHTML.indexOf('markNotifRead(7)') < body.innerHTML.indexOf('deleteNotif(7)'));
assert.doesNotMatch(body.innerHTML, />查看</);
assert.match(body.innerHTML, /notif-delete-btn/);
""",
    )
    assert 'id="notifListActions"' in HTML
    assert 'onclick="markAllNotifRead()"' in HTML
    assert 'onclick="deleteAllNotifications()"' in HTML
    assert '.notif-delete-btn{background:rgba(239,68,68,0.16)' in HTML
    assert '.notif-delete-btn{background:rgba(239,68,68,0.16);border-color:rgba(239,68,68,0.35);color:#ef4444' in HTML


def test_notification_detail_renders_read_and_delete_actions_for_an_unread_item():
    source = notification_source_with_action_state_helpers()
    run_node(
        source,
        """
const body = { innerHTML: '' };
const elements = {
  notifBody: body,
  notifBackBtn: { style: {} },
  notifPanelTitle: { textContent: '' },
  notifListActions: { style: {} },
  notifMenuBadge: { style: {}, textContent: '' },
};
context.document = {
  querySelector: () => ({ classList: { toggle: () => {} } }),
  getElementById: id => elements[id],
};
context.__setNotifTestState([
  { id: 7, title: '公告', body: '正文', format: 'plain', created_at: '2026-07-29T00:00:00', read_at: null },
], 1);
context.showNotifDetail(7);
assert.match(body.innerHTML, /markNotifRead\(7, true\)/);
assert.match(body.innerHTML, /deleteNotif\(7, true\)/);
assert.match(body.innerHTML, /notif-delete-primary/);
""",
    )


def test_delete_all_notifications_requires_confirmation_before_requesting():
    source = notification_source_with_action_state_helpers()
    run_node(
        source,
        """
const body = { innerHTML: '' };
const menuBadge = { style: {}, textContent: '' };
context.document = {
  querySelector: () => ({ classList: { toggle: () => {} } }),
  getElementById: id => id === 'notifBody' ? body : menuBadge,
};
context.authToken = 'token';
context.confirm = () => false;
context.apiFetch = () => { throw new Error('request must not be sent'); };
context.__setNotifTestState([
  { id: 7, title: '公告', created_at: '2026-07-29T00:00:00', read_at: null },
], 1);
assert.equal(await context.deleteAllNotifications(), false);
assert.equal(context.__getNotifTestState().items[0].id, 7);
assert.equal(context.__getNotifTestState().unread, 1);
""",
    )


def test_optimistic_notification_delete_filters_a_stale_refresh_and_rolls_back_on_error():
    source = notification_source_with_action_state_helpers()
    run_node(
        source,
        """
const body = { innerHTML: '' };
const menuBadge = { style: {}, textContent: '' };
context.document = {
  querySelector: () => ({ classList: { toggle: () => {} } }),
  getElementById: id => id === 'notifBody' ? body : menuBadge,
};
context.authToken = 'token';
context.showToast = () => {};
let rejectDelete;
context.apiFetch = url => {
  if (url === '/notifications/7') return new Promise((resolve, reject) => { rejectDelete = reject; });
  return Promise.resolve({
    items: [{ id: 7, title: '公告', created_at: '2026-07-29T00:00:00', read_at: null }],
    unread: 1,
  });
};
context.__setNotifTestState([
  { id: 7, title: '公告', created_at: '2026-07-29T00:00:00', read_at: null },
], 1);
const request = context.deleteNotif(7);
assert.deepEqual(context.__getNotifTestState().items, []);
assert.equal(context.__getNotifTestState().unread, 0);
await context.refreshNotifStatus();
assert.deepEqual(context.__getNotifTestState().items, []);
rejectDelete(new Error('offline'));
assert.equal(await request, false);
assert.equal(context.__getNotifTestState().items[0].id, 7);
assert.equal(context.__getNotifTestState().unread, 1);
""",
    )


def test_markdown_lists_keep_unordered_bullets_out_of_ordered_lists():
    markdown_source = """
function esc(value) {
  return String(value).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
""" + source_between("function renderMarkdownListBlocks", "function resolveImageUrl")
    run_node(
        markdown_source,
        """
const bullets = context.renderMarkdown('- first\\n- second');
assert.equal(bullets, '<ul><li>first</li><li>second</li></ul>');
assert.equal(bullets.includes('<ol>'), false);

const compactBullets = context.renderMarkdown('-first\\n-second');
assert.equal(compactBullets, '<ul><li>first</li><li>second</li></ul>');

const ordered = context.renderMarkdown('1. first\\n2. second');
assert.equal(ordered, '<ol><li>first</li><li>second</li></ol>');

const spacedOrdered = context.renderMarkdown('1. first\\n\\n2. second\\n\\n3. third');
assert.equal(spacedOrdered, '<ol><li>first</li><li>second</li><li>third</li></ol>');

const orderedThenBullets = context.renderMarkdown('1. first\\n\\n2. second\\n\\n- bullet');
assert.equal(orderedThenBullets, '<ol><li>first</li><li>second</li></ol></p><p><ul><li>bullet</li></ul>');

const orderedThenParagraph = context.renderMarkdown('1. first\\n\\n2. second\\n\\nA paragraph');
assert.equal(orderedThenParagraph, '<ol><li>first</li><li>second</li></ol></p><p>A paragraph');

const skippedSourceNumber = context.renderMarkdown('1. first\\n\\n3. third');
assert.equal(skippedSourceNumber, '<ol><li>first</li><li>third</li></ol>');
""",
    )


def test_notification_markdown_uses_a_bounded_rendering_container():
    css = HTML[HTML.index(".notif-detail-body{"):HTML.index("/* ═══ AI Settings Panel")]
    assert ".notif-markdown{" in css
    assert ".notif-markdown pre,.notif-markdown code{" in css
    assert ".notif-markdown img{" in css
    assert ".notif-markdown table{" in css
    assert 'id="notifPubPreview" class="notif-detail-body notif-markdown"' in HTML
    assert '<div class="notif-detail-body notif-markdown">${bodyHtml}</div>' in HTML


def test_notification_list_uses_compact_browser_local_dates_and_keeps_time_on_mobile():
    notification_source = source_between(
        "function formatNotifTime(",
        "function renderNotifList()",
    )
    run_node(
        notification_source,
        """
const now = new Date(2026, 6, 29, 12, 0);
assert.equal(context.formatNotifListTime('2026-07-29T09:05:00', now), '今天 09:05');
assert.equal(context.formatNotifListTime('2026-07-28T09:05:00', now), '昨天 09:05');
assert.equal(context.formatNotifListTime('2026-07-27T09:05:00', now), '前天 09:05');
assert.equal(context.formatNotifListTime('2026-07-26T09:05:00', now), '7-26 09:05');
""",
    )
    mobile_css = HTML[HTML.index('@media(max-width:640px){'):HTML.index('</style>')]
    assert '.notif-time{display:none}' not in mobile_css


def test_unread_notification_uses_hoverable_status_action_not_new_tag():
    assert 'class="notif-unread-action"' in HTML
    assert 'markNotifRead(${n.id})' in HTML
    assert '<span class="notification-new-tag">new</span>' not in source_between(
        "function renderNotifList()",
        "function retryNotifications()",
    )
    assert '.notif-unread-action::before{content:\'未读\'' in HTML
    assert '.notif-unread-action:hover::before,.notif-unread-action:focus-visible::before{content:\'已读\'' in HTML


def test_poll_refresh_job_rejects_terminal_status_for_another_job():
    poll = source_between("async function pollRefreshJob(", "function rebuildCategoryMap")
    run_node(
        poll,
        """
context.abortableDelay = async () => {};
context.requestRefreshStatusOnce = async () => ({
  job_id: 'other-job', status: 'completed', new_count: 99,
});
context.Date = { now: () => 0 };
await assert.rejects(
  context.pollRefreshJob('expected-job', 1000),
  error => /取代|变更|失效/.test(error.message) && !error.message.includes('other-job'),
);
""",
    )


def test_status_request_uses_abort_signal_with_caller_bounded_timeout():
    request = source_between("async function requestRefreshStatusOnce(", "async function pollRefreshJob(")
    run_node(
        request,
        """
const timers = [];
let fetchOptions;
context.authToken = 'token';
context.parseRefreshResponse = async () => ({ status: 'running' });
context.setTimeout = (callback, timeout) => { timers.push(timeout); return 7; };
context.clearTimeout = id => assert.equal(id, 7);
context.AbortController = class {
  constructor() { this.signal = { marker: 'status-signal' }; }
  abort() {}
};
context.fetch = async (url, options) => {
  assert.equal(url, '/auth/refresh/status?job_id=job-1');
  fetchOptions = options;
  return {};
};
await context.requestRefreshStatusOnce('job-1', 37);
assert.deepEqual(timers, [37]);
assert.equal(fetchOptions.signal.marker, 'status-signal');
""",
    )


def test_poll_refresh_job_caps_each_status_request_to_remaining_deadline():
    poll = source_between("async function pollRefreshJob(", "function rebuildCategoryMap")
    run_node(
        poll,
        """
let now = 0;
let requestedTimeout = null;
context.Date = { now: () => now };
context.abortableDelay = async timeout => {
  // First poll uses the short FIRST_POLL_DELAY_MS (300), capped by the
  // remaining deadline (1000) — so the shorter of the two wins here.
  assert.equal(timeout, 300);
  now = 600;
};
context.requestRefreshStatusOnce = async (jobId, timeout) => {
  assert.equal(jobId, 'job-1');
  requestedTimeout = timeout;
  return { job_id: 'job-1', status: 'completed', new_count: 2 };
};
const status = await context.pollRefreshJob('job-1', 1000);
assert.equal(status.new_count, 2);
// remaining (400) < 5s cap, so the shorter remaining wins.
assert.equal(requestedTimeout, 400);
""",
    )


def test_poll_refresh_job_caps_each_status_request_to_five_seconds():
    poll = source_between("async function pollRefreshJob(", "function rebuildCategoryMap")
    run_node(
        poll,
        """
let now = 0;
let requestedTimeout = null;
context.Date = { now: () => now };
context.abortableDelay = async () => { now += 300; };
context.requestRefreshStatusOnce = async (jobId, timeout) => {
  requestedTimeout = timeout;
  return { job_id: 'job-1', status: 'completed', new_count: 1 };
};
// A huge remaining deadline must NOT become a single per-request timeout —
// each status request is capped at 5s so one stuck request can't burn the
// whole budget (and, before, get doubled by an inner retry).
await context.pollRefreshJob('job-1', 200000);
assert.equal(requestedTimeout, 5000);
""",
    )


def test_poll_refresh_job_uses_steady_interval_after_first_poll():
    poll = source_between("async function pollRefreshJob(", "function rebuildCategoryMap")
    run_node(
        poll,
        """
let now = 0;
const delays = [];
context.Date = { now: () => now };
context.abortableDelay = async timeout => {
  delays.push(timeout);
  now += 100;
};
let call = 0;
context.requestRefreshStatusOnce = async () => {
  call++;
  if (call < 3) return { job_id: 'job-1', status: 'running' };
  return { job_id: 'job-1', status: 'completed', new_count: 0 };
};
await context.pollRefreshJob('job-1', 100000);
assert.deepEqual(delays, [300, 800, 800]);
""",
    )


def test_poll_refresh_job_tolerates_transient_failures_below_the_threshold():
    poll = source_between("async function pollRefreshJob(", "function rebuildCategoryMap")
    run_node(
        poll,
        """
let now = 0;
context.Date = { now: () => now };
context.abortableDelay = async () => { now += 100; };
context.isTransientRefreshError = error => error && error.transient === true;
let call = 0;
context.requestRefreshStatusOnce = async () => {
  call++;
  if (call <= 2) throw Object.assign(new Error('Load failed'), { transient: true });
  return { job_id: 'job-1', status: 'completed', new_count: 5 };
};
const status = await context.pollRefreshJob('job-1', 100000);
assert.equal(status.new_count, 5);
assert.equal(call, 3);
""",
    )


def test_poll_refresh_job_gives_up_after_too_many_consecutive_transient_failures():
    poll = source_between("async function pollRefreshJob(", "function rebuildCategoryMap")
    run_node(
        poll,
        """
let now = 0;
context.Date = { now: () => now };
context.abortableDelay = async () => { now += 100; };
context.isTransientRefreshError = error => error && error.transient === true;
let call = 0;
context.requestRefreshStatusOnce = async () => {
  call++;
  throw Object.assign(new Error('Load failed'), { transient: true });
};
await assert.rejects(
  context.pollRefreshJob('job-1', 100000),
  error => error.message === 'Load failed',
);
assert.equal(call, 3);
""",
    )


def test_poll_refresh_job_does_not_tolerate_non_transient_errors():
    poll = source_between("async function pollRefreshJob(", "function rebuildCategoryMap")
    run_node(
        poll,
        """
let now = 0;
context.Date = { now: () => now };
context.abortableDelay = async () => { now += 100; };
context.isTransientRefreshError = () => false;
let call = 0;
context.requestRefreshStatusOnce = async () => {
  call++;
  throw new Error('刷新失败，请稍后重试');
};
await assert.rejects(
  context.pollRefreshJob('job-1', 100000),
  error => error.message === '刷新失败，请稍后重试',
);
assert.equal(call, 1);
""",
    )


def test_status_poll_targets_exact_job_and_links_fetch_to_flow_abort():
    request = source_between("async function requestRefreshStatusOnce(", "async function pollRefreshJob(")
    run_node(
        request,
        """
const flow = new AbortController();
let fetchUrl = '';
let fetchSignal;
context.authToken = 'token';
context.parseRefreshResponse = async () => ({ job_id: 'job/a', status: 'running' });
context.fetch = (url, options) => {
  fetchUrl = url;
  fetchSignal = options.signal;
  return new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(
      Object.assign(new Error('cancelled'), { name: 'AbortError' })
    ));
  });
};
context.setTimeout = () => 9;
context.clearTimeout = () => {};
const pending = context.requestRefreshStatusOnce('job/a', 5000, flow.signal);
await Promise.resolve();
assert.equal(fetchUrl, '/auth/refresh/status?job_id=job%2Fa');
assert.notEqual(fetchSignal, flow.signal);
flow.abort();
await assert.rejects(pending, error => error.name === 'AbortError');
""",
    )


def test_poll_refresh_job_passes_exact_job_id_to_every_status_request():
    poll = source_between("async function pollRefreshJob(", "function rebuildCategoryMap")
    run_node(
        poll,
        """
let now = 0;
const calls = [];
const flow = new AbortController();
context.Date = { now: () => now };
context.abortableDelay = async (timeout, signal) => {
  assert.equal(signal, flow.signal);
  now += timeout;
};
context.requestRefreshStatusOnce = async (jobId, timeout, signal) => {
  calls.push([jobId, timeout, signal]);
  return { job_id: jobId, status: 'completed', new_count: 0 };
};
await context.pollRefreshJob('job-a', 5000, flow.signal);
assert.equal(calls.length, 1);
assert.equal(calls[0][0], 'job-a');
assert.equal(calls[0][2], flow.signal);
""",
    )


def trigger_context_setup(poll_body):
    return f"""
context.refreshInProgress = false;
context.refreshFlowGeneration = 0;
context.refreshFlowController = null;
context.authToken = 'token';
context.document = {{ hidden: false }};
context.filter = 'all';
context.currentPage = 1;
context.pageNavigationSequence = 3;
context.pageRequestSequence = 8;
context.pageRequestPendingSequence = 0;
context.pageNavigationPending = false;
context.cancelStartupEmptyRevalidation = () => {{}};
context.hideNewArticlesPrompt = () => {{}};
context.setRefreshProgressLabel = () => {{}};
context.showToast = message => context.toasts.push(message);
context.toasts = [];
context.requestRefreshOnce = async () => ({{ job_id: 'job-1', status: 'running' }});
context.pollRefreshJob = async () => {{ {poll_body} }};
context.refreshErrorMessage = error => error.message || error.error || 'failed';
context.hasBlockingOverlayOpen = () => context.overlayOpen;
context.overlayOpen = false;
context.setRefreshRunning = running => {{
  context.refreshInProgress = running;
  context.runningStates.push(running);
}};
context.runningStates = [];
context.showNewArticlesPrompt = () => context.promptStates.push(context.refreshInProgress);
context.promptStates = [];
context.consumePendingNewArticles = () => context.consumed++;
context.consumed = 0;
context.loadCalls = 0;
context.latestKnownTimestamp = 0;
context.latestNewsTimestamp = () => 0;
context.loadSinceCalls = [];
context.loadSince = async (timestamp, options) => {{
  context.loadSinceCalls.push({{ timestamp, manual: !!(options && options.manual) }});
  return 0;
}};
"""


def refresh_trigger_source():
    return source_between(
        "function mergeRefreshArticleIds(",
        "async function triggerRefresh()",
    ) + source_between("async function triggerRefresh()", "function setRefreshRunning")


def test_refresh_id_merge_normalizes_and_deduplicates():
    helpers = source_between(
        "function mergeRefreshArticleIds(",
        "async function triggerRefresh()",
    )
    run_node(
        helpers,
        """
const ids = vm.runInContext('new Set()', context);
assert.equal(context.mergeRefreshArticleIds(ids, [2, '3', 2, 0, null, 'bad']), 2);
assert.deepEqual(Array.from(ids).sort((a, b) => a - b), [2, 3]);
assert.equal(context.mergeRefreshArticleIds(ids, [{id: 3}, {id: 4}]), 3);
assert.deepEqual(Array.from(ids).sort((a, b) => a - b), [2, 3, 4]);
""",
    )


def test_manual_refresh_unifies_immediate_progress_and_terminal_ids_without_double_counting():
    trigger = refresh_trigger_source()
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 2, new_ids: [2, 3] };"
    )
    run_node(
        trigger,
        setup
        + """
context.window = { scrollY: 0 };
context.labels = [];
context.setRefreshProgressLabel = count => context.labels.push(count);
context.applyStreamedRefreshBatch = async () => true;
context.loadNewsPage = async () => true;
context.loadSince = async (timestamp, options) => {
  options.onDiscovered([{ id: 1 }, { id: 2 }]);
  return 2;
};
context.pollRefreshJob = async (jobId, timeout, signal, onProgress) => {
  onProgress({ new_count_so_far: 2, new_ids_so_far: [2, 3] });
  return { job_id: 'job-1', status: 'completed', new_count: 2, new_ids: [2, 3] };
};
await context.triggerRefresh();
assert.equal(context.labels.at(-1), 3);
assert.ok(context.toasts.includes('✅ 更新完成，新增 3 篇文章'));
""",
    )


def test_refresh_discovery_count_is_global_not_filter_scoped():
    trigger = refresh_trigger_source()
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 1, new_ids: [12] };"
    )
    run_node(
        trigger,
        setup
        + """
context.filter = 'cat:Tech';
context.window = { scrollY: 0 };
context.setRefreshProgressLabel = count => context.lastLabel = count;
context.loadNewsPage = async () => true;
context.loadSince = async (timestamp, options) => {
  options.onDiscovered([{ id: 10, source: 'Tech' }, { id: 11, source: 'News' }]);
  return 2;
};
await context.triggerRefresh();
assert.equal(context.lastLabel, 3);
assert.ok(context.toasts.includes('✅ 更新完成，新增 3 篇文章'));
""",
    )


def test_manual_refresh_does_not_guess_ids_from_terminal_count_when_ids_are_missing():
    trigger = refresh_trigger_source()
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 5 };"
    )
    run_node(
        trigger,
        setup
        + """
context.currentPage = 2;
await context.triggerRefresh();
assert.ok(context.toasts.includes('✅ 已是最新'));
assert.ok(!context.toasts.includes('✅ 更新完成，新增 5 篇文章'));
""",
    )


def test_retry_and_deleted_attempt_counts_do_not_inflate_final_set_or_toast():
    trigger = refresh_trigger_source()
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 1, new_ids: [4] };"
    )
    run_node(
        trigger,
        setup
        + """
context.window = { scrollY: 200 };
context.labels = [];
context.setRefreshProgressLabel = count => context.labels.push(count);
context.loadNewsPage = async () => true;
context.pollRefreshJob = async (jobId, timeout, signal, onProgress) => {
  // The diagnostic attempted count can be larger for an old/mixed producer.
  // Only the filtered actual-new ID stream is authoritative for the Set/Toast.
  onProgress({ new_count_so_far: 3, new_ids_so_far: [4] });
  return {
    job_id: 'job-1',
    status: 'completed',
    new_count: 1,
    new_ids: [4],
  };
};
await context.triggerRefresh();
assert.deepEqual(context.labels, [1]);
assert.ok(context.toasts.includes('✅ 更新完成，新增 1 篇文章'));
assert.ok(!context.toasts.includes('✅ 更新完成，新增 3 篇文章'));
""",
    )


def test_unified_refresh_count_adds_no_fetch_or_status_poll():
    trigger = refresh_trigger_source()
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 0, new_ids: [] };"
    )
    run_node(
        trigger,
        setup
        + """
context.startCalls = 0;
context.pollCalls = 0;
context.sinceCalls = 0;
context.requestRefreshOnce = async () => {
  context.startCalls++;
  return { job_id: 'job-1', status: 'running' };
};
context.pollRefreshJob = async () => {
  context.pollCalls++;
  return { job_id: 'job-1', status: 'completed', new_count: 0, new_ids: [] };
};
context.loadSince = async () => { context.sinceCalls++; return 0; };
await context.triggerRefresh();
assert.deepEqual(
  [context.startCalls, context.pollCalls, context.sinceCalls],
  [1, 1, 1],
);
""",
    )


def test_manual_refresh_feeds_new_articles_into_pending_queue_when_not_on_page_one():
    trigger = refresh_trigger_source()
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 3, new_ids: [101, 102, 103] };"
    )
    run_node(
        trigger,
        setup
        + """
context.currentPage = 2;
context.latestKnownTimestamp = 555;
await context.triggerRefresh();
// One immediate check (fired alongside the scrape job, regardless of outcome) and
// one completion-time check (since new_count > 0 and page 1's branch never ran).
assert.deepEqual(context.loadSinceCalls, [
  { timestamp: 555, manual: true },
  { timestamp: 555, manual: false },
]);
assert.ok(context.toasts.includes('✅ 更新完成，新增 3 篇文章'));
""",
    )


def test_manual_refresh_immediate_check_fires_even_when_job_finds_nothing_new():
    trigger = refresh_trigger_source()
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 0 };"
    )
    run_node(
        trigger,
        setup
        + """
context.currentPage = 2;
await context.triggerRefresh();
// The immediate "what's already in SQLite" check always fires — it's independent
// of the job's own outcome — but the completion-time re-check is skipped since
// new_count is 0.
assert.deepEqual(context.loadSinceCalls, [{ timestamp: 0, manual: true }]);
assert.ok(context.toasts.includes('✅ 已是最新'));
""",
    )


def test_manual_refresh_skips_completion_time_check_when_page_one_branch_already_applied():
    trigger = refresh_trigger_source()
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 4 };"
    )
    run_node(
        trigger,
        setup
        + """
context.loadNewsPage = async (page, options) => {
  context.pageRequestSequence++;
  context.pageRequestPendingSequence = context.pageRequestSequence;
  context.pageRequestPendingSequence = 0;
  return true;
};
await context.triggerRefresh();
// Immediate check still fires; completion-time check is redundant once page 1's
// own branch has already applied the new articles to the DOM.
assert.deepEqual(context.loadSinceCalls, [{ timestamp: 0, manual: true }]);
assert.equal(context.consumed, 1);
""",
    )


def test_manual_refresh_pending_queue_feed_prefers_latest_known_timestamp_over_page_snapshot():
    trigger = refresh_trigger_source()
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 1 };"
    )
    run_node(
        trigger,
        setup
        + """
context.currentPage = 3;
context.latestKnownTimestamp = 0; // nothing observed yet this session -> falls back
context.latestNewsTimestamp = () => 42;
await context.triggerRefresh();
assert.deepEqual(context.loadSinceCalls, [
  { timestamp: 42, manual: true },
  { timestamp: 42, manual: false },
]);
""",
    )


def test_manual_refresh_skips_apply_after_navigation_changed_then_returned():
    trigger = refresh_trigger_source()
    setup = trigger_context_setup(
        "context.pageNavigationSequence += 2; "
        "return { job_id: 'job-1', status: 'completed', new_count: 1 };"
    )
    run_node(
        trigger,
        setup
        + """
context.loadNewsPage = async () => { context.loadCalls++; return true; };
await context.triggerRefresh();
assert.equal(context.loadCalls, 0);
assert.equal(context.consumed, 0);
assert.deepEqual(context.runningStates, [true, false]);
assert.deepEqual(context.promptStates, [false]);
""",
    )


def test_manual_refresh_skips_apply_while_page_navigation_is_pending():
    trigger = refresh_trigger_source()
    setup = trigger_context_setup(
        "context.pageNavigationPending = true; "
        "return { job_id: 'job-1', status: 'completed', new_count: 1 };"
    )
    run_node(
        trigger,
        setup
        + """
context.loadNewsPage = async () => { context.loadCalls++; return true; };
await context.triggerRefresh();
assert.equal(context.loadCalls, 0);
assert.equal(context.consumed, 0);
assert.deepEqual(context.promptStates, [false]);
""",
    )


def test_manual_refresh_does_not_replace_a_list_request_already_in_flight():
    trigger = refresh_trigger_source()
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 1 };"
    )
    run_node(
        trigger,
        setup
        + """
context.filter = 'cat:Tech';
context.pageRequestPendingSequence = context.pageRequestSequence;
context.abortCalls = 0;
context.pageRequestController = { abort: () => context.abortCalls++ };
context.loadNewsPage = async () => {
  context.loadCalls++;
  context.pageRequestController.abort();
  context.filter = 'all';
  return true;
};
await context.triggerRefresh();
assert.equal(context.loadCalls, 0);
assert.equal(context.abortCalls, 0);
assert.equal(context.filter, 'cat:Tech');
assert.equal(context.pageRequestPendingSequence, 8);
assert.equal(context.consumed, 0);
assert.deepEqual(context.runningStates, [true, false]);
assert.deepEqual(context.promptStates, [false]);
""",
    )


def test_manual_refresh_application_guard_blocks_overlay_opened_mid_load():
    trigger = refresh_trigger_source()
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 1 };"
    )
    run_node(
        trigger,
        setup
        + """
context.loadNewsPage = async (page, options) => {
  context.loadCalls++;
  assert.equal(typeof options.applicationGuard, 'function');
  context.pageRequestSequence++;
  context.pageRequestPendingSequence = context.pageRequestSequence;
  context.overlayOpen = true;
  const applied = options.applicationGuard();
  context.pageRequestPendingSequence = 0;
  return applied;
};
await context.triggerRefresh();
assert.equal(context.loadCalls, 1);
assert.equal(context.consumed, 0);
assert.deepEqual(context.promptStates, [false]);
""",
    )


def test_manual_refresh_consumes_pending_only_after_confirmed_application():
    trigger = refresh_trigger_source()
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 1 };"
    )
    run_node(
        trigger,
        setup
        + """
context.applied = false;
context.consumePendingNewArticles = () => {
  assert.equal(context.applied, true);
  assert.equal(context.refreshInProgress, true);
  context.consumed++;
};
context.loadNewsPage = async (page, options) => {
  context.loadCalls++;
  context.pageRequestSequence++;
  context.pageRequestPendingSequence = context.pageRequestSequence;
  assert.equal(options.applicationGuard(), true);
  context.applied = true;
  context.pageRequestPendingSequence = 0;
  return true;
};
await context.triggerRefresh();
assert.equal(context.loadCalls, 1);
assert.equal(context.consumed, 1);
assert.deepEqual(context.runningStates, [true, false]);
assert.deepEqual(context.promptStates, [false]);
""",
    )


def test_manual_refresh_reports_streaming_progress_on_button_label():
    trigger = refresh_trigger_source()
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 3, new_ids: [101, 102, 103] };"
    )
    run_node(
        trigger,
        setup
        + """
context.window = { scrollY: 0 };
context.labels = [];
context.setRefreshProgressLabel = count => context.labels.push(count);
context.applyStreamedRefreshBatch = async () => { context.applyCalls++; };
context.applyCalls = 0;
context.pollRefreshJob = async (jobId, timeout, signal, onProgress) => {
  onProgress({ new_count_so_far: 2, new_ids_so_far: [101, 102] });
  onProgress({ new_count_so_far: 2, new_ids_so_far: [101, 102] }); // no increase — must not relabel or reapply
  return { job_id: 'job-1', status: 'completed', new_count: 3, new_ids: [101, 102, 103] };
};
await context.triggerRefresh();
assert.deepEqual(context.labels, [2, 3]);
assert.equal(context.applyCalls, 1);
""",
    )


def test_manual_refresh_skips_streamed_apply_when_scrolled_away_from_top():
    trigger = refresh_trigger_source()
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 1 };"
    )
    run_node(
        trigger,
        setup
        + """
context.window = { scrollY: 200 };
context.labels = [];
context.setRefreshProgressLabel = count => context.labels.push(count);
context.applyStreamedRefreshBatch = async () => { context.applyCalls++; };
context.applyCalls = 0;
context.pollRefreshJob = async (jobId, timeout, signal, onProgress) => {
  onProgress({ new_count_so_far: 4, new_ids_so_far: [201, 202, 203, 204] });
  return { job_id: 'job-1', status: 'completed', new_count: 4, new_ids: [201, 202, 203, 204] };
};
await context.triggerRefresh();
assert.deepEqual(context.labels, [4]);
assert.equal(context.applyCalls, 0);
""",
    )


def test_manual_refresh_skips_streamed_apply_after_page_navigation_changed_view():
    trigger = refresh_trigger_source()
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 1 };"
    )
    run_node(
        trigger,
        setup
        + """
context.window = { scrollY: 0 };
context.applyStreamedRefreshBatch = async () => { context.applyCalls++; };
context.applyCalls = 0;
context.setRefreshProgressLabel = () => {};
context.pollRefreshJob = async (jobId, timeout, signal, onProgress) => {
  context.pageNavigationSequence += 1;
  onProgress({ new_count_so_far: 2 });
  return { job_id: 'job-1', status: 'completed', new_count: 2 };
};
await context.triggerRefresh();
assert.equal(context.applyCalls, 0);
""",
    )


def test_manual_refresh_streamed_apply_is_throttled_to_one_per_three_seconds():
    trigger = refresh_trigger_source()
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 1 };"
    )
    run_node(
        trigger,
        setup
        + """
context.window = { scrollY: 0 };
context.setRefreshProgressLabel = () => {};
let now = 10000; // start well past 0 so the first tick isn't starved by lastStreamedApplyAt=0
context.Date = { now: () => now };
context.applyStreamedRefreshBatch = async () => { context.applyCalls++; return true; };
context.applyCalls = 0;
// applyStreamedRefreshBatch is fire-and-forget (not awaited by handleRefreshProgress),
// so give its .finally() a couple of microtask turns to clear streamApplyInFlight
// between ticks, same as would happen across real pollRefreshJob's ~1.2s delay.
const flushMicrotasks = async () => { await Promise.resolve(); await Promise.resolve(); };
context.pollRefreshJob = async (jobId, timeout, signal, onProgress) => {
  onProgress({ new_count_so_far: 2 });
  await flushMicrotasks();
  now += 500; // well under the 3000ms throttle window
  onProgress({ new_count_so_far: 4 });
  await flushMicrotasks();
  now += 3000;
  onProgress({ new_count_so_far: 6 });
  await flushMicrotasks();
  return { job_id: 'job-1', status: 'completed', new_count: 6 };
};
await context.triggerRefresh();
assert.equal(context.applyCalls, 2);
""",
    )


def test_manual_refresh_flow_cancellation_aborts_poller_and_suppresses_stale_toast():
    helpers = source_between("function cancelRefreshFlow(", "function rebuildCategoryMap")
    trigger = refresh_trigger_source()
    run_node(
        helpers + trigger,
        """
context.refreshInProgress = false;
context.refreshFlowGeneration = 0;
context.refreshFlowController = null;
context.authToken = 'token';
context.filter = 'all';
context.currentPage = 1;
context.pageNavigationSequence = 0;
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestPendingSequence = 0;
context.pageNavigationPending = false;
context.document = { hidden: false };
context.cancelStartupEmptyRevalidation = () => {};
context.hideNewArticlesPrompt = () => {};
context.promptCalls = 0;
context.showNewArticlesPrompt = () => context.promptCalls++;
context.toasts = [];
context.showToast = message => context.toasts.push(message);
context.refreshErrorMessage = error => error.message || 'failed';
context.hasBlockingOverlayOpen = () => false;
context.consumePendingNewArticles = () => {};
context.loadNewsPage = async () => true;
context.latestKnownTimestamp = 0;
context.latestNewsTimestamp = () => 0;
context.loadSince = async () => 0;
context.labels = [];
context.setRefreshProgressLabel = count => context.labels.push(count);
context.runningStates = [];
context.setRefreshRunning = running => {
  context.refreshInProgress = running;
  context.runningStates.push(running);
};
context.requestRefreshOnce = async signal => {
  assert.equal(signal, context.refreshFlowController.signal);
  return { job_id: 'job-a', status: 'running' };
};
let markPollStarted;
const pollStarted = new Promise(resolve => { markPollStarted = resolve; });
let lateProgress;
context.pollRefreshJob = (jobId, timeout, signal, onProgress) => new Promise((resolve, reject) => {
  assert.equal(jobId, 'job-a');
  assert.equal(signal, context.refreshFlowController.signal);
  lateProgress = onProgress;
  markPollStarted();
  signal.addEventListener('abort', () => reject(
    Object.assign(new Error('cancelled'), { name: 'AbortError' })
  ));
});
const pending = context.triggerRefresh();
await pollStarted;
context.cancelRefreshFlow();
lateProgress({ new_count_so_far: 1, new_ids_so_far: [999] });
await pending;
assert.deepEqual(context.labels, []);
assert.deepEqual(context.runningStates, [true, false]);
assert.deepEqual(context.toasts, ['🔄 正在后台抓取...']);
assert.equal(context.promptCalls, 0);
assert.equal(context.refreshInProgress, false);
assert.equal(context.refreshFlowController, null);
""",
    )


def test_cancelled_delayed_immediate_check_cannot_pollute_replacement_refresh_count():
    cancel_helpers = source_between("function cancelRefreshFlow(", "function rebuildCategoryMap")
    trigger = refresh_trigger_source()
    incremental = source_between("async function loadSince(", "function rebuildSourceFilterGroups")
    run_node(
        cancel_helpers + trigger + incremental,
        """
context.refreshInProgress = false;
context.refreshFlowGeneration = 0;
context.refreshFlowController = null;
context.authToken = 'token';
context.filter = 'all';
context.currentPage = 1;
context.pageNavigationSequence = 0;
context.pageRequestSequence = 0;
context.pageRequestPendingSequence = 0;
context.pageNavigationPending = false;
context.document = { hidden: false };
context.window = { scrollY: 0 };
context.cancelStartupEmptyRevalidation = () => {};
context.hideNewArticlesPrompt = () => {};
context.showNewArticlesPrompt = () => {};
context.refreshErrorMessage = error => error.message || 'failed';
context.hasBlockingOverlayOpen = () => false;
context.latestNewsTimestamp = () => 100;
context.loadNewsPage = async () => true;
context.consumePendingNewArticles = () => {};
context.runningStates = [];
context.setRefreshRunning = running => {
  context.refreshInProgress = running;
  context.runningStates.push(running);
};
context.labels = [];
context.setRefreshProgressLabel = count => context.labels.push(count);
context.toasts = [];
context.showToast = message => context.toasts.push(message);

context.INCREMENTAL_FETCH_LIMIT = 200;
context.seenArticleIds = new Set();
context.latestKnownTimestamp = 100;
context.pendingNewArticleCount = 0;
context.pendingNewItems = [];
context.contentEpoch = 0;
context.bumpContentEpoch = () => { context.contentEpoch++; };
context.isSwFallbackResponse = () => false;
context.setTimeout = () => 1;
context.clearTimeout = () => {};
context.countRefreshes = 0;
context.refreshTodayArticleCount = () => { context.countRefreshes++; };
context.sourceLoads = 0;
context.loadSourceCategories = async () => { context.sourceLoads++; };
context.pageFetches = 0;
context.fetchNewsPage = async () => {
  context.pageFetches++;
  return { items: [{ id: 42, timestamp: 101, source: 's' }] };
};
context.writeCachedNewsPage = async () => {};
context.pendingRelevantCount = () => 1;
context.showLatestAfterIdle = () => {};
context.showNewArticlesPrompt = () => {};
context.applyCalls = 0;
context.applyNewsPage = () => { context.applyCalls++; };
context.lastUserActivityAt = 0;
context.IDLE_LATEST_DELAY_MS = 1;

let resolveImmediateA;
let resolveImmediateB;
let sinceFetchCalls = 0;
context.fetch = () => new Promise(resolve => {
  sinceFetchCalls++;
  if (sinceFetchCalls === 1) resolveImmediateA = resolve;
  else if (sinceFetchCalls === 2) resolveImmediateB = resolve;
  else throw new Error('unexpected incremental request');
});
const responseFor42 = () => ({
  json: async () => ({ items: [{ id: 42, timestamp: 101, source: 's' }] }),
});

let nextJob = 0;
context.requestRefreshOnce = async () => ({
  job_id: ++nextJob === 1 ? 'job-a' : 'job-b',
  status: 'running',
});
let markAPollStarted;
const aPollStarted = new Promise(resolve => { markAPollStarted = resolve; });
context.pollRefreshJob = (jobId, timeout, signal) => {
  if (jobId === 'job-a') {
    markAPollStarted();
    return new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(
      Object.assign(new Error('cancelled'), { name: 'AbortError' })
    )));
  }
  return Promise.resolve({
    job_id: 'job-b',
    status: 'completed',
    new_count: 0,
    new_ids: [],
  });
};

const flowA = context.triggerRefresh();
await aPollStarted;
context.cancelRefreshFlow();

// Flow B is active before A's ignored-abort fetch resolves. If A mutates
// seenArticleIds here, B will wrongly filter out ID 42 and finish with a zero Toast.
const flowB = context.triggerRefresh();
await Promise.resolve();
assert.equal(typeof resolveImmediateB, 'function');
resolveImmediateA(responseFor42());
await Promise.resolve();
await Promise.resolve();
assert.deepEqual(Array.from(context.seenArticleIds), []);
assert.equal(context.latestKnownTimestamp, 100);
assert.equal(context.pendingNewArticleCount, 0);
assert.equal(context.contentEpoch, 0);
assert.equal(context.sourceLoads, 0);
assert.equal(context.pageFetches, 0);
assert.equal(context.applyCalls, 0);

resolveImmediateB(responseFor42());
await flowB;
await flowA;

assert.deepEqual(Array.from(context.seenArticleIds), [42]);
assert.equal(context.latestKnownTimestamp, 101);
assert.equal(context.pendingNewArticleCount, 1);
assert.deepEqual(Array.from(context.pendingNewItems, item => item.id), [42]);
assert.equal(context.contentEpoch, 1);
assert.deepEqual(context.labels, [1]);
assert.deepEqual(context.toasts, [
  '🔄 正在后台抓取...',
  '🔄 正在后台抓取...',
  '✅ 更新完成，新增 1 篇文章',
]);
""",
    )


def test_incremental_check_overlapping_manual_refresh_only_queues_pending_items():
    incremental = source_between("async function loadSince(", "function rebuildSourceFilterGroups")
    run_node(
        incremental,
        """
context.refreshInProgress = true;
context.INCREMENTAL_FETCH_LIMIT = 200;
context.seenArticleIds = new Set();
context.latestKnownTimestamp = 100;
context.pendingNewArticleCount = 0;
context.pendingNewItems = [];
context.contentEpoch = 0;
context.bumpContentEpoch = () => { context.epochBumps = (context.epochBumps || 0) + 1; };
context.fetch = async () => ({
  json: async () => ({ items: [{ id: 2, timestamp: 101, source: 's' }] }),
});
context.isSwFallbackResponse = () => false;
context.setTimeout = () => 1;
context.clearTimeout = () => {};
context.refreshTodayArticleCount = () => { context.countRefreshes++; };
context.countRefreshes = 0;
context.loadSourceCategories = async () => { context.sourceLoads++; };
context.sourceLoads = 0;
context.fetchNewsPage = async () => { context.pageFetches++; return { items: [] }; };
context.pageFetches = 0;
context.writeCachedNewsPage = async () => {};
context.pendingRelevantCount = () => 1;
context.showLatestAfterIdle = () => { context.applies++; };
context.showNewArticlesPrompt = () => { context.applies++; };
context.applyNewsPage = () => { context.applies++; };
context.consumePendingNewArticles = () => { context.consumes++; };
context.applies = 0;
context.consumes = 0;
context.filter = 'all';
context.currentPage = 1;
context.window = { scrollY: 0 };
context.hasBlockingOverlayOpen = () => false;
context.lastUserActivityAt = 0;
context.IDLE_LATEST_DELAY_MS = 1;
const added = await context.loadSince(100);
assert.equal(added, 1);
assert.equal(context.epochBumps, 1);
assert.equal(context.pendingNewArticleCount, 1);
assert.deepEqual(Array.from(context.pendingNewItems, item => item.id), [2]);
assert.equal(context.countRefreshes, 1);
assert.equal(context.sourceLoads, 0);
assert.equal(context.pageFetches, 0);
assert.equal(context.applies, 0);
assert.equal(context.consumes, 0);
""",
    )


def test_manual_incremental_check_applies_immediately_despite_refresh_in_progress():
    incremental = source_between("async function loadSince(", "function rebuildSourceFilterGroups")
    run_node(
        incremental,
        """
context.refreshInProgress = true; // a manual-refresh job is running for its full duration
context.INCREMENTAL_FETCH_LIMIT = 200;
context.seenArticleIds = new Set();
context.latestKnownTimestamp = 100;
context.pendingNewArticleCount = 0;
context.pendingNewItems = [];
context.contentEpoch = 0;
context.bumpContentEpoch = () => { context.epochBumps = (context.epochBumps || 0) + 1; };
context.fetch = async () => ({
  json: async () => ({ items: [{ id: 2, timestamp: 101, source: 's' }] }),
});
context.isSwFallbackResponse = () => false;
context.setTimeout = () => 1;
context.clearTimeout = () => {};
context.refreshTodayArticleCount = () => { context.countRefreshes++; };
context.countRefreshes = 0;
context.loadSourceCategories = async () => { context.sourceLoads++; };
context.sourceLoads = 0;
context.fetchNewsPage = async () => { context.pageFetches++; return { items: [] }; };
context.pageFetches = 0;
context.writeCachedNewsPage = async () => {};
context.pendingRelevantCount = () => 1;
context.showLatestAfterIdle = () => { context.applies++; };
context.showNewArticlesPrompt = () => { context.applies++; };
context.applyNewsPage = () => { context.applies++; };
context.consumePendingNewArticles = () => { context.consumes++; };
context.applies = 0;
context.consumes = 0;
context.filter = 'all';
context.currentPage = 1;
context.window = { scrollY: 0 };
context.hasBlockingOverlayOpen = () => false;
context.lastUserActivityAt = 0;
context.IDLE_LATEST_DELAY_MS = 1;
// { manual: true } bypasses the defer-while-refreshing early return that the
// sibling test above (without `manual`) relies on.
context.discovered = [];
context.discoveryCalls = 0;
const recordDiscovery = items => {
  context.discoveryCalls++;
  context.discovered.push(...items.map(item => item.id));
};
const added = await context.loadSince(100, {
  manual: true,
  onDiscovered: recordDiscovery,
});
assert.equal(added, 1);
assert.deepEqual(context.discovered, [2]);
assert.equal(context.discoveryCalls, 1);
assert.equal(context.sourceLoads, 1);
assert.equal(context.pageFetches, 1);
assert.equal(context.applies, 1); // atLatestTop on page 1 -> applyNewsPage()
assert.equal(context.consumes, 1);
// The same response is filtered out by seenArticleIds on a later check, so the
// discovery callback must not run when there are no accepted new items.
assert.equal(await context.loadSince(100, {
  manual: true,
  onDiscovered: recordDiscovery,
}), 0);
assert.deepEqual(context.discovered, [2]);
assert.equal(context.discoveryCalls, 1);
""",
    )


# ─── contentEpoch: per-record stamping closes the TOCTOU window ───────────
#
# Code review (P1 x2) found that the original design — checking
# `epochAtStart === contentEpoch` immediately before an async cache write —
# left a race: a bump landing during that write's own await still let stale
# data reach the cache, and loadNewsPageRequest (the plain page-load path,
# not just pagination) never checked epoch at all. The fix stamps every
# stored record with the epoch that was live when the underlying fetch was
# *requested*, and rejects mismatched records at *read* time instead —
# closing the window regardless of when a bump lands, and covering every
# caller uniformly since the check lives in the shared read helpers.

def test_read_buffered_page_rejects_a_record_stamped_with_a_stale_epoch():
    buffer_fns = source_between("function rememberBufferedPage(", "async function prefetchNewsPage")
    run_node(
        buffer_fns,
        """
context.newsPageCacheKey = (page, activeFilter) => `page:${activeFilter}:${page}`;
context.PAGE_MEMORY_BUFFER_LIMIT = 10;
context.pageMemoryBuffer = new Map();
context.warmPageCoverImages = () => {};
context.contentEpoch = 1;
context.rememberBufferedPage(1, 'all', { items: [{ id: 1 }] });
context.contentEpoch = 2; // a bump landed after the record was stored
const stale = context.readBufferedPage(1, 'all');
assert.equal(stale, null);
// The stale entry must also be evicted, not just skipped, so it can't be
// served again after another read re-bumps back to the same epoch value.
assert.equal(context.pageMemoryBuffer.has('page:all:1'), false);
""",
    )


def test_read_buffered_page_accepts_a_record_stamped_with_the_live_epoch():
    buffer_fns = source_between("function rememberBufferedPage(", "async function prefetchNewsPage")
    run_node(
        buffer_fns,
        """
context.newsPageCacheKey = (page, activeFilter) => `page:${activeFilter}:${page}`;
context.PAGE_MEMORY_BUFFER_LIMIT = 10;
context.pageMemoryBuffer = new Map();
context.warmPageCoverImages = () => {};
context.contentEpoch = 3;
context.rememberBufferedPage(1, 'all', { items: [{ id: 1 }] });
const fresh = context.readBufferedPage(1, 'all');
assert.deepEqual(fresh, { items: [{ id: 1 }] });
""",
    )


def test_remember_buffered_page_stamps_the_epoch_passed_by_the_caller_not_the_live_one():
    buffer_fns = source_between("function rememberBufferedPage(", "async function prefetchNewsPage")
    run_node(
        buffer_fns,
        """
context.newsPageCacheKey = (page, activeFilter) => `page:${activeFilter}:${page}`;
context.PAGE_MEMORY_BUFFER_LIMIT = 10;
context.pageMemoryBuffer = new Map();
context.warmPageCoverImages = () => {};
context.contentEpoch = 9; // live epoch has already moved on by the time this lands
context.rememberBufferedPage(1, 'all', { items: [{ id: 1 }] }, 5); // stamped with the epoch at request time
const rejected = context.readBufferedPage(1, 'all');
assert.equal(rejected, null);
""",
    )


def test_read_cached_news_page_rejects_a_record_stamped_with_a_stale_epoch():
    cache_fns = source_between("async function readCachedNewsPage(", "async function cleanupNewsCache")
    run_node(
        cache_fns,
        """
const store = {};
context.newsPageCacheKey = (page, activeFilter) => `page:${activeFilter}:${page}`;
context.readNewsCacheEntry = async key => store[key] || null;
context.writeNewsCacheEntry = async record => { store[record.key] = record; };
context.NEWS_CACHE_MAX_AGE_MS = 999999999;
context.contentEpoch = 5;
await context.writeCachedNewsPage(1, 'all', { items: [{ id: 1 }] }); // stamps epoch=5 (default = live)
context.contentEpoch = 6; // a bump landed after the write completed
const result = await context.readCachedNewsPage(1, 'all');
assert.equal(result, null);
""",
    )


def test_write_cached_news_page_stamps_the_epoch_passed_by_the_caller_not_the_live_one():
    cache_fns = source_between("async function readCachedNewsPage(", "async function cleanupNewsCache")
    run_node(
        cache_fns,
        """
const store = {};
context.newsPageCacheKey = (page, activeFilter) => `page:${activeFilter}:${page}`;
context.readNewsCacheEntry = async key => store[key] || null;
context.writeNewsCacheEntry = async record => { store[record.key] = record; };
context.NEWS_CACHE_MAX_AGE_MS = 999999999;
context.contentEpoch = 9; // live epoch has already moved on by the time this write lands
await context.writeCachedNewsPage(1, 'all', { items: [{ id: 1 }] }, 5); // epoch at request time
assert.equal(store['page:all:1'].epoch, 5);
const rejected = await context.readCachedNewsPage(1, 'all');
assert.equal(rejected, null);
""",
    )


def test_read_cached_news_page_treats_a_pre_epoch_record_as_valid_at_epoch_zero():
    cache_fns = source_between("async function readCachedNewsPage(", "async function cleanupNewsCache")
    run_node(
        cache_fns,
        """
const store = {
  'page:all:1': { key: 'page:all:1', kind: 'page', updatedAt: Date.now(), data: { items: [{ id: 1 }] } },
};
context.newsPageCacheKey = (page, activeFilter) => `page:${activeFilter}:${page}`;
context.readNewsCacheEntry = async key => store[key] || null;
context.writeNewsCacheEntry = async record => { store[record.key] = record; };
context.NEWS_CACHE_MAX_AGE_MS = 999999999;
context.contentEpoch = 0; // fresh session, no bump has ever happened yet
const result = await context.readCachedNewsPage(1, 'all');
assert.deepEqual(result, { items: [{ id: 1 }] });
""",
    )


def test_load_news_page_stamps_cache_writes_with_the_epoch_captured_at_request_start():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
context.contentEpoch = 5;
context.pageRequestSequence = 0;
context.pageRequestController = null;
context.news = [];
context.readCachedNewsPage = async () => null;
context.writeCalls = [];
context.rememberCalls = [];
context.writeCachedNewsPage = async (page, activeFilter, data, epoch) => {
  context.writeCalls.push(epoch);
  context.contentEpoch = 9; // simulate a bump landing while this write is in flight
};
context.rememberBufferedPage = (page, activeFilter, data, epoch) => { context.rememberCalls.push(epoch); };
context.applyNewsPage = () => {};
context.renderColdStartSkeleton = () => {};
context.fetchNewsPage = async () => ({ items: [{ id: 1 }], total: 1 });
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.renderColdStartError = () => {};
context.showToast = () => {};
context.currentTotal = 1;
context.document = { getElementById: () => ({ classList: { contains: () => false } }) };
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.setTimeout = () => 1;
context.clearTimeout = () => {};
const loaded = await context.loadNewsPage(1, {
  activeFilter: 'all',
  useCache: false,
  forceNetwork: true,
  userInitiated: true,
});
assert.equal(loaded, true);
// Must stamp with the epoch captured before the fetch (5), not the live value (9)
// a concurrent bump moved contentEpoch to while the write was in flight — otherwise
// stale data fetched under the old assumption would be marked fresh for the new epoch.
assert.deepEqual(context.writeCalls, [5]);
assert.deepEqual(context.rememberCalls, [5]);
""",
    )


def test_successful_empty_cold_start_revalidates_with_get_until_articles_arrive():
    startup = source_between(
        "function isStartupInitializationResponse(",
        "function renderSourceDeepLinkError",
    )
    run_node(
        startup,
        """
context.coldStartInitializationActive = false;
context.coldStartInitializationTimedOut = false;
context.startupCalibrationGeneration = 0;
context.startupCalibrationController = null;
context.startupCalibrationTimer = null;
context.filter = 'all';
context.currentPage = 1;
context.pageRequestSequence = 7;
context.document = { hidden: false };
context.renderList = () => context.renders.push(
  context.coldStartInitializationActive ? 'initializing' : 'settled'
);
context.renders = [];
context.applied = [];
context.applyNewsPage = data => context.applied.push(data.items.map(item => item.id));
context.scheduleAdjacentPagePrefetch = () => {};
context.fetchNewsPage = async (page, activeFilter, signal) => {
  context.fetchCalls.push({ page, activeFilter, signal });
  return { items: [{ id: 9 }], total: 1, page: 1 };
};
context.fetchCalls = [];
const scheduled = [];
let nextTimer = 0;
context.setTimeout = callback => {
  const timer = { id: ++nextTimer, callback };
  scheduled.push(timer);
  return timer.id;
};
context.clearTimeout = id => {
  const index = scheduled.findIndex(timer => timer.id === id);
  if (index >= 0) scheduled.splice(index, 1);
};
const initial = {
  items: [], total: 0, page: 1,
  diagnostics: { refresh_job: { status: 'running', trigger: 'startup' } },
};
const started = context.startStartupEmptyRevalidation(initial, {
  page: 1, activeFilter: 'all', requestSeq: 7, maxAttempts: 3, intervalMs: 5000,
});
assert.equal(started, true);
assert.deepEqual(context.renders, ['initializing']);
assert.equal(scheduled.length, 1);
await scheduled.shift().callback();
assert.equal(context.fetchCalls.length, 1);
assert.equal(context.fetchCalls[0].page, 1);
assert.equal(context.fetchCalls[0].activeFilter, 'all');
assert.ok(context.fetchCalls[0].signal);
assert.deepEqual(context.applied, [[9]]);
assert.equal(context.coldStartInitializationActive, false);
assert.equal(scheduled.length, 0);
""",
    )


def test_empty_cold_start_revalidation_stops_on_terminal_job_or_attempt_limit():
    startup = source_between(
        "function isStartupInitializationResponse(",
        "function renderSourceDeepLinkError",
    )
    run_node(
        startup,
        """
context.coldStartInitializationActive = false;
context.coldStartInitializationTimedOut = false;
context.startupCalibrationGeneration = 0;
context.startupCalibrationController = null;
context.startupCalibrationTimer = null;
context.filter = 'all';
context.currentPage = 1;
context.pageRequestSequence = 3;
context.document = { hidden: false };
context.renderList = () => context.renders++;
context.renders = 0;
context.applied = 0;
context.applyNewsPage = () => context.applied++;
context.scheduleAdjacentPagePrefetch = () => {};
const initial = {
  items: [], total: 0, page: 1,
  diagnostics: { refresh_job: { status: 'running', trigger: 'startup' } },
};
const timers = [];
let nextTimer = 0;
context.setTimeout = callback => {
  const timer = { id: ++nextTimer, callback };
  timers.push(timer);
  return timer.id;
};
context.clearTimeout = id => {
  const index = timers.findIndex(timer => timer.id === id);
  if (index >= 0) timers.splice(index, 1);
};
context.fetchNewsPage = async () => ({
  items: [], total: 0, page: 1,
  diagnostics: { refresh_job: { status: 'completed', trigger: 'startup' } },
});
context.startStartupEmptyRevalidation(initial, {
  page: 1, activeFilter: 'all', requestSeq: 3, maxAttempts: 2, intervalMs: 5000,
});
await timers.shift().callback();
assert.equal(context.applied, 1);
assert.equal(context.coldStartInitializationActive, false);
assert.equal(timers.length, 0);

context.fetchNewsPage = async () => initial;
context.startStartupEmptyRevalidation(initial, {
  page: 1, activeFilter: 'all', requestSeq: 3, maxAttempts: 1, intervalMs: 5000,
});
await timers.shift().callback();
assert.equal(context.coldStartInitializationActive, false);
assert.equal(context.coldStartInitializationTimedOut, true);
assert.equal(timers.length, 0);
""",
    )


def test_view_navigation_cancels_startup_revalidation_and_hidden_work_without_posting():
    startup = source_between(
        "function isStartupInitializationResponse(",
        "function renderSourceDeepLinkError",
    )
    run_node(
        startup,
        """
context.coldStartInitializationActive = false;
context.coldStartInitializationTimedOut = false;
context.startupCalibrationGeneration = 0;
context.startupCalibrationController = null;
context.startupCalibrationTimer = null;
context.filter = 'all';
context.currentPage = 1;
context.pageRequestSequence = 4;
context.document = { hidden: false };
context.renderList = () => {};
context.applyNewsPage = () => context.applied++;
context.applied = 0;
context.scheduleAdjacentPagePrefetch = () => {};
context.postCalls = 0;
context.requestRefreshOnce = () => { context.postCalls++; };
let resolveFetch;
context.fetchNewsPage = (page, activeFilter, signal) => new Promise((resolve, reject) => {
  resolveFetch = resolve;
  signal.addEventListener('abort', () => reject(
    Object.assign(new Error('cancelled'), { name: 'AbortError' })
  ));
});
const timers = [];
context.setTimeout = callback => { timers.push(callback); return timers.length; };
context.clearTimeout = () => {};
const initial = {
  items: [], total: 0, page: 1,
  diagnostics: { refresh_job: { status: 'running', trigger: 'startup' } },
};
context.startStartupEmptyRevalidation(initial, {
  page: 1, activeFilter: 'all', requestSeq: 4, maxAttempts: 3, intervalMs: 5000,
});
const inFlight = timers.shift()();
await Promise.resolve();
context.cancelStartupEmptyRevalidation();
await inFlight;
resolveFetch({ items: [{ id: 1 }], total: 1 });
await Promise.resolve();
assert.equal(context.applied, 0);
assert.equal(context.postCalls, 0);
assert.equal(context.coldStartInitializationActive, false);
""",
    )


def test_return_to_foreground_resumes_empty_startup_with_get_only():
    foreground = source_between(
        "function onReturnToForeground()",
        "document.addEventListener('visibilitychange', () =>",
    )
    run_node(
        foreground,
        """
context.Date = { now: () => 5000 };
context.lastForegroundSyncAt = 0;
context.pageVisibleSince = 0;
context.lastUserActivityAt = 0;
context.latestKnownTimestamp = 0;
context.latestNewsTimestamp = () => 0;
context.news = [];
context.filter = 'all';
context.currentPage = 1;
context.lastNewsDiagnostics = {
  refresh_job: { status: 'running', trigger: 'startup' },
};
context.loadSince = () => { context.incrementalCalls++; };
context.incrementalCalls = 0;
context.pollTitleUpdates = () => {};
context.authToken = 'token';
context.refreshNotifStatus = () => { context.notifRefreshCalls++; };
context.notifRefreshCalls = 0;
context.loadCalls = [];
context.loadNewsPage = (page, options) => context.loadCalls.push([page, options]);
context.postCalls = 0;
context.requestRefreshOnce = () => { context.postCalls++; };
context.sourceMetadataNetworkOk = false;
context.metadataRetries = [];
context.scheduleSourceMetadataRetry = opts => context.metadataRetries.push(opts);
context.onReturnToForeground();
assert.equal(context.incrementalCalls, 0);
assert.equal(context.loadCalls.length, 1);
assert.equal(context.loadCalls[0][0], 1);
assert.equal(context.loadCalls[0][1].activeFilter, 'all');
assert.equal(context.loadCalls[0][1].forceNetwork, true);
assert.equal(context.postCalls, 0);
// Metadata never loaded → foreground resume re-attempts immediately.
assert.deepEqual(context.metadataRetries, [{ immediate: true }]);
// Foreground resume also re-checks notifications (dot may have gone stale
// while the tab/PWA was frozen in the background).
assert.equal(context.notifRefreshCalls, 1);
""",
    )


def test_load_news_page_checks_application_guard_before_mutating_visible_list():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestController = null;
context.news = [{ id: 1 }];
context.readCachedNewsPage = async () => null;
context.rememberBufferedPage = () => {};
context.applyNewsPage = () => context.applied++;
context.applied = 0;
context.renderColdStartSkeleton = () => {};
context.fetchNewsPage = async () => ({ items: [{ id: 2 }], total: 1 });
context.writeCachedNewsPage = async () => {};
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.renderColdStartError = () => {};
context.showToast = () => {};
context.currentTotal = 1;
context.document = {
  getElementById: () => ({ classList: { contains: () => false } }),
};
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.setTimeout = () => 1;
context.clearTimeout = () => {};
const loaded = await context.loadNewsPage(1, {
  activeFilter: 'all',
  useCache: false,
  forceNetwork: true,
  applicationGuard: () => false,
});
assert.equal(loaded, false);
assert.equal(context.applied, 0);
""",
    )


def test_manual_refresh_completion_list_fetch_is_aborted_by_flow_signal():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestPendingSequence = 0;
context.pageRequestController = null;
context.news = [{ id: 1 }];
context.readCachedNewsPage = async () => null;
context.rememberBufferedPage = () => {};
context.applyNewsPage = () => context.applied++;
context.applied = 0;
context.renderColdStartSkeleton = () => {};
let capturedSignal;
let resolveFetch;
context.fetchNewsPage = (page, activeFilter, signal) => {
  capturedSignal = signal;
  return new Promise(resolve => { resolveFetch = resolve; });
};
context.writeCachedNewsPage = async () => {};
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.renderColdStartError = () => {};
context.showToast = () => {};
context.currentTotal = 1;
context.document = {
  getElementById: () => ({ classList: { contains: () => false } }),
};
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.setTimeout = () => 1;
context.clearTimeout = () => {};
const flow = new AbortController();
const pending = context.loadNewsPage(1, {
  activeFilter: 'all', useCache: false, forceNetwork: true,
  externalSignal: flow.signal,
});
await Promise.resolve();
assert.ok(capturedSignal);
assert.equal(capturedSignal.aborted, false);
flow.abort();
await Promise.resolve();
assert.equal(capturedSignal.aborted, true);
resolveFetch({ items: [{ id: 2 }], total: 1 });
const loaded = await pending;
assert.equal(loaded, false);
assert.equal(context.applied, 0);
""",
    )


def test_startup_empty_network_response_keeps_nonempty_cached_articles_visible():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestPendingSequence = 0;
context.pageRequestController = null;
context.news = [];
const cached = { items: [{ id: 1 }], total: 1, page: 1 };
const initializing = {
  items: [], total: 0, page: 1,
  diagnostics: { refresh_job: { status: 'running', trigger: 'startup' } },
};
context.readCachedNewsPage = async () => cached;
context.rememberBufferedPage = () => {};
context.applied = [];
context.applyNewsPage = data => {
  context.applied.push(data.items.map(item => item.id));
  context.news = data.items;
};
context.renderColdStartSkeleton = () => {};
context.fetchNewsPage = async () => initializing;
context.cacheWrites = 0;
context.writeCachedNewsPage = async () => { context.cacheWrites++; };
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.renderColdStartError = () => {};
context.showToast = () => {};
context.currentTotal = 1;
context.document = {
  getElementById: () => ({ classList: { contains: () => false } }),
};
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.setTimeout = () => 1;
context.clearTimeout = () => {};
context.isStartupInitializationResponse = data => data === initializing;
context.startCalls = 0;
context.startStartupEmptyRevalidation = () => { context.startCalls++; };
const loaded = await context.loadNewsPage(1, {
  activeFilter: 'all', useCache: true, forceNetwork: true,
});
assert.equal(loaded, true);
assert.deepEqual(context.applied, [[1]]);
assert.deepEqual(Array.from(context.news, item => item.id), [1]);
assert.equal(context.startCalls, 1);
assert.equal(context.cacheWrites, 0);
""",
    )


def test_load_news_page_checks_application_guard_before_applying_cached_list():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestController = null;
context.news = [{ id: 1 }];
context.readCachedNewsPage = async () => ({ items: [{ id: 2 }], total: 1 });
context.rememberBufferedPage = () => {};
context.applyNewsPage = () => context.applied++;
context.applied = 0;
context.renderColdStartSkeleton = () => {};
context.fetchNewsPage = async () => ({ items: [{ id: 3 }], total: 1 });
context.writeCachedNewsPage = async () => {};
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.renderColdStartError = () => {};
context.showToast = () => {};
context.currentTotal = 1;
context.document = {
  getElementById: () => ({ classList: { contains: () => false } }),
};
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.setTimeout = () => 1;
context.clearTimeout = () => {};
const loaded = await context.loadNewsPage(1, {
  activeFilter: 'all',
  useCache: true,
  forceNetwork: true,
  applicationGuard: () => false,
});
assert.equal(loaded, false);
assert.equal(context.applied, 0);
""",
    )


def test_overlapping_list_request_finally_cannot_clear_newer_pending_marker():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestPendingSequence = 0;
context.pageRequestController = null;
context.news = [{ id: 1 }];
context.readCachedNewsPage = async () => null;
context.rememberBufferedPage = () => {};
context.applyNewsPage = () => {};
context.renderColdStartSkeleton = () => {};
const resolvers = [];
context.fetchNewsPage = () => new Promise(resolve => resolvers.push(resolve));
context.writeCachedNewsPage = async () => {};
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.renderColdStartError = () => {};
context.showToast = () => {};
context.currentTotal = 1;
context.document = {
  getElementById: () => ({ classList: { contains: () => false } }),
};
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.setTimeout = () => 1;
context.clearTimeout = () => {};
const first = context.loadNewsPage(1, { activeFilter: 'all', useCache: false });
await Promise.resolve();
assert.equal(context.pageRequestPendingSequence, 1);
const second = context.loadNewsPage(1, { activeFilter: 'cat:Tech', useCache: false });
await Promise.resolve();
assert.equal(context.pageRequestPendingSequence, 2);
resolvers[0]({ items: [{ id: 2 }], total: 1 });
await first;
assert.equal(context.pageRequestPendingSequence, 2);
resolvers[1]({ items: [{ id: 3 }], total: 1 });
await second;
assert.equal(context.pageRequestPendingSequence, 0);
""",
    )


def test_cached_source_metadata_renders_while_network_revalidation_is_pending():
    load_sources = source_between("function persistSourceMetadata(", "function sourceLabel")
    run_node(
        load_sources,
        """
const events = [];
let resolveNetwork;
context.readNewsCacheEntry = async () => ({ data: { sources: ['cached'] } });
context.withCacheTimeout = async value => value;
context.withPromiseTimeout = async value => value;
context.rebuildCategoryMap = sources => events.push(`rebuild:${sources[0]}`);
context.renderFilters = () => events.push('render');
context.isRestrictedUser = () => false;
context.apiFetch = () => {
  events.push('network');
  return new Promise(resolve => { resolveNetwork = resolve; });
};
context.writeNewsCacheEntry = () => {};
context.delay = () => new Promise(() => {});
context.setTimeout = () => 1;
context.clearTimeout = () => {};
const loading = context.loadSourceCategories();
await new Promise(resolve => setImmediate(resolve));
assert.deepEqual(events, ['rebuild:cached', 'render', 'network']);
resolveNetwork({ sources: ['fresh'] });
await loading;
assert.deepEqual(events, [
  'rebuild:cached', 'render', 'network', 'rebuild:fresh', 'render',
]);
""",
    )


def test_cached_source_metadata_network_failure_uses_quiet_nonblocking_hint():
    load_sources = source_between("function persistSourceMetadata(", "function sourceLabel")
    run_node(
        load_sources,
        """
context.readNewsCacheEntry = async () => ({ data: { sources: ['cached'] } });
context.withCacheTimeout = async value => value;
context.rebuildCategoryMap = () => {};
context.renderFilters = () => {};
context.isRestrictedUser = () => false;
context.apiFetch = async () => { throw new Error('offline'); };
context.writeNewsCacheEntry = () => {};
context.delay = () => new Promise(() => {});
context.hasLoadedNewsOnce = true;
context.document = { hidden: false };
context.toasts = [];
context.showToast = message => context.toasts.push(message);
context.setTimeout = () => 1;
context.clearTimeout = () => {};
await context.loadSourceCategories();
assert.deepEqual(context.toasts, ['来源信息暂未更新，已显示缓存内容']);
context.toasts = [];
await context.loadSourceCategories({ quietNetworkError: true });
assert.deepEqual(context.toasts, []);
context.apiFetch = async () => { throw Object.assign(new Error('cancelled'), { name: 'AbortError' }); };
await context.loadSourceCategories();
assert.deepEqual(context.toasts, []);
""",
    )


def test_source_metadata_load_exits_after_refresh_flow_is_cancelled_during_cache_await():
    load_sources = source_between("function persistSourceMetadata(", "function sourceLabel")
    run_node(
        load_sources,
        """
let resolveCache;
context.readNewsCacheEntry = () => new Promise(resolve => { resolveCache = resolve; });
context.withCacheTimeout = async value => value;
context.events = [];
context.rebuildCategoryMap = sources => context.events.push(`rebuild:${sources[0]}`);
context.renderFilters = () => context.events.push('render');
context.isRestrictedUser = () => false;
context.apiFetch = async () => {
  context.events.push('network');
  return { sources: ['fresh'] };
};
context.writeNewsCacheEntry = () => context.events.push('cache-write');
context.delay = () => new Promise(() => {});
context.setTimeout = () => 1;
context.clearTimeout = () => {};
const controller = new AbortController();
let current = true;
const loading = context.loadSourceCategories({
  externalSignal: controller.signal,
  applicationGuard: () => current,
});
current = false;
controller.abort();
resolveCache({ data: { sources: ['stale'] } });
await loading;
assert.deepEqual(context.events, []);
""",
    )


def test_today_count_ignores_response_after_refresh_flow_cancellation():
    today_count = source_between("async function refreshTodayArticleCount(", "let dailySummaryState")
    run_node(
        today_count,
        """
context.beijingDateString = () => '2026-07-27';
context.countTodayArticles = () => context.todayArticleCount == null ? 0 : context.todayArticleCount;
context.todayArticleCount = null;
context.setTimeout = () => 1;
context.clearTimeout = () => {};
let resolveJson;
let requestSignal;
context.fetch = async (url, options) => {
  requestSignal = options.signal;
  return {
    ok: true,
    json: () => new Promise(resolve => { resolveJson = resolve; }),
  };
};
const controller = new AbortController();
let current = true;
const loading = context.refreshTodayArticleCount(8000, {
  externalSignal: controller.signal,
  applicationGuard: () => current,
});
while (typeof resolveJson !== 'function') await Promise.resolve();
current = false;
controller.abort();
resolveJson({ total: 9 });
await loading;
assert.equal(requestSignal.aborted, true);
assert.equal(context.todayArticleCount, null);
""",
    )


def test_source_metadata_retry_exits_when_owning_refresh_flow_is_cancelled():
    load_sources = source_between("function persistSourceMetadata(", "function sourceLabel")
    run_node(
        load_sources,
        """
context.sourceMetadataRetryScheduled = false;
context.SOURCE_METADATA_RETRY_DELAYS = [1];
context.SOURCE_METADATA_RETRY_TIMEOUT_MS = 25000;
let resumeDelay;
context.delay = () => new Promise(resolve => { resumeDelay = resolve; });
context.document = { hidden: false };
context.events = [];
context.fetchSourceMetadata = async () => {
  context.events.push('network');
  return { sources: ['late'] };
};
context.rebuildCategoryMap = () => context.events.push('rebuild');
context.renderFilters = () => context.events.push('render');
context.persistSourceMetadata = () => context.events.push('persist');
context.retrySourceDeepLink = () => context.events.push('deep-link');
const controller = new AbortController();
let current = true;
const retrying = context.scheduleSourceMetadataRetry({
  externalSignal: controller.signal,
  applicationGuard: () => current,
});
while (typeof resumeDelay !== 'function') await Promise.resolve();
current = false;
controller.abort();
resumeDelay();
await retrying;
assert.deepEqual(context.events, []);
assert.equal(context.sourceMetadataRetryScheduled, false);
""",
    )


def test_idle_latest_apply_exits_after_refresh_flow_cancellation_during_cache_write():
    show_latest = source_between("async function showLatestAfterIdle(", "function scheduleAdjacentPagePrefetch")
    run_node(
        show_latest,
        """
context.filter = 'all';
context.currentPage = 2;
context.contentEpoch = 5;
context.pendingRelevantCount = () => 1;
context.document = { hidden: false };
context.hasBlockingOverlayOpen = () => false;
context.lastUserActivityAt = 0;
context.IDLE_LATEST_DELAY_MS = 1;
context.Date = { now: () => 100 };
context.fetchNewsPage = async () => ({ items: [{ id: 7 }] });
let finishCacheWrite;
context.writeCachedNewsPage = () => new Promise(resolve => { finishCacheWrite = resolve; });
context.scrollCalls = 0;
context.scrollPageToTop = async () => { context.scrollCalls++; return true; };
context.applyCalls = 0;
context.applyNewsPage = () => { context.applyCalls++; };
context.consumePendingNewArticles = () => {};
context.syncListUrl = () => {};
const controller = new AbortController();
let current = true;
const applying = context.showLatestAfterIdle({
  externalSignal: controller.signal,
  applicationGuard: () => current,
});
while (typeof finishCacheWrite !== 'function') await Promise.resolve();
current = false;
controller.abort();
finishCacheWrite();
await applying;
assert.equal(context.currentPage, 2);
assert.equal(context.scrollCalls, 0);
assert.equal(context.applyCalls, 0);
""",
    )


def test_idle_latest_scroll_stops_after_owning_refresh_flow_is_cancelled():
    cancel_flow = source_between("function cancelRefreshFlow()", "function cancelViewBoundRefreshWork()")
    show_latest = source_between("async function showLatestAfterIdle(", "function scheduleAdjacentPagePrefetch")
    scroll_to_top = source_between("function scrollPageToTop(", "function stabilizePageTop")
    run_node(
        cancel_flow + show_latest + scroll_to_top,
        """
const frames = [];
context.requestAnimationFrame = callback => { frames.push(callback); return frames.length; };
context.performance = { now: () => 0 };
context.window = {
  scrollY: 100,
  scrollTo: () => { context.scrollCalls++; },
};
context.document = { hidden: false, documentElement: { scrollTop: 0 } };
context.activeScrollMotion = null;
context.PAGE_SWITCH_TOP_THRESHOLD = 4;
context.filter = 'all';
context.currentPage = 2;
context.contentEpoch = 5;
context.pendingNewArticleCount = 1;
context.pendingRelevantCount = () => context.pendingNewArticleCount;
context.hasBlockingOverlayOpen = () => false;
context.lastUserActivityAt = 0;
context.IDLE_LATEST_DELAY_MS = 1;
context.Date = { now: () => 100 };
context.fetchNewsPage = async () => ({ items: [{ id: 7 }] });
context.writeCachedNewsPage = async () => {};
context.applyCalls = 0;
context.applyNewsPage = () => { context.applyCalls++; };
context.consumeCalls = 0;
context.consumePendingNewArticles = () => { context.consumeCalls++; };
context.syncCalls = 0;
context.syncListUrl = () => { context.syncCalls++; };
context.scrollCalls = 0;
context.programmaticScrollUntil = 0;
context.refreshInProgress = true;
context.refreshFlowGeneration = 1;
const controller = new AbortController();
context.refreshFlowController = controller;
context.setRefreshRunning = running => { context.refreshInProgress = running; };
const applying = context.showLatestAfterIdle({
  externalSignal: controller.signal,
  applicationGuard: () => context.refreshFlowController === controller && !controller.signal.aborted,
});
while (frames.length === 0) await Promise.resolve();
// Let the motion begin, then invalidate its owning refresh flow before its next frame.
frames.shift()(0);
await Promise.resolve();
assert.equal(context.scrollCalls, 1);
assert.ok(frames.length > 0);
context.cancelRefreshFlow();
const scrollCallsAtCancel = context.scrollCalls;
const programmaticScrollAtCancel = context.programmaticScrollUntil;
while (frames.length) {
  frames.shift()(400);
  await Promise.resolve();
}
await applying;
assert.equal(context.scrollCalls, scrollCallsAtCancel);
assert.equal(context.applyCalls, 0);
assert.equal(context.consumeCalls, 0);
assert.equal(context.syncCalls, 0);
assert.equal(context.currentPage, 2);
assert.equal(context.pendingNewArticleCount, 1);
assert.equal(context.contentEpoch, 5);
assert.equal(context.programmaticScrollUntil, programmaticScrollAtCancel);
""",
    )


def test_bootstrap_starts_source_news_and_count_requests_without_serial_waits():
    bootstrap = source_between("async function bootstrapNews(", "// Initial load")
    run_node(
        bootstrap,
        """
const starts = [];
const resolvers = {};
const pending = name => {
  starts.push(name);
  return new Promise(resolve => { resolvers[name] = resolve; });
};
context.filter = 'all';
context.currentPage = 9;
context.location = { pathname: '/', search: '' };
context.pageNavigationSequence = 0;
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.listStateFromUrl = () => ({ activeFilter: 'cat:Tech' });
context.renderTopCatBar = () => starts.push('topbar');
context.loadSourceCategories = () => pending('sources');
context.loadNewsPage = (page, options) => {
  assert.equal(page, 1);
  assert.equal(options.activeFilter, 'cat:Tech');
  assert.equal(options.useCache, true);
  assert.equal(options.forceNetwork, true);
  assert.equal(options.networkRetries, 1);
  return pending('news');
};
context.refreshTodayArticleCount = () => pending('count');
context.renderFilters = () => starts.push('filters');
context.syncListUrl = () => starts.push('sync');
context.resetMobileColdStartScroll = () => starts.push('scroll');
const loading = context.bootstrapNews();
assert.deepEqual(starts, ['topbar', 'sources', 'news', 'count']);
assert.equal(context.filter, 'cat:Tech');
assert.equal(context.currentPage, 1);
resolvers.sources();
resolvers.news();
resolvers.count();
await loading;
assert.deepEqual(starts.slice(-3), ['filters', 'sync', 'scroll']);
""",
    )


def test_bootstrap_resolves_source_label_deep_link_after_metadata_hydration():
    list_state = source_between("function listStateFromUrl()", "async function restoreListStateFromUrl")
    bootstrap = source_between("async function bootstrapNews(", "// Initial load")
    run_node(
        list_state + bootstrap,
        """
const sourceKey = 'srcgrp:' + encodeURIComponent('财经早餐');
context.location = { pathname: '/', search: '?source=' + encodeURIComponent('财经早餐') + '&page=1' };
context.sourceFilterGroups = {};
context.CATEGORY_ORDER = ['News', 'Tech', 'Biz', 'Info'];
context.URLSearchParams = URLSearchParams;
context.filter = 'all';
context.currentPage = 9;
context.pageNavigationSequence = 0;
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.renderTopCatBar = () => {};
context.loadSourceCategories = async ({ onMetadataReady } = {}) => {
  await Promise.resolve();
  context.sourceFilterGroups[sourceKey] = {
    key: sourceKey,
    label: '财经早餐',
    sources: ['财经早餐'],
  };
  if (onMetadataReady) onMetadataReady();
};
context.requested = null;
context.loadNewsPage = async (page, options) => {
  context.requested = { page, activeFilter: options.activeFilter };
  return true;
};
context.refreshTodayArticleCount = async () => {};
context.renderFilters = () => {};
context.syncListUrl = () => {};
context.resetMobileColdStartScroll = () => {};
context.renderSourceDeepLinkError = () => {};
await context.bootstrapNews();
assert.deepEqual(context.requested, { page: 1, activeFilter: sourceKey });
assert.equal(context.filter, sourceKey);
assert.equal(context.currentPage, 1);
""",
    )


def test_source_deep_link_metadata_network_timeout_enters_dedicated_error_state():
    load_sources = source_between("function persistSourceMetadata(", "function sourceLabel")
    cache_timeout = source_between("async function withCacheTimeout(", "async function loadNewsPageRequest")
    bootstrap = source_between("async function bootstrapNews(", "// Initial load")
    run_node(
        load_sources + cache_timeout + bootstrap,
        """
const originalSearch = '?source=' + encodeURIComponent('财经早餐') + '&page=1';
context.location = { pathname: '/', search: originalSearch };
context.filter = 'all';
context.currentPage = 1;
context.pageNavigationSequence = 0;
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.listStateFromUrl = () => ({ activeFilter: 'all', page: 1 });
context.readNewsCacheEntry = async () => null;
context.rebuildCategoryMap = () => {};
context.renderFilters = () => {};
context.isRestrictedUser = () => false;
context.apiFetch = (url, options = {}) => new Promise((resolve, reject) => {
  if (options.signal) {
    options.signal.addEventListener('abort', () => reject(
      Object.assign(new Error('timed out'), { name: 'AbortError' })
    ));
  }
});
context.writeNewsCacheEntry = () => {};
context.delay = () => new Promise(() => {});
context.renderTopCatBar = () => {};
context.newsCalls = 0;
context.loadNewsPage = async () => { context.newsCalls++; return true; };
context.refreshTodayArticleCount = async () => {};
context.syncCalls = 0;
context.syncListUrl = () => context.syncCalls++;
context.resetMobileColdStartScroll = () => {};
context.sourceErrors = [];
context.renderSourceDeepLinkError = message => context.sourceErrors.push(message);
context.genericErrors = [];
context.renderColdStartError = message => context.genericErrors.push(message);
context.setTimeout = callback => setTimeout(callback, 5);
context.clearTimeout = clearTimeout;
const loading = context.bootstrapNews();
const outcome = await Promise.race([
  loading.then(() => 'done'),
  new Promise(resolve => setTimeout(() => resolve('hung'), 60)),
]);
assert.equal(outcome, 'done');
assert.equal(context.newsCalls, 0);
assert.equal(context.sourceErrors.length, 1);
assert.equal(context.genericErrors.length, 0);
assert.equal(context.location.search, originalSearch);
assert.equal(context.syncCalls, 0);
""",
    )


def test_each_fresh_source_timeout_aborts_its_fetch_and_clears_its_timer():
    load_sources = source_between("function persistSourceMetadata(", "function sourceLabel")
    cache_timeout = source_between("async function withCacheTimeout(", "async function loadNewsPageRequest")
    run_node(
        load_sources + cache_timeout,
        """
context.readNewsCacheEntry = async () => null;
context.rebuildCategoryMap = () => {};
context.renderFilters = () => {};
context.isRestrictedUser = () => false;
context.writeNewsCacheEntry = () => {};
context.delay = () => new Promise(() => {});
context.controllers = [];
context.AbortController = class {
  constructor() {
    const listeners = [];
    this.signal = {
      aborted: false,
      addEventListener(type, listener) { if (type === 'abort') listeners.push(listener); },
    };
    this.listeners = listeners;
    context.controllers.push(this);
  }
  abort() {
    if (this.signal.aborted) return;
    this.signal.aborted = true;
    this.listeners.forEach(listener => listener());
  }
};
context.activeFetches = 0;
context.receivedSignals = [];
context.apiFetch = (url, options) => {
  context.receivedSignals.push(options && options.signal);
  context.activeFetches++;
  return new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => {
      context.activeFetches--;
      reject(Object.assign(new Error('timed out'), { name: 'AbortError' }));
    });
  });
};
let timerId = 0;
context.clearedTimers = [];
context.setTimeout = callback => {
  const id = ++timerId;
  setImmediate(callback);
  return id;
};
context.clearTimeout = id => context.clearedTimers.push(id);
await context.loadSourceCategories({ useCache: false, networkTimeoutMs: 5 });
await context.loadSourceCategories({ useCache: false, networkTimeoutMs: 5 });
assert.equal(context.controllers.length, 2);
assert.equal(context.receivedSignals.length, 2);
assert.equal(context.receivedSignals[0], context.controllers[0].signal);
assert.equal(context.receivedSignals[1], context.controllers[1].signal);
assert.deepEqual(context.controllers.map(controller => controller.signal.aborted), [true, true]);
assert.equal(context.activeFetches, 0);
assert.deepEqual(context.clearedTimers, [1, 2]);
""",
    )


def test_source_deep_link_wait_does_not_override_newer_user_navigation():
    list_state = source_between("function listStateFromUrl()", "async function restoreListStateFromUrl")
    bootstrap = source_between("async function bootstrapNews(", "// Initial load")
    run_node(
        list_state + bootstrap,
        """
const sourceKey = 'srcgrp:' + encodeURIComponent('财经早餐');
context.location = {
  pathname: '/',
  search: '?source=' + encodeURIComponent('财经早餐') + '&page=1',
};
context.sourceFilterGroups = {};
context.CATEGORY_ORDER = ['News', 'Tech', 'Biz', 'Info'];
context.filter = 'all';
context.currentPage = 1;
context.pageNavigationSequence = 4;
context.pageRequestSequence = 7;
context.renderTopCatBar = () => {};
let notifyMetadata;
let resolveSources;
context.loadSourceCategories = ({ onMetadataReady } = {}) => {
  notifyMetadata = onMetadataReady;
  return new Promise(resolve => { resolveSources = resolve; });
};
context.newsCalls = [];
context.loadNewsPage = async (page, options) => {
  context.newsCalls.push({ page, activeFilter: options.activeFilter });
  return true;
};
context.refreshTodayArticleCount = async () => {};
context.renderFilters = () => {};
context.syncListUrl = () => {};
context.resetMobileColdStartScroll = () => {};
context.renderSourceDeepLinkError = () => {};
const loading = context.bootstrapNews();
assert.equal(typeof notifyMetadata, 'function');
context.location.search = '?category=Tech&page=1';
context.filter = 'cat:Tech';
context.currentPage = 1;
context.pageNavigationSequence++;
context.pageRequestSequence++;
context.sourceFilterGroups[sourceKey] = {
  key: sourceKey, label: '财经早餐', sources: ['财经早餐'],
};
notifyMetadata();
resolveSources();
await loading;
assert.deepEqual(context.newsCalls, []);
assert.equal(context.filter, 'cat:Tech');
assert.equal(context.location.search, '?category=Tech&page=1');
""",
    )


def test_source_retry_lock_releases_after_hung_today_count_is_aborted():
    list_state = source_between("function listStateFromUrl()", "async function restoreListStateFromUrl")
    today_count = source_between("async function refreshTodayArticleCount(", "let dailySummaryState")
    cold_start = source_between("function resetMobileColdStartScroll()", "// Initial load")
    run_node(
        list_state + today_count + cold_start,
        """
const originalSearch = '?source=' + encodeURIComponent('财经早餐') + '&page=1';
context.location = { pathname: '/', search: originalSearch };
context.sourceFilterGroups = {};
context.CATEGORY_ORDER = ['News', 'Tech', 'Biz', 'Info'];
context.filter = 'all';
context.currentPage = 1;
context.pageNavigationSequence = 0;
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.renderTopCatBar = () => {};
context.metadataCalls = 0;
context.loadSourceCategories = async ({ onMetadataReady } = {}) => {
  context.metadataCalls++;
  if (onMetadataReady) onMetadataReady();
};
context.loadNewsPage = async () => { throw new Error('must not load All'); };
context.renderFilters = () => {};
context.syncListUrl = () => {};
context.resetMobileColdStartScroll = () => {};
context.renderSourceDeepLinkError = () => {};
context.beijingDateString = () => '2026-07-16';
context.countTodayArticles = () => 0;
context.todayArticleCount = null;
context.countControllers = [];
context.AbortController = class {
  constructor() {
    const listeners = [];
    this.signal = {
      aborted: false,
      addEventListener(type, listener) { if (type === 'abort') listeners.push(listener); },
    };
    this.listeners = listeners;
    context.countControllers.push(this);
  }
  abort() {
    if (this.signal.aborted) return;
    this.signal.aborted = true;
    this.listeners.forEach(listener => listener());
  }
};
context.countFetchCalls = 0;
context.fetch = (url, options = {}) => {
  context.countFetchCalls++;
  if (context.countFetchCalls === 1) return Promise.resolve({ ok: false });
  return new Promise((resolve, reject) => {
    if (options.signal) {
      options.signal.addEventListener('abort', () => reject(
        Object.assign(new Error('timed out'), { name: 'AbortError' })
      ));
    }
  });
};
let nextTimer = 0;
const cancelledTimers = new Set();
context.setTimeout = callback => {
  const id = ++nextTimer;
  setImmediate(() => { if (!cancelledTimers.has(id)) callback(); });
  return id;
};
context.clearTimeout = id => cancelledTimers.add(id);
await context.bootstrapNews();
const first = context.retrySourceDeepLink();
const outcome = await Promise.race([
  first.then(value => ({ state: 'done', value })),
  new Promise(resolve => setTimeout(() => resolve({ state: 'hung' }), 60)),
]);
assert.deepEqual(outcome, { state: 'done', value: false });
assert.equal(context.countControllers.length, 2);
assert.equal(context.countControllers[0].signal.aborted, false);
assert.equal(context.countControllers[1].signal.aborted, true);
const second = await context.retrySourceDeepLink();
assert.equal(second, false);
assert.equal(context.metadataCalls, 3);
assert.equal(context.countControllers.length, 3);
assert.equal(context.countControllers[2].signal.aborted, true);
""",
    )


def test_source_deep_link_error_retry_refetches_metadata_and_reparses_original_url():
    list_state = source_between("function listStateFromUrl()", "async function restoreListStateFromUrl")
    cold_start = source_between("function resetMobileColdStartScroll()", "// Initial load")
    run_node(
        list_state + cold_start,
        """
const sourceKey = 'srcgrp:' + encodeURIComponent('财经早餐');
const originalSearch = '?source=' + encodeURIComponent('财经早餐') + '&page=1';
context.location = { pathname: '/', search: originalSearch };
context.sourceFilterGroups = {};
context.CATEGORY_ORDER = ['News', 'Tech', 'Biz', 'Info'];
context.filter = 'all';
context.currentPage = 1;
context.pageNavigationSequence = 0;
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.renderTopCatBar = () => {};
context.metadataCalls = 0;
context.loadSourceCategories = async ({ onMetadataReady } = {}) => {
  context.metadataCalls++;
  if (context.metadataCalls === 2) {
    context.sourceFilterGroups[sourceKey] = {
      key: sourceKey, label: '财经早餐', sources: ['财经早餐'],
    };
  }
  if (onMetadataReady) onMetadataReady();
};
context.newsCalls = [];
context.loadNewsPage = async (page, options) => {
  context.newsCalls.push({ page, activeFilter: options.activeFilter });
  return true;
};
context.refreshTodayArticleCount = async () => {};
context.renderFilters = () => {};
context.syncListUrl = () => {};
context.resetMobileColdStartScroll = () => {};
context.sourceErrors = [];
context.renderSourceDeepLinkError = message => context.sourceErrors.push(message);
context.renderColdStartError = () => {};
context.loadDataCalls = 0;
context.loadData = () => context.loadDataCalls++;
await context.bootstrapNews();
assert.equal(context.sourceErrors.length, 1);
assert.equal(context.newsCalls.length, 0);
assert.equal(context.location.search, originalSearch);
assert.equal(typeof context.retrySourceDeepLink, 'function');
await context.retrySourceDeepLink();
assert.equal(context.metadataCalls, 2);
assert.deepEqual(context.newsCalls, [{ page: 1, activeFilter: sourceKey }]);
assert.equal(context.loadDataCalls, 0);
assert.equal(context.location.search, originalSearch);
""",
    )


def test_load_news_page_surfaces_a_toast_for_sw_fallback_failures_even_when_quiet_otherwise():
    # loadNewsPageRequest() normally stays silent on failure when there's no cache
    # to fall back on but the page already has news rendered and this wasn't a
    # user-initiated load (routine background prefetch shouldn't nag the user).
    # A SW-fallback failure is the one exception — see isSwFallbackResponse().
    params = source_between("function buildNewsPageParams(", "function listUrlForState")
    fetch_page = source_between("function isSwFallbackResponse(", "function warmPageCoverImages")
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        params + fetch_page + load_page,
        """
context.PAGE_SIZE = 30;
context.sourceFilterGroups = {};
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestPendingSequence = 0;
context.pageRequestController = null;
context.news = [{ id: 1 }]; // page already has content -> not the empty cold-start case
context.readCachedNewsPage = async () => null; // cache miss
context.rememberBufferedPage = () => {};
context.applyNewsPage = () => {};
context.renderColdStartSkeleton = () => {};
context.writeCachedNewsPage = async () => {};
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.renderColdStartError = () => { throw new Error('must not hit the empty-list error path'); };
context.toasts = [];
context.showToast = message => context.toasts.push(message);
context.currentTotal = 0;
context.document = {
  getElementById: () => ({ classList: { contains: () => false } }),
};
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.buildNewsPageParams = () => new URLSearchParams();
context.fetch = async () => ({
  ok: true,
  headers: { get: name => (name === 'X-SW-Fallback' ? '1' : null) },
  json: async () => ({ items: [{ id: 2 }] }),
});
context.setTimeout = () => 1;
context.clearTimeout = () => {};
const loaded = await context.loadNewsPage(1, { activeFilter: 'all', useCache: false, forceNetwork: true });
assert.equal(loaded, false);
assert.deepEqual(context.toasts, ['内容可能不是最新，下拉或点击刷新重试']);
""",
    )


def test_load_news_page_stays_quiet_for_ordinary_background_prefetch_failures():
    # Regression guard for the branch above: a plain (non-SW-fallback) network
    # error in the same "cache miss, news already showing, not user-initiated"
    # shape must remain silent, same as before this change.
    params = source_between("function buildNewsPageParams(", "function listUrlForState")
    fetch_page = source_between("function isSwFallbackResponse(", "function warmPageCoverImages")
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        params + fetch_page + load_page,
        """
context.PAGE_SIZE = 30;
context.sourceFilterGroups = {};
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestPendingSequence = 0;
context.pageRequestController = null;
context.news = [{ id: 1 }];
context.readCachedNewsPage = async () => null;
context.rememberBufferedPage = () => {};
context.applyNewsPage = () => {};
context.renderColdStartSkeleton = () => {};
context.writeCachedNewsPage = async () => {};
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.renderColdStartError = () => { throw new Error('must not hit the empty-list error path'); };
context.toasts = [];
context.showToast = message => context.toasts.push(message);
context.currentTotal = 0;
context.document = {
  getElementById: () => ({ classList: { contains: () => false } }),
};
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.buildNewsPageParams = () => new URLSearchParams();
context.fetch = async () => { throw new TypeError('Failed to fetch'); };
context.setTimeout = () => 1;
context.clearTimeout = () => {};
const loaded = await context.loadNewsPage(1, { activeFilter: 'all', useCache: false, forceNetwork: true });
assert.equal(loaded, false);
assert.deepEqual(context.toasts, []);
""",
    )


def test_real_load_news_page_forwards_source_snapshot_to_fetch_query():
    params = source_between("function buildNewsPageParams(", "function listUrlForState")
    fetch_page = source_between("async function fetchNewsPage(", "function warmPageCoverImages")
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        params + fetch_page + load_page,
        """
const sourceKey = 'srcgrp:' + encodeURIComponent('财经早餐');
const sourceSnapshot = ['财经早餐', '财经早餐别名'];
context.PAGE_SIZE = 30;
context.sourceFilterGroups = {};
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestPendingSequence = 0;
context.pageRequestController = null;
context.news = [];
context.readCachedNewsPage = async () => null;
context.rememberBufferedPage = () => {};
context.applyNewsPage = () => {};
context.renderColdStartSkeleton = () => {};
context.writeCachedNewsPage = async () => {};
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.renderColdStartError = () => {};
context.showToast = () => {};
context.currentTotal = 0;
context.document = {
  getElementById: () => ({ classList: { contains: () => false } }),
};
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.requestUrl = '';
context.isSwFallbackResponse = () => false;
context.fetch = async (url, options) => {
  context.requestUrl = url;
  assert.ok(options.signal);
  return {
    ok: true,
    json: async () => ({ items: [{ id: 1 }], total: 1 }),
  };
};
context.setTimeout = () => 1;
context.clearTimeout = () => {};
const loaded = await context.loadNewsPage(1, {
  activeFilter: sourceKey,
  useCache: false,
  forceNetwork: true,
  sourceSnapshot,
});
assert.equal(loaded, true);
const query = new URLSearchParams(context.requestUrl.split('?')[1]);
assert.deepEqual(query.getAll('source'), sourceSnapshot);
assert.equal(context.pageRequestPendingSequence, 0);
""",
    )


def test_source_deep_link_snapshots_sources_before_metadata_revalidation_removes_group():
    params = source_between("function buildNewsPageParams(", "function listUrlForState")
    list_state = source_between("function listStateFromUrl()", "async function restoreListStateFromUrl")
    bootstrap = source_between("async function bootstrapNews(", "// Initial load")
    run_node(
        params + list_state + bootstrap,
        """
const sourceKey = 'srcgrp:' + encodeURIComponent('财经早餐');
context.location = {
  pathname: '/',
  search: '?source=' + encodeURIComponent('财经早餐') + '&page=1',
};
context.sourceFilterGroups = {};
context.CATEGORY_ORDER = ['News', 'Tech', 'Biz', 'Info'];
context.PAGE_SIZE = 30;
context.filter = 'all';
context.currentPage = 1;
context.pageNavigationSequence = 0;
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.renderTopCatBar = () => {};
context.loadSourceCategories = ({ onMetadataReady } = {}) => {
  context.sourceFilterGroups[sourceKey] = {
    key: sourceKey,
    label: '财经早餐',
    sources: ['财经早餐', '财经早餐别名'],
  };
  if (onMetadataReady) onMetadataReady();
  return new Promise(resolve => setImmediate(() => {
    context.sourceFilterGroups = {};
    resolve();
  }));
};
context.requestSources = null;
context.loadNewsPage = async (page, options) => {
  await new Promise(resolve => setImmediate(resolve));
  const built = context.buildNewsPageParams(page, options.activeFilter, options.sourceSnapshot);
  context.requestSources = built.getAll('source');
  return true;
};
context.refreshTodayArticleCount = async () => {};
context.renderFilters = () => {};
context.syncListUrl = () => {};
context.resetMobileColdStartScroll = () => {};
context.renderSourceDeepLinkError = () => {};
await context.bootstrapNews();
assert.deepEqual(context.requestSources, ['财经早餐', '财经早餐别名']);
""",
    )


def test_startup_empty_revalidation_times_out_hung_gets_and_reaches_bound():
    startup = source_between(
        "function isStartupInitializationResponse(",
        "function renderSourceDeepLinkError",
    )
    run_node(
        startup,
        """
context.coldStartInitializationActive = false;
context.coldStartInitializationTimedOut = false;
context.startupCalibrationGeneration = 0;
context.startupCalibrationController = null;
context.startupCalibrationTimer = null;
context.filter = 'all';
context.currentPage = 1;
context.pageRequestSequence = 12;
context.document = { hidden: false };
context.renderList = () => {};
context.applyNewsPage = () => { context.applied++; };
context.applied = 0;
context.scheduleAdjacentPagePrefetch = () => {};
context.fetchSignals = [];
context.fetchNewsPage = (page, activeFilter, signal) => {
  context.fetchSignals.push(signal);
  return new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => reject(
      Object.assign(new Error('timed out'), { name: 'AbortError' })
    ));
  });
};
let nextTimer = 0;
context.timers = [];
context.cleared = [];
context.setTimeout = (callback, timeout) => {
  const timer = { id: ++nextTimer, callback, timeout };
  context.timers.push(timer);
  return timer.id;
};
context.clearTimeout = id => context.cleared.push(id);
const initial = {
  items: [], total: 0, page: 1,
  diagnostics: { refresh_job: { status: 'running', trigger: 'startup' } },
};
context.startStartupEmptyRevalidation(initial, {
  page: 1, activeFilter: 'all', requestSeq: 12,
  maxAttempts: 2, intervalMs: 5000, requestTimeoutMs: 12000,
});
const firstInterval = context.timers.shift();
assert.equal(firstInterval.timeout, 5000);
const firstPoll = firstInterval.callback();
await Promise.resolve();
assert.equal(context.fetchSignals.length, 1);
assert.equal(context.fetchSignals[0].aborted, false);
assert.equal(context.timers.length, 1);
const firstRequestTimeout = context.timers.shift();
assert.equal(firstRequestTimeout.timeout, 12000);
firstRequestTimeout.callback();
await firstPoll;
assert.equal(context.fetchSignals[0].aborted, true);
assert.ok(context.cleared.includes(firstRequestTimeout.id));
assert.equal(context.timers.length, 1);

const secondInterval = context.timers.shift();
assert.equal(secondInterval.timeout, 5000);
const secondPoll = secondInterval.callback();
await Promise.resolve();
assert.equal(context.fetchSignals.length, 2);
const secondRequestTimeout = context.timers.shift();
assert.equal(secondRequestTimeout.timeout, 12000);
secondRequestTimeout.callback();
await secondPoll;
assert.equal(context.fetchSignals[1].aborted, true);
assert.ok(context.cleared.includes(secondRequestTimeout.id));
assert.equal(context.coldStartInitializationActive, false);
assert.equal(context.coldStartInitializationTimedOut, true);
assert.equal(context.timers.length, 0);
assert.equal(context.applied, 0);
""",
    )


def test_cold_start_network_retry_uses_a_fresh_abort_controller():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestPendingSequence = 0;
context.pageRequestController = null;
context.news = [];
context.readCachedNewsPage = async () => ({ items: [{ id: 'cached' }], total: 1 });
context.rememberBufferedPage = () => {};
context.applied = [];
context.applyNewsPage = (data, page, activeFilter, options) => context.applied.push({
  id: data.items[0].id,
  preserveDom: options && options.preserveDom,
});
context.renderColdStartSkeleton = () => {};
context.fetchSignals = [];
context.fetchNewsPage = async (page, activeFilter, signal) => {
  context.fetchSignals.push(signal);
  if (context.fetchSignals.length === 1) throw new Error('offline');
  return { items: [{ id: 'fresh' }], total: 1 };
};
context.writeCachedNewsPage = async () => {};
context.scheduleAdjacentPagePrefetch = () => {};
context.loading = [];
context.setPageLoading = state => context.loading.push(state);
context.renderColdStartError = () => { throw new Error('must preserve cache'); };
context.showToast = () => {};
context.currentTotal = 1;
context.document = {
  getElementById: () => ({ classList: { contains: () => false } }),
};
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.delayCalls = [];
context.delay = async milliseconds => context.delayCalls.push(milliseconds);
context.setTimeout = () => 1;
context.clearTimeout = () => {};
let nextController = 0;
context.AbortController = class {
  constructor() { this.signal = { controller: ++nextController }; }
  abort() {}
};
const loaded = await context.loadNewsPage(1, {
  activeFilter: 'all',
  useCache: true,
  forceNetwork: true,
  userInitiated: true,
  networkRetries: 1,
});
assert.equal(loaded, true);
assert.deepEqual(context.applied, [
  { id: 'cached', preserveDom: undefined },
  { id: 'fresh', preserveDom: true },
]);
assert.equal(context.fetchSignals.length, 2);
assert.notEqual(context.fetchSignals[0], context.fetchSignals[1]);
assert.deepEqual(context.fetchSignals.map(signal => signal.controller), [1, 2]);
assert.deepEqual(context.delayCalls, [600]);
assert.equal(context.pageRequestPendingSequence, 0);
""",
    )


def test_hung_page_cache_read_cannot_block_cold_start_network():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestPendingSequence = 0;
context.pageRequestController = null;
context.news = [];
context.readCachedNewsPage = () => new Promise(() => {});
context.rememberBufferedPage = () => {};
context.applied = [];
context.applyNewsPage = data => context.applied.push(data.items[0].id);
context.renderColdStartSkeleton = () => {};
context.fetchCalls = 0;
context.fetchNewsPage = async () => {
  context.fetchCalls++;
  return { items: [{ id: 'fresh' }], total: 1 };
};
context.writeCachedNewsPage = async () => {};
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.renderColdStartError = () => {};
context.showToast = () => {};
context.currentTotal = 0;
context.document = {
  getElementById: () => ({ classList: { contains: () => false } }),
};
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.setTimeout = callback => setTimeout(callback, 5);
context.clearTimeout = clearTimeout;
const loading = context.loadNewsPage(1, {
  activeFilter: 'all', useCache: true, forceNetwork: true,
});
const outcome = await Promise.race([
  loading.then(value => ({ state: 'done', value })),
  new Promise(resolve => setTimeout(() => resolve({ state: 'hung' }), 40)),
]);
assert.deepEqual(outcome, { state: 'done', value: true });
assert.equal(context.fetchCalls, 1);
assert.deepEqual(context.applied, ['fresh']);
""",
    )


def test_hung_page_cache_write_cannot_block_fresh_network_application():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestPendingSequence = 0;
context.pageRequestController = null;
context.news = [];
context.readCachedNewsPage = async () => null;
context.rememberBufferedPage = () => {};
context.applied = [];
context.applyNewsPage = data => context.applied.push(data.items[0].id);
context.renderColdStartSkeleton = () => {};
context.fetchNewsPage = async () => ({ items: [{ id: 'fresh' }], total: 1 });
context.writeCachedNewsPage = () => new Promise(() => {});
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.renderColdStartError = () => {};
context.showToast = () => {};
context.currentTotal = 0;
context.document = {
  getElementById: () => ({ classList: { contains: () => false } }),
};
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.setTimeout = callback => setTimeout(callback, 5);
context.clearTimeout = clearTimeout;
const loading = context.loadNewsPage(1, {
  activeFilter: 'all', useCache: true, forceNetwork: true,
});
const outcome = await Promise.race([
  loading.then(value => ({ state: 'done', value })),
  new Promise(resolve => setTimeout(() => resolve({ state: 'hung' }), 40)),
]);
assert.deepEqual(outcome, { state: 'done', value: true });
assert.deepEqual(context.applied, ['fresh']);
""",
    )


def test_network_timeout_aborts_first_attempt_then_retries_with_fresh_controller():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestPendingSequence = 0;
context.pageRequestController = null;
context.news = [];
context.readCachedNewsPage = async () => null;
context.rememberBufferedPage = () => {};
context.applied = [];
context.applyNewsPage = data => context.applied.push(data.items[0].id);
context.renderColdStartSkeleton = () => {};
context.controllers = [];
context.AbortController = class {
  constructor() {
    const listeners = [];
    this.signal = {
      aborted: false,
      addEventListener(type, listener) { if (type === 'abort') listeners.push(listener); },
    };
    this.listeners = listeners;
    context.controllers.push(this);
  }
  abort() {
    if (this.signal.aborted) return;
    this.signal.aborted = true;
    this.listeners.forEach(listener => listener());
  }
};
context.fetchCalls = 0;
context.fetchNewsPage = (page, activeFilter, signal) => {
  context.fetchCalls++;
  if (context.fetchCalls === 2) {
    return Promise.resolve({ items: [{ id: 'fresh' }], total: 1 });
  }
  return new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => reject(
      Object.assign(new Error('timed out'), { name: 'AbortError' })
    ));
  });
};
context.writeCachedNewsPage = async () => {};
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.renderColdStartError = () => {};
context.showToast = () => {};
context.currentTotal = 0;
context.document = {
  getElementById: () => ({ classList: { contains: () => false } }),
};
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.delay = async () => {};
const cancelled = new Set();
let nextTimer = 0;
let networkTimers = 0;
context.setTimeout = (callback, milliseconds) => {
  const id = ++nextTimer;
  if (milliseconds === 12000 && networkTimers++ === 0) {
    setImmediate(() => { if (!cancelled.has(id)) callback(); });
  }
  return id;
};
context.clearTimeout = id => cancelled.add(id);
const loaded = await context.loadNewsPage(1, {
  activeFilter: 'all', useCache: false, forceNetwork: true, networkRetries: 1,
});
assert.equal(loaded, true);
assert.equal(context.fetchCalls, 2);
assert.equal(context.controllers.length, 2);
assert.notEqual(context.controllers[0], context.controllers[1]);
assert.equal(context.controllers[0].signal.aborted, true);
assert.equal(context.controllers[1].signal.aborted, false);
assert.deepEqual(context.applied, ['fresh']);
""",
    )


def test_retry_stops_when_an_overlapping_request_supersedes_the_owner():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestPendingSequence = 0;
context.pageRequestController = null;
context.news = [{ id: 'visible' }];
context.readCachedNewsPage = async () => null;
context.rememberBufferedPage = () => {};
context.applied = [];
context.applyNewsPage = data => context.applied.push(data.items[0].id);
context.renderColdStartSkeleton = () => {};
context.fetches = [];
context.fetchNewsPage = async (page, activeFilter) => {
  context.fetches.push(activeFilter);
  if (activeFilter === 'all') throw new Error('offline');
  return { items: [{ id: 'tech' }], total: 1 };
};
context.writeCachedNewsPage = async () => {};
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.errors = [];
context.renderColdStartError = message => context.errors.push(message);
context.toasts = [];
context.showToast = message => context.toasts.push(message);
context.currentTotal = 1;
context.document = {
  getElementById: () => ({ classList: { contains: () => false } }),
};
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
let releaseRetry;
context.delay = () => new Promise(resolve => { releaseRetry = resolve; });
context.setTimeout = () => 1;
context.clearTimeout = () => {};
const first = context.loadNewsPage(1, {
  activeFilter: 'all', useCache: false, userInitiated: true, networkRetries: 1,
});
for (let turn = 0; turn < 5 && !releaseRetry; turn++) await Promise.resolve();
assert.equal(typeof releaseRetry, 'function');
const second = context.loadNewsPage(1, {
  activeFilter: 'cat:Tech', useCache: false, userInitiated: true,
});
assert.equal(await second, true);
releaseRetry();
assert.equal(await first, false);
assert.deepEqual(context.fetches, ['all', 'cat:Tech']);
assert.deepEqual(context.applied, ['tech']);
assert.deepEqual(context.errors, []);
assert.deepEqual(context.toasts, []);
assert.equal(context.pageRequestPendingSequence, 0);
""",
    )


def test_final_cold_start_failure_keeps_cached_articles_visible():
    load_page = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
    run_node(
        load_page,
        """
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestPendingSequence = 0;
context.pageRequestController = null;
context.news = [];
context.readCachedNewsPage = async () => ({ items: [{ id: 'cached' }], total: 1 });
context.rememberBufferedPage = () => {};
context.applied = [];
context.applyNewsPage = data => context.applied.push(data.items[0].id);
context.renderColdStartSkeleton = () => {};
context.fetchCalls = 0;
context.fetchNewsPage = async () => {
  context.fetchCalls++;
  throw Object.assign(new Error('timeout'), { name: 'AbortError' });
};
context.writeCachedNewsPage = async () => {};
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.errors = [];
context.renderColdStartError = message => context.errors.push(message);
context.toasts = [];
context.showToast = message => context.toasts.push(message);
context.currentTotal = 1;
context.document = {
  getElementById: () => ({ classList: { contains: () => false } }),
};
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.delay = async () => {};
context.setTimeout = () => 1;
context.clearTimeout = () => {};
const loaded = await context.loadNewsPage(1, {
  activeFilter: 'all',
  useCache: true,
  forceNetwork: true,
  userInitiated: true,
  networkRetries: 1,
});
assert.equal(loaded, true);
assert.equal(context.fetchCalls, 2);
assert.deepEqual(context.applied, ['cached']);
assert.deepEqual(context.errors, []);
assert.deepEqual(context.toasts, ['连接超时，请稍后重试']);
""",
    )


def test_manual_refresh_failure_restores_prompt_only_after_running_state_clears():
    trigger = refresh_trigger_source()
    setup = trigger_context_setup("throw Object.assign(new Error('timeout'), { name: 'AbortError' });")
    run_node(
        trigger,
        setup
        + """
context.loadNewsPage = async () => { throw new Error('must not load'); };
await context.triggerRefresh();
assert.deepEqual(context.runningStates, [true, false]);
assert.deepEqual(context.promptStates, [false]);
assert.equal(context.refreshInProgress, false);
""",
    )


def test_logo_is_keyboard_semantic_and_ignores_reentrant_activation():
    assert 'role="button"' in HTML[HTML.index('class="logo"'):HTML.index('</div>', HTML.index('class="logo"'))]
    assert 'tabindex="0"' in HTML[HTML.index('class="logo"'):HTML.index('</div>', HTML.index('class="logo"'))]
    assert "function handleLogoKeydown(event)" in HTML

    logo = source_between("async function refreshHomepage()", "async function scrollToTopAndCheckLatest")
    run_node(
        logo,
        """
context.logoRefreshInProgress = false;
context.cancelViewBoundRefreshWork = () => {};
context.programmaticScrollUntil = 0;
context.pendingLatestPage = null;
context.filter = 'all';
context.currentPage = 1;
context.applyNewsPage = () => {};
context.consumePendingNewArticles = () => {};
context.syncListUrl = () => {};
context.showToast = () => {};
context.stabilizePageTop = () => {};
context.latestKnownTimestamp = 10;
context.latestNewsTimestamp = () => 10;
context.loadSince = async () => {};
context.scrollPageToTop = async ({ onNearTop }) => { onNearTop(); return true; };
const resolvers = [];
context.prepareCalls = 0;
context.preparePageNavigation = () => {
  context.prepareCalls++;
  return new Promise(resolve => resolvers.push(resolve));
};
const first = context.refreshHomepage();
await Promise.resolve();
const second = context.refreshHomepage();
await Promise.resolve();
assert.equal(context.prepareCalls, 1);
resolvers.forEach(resolve => resolve({ items: [], total: 0 }));
await Promise.all([first, second]);
assert.equal(context.logoRefreshInProgress, false);
""",
    )


def test_logo_keyboard_handler_activates_only_enter_or_space():
    handler = source_between("function handleLogoKeydown(event)", "async function refreshHomepage()")
    run_node(
        handler,
        """
context.activations = 0;
context.refreshHomepage = () => context.activations++;
let prevented = 0;
const event = key => ({ key, preventDefault: () => prevented++ });
context.handleLogoKeydown(event('Escape'));
context.handleLogoKeydown(event('Enter'));
context.handleLogoKeydown(event(' '));
assert.equal(context.activations, 2);
assert.equal(prevented, 2);
""",
    )


def test_manual_refresh_cancels_pending_startup_calibration_before_updating():
    trigger = refresh_trigger_source()
    run_node(
        trigger,
        """
context.refreshInProgress = false;
context.refreshFlowGeneration = 0;
context.refreshFlowController = null;
context.authToken = 'token';
context.filter = 'all';
context.currentPage = 2;
context.pageNavigationSequence = 2;
context.pageRequestSequence = 4;
context.pageRequestPendingSequence = 0;
context.pageNavigationPending = false;
context.document = { hidden: false };
context.hideNewArticlesPrompt = () => {};
context.promptCalls = 0;
context.showNewArticlesPrompt = () => { context.promptCalls++; };
context.toasts = [];
context.showToast = message => context.toasts.push(message);
context.refreshErrorMessage = error => error.message || error.error || 'failed';
context.hasBlockingOverlayOpen = () => false;
context.consumePendingNewArticles = () => {};
context.loadNewsPage = async () => { throw new Error('page 2 must not calibrate'); };
context.latestKnownTimestamp = 0;
context.latestNewsTimestamp = () => 0;
context.loadSinceCalls = [];
context.loadSince = async (timestamp, options) => {
  context.loadSinceCalls.push({ timestamp, manual: !!(options && options.manual) });
  return 0;
};
context.runningStates = [];
context.setRefreshRunning = running => {
  context.refreshInProgress = running;
  context.runningStates.push(running);
};
const oldController = new AbortController();
let oldApplied = 0;
const oldGet = new Promise(resolve => {
  oldController.signal.addEventListener('abort', () => resolve('aborted'));
}).then(outcome => {
  if (outcome !== 'aborted') oldApplied++;
});
context.cancelCalls = 0;
context.cancelStartupEmptyRevalidation = () => {
  context.cancelCalls++;
  oldController.abort();
};
context.requestRefreshOnce = async () => ({ job_id: 'manual-job', status: 'running' });
let resolveManual;
let markManualPolling;
const manualPolling = new Promise(resolve => { markManualPolling = resolve; });
context.pollRefreshJob = () => new Promise(resolve => {
  resolveManual = resolve;
  markManualPolling();
});
const manual = context.triggerRefresh();
await manualPolling;
assert.equal(context.cancelCalls, 1);
assert.equal(oldController.signal.aborted, true);
await oldGet;
assert.equal(oldApplied, 0);
assert.equal(context.refreshInProgress, true);
assert.deepEqual(context.runningStates, [true]);
assert.deepEqual(context.toasts, ['🔄 正在后台抓取...']);
assert.equal(context.promptCalls, 0);
resolveManual({ job_id: 'manual-job', status: 'completed', new_count: 0 });
await manual;
assert.deepEqual(context.runningStates, [true, false]);
assert.equal(context.refreshInProgress, false);
assert.ok(context.toasts.includes('✅ 已是最新'));
assert.deepEqual(context.loadSinceCalls, [{ timestamp: 0, manual: true }]);
""",
    )


def test_startup_empty_revalidation_has_wall_clock_deadline_for_hung_get():
    startup = source_between(
        "function isStartupInitializationResponse(",
        "function renderSourceDeepLinkError",
    )
    run_node(
        startup,
        """
let now = 0;
context.Date = { now: () => now };
context.coldStartInitializationActive = false;
context.coldStartInitializationTimedOut = false;
context.startupCalibrationGeneration = 0;
context.startupCalibrationController = null;
context.startupCalibrationTimer = null;
context.filter = 'all';
context.currentPage = 1;
context.pageRequestSequence = 1;
context.document = { hidden: false };
context.renderList = () => {};
context.applyNewsPage = () => {};
context.scheduleAdjacentPagePrefetch = () => {};
context.fetchNewsPage = (page, activeFilter, signal) => new Promise((resolve, reject) => {
  signal.addEventListener('abort', () => reject(
    Object.assign(new Error('timed out'), { name: 'AbortError' })
  ));
});
const timers = [];
let timerId = 0;
context.setTimeout = (callback, timeout) => {
  const timer = { id: ++timerId, timeout, callback: () => { now += timeout; return callback(); } };
  timers.push(timer);
  return timer.id;
};
context.clearTimeout = id => {
  const index = timers.findIndex(timer => timer.id === id);
  if (index >= 0) timers.splice(index, 1);
};
const initial = {
  items: [], total: 0,
  diagnostics: { refresh_job: { status: 'running', trigger: 'startup' } },
};
context.startStartupEmptyRevalidation(initial, {
  page: 1, activeFilter: 'all', requestSeq: 1,
  maxAttempts: 24, intervalMs: 5, requestTimeoutMs: 12, maxDurationMs: 10,
});
const interval = timers.shift();
assert.equal(interval.timeout, 5);
const poll = interval.callback();
await Promise.resolve();
const requestTimeout = timers.shift();
assert.equal(requestTimeout.timeout, 5);
requestTimeout.callback();
await poll;
assert.equal(now, 10);
assert.equal(context.coldStartInitializationActive, false);
assert.equal(context.coldStartInitializationTimedOut, true);
assert.equal(timers.length, 0);
""",
    )


# ─── Refresh button progress label ─────────────────────────────────────
#
# The pill is a fixed-width container and the fetched-article count grows
# unpredictably as a refresh streams in, so a full sentence ("已获取 N 篇")
# could wrap to two lines on narrow (mobile) widths, stretching the pill
# vertically. Switched to a short "+N" with the full sentence moved to the
# title attribute (hover/long-press) instead.

def _refresh_label_fns():
    return source_between("function setRefreshRunning(", "async function applyStreamedRefreshBatch(")


def test_set_refresh_progress_label_shows_short_plus_count_with_full_text_in_title():
    fns = _refresh_label_fns()
    run_node(
        fns,
        """
context.refreshInProgress = true;
const span = { textContent: '' };
const btn = { title: '', querySelector: () => span };
context.document = { getElementById: () => btn };
context.setRefreshProgressLabel(12);
assert.equal(span.textContent, '+12');
assert.equal(btn.title, '已获取 12 篇');
""",
    )


def test_set_refresh_progress_label_falls_back_to_running_text_when_count_is_zero():
    fns = _refresh_label_fns()
    run_node(
        fns,
        """
context.refreshInProgress = true;
const span = { textContent: '' };
const btn = { title: 'stale', querySelector: () => span };
context.document = { getElementById: () => btn };
context.setRefreshProgressLabel(0);
assert.equal(span.textContent, '更新中');
assert.equal(btn.title, '');
""",
    )


def test_set_refresh_progress_label_does_nothing_when_refresh_is_not_running():
    fns = _refresh_label_fns()
    run_node(
        fns,
        """
context.refreshInProgress = false;
const span = { textContent: 'unchanged' };
const btn = { title: 'unchanged', querySelector: () => span };
context.document = { getElementById: () => btn };
context.setRefreshProgressLabel(5);
assert.equal(span.textContent, 'unchanged');
assert.equal(btn.title, 'unchanged');
""",
    )


def test_set_refresh_running_clears_any_stale_progress_title():
    fns = _refresh_label_fns()
    run_node(
        fns,
        """
const span = { textContent: '' };
const btn = { title: '已获取 12 篇', querySelector: () => span, classList: { toggle: () => {} } };
context.document = { getElementById: () => btn };
context.setRefreshRunning(false);
assert.equal(btn.title, '');
assert.equal(span.textContent, '刷新');
""",
    )


# ─── Admin purge date picker ────────────────────────────────────────────

def _purge_picker_fns():
    return source_between("function normalizedPurgeDate(", "async function previewPurge(")


def test_open_purge_date_picker_prefills_from_text_field_and_caps_at_server_today():
    fns = _purge_picker_fns()
    run_node(
        fns,
        """
const elements = {
  purgeBeforeDate: { value: '2026/07/10' },
  purgeDatePicker: { value: '', max: '', style: {}, showPicker: () => { context.pickerShown = true; } },
};
context.document = { getElementById: id => elements[id] };
// The server's own "today" (see loadServerStats()'s server_date), not the browser's
// UTC/local date — they can disagree around midnight in either timezone.
context.serverTodayDate = '2026-07-16';
context.pickerShown = false;
context.openPurgeDatePicker();
assert.equal(elements.purgeDatePicker.value, '2026-07-10');
assert.equal(elements.purgeDatePicker.max, '2026-07-16');
assert.equal(context.pickerShown, true);
""",
    )


def test_open_purge_date_picker_leaves_max_unset_when_server_today_is_unknown():
    fns = _purge_picker_fns()
    run_node(
        fns,
        """
const removedAttrs = [];
const elements = {
  purgeBeforeDate: { value: '' },
  purgeDatePicker: {
    value: '', max: 'stale-value', style: {},
    showPicker: () => { context.pickerShown = true; },
    removeAttribute: name => removedAttrs.push(name),
  },
};
context.document = { getElementById: id => elements[id] };
context.serverTodayDate = ''; // hasn't arrived yet (stats poll hasn't succeeded)
context.pickerShown = false;
context.openPurgeDatePicker();
assert.deepEqual(removedAttrs, ['max']);
""",
    )


def test_open_purge_date_picker_falls_back_to_focus_click_without_show_picker():
    fns = _purge_picker_fns()
    run_node(
        fns,
        """
const calls = [];
const elements = {
  purgeBeforeDate: { value: '' },
  purgeDatePicker: { value: '', max: '', style: {}, focus: () => calls.push('focus'), click: () => calls.push('click') },
};
context.document = { getElementById: id => elements[id] };
context.serverTodayDate = '2026-07-16';
context.openPurgeDatePicker();
assert.deepEqual(calls, ['focus', 'click']);
assert.equal(elements.purgeDatePicker.style.pointerEvents, 'auto');
""",
    )


def test_apply_purge_date_picker_fills_text_field_and_resets_confirm_state():
    fns = _purge_picker_fns()
    run_node(
        fns,
        """
const elements = {
  purgeBeforeDate: { value: '' },
  purgeDatePicker: { value: '2026-07-15', style: {} },
  purgeConfirmBtn: { disabled: false }, // simulate a stale "ready to delete" state
  purgeStatus: { textContent: '将删除 12 篇文章...' },
};
context.document = { getElementById: id => elements[id] };
context.applyPurgeDatePicker();
assert.equal(elements.purgeBeforeDate.value, '2026/07/15');
assert.equal(elements.purgeConfirmBtn.disabled, true);
assert.equal(elements.purgeStatus.textContent, '');
""",
    )


def test_apply_purge_date_picker_does_nothing_when_picker_has_no_value():
    fns = _purge_picker_fns()
    run_node(
        fns,
        """
const elements = {
  purgeBeforeDate: { value: '2026/01/01' },
  purgeDatePicker: { value: '', style: {} },
  purgeConfirmBtn: { disabled: false },
  purgeStatus: { textContent: 'old status' },
};
context.document = { getElementById: id => elements[id] };
context.applyPurgeDatePicker();
assert.equal(elements.purgeBeforeDate.value, '2026/01/01');
assert.equal(elements.purgeConfirmBtn.disabled, false);
assert.equal(elements.purgeStatus.textContent, 'old status');
""",
    )


def test_refresh_counts_articles_the_fallback_check_discovers():
    """Cold start after a long gap: the button said +10 while the bubble said 65.

    When the page-1 completion branch can't run (a background page request was
    still in flight when the user clicked — routine right after a cold start),
    the flow falls back to loadSince() to queue what the job found. That call is
    what fills the "有 N 篇新文章" bubble, so its discoveries have to reach the
    same counter the button label and completion toast read from, or the two
    numbers disagree on screen.
    """
    trigger = refresh_trigger_source()
    setup = trigger_context_setup(
        "return { job_id: 'job-1', status: 'completed', new_count: 10, "
        "new_ids: [1,2,3,4,5,6,7,8,9,10] };"
    )
    run_node(
        trigger,
        setup
        + """
// A page request was still in flight at click time, so the completion branch
// is skipped and the fallback loadSince() is the one that finds the backlog.
context.pageRequestPendingSequence = 4;
context.window = { scrollY: 0 };
context.labels = [];
context.setRefreshProgressLabel = count => context.labels.push(count);
context.loadNewsPage = async () => { throw new Error('page-1 branch must not run here'); };
context.loadSince = async (timestamp, options) => {
  if (options && options.manual) return 0;          // immediate check: DB not filled yet
  const backlog = Array.from({ length: 65 }, (_, i) => ({ id: 100 + i }));
  if (options && typeof options.onDiscovered === 'function') options.onDiscovered(backlog);
  return backlog.length;                            // these are what the bubble counts
};

await context.triggerRefresh();

// 65 backlog articles + the 10 this job ingested, deduped into one set.
assert.equal(context.labels.at(-1), 75);
assert.ok(context.toasts.includes('✅ 更新完成，新增 75 篇文章'));
""",
    )


def test_a_background_check_during_a_manual_refresh_feeds_the_same_counter():
    """The 60s poll and the resume check queue into the bubble too.

    They know nothing about the running refresh, so without the sink their
    discoveries would show up in "有 N 篇新文章" while the button label ignored
    them — the same mismatch the fallback check caused.
    """
    incremental = source_between("async function loadSince(", "function rebuildSourceFilterGroups")
    run_node(
        incremental,
        """
context.refreshInProgress = true;
context.INCREMENTAL_FETCH_LIMIT = 200;
context.seenArticleIds = new Set();
context.latestKnownTimestamp = 100;
context.pendingNewArticleCount = 0;
context.pendingNewItems = [];
context.contentEpoch = 0;
context.bumpContentEpoch = () => {};
context.fetch = async () => ({
  json: async () => ({ items: [
    { id: 2, timestamp: 101, source: 's' },
    { id: 3, timestamp: 102, source: 's' },
  ] }),
});
context.isSwFallbackResponse = () => false;
context.setTimeout = () => 1;
context.clearTimeout = () => {};
context.refreshTodayArticleCount = () => {};
context.discovered = [];
context.activeRefreshDiscoverySink = items => context.discovered.push(...items.map(i => i.id));

// No onDiscovered passed — this is the plain periodic poll.
const added = await context.loadSince(100);

assert.equal(added, 2);
assert.deepEqual(Array.from(context.pendingNewItems, item => item.id), [2, 3]);
assert.deepEqual(context.discovered, [2, 3]);   // the bubble's items reached the counter
""",
    )
