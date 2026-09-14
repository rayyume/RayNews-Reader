from pathlib import Path

from test_frontend_refresh_behavior import run_node


ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")


def source_between(start, end):
    start_index = HTML.index(start)
    return HTML[start_index:HTML.index(end, start_index)]


ARTICLE_HISTORY = source_between(
    "function usesMobileArticleNavigation()",
    "function rememberArticleReturnState(id)",
)
ARTICLE_CLOSE = source_between(
    "let _closingOverlay = false;",
    "let articleBackTouchStart = null;",
)
ARTICLE_RETURN = source_between(
    "function rememberArticleReturnState(id)",
    "function openArticle(id)",
)


def article_swipe_source():
    detector_start = HTML.index("function usesIpad")
    detector_end = HTML.index("function onReturnToForeground()", detector_start)
    swipe_start = HTML.index("// ═══ Mobile: swipe-to-go-back on article overlay")
    swipe_end = HTML.index("</script>", swipe_start)
    return HTML[detector_start:detector_end] + HTML[swipe_start:swipe_end]


def trackpad_runtime_source(*, standalone):
    standalone_js = "true" if standalone else "false"
    return f"""
globalThis.handlers = {{}};
globalThis.overlay = {{
  classList: {{ contains: name => name === 'open' }},
  style: {{}},
  addEventListener: (name, handler) => {{ handlers[name] = handler; }},
  removeEventListener: (name, handler) => {{
    if (handlers[name] === handler) delete handlers[name];
  }},
}};
globalThis.lightbox = {{ classList: {{ contains: () => false }} }};
globalThis.navigator = {{ platform: 'MacIntel', maxTouchPoints: 5 }};
globalThis.window = {{ matchMedia: () => ({{ matches: {standalone_js} }}) }};
globalThis.document = {{
  getElementById: id => id === 'overlay' ? overlay : lightbox,
}};
globalThis.usesMobileArticleNavigation = () => true;
globalThis.clearTimeout = () => {{}};
globalThis.setTimeout = () => 1;
globalThis.closeCalls = 0;
globalThis.closeArticle = () => {{ closeCalls += 1; }};
globalThis.closeArticleFromButton = () => {{ closeCalls += 1; }};
""" + article_swipe_source()


def test_touch_device_opening_an_article_adds_a_real_history_entry():
    """Replacing the list entry makes Android/iOS system Back leave RayNews."""
    run_node(
        ARTICLE_HISTORY,
        """
const pushed = [];
const replaced = [];
context.navigator = { maxTouchPoints: 5 };
context.window = {
  location: { pathname: '/', search: '?page=2' },
  matchMedia: query => ({
    matches: query === '(hover: none) and (pointer: coarse)',
  }),
};
context.location = { hash: '', pathname: '/', search: '?page=2' };
context.history = {
  state: { raynewsHome: true, page: 2 },
  pushState: (state, title, url) => pushed.push({ state, url }),
  replaceState: (state, title, url) => replaced.push({ state, url }),
};

context.syncArticleHistory(42, '2026-09-09');

assert.deepEqual(pushed, [{
  state: { raynewsArticle: true, articleId: 42 },
  url: '#/article/26-09-09-42',
}]);
assert.deepEqual(replaced, []);
""",
    )


def test_article_loading_state_gets_history_before_its_date_is_known():
    """Favorites and source-history articles must be Back-safe while loading."""
    run_node(
        ARTICLE_HISTORY,
        """
const pushed = [];
context.navigator = { maxTouchPoints: 5 };
context.window = {
  location: { pathname: '/', search: '?page=2' },
  matchMedia: () => ({ matches: true }),
};
context.location = { hash: '', pathname: '/', search: '?page=2' };
context.history = {
  state: { raynewsHome: true, page: 2 },
  pushState: (state, title, url) => pushed.push({ state, url }),
  replaceState: () => {},
};

context.syncArticleHistory(42, '');

assert.deepEqual(pushed, [{
  state: { raynewsArticle: true, articleId: 42 },
  url: undefined,
}]);
""",
    )


def test_mobile_back_traverses_article_history_before_closing_overlay():
    """System, browser, and UI Back must share the same history traversal."""
    run_node(
        ARTICLE_HISTORY + ARTICLE_CLOSE,
        """
const openClasses = new Set(['open']);
const overlay = {
  classList: {
    contains: name => openClasses.has(name),
    remove: name => openClasses.delete(name),
  },
  style: {},
};
const lightbox = { classList: { contains: () => false } };
context.document = {
  getElementById: id => id === 'overlay' ? overlay : lightbox,
};
context.navigator = { maxTouchPoints: 5 };
context.window = {
  location: { pathname: '/', search: '?page=2' },
  matchMedia: query => ({
    matches: query === '(hover: none) and (pointer: coarse)',
  }),
};
context.location = { hash: '#/article/26-09-09-42' };
let backCalls = 0;
context.history = {
  state: { raynewsArticle: true },
  back: () => { backCalls += 1; },
  replaceState: () => { throw new Error('article Back must not replace history'); },
};
context.activeArticleRequestId = 0;
context.pendingNewArticleCount = 0;
context.unlockArticleBackground = () => {};
context.showNewArticlesPrompt = () => {};
context.restoreArticleReturnState = callback => callback();

context.closeArticle();
context.closeArticle();

assert.equal(backCalls, 1);
assert.equal(openClasses.has('open'), true);

context.history.state = { raynewsHome: true, page: 2 };
context.location.hash = '';
context.closeArticle(true);
assert.equal(openClasses.has('open'), false);
""",
    )


def test_ipad_trackpad_claims_the_first_horizontal_right_wheel_event():
    """Waiting for the distance threshold lets WebKit start native Back."""
    run_node(
        trackpad_runtime_source(standalone=True),
        """
let prevented = false;
context.handlers.wheel({
  deltaX: -30,
  deltaY: 0,
  cancelable: true,
  preventDefault: () => { prevented = true; },
});

assert.equal(prevented, true);
assert.equal(context.closeCalls, 0);
""",
    )


def test_ipad_browser_trackpad_uses_the_same_article_gesture_as_the_pwa():
    """The Safari tab must not fall through merely because it is not standalone."""
    run_node(
        trackpad_runtime_source(standalone=False),
        """
let prevented = false;
context.handlers.wheel({
  deltaX: -30,
  deltaY: 0,
  cancelable: true,
  preventDefault: () => { prevented = true; },
});

assert.equal(prevented, true);
""",
    )


def test_non_navigation_wheel_input_remains_native():
    """Vertical scroll, leftward motion, and non-iPad devices stay untouched."""
    run_node(
        trackpad_runtime_source(standalone=True),
        """
let preventCalls = 0;
const event = (deltaX, deltaY) => ({
  deltaX,
  deltaY,
  cancelable: true,
  preventDefault: () => { preventCalls += 1; },
});

context.handlers.wheel(event(-10, 40));
context.handlers.wheel(event(30, 0));
context.navigator.maxTouchPoints = 0;
context.handlers.wheel(event(-30, 0));

assert.equal(preventCalls, 0);
""",
    )


def test_non_cancelable_ipad_wheel_sequence_is_left_to_native_history():
    """The app must not close too when WebKit already owns the gesture."""
    run_node(
        trackpad_runtime_source(standalone=True),
        """
context.handlers.wheel({
  deltaX: -100,
  deltaY: 0,
  cancelable: false,
  preventDefault: () => { throw new Error('non-cancelable event'); },
});

assert.equal(context.closeCalls, 0);
""",
    )


def test_claimed_ipad_wheel_sequence_accumulates_non_cancelable_events():
    """WebKit may expose only the first event in a wheel sequence as cancelable."""
    run_node(
        trackpad_runtime_source(standalone=True),
        """
let preventCalls = 0;
context.handlers.wheel({
  deltaX: -30,
  deltaY: 0,
  cancelable: true,
  preventDefault: () => { preventCalls += 1; },
});
context.handlers.wheel({
  deltaX: -60,
  deltaY: 0,
  cancelable: false,
  preventDefault: () => { throw new Error('later event is not cancelable'); },
});

assert.equal(preventCalls, 1);
assert.equal(context.closeCalls, 1);
""",
    )


def test_forward_reopen_does_not_replace_or_duplicate_provisional_entry():
    """Restoring an existing article state must preserve that Forward entry."""
    run_node(
        ARTICLE_HISTORY + ARTICLE_RETURN,
        """
context.window = { scrollY: 240, matchMedia: () => ({ matches: false }) };
context.navigator = { maxTouchPoints: 0 };
context.location = { hash: '' };
let pushCalls = 0;
let replaceCalls = 0;
context.history = {
  state: { raynewsArticle: true, articleId: 42 },
  pushState: () => { pushCalls += 1; },
  replaceState: () => { replaceCalls += 1; },
};
context.document = {
  getElementById: () => ({
    classList: { contains: () => false },
    scrollTop: 0,
  }),
  querySelector: () => null,
};
context.rememberCurrentListHistory = () => {
  context.history.replaceState({ raynewsHome: true }, '', '/');
};

context.rememberArticleReturnState(42);
context.syncArticleHistory(42, '');

assert.equal(pushCalls, 0);
assert.equal(replaceCalls, 0);
""",
    )


def test_forward_reopens_an_article_that_was_still_loading_on_back():
    """A provisional article entry must remain a usable Forward destination."""
    popstate = source_between(
        "window.addEventListener('popstate'",
        "window.addEventListener('keydown'",
    )
    setup = """
globalThis.handlers = {};
globalThis.window = {
  addEventListener: (name, handler) => { handlers[name] = handler; },
};
globalThis.location = { hash: '' };
globalThis.history = {
  state: { raynewsArticle: true, articleId: 42 },
};
globalThis.overlay = { classList: { contains: () => false } };
globalThis.document = { getElementById: () => overlay };
globalThis.articleReturnInProgress = false;
globalThis.openedArticles = [];
globalThis.openArticle = id => { openedArticles.push(id); };
globalThis.closeArticle = () => { throw new Error('nothing is open to close'); };
globalThis.listRestoreCalls = 0;
globalThis.restoreListStateFromUrl = () => { listRestoreCalls += 1; };
"""
    run_node(
        setup + popstate,
        """
context.handlers.popstate();

assert.deepEqual(context.openedArticles, [42]);
assert.equal(context.listRestoreCalls, 0);
""",
    )


def test_switching_articles_replaces_the_single_article_history_entry():
    """Moving between articles must not require multiple Back traversals."""
    run_node(
        ARTICLE_HISTORY,
        """
const pushed = [];
const replaced = [];
context.navigator = { maxTouchPoints: 0 };
context.window = { location: { pathname: '/', search: '?page=2' }, matchMedia: () => ({ matches: false }) };
context.location = { hash: '#/article/26-09-09-41' };
context.history = {
  state: { raynewsArticle: true, articleId: 41 },
  pushState: (state, title, url) => pushed.push({ state, url }),
  replaceState: (state, title, url) => replaced.push({ state, url }),
};

context.syncArticleHistory(42, '2026-09-09');

assert.deepEqual(pushed, []);
assert.deepEqual(replaced, [{
  state: { raynewsArticle: true, articleId: 42 },
  url: '#/article/26-09-09-42',
}]);
""",
    )


def test_touch_swipe_ignores_descendant_transitions_before_overlay_transform():
    """A child animation must not consume the article swipe completion."""
    run_node(
        trackpad_runtime_source(standalone=True),
        """
context.handlers.touchstart({
  target: { closest: () => null },
  touches: [{ clientX: 10, clientY: 20 }],
  preventDefault: () => {},
});
context.handlers.touchmove({
  touches: [{ clientX: 100, clientY: 20 }],
  preventDefault: () => {},
});
context.handlers.touchend();

context.handlers.transitionend({ type: 'transitionend', target: {}, propertyName: 'opacity' });
assert.equal(context.closeCalls, 0);

context.handlers.transitionend({ type: 'transitionend', target: context.overlay, propertyName: 'transform' });
assert.equal(context.closeCalls, 1);
""",
    )


def test_touch_swipe_transition_cancel_still_finishes_navigation():
    """Canceled CSS transitions must not leave an invisible open overlay."""
    run_node(
        trackpad_runtime_source(standalone=True),
        """
context.handlers.touchstart({
  target: { closest: () => null },
  touches: [{ clientX: 10, clientY: 20 }],
  preventDefault: () => {},
});
context.handlers.touchmove({
  touches: [{ clientX: 100, clientY: 20 }],
  preventDefault: () => {},
});
context.handlers.touchend();

assert.equal(typeof context.handlers.transitioncancel, 'function');
context.handlers.transitioncancel({ type: 'transitioncancel', target: context.overlay });
assert.equal(context.closeCalls, 1);
""",
    )


def test_touch_swipe_timeout_finishes_navigation_when_css_emits_no_event():
    """A missing transition event must not leave an invisible article open."""
    run_node(
        trackpad_runtime_source(standalone=True),
        """
let swipeFallback = null;
context.setTimeout = callback => { swipeFallback = callback; return 1; };
context.handlers.touchstart({
  target: { closest: () => null },
  touches: [{ clientX: 10, clientY: 20 }],
  preventDefault: () => {},
});
context.handlers.touchmove({
  touches: [{ clientX: 100, clientY: 20 }],
  preventDefault: () => {},
});
context.handlers.touchend();

assert.equal(typeof swipeFallback, 'function');
swipeFallback();
assert.equal(context.closeCalls, 1);
""",
    )
