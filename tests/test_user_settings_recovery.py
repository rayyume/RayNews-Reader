"""A failed settings read must not revoke sharing until the server says so."""

from test_frontend_refresh_behavior import run_node, source_between


SETTINGS = source_between(
    'function scheduleUserSettingsRetry(', 'function findOpenArticleShareData('
)
SETUP = '''
context.sharedSettingsRequestSequence = 0;
context.sharedSessionGeneration = 1;
context.sharedSettingsRetryTimer = null;
context.sharedSettingsRetryCount = 0;
context.authToken = 'account-a';
context.userAutoSettings = {share_active:true,share_view_summary:true,share_view_translation:true};
context.document = {hidden:false};
context.applyThemePreference = () => {};
context.synchronizeShareAccessState = async next => {context.userAutoSettings = next;};
context.timers = [];
context.setTimeout = (callback, delay) => {context.timers.push({callback,delay}); return context.timers.length;};
context.clearTimeout = () => {};
'''


def test_error_response_preserves_sharing_and_retries_without_opening_settings():
    run_node(SETTINGS, '''
context.apiFetch = async (_url, options) => {
  assert.equal(options.cache, 'no-store');
  return {error:'auth_expired',status:401};
};
assert.equal(await context.loadUserSettings(), false);
assert.equal(context.userAutoSettings.share_active, true);
assert.equal(context.userAutoSettings.share_view_summary, true);
assert.equal(context.timers.length, 1);
assert.equal(context.timers[0].delay, 2000);
context.apiFetch = async () => ({share_active:true,share_ai_results:true,
  share_view_summary:true,share_view_translation:true});
context.timers[0].callback();
await new Promise(resolve => setImmediate(resolve));
assert.equal(context.userAutoSettings.share_active, true);
assert.equal(context.userAutoSettings.share_view_translation, true);
assert.equal(context.sharedSettingsRetryCount, 0);
''', SETUP)


def test_network_failure_retries_but_obsolete_account_response_does_not():
    run_node(SETTINGS, '''
context.apiFetch = async () => {throw new TypeError('network down');};
assert.equal(await context.loadUserSettings(), false);
assert.equal(context.timers.length, 1);
let complete;
context.apiFetch = () => new Promise(resolve => {complete=resolve;});
const obsolete = context.loadUserSettings();
context.sharedSessionGeneration++;
context.authToken = 'account-b';
complete({share_active:false});
assert.equal(await obsolete, false);
assert.equal(context.userAutoSettings.share_active, true);
assert.equal(context.timers.length, 1);
''', SETUP)


def test_foreground_resume_refreshes_settings():
    foreground = source_between('function onReturnToForeground()', "document.addEventListener('visibilitychange'",)
    assert 'if (authToken) loadUserSettings();' in foreground
