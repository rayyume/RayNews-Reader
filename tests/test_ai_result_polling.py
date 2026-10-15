from test_frontend_refresh_behavior import run_node, source_between

SCHEDULER = source_between('let aiResultCursor =', 'function markArticleOpen(') + '''
globalThis.qaPollState = () => ({cursor:aiResultCursor,pending:!!aiResultPoll,failures:aiResultFailures});
globalThis.qaMakePollDue = () => { aiResultNextPollAt=0; };
'''
SETUP = '''
context.sharedSessionGeneration=1;
context.authToken='account-a'; context.authUser={id:7};
context.translationUpdateCursor=''; context.translationUpdateBaselineUncertain=false;
context.translationUpdatePollController=null; context.translationUpdatePolling=false; context.translationUpdatePollPromise=null;
context.sharedResultVersions=new Map();
context.setTimeout=(callback,ms)=>{ const timer=setTimeout(callback,ms); timer.unref(); return timer; }; context.clearTimeout=clearTimeout;
context.cancelSharedResultReads=()=>{};
context.events={};
context.document={hidden:false,getElementById:()=>null,addEventListener:(name,fn)=>context.events[name]=fn};
context.window={addEventListener:()=>{}};
context.storage=new Map();
context.localStorage={getItem:key=>context.storage.get(key)||null,setItem:(key,value)=>context.storage.set(key,value),removeItem:key=>context.storage.delete(key)};
context.fetch=async()=>({ok:true,json:async()=>({cursor:'epoch|0',items:[],has_more:false})});
'''


def test_poll_merges_inflight_and_discards_previous_account_response():
    run_node(SCHEDULER, '''
let complete; let calls=0;
context.fetch=()=>{calls++;return new Promise(resolve=>complete=resolve);};
const first=context.pollAIResultUpdates(true);
const duplicate=context.pollAIResultUpdates(true);
assert.equal(calls,1);
context.sharedSessionGeneration++; context.authToken='account-b';
context.resetAIResultUpdates();
complete({ok:true,json:async()=>({cursor:'old-account|999',items:[],has_more:false})});
await Promise.all([first,duplicate]);
assert.equal(context.qaPollState().cursor,'');
assert.equal(context.qaPollState().pending,false);
''', SETUP)


def test_hidden_pause_failure_recovery_and_pagination():
    run_node(SCHEDULER, '''
let calls=0;
context.fetch=async()=>{calls++;return {ok:false,status:503};};
context.document.hidden=true;
await context.pollAIResultUpdates(true);
assert.equal(calls,0);
context.document.hidden=false;
await context.pollAIResultUpdates(true);
assert.equal(context.qaPollState().cursor,'');
assert.equal(context.qaPollState().failures,1);
context.fetch=async()=>({ok:true,json:async()=>({cursor:'epoch|'+(++calls),items:[],has_more:calls<4})});
await context.pollAIResultUpdates(true);
assert.equal(context.qaPollState().cursor,'epoch|4');
assert.equal(context.qaPollState().failures,0);
''', SETUP)


def test_follower_baselines_once_then_leader_lease_prevents_extra_polls():
    run_node(SCHEDULER, '''
context.storage.set('raynews-ai-result-lease-7',JSON.stringify({owner:'another-tab',expires:Date.now()+30000}));
let calls=0;
context.fetch=async()=>{calls++;return {ok:true,json:async()=>({cursor:'epoch|0',items:[],has_more:false})};};
await context.pollAIResultUpdates(true);
assert.equal(calls,1); // Initial follower catch-up is permitted.
context.qaMakePollDue();
await context.pollAIResultUpdates();
assert.equal(calls,1);
context.storage.set('raynews-ai-result-lease-7',JSON.stringify({owner:'closed-tab',expires:0}));
await context.pollAIResultUpdates(true);
assert.equal(calls,2);
context.document.hidden=true;
context.events.visibilitychange();
assert.equal(context.storage.has('raynews-ai-result-lease-7'),false);
''', SETUP)
