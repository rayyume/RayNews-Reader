"""Race and recovery tests execute the actual frontend reader in Node."""
import pytest
from test_frontend_refresh_behavior import run_node, source_between

HELPERS = source_between('function sharedPermissionKey(', 'function showAIActions(')
SETUP = r'''
context.sharedSessionGeneration = 1;
context.sharedAccessGeneration = 0;
context.sharedResultReads = new Map();
context.authToken = 'account-a';
context.activeArticleRequestId = 1;
context.userAutoSettings = {share_active:true,share_view_summary:true,share_view_translation:true,share_view_title:true};
context.DOMException = DOMException;
context.setTimeout = setTimeout;
context.clearTimeout = clearTimeout;
context.abortableDelay = async () => {};
context.isRestrictedUser = () => false;
context.esc = text => text;
context.syncArticleTitle = () => {};
context.proxyImages = text => text;
context.sanitizeTranslatedHtml = text => text;
context.elements = {};
context.h1 = {textContent:'Original'};
context.document = {
  getElementById: id => context.elements[id] || null,
  querySelector: () => null,
  createElement: () => ({style:{},setAttribute(){},appendChild(){},remove(){delete context.elements[this.id];}}),
};
context.elements.overlay = {dataset:{articleId:'1'},classList:{contains:() => context.open}};
context.open = true;
context.elements.articleWrap = {
  appendChild(el){context.elements[el.id]=el;}, prepend(el){context.elements[el.id]=el;},
  querySelector: selector => selector === 'h1' ? context.h1 : null,
};
context.elements.articleBody = {dataset:{},innerHTML:'<p>original</p>'};
context.ok = data => ({ok:true,json:async()=>data,headers:{get:()=>null}});
'''


@pytest.mark.parametrize('transition', [
    "context.activeArticleRequestId++; context.elements.overlay.dataset.articleId='2';",
    "context.activeArticleRequestId+=2;",  # A -> B -> A
    "context.open=false;",
    "context.sharedSessionGeneration++; context.authToken=null;",
    "context.sharedSessionGeneration++; context.authToken='account-b';",
    "context.userAutoSettings.share_active=false;",
    "context.userAutoSettings.share_view_summary=false;",
    "context.sharedAccessGeneration+=2;",
])
def test_late_response_cannot_update_new_context(transition):
    run_node(HELPERS, f'''
let complete;
context.fetch = () => new Promise(resolve => complete = resolve);
const pending = context.autoDisplaySummary(1);
{transition}
complete(context.ok({{summary:'OLD ACCOUNT OR ARTICLE'}}));
await pending;
assert.equal(context.elements.aiSummaryTop, undefined);
''', SETUP)


def test_inflight_dedup_and_empty_response_can_be_retried():
    run_node(HELPERS, r'''
let complete; let requests = 0;
context.fetch = () => {requests++; return new Promise(resolve => complete=resolve);};
const ctx = context.sharedArticleContext(1);
const first = context.readSharedResult(ctx);
const second = context.readSharedResult(ctx);
assert.strictEqual(first,second);
complete(context.ok({})); await first;
context.fetch = async () => {requests++; return context.ok({summary:'new'});};
assert.equal((await context.readSharedResult(ctx)).summary,'new');
assert.equal(requests,2);
assert.equal(context.sharedResultReads.size,0);
''', SETUP)


@pytest.mark.parametrize('status', [401,403,404,400])
def test_nonretryable_http_failure_is_not_empty(status):
    run_node(HELPERS, f'''
let requests=0;
context.fetch=async()=>{{requests++;return {{ok:false,status:{status},headers:{{get:()=>null}}}};}};
await assert.rejects(context.readSharedResult(context.sharedArticleContext(1)), e=>e.status==={status});
assert.equal(requests,1);
''', SETUP)


def test_transient_failure_recovers_without_generation():
    run_node(HELPERS, r'''
let requests=0;
context.fetch=async()=>++requests===1 ? {ok:false,status:503,headers:{get:()=>null}} : context.ok({summary:'recovered'});
await context.autoDisplaySummary(1);
assert.equal(requests,2);
assert.match(context.elements.aiSummaryTop.innerHTML,/recovered/);
''', SETUP)


def test_exhausted_failure_is_recoverable_and_retry_after_is_respected():
    run_node(HELPERS, r'''
let requests=0;
context.fetch=async()=>{requests++;return {ok:false,status:503,headers:{get:()=>null}};};
await assert.rejects(context.readSharedResult(context.sharedArticleContext(1)));
assert.equal(requests,3);
context.fetch=async()=>{requests++;return {ok:false,status:429,headers:{get:()=> '3600'}};};
await assert.rejects(context.readSharedResult(context.sharedArticleContext(1)));
assert.equal(requests,4);
context.fetch=async()=>context.ok({summary:'next attempt'});
assert.equal((await context.readSharedResult(context.sharedArticleContext(1))).summary,'next attempt');
''', SETUP)


def test_real_cancellation_does_not_retry():
    run_node(HELPERS, r'''
let requests=0;
context.fetch=(_url,{signal})=>{requests++;return new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('cancel','AbortError'))));};
const pending=context.readSharedResult(context.sharedArticleContext(1));
context.cancelSharedResultReads();
await assert.rejects(pending,e=>e.name==='AbortError');
assert.equal(requests,1);
''', SETUP)


def test_first_timeout_then_success():
    run_node(HELPERS, r'''
let requests=0;
context.setTimeout=(callback,ms)=>setTimeout(callback,ms===8000?1:ms);
context.fetch=(_url,{signal})=>++requests===1
 ? new Promise((_resolve,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('timeout','AbortError'))))
 : Promise.resolve(context.ok({summary:'after timeout'}));
assert.equal((await context.readSharedResult(context.sharedArticleContext(1))).summary,'after timeout');
assert.equal(requests,2);
''', SETUP)
