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


def test_the_boxes_on_a_page_show_on_its_white_paper_in_dark_mode(settings, store, page, monkeypatch):
    """The page is white paper in both themes, but its boxes took the dark theme's pastel colours: an amber box was
    1.95:1 on the white page. They keep the light colours, and an amber box has a dashed edge and a red one a ring."""
    email = scan_to_check(settings, store, monkeypatch)
    page.emulate_media(color_scheme="dark")
    with serving(web.create_app(settings, store)) as base:
        _open_page_tab(page, base, email)
        page.wait_for_selector(".pv-box.check")
        shown = page.evaluate(BOXES)
    assert shown["sheet"] == "rgb(255, 255, 255)"
    seen = {name: round(pm.contrast(hex_of(shown[name]), "#ffffff"), 2) for name in ("check", "differs", "ok")}
    assert all(ratio >= 3 for ratio in seen.values()), seen
    paper = pm.workspace("light")
    assert shown["check"] == rgb(paper["--paper-check"]) and shown["differs"] == rgb(paper["--paper-differs"])
    assert shown["ok"] == rgb(paper["--paper-ok"])
    assert shown["checkStyle"] == "dashed" and shown["okStyle"] == "solid"
    assert rgb(paper["--paper-differs"]) in shown["differsRing"]
