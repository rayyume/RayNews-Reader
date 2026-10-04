"""Recovery after an iOS PWA resume.

Opening the app after a long time in the background used to dead-end: the first
requests are answered from the Service Worker cache (or hang on a socket the OS
froze), the list showed "内容可能不是最新" / "连接超时" with a 重试 button that
fired a single request into the same dead connection, and only the manual
refresh button — pressed late enough for the network to be back — recovered.

The fixes under test: the stale body the Service Worker already handed us is
rendered instead of an error card, retries are cache-busted and fail fast, and a
failed list load re-tries itself on a backoff (plus immediately on 'online' and
on the next foreground resume) rather than waiting for a tap.
"""

from pathlib import Path

from test_frontend_refresh_behavior import run_node, source_between

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
SW = (ROOT / "frontend" / "sw.js").read_text(encoding="utf-8")

PARAMS = source_between("function buildNewsPageParams(", "function listUrlForState")
FETCH_PAGE = source_between("function isSwFallbackResponse(", "function warmPageCoverImages")
LOAD_PAGE = source_between("async function loadNewsPage(", "function applyPageCalibrationWhenActive")
RECOVERY = source_between("let listLoadDegraded = false;", "function renderColdStartError(")
# `let` bindings live in the script scope, not on the VM's global object, so
# expose the one the tests assert on.
RECOVERY += "\nglobalThis.readDegraded = () => listLoadDegraded;\n"

COLD_START_CONTEXT = """
context.PAGE_SIZE = 30;
context.sourceFilterGroups = {};
context.contentEpoch = 0;
context.pageRequestSequence = 0;
context.pageRequestPendingSequence = 0;
context.pageRequestController = null;
context.news = [];                      // nothing on screen: the resume case
context.readCachedNewsPage = async () => null;   // storage evicted / cold start
context.rememberBufferedPage = () => {};
context.applied = [];
context.applyNewsPage = (data, page, filter, opts) => {
  context.applied.push(data);
  context.news = data.items || [];
};
context.renderColdStartSkeleton = () => {};
context.writeCachedNewsPage = async () => {};
context.scheduleAdjacentPagePrefetch = () => {};
context.setPageLoading = () => {};
context.errors = [];
context.renderColdStartError = message => context.errors.push(message);
context.toasts = [];
context.showToast = message => context.toasts.push(message);
context.currentTotal = 0;
context.document = { getElementById: () => ({ classList: { contains: () => false } }) };
context.articleReturnInProgress = false;
context.pendingLatestPage = null;
context.buildNewsPageParams = () => new URLSearchParams();
context.setTimeout = () => 1;
context.clearTimeout = () => {};
context.recoveryCalls = [];
context.scheduleNetworkRecoveryRetry = opts => context.recoveryCalls.push(opts);
context.cancelNetworkRecoveryRetry = () => { context.cancelled = (context.cancelled || 0) + 1; };
"""


def test_sw_cached_content_is_shown_instead_of_a_dead_end_error_card():
    run_node(
        PARAMS + FETCH_PAGE + LOAD_PAGE,
        COLD_START_CONTEXT + """
context.fetch = async () => ({
  ok: true,
  headers: { get: name => (name === 'X-SW-Fallback' ? '1' : null) },
  json: async () => ({ items: [{ id: 7 }], total: 1, page: 1 }),
});

const loaded = await context.loadNewsPage(1, { activeFilter: 'all', useCache: true, forceNetwork: true });

assert.equal(loaded, true);
assert.deepEqual(context.applied, [{ items: [{ id: 7 }], total: 1, page: 1 }]);
assert.deepEqual(context.errors, []);                       // no 📡 dead end
assert.deepEqual(context.toasts, ['内容可能不是最新，下拉或点击刷新重试']);
assert.equal(context.recoveryCalls.length, 1);              // and it keeps trying
""",
    )


def test_an_unreachable_network_still_shows_the_error_card_but_arms_the_retry_chain():
    run_node(
        PARAMS + FETCH_PAGE + LOAD_PAGE,
        COLD_START_CONTEXT + """
context.fetch = async () => { throw new TypeError('Failed to fetch'); };

const loaded = await context.loadNewsPage(1, { activeFilter: 'all', useCache: true, forceNetwork: true });

assert.equal(loaded, false);
assert.deepEqual(context.errors, ['加载失败，请检查网络']);
assert.equal(context.recoveryCalls.length, 1);
assert.deepEqual(context.recoveryCalls[0], { page: 1, activeFilter: 'all' });
""",
    )


def test_a_retry_is_cache_busted_and_the_earlier_attempt_gives_up_early():
    run_node(
        PARAMS + FETCH_PAGE + LOAD_PAGE,
        COLD_START_CONTEXT + """
context.delay = async () => {};
context.timeouts = [];
context.setTimeout = (fn, ms) => { context.timeouts.push(ms); return context.timeouts.length; };
context.buildNewsPageParams = () => new URLSearchParams({ page: '1' });
context.urls = [];
let calls = 0;
context.fetch = async url => {
  context.urls.push(url);
  calls++;
  if (calls === 1) throw Object.assign(new Error('hung'), { name: 'AbortError' });
  return {
    ok: true,
    headers: { get: () => null },
    json: async () => ({ items: [{ id: 9 }], total: 1, page: 1 }),
  };
};

const loaded = await context.loadNewsPage(1, {
  activeFilter: 'all', useCache: false, forceNetwork: true, networkRetries: 1,
});

assert.equal(loaded, true);
assert.equal(context.urls.length, 2);
// The first attempt must not burn the full budget on a frozen socket, and the
// retry must not be answerable by it.
assert.equal(context.timeouts[0], 6000);
assert.equal(context.timeouts[1], 12000);
assert.equal(context.urls[0].includes('&t='), false);
assert.equal(/[?&]t=\\d+/.test(context.urls[1]), true);
// A successful load stops any recovery chain armed by an earlier failure.
assert.equal(context.cancelled, 1);
""",
    )


def test_automatic_recovery_retries_do_not_re_toast_the_user():
    # The chain can run eight times; the user was told once already.
    run_node(
        PARAMS + FETCH_PAGE + LOAD_PAGE,
        COLD_START_CONTEXT + """
context.fetch = async () => { throw new TypeError('Failed to fetch'); };

await context.loadNewsPage(1, {
  activeFilter: 'all', useCache: false, forceNetwork: true, suppressFailureToast: true,
});

assert.deepEqual(context.toasts, []);
assert.deepEqual(context.errors, ['加载失败，请检查网络']);  // the card still updates
assert.equal(context.recoveryCalls.length, 1);              // and the chain continues
""",
    )


def test_recovery_retries_on_a_growing_backoff_and_stops_once_it_succeeds():
    run_node(
        RECOVERY,
        """
context.currentPage = 1;
context.filter = 'all';
context.document = { hidden: false };
context.timers = [];
context.setTimeout = (fn, ms) => { context.timers.push({ fn, ms }); return context.timers.length; };
context.cleared = [];
context.clearTimeout = id => context.cleared.push(id);
context.loads = [];
context.loadNewsPage = (page, options) => { context.loads.push({ page, options }); return true; };

context.scheduleNetworkRecoveryRetry({ page: 1, activeFilter: 'all' });
assert.equal(context.timers[0].ms, 1500);
assert.equal(context.readDegraded(), true);
// A second request while one is already armed must not stack timers.
context.scheduleNetworkRecoveryRetry({ page: 1, activeFilter: 'all' });
assert.equal(context.timers.length, 1);

context.timers[0].fn();                       // the retry fires
assert.equal(context.loads.length, 1);
assert.equal(context.loads[0].options.forceNetwork, true);
assert.equal(context.loads[0].options.useCache, false);

context.scheduleNetworkRecoveryRetry({ page: 1, activeFilter: 'all' });   // it failed again
assert.equal(context.timers[1].ms, 4000);     // backoff grows
context.timers[1].fn();
context.scheduleNetworkRecoveryRetry({ page: 1, activeFilter: 'all' });
assert.equal(context.timers[2].ms, 8000);

context.cancelNetworkRecoveryRetry();
assert.equal(context.readDegraded(), false);
context.scheduleNetworkRecoveryRetry({ page: 1, activeFilter: 'all' });
assert.equal(context.timers[3].ms, 1500);     // counter reset with the chain
""",
    )


def test_recovery_does_not_fire_while_the_app_is_backgrounded():
    run_node(
        RECOVERY,
        """
context.currentPage = 1;
context.filter = 'all';
context.document = { hidden: true };
context.timers = [];
context.setTimeout = (fn, ms) => { context.timers.push({ fn, ms }); return context.timers.length; };
context.clearTimeout = () => {};
context.loads = [];
context.loadNewsPage = () => { context.loads.push(1); return true; };

context.scheduleNetworkRecoveryRetry({ page: 1, activeFilter: 'all' });
context.timers[0].fn();
assert.equal(context.loads.length, 0);   // frozen tab: onReturnToForeground re-arms it
assert.equal(context.readDegraded(), true);
""",
    )


def test_online_and_foreground_resume_retry_immediately_when_degraded():
    run_node(
        RECOVERY,
        """
context.currentPage = 2;
context.filter = 'all';
context.document = { hidden: false };
context.timers = [];
context.setTimeout = (fn, ms) => { context.timers.push({ fn, ms }); return context.timers.length; };
context.clearTimeout = () => {};
context.loadNewsPage = () => true;

context.scheduleNetworkRecoveryRetry({ page: 2, activeFilter: 'all' });
assert.equal(context.timers[0].ms, 1500);
context.scheduleNetworkRecoveryRetry({ immediate: true });
assert.equal(context.timers[1].ms, 0);   // no waiting once connectivity is back
""",
    )


def test_resume_and_online_handlers_are_wired_to_the_recovery_chain():
    foreground = source_between(
        "function onReturnToForeground()",
        "document.addEventListener('visibilitychange', () =>",
    )
    assert "if (listLoadDegraded) scheduleNetworkRecoveryRetry({ immediate: true });" in foreground
    online = HTML[HTML.index("window.addEventListener('online', retryBrokenVisibleImages);"):][:400]
    assert "listLoadDegraded && !document.hidden" in online


def test_cold_start_retry_button_retries_the_list_several_times():
    assert 'onclick="retryColdStartLoad()"' in HTML
    retry = source_between("function retryColdStartLoad()", "function renderColdStartError(")
    assert "networkRetries: 2" in retry
    assert "forceNetwork: true" in retry
    # Source metadata must not gate the list: it is kicked off, never awaited.
    assert "await loadSourceCategories" not in retry
    assert "loadSourceCategories();" in retry


def test_service_worker_bounds_its_own_network_wait():
    assert "function fetchWithTimeout(request" in SW
    # Every network-first handler that can strand the app on resume.
    assert SW.count("fetchWithTimeout(event.request)") == 4
    run_node(
        SW[SW.index("const NETWORK_TIMEOUT_MS"):SW.index("self.addEventListener('install'")],
        """
let timerFn = null;
context.setTimeout = (fn, ms) => { timerFn = fn; context.timeoutMs = ms; return 1; };
context.clearTimeout = () => {};
context.fetch = () => new Promise(() => {});   // a socket the OS froze
const pending = context.fetchWithTimeout({ url: '/api/news' });
timerFn();
await assert.rejects(pending, error => error.name === 'SwNetworkTimeoutError');
assert.equal(context.timeoutMs, 8000);
""",
    )
