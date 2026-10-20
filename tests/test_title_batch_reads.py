"""Deterministic in-flight title updates against the actual frontend loader."""
from test_frontend_refresh_behavior import run_node, source_between

LOADER = source_between('const serverTitleResults =', 'function displayTitle(')
UPDATES = source_between('function applyAIResultPage(', 'async function pollAIResultUpdates(')
SETUP = '''
context.queueMicrotask=queueMicrotask;
context.setTimeout=setTimeout; context.clearTimeout=clearTimeout;
context.aiSessionGeneration=1; context.authToken='user-a';
context.userAutoSettings={view_title_translation:true,view_title_shortening:true};
context.aiAccessKey=()=>JSON.stringify(context.userAutoSettings);
context.news=[{id:1},{id:2}]; context.searchItems=[]; context.sourceArticlesState={items:[]};
context.document={hidden:false,getElementById:()=>null};
context.renderList=()=>{}; context.renderSearchResults=()=>{}; context.renderSourceArticles=()=>{}; context.renderFavorites=()=>{};
context.cancelAIResultReads=()=>{}; context.clearAIArticleView=()=>{};
context.refreshCurrentAIResult=()=>{};
context.requests=[];
context.fetch=(url,options)=>new Promise(resolve=>context.requests.push({url,options,resolve}));
context.ok=items=>({ok:true,json:async()=>({items})});
context.flush=()=>new Promise(resolve=>setImmediate(resolve));
'''


def test_updates_during_batch_are_reread_and_stale_payload_never_replaces_cache():
    run_node(LOADER + UPDATES, '''
const initial=context.loadVisibleAITitles();
assert.equal(context.requests.length,1);
context.applyAIResultPage({items:[{id:1,version:2}]});
context.applyAIResultPage({items:[{id:1,version:3}]});
context.requests[0].resolve(context.ok({'1':{title_translation:'STALE'},'2':{title_translation:'Unchanged'}}));
await initial; await context.flush();
assert.equal(context.requests.length,2);
assert.equal(vm.runInContext('serverTitleResults.has(1)',context),false);
assert.equal(vm.runInContext('serverTitleResults.get(2).title_translation',context),'Unchanged');
assert.match(context.requests[1].url,/ids=1$/);
context.requests[1].resolve(context.ok({'1':{title_translation:'Latest'}}));
await context.flush();
assert.equal(vm.runInContext('serverTitleResults.get(1).title_translation',context),'Latest');
assert.equal(context.requests.length,2);
''', SETUP)


def test_visible_items_added_while_busy_are_not_lost():
    run_node(LOADER, '''
const initial=context.loadVisibleAITitles();
context.news.push({id:3});
await context.loadVisibleAITitles();
context.requests[0].resolve(context.ok({'1':{},'2':{}}));
await initial; await context.flush();
assert.equal(context.requests.length,2);
assert.match(context.requests[1].url,/ids=3$/);
context.requests[1].resolve(context.ok({'3':{title_translation:'New article'}}));
await context.flush();
assert.equal(vm.runInContext('serverTitleResults.get(3).title_translation',context),'New article');
''', SETUP)


def test_current_article_result_cannot_be_overwritten_by_an_older_batch():
    run_node(LOADER, '''
const initial=context.loadVisibleAITitles();
context.storeAITitleResult(1,{title_translation:'Current article result'});
context.requests[0].resolve(context.ok({'1':{title_translation:'STALE'},'2':{}}));
await initial; await context.flush();
assert.equal(vm.runInContext('serverTitleResults.get(1).title_translation',context),'Current article result');
assert.equal(context.requests.length,1);
''', SETUP)


def test_account_change_while_busy_catches_up_without_publishing_previous_account():
    run_node(LOADER, '''
const initial=context.loadVisibleAITitles();
context.aiSessionGeneration++; context.authToken='user-b';
context.resetAITitleResults();
await context.loadVisibleAITitles();
context.requests[0].resolve(context.ok({'1':{title_translation:'PRIVATE A'},'2':{}}));
await initial; await context.flush();
assert.equal(context.requests.length,2);
assert.equal(context.requests[1].options.headers.Authorization,'Bearer user-b');
assert.equal(vm.runInContext('serverTitleResults.has(1)',context),false);
context.requests[1].resolve(context.ok({'1':{title_translation:'B'},'2':{}}));
await context.flush();
assert.equal(vm.runInContext('serverTitleResults.get(1).title_translation',context),'B');
''', SETUP)


def test_failed_followup_keeps_invalidated_item_retryable():
    run_node(LOADER + UPDATES, '''
const initial=context.loadVisibleAITitles();
context.applyAIResultPage({items:[{id:1,version:2}]});
context.requests[0].resolve(context.ok({'1':{title_translation:'STALE'},'2':{}}));
await initial; await context.flush();
context.requests[1].resolve({ok:false,status:503});
await context.flush();
assert.equal(vm.runInContext('serverTitleResults.has(1)',context),false);
const retry=context.loadVisibleAITitles();
assert.equal(context.requests.length,3);
context.requests[2].resolve(context.ok({'1':{title_translation:'Recovered'}}));
await retry;
assert.equal(vm.runInContext('serverTitleResults.get(1).title_translation',context),'Recovered');
''', SETUP)
