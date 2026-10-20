"""A failed settings read must not revoke sharing until the server says so."""

from test_frontend_refresh_behavior import run_node, source_between


SETTINGS = source_between(
    'function scheduleUserSettingsRetry(', 'function findOpenArticleData('
)
SETUP = '''
context.normalizeAIAccess=s=>({...s,ai_active:Object.values(s).some(value=>value===true)});
context.renderAIFunctions=()=>{}; context.loadVisibleAITitles=async()=>{};
context.aiSettingsRequestSequence = 0;
context.aiSessionGeneration = 1;
context.aiSettingsRetryTimer = null;
context.aiSettingsRetryCount = 0;
context.authToken = 'account-a';
context.userAutoSettings = {ai_active:true,view_summary:true,view_translation:true};
context.document = {hidden:false};
context.applyThemePreference = () => {};
context.synchronizeAIAccessState = async next => {context.userAutoSettings = next;};
context.digestSyncCalls = 0;
context.syncDailySummaryToday = () => {context.digestSyncCalls++;};
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
assert.equal(context.userAutoSettings.ai_active, true);
assert.equal(context.userAutoSettings.view_summary, true);
assert.equal(context.timers.length, 1);
assert.equal(context.timers[0].delay, 2000);
context.apiFetch = async () => ({access:{permissions:{summary:true,translation:true},view_summary:true,view_translation:true}});
context.timers[0].callback();
await new Promise(resolve => setImmediate(resolve));
assert.equal(context.userAutoSettings.ai_active, true);
assert.equal(context.userAutoSettings.view_translation, true);
assert.equal(context.aiSettingsRetryCount, 0);
''', SETUP)


def test_network_failure_retries_but_obsolete_account_response_does_not():
    run_node(SETTINGS, '''
context.apiFetch = async () => {throw new TypeError('network down');};
assert.equal(await context.loadUserSettings(), false);
assert.equal(context.timers.length, 1);
let complete;
context.apiFetch = () => new Promise(resolve => {complete=resolve;});
const obsolete = context.loadUserSettings();
context.aiSessionGeneration++;
context.authToken = 'account-b';
complete({ai_active:false});
assert.equal(await obsolete, false);
assert.equal(context.userAutoSettings.ai_active, true);
assert.equal(context.timers.length, 1);
''', SETUP)


def test_foreground_resume_refreshes_settings():
    foreground = source_between('function onReturnToForeground()', "document.addEventListener('visibilitychange'",)
    assert 'if (authToken) loadUserSettings();' in foreground


def test_successful_settings_load_syncs_digest_before_slow_title_reads():
    run_node(SETTINGS, '''
context.apiFetch=async()=>({access:{permissions:{daily_digest:true},view_daily_digest:true}});
context.loadVisibleAITitles=()=>new Promise(()=>{});
context.loadUserSettings();
await new Promise(resolve=>setImmediate(resolve));
assert.equal(context.userAutoSettings.view_daily_digest,true);
assert.equal(context.digestSyncCalls,1);
''', SETUP)


def test_obsolete_settings_continuation_never_syncs_new_account_digest():
    run_node(SETTINGS, '''
let finish;
context.apiFetch=async()=>({access:{permissions:{daily_digest:true},view_daily_digest:true}});
context.synchronizeAIAccessState=()=>new Promise(resolve=>{finish=resolve;});
const loading=context.loadUserSettings();
await new Promise(resolve=>setImmediate(resolve));
context.aiSessionGeneration++;
context.authToken='account-b';
finish();
assert.equal(await loading,false);
assert.equal(context.digestSyncCalls,0);
''', SETUP)


def test_older_digest_response_cannot_overwrite_newer_state():
    source = source_between('async function syncDailySummaryToday()', '// ═══ Daily Summary (Sidebar)')
    run_node(source, '''
context.authToken='token';context.aiSessionGeneration=1;
context.userAutoSettings={view_daily_digest:true};
context.dailySummaryRequestSequence=0;
context.aiAccessKey=()=> 'digest-enabled';
context.dailySummaryState={status:'scheduled',generateAt:'21:00'};
context.isDailySummaryOpen=()=>false;
context.setDailySummaryState=next=>{context.dailySummaryState={...context.dailySummaryState,...next};};
context.parseJsonResponse=async response=>response;
const requests=[];
context.fetch=()=>new Promise(resolve=>requests.push(resolve));
const older=context.syncDailySummaryToday();
const newer=context.syncDailySummaryToday();
requests[1]({status:'completed',updated_at:'new-version',summary:'Current digest'});
await newer;
requests[0]({status:'scheduled'});
await older;
assert.equal(context.dailySummaryState.status,'completed');
assert.equal(context.dailySummaryState.result.summary,'Current digest');
''')


def test_account_change_while_digest_body_is_loading_discards_response():
    source = source_between('async function syncDailySummaryToday()', '// ═══ Daily Summary (Sidebar)')
    run_node(source, '''
context.authToken='account-a';context.aiSessionGeneration=1;
context.userAutoSettings={view_daily_digest:true};
context.dailySummaryRequestSequence=0;
context.aiAccessKey=()=> 'same-access-flags';
context.dailySummaryState={status:'scheduled',generateAt:'21:00'};
context.isDailySummaryOpen=()=>false;
context.setDailySummaryState=next=>{context.dailySummaryState={...context.dailySummaryState,...next};};
let finishBody;
context.parseJsonResponse=()=>new Promise(resolve=>{finishBody=resolve;});
context.fetch=async()=>({status:200});
const loading=context.syncDailySummaryToday();
await new Promise(resolve=>setImmediate(resolve));
context.aiSessionGeneration++;
context.authToken='account-b';
finishBody({status:'completed',updated_at:'old-account',summary:'Do not apply'});
await loading;
assert.equal(context.dailySummaryState.status,'scheduled');
assert.equal(context.dailySummaryState.result,undefined);
''')
