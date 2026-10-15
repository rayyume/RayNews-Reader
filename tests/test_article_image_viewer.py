from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")


def between(start: str, end: str) -> str:
    left = HTML.index(start)
    right = HTML.index(end, left)
    return HTML[left:right]


def test_viewer_has_accessible_stage_close_button_and_single_delegated_entry():
    assert 'id="lbStage"' in HTML
    assert 'id="lbCloseBtn"' in HTML
    assert 'role="dialog"' in HTML
    assert 'aria-modal="true"' in HTML
    assert "function openImageViewer(image)" in HTML
    assert "articleWrap.addEventListener('click'" in HTML
    delegated = between(
        "articleWrap.addEventListener('click'",
        "\n});",
    )
    assert "closest('img')" in delegated
    assert "openImageViewer(image)" in delegated


def test_dynamic_translation_paths_do_not_bind_image_click_handlers():
    translate = between("async function aiTranslate(", "async function autoDisplaySummary(")
    auto_display = between("async function autoDisplaySummary(", "function showAIActions(")
    render_body = between("function renderArticleBody(", "function formatTime(")
    for block in (translate, auto_display, render_body):
        assert "querySelectorAll('img').forEach" not in block
        assert "img.addEventListener('click'" not in block


def test_viewer_no_longer_mutates_viewport_meta_for_zoom():
    close_block = between("function closeLightbox(", "function shareArticle()")
    assert "meta[name=viewport]" not in close_block
    assert "maximum-scale" not in close_block


def test_viewer_runtime_opens_resets_traps_focus_and_restores_trigger():
    viewer = between("const lightboxGesture =", "function shareArticle()")
    runtime = r'''
const assert = require('node:assert/strict');

class FakeClassList {
  constructor() { this.values = new Set(); }
  add(value) { this.values.add(value); }
  remove(value) { this.values.delete(value); }
  contains(value) { return this.values.has(value); }
}

class FakeElement {
  constructor(id, { tabIndex = 0, connected = true } = {}) {
    this.id = id;
    this.tabIndex = tabIndex;
    this.isConnected = connected;
    this.disabled = false;
    this.attrs = new Map();
    this.style = {};
    this.classList = new FakeClassList();
    this.listeners = {};
    this.focusables = [];
    this.focusCount = 0;
  }
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  removeAttribute(name) { this.attrs.delete(name); }
  getAttribute(name) { return this.attrs.get(name) || null; }
  hasAttribute(name) { return this.attrs.has(name); }
  addEventListener(type, handler) { this.listeners[type] = handler; }
  querySelectorAll() { return this.focusables; }
  getClientRects() { return this.isConnected ? [{}] : []; }
  focus() { this.focusCount += 1; document.activeElement = this; }
  contains(node) { return node === this || this.focusables.includes(node); }
}

class FakeImage extends FakeElement {
  constructor(id) {
    super(id);
    this.currentSrc = 'https://cdn.example/resolved.jpg';
    this.src = 'https://cdn.example/fallback.jpg';
    this.alt = '图片说明';
    this.complete = true;
    this.naturalWidth = 640;
  }
}

global.HTMLImageElement = FakeImage;
const articleWrap = new FakeElement('articleWrap');
const lb = new FakeElement('lb');
const closeButton = new FakeElement('lbCloseBtn');
const lbStage = new FakeElement('lbStage');
const lbImage = new FakeImage('lbImg');
const trigger = new FakeImage('trigger');
lb.focusables = [closeButton];
const elements = { articleWrap, lb, lbCloseBtn: closeButton, lbStage, lbImg: lbImage };
const documentListeners = {};
global.document = {
  body: { style: {} },
  activeElement: trigger,
  getElementById(id) { return elements[id]; },
  addEventListener(type, handler) { documentListeners[type] = handler; },
};

let lockCalls = 0;
let unlockCalls = 0;
global.lockBodyScroll = () => { lockCalls += 1; };
global.unlockBodyScroll = () => { unlockCalls += 1; };
global.recoverImageLoad = () => { throw new Error('loaded image should not recover'); };

eval(process.argv[1]);

openImageViewer(trigger);
assert.equal(lbImage.style.transform, '', 'opening clears any prior gesture transform');
assert.equal(lbImage.src, trigger.currentSrc);
assert.equal(lbImage.alt, trigger.alt);
assert.equal(lb.classList.contains('open'), true);
assert.equal(lb.getAttribute('aria-hidden'), 'false');
assert.equal(lockCalls, 1);
assert.equal(closeButton.focusCount, 1, 'focus enters the dialog close control');

const tabEvent = { key: 'Tab', shiftKey: false, prevented: false, preventDefault() { this.prevented = true; } };
assert.equal(typeof documentListeners.keydown, 'function', 'dialog installs a Tab trap');
documentListeners.keydown(tabEvent);
assert.equal(tabEvent.prevented, true);
assert.equal(document.activeElement, closeButton);

const shiftTabEvent = { key: 'Tab', shiftKey: true, prevented: false, preventDefault() { this.prevented = true; } };
documentListeners.keydown(shiftTabEvent);
assert.equal(shiftTabEvent.prevented, true);
assert.equal(document.activeElement, closeButton);

closeLightbox();
assert.equal(lb.classList.contains('open'), false);
assert.equal(lb.getAttribute('aria-hidden'), 'true');
assert.equal(unlockCalls, 1);
assert.equal(trigger.focusCount, 1, 'connected focusable trigger regains focus');
'''
    result = subprocess.run(
        ["node", "-e", runtime, viewer],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def run_node(js: str) -> None:
    subprocess.run(
        ["node", "--input-type=module", "-e", js],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )


def test_gesture_helpers_clamp_scale_distance_and_offsets():
    helpers = between("function clampLightboxScale(", "function resetLightboxGesture(")
    run_node(
        """
import assert from 'node:assert/strict';
const context = globalThis;
context.document = {
  getElementById(id) {
    if (id === 'lbStage') return { clientWidth: 320, clientHeight: 640 };
    if (id === 'lbImg') return { offsetWidth: 300, offsetHeight: 200 };
    return null;
  }
};
"""
        + helpers
        + """
assert.equal(clampLightboxScale(.2), 1);
assert.equal(clampLightboxScale(2.5), 2.5);
assert.equal(clampLightboxScale(8), 4);
assert.equal(lightboxDistance({x:0,y:0}, {x:3,y:4}), 5);
assert.deepEqual(clampLightboxOffset(999, -999, 1), {x:0,y:0});
const limited = clampLightboxOffset(999, -999, 2);
assert.ok(limited.x <= 140 && limited.x >= -140);
assert.ok(limited.y <= 0 && limited.y >= -320);
"""
    )


def test_close_resets_transform_and_pointer_state_before_unlocking_scroll():
    close_block = between("function closeLightbox(", "function shareArticle()")
    assert "resetLightboxGesture();" in close_block
    assert close_block.index("resetLightboxGesture();") < close_block.index("unlockBodyScroll();")
    assert "aria-hidden', 'true'" in close_block


def test_gesture_surface_uses_centered_transform_origin_without_browser_pinch_zoom():
    stage_css = between('.lb-stage{', '\n.lb img{')
    image_css = between('.lb img{', '\n.lb-close{')
    assert 'touch-action:none' in stage_css
    assert 'transform-origin:center' in image_css


def test_gesture_runtime_dispatches_centered_pinch_pan_cancel_and_close_reset():
    viewer = between("const lightboxGesture =", "function shareArticle()")
    runtime = r'''
const assert = require('node:assert/strict');

class FakeClassList {
  constructor(values = []) { this.values = new Set(values); }
  add(value) { this.values.add(value); }
  remove(value) { this.values.delete(value); }
  contains(value) { return this.values.has(value); }
}

class FakeElement {
  constructor(id) {
    this.id = id;
    this.attrs = new Map();
    this.style = {};
    this.classList = new FakeClassList();
    this.listeners = {};
    this.captured = [];
    this.removed = [];
  }
  addEventListener(type, handler) {
    (this.listeners[type] ||= []).push(handler);
  }
  setPointerCapture(pointerId) { this.captured.push(pointerId); }
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  removeAttribute(name) { this.removed.push(name); this.attrs.delete(name); }
  querySelectorAll() { return []; }
  getBoundingClientRect() {
    return { left: 0, top: 0, width: this.clientWidth || 0, height: this.clientHeight || 0 };
  }
  focus() {}
}

const articleWrap = new FakeElement('articleWrap');
const lb = new FakeElement('lb');
lb.classList.add('open');
const lbStage = new FakeElement('lbStage');
lbStage.clientWidth = 320;
lbStage.clientHeight = 640;
const lbImg = new FakeElement('lbImg');
lbImg.offsetWidth = 300;
lbImg.offsetHeight = 400;
const lbCloseBtn = new FakeElement('lbCloseBtn');
const elements = { articleWrap, lb, lbStage, lbImg, lbCloseBtn };
global.document = {
  getElementById(id) { return elements[id]; },
  addEventListener() {},
  activeElement: null,
};
global.unlockBodyScroll = () => {};

const parseTransform = () => {
  const match = /^translate3d\(([-\d.]+)px,([-\d.]+)px,0\) scale\(([-\d.]+)\)$/.exec(lbImg.style.transform);
  assert.ok(match, `expected transform, got ${lbImg.style.transform}`);
  return { x: Number(match[1]), y: Number(match[2]), scale: Number(match[3]) };
};
const approx = (actual, expected, label) => {
  assert.ok(Math.abs(actual - expected) < 1e-9, `${label}: expected ${expected}, got ${actual}`);
};
const dispatch = (type, pointerId, clientX, clientY) => {
  const handlers = lbStage.listeners[type];
  assert.equal(handlers.length, 1, `${type} listener registration`);
  handlers[0]({ currentTarget: lbStage, pointerId, clientX, clientY });
};

eval(process.argv[1]);
for (const type of ['pointerdown', 'pointermove', 'pointerup', 'pointercancel']) {
  assert.equal(lbStage.listeners[type].length, 1, `${type} registered exactly once`);
}

// The scale and translated midpoint are both preserved around the first pinch centre.
dispatch('pointerdown', 1, 100, 300);
dispatch('pointerdown', 2, 140, 300);
dispatch('pointermove', 2, 180, 340);
const scale = Math.hypot(80, 40) / 40;
let transform = parseTransform();
approx(transform.scale, scale, 'off-centre pinch scale');
approx(transform.x, 20 + (scale - 1) * 40, 'off-centre pinch x');
approx(transform.y, 20 + (scale - 1) * 20, 'off-centre pinch y');

// Lifting one finger creates a one-finger drag baseline; oversized movement clamps it.
dispatch('pointerup', 2, 180, 340);
dispatch('pointermove', 1, 1000, 1000);
transform = parseTransform();
approx(transform.x, (300 * scale - 320) / 2, 'pan x clamp');
approx(transform.y, (400 * scale - 640) / 2, 'pan y clamp');
const beforeCancel = lbImg.style.transform;
dispatch('pointercancel', 1, 1000, 1000);
dispatch('pointermove', 1, 2000, 2000);
assert.equal(lbImg.style.transform, beforeCancel, 'cancelled pointer no longer moves the image');

// Runtime dispatch also proves both scale bounds.
lb.classList.add('open');
dispatch('pointerdown', 3, 10, 10);
dispatch('pointerdown', 4, 20, 10);
dispatch('pointermove', 4, 11, 10);
assert.equal(parseTransform().scale, 1, 'pinch scale has a 1× floor');
dispatch('pointercancel', 3, 10, 10);
dispatch('pointercancel', 4, 11, 10);
closeLightbox();
lb.classList.add('open');
dispatch('pointerdown', 5, 10, 10);
dispatch('pointerdown', 6, 20, 10);
dispatch('pointermove', 6, 1000, 10);
assert.equal(parseTransform().scale, 4, 'pinch scale has a 4× cap');

// Additional pointers are ignored, so the first two remain the gesture pair.
const capturesBeforeThirdPointer = lbStage.captured.length;
dispatch('pointerdown', 7, 30, 10);
assert.equal(lbStage.captured.length, capturesBeforeThirdPointer, 'third pointer is ignored');
assert.deepEqual(lbStage.captured.slice(-2), [5, 6], 'the first two pointers remain the gesture pair');
dispatch('pointerup', 7, 30, 10);
dispatch('pointermove', 6, 30, 10);
assert.equal(parseTransform().scale, 2, 'ignored third pointerup preserves the active pinch baseline');
dispatch('pointercancel', 7, 30, 10);
dispatch('pointermove', 6, 40, 10);
assert.equal(parseTransform().scale, 3, 'ignored third pointercancel preserves the active pinch baseline');
closeLightbox();
assert.equal(lbImg.style.transform, '', 'close clears the transform');
assert.ok(lbImg.removed.includes('src'), 'close clears the image source');
const capturesBeforeClosedPointer = lbStage.captured.length;
dispatch('pointerdown', 8, 50, 50);
assert.equal(lbStage.captured.length, capturesBeforeClosedPointer, 'closed viewer ignores pointer input');
'''
    result = subprocess.run(
        ["node", "-e", runtime, viewer],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_closed_viewer_markup_starts_outside_the_interaction_tree():
    assert '<div class="lb" id="lb" role="dialog" aria-modal="true"\n     aria-label="图片全屏查看" aria-hidden="true" inert>' in HTML


def test_closed_viewer_and_dynamic_article_images_have_real_keyboard_focus_semantics():
    viewer = between("const lightboxGesture =", "function shareArticle()")
    runtime = r'''
const assert = require('node:assert/strict');

class FakeClassList {
  constructor(values = []) { this.values = new Set(values); }
  add(value) { this.values.add(value); }
  remove(value) { this.values.delete(value); }
  contains(value) { return this.values.has(value); }
}

class FakeElement {
  constructor(id, { tabIndex = -1, parent = null } = {}) {
    this.id = id;
    this.tabIndex = tabIndex;
    this.parentElement = parent;
    this.isConnected = true;
    this.disabled = false;
    this.inert = false;
    this.attrs = new Map();
    this.style = {};
    this.classList = new FakeClassList();
    this.listeners = {};
    this.images = [];
    this.focusables = [];
    this.lastFocusOptions = null;
  }
  setAttribute(name, value) {
    this.attrs.set(name, String(value));
    if (name === 'tabindex') this.tabIndex = Number(value);
    if (name === 'inert') this.inert = true;
  }
  removeAttribute(name) {
    this.attrs.delete(name);
    if (name === 'inert') this.inert = false;
  }
  getAttribute(name) { return this.attrs.get(name) ?? null; }
  hasAttribute(name) { return this.attrs.has(name); }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  querySelectorAll(selector) {
    if (selector === 'img') return this.images;
    return this.focusables;
  }
  getClientRects() { return this.isConnected ? [{}] : []; }
  contains(node) {
    return node === this
      || node.parentElement === this
      || this.focusables.includes(node)
      || this.images.includes(node);
  }
  canReceiveFocus() {
    for (let node = this; node; node = node.parentElement) {
      if (!node.isConnected || node.inert) return false;
    }
    return !this.disabled && this.tabIndex >= 0;
  }
  focus(options) {
    if (!this.canReceiveFocus()) return;
    this.lastFocusOptions = options || null;
    document.activeElement = this;
  }
}

class FakeImage extends FakeElement {
  constructor(id, parent) {
    super(id, { tabIndex: -1, parent });
    this.currentSrc = `https://cdn.example/${id}.jpg`;
    this.src = this.currentSrc;
    this.alt = '图片说明';
    this.complete = true;
    this.naturalWidth = 640;
  }
  closest(selector) { return selector === 'img' ? this : null; }
}

const observers = [];
global.MutationObserver = class {
  constructor(callback) { this.callback = callback; observers.push(this); }
  observe() {}
};
global.HTMLImageElement = FakeImage;
const articleWrap = new FakeElement('articleWrap');
const overlay = new FakeElement('overlay');
const backButton = new FakeElement('backBtn', { tabIndex: 0, parent: overlay });
const lb = new FakeElement('lb');
lb.setAttribute('inert', '');
const closeButton = new FakeElement('lbCloseBtn', { tabIndex: 0, parent: lb });
const lbStage = new FakeElement('lbStage', { parent: lb });
const lbImage = new FakeImage('lbImg', lbStage);
lb.focusables = [closeButton];
const trigger = new FakeImage('dynamic-image', articleWrap);
trigger.setAttribute('role', 'presentation');
const elements = {
  articleWrap,
  overlay,
  backBtn: backButton,
  lb,
  lbCloseBtn: closeButton,
  lbStage,
  lbImg: lbImage,
};
const documentListeners = {};
global.document = {
  body: { style: {} },
  activeElement: backButton,
  getElementById(id) { return elements[id]; },
  addEventListener(type, handler) { (documentListeners[type] ||= []).push(handler); },
};
global.lockBodyScroll = () => {};
global.unlockBodyScroll = () => {};
global.recoverImageLoad = () => { throw new Error('loaded image should not recover'); };

eval(process.argv[1]);
assert.equal(observers.length, 1, 'dynamic article content is observed once');
articleWrap.images = [trigger];
observers[0].callback([{ addedNodes: [trigger] }]);
assert.equal(trigger.tabIndex, 0, 'dynamic image joins the sequential focus order');
assert.equal(trigger.getAttribute('role'), 'button');
assert.equal(trigger.getAttribute('aria-label'), '查看大图：图片说明');
assert.equal(articleWrap.listeners.keydown.length, 1, 'one delegated keyboard handler');

const keydown = (key) => {
  const event = {
    key,
    target: trigger,
    prevented: false,
    stopped: false,
    preventDefault() { this.prevented = true; },
    stopPropagation() { this.stopped = true; },
  };
  articleWrap.listeners.keydown[0](event);
  assert.equal(event.prevented, true, `${key} prevents default browser behavior`);
  assert.equal(event.stopped, true, `${key} uses the delegated viewer boundary`);
};

trigger.focus();
keydown('Enter');
assert.equal(lb.inert, false, 'open viewer enters the focus tree');
assert.equal(lb.getAttribute('aria-hidden'), 'false');
assert.equal(document.activeElement, closeButton, 'focus enters the close button');
closeLightbox();
assert.equal(lb.inert, true, 'closed viewer leaves the focus tree');
assert.equal(document.activeElement, trigger, 'focus returns to the keyboard opener');
assert.deepEqual(trigger.lastFocusOptions, { preventScroll: true });
const closedTabOrder = [backButton, trigger, closeButton]
  .filter(element => element.canReceiveFocus())
  .map(element => element.id);
assert.deepEqual(closedTabOrder, ['backBtn', 'dynamic-image']);

trigger.focus();
keydown(' ');
assert.equal(document.activeElement, closeButton, 'Space opens through the same viewer');
trigger.isConnected = false;
closeLightbox();
assert.equal(document.activeElement, backButton, 'disconnected opener falls back to article navigation');
assert.deepEqual(backButton.lastFocusOptions, { preventScroll: true });
closeButton.focus();
assert.equal(document.activeElement, backButton, 'closed inert dialog cannot recapture focus');
'''
    result = subprocess.run(
        ["node", "-e", runtime, viewer],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_switching_articles_closes_viewer_before_replacing_content_and_balances_scroll_lock():
    viewer = between("const lightboxGesture =", "function shareArticle()")
    open_article = between("function openArticle(id)", "function fetchArticleDetail(")
    runtime = r'''
const assert = require('node:assert/strict');

class FakeClassList {
  constructor(values = []) { this.values = new Set(values); }
  add(value) { this.values.add(value); }
  remove(value) { this.values.delete(value); }
  contains(value) { return this.values.has(value); }
}

class FakeElement {
  constructor(id, { tabIndex = -1, parent = null } = {}) {
    this.id = id;
    this.tabIndex = tabIndex;
    this.parentElement = parent;
    this.isConnected = true;
    this.disabled = false;
    this.inert = false;
    this.attrs = new Map();
    this.style = {};
    this.classList = new FakeClassList();
    this.listeners = {};
    this.dataset = {};
    this.children = [];
    this._innerHTML = '';
    this.viewerOpenDuringReplacement = null;
    this.scrollTop = 0;
  }
  set innerHTML(value) {
    this.viewerOpenDuringReplacement = lb.classList.contains('open');
    for (const child of this.children) child.isConnected = false;
    this.children = [];
    this._innerHTML = value;
  }
  get innerHTML() { return this._innerHTML; }
  setAttribute(name, value) {
    this.attrs.set(name, String(value));
    if (name === 'tabindex') this.tabIndex = Number(value);
    if (name === 'inert') this.inert = true;
  }
  removeAttribute(name) {
    this.attrs.delete(name);
    if (name === 'src') this.src = '';
    if (name === 'inert') this.inert = false;
  }
  getAttribute(name) { return this.attrs.get(name) ?? null; }
  hasAttribute(name) { return this.attrs.has(name); }
  addEventListener(type, handler) { (this.listeners[type] ||= []).push(handler); }
  querySelectorAll() { return []; }
  getClientRects() { return this.isConnected ? [{}] : []; }
  getBoundingClientRect() {
    return { left: 0, top: 0, width: this.clientWidth || 0, height: this.clientHeight || 0 };
  }
  contains(node) { return node === this || node.parentElement === this; }
  focus(options) {
    if (!this.isConnected || this.inert || this.tabIndex < 0) return;
    this.lastFocusOptions = options;
    document.activeElement = this;
  }
  setPointerCapture() {}
}

class FakeImage extends FakeElement {
  constructor(id, parent) {
    super(id, { tabIndex: 0, parent });
    this.currentSrc = `https://cdn.example/${id}.jpg`;
    this.src = this.currentSrc;
    this.alt = '图片说明';
    this.complete = true;
    this.naturalWidth = 640;
    this.offsetWidth = 300;
    this.offsetHeight = 400;
  }
  closest(selector) { return selector === 'img' ? this : null; }
}

global.MutationObserver = class { constructor() {} observe() {} };
global.HTMLImageElement = FakeImage;
const overlay = new FakeElement('overlay');
overlay.classList.add('open');
overlay.dataset.articleId = '1';
const articleWrap = new FakeElement('articleWrap', { parent: overlay });
const trigger = new FakeImage('old-article-image', articleWrap);
articleWrap.children = [trigger];
const backButton = new FakeElement('backBtn', { tabIndex: 0, parent: overlay });
const lb = new FakeElement('lb');
lb.setAttribute('aria-hidden', 'true');
lb.setAttribute('inert', '');
const closeButton = new FakeElement('lbCloseBtn', { tabIndex: 0, parent: lb });
const lbStage = new FakeElement('lbStage', { parent: lb });
lbStage.clientWidth = 320;
lbStage.clientHeight = 640;
const lbImg = new FakeImage('lbImg', lbStage);
const favBtn = new FakeElement('favBtn');
favBtn.title = '';
const elements = { overlay, articleWrap, backBtn: backButton, lb, lbCloseBtn: closeButton, lbStage, lbImg, favBtn };
global.document = {
  body: { style: {} },
  activeElement: trigger,
  getElementById(id) { return elements[id]; },
  addEventListener() {},
};

let scrollLockCount = 1;
global.lockBodyScroll = () => { scrollLockCount += 1; };
global.unlockBodyScroll = () => { scrollLockCount = Math.max(0, scrollLockCount - 1); };
global.lockArticleBackground = () => { scrollLockCount += 1; };
global.cancelViewBoundRefreshWork = () => {};
global.rememberArticleReturnState = () => {};
global.isRestrictedUser = () => true;
global.proxyImgSrc = value => value;
global.displayTitle = item => item.title;
global.feedSourceOf = item => item.source;
global.displaySourceForArticle = value => value;
global.badgeStyle = () => '';
global.sourceBadgeTitle = value => value;
global.sourceLabel = value => value;
global.formatTime = () => '';
global.esc = value => String(value);
global.syncArticleHistory = () => {};
global.fetchArticleDetail = () => new Promise(() => {});
global.autoDisplaySummary = () => {};
global.authToken = null;
global.userAutoSettings = null;
global.activeArticleRequestId = 0;
global.markArticleOpen = () => {};
global.news = [
  { id: 1, title: '旧文章', source: 'test', date: '2026-07-26', time: '12:00', thumb: '' },
  { id: 2, title: '新文章', source: 'test', date: '2026-07-27', time: '12:00', thumb: '' },
];

eval(process.argv[1] + '\nglobalThis.__lightboxGesture = lightboxGesture;');
eval(process.argv[2]);
openImageViewer(trigger);
const dispatchPointer = (type, pointerId, clientX, clientY) => {
  lbStage.listeners[type][0]({ currentTarget: lbStage, pointerId, clientX, clientY });
};
dispatchPointer('pointerdown', 1, 100, 300);
dispatchPointer('pointerdown', 2, 140, 300);
dispatchPointer('pointermove', 2, 180, 300);
assert.equal(__lightboxGesture.scale, 2);
assert.equal(__lightboxGesture.pointers.size, 2);
assert.equal(scrollLockCount, 2, 'article and viewer each hold one lock');

openArticle(2);
assert.equal(articleWrap.viewerOpenDuringReplacement, false, 'viewer closes before article DOM replacement');
assert.equal(lb.classList.contains('open'), false);
assert.equal(lb.getAttribute('aria-hidden'), 'true');
assert.equal(lb.inert, true);
assert.equal(__lightboxGesture.scale, 1);
assert.equal(__lightboxGesture.x, 0);
assert.equal(__lightboxGesture.y, 0);
assert.equal(__lightboxGesture.pointers.size, 0);
assert.equal(lbImg.style.transform, '');
assert.equal(lbImg.src, '');
assert.equal(document.activeElement, backButton, 'article switch leaves focus on a connected fallback');
assert.equal(scrollLockCount, 1, 'article switch retains only the existing article lock');
'''
    result = subprocess.run(
        ["node", "-e", runtime, viewer, open_article],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
