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
    1.95:1 on the white page. They keep the light colours, and an amber box has a dotted edge and a red one a ring."""
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
    assert shown["checkStyle"] == "dotted" and shown["okStyle"] == "solid"
    assert rgb(paper["--paper-differs"]) in shown["differsRing"]


EDGES = """() => {
  const edge = (sel) => { const e = document.querySelector(sel); return e ? getComputedStyle(e).borderTopStyle : null; };
  // The outline drawn while marking a table is only there while dragging: one made the same way.
  const rubber = document.createElement('div'); rubber.className = 'pv-rubber'; document.querySelector('.pv-sheet').append(rubber);
  const out = { check: edge('.pv-box.check'), swatch: edge('.pv-swatch.check'), table: edge('.pv-tbl'),
                chip: edge('.pv-chip'), rubber: edge('.pv-rubber'), about: document.querySelector('.pv-about').textContent };
  rubber.remove();
  return out;
}"""


def test_an_amber_box_on_a_page_isnt_drawn_the_way_a_table_is(settings, store, page, monkeypatch):
    """Above the page it says "Dashed outlines are tables: click one to see it as a table", and the tables, their
    chips and the outline drawn to mark one are dashed; but the amber boxes were given a dashed edge too, to tell them
    from the green ones by more than colour. Someone who can't tell the amber from the blue was told an amber box was a
    table."""
    email = scan_to_check(settings, store, monkeypatch)
    with serving(web.create_app(settings, store)) as base:
        _open_page_tab(page, base, email)
        page.wait_for_selector(".pv-box.check")
        page.wait_for_selector(".pv-tbl")
        shown = page.evaluate(EDGES)
    assert "Dashed outlines are tables" in shown["about"], shown["about"]
    assert shown["table"] == shown["chip"] == shown["rubber"] == "dashed", shown
    assert shown["check"] not in ("dashed", "solid") and shown["swatch"] == shown["check"], shown


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


def test_ticking_a_box_with_the_keyboard_keeps_the_focus_on_it(settings, store, page):
    """Each box saves the moment it is ticked. It was disabled while saving, which sent the focus to the page itself:
    the next Tab went back to the top of Settings, and a screen reader lost its place. The line saying "Saved" was a
    new element each time, which a screen reader often doesn't read out; it is the same one, with new words."""
    from controller_inbox import appearance

    with serving(web.create_app(settings, store)) as base:
        page.goto(f"{base}/app/settings")
        page.wait_for_selector("#set-colors")
        page.evaluate("() => { document.querySelector('#set-appearance [data-status=colors]').kept = true; }")
        for box, after, want in (("set-colors", "set-importance-words", {"colors": "color-blind", "importance_words": False}),
                                 ("set-importance-words", None, {"colors": "color-blind", "importance_words": True})):
            page.focus(f"#{box}")
            page.keyboard.press("Space")
            page.wait_for_function("box => document.querySelector('#' + box).checked && document.querySelector('#set-appearance [data-status=colors].ok')", arg=box)
            page.wait_for_function("() => !document.querySelector('#set-appearance [aria-busy]')")
            assert page.evaluate("() => document.activeElement.id") == box
            assert page.evaluate("() => document.querySelector('#set-appearance [data-status=colors]').kept === true"), "the same line"
            if after:
                page.keyboard.press("Tab")
                assert page.evaluate("() => document.activeElement.id") == after
            assert appearance.current(store) == want
            page.evaluate("() => { document.querySelector('#set-appearance [data-status=colors]').className = 'set-status'; }")


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
  const reason = li.querySelector('.item-row1 .tag:not(.item-level)');
  return { key: li.dataset.key, level, text: tag ? tag.textContent : null, shown, classes: tag ? tag.className : '',
           background: tag ? getComputedStyle(tag).backgroundColor : '', outline: tag ? getComputedStyle(tag).boxShadow : '',
           color: tag ? getComputedStyle(tag).color : '', reason: reason ? getComputedStyle(reason).backgroundColor : null,
           locked: li.classList.contains('is-locked'), held: (li.querySelector('.chip-danger') || {}).textContent || '' };
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
        grey = page.evaluate("() => { const t = document.createElement('span'); t.className = 'tag'; document.body.append(t); const c = getComputedStyle(t).color; t.remove(); return c; }")
        seen_levels, held = set(), set()
        for nav, path in (("today", "/app"), ("folder:important", "/app/folder/important"), ("all", "/app/all")):
            page.click(f".rail-item[data-id='{nav}']")
            page.wait_for_url(f"**{path}")
            page.wait_for_selector(".items .item")
            rows = page.evaluate(LEVELS)
            assert rows, path
            for row in rows:
                assert row["level"], row
                if row["locked"]:
                    # Held as possible payment fraud: its edge is the held one, so its word says that.
                    assert row["shown"] and row["text"] == "Held for fraud" and "tag-held" in row["classes"], (path, row)
                    assert row["held"], ("a held email still has its held chip", row)
                    held.add(row["held"])
                    continue
                assert row["shown"] and row["text"] == WORDS[row["level"]], (path, row)
                if row["level"] in ("medium", "low"):
                    assert row["color"] == grey, ("Medium and Low are in the quiet grey", row)
                else:
                    assert f"tag-prio-{row['level']}" in row["classes"]
                # Outlined on the row, where the reason a row is in Today ("Due today", "Overdue") is a filled tag: side
                # by side, "how important" and "why it is here" don't look like the same kind of label.
                assert row["background"] == "rgba(0, 0, 0, 0)" and "inset" in row["outline"], ("outlined", row)
                if row["reason"] is not None:
                    assert row["reason"] != "rgba(0, 0, 0, 0)", ("the reason stays a filled tag", row)
                seen_levels.add(row["level"])
        assert {"critical", "high", "medium"} <= seen_levels, seen_levels
        assert held, "the sample's held emails"
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


def test_a_held_email_with_a_file_says_it_is_held_for_fraud_in_words(browser, settings, store, mail):
    """A bank-change scam with an invoice attached, the usual kind: its chip says "Files locked" (it has a file), and
    with the importance in words it said "Critical", on an edge the red of Critical. Nothing said held, or fraud."""
    from controller_inbox import appearance

    appearance.save(store, importance_words=True)
    context = _context(browser, "light")
    page = context.new_page()
    with serving(web.create_app(settings, store)) as base:
        for path in ("/app", "/app/folder/important", "/app/all"):
            page.goto(f"{base}{path}")
            page.wait_for_selector(".items .item")
            held = [row for row in page.evaluate(LEVELS) if row["locked"]]
            assert [(row["text"], row["shown"], row["held"]) for row in held] == [("Held for fraud", True, "Files locked")], (path, held)
    context.close()


CLASSIC_LEVELS = """() => [...document.querySelectorAll('.focus-item, .folder-card li:not(.muted)')].map((li) => {
  const pill = li.querySelector('.item-level');
  const shown = Boolean(pill) && getComputedStyle(pill).display !== 'none' && pill.getBoundingClientRect().width > 0;
  return { text: pill ? pill.textContent : null, shown, inside: !pill || pill.getBoundingClientRect().right <= li.getBoundingClientRect().right };
})"""


@pytest.mark.parametrize("width", [1280, 390])
def test_classic_today_writes_the_importance_on_each_email_when_asked(browser, settings, loaded, width):
    """Today, the classic home page, had no importance on its emails with the box ticked: only the Inbox and folder
    lists had it, in their Importance column."""
    from controller_inbox import appearance

    context = _context(browser, "light")
    page = context.new_page()
    page.set_viewport_size({"width": width, "height": 900})
    with serving(web.create_app(settings, loaded)) as base:
        page.goto(f"{base}/")
        rows = page.evaluate(CLASSIC_LEVELS)
        assert rows and not [row for row in rows if row["shown"]], rows
        appearance.save(loaded, importance_words=True)
        page.reload()
        rows = page.evaluate(CLASSIC_LEVELS)
        assert all(row["shown"] and row["text"] in WORDS.values() and row["inside"] for row in rows), rows
        assert {"Critical", "High"} <= {row["text"] for row in rows}
    context.close()


# The longest words Today can show on one row, with a 7-figure amount (a held email keeps its longer "Held for
# fraud"); then whether anything in a row's first line runs past its text box (on its right, the done button's room).
FIT = """() => {
  for (const li of document.querySelectorAll('.items .item.kind-focus')) {
    const kind = li.querySelector('.item-row1 .tag:not(.item-level)');
    if (kind) kind.textContent = 'New · needs a look';
    const level = li.querySelector('.item-level');
    if (level && !li.classList.contains('is-locked')) level.textContent = 'Critical';
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


IN_SIGHT = """sel => { const r = document.querySelector(sel).getBoundingClientRect();
  return r.top >= 0 && r.bottom <= window.innerHeight && r.width > 0; }"""


@pytest.mark.parametrize("width", [1280, 390])
def test_the_classic_setup_page_ticks_both_and_the_lists_follow(browser, settings, loaded, width):
    context = _context(browser, "light")
    page = context.new_page()
    page.set_viewport_size({"width": width, "height": 800 if width > 400 else 844})
    with serving(web.create_app(settings, loaded)) as base:
        page.goto(f"{base}/inbox")
        assert rgb(pm.classic("standard")["--imp-critical"]) in page.evaluate(ROW_EDGE)
        page.goto(f"{base}/settings")
        page.check("#colors input[name='colors']")
        page.check("#colors input[name='importance_words']")
        page.click("#colors button[type='submit']")
        page.wait_for_url("**/settings?notice=colors#colors")
        assert "now look this way" in page.inner_text("#colors .notice")
        # Said where the page goes back to, not at its top out of sight above it.
        assert page.evaluate(IN_SIGHT, "#colors .notice"), "Saved, in sight"
        # Only the importance words changed: said in sight too.
        page.uncheck("#colors input[name='importance_words']")
        page.click("#colors button[type='submit']")
        page.wait_for_url("**/settings?notice=importance-words#colors")
        assert "workspace's lists" in page.inner_text("#colors .notice") and page.evaluate(IN_SIGHT, "#colors .notice")
        page.check("#colors input[name='importance_words']")
        page.click("#colors button[type='submit']")
        page.wait_for_url("**/settings?notice=importance-words#colors")
        assert page.evaluate("() => document.documentElement.dataset.colors") == "color-blind"
        assert page.is_checked("#colors input[name='colors']") and page.is_checked("#colors input[name='importance_words']")
        # A ticked box's choice is outlined, as the inbox profile's is.
        outline = page.evaluate("() => getComputedStyle(document.querySelector('#colors .choice')).borderTopColor")
        assert outline == rgb(pm.classic()["--navy"])
        page.goto(f"{base}/inbox")
        assert rgb(pm.classic("color-blind")["--imp-critical"]) in page.evaluate(ROW_EDGE)
    context.close()


BOXES_LINE_UP = """() => [...document.querySelectorAll('#colors .choice')].map((el) => {
  const r = el.getBoundingClientRect(); return [Math.round(r.left), Math.round(r.right)]; })"""


@pytest.mark.parametrize("width", [1280, 390])
def test_the_classic_colors_boxes_line_up(browser, settings, loaded, width):
    """Setup → Colors drew each box to the width of its own words, so the two outlines ended in different places."""
    context = _context(browser, "light")
    page = context.new_page()
    page.set_viewport_size({"width": width, "height": 800})
    with serving(web.create_app(settings, loaded)) as base:
        page.goto(f"{base}/settings")
        edges = page.evaluate(BOXES_LINE_UP)
        assert len(edges) == 2 and edges[0] == edges[1], edges
        assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth"), "nothing wider than the screen"
    context.close()


COLORS_ON = "() => [document.documentElement.dataset.colors || null, document.documentElement.dataset.importanceWords || null]"


def test_back_after_changing_the_colors_shows_the_classic_pages_in_the_new_ones(browser, settings, loaded):
    """Back after Setup → Colors → Save showed the page before in the old colors: the classic pages were sent with
    nothing saying not to keep them, so the browser showed the copy it kept until the page was loaded again."""
    from controller_inbox import appearance

    appearance.save(loaded, colors="color-blind")
    context = _context(browser, "light")
    page = context.new_page()
    with serving(web.create_app(settings, loaded)) as base:
        page.goto(f"{base}/inbox")
        assert page.evaluate(COLORS_ON) == ["color-blind", None]
        page.click(".rail a[href='/settings']")
        page.wait_for_url("**/settings")
        page.uncheck("#colors input[name='colors']")
        page.click("#colors button[type='submit']")
        page.wait_for_url("**/settings?notice=colors#colors")
        page.go_back()
        page.go_back()
        page.wait_for_url("**/inbox")
        page.wait_for_selector("table.mail tr.critical")
        assert page.evaluate(COLORS_ON) == [None, None]
        assert rgb(pm.classic("standard")["--imp-critical"]) in page.evaluate(ROW_EDGE)
    context.close()


def test_a_classic_page_kept_open_follows_the_colors_when_shown_again(browser, settings, loaded):
    """A classic page the browser keeps as it was (Back, with the back/forward cache), or the chat in a window of its
    own, kept the colors it was sent with until it was loaded again. Shown again, it asks."""
    from controller_inbox import appearance

    context = _context(browser, "light")
    page = context.new_page()
    with serving(web.create_app(settings, loaded)) as base:
        page.goto(f"{base}/chat/window")
        page.wait_for_selector("#chat")
        assert page.evaluate(COLORS_ON) == [None, None]
        appearance.save(loaded, colors="color-blind", importance_words=True)
        page.evaluate("() => window.dispatchEvent(new Event('focus'))")  # the chat's window comes to the front
        page.wait_for_function(f"() => ({COLORS_ON})().join() === 'color-blind,on'")
        appearance.save(loaded, colors="standard", importance_words=False)
        page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pageshow', { persisted: true }))")
        page.wait_for_function(f"() => ({COLORS_ON})().join() === ','")
    context.close()


@pytest.mark.parametrize("changed", ["colors", "importance_words"])
def test_classic_setup_left_open_keeps_a_change_made_in_the_workspace(browser, settings, loaded, changed):
    """Classic Setup open in one tab, the workspace in another. A box ticked in the workspace turned the Setup tab's
    page to the new colors when it was shown again, but its own boxes still showed the old ones; ticking the other
    box there and pressing Save turned the workspace's choice off again, with "Saved" said."""
    from controller_inbox import appearance

    other = "importance_words" if changed == "colors" else "colors"
    box = {"colors": "#set-colors", "importance_words": "#set-importance-words"}
    context = _context(browser, "light")
    classic = context.new_page()
    with serving(web.create_app(settings, loaded)) as base:
        classic.goto(f"{base}/settings")
        assert not classic.is_checked(f"#colors input[name='{changed}']")
        workspace = context.new_page()
        workspace.goto(f"{base}/app/settings")
        workspace.check(box[changed])
        workspace.wait_for_selector("#set-appearance [data-status='colors'].ok")
        classic.bring_to_front()
        classic.evaluate("() => window.dispatchEvent(new Event('focus'))")  # its tab shown again
        attr = "colors" if changed == "colors" else "importanceWords"
        classic.wait_for_function(f"() => Boolean(document.documentElement.dataset.{attr})")
        classic.wait_for_function(f"() => document.querySelector(\"#colors input[name='{changed}']\").checked")
        classic.check(f"#colors input[name='{other}']")
        classic.click("#colors button[type='submit']")
        classic.wait_for_url("**/settings?notice=*")
        assert appearance.current(loaded) == {"colors": "color-blind", "importance_words": True}
        assert classic.is_checked("#colors input[name='colors']") and classic.is_checked("#colors input[name='importance_words']")

        # A box changed on the page and not saved yet stays as it was ticked, whatever changes elsewhere.
        classic.uncheck(f"#colors input[name='{changed}']")
        appearance.save(loaded, colors="standard", importance_words=False)
        classic.evaluate("() => window.dispatchEvent(new Event('focus'))")
        classic.wait_for_function(f"() => document.querySelector(\"#colors input[name='{other}']\").checked === false")
        assert not classic.is_checked(f"#colors input[name='{changed}']")
        appearance.save(loaded, colors="color-blind", importance_words=True)
        classic.evaluate("() => window.dispatchEvent(new Event('focus'))")
        classic.wait_for_function(f"() => document.querySelector(\"#colors input[name='{other}']\").checked === true")
        assert not classic.is_checked(f"#colors input[name='{changed}']"), "left as the person ticked it"
        classic.click("#colors button[type='submit']")
        classic.wait_for_url("**/settings?notice=*")
        want = {"colors": "color-blind", "importance_words": True} | {changed: "standard" if changed == "colors" else False}
        assert appearance.current(loaded) == want
    context.close()


# Shapes and signs as well as colour ----------------------------------------------------------------------------------

ALERT_PATH = "M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0zM12 9v4M12 17h.01"
TOAST = """async () => {
  const { toast } = await import('/static/ui/dom.js');
  toast('That conversation could not be deleted.', { tone: 'error', ms: 60000 });
  const box = document.querySelector('.toast.error');
  const sign = box.querySelector('.toast-text').previousElementSibling;
  return { sign: sign && sign.tagName.toLowerCase() === 'svg' ? sign.querySelector('path').getAttribute('d') : null,
           background: getComputedStyle(box).backgroundColor, color: getComputedStyle(box).color };
}"""


@pytest.mark.parametrize("how", list(THEMES))
@pytest.mark.parametrize("colors", ["standard", "color-blind"])
def test_an_error_message_has_a_warning_sign_and_is_red_in_every_theme(browser, settings, store, how, colors):
    """An error message differed from any other by its red alone, and in the dark theme not even that: the dark
    theme's light messages won over it."""
    from controller_inbox import appearance

    appearance.save(store, colors=colors)
    system, chosen = THEMES[how]
    context = _context(browser, system, chosen)
    page = context.new_page()
    with serving(web.create_app(settings, store)) as base:
        page.goto(f"{base}/app/settings")
        page.wait_for_selector("body.ready")
        shown = page.evaluate(TOAST)
    context.close()
    assert shown["sign"] == ALERT_PATH
    assert shown["background"] == rgb(pm.workspace(chosen or system, colors)["--toast-error"]) and shown["color"] == "rgb(255, 255, 255)"


def test_a_self_check_note_has_the_info_sign_and_a_problem_the_warning_sign(settings, store, page, monkeypatch):
    """A note ("No local model is running") and a problem had the same warning sign, told apart by colour alone."""
    from controller_inbox import doctor

    monkeypatch.setattr(doctor, "checks", lambda **_kw: [
        doctor.Check("ok", "Python 3.12"), doctor.Check("note", "No local model is running."), doctor.Check("problem", "Port 8765 is busy.", "Close it.")])
    with serving(web.create_app(settings, store)) as base:
        page.goto(f"{base}/app/settings#set-about")
        page.click("#set-about button:has-text('Check')")
        page.wait_for_selector("#set-about .set-check.is-problem")
        shown = page.evaluate("""async () => {
          const { icon } = await import('/static/ui/dom.js');
          const d = (name) => icon(name).querySelector('path').getAttribute('d');
          const signs = { [d('check')]: 'check', [d('info')]: 'info', [d('alert')]: 'alert' };
          return [...document.querySelectorAll('#set-about .set-checks li')].map((li) => [li.className, signs[li.querySelector('svg path').getAttribute('d')]]);
        }""")
    assert shown == [["set-check is-ok", "check"], ["set-check is-note", "info"], ["set-check is-problem", "alert"]]


DOTS = """() => {
  const make = (html) => { const box = document.createElement('div'); box.innerHTML = html; document.body.append(box); return box; };
  const shape = (el) => { const s = getComputedStyle(el); return { background: s.backgroundColor, shadow: s.boxShadow, radius: s.borderRadius }; };
  const dots = make('<span class="pv-dot ok"></span><span class="pv-dot check"></span><span class="pv-dot differs"></span>');
  const out = { ok: shape(dots.children[0]), check: shape(dots.children[1]), differs: shape(dots.children[2]),
                chat: shape(document.querySelector('#chat-btn .dot')), chatTitle: document.querySelector('#chat-btn').title };
  document.querySelector('#chat-btn').classList.add('live');
  out.chatLive = shape(document.querySelector('#chat-btn .dot'));
  return out;
}"""


def test_dots_differ_by_shape_as_well_as_colour(settings, store, page):
    """The key details' dots on a page, and the dot saying whether a local model is running, were told apart by
    colour alone."""
    with serving(web.create_app(settings, store)) as base:
        page.goto(f"{base}/app/settings")
        page.wait_for_function("() => document.querySelector('#chat-btn').title !== ''")  # after the first status check
        shown = page.evaluate(DOTS)
        page.goto(f"{base}/settings")
        fab = page.evaluate("""() => { const dot = document.querySelector('#chat-toggle .dot'); const s = getComputedStyle(dot);
          return { background: s.backgroundColor, shadow: s.boxShadow, title: document.querySelector('#chat-toggle').title }; }""")
    clear = "rgba(0, 0, 0, 0)"
    assert shown["ok"]["background"] != clear and shown["ok"]["radius"] == "50%"
    assert shown["check"]["background"] == clear and "inset" in shown["check"]["shadow"], "a ring"
    assert shown["differs"]["radius"] == "1px", "a square"
    # No model running: a hollow ring, and words on hover; running, a filled dot.
    assert shown["chat"]["background"] == clear and "inset" in shown["chat"]["shadow"]
    assert shown["chatTitle"] == "Ask CloseDesk: no local model running"
    assert shown["chatLive"]["background"] != clear and shown["chatLive"]["shadow"] == "none"
    assert fab["background"] == clear and "inset" in fab["shadow"] and fab["title"] == "Ask CloseDesk: no local model running"


@pytest.mark.parametrize("width", [1280, 390])
def test_ask_closedesk_keeps_its_name_at_every_width(settings, store, page, width):
    """On a narrow window the button shows only its icon, and the model's words on hover became its name: a screen
    reader said "No local model running, button" for the button that opens the chat, and the tooltip didn't say what
    the button does."""
    page.set_viewport_size({"width": width, "height": 844})
    with serving(web.create_app(settings, store)) as base:
        page.goto(f"{base}/app/settings")
        page.wait_for_function("() => document.querySelector('#chat-btn').title !== ''")  # after the first status check
        named = page.get_by_role("button", name="Ask CloseDesk", exact=True)
        assert named.count() == 1 and named.get_attribute("id") == "chat-btn"
        assert page.get_by_role("button", name="No local model running").count() == 0
        assert named.get_attribute("title").startswith("Ask CloseDesk"), "what the button does, on hover too"


MONTH_DOTS = """() => {
  const box = document.createElement('table'); box.className = 'cal-month';
  box.innerHTML = '<tr><td><button class="cal-chip"></button><button class="cal-chip all-day"></button><button class="cal-chip cancelled"></button></td></tr>';
  document.body.append(box);
  return [...box.querySelectorAll('.cal-chip')].map((el) => { const s = getComputedStyle(el); return { background: s.backgroundColor, shadow: s.boxShadow, radius: s.borderRadius }; });
}"""


def test_a_phones_month_marks_all_day_and_cancelled_meetings_by_shape(settings, store, page):
    page.set_viewport_size({"width": 390, "height": 844})
    with serving(web.create_app(settings, store)) as base:
        page.goto(f"{base}/app/settings")
        page.wait_for_selector("body.ready")
        meeting, all_day, cancelled = page.evaluate(MONTH_DOTS)
    assert meeting["radius"] == "50%" and all_day["radius"] == "2px", "all day: a square"
    assert cancelled["background"] == "rgba(0, 0, 0, 0)" and "inset" in cancelled["shadow"], "cancelled: a ring"


CHECKS = """() => [...document.querySelectorAll('#chat .msg-check li')].map((li) => ({
  text: li.innerText.trim(), warn: li.classList.contains('warn'), sign: getComputedStyle(li, '::before').content,
  color: getComputedStyle(li, '::before').color }))"""


def test_the_classic_chat_marks_a_figure_to_check_as_a_warning(settings, store, page):
    """The workspace showed a figure to check with the warning sign on amber; the classic chat showed it as any
    other check."""
    from test_check_warnings_browser import CORRECTED, NOT_FOUND, _saved

    chat_id = _saved(store)
    with serving(web.create_app(settings, store)) as base:
        page.goto(f"{base}/chat/window")
        page.evaluate("id => localStorage.setItem('closedesk-chat-id', id)", chat_id)
        page.reload()
        page.wait_for_selector("#chat .msg-check li")
        items = page.evaluate(CHECKS)
        box = page.evaluate("() => { const b = document.querySelector('#chat .msg-check'); return { warn: b.classList.contains('warn'), background: getComputedStyle(b).backgroundColor }; }")
    tokens = pm.classic()
    assert [(item["text"], item["warn"]) for item in items] == [(CORRECTED, False), (NOT_FOUND, True)]
    assert "⚠" in items[1]["sign"] and items[1]["color"] == rgb(tokens["--warn"])
    assert box["warn"] and box["background"] == rgb(tokens["--warn-soft"])


def test_ctrl_k_turns_the_colors_for_color_blindness_on_and_off(settings, store, page):
    from controller_inbox import appearance

    with serving(web.create_app(settings, store)) as base:
        page.goto(f"{base}/app/settings")
        page.wait_for_selector("body.ready")
        for want in ("Turn on colors for color blindness", "Turn off colors for color blindness"):
            page.keyboard.press("Control+k")
            page.fill(".palette input", "color blind")
            page.wait_for_selector(f".pal-item.on:has-text('{want}')")
            page.keyboard.press("Enter")
            on = want.startswith("Turn on")
            page.wait_for_function("on => (document.documentElement.dataset.colors === 'color-blind') === on", arg=on)
            # With no box in sight, it says which: on or off.
            said = page.wait_for_selector(".toast:has-text('Colors for color blindness')").inner_text()
            assert said.startswith(f"Colors for color blindness {'on' if on else 'off'}:") and "classic pages" in said, said
            page.evaluate("() => document.querySelectorAll('.toast').forEach((t) => t.remove())")
            # Settings, open beside it, says so.
            page.wait_for_function("on => { const box = document.querySelector('#set-colors'); return Boolean(box) && box.checked === on; }", arg=on)
            assert appearance.current(store)["colors"] == ("color-blind" if on else "standard")


@pytest.mark.parametrize("stopped", ["only the save fails", "nothing answers"])
def test_a_save_that_cant_reach_closedesk_says_so_in_plain_words(settings, store, page, stopped):
    """With CloseDesk not answering (stopped, say), the box and Ctrl K said the browser's own "Failed to fetch". With
    nothing answering at all, the page then asked for the settings again, which failed too, and that message took the
    save's place: under Model and status, out of sight, with nothing said beside the box."""
    from controller_inbox import appearance

    appearance.save(store, colors="color-blind")
    with serving(web.create_app(settings, store)) as base:
        page.goto(f"{base}/app/settings")
        page.wait_for_selector("#set-colors")
        page.route("**/api/settings/colors" if stopped == "only the save fails" else "**/api/**", lambda route: route.abort())
        line = "#set-appearance [data-status='colors']"
        for box in ("#set-colors", "#set-importance-words"):
            was = page.is_checked(box)
            page.evaluate("sel => { const l = document.querySelector(sel); l.className = 'set-status'; l.replaceChildren(); }", line)
            page.click(box)
            page.wait_for_selector(f"{line}.error")
            said = page.inner_text(line)
            assert "Failed to fetch" not in said and "CloseDesk isn't answering" in said, said
            page.wait_for_function("() => !document.querySelector('#set-appearance [aria-busy]')")
            assert page.is_checked(box) == was, "nothing was saved"
            assert page.inner_text("#set-model [data-status='model']") == "", "said beside the box, not elsewhere"
        assert appearance.current(store) == {"colors": "color-blind", "importance_words": False}
        page.keyboard.press("Control+k")
        page.fill(".palette input", "color blind")
        page.wait_for_selector(".pal-item.on:has-text('Turn off colors for color blindness')")
        page.keyboard.press("Enter")
        toast = page.wait_for_selector(".toast.error")
        assert "Failed to fetch" not in toast.inner_text() and "CloseDesk isn't answering" in toast.inner_text()


def test_a_save_whose_page_cant_be_read_again_says_both_under_the_box(settings, store, page):
    """Saved, and then the settings couldn't be read again: the message for that replaced "Saved" and went under
    Model and status, out of sight."""
    from controller_inbox import appearance

    with serving(web.create_app(settings, store)) as base:
        page.goto(f"{base}/app/settings")
        page.wait_for_selector("#set-colors")
        page.route("**/api/settings", lambda route: route.abort() if route.request.method == "GET" else route.continue_())
        page.click("#set-colors")
        line = "#set-appearance [data-status='colors']"
        page.wait_for_selector(f"{line}.error")
        said = page.inner_text(line)
        assert said.startswith(web.NOTICES["colors"]) and said.endswith(" CloseDesk isn't answering. If it has closed, double-click CloseDesk to start it again."), said
        assert page.is_checked("#set-colors") and appearance.current(store)["colors"] == "color-blind"
        assert page.inner_text("#set-model [data-status='model']") == ""


WATCH_LINE = """sel => { const line = document.querySelector(sel); window.watched = line; window.saidAgain = [];
  new MutationObserver((records) => { if (records.some((r) => r.addedNodes.length || r.type === 'characterData')) window.saidAgain.push(line.textContent); })
    .observe(line, { childList: true, subtree: true, characterData: true }); }"""


def test_saved_is_said_once_not_each_time_the_page_is_drawn_again(settings, store, page):
    """The line under a change stays the same element, so a screen reader reads out its new words. But each time the
    page was drawn again (a theme click, a run starting or ending in the background, a change made elsewhere) the
    same words were put in it again, and read out again: after a theme click, which is kept in this browser only, a
    screen reader said the colors were saved for both UIs."""
    import threading

    from controller_inbox import appearance

    app = web.create_app(settings, store)
    with serving(app) as base:
        page.goto(f"{base}/app/settings")
        page.wait_for_selector("#set-colors")
        page.check("#set-colors")
        line = "#set-appearance [data-status='colors']"
        page.wait_for_selector(f"{line}.ok")
        page.wait_for_function("() => !document.querySelector('#set-appearance [aria-busy]')")
        page.evaluate(WATCH_LINE, line)
        page.click("#set-appearance .seg:has-text('Dark')")
        page.wait_for_selector("#set-appearance .seg.on:has-text('Dark')")
        release = threading.Event()
        assert app.state.job.start(lambda progress: {"ingested": 0} if release.wait(20) else {})
        page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")  # asks for the job now
        page.wait_for_selector("#top[data-busy]")
        release.set()
        page.wait_for_selector("#top:not([data-busy])")
        appearance.save(store, importance_words=True)  # in another window
        page.evaluate("() => document.dispatchEvent(new Event('visibilitychange'))")
        page.wait_for_function("() => document.querySelector('#set-importance-words').checked")
        page.wait_for_timeout(300)
        assert page.evaluate("() => window.saidAgain") == []
        assert page.evaluate(f"() => document.querySelector(\"{line}\") === window.watched")
        assert web.NOTICES["colors"] in page.inner_text(line), "still there to read"


LONG_TOAST = """async (message) => {
  const { toast } = await import('/static/ui/dom.js');
  document.querySelectorAll('.toast').forEach((t) => t.remove());
  toast(message, { tone: 'error', ms: 60000 });
  const box = document.querySelector('.toast.error');
  const text = box.querySelector('.toast-text');
  const style = getComputedStyle(text);
  const line = parseFloat(style.lineHeight);
  const middle = (el) => { const r = el.getBoundingClientRect(); return (r.top + r.bottom) / 2; };
  const top = text.getBoundingClientRect().top + parseFloat(style.paddingTop);
  const height = text.getBoundingClientRect().height - parseFloat(style.paddingTop) - parseFloat(style.paddingBottom);
  return { width: box.getBoundingClientRect().width, lines: Math.round(height / line), firstLine: top + line / 2,
           sign: middle(box.querySelector(':scope > .ic')), close: middle(box.querySelector('.toast-close')) };
}"""


@pytest.mark.parametrize("width", [1280, 390])
def test_a_long_error_message_has_its_warning_sign_at_its_first_line(settings, store, page, width):
    """A long red message (Process new mail when LM Studio can't load the model: eight lines) had its warning sign,
    and the × that closes it, halfway down its side, not beside its first line. On a phone every message wrapped at
    half the screen's width, which made it longer still."""
    page.set_viewport_size({"width": width, "height": 800})
    with serving(web.create_app(settings, store)) as base:
        page.goto(f"{base}/app/settings")
        page.wait_for_selector("body.ready")
        for message, lines in (("That conversation could not be deleted.", 1), ("Processed. Read 1 new file. " + "LM Studio couldn't load the model you chose: there isn't enough memory for it on this computer. " * 5, 4)):
            shown = page.evaluate(LONG_TOAST, message)
            assert shown["lines"] >= lines, shown
            assert abs(shown["sign"] - shown["firstLine"]) <= 2, shown
            assert abs(shown["close"] - shown["firstLine"]) <= 2, shown
            if lines > 1:
                assert shown["width"] >= min(560, width - 24) - 1, shown


# "CloseDesk isn't answering", said once ------------------------------------------------------------------------------

NOT_ANSWERING = "CloseDesk isn't answering. If it has closed, double-click CloseDesk to start it again."


def _said_once(text: str) -> None:
    """The two sentences on their own, not in the brackets of another sentence ("Something went wrong (CloseDesk isn't
    answering. … start it again.). Try again."), and not followed by "Try again" when they say to start it again."""
    assert NOT_ANSWERING in text, text
    assert ".)" not in text and "(CloseDesk" not in text and "Try again" not in text and text.count("CloseDesk isn't answering") == 1, text


@pytest.mark.parametrize("where", ["workspace", "classic"])
def test_a_question_closedesk_doesnt_answer_says_so_once(settings, store, page, where):
    with serving(web.create_app(settings, store)) as base:
        page.set_viewport_size({"width": 1280, "height": 900})
        if where == "workspace":
            page.goto(f"{base}/app/all")
            page.click("#chat-btn")
        else:
            page.goto(f"{base}/chat/window")
        page.route("**/chat", lambda route: route.abort())
        page.fill("#chat textarea", "Are the Lakeside Tooling new bank details safe to use?")
        page.press("#chat textarea", "Enter")
        answer = page.wait_for_selector("#chat .msg:is(.bot, .assistant):has-text('isn\\'t answering')")
        _said_once(answer.inner_text())


def test_a_list_an_email_or_settings_closedesk_doesnt_answer_say_so_once(settings, loaded, page):
    """Each was "Couldn't load this list (CloseDesk isn't answering. … start it again.).", and on opening the
    workspace "CloseDesk didn't answer (CloseDesk isn't answering. …). Is it still running?". The "Try again" button
    beside them is for after CloseDesk is started again: only the words are checked."""
    said = "text=isn't answering"
    with serving(web.create_app(settings, loaded)) as base:
        page.set_viewport_size({"width": 1280, "height": 900})
        # Opening the workspace.
        page.route("**/api/state", lambda route: route.abort())
        page.goto(f"{base}/app/all")
        _said_once(page.wait_for_selector(f"#reader >> {said}").inner_text())
        page.unroute("**/api/state")
        # A list.
        page.goto(f"{base}/app/all")
        page.wait_for_selector("#list .item")
        page.route("**/api/mail?**", lambda route: route.abort())
        page.click(".rail-item[data-id='folder:important']")
        _said_once(page.wait_for_selector(f"#list >> {said}").inner_text())
        page.unroute("**/api/mail?**")
        # An email.
        page.goto(f"{base}/app/all")
        page.wait_for_selector("#list .item")
        page.route("**/api/mail/*", lambda route: route.abort())
        page.click("#list .item a")
        _said_once(page.wait_for_selector(f"#reader >> {said}").inner_text())
        page.unroute("**/api/mail/*")
        # Settings, read again after a change was saved.
        page.goto(f"{base}/app/settings")
        page.wait_for_selector("#set-colors")
        page.route("**/api/settings", lambda route: route.abort() if route.request.method == "GET" else route.continue_())
        page.click("#set-colors")
        line = "#set-appearance [data-status='colors']"
        page.wait_for_selector(f"{line}.error")
        text = page.inner_text(line)
        assert text.startswith(web.NOTICES["colors"]), text
        _said_once(text)
