"""Frontend contract: aiChat() tries the direct call first and only falls back to the
same-origin /ai/chat relay when the provider blocks the browser-direct call (CORS)."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")


def _ai_chat_block():
    start = HTML.index("function isDeepSeekModel(")
    end = HTML.index("async function aiSummarize(")
    return HTML[start:end]


def _auto_display_summary_block():
    start = HTML.index("async function autoDisplaySummary(")
    end = HTML.index("function showAIActions(", start)
    return HTML[start:end]


def _translation_update_block():
    start = HTML.index("function invalidateArticleBody(")
    end = HTML.index("function articleItemHtml(", start)
    return HTML[start:end]


def _article_detail_block():
    start = HTML.index("function fetchArticleDetail(")
    end = HTML.index("function renderArticleBody(", start)
    return HTML[start:end]


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
    return HTML[start:end]


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


def test_display_title_requires_active_shared_access():
    script = f"""
const assert = require('assert');
const vm = require('vm');
const context = {{ userAutoSettings: {{ share_active: false, share_view_title: true }} }};
vm.createContext(context);
vm.runInContext({json.dumps(_display_title_block())}, context);
const article = {{ title: '共享译名', original_title: 'Original title' }};
assert.equal(context.displayTitle(article), 'Original title');
context.userAutoSettings.share_active = true;
assert.equal(context.displayTitle(article), '共享译名');
context.userAutoSettings.share_view_title = false;
assert.equal(context.displayTitle(article), 'Original title');
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout


def test_share_tab_shows_one_of_two_pending_revalidation_failure_warning():
    """A first scheduled failure must warn without making the active share look down."""
    script = rf"""
const assert = require('assert');
const vm = require('vm');
const elements = Object.fromEntries([
  'shareAiResults', 'shareViewTitle', 'shareViewTranslation', 'shareViewSummary',
].map(id => [id, {{ checked: false, disabled: false }}]));
elements.shareSubHint = {{ textContent: '', style: {{ display: '' }} }};
elements.shareCheckStatus = {{ textContent: '', style: {{ color: '' }} }};
const context = {{
  userAutoSettings: {{
    share_ai_results: true,
    share_active: true,
    share_suspended: false,
    share_revalidation_failure_streak: 1,
    share_revalidation_last_failure_at: '2026-07-30T10:00:00',
    share_revalidation_last_failure_error: 'AI API HTTP 503',
  }},
  loadUserSettings: async () => {{}},
  document: {{ getElementById: id => elements[id] }},
}};
vm.createContext(context);
vm.runInContext({json.dumps(_share_tab_block())}, context);
(async () => {{
  await context.loadShareTab();
  assert.match(elements.shareCheckStatus.textContent, /后台复核暂时失败（1\/2）/);
  assert.match(elements.shareCheckStatus.textContent, /共享仍在运行/);
  assert.match(elements.shareCheckStatus.textContent, /AI API HTTP 503/);
  assert.equal(elements.shareCheckStatus.style.color, '#e5a84d');
}})().catch(error => {{ console.error(error && error.stack ? error.stack : error); process.exitCode = 1; }});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout


def test_share_tab_marks_unvalidated_inactive_intent_unavailable_and_disables_controls():
    """A stale 1/2 value must not claim an inactive share is still running."""
    script = rf"""
const assert = require('assert');
const vm = require('vm');
const elements = Object.fromEntries([
  'shareAiResults', 'shareViewTitle', 'shareViewTranslation', 'shareViewSummary',
].map(id => [id, {{ checked: false, disabled: false }}]));
elements.shareSubHint = {{ textContent: '', style: {{ display: '' }} }};
elements.shareCheckStatus = {{ textContent: '', style: {{ color: '' }} }};
const context = {{
  userAutoSettings: {{
    share_ai_results: true,
    share_active: false,
    share_suspended: false,
    share_view_title: true,
    share_view_translation: true,
    share_view_summary: true,
    share_revalidation_failure_streak: 1,
    share_revalidation_last_failure_error: 'AI API HTTP 503',
  }},
  loadUserSettings: async () => {{}},
  document: {{ getElementById: id => elements[id] }},
}};
vm.createContext(context);
vm.runInContext({json.dumps(_share_tab_block())}, context);
(async () => {{
  await context.loadShareTab();
  assert.doesNotMatch(elements.shareCheckStatus.textContent, /共享仍在运行/);
  assert.doesNotMatch(elements.shareCheckStatus.textContent, /后台复核暂时失败/);
  assert.match(elements.shareCheckStatus.textContent, /当前 AI 配置尚未通过校验/);
  assert.match(elements.shareCheckStatus.textContent, /共享暂时不可用/);
  assert.match(elements.shareSubHint.textContent, /共享暂时不可用/);
  for (const id of ['shareViewTitle', 'shareViewTranslation', 'shareViewSummary']) {{
    assert.equal(elements[id].disabled, true);
    assert.equal(elements[id].checked, true);
  }}
}})().catch(error => {{ console.error(error && error.stack ? error.stack : error); process.exitCode = 1; }});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout


def test_share_subcontrols_stay_editable_while_enabling_new_unsaved_intent():
    """Inactive persisted intent is unavailable; a newly checked switch is still editable."""
    script = rf"""
const assert = require('assert');
const vm = require('vm');
const elements = Object.fromEntries([
  'shareViewTitle', 'shareViewTranslation', 'shareViewSummary',
].map(id => [id, {{ checked: false, disabled: false }}]));
elements.shareAiResults = {{ checked: true, disabled: false }};
elements.shareSubHint = {{ textContent: '', style: {{ display: '' }} }};
const context = {{
  userAutoSettings: {{
    share_ai_results: false,
    share_active: false,
    share_suspended: false,
  }},
  document: {{ getElementById: id => elements[id] }},
}};
vm.createContext(context);
vm.runInContext({json.dumps(_share_tab_block())}, context);
context.updateShareSubToggleState();
for (const id of ['shareViewTitle', 'shareViewTranslation', 'shareViewSummary']) {{
  assert.equal(elements[id].disabled, false);
}}
assert.equal(elements.shareSubHint.textContent, '');
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout


def test_failed_share_checks_refresh_effective_title_access_for_save_and_manual_probe():
    script = f"""
const assert = require('assert');
const vm = require('vm');
const elements = Object.fromEntries([
  'aiProvider', 'aiEndpoint', 'aiModel', 'aiProviderType', 'aiApiKey',
].map(id => [id, {{ value: '' }}]));
elements.aiEnabled = {{ checked: true }};
let mode = 'save-failed';
let reloads = 0;
let renders = 0;
const statuses = [];
const renderedTitles = [];
const context = {{
  console,
  authToken: 'token',
  userAutoSettings: {{ share_active: true, share_view_title: true }},
  document: {{ getElementById: id => elements[id] || {{ value: '', checked: false }} }},
  showSettingsStatus: (...args) => statuses.push(args),
  loadAIConfig: () => {{}},
  logout: () => {{}},
  renderList: () => {{ renders++; renderedTitles.push(context.displayTitle({{ title: 'Shared title', original_title: 'Original title' }})); }},
  loadUserSettings: async () => {{
    reloads++;
    context.userAutoSettings = {{ share_active: false, share_view_title: true, share_suspended: true }};
    context.renderList();
  }},
}};
context.fetch = async url => {{
  const body = mode === 'save-failed'
    ? {{ share_check: {{ error: 'AI API HTTP 401' }} }}
    : mode === 'manual-failed'
      ? {{ error: 'AI API HTTP 401', share_check: {{ error: 'AI API HTTP 401' }} }}
      : mode === 'save-stale'
        ? {{ share_check: {{ status: 'stale', restored: false }} }}
        : {{ response: 'pong', share_check: {{ restored: true }} }};
  return {{ status: mode === 'manual-failed' ? 502 : 200, text: async () => JSON.stringify(body) }};
}};
vm.createContext(context);
vm.runInContext({json.dumps(_display_title_block() + _share_connection_actions_block())}, context);

(async () => {{
await context.saveAIConfig();
assert.equal(reloads, 1);
assert.equal(renders, 1);
assert.deepEqual(renderedTitles, ['Original title']);
assert.match(statuses.at(-1)[0], /AI 配置已保存，但连接校验失败/);

mode = 'manual-failed';
await context.testAIConnection();
assert.equal(reloads, 2);
assert.equal(renders, 2);
assert.equal(renderedTitles.at(-1), 'Original title');
assert.equal(statuses.at(-1)[0], '❌ AI API HTTP 401');

mode = 'save-stale';
await context.saveAIConfig();
assert.equal(reloads, 3);
assert.equal(renders, 3);
assert.equal(renderedTitles.at(-1), 'Original title');

mode = 'manual-restored';
await context.testAIConnection();
assert.equal(reloads, 4);
assert.equal(renders, 4);
assert.match(statuses.at(-1)[0], /共享状态已自动恢复/);
}})().catch(error => {{ console.error(error && error.stack ? error.stack : error); process.exitCode = 1; }});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout


def test_central_share_state_sync_revokes_and_recovers_every_open_surface():
    script = f"""
const assert = require('assert');
const vm = require('vm');
const classes = open => ({{ contains: name => name === 'open' && open }});
const h1 = {{ textContent: 'Shared title' }};
const body = {{
  innerHTML: '<p>共享译文</p>',
  dataset: {{
    originalHtml: '<p>Original body</p>',
    originalTitle: 'Original title',
    translatedHtml: '<p>共享译文</p>',
    translatedTitle: 'Shared title',
    showingTranslation: 'true',
    sharedTranslation: 'true',
  }},
}};
const summary = {{ removed: false, remove() {{ this.removed = true; }} }};
const elements = {{
  overlay: {{ dataset: {{ articleId: '42' }}, classList: classes(true) }},
  articleBody: body,
  articleWrap: {{ querySelector: selector => selector === 'h1' ? h1 : null }},
  aiSummaryTop: summary,
  favOverlay: {{ classList: classes(true) }},
  sourceArticlesOverlay: {{ classList: classes(true) }},
  searchOverlay: {{ classList: classes(true) }},
}};
const calls = [];
const context = {{
  console,
  userAutoSettings: {{ share_active: true, share_view_title: true }},
  articleBodyCache: {{
    42: {{ id: 42, title: 'Shared title', original_title: 'Original title', body_html: '<p>Original body</p>' }},
  }},
  news: [{{ id: 42, title: 'Shared title', original_title: 'Original title' }}],
  searchItems: [{{ id: 42, title: 'Shared title', original_title: 'Original title' }}],
  sourceArticlesState: {{ items: [{{ id: 42, title: 'Shared title', original_title: 'Original title' }}] }},
  document: {{
    getElementById: id => elements[id] || null,
  }},
  applyThemePreference: () => {{}},
  renderList: () => calls.push('main'),
  renderSearchResults: () => calls.push('search'),
  loadFavorites: async () => calls.push('favorites'),
  renderSourceArticles: () => calls.push('source'),
  autoDisplaySummary: async id => calls.push('detail-shared-' + id),
}};
vm.createContext(context);
vm.runInContext({json.dumps(_display_title_block() + _user_settings_sync_block())}, context);
(async () => {{
  await context.synchronizeShareAccessState({{
    share_active: false,
    share_view_title: true,
    share_view_translation: true,
    share_view_summary: true,
  }});
  assert.equal(body.innerHTML, '<p>Original body</p>');
  assert.equal(h1.textContent, 'Original title');
  assert.equal(summary.removed, true);
  assert.equal(body.dataset.translatedHtml, undefined);
  assert.deepEqual(calls, ['main', 'search', 'favorites', 'source']);

  summary.removed = false;
  await context.synchronizeShareAccessState({{
    share_active: true,
    share_view_title: true,
    share_view_translation: true,
    share_view_summary: true,
  }});
  assert.equal(h1.textContent, 'Shared title');
  assert.deepEqual(calls, [
    'main', 'search', 'favorites', 'source',
    'main', 'search', 'favorites', 'source', 'detail-shared-42',
  ]);
}})().catch(error => {{ console.error(error && error.stack ? error.stack : error); process.exitCode = 1; }});
"""
    result = subprocess.run(
        ["node", "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_notification_poll_refreshes_settings_once_per_share_transition():
    script = f"""
const assert = require('assert');
const vm = require('vm');
let response = {{ items: [{{ id: 1, type: 'general' }}], unread: 1 }};
let settingsRefreshes = 0;
const context = {{
  console,
  authToken: 'token',
  notifItems: [],
  notifUnread: 0,
  document: {{ querySelector: () => null, getElementById: () => null }},
  apiFetch: async () => response,
  loadUserSettings: async () => {{ settingsRefreshes++; }},
}};
vm.createContext(context);
vm.runInContext({json.dumps(_notification_status_block())}, context);
(async () => {{
  await context.refreshNotifStatus();
  assert.equal(settingsRefreshes, 0);
  response = {{
    items: [{{ id: 2, type: 'share_suspended' }}, {{ id: 1, type: 'general' }}],
    unread: 2,
  }};
  await context.refreshNotifStatus();
  assert.equal(settingsRefreshes, 1);
  await context.refreshNotifStatus();
  assert.equal(settingsRefreshes, 1);
  response = {{
    items: [{{ id: 3, type: 'share_restored' }}, {{ id: 2, type: 'share_suspended' }}],
    unread: 3,
  }};
  await context.refreshNotifStatus();
  assert.equal(settingsRefreshes, 2);
}})().catch(error => {{ console.error(error && error.stack ? error.stack : error); process.exitCode = 1; }});
"""
    result = subprocess.run(
        ["node", "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_favorite_and_source_history_titles_use_effective_title_gate():
    script = f"""
const assert = require('assert');
const vm = require('vm');
const bodies = {{ sourceArticlesBody: {{ innerHTML: '' }}, favBody: {{ innerHTML: '' }} }};
const context = {{
  userAutoSettings: {{ share_active: false, share_view_title: true }},
  authUser: {{ role: 'user' }},
  document: {{ getElementById: id => bodies[id] }},
  esc: value => String(value),
  proxyImgSrc: value => value,
  feedSourceOf: item => item.feed_source || item.source,
  displaySourceForArticle: value => value,
  badgeStyle: () => '',
  sourceBadgeTitle: value => value,
  sourceLabel: value => value,
  formatTime: () => '',
}};
vm.createContext(context);
vm.runInContext({json.dumps(_display_title_block() + _title_list_rendering_block())}, context);
const item = {{ id: 7, article_id: 7, title: 'Shared title', original_title: 'Original title', source: 'Feed', feed_source: 'Feed', date: '', time: '' }};
context.renderSourceArticles([item]);
context.renderFavorites({{ items: [item] }});
for (const html of [bodies.sourceArticlesBody.innerHTML, bodies.favBody.innerHTML]) {{
  assert.match(html, /Original title/);
  assert.doesNotMatch(html, /Shared title/);
}}
context.userAutoSettings.share_active = true;
context.renderSourceArticles([item]);
context.renderFavorites({{ items: [item] }});
for (const html of [bodies.sourceArticlesBody.innerHTML, bodies.favBody.innerHTML]) {{
  assert.match(html, /Shared title/);
}}
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
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
vm.runInContext({json.dumps(_translation_update_block())}, context);
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


def test_cors_friendly_endpoint_uses_direct_and_never_relays():
    _run("""
let relayed = false;
context.fetch = async () => { relayed = true; return { ok: true, json: async () => ({ content: 'x' }) }; };
context.callOpenAiChat = async () => 'direct-answer';
context.callClaudeChat = async () => { throw new Error('wrong path'); };

const out = await context.aiChat(
  { provider_type: 'openai', endpoint: 'https://api.deepseek.com/v1' },
  [{ role: 'user', content: 'hi' }], 100, 0.3);

assert.equal(out, 'direct-answer');
assert.equal(relayed, false);
assert.equal(context._store.aiRelayOrigins, undefined);  // origin not marked
""")


def test_opencode_go_endpoint_uses_relay_without_attempting_browser_direct_call():
    _run("""
const relayCalls = [];
context.fetch = async (url, opts) => {
  relayCalls.push({ url, body: JSON.parse(opts.body) });
  return { ok: true, json: async () => ({ content: 'relayed' }) };
};
let directCalls = 0;
context.callOpenAiChat = async () => { directCalls++; throw new Error('must not call direct'); };

const out = await context.aiChat(
  { provider_type: 'openai', endpoint: 'https://opencode.ai/zen/go/v1' },
  [{ role: 'user', content: 'hi' }], 100, 0.3);

assert.equal(out, 'relayed');
assert.equal(directCalls, 0);
assert.equal(relayCalls.length, 1);
assert.equal(relayCalls[0].url, '/ai/chat');
""")


def test_browser_direct_deepseek_request_disables_thinking():
    _run("""
const calls = [];
context.fetch = async (url, opts) => {
  calls.push({ url, body: JSON.parse(opts.body) });
  return { ok: true, json: async () => ({ choices: [{ message: { content: 'ok' } }] }) };
};
const deepseekCfg = { endpoint: 'https://opencode.ai/zen/go/v1', api_key: 'key', model: 'DeepSeek-V4-Flash' };
assert.equal(await context.callOpenAiChat(deepseekCfg, [{ role: 'user', content: 'hi' }], 1024, 0.3), 'ok');
assert.deepEqual(calls[0].body.thinking, { type: 'disabled' });

calls.length = 0;
const openAiCfg = { endpoint: 'https://api.openai.com/v1', api_key: 'key', model: 'gpt-4o-mini' };
await context.callOpenAiChat(openAiCfg, [{ role: 'user', content: 'hi' }], 1024, 0.3);
assert.equal('thinking' in calls[0].body, false);
""")


def test_cors_blocked_endpoint_falls_back_to_relay_and_remembers_origin():
    _run("""
const relayCalls = [];
context.fetch = async (url, opts) => {
  relayCalls.push({ url, body: JSON.parse(opts.body), auth: opts.headers.Authorization });
  return { ok: true, json: async () => ({ content: 'relayed' }) };
};
let directCalls = 0;
context.callOpenAiChat = async () => { directCalls++; throw new TypeError('Load failed'); };
context.callClaudeChat = async () => { throw new Error('unused'); };

const cfg = { provider_type: 'openai', endpoint: 'https://cors-blocked.example/v1' };
const out1 = await context.aiChat(cfg, [{ role: 'user', content: 'hi' }], 8000, 0.3);
assert.equal(out1, 'relayed');
assert.equal(directCalls, 1);                 // tried direct once
assert.equal(relayCalls.length, 1);
assert.equal(relayCalls[0].url, '/ai/chat');
assert.equal(relayCalls[0].auth, 'Bearer tok');
assert.equal(relayCalls[0].body.max_tokens, 8000);
// Origin remembered so the next call skips the doomed direct attempt.
assert.deepEqual(JSON.parse(context._store.aiRelayOrigins), ['https://cors-blocked.example']);

const out2 = await context.aiChat(cfg, [{ role: 'user', content: 'again' }], 8000, 0.3);
assert.equal(out2, 'relayed');
assert.equal(directCalls, 1);                 // did NOT try direct a second time
assert.equal(relayCalls.length, 2);
""")


def test_real_api_error_is_not_rerouted_through_relay():
    _run("""
let relayed = false;
context.fetch = async () => { relayed = true; return { ok: true, json: async () => ({ content: 'x' }) }; };
// A 4xx/5xx from the provider surfaces as a plain Error, not a TypeError — must propagate.
context.callOpenAiChat = async () => { throw new Error('AI API HTTP 500: boom'); };

await assert.rejects(
  context.aiChat({ provider_type: 'openai', endpoint: 'https://api.deepseek.com/v1' },
                 [{ role: 'user', content: 'hi' }], 100, 0.3),
  err => /AI API HTTP 500/.test(err.message));
assert.equal(relayed, false);                 // did not silently reroute a real error
assert.equal(context._store.aiRelayOrigins, undefined);
""")


def test_network_drop_after_a_prior_direct_success_is_not_relayed():
    # A CORS-friendly endpoint proven to work must not have a later network drop
    # (which may already have reached and billed the provider) silently retried through
    # the relay — that would double-charge.
    _run("""
let relayed = false;
context.fetch = async () => { relayed = true; return { ok: true, json: async () => ({ content: 'x' }) }; };
let directCalls = 0;
context.callOpenAiChat = async () => {
  directCalls++;
  if (directCalls === 1) return 'ok';           // first call succeeds → origin proven CORS-friendly
  throw new TypeError('Load failed');           // second: a genuine network drop
};
const cfg = { provider_type: 'openai', endpoint: 'https://api.deepseek.com/v1' };

assert.equal(await context.aiChat(cfg, [{ role: 'user', content: 'a' }], 100, 0.3), 'ok');
assert.deepEqual(JSON.parse(context._store.aiDirectOkOrigins), ['https://api.deepseek.com']);

await assert.rejects(
  context.aiChat(cfg, [{ role: 'user', content: 'b' }], 100, 0.3),
  err => err instanceof TypeError);
assert.equal(relayed, false);                   // did NOT reroute → no duplicate charge
assert.equal(context._store.aiRelayOrigins, undefined);
""")


def test_relay_verdict_not_persisted_when_the_relay_itself_fails():
    # A one-off outage where BOTH direct and relay fail must not permanently pin a
    # (possibly CORS-friendly) endpoint to the relay path.
    _run("""
context.fetch = async () => { throw new TypeError('Load failed'); };  // relay also down
context.callOpenAiChat = async () => { throw new TypeError('Load failed'); };
const cfg = { provider_type: 'openai', endpoint: 'https://api.deepseek.com/v1' };

await assert.rejects(context.aiChat(cfg, [{ role: 'user', content: 'x' }], 100, 0.3));
assert.equal(context._store.aiRelayOrigins, undefined);  // not persisted on relay failure
""")


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
  share_active: true,
  share_view_translation: true,
  share_view_title: false,
};
await context.autoDisplaySummary(42);
assert.equal(body.innerHTML, '<p>共享译文</p>');
assert.equal(h1.textContent, 'Original title');
assert.equal(synced, 0);

body.innerHTML = '<p>English original again</p>';
body.dataset = {};
context.userAutoSettings.share_view_translation = false;
await context.autoDisplaySummary(42);
assert.equal(body.innerHTML, '<p>English original again</p>');

context.userAutoSettings.share_view_translation = true;
context.userAutoSettings.share_active = false;
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
vm.runInContext({json.dumps(_translation_update_block())}, context);
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


def test_first_detail_waits_for_translation_cursor_baseline():
    script = rf"""
const assert = require('assert');
const vm = require('vm');
const calls = [];
const context = {{
  console,
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
vm.runInContext({json.dumps(_article_detail_block() + _translation_update_block())}, context);
(async () => {{
  assert.deepEqual(await context.fetchArticleDetail(42), {{ body_html: '<p>current</p>' }});
  assert.match(calls[0].url, /^\/ai\/translation-updates\?since=/);
  assert.match(calls[1].url, /^\/api\/news\/42\?/);
}})().catch(error => {{ console.error(error && error.stack ? error.stack : error); process.exitCode = 1; }});
"""
    result = subprocess.run(["node", "-e", script], cwd=ROOT,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr or result.stdout


def test_article_detail_starts_after_bounded_baseline_wait():
    script = rf"""
const assert = require('assert');
const vm = require('vm');
let resolveBaseline;
let detailStartedAt = 0;
const startedAt = Date.now();
const context = {{
  console,
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
vm.runInContext({json.dumps(_article_detail_block() + _translation_update_block())}, context);
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
    assert.ok(elapsed >= 20 && elapsed <= 500, `detail started after ${{elapsed}}ms`);
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
vm.runInContext({json.dumps(_article_detail_block() + _translation_update_block())}, context);
(async () => {{
  let watchdog;
  let baselineSettled = false;
  try {{
    await Promise.race([
      context.fetchArticleDetail(42),
      new Promise((_resolve, reject) => {{ watchdog = setTimeout(() => reject(new Error('detail cache was blocked by baseline')), 500); }}),
    ]);
    assert.deepEqual(context.articleBodyCache[42], {{ body_html: '<p>English cached</p>' }});
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
vm.runInContext({json.dumps(_article_detail_block() + _translation_update_block())}, context);
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
    assert.deepEqual(context.articleBodyCache[1], {{ body_html: '<p>detail 1</p>' }});

    const second = context.fetchArticleDetail(2);
    assert.ok(context.articleBodyPromises[2]);
    assert.equal(context.articleBodyControllers[2], undefined);
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
    assert.deepEqual(context.articleBodyCache[2], {{ body_html: '<p>detail 2</p>' }});
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
vm.runInContext({json.dumps(_translation_update_block())}, context);
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
vm.runInContext({json.dumps(_article_detail_block() + _translation_update_block())}, context);
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
