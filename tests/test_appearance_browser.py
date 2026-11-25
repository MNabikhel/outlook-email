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


# In a contrast theme: whether the computer's colors are forced, each box's edge, ring and fill, and each legend
# swatch's and key-detail dot's.
FORCED_BOXES = """() => {
  const box = (el) => { const s = getComputedStyle(el); return { edge: s.borderTopColor, style: s.borderTopStyle, ring: s.boxShadow, fill: s.backgroundColor, radius: s.borderRadius }; };
  const dots = document.createElement('div');
  dots.innerHTML = '<span class="pv-dot ok"></span><span class="pv-dot check"></span><span class="pv-dot differs"></span>';
  document.querySelector('.pv-keys').append(dots);
  const out = { forced: matchMedia('(forced-colors: active)').matches,
                boxes: Object.fromEntries(['ok', 'check', 'differs'].map((n) => [n, box(document.querySelector(`.pv-sheet .pv-box.${n}`))])),
                swatches: Object.fromEntries([...document.querySelectorAll('.pv-swatch')].map((el) => [[...el.classList].pop(), box(el)])),
                dots: Object.fromEntries(['ok', 'check', 'differs'].map((n) => [n, box(dots.querySelector(`.pv-dot.${n}`))])) };
  dots.remove();
  return out;
}"""
CLEAR = "rgba(0, 0, 0, 0)"


@pytest.mark.parametrize("system", ["light", "dark"])
def test_the_boxes_on_a_page_keep_their_colors_and_shapes_in_a_contrast_theme(browser, settings, store, monkeypatch, system):
    """A Windows contrast theme (high contrast) draws every edge in the color of its words and drops shadows: a red
    box (its ring a shadow) looked like a green one, and in a dark theme every edge was white on the white page
    image. The key details' amber dot (a ring, drawn as a shadow) vanished, the green and red ones took the page's
    color, and the legend's red swatch lost its ring. They keep their own colors and shapes there: the page's image
    isn't changed by the theme, and the boxes are drawn on it."""
    email = scan_to_check(settings, store, monkeypatch)
    context = browser.new_context(viewport={"width": 1440, "height": 900}, color_scheme=system, forced_colors="active")
    context.set_default_timeout(15000)
    page = context.new_page()
    with serving(web.create_app(settings, store)) as base:
        _open_page_tab(page, base, email)
        page.wait_for_selector(".pv-box.check")
        page.wait_for_selector(".pv-keys:not([hidden])")
        shown = page.evaluate(FORCED_BOXES)
    context.close()
    assert shown["forced"], "a contrast theme"
    paper, theme = pm.workspace("light"), pm.workspace(system)
    boxes = shown["boxes"]
    assert {name: box["edge"] for name, box in boxes.items()} == {name: rgb(paper[f"--paper-{name}"]) for name in boxes}, boxes
    assert (boxes["ok"]["style"], boxes["check"]["style"], boxes["differs"]["style"]) == ("solid", "dotted", "solid"), boxes
    assert boxes["ok"]["ring"] == "none" and rgb(paper["--paper-differs"]) in boxes["differs"]["ring"], ("the red one's ring", boxes)
    assert all(box["fill"] != CLEAR for box in boxes.values()), ("tinted, as on the page without the theme", boxes)
    swatches = shown["swatches"]
    assert {"ok", "check", "differs"} <= set(swatches), swatches
    assert len({swatches[name]["edge"] for name in ("ok", "check", "differs")}) == 3, swatches
    assert swatches["check"]["style"] == "dotted" and swatches["differs"]["ring"] != "none", swatches
    dots = shown["dots"]
    assert dots["ok"]["fill"] == rgb(theme["--ok"]) and dots["ok"]["radius"] == "50%", ("a filled dot", dots)
    assert dots["check"]["fill"] == CLEAR and rgb(theme["--imp-high"]) in dots["check"]["ring"], ("a ring", dots)
    assert dots["differs"]["fill"] == rgb(theme["--danger"]) and dots["differs"]["radius"] == "1px", ("a square", dots)


OUTLINES = """() => {
  // The box drawn while teaching a field or marking a table is only there while dragging: one made the same way.
  const rubber = document.createElement('div'); rubber.className = 'pv-rubber'; document.querySelector('.pv-sheet').append(rubber);
  const out = { taught: getComputedStyle(document.querySelector('.pv-taught')).borderTopColor,
                rubber: getComputedStyle(rubber).borderTopColor };
  rubber.remove();
  return out;
}"""


@pytest.mark.parametrize("colors", ["standard", "color-blind"])
def test_a_taught_field_and_the_box_being_drawn_show_on_the_white_paper_in_dark_mode(settings, store, page, colors):
    """A taught field's outline and the box drawn while teaching a field or marking a table took the dark theme's
    green and blue, pastel on the white page: 1.87:1 and 2.43:1 (1.64:1 with the colors for color blindness)."""
    from fastapi.testclient import TestClient
    from msgfactory import PDF
    from test_page_view import _detailed_invoice
    from test_teach_and_preview import _teach
    from test_workspace import _harbor

    from controller_inbox import appearance

    email = _harbor(settings, store, ("HS-10482.pdf", _detailed_invoice(), PDF))
    app = web.create_app(settings, store)
    _teach(TestClient(app), email, "total", "1,874.40")
    appearance.save(store, colors=colors)
    page.emulate_media(color_scheme="dark")
    with serving(app) as base:
        _open_page_tab(page, base, email)
        page.wait_for_selector(".pv-taught")
        shown = page.evaluate(OUTLINES)
    paper = pm.workspace("light", colors)
    assert shown == {"taught": rgb(paper["--paper-ok"]), "rubber": rgb(paper["--paper-accent"])}, shown
    seen = {name: round(pm.contrast(hex_of(value), "#ffffff"), 2) for name, value in shown.items()}
    assert all(ratio >= 3 for ratio in seen.values()), seen


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


# The save of the colors box (not of the words box) reaches CloseDesk 1.5 s late, as on a computer busy with a run.
SLOW_COLORS = """(() => { const send = window.fetch;
  window.fetch = (url, options = {}) => String(url).includes('/api/settings/colors') && options.method === 'POST' && String(options.body).includes('"colors"')
    ? new Promise((resolve) => setTimeout(resolve, 1500)).then(() => send(url, options)) : send(url, options); })();"""
# How the colors box looks from the moment it is unticked: every 10 ms, as the page draws it then.
WATCH_BOX = """() => document.addEventListener('change', (event) => {
  if (event.target.id !== 'set-colors' || window.seen) return;
  window.seen = [];
  setInterval(() => { const box = document.querySelector('#set-colors'); window.seen.push(box ? box.checked : null); }, 10);
}, true)"""


def test_ticking_the_other_colors_box_while_one_is_saving_leaves_the_first_as_ticked(settings, store, page):
    """Both ticked; "Colors for color blindness" unticked, and "Show Critical, High, Medium or Low" unticked while the
    first was still being saved: the second's answer drew the page again from the colors in use, which the first
    hadn't changed yet, so the first box ticked itself again for as long as its save took. Each box shows what was
    last asked of it until its save is answered."""
    from controller_inbox import appearance

    appearance.save(store, colors="color-blind", importance_words=True)
    page.add_init_script(SLOW_COLORS)
    with serving(web.create_app(settings, store)) as base:
        page.goto(f"{base}/app/settings")
        page.wait_for_selector("#set-colors:checked")
        page.evaluate(WATCH_BOX)
        page.click("#set-colors")
        page.wait_for_timeout(50)
        page.click("#set-importance-words")
        page.wait_for_function("() => !document.documentElement.dataset.colors && !document.documentElement.dataset.importanceWords")
        page.wait_for_function("() => !document.querySelector('#set-appearance [aria-busy]')")
        page.wait_for_timeout(300)  # the page drawn again after the last answer
        seen = page.evaluate("() => window.seen")
        assert seen and True not in seen, ("the colors box ticked itself again", seen.count(True), len(seen))
        assert page.evaluate("() => [document.querySelector('#set-colors').checked, document.querySelector('#set-importance-words').checked]") == [False, False]
        assert appearance.current(store) == {"colors": "standard", "importance_words": False}


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
           locked: li.classList.contains('is-locked'), held: (li.querySelector('.chip-danger') || {}).textContent || '',
           heldShown: [...li.querySelectorAll('.chip-danger')].some((c) => c.getBoundingClientRect().width > 0),
           emptyLine: [...li.querySelectorAll('.item-meta')].some((m) => m.getBoundingClientRect().height > 0
             && ![...m.children].some((c) => c.getBoundingClientRect().width > 0)) };
})"""
WORDS = {"critical": "Critical", "high": "High", "medium": "Medium", "low": "Low"}

# Each email's importance as a list shows it, on the classic pages and in the workspace's digest: its word, the email
# it is on, and how it is drawn (its fill, its edge, its colour, and the colour of what is under it), with the reason
# tag beside it on a focus row.
ROW_WORDS = """sel => [...document.querySelectorAll(sel)].map((li) => {
  const tag = li.querySelector('.item-level');
  const link = li.querySelector('a[href^="/inbox/"], a[href^="/app/mail/"]');
  const style = tag ? getComputedStyle(tag) : null;
  let under = '', el = tag;
  while (el && !under) { const bg = getComputedStyle(el).backgroundColor; if (!/^rgba\\(.*,\\s*0\\)$/.test(bg) && bg !== 'transparent') under = bg; el = el.parentElement; }
  const reason = li.querySelector('.focus-top .tag, .tag:not(.item-level)');
  return { email: link ? decodeURIComponent(link.getAttribute('href').split('?')[0].split('/').pop()) : '',
           text: tag ? tag.textContent : null, classes: tag ? tag.className : '',
           shown: Boolean(tag) && style.display !== 'none' && tag.getBoundingClientRect().width > 0,
           inside: !tag || tag.getBoundingClientRect().right <= li.getBoundingClientRect().right,
           color: style ? style.color : '', background: style ? style.backgroundColor : '', outline: style ? style.boxShadow : '',
           size: style ? `${style.fontSize} ${style.letterSpacing}` : '',
           under, reason: reason && reason !== tag ? getComputedStyle(reason).backgroundColor : null };
})"""
HELD = "demo-bec-wire"  # the sample's wire change, held as possible payment fraud


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
            if nav == "today":
                # Today's box of emails to verify by phone, as the digest's (drawn twice: a phone shows it above the
                # list).
                page.wait_for_selector(".card-danger .plain-list li:visible")
                shown = [(row["email"], row["text"]) for row in page.evaluate(ROW_WORDS, ".card-danger .plain-list li") if row["shown"]]
                assert shown == [(HELD, "Held for fraud")], shown
            for row in rows:
                assert row["level"], row
                assert not row["emptyLine"], ("no line left empty", path, row)
                if row["locked"]:
                    # Held as possible payment fraud: its edge is the held one, so its word says that. The chip below
                    # said "Held: possible fraud" as well, on the same row: it goes while the word says it. "Files
                    # locked" says more, and stays.
                    assert row["shown"] and row["text"] == "Held for fraud" and "tag-held" in row["classes"], (path, row)
                    assert row["held"], ("a held email still has its held chip", row)
                    assert row["heldShown"] == (row["held"] == "Files locked"), ("held said once", path, row)
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
        for nav in ("all", "today"):
            page.click(f".rail-item[data-id='{nav}']")
            page.wait_for_selector(".items .item")
            rows = page.evaluate(LEVELS)
            assert not [row for row in rows if row["shown"]]
            # With no word, the chip says it is held.
            assert [row for row in rows if row["locked"]] and all(row["heldShown"] for row in rows if row["locked"]), nav
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




def _words_say_the_right_thing(rows: list[dict], where) -> None:
    """Each email's word is its importance; the held one's says it is held, wherever it is listed."""
    assert rows, where
    for row in rows:
        assert row["shown"] and row["inside"], (where, row)
        if row["email"] == HELD:
            assert row["text"] == "Held for fraud", ("one email, one word, on every page", where, row)
        else:
            assert row["text"] in WORDS.values(), (where, row)
    assert HELD in {row["email"] for row in rows}, where


def _drawn_apart_from_the_reason(rows: list[dict], where) -> None:
    """Outlined, with no fill, in a colour that reads on what is under it: beside it, the reason the email is listed
    ("Due today", "Verify by phone") is a filled tag, and "how important" doesn't look like a second "why"."""
    for row in rows:
        assert row["background"] == "rgba(0, 0, 0, 0)" and "inset" in row["outline"], ("outlined", where, row)
        text, under, edge = hex_of(row["color"]), hex_of(row["under"]), hex_of(row["outline"])
        assert pm.contrast(text, under) >= 4.5, ("its word", where, row, round(pm.contrast(text, under), 2))
        assert pm.contrast(edge, under) >= 3.0, ("its edge shows", where, row, round(pm.contrast(edge, under), 2))
        if row["reason"] is not None:
            assert row["reason"] != "rgba(0, 0, 0, 0)" and row["reason"] != row["background"], ("the reason stays filled", where, row)
    # The same word at the same size wherever it is: not the folder card's heading's ("NEEDS YOU").
    assert len({row["size"] for row in rows}) == 1, (where, {row["size"] for row in rows})


CLASSIC_TODAY = ".panel.alert li, .focus-item, .folder-card li:not(.muted)"


@pytest.mark.parametrize(("width", "colors"), [(1280, "standard"), (390, "standard"), (1280, "color-blind")])
def test_classic_today_writes_the_importance_on_each_email_when_asked(browser, settings, loaded, width, colors):
    """Today, the classic home page, had no importance on its emails with the box ticked: only the Inbox and folder
    lists had it, in their Importance column. Then it had it, but the held email said "Critical", the Medium and Low
    words in the folder cards were grey on their fill (3.65:1), and each word was filled in the colour of the reason
    tag beside it ("Due today" and "HIGH" both amber)."""
    from controller_inbox import appearance

    appearance.save(loaded, colors=colors)
    context = _context(browser, "light")
    page = context.new_page()
    page.set_viewport_size({"width": width, "height": 900})
    with serving(web.create_app(settings, loaded)) as base:
        page.goto(f"{base}/")
        rows = page.evaluate(ROW_WORDS, CLASSIC_TODAY)
        # Unticked, only a held email says what it is, as the lists' held chip does (but not in the red box of emails
        # to verify by phone, which says it): its focus row and its row in Needs you.
        assert rows and [(row["email"], row["text"]) for row in rows if row["shown"]] == [(HELD, "Held for fraud")] * 2, rows
        assert not [row for row in page.evaluate(ROW_WORDS, ".panel.alert li") if row["shown"]]
        appearance.save(loaded, importance_words=True)
        page.reload()
        rows = page.evaluate(ROW_WORDS, CLASSIC_TODAY)
        _words_say_the_right_thing(rows, "/")
        assert {"Critical", "High", "Medium", "Low"} <= {row["text"] for row in rows}
        _drawn_apart_from_the_reason(rows, "/")
        # In the red box of emails to verify by phone, as in the digest's.
        assert [row["text"] for row in page.evaluate(ROW_WORDS, ".panel.alert li")] == ["Held for fraud"]
    context.close()


# How much of each subject a folder card on classic Today shows on its one line: the characters before the edge it is
# cut at (the card's, or the link's own when it cuts its words itself).
SUBJECT_SHOWN = """() => [...document.querySelectorAll('.folder-card li:not(.muted)')].map((li) => {
  const link = li.querySelector('a[href^="/inbox/"]');
  const words = link.firstChild;
  let right = li.getBoundingClientRect().right - parseFloat(getComputedStyle(li).paddingRight);
  if (getComputedStyle(link).overflow !== 'visible') right = Math.min(right, link.getBoundingClientRect().right);
  const range = document.createRange();
  let shown = 0;
  for (; shown < words.length; shown++) {
    range.setStart(words, shown);
    range.setEnd(words, shown + 1);
    if (range.getBoundingClientRect().right > right + 0.5) break;
  }
  const tag = li.querySelector('.item-level');
  return { email: link.getAttribute('href'), subject: words.textContent, shown, title: link.title,
           tag: tag && getComputedStyle(tag).display !== 'none' ? tag.getBoundingClientRect().right <= right + 0.5 : null };
})"""


@pytest.mark.parametrize("width", [981, 1100, 1280])
def test_the_importance_word_leaves_the_subject_its_line_in_todays_folder_cards(browser, settings, loaded, width):
    """Each email in classic Today's "Needs you", "Worth knowing" and "Filed for reference" is one line, cut with "…"
    at the card's edge, and the word was put at its start: at 981px wide the held email went from 27 characters of
    its subject to 10 ("HELD FOR FRAUD  Urgent: …"), and the Critical ones from about 25 to 15. The word has a line
    of its own, above the subject."""
    from controller_inbox import appearance

    context = _context(browser, "light")
    page = context.new_page()
    page.set_viewport_size({"width": width, "height": 900})
    with serving(web.create_app(settings, loaded)) as base:
        page.goto(f"{base}/")
        without = page.evaluate(SUBJECT_SHOWN)
        appearance.save(loaded, importance_words=True)
        page.reload()
        shown = page.evaluate(SUBJECT_SHOWN)
    context.close()
    assert len(shown) == len(without) >= 6 and [row["email"] for row in shown] == [row["email"] for row in without]
    assert [row for row in without if row["shown"] < len(row["subject"])], ("some subjects are cut", width, without)
    cut = {
        row["subject"]: (before["shown"], row["shown"])
        for row, before in zip(shown, without, strict=True)
        if row["shown"] < before["shown"] - 2
    }
    assert not cut, (width, cut)
    assert all(row["tag"] is True for row in shown), ("each word shown, inside the card", shown)
    assert all(row["title"] == row["subject"] for row in shown), "the whole subject under the pointer"


MODEL_FLAGS = """() => [...document.querySelectorAll('table.mail tbody tr')].map((tr) => {
  const cell = tr.querySelector('a.subj').closest('td');
  const above = [...cell.children].filter((el) => el.matches('.summary, .why, .flags:not(.model-flags), a.subj')).pop();
  const flag = cell.querySelector('.flag.needs_model:not(.flags:not(.model-flags) .flag), .flag.bionic, .flag.user_trained');
  return { subject: tr.querySelector('a.subj').textContent,
           gap: flag && above ? flag.getBoundingClientRect().top - above.getBoundingClientRect().bottom : null,
           waiting: [...cell.querySelectorAll('.flag')].filter((f) => /^(script draft|needs model)$/i.test(f.innerText.trim())).map((f) => f.innerText.trim()) };
})"""


@pytest.mark.parametrize("width", [1280, 390])
def test_the_classic_inbox_says_once_that_an_email_waits_for_the_model_with_room_above_it(browser, settings, loaded, width):
    """The dashed "Script draft" box sat flush against the summary line above it (0px: its top edge touched the
    descenders of "Confirm by phone before any payment."), and the same row had a second, identical dashed box, "Needs
    Model", saying the email waits for the model too."""
    context = _context(browser, "light")
    page = context.new_page()
    page.set_viewport_size({"width": width, "height": 900})
    with serving(web.create_app(settings, loaded)) as base:
        page.goto(f"{base}/inbox")
        rows = page.evaluate(MODEL_FLAGS)
    context.close()
    waiting = [row for row in rows if row["waiting"]]
    assert waiting, rows
    for row in rows:
        assert len(row["waiting"]) <= 1, ("said once", row)
        if row["gap"] is not None:
            assert row["gap"] >= 4, ("room above it", width, row)
    assert [row for row in waiting if row["gap"] is not None], waiting


FOCUS_EDGES = """() => [...document.querySelectorAll('.focus-item')].map((li) => {
  const level = li.querySelector('.item-level');
  return { title: (li.querySelector('.focus-top a') || {}).textContent || '', email: (li.querySelector('.focus-top a') || { href: '' }).href.split('/').pop(),
           kind: [...li.classList].filter((name) => name !== 'focus-item' && !name.startsWith('imp-')).join(' '),
           level: level ? ['critical', 'high', 'medium', 'low', 'held'].find((name) => level.classList.contains(name)) || '' : '',
           edge: getComputedStyle(li).borderLeftColor };
})"""


def _a_medium_task_overdue(store) -> str:
    """The sample has no task overdue on its day: a Medium email's task due today is made two days late."""
    from controller_inbox.models import ActionStatus, Importance

    for stub in store.list_emails(importance="medium"):
        email = store.get_email(stub.id)
        due = [action for action in email.actions if action.status == ActionStatus.OPEN and action.due_date == "2026-09-22"]
        if email.importance == Importance.MEDIUM and due:
            for action in due:
                action.due_date = "2026-09-20"
            store.upsert_email(email)
            return email.id
    raise AssertionError("no Medium email with a task due today")


@pytest.mark.parametrize("colors", ["standard", "color-blind"])
@pytest.mark.parametrize("path", ["/", "/digest"])
def test_a_critical_email_on_classic_today_has_the_red_edge_whatever_its_reason(browser, settings, loaded, colors, path):
    """Classic Today gave "Review possible duplicate invoice INV-10482", a Critical email due today, the amber edge of
    a High one (the edge said the reason, and due today is amber), where the workspace's Today and every list give it
    the red of Critical. Its edge now says its importance when it is Critical or High, as theirs do, and a held email
    (or one to verify by phone) has the red of danger. The others had the edge of their reason, in the same colors: a
    Medium email due today had the amber of High, and an overdue one the red of danger, 2.2 from Critical's. Their edge
    is now the plain one, as in the workspace, where only Critical, High and held emails have a colored edge: the
    reason is said in its tag."""
    from controller_inbox import appearance

    appearance.save(loaded, colors=colors)
    overdue = _a_medium_task_overdue(loaded)
    tokens = pm.classic(colors)
    context = _context(browser, "light")
    page = context.new_page()
    with serving(web.create_app(settings, loaded)) as base:
        if path == "/digest":
            page.goto(f"{base}/")
            page.request.post(f"{base}/digest/rebuild", headers={"origin": base})
        page.goto(f"{base}{path}")
        rows = page.evaluate(FOCUS_EDGES)
    context.close()
    assert [row for row in rows if row["level"] == "critical" and row["kind"] == "due_today"], ("the case seen", rows)
    assert [row for row in rows if row["level"] == "medium" and row["kind"] == "due_today"], ("a Medium email due today", rows)
    assert [row for row in rows if row["level"] == "medium" and row["kind"] == "overdue" and row["email"] == overdue], rows
    for row in rows:
        edge = hex_of(row["edge"])
        if row["level"] == "critical":
            assert edge == tokens["--imp-critical"], (path, colors, row)
        elif row["level"] == "held" or "fraud" in row["kind"].split():
            assert edge == tokens["--danger"], (path, colors, row)
        elif row["level"] == "high":
            assert edge == tokens["--imp-high"], (path, colors, row)
        else:
            assert edge == tokens["--line"], ("the plain edge, whatever the reason", path, colors, row)


# The digest's lists of emails (not its tasks, which have their own priority): what needs a phone call, the focus
# list, and what came in, in each folder.
DIGEST_EMAILS = {
    "workspace": ".page .card-danger .plain-list li, .page ol.ranked li, .page .cards3 .plain-list li",
    "classic": ".panel.alert ul.stack li, .focus-item, .split.three ul.stack li",
}


@pytest.mark.parametrize(("where", "colors"), [("workspace", "standard"), ("classic", "standard"), ("classic", "color-blind")])
def test_the_digest_writes_the_importance_on_each_email_when_asked(browser, settings, loaded, where, colors):
    """The digest lists the day's emails, as Today does: with the box ticked, each says its importance there too, in
    the workspace as on the classic page. The held email said "Critical" there, where the lists say "Held for fraud";
    and on the classic page each word was filled like the reason tag beside it, and in the red "Do not process" box,
    in nearly the red of the box (1.08:1), so it read as the first word of the subject."""
    from controller_inbox import appearance

    appearance.save(loaded, colors=colors)
    context = _context(browser, "light")
    page = context.new_page()
    with serving(web.create_app(settings, loaded)) as base:
        page.request.post(f"{base}/digest/rebuild", headers={"origin": base})
        appearance.save(loaded, importance_words=True)
        page.goto(f"{base}/app/digest" if where == "workspace" else f"{base}/digest")
        page.wait_for_selector(DIGEST_EMAILS[where])
        rows = page.evaluate(ROW_WORDS, DIGEST_EMAILS[where])
        assert len(rows) >= 10, rows
        _words_say_the_right_thing(rows, where)
        # Its box to verify by phone, its row in the focus list, and in Needs you.
        assert [row["text"] for row in rows if row["email"] == HELD] == ["Held for fraud"] * 3, rows
        if where == "classic":
            _drawn_apart_from_the_reason(rows, where)
    context.close()


# Each email of the digest (and of Today's box of emails to verify by phone): its edge, where it is drawn, and where
# its words start.
PLAIN_EDGES = """sel => [...document.querySelectorAll(sel)].filter((li) => li.getBoundingClientRect().width > 0).map((li) => {
  const link = li.querySelector('a[href^="/app/mail/"]');
  const edge = getComputedStyle(li, '::before'), r = li.getBoundingClientRect();
  const card = li.closest('.card').getBoundingClientRect();
  const word = li.querySelector('.item-level');
  return { email: link ? decodeURIComponent(link.getAttribute('href').split('?')[0].split('/').pop()) : '',
           edge: edge.content !== 'none' ? edge.backgroundColor : null,
           edgeLeft: r.left + parseFloat(edge.left), edgeRight: r.left + parseFloat(edge.left) + parseFloat(edge.width),
           text: r.left + parseFloat(getComputedStyle(li).paddingLeft), list: li.parentElement.getBoundingClientRect().left,
           card: card.left + parseFloat(getComputedStyle(li.closest('.card')).borderLeftWidth),
           inBox: Boolean(li.closest('.card-danger')),
           word: word && getComputedStyle(word).display !== 'none' && word.getBoundingClientRect().width > 0 ? word.textContent : null };
})"""
CODING_ROWS = """() => [...document.querySelectorAll('.items .item.kind-coding')].map((li) => {
  const tag = li.querySelector('.item-level');
  const held = [...li.querySelectorAll('.chip-danger')].filter((chip) => chip.getBoundingClientRect().width > 0);
  return { email: li.dataset.key.split(':').pop(), edge: getComputedStyle(li, '::before').backgroundColor,
           word: tag && getComputedStyle(tag).display !== 'none' ? tag.textContent : null,
           held: held.map((chip) => chip.textContent), state: li.querySelector('.item-row1 .tag').textContent };
})"""


def _edge_of(store, email_id: str, colors: str) -> str | None:
    """The edge an email has in the lists: a held one the red of danger, a Critical or High one its importance's, the
    others none."""
    from controller_inbox import fraud

    email, tokens = store.get_email(email_id), pm.workspace("light", colors)
    if fraud.attachments_locked(email):
        return rgb(tokens["--danger"])
    level = email.importance.value
    return rgb(tokens[f"--imp-{level}"]) if level in ("critical", "high") else None


@pytest.mark.parametrize("colors", ["standard", "color-blind"])
def test_the_digest_and_the_ap_coding_list_mark_critical_high_and_held_as_the_lists_do(browser, settings, loaded, colors):
    """Settings says that without the importance words only Critical, High and held emails are marked, by a colored
    edge, and with them each email in the lists and the daily digest says its importance. The workspace's digest
    marked nothing without the words, and the AP coding list's rows had neither edge nor word: its duplicate invoice is
    Critical."""
    from controller_inbox import appearance

    coded = loaded.cost_codings(status=None)
    held = loaded.get_email(coded[-1]["email_id"])
    loaded.save_cost_coding(held.id, status="confirmed", codes=[{"code": "6100", "description": "Supplies"}])
    held.flags.append("fraud_risk")  # held after its coding was confirmed: it stays on the list
    loaded.upsert_email(held)
    appearance.save(loaded, colors=colors)
    context = _context(browser, "light")
    page = context.new_page()
    with serving(web.create_app(settings, loaded)) as base:
        page.request.post(f"{base}/digest/rebuild", headers={"origin": base})
        for path, sel in (("/app/digest", DIGEST_EMAILS["workspace"]), ("/app", ".card-danger .plain-list li")):
            page.goto(f"{base}{path}")
            page.wait_for_selector(f"{sel}:visible")  # Today draws its box twice: a phone shows it above the list
            rows = page.evaluate(PLAIN_EDGES, sel)
            assert rows, path
            marked = 0
            for row in rows:
                want = _edge_of(loaded, row["email"], colors)
                assert row["edge"] == want, (path, row, want)
                # A held email says so in words, its red edge being nearly a Critical one's; but in the box of emails
                # to verify by phone, which says it, only when the importance words are asked for.
                held_here = want == rgb(pm.workspace("light", colors)["--danger"]) and not row["inBox"]
                assert row["word"] == ("Held for fraud" if held_here else None), (path, row)
                # Each email's words start where the others' do; the edge is in the card's margin, beside them.
                assert row["text"] == row["list"], (path, row)
                if want:
                    marked += 1
                    assert row["card"] < row["edgeLeft"] < row["edgeRight"] < row["text"], (path, row)
            assert marked, path
        page.goto(f"{base}/app/coding?status=all")
        page.wait_for_selector(".items .item.kind-coding")
        rows = page.evaluate(CODING_ROWS)
        assert {row["email"] for row in rows} == {item["email_id"] for item in coded}, rows
        for row in rows:
            assert row["edge"] == (_edge_of(loaded, row["email"], colors) or "rgba(0, 0, 0, 0)"), row
            assert row["word"] is None, row
            # Its edge is nearly the red of a Critical one's, and it says "Confirmed": it says it is held, in words.
            assert row["held"] == (["Held: possible fraud"] if row["email"] == held.id else []), row
        assert "critical" in {loaded.get_email(row["email"]).importance.value for row in rows}
        assert [row["state"] for row in rows if row["email"] == held.id] == ["Confirmed"], rows
        appearance.save(loaded, importance_words=True)
        page.reload()
        page.wait_for_selector(".items .item.kind-coding .item-level")
        for row in page.evaluate(CODING_ROWS):
            email = loaded.get_email(row["email"])
            assert row["word"] == ("Held for fraud" if email.id == held.id else WORDS[email.importance.value]), row
            assert row["held"] == [], ("said once: the word says it", row)
    context.close()


FRAUD_ROWS = """() => [...document.querySelectorAll('.items .item.kind-fraud')].map((li) => {
  const words = [...li.querySelectorAll('.item-row1 .tag')].filter((tag) => tag.getBoundingClientRect().width > 0);
  return { email: li.dataset.key, edge: getComputedStyle(li, '::before').backgroundColor, words: words.map((tag) => tag.textContent) };
})"""


@pytest.mark.parametrize("colors", ["standard", "color-blind"])
def test_the_fraud_check_list_marks_critical_high_and_held_as_the_lists_do(browser, settings, loaded, colors):
    """A Caution email on the Fraud check list that is Critical or High had neither the colored edge nor, with the
    importance words ticked, its word: the list's rows had no importance. A Blocked one, held, says "Blocked", and not
    "Held for fraud" beside it."""
    from controller_inbox import appearance
    from test_appearance import CAUTIONS

    for email_id in CAUTIONS:
        loaded.save_fraud_check(email_id, 40, "caution", [{"key": "bank", "label": "Bank details mentioned", "points": 40}], "2026-09-22T12:00:00+00:00")
    appearance.save(loaded, colors=colors)
    context = _context(browser, "light")
    page = context.new_page()
    with serving(web.create_app(settings, loaded)) as base:
        page.goto(f"{base}/app/fraud")
        page.wait_for_selector(".items .item.kind-fraud")
        rows = {row["email"]: row for row in page.evaluate(FRAUD_ROWS)}
        assert set(rows) == {HELD, *CAUTIONS}, rows
        for email_id, row in rows.items():
            assert row["edge"] == _edge_of(loaded, email_id, colors), row
            assert row["words"] == (["Blocked"] if email_id == HELD else ["Caution"]), row
        appearance.save(loaded, importance_words=True)
        page.reload()
        page.wait_for_selector(".items .item.kind-fraud .item-level")
        rows = {row["email"]: row for row in page.evaluate(FRAUD_ROWS)}
        assert {email_id: row["words"] for email_id, row in rows.items()} == {
            HELD: ["Blocked"], CAUTIONS[0]: ["Caution", "Critical"], CAUTIONS[1]: ["Caution", "High"]}, rows
    context.close()


CLASSIC_LIST_EDGES = """sel => [...document.querySelectorAll(sel)].filter((li) => li.getBoundingClientRect().width > 0).map((li) => {
  const link = li.querySelector('a[href^="/inbox/"]');
  const edge = getComputedStyle(li, '::before'), r = li.getBoundingClientRect();
  const panel = li.closest('.panel'), word = li.querySelector('.pill.item-level');
  return { email: link ? link.getAttribute('href').split('/').pop() : '', edge: edge.content !== 'none' ? edge.backgroundColor : null,
           width: edge.width, edgeLeft: r.left + parseFloat(edge.left), edgeRight: r.left + parseFloat(edge.left) + parseFloat(edge.width),
           text: (li.querySelector('a, .pill') || li).getBoundingClientRect().left, inside: r.left + parseFloat(getComputedStyle(li).paddingLeft),
           panel: panel.getBoundingClientRect().left + parseFloat(getComputedStyle(panel).borderLeftWidth),
           word: word && getComputedStyle(word).display !== 'none' ? word.textContent : null };
})"""
def _classic_edge_of(store, email_id: str, colors: str) -> str | None:
    """The edge an email has on the classic pages: a held one the red of danger, a Critical or High one its
    importance's, the others none."""
    from controller_inbox import fraud

    email, tokens = store.get_email(email_id), pm.classic(colors)
    if fraud.attachments_locked(email):
        return rgb(tokens["--danger"])
    level = email.importance.value
    return rgb(tokens[f"--imp-{level}"]) if level in ("critical", "high") else None


@pytest.mark.parametrize("colors", ["standard", "color-blind"])
@pytest.mark.parametrize("path", ["/", "/digest"])
def test_classic_today_and_the_digest_mark_critical_high_and_held_in_every_list(browser, settings, loaded, colors, path):
    """Settings says the classic pages follow it, and that without the importance words Critical, High and held
    emails are marked by a colored edge; but classic Today's folder cards and the classic digest's lists of emails
    marked none of them, the held wiring email, the CP2000 notice and the Critical duplicate invoice included. They
    have the Inbox's edges now, in the card's margin; a held email says so in words too, its red edge being nearly a
    Critical one's. The red box of emails to verify by phone has no edge, and says it."""
    from controller_inbox import appearance

    appearance.save(loaded, colors=colors)
    context = _context(browser, "light")
    page = context.new_page()
    with serving(web.create_app(settings, loaded)) as base:
        page.request.post(f"{base}/digest/rebuild", headers={"origin": base})
        page.goto(f"{base}{path}")
        rows = page.evaluate(CLASSIC_LIST_EDGES, ".folder-card li:not(.muted)" if path == "/" else ".split.three ul.stack li")
        box = page.evaluate(CLASSIC_LIST_EDGES, ".panel.alert li")
        appearance.save(loaded, importance_words=True)
        page.reload()
        worded = page.evaluate(CLASSIC_LIST_EDGES, ".folder-card li:not(.muted)" if path == "/" else ".split.three ul.stack li")
    context.close()
    assert len(rows) >= 6 and box, (rows, box)
    marked = {}
    for row in rows:
        want = _classic_edge_of(loaded, row["email"], colors)
        assert row["edge"] == want, (path, row, want)
        if want:
            marked[row["email"]] = want
            assert row["width"] == ("4px" if want == rgb(pm.classic(colors)["--danger"]) else "3px"), row
            assert row["panel"] < row["edgeLeft"] < row["edgeRight"] < row["inside"], ("in the card's margin", path, row)
        assert row["word"] == ("Held for fraud" if want == rgb(pm.classic(colors)["--danger"]) else None), (path, row)
    assert {"demo-bec-wire", "demo-tax", "demo-inv-10482-dup"} <= set(marked), marked
    assert len({row["inside"] for row in rows}) <= 3, ("each email's words start where the others' do", rows)
    assert all(row["edge"] is None and row["word"] is None for row in box), ("the box says it", box)
    assert [(row["email"], row["edge"]) for row in worded] == [(row["email"], row["edge"]) for row in rows], "the edges stay"


# Printed, each red heading and tag of the printable digest: its words' colour, its fill and its edge. White on red
# is how they are on screen.
PRINTED = """() => [...document.querySelectorAll('.alert h2, .tag.fraud, .tag.overdue, .tag.due_today')].map((el) => {
  const s = getComputedStyle(el);
  return { what: el.matches('h2') ? 'alert heading' : el.className, text: el.textContent, color: s.color, fill: s.backgroundColor,
           edge: s.borderBottomStyle !== 'none' && parseFloat(s.borderBottomWidth) > 0 ? s.borderBottomColor : null,
           box: getComputedStyle(el.closest('.alert') || el).backgroundColor };
})"""


@pytest.mark.parametrize("colors", ["standard", "color-blind"])
def test_the_printed_digest_keeps_its_red_words_without_background_graphics(browser, settings, loaded, colors):
    """Printed as a browser prints by default, without "Background graphics", the printable digest lost its red
    fills and kept the white words on them: "Do not process — verify by phone" and each "Verify by phone" and
    "Overdue" tag came out light grey on white, the faintest words on the page. Printed, they are the red words of
    the colors in use on the paper, with a red edge, and the amber "Due today" tag keeps an amber edge."""
    from controller_inbox import appearance

    appearance.save(loaded, colors=colors)
    _a_medium_task_overdue(loaded)
    tokens = pm.classic(colors)
    context = _context(browser, "light")
    page = context.new_page()
    with serving(web.create_app(settings, loaded)) as base:
        page.request.post(f"{base}/digest/rebuild", headers={"origin": base})
        page.goto(f"{base}/digest/2026-09-22.html")
        on_screen = page.evaluate(PRINTED)
        page.emulate_media(media="print")
        printed = page.evaluate(PRINTED)
    context.close()
    seen = {row["what"] for row in printed}
    assert {"alert heading", "tag fraud", "tag overdue", "tag due_today"} <= seen, seen
    assert all(row["color"] == "rgb(255, 255, 255)" for row in on_screen if row["what"] != "tag due_today"), ("white on red on screen", on_screen)
    for row in printed:
        assert row["fill"] == "rgba(0, 0, 0, 0)" and row["box"] in ("rgba(0, 0, 0, 0)", row["fill"]), ("nothing rests on a fill", row)
        ratio = pm.contrast(hex_of(row["color"]), pm.WHITE)
        assert ratio >= 4.5, (row, round(ratio, 2))
        if row["what"] == "tag due_today":
            assert row["edge"] == rgb(tokens["--imp-high"]), row
        else:
            assert row["color"] == row["edge"] == rgb(tokens["--danger"]), row


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
  // An AP invoice held after its coding was confirmed says "Held for fraud", beside the longest coding state.
  for (const li of document.querySelectorAll('.items .item.kind-coding')) {
    li.querySelector('.item-row1 .tag:not(.item-level)').textContent = 'No code found';
    li.querySelector('.item-level').textContent = 'Held for fraud';
  }
  // A Caution email on the Fraud check list, with the longest importance word and the note that it was cleared.
  for (const li of document.querySelectorAll('.items .item.kind-fraud')) {
    const level = li.querySelector('.item-level');
    if (!level) continue;
    level.textContent = 'Critical';
    const when = li.querySelector('.item-row1 .item-when');
    if (when) { when.textContent = 'score 100 · Sep 22'; when.before(Object.assign(document.createElement('span'), { className: 'chip', textContent: 'you cleared it' })); }
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
    from test_appearance import CAUTIONS

    appearance.save(loaded, importance_words=True)
    for email_id in CAUTIONS:  # the Fraud check list's Caution rows say their importance too
        loaded.save_fraud_check(email_id, 40, "caution", [{"key": "bank", "label": "Bank details mentioned", "points": 40}], "2026-09-22T12:00:00+00:00")
    context = browser.new_context(viewport={"width": width, "height": 900})
    page = context.new_page()
    with serving(web.create_app(settings, loaded)) as base:
        problems = {}
        for path in ("/app", "/app/folder/important", "/app/all", "/app/coding?status=all", "/app/fraud"):
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


# Setup's Update button, as its form sends it (the button shows only when a newer version has been found).
PRESS_UPDATE = """() => { const form = document.createElement('form'); form.method = 'post'; form.action = '/settings/update';
  document.body.append(form); form.submit(); }"""


@pytest.mark.parametrize("refused", [False, True], ids=["updating", "refused"])
def test_back_to_the_page_the_update_button_sent_doesnt_ask_for_the_update_again(browser, settings, loaded, monkeypatch, tmp_path, refused):
    """The classic pages were sent as never to be kept, the page Setup's Update button sends too. Back to it, after it
    had gone on to Setup (or "Back to Setup" was clicked on the one saying it can't update now), showed the browser's
    "Confirm Form Resubmission", and its Reload sent the button again: CloseDesk stopped for a second update, or did
    the update refused earlier once the run had finished, without Update being pressed."""
    from controller_inbox import updates, version

    copy = tmp_path / "this copy"
    copy.mkdir()
    archival = copy / ".git_archival.txt"
    archival.write_text(f"commit: {'a' * 40}\ndate: 2026-11-13\n", encoding="utf-8")
    monkeypatch.setattr(version, "ROOT", copy)
    asked: list[str] = []  # each time the button reached CloseDesk

    def refusal(job_state: str) -> str:
        asked.append(job_state)
        return updates.BUSY if refused else ""

    monkeypatch.setattr(updates, "refusal", refusal)
    app = web.create_app(settings, loaded)
    stops: list[int] = []
    app.state.stop_server = lambda: stops.append(1)
    context = _context(browser, "light")
    page = context.new_page()
    with serving(app) as base:
        page.goto(f"{base}/settings")
        page.evaluate(PRESS_UPDATE)
        page.wait_for_url("**/settings/update")
        if refused:
            assert updates.BUSY in page.inner_text("#update")
            page.click("#update a[href='/settings#update']")
            page.wait_for_url("**/settings#update")
            page.go_back()  # net::ERR_CACHE_MISS, and the browser's page asking to send the form again
            assert updates.BUSY in page.inner_text("#update"), "the page as it was sent"
        else:
            page.wait_for_selector("[data-update-line]")
            # CloseDesk answers again, as the newer version: the page goes on to Setup by itself.
            archival.write_text(f"commit: {'b' * 40}\ndate: 2026-11-16\n", encoding="utf-8")
            page.wait_for_url("**/settings#update", timeout=15000)
            page.go_back()
            assert page.url == f"{base}/settings", "back to Setup as it was before Update, not to the page Update sent"
        page.wait_for_timeout(300)
    context.close()
    assert len(asked) == 1, asked
    assert stops == ([] if refused else [1])


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


# Windows high contrast (a Windows contrast theme): the page in the computer's own colors. What its Canvas (the
# background) and CanvasText (the words) are, and how each kind of row is drawn.
SYSTEM = """() => { const probe = document.createElement('span'); probe.style.cssText = 'background: Canvas; color: CanvasText';
  document.body.append(probe); const s = getComputedStyle(probe); const out = { canvas: s.backgroundColor, text: s.color };
  probe.remove(); return out; }"""
HC_ROWS = """() => [...document.querySelectorAll('.items .item')].map((li) => {
  const tag = li.querySelector('.item-level'), shown = tag && getComputedStyle(tag).display !== 'none';
  return { key: li.dataset.key, marked: ['imp-critical', 'imp-high', 'is-locked'].some((c) => li.classList.contains(c)),
           edge: getComputedStyle(li, '::before').backgroundColor, sel: li.classList.contains('is-sel'),
           outline: `${getComputedStyle(li).outlineStyle} ${getComputedStyle(li).outlineColor}`,
           word: shown ? tag.textContent : null, wordOutline: shown ? `${getComputedStyle(tag).outlineStyle} ${getComputedStyle(tag).outlineColor}` : null };
})"""
HC_CLASSIC = """() => ({
  rows: [...document.querySelectorAll('table.mail tbody tr[data-email]')].map((tr) => {
    const row = getComputedStyle(tr), pill = tr.querySelector('.pill:not(.item-level)');
    return { marked: tr.classList.contains('critical') || tr.classList.contains('high'), edge: row.borderLeftColor,
             width: row.borderLeftWidth, pill: pill ? getComputedStyle(pill).outlineStyle : null };
  }),
  focus: [...document.querySelectorAll('.focus-item')].map((li) => ({
    marked: ['imp-critical', 'imp-high', 'is-held', 'fraud'].some((c) => li.classList.contains(c)),
    width: getComputedStyle(li).borderLeftWidth, text: li.firstElementChild.getBoundingClientRect().left - li.getBoundingClientRect().left })),
})"""


def test_windows_high_contrast_still_marks_critical_high_and_held(browser, settings, loaded):
    """With a Windows contrast theme on (forced colors), the colored edges took the background's color, so nothing
    marked a Critical, High or held email; the classic Inbox lost its edges and its Importance pills their fill, and
    on classic Today every row had the same thick edge. With the importance words on, the words lost their outline:
    "Held for fraud" ran into the sender. They are drawn in the computer's own colors now, and Settings says the
    words tell the three apart there."""
    from controller_inbox import appearance

    context = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme="dark", forced_colors="active")
    context.set_default_timeout(15000)
    page = context.new_page()
    with serving(web.create_app(settings, loaded)) as base:
        page.request.post(f"{base}/digest/rebuild", headers={"origin": base})
        page.goto(f"{base}/app/all")
        page.wait_for_selector(".items .item")
        system = page.evaluate(SYSTEM)
        assert system["canvas"] != system["text"], system
        page.focus(".items")
        page.keyboard.press("j")
        page.wait_for_selector(".items .item.is-sel")
        rows = page.evaluate(HC_ROWS)
        assert {row["marked"] for row in rows} == {True, False}, rows
        for row in rows:
            assert (row["edge"] == system["text"]) is row["marked"], ("only Critical, High and held have the edge", row)
            if row["sel"]:
                assert not row["outline"].startswith("none"), ("the row picked with j and k is outlined", row)
        page.goto(f"{base}/app/digest")
        page.wait_for_selector(".page .plain-list li.is-locked")
        edges = page.evaluate(PLAIN_EDGES, DIGEST_EMAILS["workspace"])
        assert [row for row in edges if row["edge"]] and all(row["edge"] in (None, system["text"]) for row in edges), edges

        appearance.save(loaded, importance_words=True)
        page.goto(f"{base}/app/all")
        page.wait_for_selector(".items .item .item-level")
        rows = page.evaluate(HC_ROWS)
        assert "Held for fraud" in {row["word"] for row in rows}, rows
        for row in rows:
            assert row["wordOutline"] == f"solid {system['text']}", ("each word keeps its outline", row)
        appearance.save(loaded, importance_words=False)

        page.goto(f"{base}/")
        page.wait_for_selector(".focus-item")
        today = page.evaluate(HC_CLASSIC)["focus"]
        cards = page.evaluate(CLASSIC_LIST_EDGES, ".folder-card li:not(.muted)")
        assert [row for row in cards if row["edge"]] and all(row["edge"] in (None, system["text"]) for row in cards), cards
        page.goto(f"{base}/inbox")
        page.wait_for_selector("table.mail tbody tr[data-email]")
        inbox = page.evaluate(HC_CLASSIC)["rows"]
        assert {row["marked"] for row in inbox} == {True, False}, inbox
        for row in inbox:
            assert (row["edge"] == system["text"] and row["width"] == "3px") is row["marked"], ("the Inbox's edges", row)
            assert row["pill"] == "solid", ("the Importance pill keeps a box round its word", row)
        assert {row["marked"] for row in today} == {True, False}, today
        for row in today:
            assert row["width"] == ("4px" if row["marked"] else "1px"), ("classic Today: a thick edge for those only", row)
        assert len({row["text"] for row in today}) == 1, ("the words start in line", today)

        # Settings points to the words there, in both.
        page.goto(f"{base}/settings")
        assert "contrast theme" in page.inner_text("#colors")
        page.goto(f"{base}/app/settings")
        page.wait_for_selector("#set-importance-words")
        assert "contrast theme" in page.inner_text("#set-appearance")
    context.close()


SETUP_BOXES = """() => [...document.querySelectorAll('#colors input[name="colors"], #colors input[name="importance_words"]')]
  .map((box) => [box.name, box.checked])"""


@pytest.mark.parametrize("was", ["off", "on"])
@pytest.mark.parametrize("changed", ["colors", "importance_words"])
def test_back_to_classic_setup_shows_the_colors_as_they_are_and_save_keeps_them(browser, settings, loaded, changed, was):
    """Classic Setup, then the workspace's Settings in the same tab, a box ticked there, and Back. The page came again
    from CloseDesk with the new colors and its box ticked, but the browser put the box back as it had been left, as it
    does for a form: the page was in the color-blind colors with its box saying they were off. Save, even with nothing
    changed, then turned them off again."""
    from controller_inbox import appearance

    box = {"colors": "#set-colors", "importance_words": "#set-importance-words"}
    on = {"colors": "color-blind", "importance_words": True}
    off = {"colors": "standard", "importance_words": False}
    start = on if was == "on" else off
    appearance.save(loaded, **start)
    context = _context(browser, "light")
    page = context.new_page()
    with serving(web.create_app(settings, loaded)) as base:
        page.goto(f"{base}/settings")
        assert page.evaluate(SETUP_BOXES) == [["colors", was == "on"], ["importance_words", was == "on"]]
        page.goto(f"{base}/app/settings")
        page.wait_for_selector(box[changed])
        (page.uncheck if was == "on" else page.check)(box[changed])
        page.wait_for_selector("#set-appearance [data-status='colors'].ok")
        want = start | {changed: (off if was == "on" else on)[changed]}
        assert appearance.current(loaded) == want
        page.go_back()
        page.wait_for_url("**/settings")
        page.wait_for_selector("#colors form")
        shown = dict(page.evaluate(SETUP_BOXES))
        assert shown == {"colors": want["colors"] == "color-blind", "importance_words": want["importance_words"]}, shown
        page.click("#colors button[type='submit']")
        page.wait_for_url("**/settings?notice=*")
        assert appearance.current(loaded) == want, "Save with nothing changed on the page keeps what was chosen"
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


CHECK_BOXES = """() => [...document.querySelectorAll('#chat .msg-check')].map((box) => ({
  background: getComputedStyle(box).backgroundColor, edge: getComputedStyle(box).borderLeftColor,
  items: [...box.querySelectorAll('li')].map((li) => ({ text: li.innerText.trim(), sign: getComputedStyle(li, '::before').content,
                                                      color: getComputedStyle(li, '::before').color, bullet: getComputedStyle(li).listStyleType })) }))"""


@pytest.mark.parametrize("colors", ["standard", "color-blind"])
def test_the_classic_chat_shows_a_correction_in_the_green_of_done_apart_from_a_warning(settings, store, page, colors):
    """In the classic chat an answer's corrections were in the gold box of a note (#f3ecda, with a gold edge), 2.3
    apart from the new amber box of a figure to check (43° and 40° edges), and each correction had a plain bullet: the
    two were told apart by the warning sign alone, and the colors for color blindness ("done turns blue-green") left
    them as they were. As in the workspace, a correction has the tick, in the green of done, and so has its box; a box
    with a figure to check in it is amber."""
    from controller_inbox import appearance, chats
    from test_check_warnings_browser import CORRECTED, NOT_FOUND

    appearance.save(store, colors=colors)
    chat_id = chats.new_id()
    store.create_chat(chat_id, "Marketing")
    store.add_chat_turn(chat_id, "user", "How did Marketing change?")
    store.add_chat_turn(chat_id, "assistant", "Marketing went up by 31,500.", {"mode": "model", "sources": [], "checks": [CORRECTED]})
    store.add_chat_turn(chat_id, "user", "And travel?")
    store.add_chat_turn(chat_id, "assistant", "Travel is $12,480.", {"mode": "model", "sources": [], "checks": [CORRECTED, NOT_FOUND]})
    with serving(web.create_app(settings, store)) as base:
        page.goto(f"{base}/chat/window")
        page.evaluate("id => localStorage.setItem('closedesk-chat-id', id)", chat_id)
        page.reload()
        page.wait_for_function("() => document.querySelectorAll('#chat .msg-check').length === 2")
        corrected, warned = page.evaluate(CHECK_BOXES)
    tokens = pm.classic(colors)
    assert (corrected["background"], corrected["edge"]) == (rgb(tokens["--ok-soft"]), rgb(tokens["--green"])), corrected
    assert (warned["background"], warned["edge"]) == (rgb(tokens["--warn-soft"]), rgb(tokens["--warn"])), warned
    for item in [*corrected["items"], warned["items"][0]]:
        assert item["text"] == CORRECTED and "✓" in item["sign"] and item["color"] == rgb(tokens["--green"]), item
    (warning,) = warned["items"][1:]
    assert warning["text"] == NOT_FOUND and "⚠" in warning["sign"] and warning["color"] == rgb(tokens["--warn"]), warning
    assert {item["bullet"] for item in [*corrected["items"], *warned["items"]]} == {"none"}, "the sign in the bullet's place"


def test_the_classic_chats_fraud_warning_has_a_warning_sign(settings, store, page):
    """The red "Verify by phone before doing anything" bar was the one warning in a classic answer with no sign: the
    figures to check beneath it each have ⚠, and the workspace shows the same bar with its alert sign."""
    from controller_inbox import chats
    from controller_inbox.assistant import FRAUD_WARNING

    chat_id = chats.new_id()
    store.create_chat(chat_id, "Lakeside")
    store.add_chat_turn(chat_id, "user", "Are the Lakeside Tooling new bank details safe to use?")
    store.add_chat_turn(chat_id, "assistant", "Don't use them until you have called Lakeside.", {"mode": "lookup", "sources": [], "warning": FRAUD_WARNING})
    with serving(web.create_app(settings, store)) as base:
        page.goto(f"{base}/chat/window")
        page.evaluate("id => localStorage.setItem('closedesk-chat-id', id)", chat_id)
        page.reload()
        bar = page.wait_for_selector("#chat .msg-warn")
        shown = bar.evaluate("""(e) => { const sign = getComputedStyle(e, '::before');
          return { text: e.innerText, sign: sign.content, color: sign.color, words: getComputedStyle(e).color }; }""")
    assert "⚠" in shown["sign"] and shown["color"] == shown["words"], shown
    assert FRAUD_WARNING in shown["text"], shown


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
