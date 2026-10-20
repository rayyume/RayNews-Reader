"""Article AI display, search recovery and translation-cache regressions."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")


def _auto_display_summary_block():
    start = HTML.index("async function autoDisplaySummary(")
    end = HTML.index("function showAIActions(", start)
    return ("const serverTitleResults=new Map(); function storeAITitleResult(id,result) {serverTitleResults.set(id,result);} function displayTitle(item) { return item.title; }\n"
            "function aiArticleContext(id) { return {id}; }\n"
            "function aiContextCurrent() { return true; }\n"
            "function serverResultStatus() {} function markArticleVisible() {} function findOpenArticleData() { return null; } function renderList() {}\n"
            "async function readServerAIResult(ctx) { return (await fetch('/ai/result/' + ctx.id)).json(); }\n"
            + HTML[start:end])


def _translation_update_block():
    start = HTML.index("function invalidateArticleBody(")
    end = HTML.index("function articleItemHtml(", start)
    return "var aiSessionGeneration = 0; function aiArticleContext() { return {}; } function aiContextCurrent() { return true; }\n" + HTML[start:end]


def _article_detail_block():
    start = HTML.index("function fetchArticleDetail(")
    end = HTML.index("function renderArticleBody(", start)
    return HTML[start:end]


def _article_body_cache_helpers_block():
    limit_start = HTML.index("const ARTICLE_BODY_CACHE_MAX_ITEMS =")
    limit_end = HTML.index("\n", limit_start) + 1
    start = HTML.index("function cachedArticleBody(id)")
    end = HTML.index("let searchDebounceTimer", start)
    return (
        HTML[limit_start:limit_end]
        + "let articleBodyCacheRecency = new Map(Object.keys(articleBodyCache).map(key => [key, true]));\n"
        + HTML[start:end]
    )


def _display_title_block():
    start = HTML.index("function displayTitle(")
    end = HTML.index("\n}", start) + 2
    return HTML[start:end]


def _share_connection_actions_block():
    start = HTML.index("async function saveAIConfig()")
    end = HTML.index("// ─── Share Tab", start)
    return HTML[start:end]


def _user_settings_sync_block():
    start = HTML.index("async function loadUserSettings()")
    end = HTML.index("// ─── General Settings", start)
    return ("function aiAccessKey(s) { return JSON.stringify(s); }\n"
            "var aiAccessGeneration = 0; function cancelAIResultReads() {}\n" + HTML[start:end])


def _share_tab_block():
    start = HTML.index("function updateShareSubToggleState()")
    end = HTML.index("async function saveShareConfig()", start)
    return HTML[start:end]


def _notification_status_block():
    start = HTML.index("let notifDetailId")
    end = HTML.index("function openNotifications()", start)
    return HTML[start:end]


def _title_list_rendering_block():
    source_start = HTML.index("function renderSourceArticles(")
    source_end = HTML.index("async function saveSourceRow(", source_start)
    favorites_start = HTML.index("function renderFavorites(")
    favorites_end = HTML.index("function openFavArticle(", favorites_start)
    return HTML[source_start:source_end] + "\n" + HTML[favorites_start:favorites_end]


def _search_block():
    start = HTML.index("function openSearch()")
    end = HTML.index("function toggleSidebar()", start)
    return HTML[start:end]


def _run_search_vm(body):
    script = f"""
const assert = require('assert');
const vm = require('vm');

function element(extra = {{}}) {{
  return Object.assign({{
    value: '', textContent: '', innerHTML: '', className: '',
    style: {{}}, dataset: {{}},
    classList: {{ contains: () => true, add() {{}}, remove() {{}} }},
    setAttribute() {{}}, focus() {{}}, blur() {{}},
  }}, extra);
}}

const requestedIds = [];
const elements = {{
  articleSearchInput: element(),
  articleSearchClear: element(),
  searchStatus: element(),
  searchResults: element(),
  searchOverlay: element(),
}};
const timers = [];
const context = {{
  console,
  userAutoSettings:null,
  URLSearchParams,
  AbortController,
  PAGE_SIZE: 20,
  SEARCH_REQUEST_TIMEOUT_MS: 8000,
  SEARCH_MAX_AUTO_RETRIES: 2,
  SEARCH_RETRY_DELAY_MS: 500,
  searchDebounceTimer: null,
  searchRetryTimer: null,
  searchRetryCount: 0,
  searchRequestController: null,
  searchRequestSeq: 0,
  searchPage: 1,
  searchTotal: 0,
  searchItems: [],
  searchQuery: '',
  authToken: 'token',
  document: {{
    getElementById(id) {{ requestedIds.push(id); return elements[id] || null; }},
  }},
  setTimeout(fn, delay) {{
    const timer = {{ fn, delay, cleared: false }};
    timers.push(timer);
    return timer;
  }},
  clearTimeout(timer) {{ if (timer) timer.cleared = true; }},
  searchArticles: () => [],
  articleItemHtml: item => `<article data-id="${{item.id}}">${{item.title}}</article>`,
  focusSearchInput() {{}},
  lockBodyScroll() {{}},
  unlockBodyScroll() {{}},
  showToast() {{}},
  showAuth() {{}},
}};
vm.createContext(context);
vm.runInContext({json.dumps(_search_block())}, context);

async function flush() {{
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}}

(async () => {{
{body}
  assert.equal(requestedIds.some(id => id.startsWith('#')), false, requestedIds.join(','));
}})().catch(error => {{ console.error(error && error.stack ? error.stack : error); process.exitCode = 1; }});
"""
    return subprocess.run(
        ["node", "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=10
    )


def test_search_retries_once_after_transient_failure_then_renders_success():
    result = _run_search_vm(
        """
  elements.articleSearchInput.value = 'database';
  let attempts = 0;
  context.fetch = async () => {
    attempts++;
    if (attempts === 1) throw new TypeError('temporary network failure');
    return { ok: true, json: async () => ({ items: [{ id: 9, title: 'Recovered' }], total: 1 }) };
  };

  await context.fetchServerSearchResults('database', 1);
  assert.equal(attempts, 1);
  const retry = timers.find(timer => !timer.cleared && timer.delay === context.SEARCH_RETRY_DELAY_MS);
  assert.ok(retry, 'first failure did not schedule a retry');
  await retry.fn();

  assert.equal(attempts, 2);
  assert.match(elements.searchResults.innerHTML, /Recovered/);
  assert.equal(context.searchRetryCount, 0);
"""
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_search_changing_query_cancels_and_guards_the_old_retry():
    result = _run_search_vm(
        """
  elements.articleSearchInput.value = 'old';
  let attempts = 0;
  context.fetch = async () => { attempts++; throw new TypeError('temporary'); };

  await context.fetchServerSearchResults('old', 1);
  const retry = timers.find(timer => !timer.cleared && timer.delay === context.SEARCH_RETRY_DELAY_MS);
  assert.ok(retry);
  elements.articleSearchInput.value = 'new';
  context.scheduleSearchRender();
  assert.equal(retry.cleared, true);

  retry.fn(); // A queued callback can still race after clearTimeout; its own guards must stop it.
  await flush();
  assert.equal(attempts, 1);
"""
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_stale_retry_callback_cannot_detach_replacement_timer_from_close():
    result = _run_search_vm(
        """
  let attempts = 0;
  context.fetch = async () => { attempts++; throw new TypeError('temporary'); };

  elements.articleSearchInput.value = 'old';
  await context.fetchServerSearchResults('old', 1);
  const oldRetry = timers.find(timer => !timer.cleared && timer.delay === context.SEARCH_RETRY_DELAY_MS);
  assert.ok(oldRetry);

  elements.articleSearchInput.value = 'new';
  context.scheduleSearchRender();
  await context.fetchServerSearchResults('new', 1);
  const newRetry = timers.find(timer => timer !== oldRetry && !timer.cleared && timer.delay === context.SEARCH_RETRY_DELAY_MS);
  assert.ok(newRetry);
  assert.strictEqual(context.searchRetryTimer, newRetry);

  oldRetry.fn(); // Simulate a cleared callback that was already queued.
  await flush();
  assert.strictEqual(context.searchRetryTimer, newRetry);

  context.closeSearch();
  assert.equal(newRetry.cleared, true);
  newRetry.fn(); // Even a queued replacement callback must remain network-inert after close.
  await flush();
  assert.equal(attempts, 2);
"""
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_search_retry_exhaustion_keeps_local_results_and_offers_manual_retry():
    result = _run_search_vm(
        """
  elements.articleSearchInput.value = 'local';
  context.searchArticles = () => [{ id: 7, title: 'Local match' }];
  let attempts = 0;
  context.fetch = async () => { attempts++; throw new TypeError('still unavailable'); };

  await context.fetchServerSearchResults('local', 1);
  while (true) {
    const retry = timers.find(timer => !timer.cleared && !timer.ran && timer.delay === context.SEARCH_RETRY_DELAY_MS);
    if (!retry) break;
    retry.ran = true;
    await retry.fn();
  }

  assert.equal(attempts, 1 + context.SEARCH_MAX_AUTO_RETRIES);
  assert.match(elements.searchResults.innerHTML, /Local match/);
  assert.ok(elements.searchResults.innerHTML.includes('onclick="retryServerSearch()"'));
  assert.match(elements.searchResults.innerHTML, />重新搜索</);
"""
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_old_search_timeout_never_aborts_the_new_request_controller():
    result = _run_search_vm(
        """
  elements.articleSearchInput.value = 'first';
  const requests = [];
  context.fetch = (url, options) => new Promise(resolve => requests.push({ url, options, resolve }));

  context.fetchServerSearchResults('first', 1);
  const firstTimeout = timers.find(timer => timer.delay === context.SEARCH_REQUEST_TIMEOUT_MS);
  assert.ok(firstTimeout);
  elements.articleSearchInput.value = 'second';
  context.fetchServerSearchResults('second', 1);
  assert.equal(requests.length, 2);
  const newSignal = requests[1].options.signal;
  assert.equal(newSignal.aborted, false);

  firstTimeout.fn();
  assert.equal(newSignal.aborted, false);
"""
    )
    assert result.returncode == 0, result.stderr or result.stdout


















def _run_translation_update(body):
    # Evaluate only translation-update handling with the article cache and overlay
    # dependencies supplied explicitly, keeping this a browser contract test.
    script = f"""
const assert = require('assert');
const vm = require('vm');
const overlay = {{
  dataset: {{}},
  classList: {{ contains: () => false }},
}};
const articleWrap = {{ id: 'articleWrap' }};
const context = {{
  console,
  userAutoSettings:null,
  authToken: 'tok',
  articleBodyCache: {{}},
  articleBodyPromises: {{}},
  articleBodyControllers: {{}},
  articleBodyRequestGenerations: {{}},
  translationUpdateCursor: '',
  translationUpdateBaselineUncertain: false,
  translationUpdatePolling: false,
  translationUpdatePollPromise: null,
  setTimeout,
  clearTimeout,
  AbortController,
  TRANSLATION_UPDATES_POLL_TIMEOUT_MS: 8000,
  TRANSLATION_UPDATES_BASELINE_WAIT_MS: 2000,
  document: {{
    getElementById: id => id === 'overlay' ? overlay : (id === 'articleWrap' ? articleWrap : null),
  }},
  fetchArticleDetail: async () => {{ throw new Error('fetchArticleDetail not stubbed'); }},
  renderArticleBody: () => {{ throw new Error('renderArticleBody not stubbed'); }},
  autoDisplaySummary: () => {{ throw new Error('autoDisplaySummary not stubbed'); }},
  _overlay: overlay,
  _articleWrap: articleWrap,
}};
vm.createContext(context);
vm.runInContext({json.dumps(_article_body_cache_helpers_block() + _translation_update_block())}, context);
(async () => {{
{body}
}})().catch(error => {{ console.error(error && error.stack ? error.stack : error); process.exitCode = 1; }});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout


def _run(body):
    # Minimal browser shims: an in-memory localStorage and a controllable fetch.
    script = f"""
const assert = require('assert');
const vm = require('vm');
const store = {{}};
const context = {{
  console,
  userAutoSettings:null,
  authToken: 'tok',
  localStorage: {{
    getItem: k => (k in store ? store[k] : null),
    setItem: (k, v) => {{ store[k] = String(v); }},
  }},
  URL,
  _store: store,
}};
context.fetch = async () => {{ throw new Error('fetch not stubbed'); }};
vm.createContext(context);
vm.runInContext({json.dumps(_ai_chat_block())}, context);
(async () => {{
{body}
}})().catch(error => {{ console.error(error && error.stack ? error.stack : error); process.exitCode = 1; }});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout


def _run_auto_display(body):
    # Evaluate only the cache-display function with the smallest browser surface it
    # needs, so this stays a contract test rather than loading the frontend bundle.
    script = f"""
const assert = require('assert');
const vm = require('vm');
const context = {{
  console,
  userAutoSettings:null,
  authToken: 'tok',
  isRestrictedUser: () => false,
  news: [],
  proxyImages: html => html,
  sanitizeTranslatedHtml: html => html,
  syncArticleTitle: () => {{}},
  esc: value => String(value),
  document: {{
    getElementById: () => null,
    querySelector: () => null,
    createElement: () => ({{ style: {{}}, remove: () => {{}} }}),
  }},
}};
context.fetch = async () => {{ throw new Error('fetch not stubbed'); }};
vm.createContext(context);
vm.runInContext({json.dumps(_auto_display_summary_block())}, context);
(async () => {{
{body}
}})().catch(error => {{ console.error(error && error.stack ? error.stack : error); process.exitCode = 1; }});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout
















def test_pending_auto_translation_keeps_original_body_and_does_not_call_manual_translate():
    _run_auto_display("""
const body = { dataset: {}, innerHTML: '<p>English original</p>', querySelectorAll: () => [] };
context.document.getElementById = id => id === 'articleBody' ? body : (id === 'articleWrap' ? { querySelector: () => null, prepend: () => {} } : null);
context.fetch = async () => ({ json: async () => ({}) });
context.userAutoSettings = { auto_translate_content: true };
let manualCalls = 0;
context.aiTranslate = async () => { manualCalls++; };
await context.autoDisplaySummary(42);
assert.equal(manualCalls, 0);
assert.equal(body.innerHTML, '<p>English original</p>');
""")


def test_cached_translation_html_and_title_follow_independent_runtime_gates():
    _run_auto_display("""
const body = { dataset: {}, innerHTML: '<p>English original</p>', querySelectorAll: () => [] };
const h1 = { textContent: 'Original title' };
const wrap = {
  querySelector: selector => selector === 'h1' ? h1 : null,
  insertBefore: () => {},
  prepend: () => {},
};
context.document.getElementById = id => id === 'articleBody' ? body : (id === 'articleWrap' ? wrap : null);
context.fetch = async () => ({ json: async () => ({
  translation: JSON.stringify({ title: '共享译名', html: '<p>共享译文</p>' }),
}) });
let synced = 0;
context.syncArticleTitle = () => { synced++; };
context.userAutoSettings = {
  ai_active: true,
  view_translation: true,
  view_title_translation: false,
};
await context.autoDisplaySummary(42);
assert.equal(body.innerHTML, '<p>共享译文</p>');
assert.equal(h1.textContent, 'Original title');
assert.equal(synced, 0);

body.innerHTML = '<p>English original again</p>';
body.dataset = {};
context.userAutoSettings.view_translation = false;
await context.autoDisplaySummary(42);
assert.equal(body.innerHTML, '<p>English original again</p>');

context.userAutoSettings.view_translation = true;
context.userAutoSettings.ai_active = false;
await context.autoDisplaySummary(42);
assert.equal(body.innerHTML, '<p>English original again</p>');
""")


def test_translation_update_invalidates_closed_cache_and_refreshes_open_article():
    _run_translation_update(r"""
context.articleBodyCache[41] = { body_html: '<p>stale</p>' };
context.articleBodyPromises[41] = Promise.resolve({ body_html: '<p>stale</p>' });
let detailCalls = 0;
context.fetchArticleDetail = async () => { detailCalls++; return { body_html: '<p>译文</p>' }; };
context.applyTranslationUpdate({ id: 41 });
assert.equal(context.articleBodyCache[41], undefined);
assert.equal(context.articleBodyPromises[41], undefined);
assert.equal(detailCalls, 0);

context._overlay.dataset.articleId = '42';
context._overlay.classList.contains = name => name === 'open';
context.articleBodyCache[42] = { body_html: '<p>stale</p>' };
let rendered = null;
let summaryCalls = 0;
context.fetchArticleDetail = async id => {
  detailCalls++;
  assert.equal(id, 42);
  return { body_html: '<p>译文</p>' };
};
context.renderArticleBody = (wrap, data, id) => { rendered = { wrap, data, id }; };
context.autoDisplaySummary = id => { summaryCalls++; assert.equal(id, 42); };
context.applyTranslationUpdate({ id: 42 });
await Promise.resolve();
await Promise.resolve();
assert.equal(detailCalls, 1);
assert.equal(context.articleBodyCache[42], undefined);
assert.deepEqual(rendered, { wrap: context._articleWrap, data: { body_html: '<p>译文</p>' }, id: 42 });
assert.equal(summaryCalls, 1);
""")


def test_translation_update_poll_initializes_cursor_with_authenticated_request():
    _run_translation_update(r"""
const requests = [];
context.fetch = async (url, options) => {
  requests.push({ url, options });
  return { ok: true, json: async () => ({ items: [], cursor: '2026-07-19 10:00:00.000|9' }) };
};
await context.pollTranslationUpdates();
assert.equal(requests.length, 1);
assert.match(requests[0].url, /\/ai\/translation-updates\?since=&t=/);
assert.equal(requests[0].options.headers.Authorization, 'Bearer tok');
assert.equal(context.translationUpdateCursor, '2026-07-19 10:00:00.000|9');
""")


def test_translation_update_poll_times_out_and_can_retry():
    script = f"""
const assert = require('assert');
const vm = require('vm');
let calls = 0;
const context = {{
  console, authToken: 'tok', translationUpdateCursor: '',
  translationUpdateBaselineUncertain: false,
  translationUpdatePolling: false, translationUpdatePollPromise: null,
  articleBodyCache: {{}}, articleBodyPromises: {{}},
  articleBodyControllers: {{}}, articleBodyRequestGenerations: {{}},
  document: {{ getElementById: () => null }},
  setTimeout, clearTimeout, AbortController,
  TRANSLATION_UPDATES_POLL_TIMEOUT_MS: 30,
  TRANSLATION_UPDATES_BASELINE_WAIT_MS: 2000,
}};
context.fetch = (_url, options) => new Promise((_resolve, reject) => {{
  calls += 1;
  if (options.signal) options.signal.addEventListener('abort', () => reject(new Error('aborted')));
}});
vm.createContext(context);
vm.runInContext({json.dumps(_article_body_cache_helpers_block() + _translation_update_block())}, context);
(async () => {{
  let watchdog;
  try {{
    await Promise.race([
      context.pollTranslationUpdates(),
      new Promise((_resolve, reject) => {{ watchdog = setTimeout(() => reject(new Error('poll did not settle')), 100); }}),
    ]);
  }} finally {{
    clearTimeout(watchdog);
  }}
  assert.equal(context.translationUpdatePolling, false);
  assert.equal(context.translationUpdateBaselineUncertain, true);
  context.fetch = async () => ({{ok: true, json: async () => ({{items: [], cursor: 'c|0'}})}});
  await context.pollTranslationUpdates();
  assert.equal(calls, 1);
  assert.equal(context.translationUpdateCursor, 'c|0');
}})().catch(e => {{ console.error(e.stack || e); process.exitCode = 1; }});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True,
                            text=True, timeout=5)
    assert result.returncode == 0, result.stderr or result.stdout


def test_first_detail_starts_with_parallel_translation_baseline():
    script = rf"""
const assert = require('assert');
const vm = require('vm');
const calls = [];
const context = {{
  console,
  userAutoSettings:null,
  authToken: 'tok',
  translationUpdateCursor: '',
  translationUpdateBaselineUncertain: false,
  translationUpdatePolling: false,
  articleBodyCache: {{}},
  articleBodyPromises: {{}},
  articleBodyControllers: {{}},
  articleBodyRequestGenerations: {{}},
  translationUpdatePollPromise: null,
  document: {{ getElementById: () => null }},
  setTimeout,
  clearTimeout,
  AbortController,
  TRANSLATION_UPDATES_POLL_TIMEOUT_MS: 8000,
  TRANSLATION_UPDATES_BASELINE_WAIT_MS: 2000,
}};
context.fetch = async (url, options) => {{
  calls.push({{ url, options }});
  if (url.startsWith('/ai/translation-updates')) {{
    return {{ ok: true, json: async () => ({{ items: [], cursor: '2026-07-19 10:00:00.000|9' }}) }};
  }}
  return {{ ok: true, json: async () => ({{ body_html: '<p>current</p>' }}) }};
}};
vm.createContext(context);
vm.runInContext({json.dumps(_article_body_cache_helpers_block() + _article_detail_block() + _translation_update_block())}, context);
(async () => {{
  assert.deepEqual(await context.fetchArticleDetail(42), {{ body_html: '<p>current</p>' }});
  assert.match(calls[0].url, /^\/ai\/translation-updates\?since=/);
  assert.match(calls[1].url, /^\/api\/news\/42\?/);
}})().catch(error => {{ console.error(error && error.stack ? error.stack : error); process.exitCode = 1; }});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout


def test_article_detail_starts_without_waiting_for_baseline():
    script = rf"""
const assert = require('assert');
const vm = require('vm');
let resolveBaseline;
let detailStartedAt = 0;
const startedAt = Date.now();
const context = {{
  console,
  userAutoSettings:null,
  authToken: 'tok',
  translationUpdateCursor: '',
  translationUpdateBaselineUncertain: false,
  translationUpdatePolling: false,
  translationUpdatePollPromise: null,
  articleBodyCache: {{}},
  articleBodyPromises: {{}},
  articleBodyControllers: {{}},
  articleBodyRequestGenerations: {{}},
  document: {{ getElementById: () => null }},
  setTimeout,
  clearTimeout,
  AbortController,
  TRANSLATION_UPDATES_POLL_TIMEOUT_MS: 5000,
  TRANSLATION_UPDATES_BASELINE_WAIT_MS: 30,
}};
context.fetch = url => {{
  if (url.startsWith('/ai/translation-updates')) {{
    return new Promise(resolve => {{ resolveBaseline = resolve; }});
  }}
  detailStartedAt = Date.now();
  return Promise.resolve({{ ok: true, json: async () => ({{ body_html: '<p>current</p>' }}) }});
}};
vm.createContext(context);
vm.runInContext({json.dumps(_article_body_cache_helpers_block() + _article_detail_block() + _translation_update_block())}, context);
(async () => {{
  let watchdog;
  let baselineSettled = false;
  try {{
    const detail = await Promise.race([
      context.fetchArticleDetail(42),
      new Promise((_resolve, reject) => {{ watchdog = setTimeout(() => reject(new Error('detail did not start after bounded wait')), 500); }}),
    ]);
    const elapsed = detailStartedAt - startedAt;
    assert.deepEqual(detail, {{ body_html: '<p>current</p>' }});
    assert.ok(elapsed < 100, `detail started after ${{elapsed}}ms`);
    resolveBaseline({{ ok: true, json: async () => ({{ items: [], cursor: 'baseline|0' }}) }});
    baselineSettled = true;
    await context.translationUpdatePollPromise;
  }} finally {{
    clearTimeout(watchdog);
    if (!baselineSettled && resolveBaseline) {{
      resolveBaseline({{ ok: true, json: async () => ({{ items: [], cursor: 'cleanup|0' }}) }});
      await context.translationUpdatePollPromise;
    }}
  }}
}})().catch(error => {{ console.error(error && error.stack ? error.stack : error); process.exitCode = 1; }});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout


def test_late_successful_baseline_invalidates_detail_cached_after_wait_timeout():
    script = rf"""
const assert = require('assert');
const vm = require('vm');
let resolveBaseline;
const overlay = {{ dataset: {{}}, classList: {{ contains: () => false }} }};
const context = {{
  console,
  userAutoSettings:null,
  authToken: 'tok',
  translationUpdateCursor: '',
  translationUpdateBaselineUncertain: false,
  translationUpdatePolling: false,
  translationUpdatePollPromise: null,
  articleBodyCache: {{}},
  articleBodyPromises: {{}},
  articleBodyControllers: {{}},
  articleBodyRequestGenerations: {{}},
  document: {{ getElementById: id => id === 'overlay' ? overlay : null }},
  setTimeout,
  clearTimeout,
  AbortController,
  TRANSLATION_UPDATES_POLL_TIMEOUT_MS: 5000,
  TRANSLATION_UPDATES_BASELINE_WAIT_MS: 20,
}};
context.fetch = url => {{
  if (url.startsWith('/ai/translation-updates')) {{
    return new Promise(resolve => {{ resolveBaseline = resolve; }});
  }}
  return Promise.resolve({{ ok: true, json: async () => ({{ body_html: '<p>English cached</p>' }}) }});
}};
vm.createContext(context);
vm.runInContext({json.dumps(_article_body_cache_helpers_block() + _article_detail_block() + _translation_update_block())}, context);
(async () => {{
  let watchdog;
  let baselineSettled = false;
  try {{
    await Promise.race([
      context.fetchArticleDetail(42),
      new Promise((_resolve, reject) => {{ watchdog = setTimeout(() => reject(new Error('detail cache was blocked by baseline')), 500); }}),
    ]);
    assert.equal(context.articleBodyCache[42], undefined);
    assert.equal(context.translationUpdateBaselineUncertain, true);
    resolveBaseline({{ ok: true, json: async () => ({{ items: [], cursor: 'late|0' }}) }});
    baselineSettled = true;
    await context.translationUpdatePollPromise;
    assert.equal(context.translationUpdateBaselineUncertain, false);
    assert.equal(context.translationUpdateCursor, 'late|0');
    assert.equal(context.articleBodyCache[42], undefined);
  }} finally {{
    clearTimeout(watchdog);
    if (!baselineSettled && resolveBaseline) {{
      resolveBaseline({{ ok: true, json: async () => ({{ items: [], cursor: 'cleanup|0' }}) }});
      await context.translationUpdatePollPromise;
    }}
  }}
}})().catch(error => {{ console.error(error && error.stack ? error.stack : error); process.exitCode = 1; }});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout


def test_late_baseline_invalidates_only_details_that_crossed_it():
    script = rf"""
const assert = require('assert');
const vm = require('vm');
let resolveBaseline;
const detailRequests = [];
const overlay = {{ dataset: {{}}, classList: {{ contains: () => false }} }};
const context = {{
  console,
  userAutoSettings:null,
  authToken: 'tok',
  translationUpdateCursor: '',
  translationUpdateBaselineUncertain: false,
  translationUpdatePolling: false,
  translationUpdatePollPromise: null,
  articleBodyCache: {{}},
  articleBodyPromises: {{}},
  articleBodyControllers: {{}},
  articleBodyRequestGenerations: {{}},
  document: {{ getElementById: id => id === 'overlay' ? overlay : null }},
  setTimeout,
  clearTimeout,
  AbortController,
  TRANSLATION_UPDATES_POLL_TIMEOUT_MS: 5000,
  TRANSLATION_UPDATES_BASELINE_WAIT_MS: 30,
}};
context.fetch = url => {{
  if (url.startsWith('/ai/translation-updates')) {{
    return new Promise(resolve => {{ resolveBaseline = resolve; }});
  }}
  detailRequests.push(url);
  const id = url.includes('/api/news/1?') ? 1 : 2;
  return Promise.resolve({{ ok: true, json: async () => ({{ body_html: `<p>detail ${{id}}</p>` }}) }});
}};
vm.createContext(context);
vm.runInContext({json.dumps(_article_body_cache_helpers_block() + _article_detail_block() + _translation_update_block())}, context);
(async () => {{
  let baselineSettled = false;
  let watchdog;
  try {{
    const first = await Promise.race([
      context.fetchArticleDetail(1),
      new Promise((_resolve, reject) => {{ watchdog = setTimeout(() => reject(new Error('first detail did not cross bounded wait')), 500); }}),
    ]);
    clearTimeout(watchdog);
    assert.deepEqual(first, {{ body_html: '<p>detail 1</p>' }});
    assert.equal(context.translationUpdateBaselineUncertain, true);
    assert.equal(context.articleBodyCache[1], undefined);

    const second = context.fetchArticleDetail(2);
    assert.ok(context.articleBodyPromises[2]);
    assert.ok(context.articleBodyControllers[2]);
    resolveBaseline({{ ok: true, json: async () => ({{ items: [], cursor: 'shared|0' }}) }});
    baselineSettled = true;
    await context.translationUpdatePollPromise;

    const secondData = await Promise.race([
      second,
      new Promise((_resolve, reject) => {{ watchdog = setTimeout(() => reject(new Error('second detail was superseded after baseline success')), 500); }}),
    ]);
    clearTimeout(watchdog);
    assert.deepEqual(secondData, {{ body_html: '<p>detail 2</p>' }});
    assert.equal(context.translationUpdateCursor, 'shared|0');
    assert.equal(context.translationUpdateBaselineUncertain, false);
    assert.equal(context.articleBodyCache[1], undefined);
    assert.equal(context.articleBodyCache[2], undefined);
    assert.equal(detailRequests.filter(url => url.startsWith('/api/news/1?')).length, 1);
    assert.equal(detailRequests.filter(url => url.startsWith('/api/news/2?')).length, 1);
  }} finally {{
    clearTimeout(watchdog);
    if (!baselineSettled && resolveBaseline) {{
      resolveBaseline({{ ok: true, json: async () => ({{ items: [], cursor: 'cleanup|0' }}) }});
      await context.translationUpdatePollPromise;
    }}
  }}
}})().catch(error => {{ console.error(error && error.stack ? error.stack : error); process.exitCode = 1; }});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout


def test_successful_baseline_after_a_failed_initial_poll_invalidates_english_detail_cache():
    script = rf"""
const assert = require('assert');
const vm = require('vm');
const overlay = {{ dataset: {{ articleId: '42' }}, classList: {{ contains: name => name === 'open' }} }};
const calls = [];
const context = {{
  console,
  userAutoSettings:null,
  authToken: 'tok',
  translationUpdateCursor: '',
  translationUpdateBaselineUncertain: false,
  translationUpdatePolling: false,
  translationUpdatePollPromise: null,
  articleBodyCache: {{ 42: {{ body_html: '<p>English cached</p>' }} }},
  articleBodyPromises: {{}},
  articleBodyControllers: {{}},
  articleBodyRequestGenerations: {{}},
  document: {{ getElementById: id => id === 'overlay' ? overlay : (id === 'articleWrap' ? {{ id }} : null) }},
  setTimeout,
  clearTimeout,
  AbortController,
  TRANSLATION_UPDATES_POLL_TIMEOUT_MS: 8000,
  TRANSLATION_UPDATES_BASELINE_WAIT_MS: 2000,
  fetchArticleDetail: async id => {{ calls.push(['detail', id]); return {{ body_html: '<p>Chinese current</p>' }}; }},
  renderArticleBody: (wrap, data, id) => calls.push(['render', id, data.body_html]),
  autoDisplaySummary: id => calls.push(['summary', id]),
}};
let attempt = 0;
context.fetch = async () => {{
  attempt++;
  if (attempt === 1) throw new TypeError('network down');
  return {{ ok: true, json: async () => ({{ items: [], cursor: '2026-07-19 10:00:00.000|9' }}) }};
}};
vm.createContext(context);
vm.runInContext({json.dumps(_article_body_cache_helpers_block() + _translation_update_block())}, context);
(async () => {{
  await context.pollTranslationUpdates();
  assert.deepEqual(context.articleBodyCache[42], {{ body_html: '<p>English cached</p>' }});
  await context.pollTranslationUpdates();
  await Promise.resolve(); await Promise.resolve();
  assert.equal(context.articleBodyCache[42], undefined);
  assert.deepEqual(calls, [
    ['detail', 42], ['render', 42, '<p>Chinese current</p>'], ['summary', 42],
  ]);
}})().catch(error => {{ console.error(error && error.stack ? error.stack : error); process.exitCode = 1; }});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout


def test_translation_update_aborts_and_supersedes_inflight_stale_detail_request():
    script = f"""
const assert = require('assert');
const vm = require('vm');
const overlay = {{ dataset: {{ articleId: '42' }}, classList: {{ contains: name => name === 'open' }} }};
const requests = [];
let rendered = null;
const context = {{
  console,
  userAutoSettings:null,
  authToken: 'tok',
  translationUpdateCursor: 'cursor',
  translationUpdateBaselineUncertain: false,
  articleBodyCache: {{}},
  articleBodyPromises: {{}},
  articleBodyControllers: {{}},
  articleBodyRequestGenerations: {{}},
  translationUpdatePolling: false,
  translationUpdatePollPromise: null,
  document: {{ getElementById: id => id === 'overlay' ? overlay : (id === 'articleWrap' ? {{ id }} : null) }},
  setTimeout,
  clearTimeout,
  AbortController,
  TRANSLATION_UPDATES_POLL_TIMEOUT_MS: 8000,
  TRANSLATION_UPDATES_BASELINE_WAIT_MS: 2000,
  renderArticleBody: (wrap, data, id) => {{ rendered = {{ data, id }}; }},
  autoDisplaySummary: () => {{}},
}};
context.fetch = (url, options) => new Promise(resolve => requests.push({{ url, options, resolve }}));
vm.createContext(context);
vm.runInContext({json.dumps(_article_body_cache_helpers_block() + _article_detail_block() + _translation_update_block())}, context);
(async () => {{
  const stale = context.fetchArticleDetail(42);
  assert.equal(requests.length, 1);
  context.applyTranslationUpdate({{ id: 42 }});
  assert.equal(requests[0].options.signal.aborted, true);
  assert.equal(requests.length, 2);
  requests[1].resolve({{ ok: true, json: async () => ({{ body_html: '<p>译文</p>' }}) }});
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  requests[0].resolve({{ ok: true, json: async () => ({{ body_html: '<p>English stale</p>' }}) }});
  await assert.rejects(stale);
  await Promise.resolve(); await Promise.resolve();
  assert.deepEqual(context.articleBodyCache[42], {{ body_html: '<p>译文</p>' }});
  assert.deepEqual(rendered, {{ data: {{ body_html: '<p>译文</p>' }}, id: 42 }});
}})().catch(error => {{ console.error(error && error.stack ? error.stack : error); process.exitCode = 1; }});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout
