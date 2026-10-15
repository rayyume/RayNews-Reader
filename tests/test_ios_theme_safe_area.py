"""The iOS status area must use the app theme, even when iOS uses light mode."""

import pytest

from test_frontend_refresh_behavior import run_node, source_between


EARLY_THEME = source_between("<script>\n(function()", "</script>").split("<script>\n", 1)[1]
THEME_FUNCTIONS = source_between("function resolveThemePreference(", "// ═══ Auth State")


@pytest.mark.parametrize(
    ("saved_theme", "expected_background", "expected_status_style"),
    [
        ("dark", "#0a0a0c", "black-translucent"),
        ("light", "#f5f7fb", "default"),
    ],
)
def test_first_paint_status_area_follows_saved_theme_despite_light_ios(
    saved_theme, expected_background, expected_status_style
):
    run_node(EARLY_THEME + THEME_FUNCTIONS, f"""
assert.equal(context.document.documentElement.dataset.theme, '{saved_theme}');
assert.equal(context.document.documentElement.style.colorScheme, '{saved_theme}');
assert.equal(context.metas.themeColorMeta.content, '{expected_background}');
assert.equal(context.metas.appleStatusBarMeta.content, '{expected_status_style}');
""", f"""
context.storage = new Map([['raynews_theme', '{saved_theme}']]);
context.localStorage = {{
  getItem: key => context.storage.get(key),
  setItem: (key, value) => context.storage.set(key, value),
}};
context.window = {{matchMedia: () => ({{matches:true}})}};
context.metas = Object.fromEntries(['themeColorMeta','appleStatusBarMeta','themePreference']
  .map(id => [id, {{content:'',value:'',setAttribute(name,value){{this[name]=value;}}}}]));
context.document = {{
  documentElement: {{dataset:{{}},style:{{}}}},
  getElementById: id => context.metas[id],
}};
""")


def test_runtime_theme_switch_updates_status_area_without_using_ios_mode():
    run_node(THEME_FUNCTIONS, """
context.applyThemePreference('dark');
assert.equal(context.document.documentElement.dataset.theme, 'dark');
assert.equal(context.document.documentElement.style.colorScheme, 'dark');
assert.equal(context.metas.themeColorMeta.content, '#0a0a0c');
assert.equal(context.metas.appleStatusBarMeta.content, 'black-translucent');
context.applyThemePreference('light');
assert.equal(context.metas.themeColorMeta.content, '#f5f7fb');
assert.equal(context.metas.appleStatusBarMeta.content, 'default');
""", """
context.localStorage = {setItem() {}};
context.window = {matchMedia: () => ({matches:true})};
context.metas = Object.fromEntries(['themeColorMeta','appleStatusBarMeta','themePreference']
  .map(id => [id, {content:'',value:'',setAttribute(name,value){this[name]=value;}}]));
context.document = {
  documentElement: {dataset:{},style:{}},
  getElementById: id => context.metas[id],
};
""")


def test_viewport_and_root_cover_the_status_bar_safe_area():
    from test_frontend_refresh_behavior import HTML

    assert "viewport-fit=cover" in HTML
    assert "html{font-size:16px;scroll-behavior:smooth;-webkit-font-smoothing:antialiased;max-width:100%;overflow-x:hidden;background:var(--bg)}" in HTML
    assert "padding:env(safe-area-inset-top,0px)" in HTML
