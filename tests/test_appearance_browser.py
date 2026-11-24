"""The colours in a real Chromium: the cascade of theme and color-blind blocks gives what tests/palette_math.py works
out, and the boxes drawn on a page keep colours that show on the white paper in dark mode. Skipped without
Playwright's Chromium."""

from __future__ import annotations

import pytest

pytest.importorskip("playwright")

import palette_math as pm  # noqa: E402
from controller_inbox import web  # noqa: E402
from liveserver import serving  # noqa: E402
from test_workspace import _open_page_tab, scan_to_check  # noqa: E402


def rgb(value: str) -> str:
    """A hex colour as getComputedStyle writes it."""
    r, g, b = (round(c * 255) for c in pm.hex_to_rgb(value))
    return f"rgb({r}, {g}, {b})"


def hex_of(value: str) -> str:
    """getComputedStyle's rgb(r, g, b) as a hex colour."""
    return "#" + "".join(f"{int(part):02x}" for part in value[value.index("(") + 1 : value.index(")")].split(",")[:3])


TOKENS = """names => { const s = getComputedStyle(document.documentElement);
  return Object.fromEntries(names.map((n) => [n, s.getPropertyValue(n).trim().toLowerCase()])); }"""
LOOK = """([theme, colors]) => { const root = document.documentElement;
  if (theme) root.dataset.theme = theme; else delete root.dataset.theme;
  if (colors === 'color-blind') root.dataset.colors = colors; else delete root.dataset.colors; }"""


def _sheet(page, base: str, path: str) -> None:
    """A bare page with only this stylesheet, so no script changes <html> while the test does."""
    page.set_content(f'<!DOCTYPE html><html lang="en"><head><link rel="stylesheet" href="{base}{path}"></head><body></body></html>')
    page.wait_for_function("() => getComputedStyle(document.documentElement).getPropertyValue('--danger').trim() !== ''")


def test_the_python_cascade_is_the_browsers(settings, store, page):
    """For each theme of the computer, theme chosen in the workspace and colors, the tokens Chromium computes are the
    ones palette_math composes (which the contrast and distance tests check)."""
    names = ["--imp-critical", "--imp-high-fill", "--ok", "--danger", "--paper-check", "--toast-error"]
    with serving(web.create_app(settings, store)) as base:
        _sheet(page, base, "/static/ui/app.css")
        wrong = {}
        for system in ("light", "dark"):
            page.emulate_media(color_scheme=system)
            for chosen in ("", "light", "dark"):
                for colors in ("standard", "color-blind"):
                    page.evaluate(LOOK, [chosen, colors])
                    shown = page.evaluate(TOKENS, names)
                    want = pm.workspace(chosen or system, colors)
                    if shown != {name: want[name] for name in names}:
                        wrong[(system, chosen, colors)] = shown
        _sheet(page, base, "/static/app.css")
        for colors in ("standard", "color-blind"):
            page.evaluate(LOOK, ["", colors])
            shown = page.evaluate(TOKENS, ["--imp-critical", "--imp-high", "--danger", "--green"])
            want = pm.classic(colors)
            if shown != {name: want[name] for name in shown}:
                wrong[("classic", colors)] = shown
    assert not wrong, wrong


BOXES = """() => {
  const style = (sel) => { const e = document.querySelector(sel); return e && getComputedStyle(e); };
  const check = style('.pv-box.check'), differs = style('.pv-box.differs'), ok = style('.pv-box.ok');
  return { check: check.borderTopColor, checkStyle: check.borderTopStyle, differs: differs.borderTopColor,
           differsRing: differs.boxShadow, ok: ok.borderTopColor, okStyle: ok.borderTopStyle,
           sheet: style('.pv-sheet').backgroundColor };
}"""


@pytest.mark.parametrize("colors", ["standard", "color-blind"])
def test_the_boxes_on_a_page_show_on_its_white_paper_in_dark_mode(settings, store, page, monkeypatch, colors):
    """The page is white paper in both themes, but its boxes took the dark theme's pastel colours: an amber box was
    1.95:1 on the white page. They keep the light colours, and an amber box has a dashed edge and a red one a ring."""
    from controller_inbox import appearance

    email = scan_to_check(settings, store, monkeypatch)
    appearance.save(store, colors=colors)
    page.emulate_media(color_scheme="dark")
    with serving(web.create_app(settings, store)) as base:
        _open_page_tab(page, base, email)
        page.wait_for_selector(".pv-box.check")
        shown = page.evaluate(BOXES)
    assert shown["sheet"] == "rgb(255, 255, 255)"
    seen = {name: round(pm.contrast(hex_of(shown[name]), "#ffffff"), 2) for name in ("check", "differs", "ok")}
    assert all(ratio >= 3 for ratio in seen.values()), seen
    paper = pm.workspace("light", colors)
    assert shown["check"] == rgb(paper["--paper-check"]) and shown["differs"] == rgb(paper["--paper-differs"])
    assert shown["ok"] == rgb(paper["--paper-ok"])
    assert shown["checkStyle"] == "dashed" and shown["okStyle"] == "solid"
    assert rgb(paper["--paper-differs"]) in shown["differsRing"]


# The two settings ----------------------------------------------------------------------------------------------------

EDGE = """() => { const row = document.querySelector('.items .item.imp-critical:not(.is-locked)');
  return row ? getComputedStyle(row, '::before').backgroundColor : 'missing'; }"""
TOKEN = "name => getComputedStyle(document.documentElement).getPropertyValue(name).trim().toLowerCase()"
# How the theme is chosen: the computer's, or the workspace's own (saved in this browser as closedesk-theme).
THEMES = {"light": ("light", ""), "dark from the computer": ("dark", ""), "dark chosen here": ("light", "dark")}


def _context(browser, system: str, chosen: str = ""):
    context = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme=system)
    context.set_default_timeout(15000)
    if chosen:
        context.add_init_script(f"try {{ localStorage.setItem('closedesk-theme', '{chosen}'); }} catch (e) {{}}")
    return context


@pytest.mark.parametrize("how", list(THEMES))
def test_the_workspace_box_changes_the_colors_at_once_and_the_classic_pages_too(browser, settings, loaded, how):
    system, chosen = THEMES[how]
    theme = chosen or system
    standard, blind = pm.workspace(theme, "standard"), pm.workspace(theme, "color-blind")
    context = _context(browser, system, chosen)
    page = context.new_page()
    with serving(web.create_app(settings, loaded)) as base:
        page.goto(f"{base}/app/all")
        page.wait_for_selector(".items .item.imp-critical")
        assert page.evaluate(EDGE) == rgb(standard["--imp-critical"])
        page.evaluate("() => { window.notReloaded = true; }")
        page.click(".rail-item[data-id='settings']")
        page.check("#set-colors")
        page.wait_for_selector("html[data-colors='color-blind']")
        page.wait_for_selector("#set-appearance [data-status='colors'].ok")
        assert "now look this way" in page.inner_text("#set-appearance [data-status='colors']")
        assert page.is_checked("#set-colors") and not page.is_checked("#set-importance-words")
        page.click(".rail-item[data-id='all']")
        page.wait_for_selector(".items .item.imp-critical")
        assert page.evaluate(EDGE) == rgb(blind["--imp-critical"]), "the list follows without a reload"
        assert page.evaluate("() => window.notReloaded === true")
        page.reload()
        page.wait_for_selector(".items .item.imp-critical")
        assert page.evaluate(EDGE) == rgb(blind["--imp-critical"]), "and stays after one"
        classic = context.new_page()
        classic.goto(f"{base}/inbox")
        assert classic.evaluate(TOKEN, "--imp-critical") == pm.classic("color-blind")["--imp-critical"]
        classic.close()
        # Unticked, the standard colors come back.
        page.click(".rail-item[data-id='settings']")
        page.uncheck("#set-colors")
        page.wait_for_selector("html:not([data-colors])")
        page.click(".rail-item[data-id='all']")
        page.wait_for_selector(".items .item.imp-critical")
        assert page.evaluate(EDGE) == rgb(standard["--imp-critical"])
    context.close()


def test_a_tab_left_open_follows_a_change_made_on_the_classic_page(browser, settings, loaded):
    context = _context(browser, "light")
    page = context.new_page()
    page.clock.install()
    with serving(web.create_app(settings, loaded)) as base:
        page.goto(f"{base}/app/all")
        page.wait_for_selector(".items .item.imp-critical")
        other = context.new_page()
        other.goto(f"{base}/settings")
        other.evaluate("() => fetch('/settings/colors', { method: 'POST', body: new URLSearchParams({ colors: 'color-blind', importance_words: '1' }) })")
        other.close()
        page.clock.run_for(16000)  # the next status check
        page.wait_for_selector("html[data-colors='color-blind'][data-importance-words='on']")
    context.close()


def test_the_colors_are_on_the_page_before_any_script_runs(browser, settings, loaded):
    """No flash of the other colors, and nothing kept in the browser: the server writes them on <html>. Here the
    workspace's script never loads and the browser keeps nothing (as in a private window)."""
    from controller_inbox import appearance

    appearance.save(loaded, colors="color-blind", importance_words=True)
    context = browser.new_context()
    context.add_init_script("Object.defineProperty(window, 'localStorage', { get() { throw new Error('no storage'); } });")
    context.route("**/main.js*", lambda route: route.abort())
    page = context.new_page()
    with serving(web.create_app(settings, loaded)) as base:
        for path, ui in (("/app", "workspace"), ("/", "classic"), ("/settings", "classic")):
            page.goto(f"{base}{path}", wait_until="domcontentloaded")
            assert page.evaluate("() => [document.documentElement.dataset.colors, document.documentElement.dataset.importanceWords]") == ["color-blind", "on"], path
            want = pm.workspace("light", "color-blind") if ui == "workspace" else pm.classic("color-blind")
            assert page.evaluate(TOKEN, "--imp-critical") == want["--imp-critical"], path
    context.close()


# The importance in words on each row ---------------------------------------------------------------------------------

LEVELS = """() => [...document.querySelectorAll('.items .item.kind-mail, .items .item.kind-focus')].map((li) => {
  const level = ['critical', 'high', 'medium', 'low'].find((k) => li.classList.contains('imp-' + k)) || '';
  const tag = li.querySelector('.item-level');
  const shown = tag && getComputedStyle(tag).display !== 'none' && tag.getBoundingClientRect().width > 0;
  return { key: li.dataset.key, level, text: tag ? tag.textContent : null, shown, classes: tag ? tag.className : '',
           background: tag ? getComputedStyle(tag).backgroundColor : '', locked: li.classList.contains('is-locked'),
           held: Boolean(li.querySelector('.chip-danger')) };
})"""
WORDS = {"critical": "Critical", "high": "High", "medium": "Medium", "low": "Low"}


def test_the_importance_shows_in_words_on_each_email_when_asked(browser, settings, loaded):
    context = _context(browser, "light")
    page = context.new_page()
    with serving(web.create_app(settings, loaded)) as base:
        page.goto(f"{base}/app/settings")
        page.evaluate("() => { window.notReloaded = true; }")
        page.check("#set-importance-words")
        page.wait_for_selector("html[data-importance-words='on']")
        grey = page.evaluate("() => { const t = document.createElement('span'); t.className = 'tag'; document.body.append(t); const c = getComputedStyle(t).backgroundColor; t.remove(); return c; }")
        seen_levels = set()
        for nav, path in (("today", "/app"), ("folder:important", "/app/folder/important"), ("all", "/app/all")):
            page.click(f".rail-item[data-id='{nav}']")
            page.wait_for_url(f"**{path}")
            page.wait_for_selector(".items .item")
            rows = page.evaluate(LEVELS)
            assert rows, path
            for row in rows:
                assert row["level"], row
                assert row["shown"] and row["text"] == WORDS[row["level"]], (path, row)
                if row["level"] in ("medium", "low"):
                    assert row["background"] == grey, ("Medium and Low are the quiet grey tag", row)
                else:
                    assert f"tag-prio-{row['level']}" in row["classes"]
                if row["locked"]:
                    assert row["held"], ("a held email still says it is held", row)
                seen_levels.add(row["level"])
        assert {"critical", "high", "medium"} <= seen_levels, seen_levels
        assert page.evaluate("() => window.notReloaded === true")
        # Unticked: the words go, the colored edge stays.
        page.click(".rail-item[data-id='settings']")
        page.uncheck("#set-importance-words")
        page.wait_for_selector("html:not([data-importance-words])")
        page.click(".rail-item[data-id='all']")
        page.wait_for_selector(".items .item")
        assert not [row for row in page.evaluate(LEVELS) if row["shown"]]
        assert page.evaluate("() => window.notReloaded === true")
    context.close()


# The longest words Today can show on one row, with a 7-figure amount; then whether anything in a row's first line
# runs past its text box (on its right, the done button's room).
FIT = """() => {
  for (const li of document.querySelectorAll('.items .item.kind-focus')) {
    const kind = li.querySelector('.item-row1 .tag:not(.item-level)');
    if (kind) kind.textContent = 'New · needs a look';
    const level = li.querySelector('.item-level');
    if (level) level.textContent = 'Critical';
    const amount = li.querySelector('.item-amount');
    if (amount) amount.textContent = '$1,234,567.89';
    else { const a = document.createElement('span'); a.className = 'item-amount'; a.textContent = '$1,234,567.89'; li.querySelector('.item-row1').append(a); }
  }
  const out = [];
  for (const li of document.querySelectorAll('.items .item')) {
    const main = li.querySelector('.item-main');
    const right = main.getBoundingClientRect().right - parseFloat(getComputedStyle(main).paddingRight);
    for (const el of li.querySelectorAll('.item-row1 > *')) {
      const r = el.getBoundingClientRect();
      if (r.right > right + 0.5) out.push({ row: li.dataset.key, el: el.className, over: Math.round(r.right - right) });
    }
    const from = li.querySelector('.item-from');
    if (from && from.getBoundingClientRect().width <= 0) out.push({ row: li.dataset.key, el: 'item-from squeezed out' });
  }
  return out;
}"""


@pytest.mark.parametrize("width", [1280, 1100, 390, 320])
def test_the_words_fit_each_row_at_every_width(browser, settings, loaded, width):
    from controller_inbox import appearance

    appearance.save(loaded, importance_words=True)
    context = browser.new_context(viewport={"width": width, "height": 900})
    page = context.new_page()
    with serving(web.create_app(settings, loaded)) as base:
        problems = {}
        for path in ("/app", "/app/folder/important", "/app/all"):
            page.goto(f"{base}{path}")
            page.wait_for_selector(".items .item .item-level")
            problems[path] = page.evaluate(FIT)
    context.close()
    assert problems == {path: [] for path in problems}


ROW_EDGE = """() => { const row = document.querySelector('table.mail tr.critical');
  return row ? getComputedStyle(row).boxShadow : 'missing'; }"""


def test_the_classic_setup_page_ticks_both_and_the_lists_follow(browser, settings, loaded):
    context = _context(browser, "light")
    page = context.new_page()
    with serving(web.create_app(settings, loaded)) as base:
        page.goto(f"{base}/inbox")
        assert rgb(pm.classic("standard")["--imp-critical"]) in page.evaluate(ROW_EDGE)
        page.goto(f"{base}/settings")
        page.check("#colors input[name='colors']")
        page.check("#colors input[name='importance_words']")
        page.click("#colors button[type='submit']")
        page.wait_for_url("**/settings?notice=colors#colors")
        assert "now look this way" in page.inner_text(".notice")
        assert page.evaluate("() => document.documentElement.dataset.colors") == "color-blind"
        assert page.is_checked("#colors input[name='colors']") and page.is_checked("#colors input[name='importance_words']")
        # A ticked box's choice is outlined, as the inbox profile's is.
        outline = page.evaluate("() => getComputedStyle(document.querySelector('#colors .choice')).borderTopColor")
        assert outline == rgb(pm.classic()["--navy"])
        page.goto(f"{base}/inbox")
        assert rgb(pm.classic("color-blind")["--imp-critical"]) in page.evaluate(ROW_EDGE)
    context.close()
