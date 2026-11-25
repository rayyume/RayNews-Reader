"""Check document theming; the native iOS status bar needs device validation."""

import json
from urllib.parse import urljoin

import pytest

from test_frontend_refresh_behavior import HTML, ROOT, run_node, source_between


EARLY_THEME = source_between("<script>\n(function()", "</script>").split("<script>\n", 1)[1]
THEME_FUNCTIONS = source_between("function resolveThemePreference(", "// ═══ Auth State")


def test_pwa_declares_its_existing_home_identity_on_each_deployment_origin():
    manifest = json.loads((ROOT / "frontend" / "manifest.json").read_text(encoding="utf-8"))
    assert '<link rel="manifest" href="/manifest.json">' in HTML
    assert f'<meta name="apple-mobile-web-app-title" content="{manifest["short_name"]}">' in HTML
    for origin in ("https://news.example.test/", "https://another.example.test/"):
        start_url = urljoin(origin, manifest["start_url"])
        assert urljoin(start_url, manifest["id"]) == start_url == origin
        assert urljoin(origin, manifest["scope"]) == origin


@pytest.mark.parametrize(
    ("saved_theme", "expected_background"),
    [
        ("dark", "#0a0a0c"),
        ("light", "#f5f7fb"),
    ],
)
def test_first_paint_document_theme_follows_saved_preference_despite_light_ios(
    saved_theme, expected_background
):
    run_node(EARLY_THEME + THEME_FUNCTIONS, f"""
assert.equal(context.document.documentElement.dataset.theme, '{saved_theme}');
assert.equal(context.document.documentElement.style.colorScheme, '{saved_theme}');
assert.equal(context.metas.themeColorMeta.content, '{expected_background}');
""", f"""
context.storage = new Map([['raynews_theme', '{saved_theme}']]);
context.localStorage = {{
  getItem: key => context.storage.get(key),
  setItem: (key, value) => context.storage.set(key, value),
}};
context.window = {{matchMedia: () => ({{matches:true}})}};
context.metas = Object.fromEntries(['themeColorMeta','themePreference']
  .map(id => [id, {{content:'',value:'',setAttribute(name,value){{this[name]=value;}}}}]));
context.document = {{
  documentElement: {{dataset:{{}},style:{{}}}},
  getElementById: id => context.metas[id],
}};
""")


def test_runtime_theme_switch_updates_document_and_theme_color():
    run_node(THEME_FUNCTIONS, """
context.applyThemePreference('dark');
assert.equal(context.document.documentElement.dataset.theme, 'dark');
assert.equal(context.document.documentElement.style.colorScheme, 'dark');
assert.equal(context.metas.themeColorMeta.content, '#0a0a0c');
context.applyThemePreference('light');
assert.equal(context.metas.themeColorMeta.content, '#f5f7fb');
""", """
context.localStorage = {setItem() {}};
context.window = {matchMedia: () => ({matches:true})};
context.metas = Object.fromEntries(['themeColorMeta','themePreference']
  .map(id => [id, {content:'',value:'',setAttribute(name,value){this[name]=value;}}]));
context.document = {
  documentElement: {dataset:{},style:{}},
  getElementById: id => context.metas[id],
};
""")


def test_viewport_and_root_prepare_translucent_status_bar_safe_area():
    from test_frontend_refresh_behavior import HTML

    assert "viewport-fit=cover" in HTML
    assert '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">' in HTML
    assert "appleStatusBarMeta" not in HTML
    assert "html{font-size:16px;scroll-behavior:auto;-webkit-font-smoothing:antialiased;max-width:100%;overflow-x:hidden;background:var(--bg)}" in HTML
    assert "padding:env(safe-area-inset-top,0px)" in HTML


def test_ios_standalone_headers_use_a_solid_safe_area():
    from test_frontend_refresh_behavior import HTML

    assert ".header{position:fixed;top:0;" in HTML
    assert "background:var(--header-bg);backdrop-filter:blur(20px)" in HTML
    assert ".topcat-bar{position:fixed;" in HTML
    assert ".overlay-header{position:sticky;" in HTML
    assert ".search-header{flex-shrink:0;" in HTML
    assert ".ios-standalone :is(.header,.topcat-bar,.overlay-header,.search-header){background:var(--bg);backdrop-filter:none;-webkit-backdrop-filter:none}" in HTML
