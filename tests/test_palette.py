"""The colours that carry meaning, checked with numbers: Critical is a true red and High an amber, the levels stay apart
for people with color blindness (as simulated), and every use passes WCAG contrast. The maths and the tokens are read
by tests/palette_math.py; `python tests/palette_math.py` prints the whole report."""

from __future__ import annotations

import re

import pytest

import palette_math as pm


def test_the_colour_maths_matches_published_values():
    # Sharma, Wu & Dalal (2005), pairs 1, 7, 17 and 25.
    for one, two, want in (
        ((50.0, 2.6772, -79.7751), (50.0, 0.0, -82.7485), 2.0425),
        ((50.0, 0.0, 0.0), (50.0, -1.0, 2.0), 2.3669),
        ((50.0, 2.5, 0.0), (73.0, 25.0, -18.0), 27.1492),
        ((60.2574, -34.0099, 36.2677), (60.4626, -34.1751, 39.4387), 1.2644),
    ):
        assert pm.ciede2000(one, two) == pytest.approx(want, abs=1e-4)
    assert pm.contrast("#ffffff", "#000000") == pytest.approx(21)
    assert pm.contrast("#767676", "#ffffff") == pytest.approx(4.54, abs=0.01)
    for vision in pm.MACHADO:  # each matrix row sums to 1
        assert pm.simulate("#ffffff", vision) == "#ffffff" and pm.simulate("#000000", vision) == "#000000"
    # Pure red under protanopia: (0.152286, 0.114503, 0) in linear RGB by the first matrix column, a dark olive.
    assert pm.simulate("#ff0000", "protan") == "#6d5f00"
    for value in ("#c2361f", "#2557a7", "#17734a"):
        assert pm.oklch_to_hex(*pm.oklch(value)) == value


def test_each_theme_block_sets_the_same_tokens():
    css = pm.WORKSPACE_CSS
    dark_system, dark_chosen = pm.css_block(css, pm.DARK_SYSTEM, pm.DARK_MEDIA), pm.css_block(css, pm.DARK_CHOSEN)
    assert dark_system == dark_chosen, "the dark theme from the computer and the one chosen in the workspace differ"
    blind_system = pm.css_block(css, pm.COLOR_BLIND_DARK_SYSTEM, pm.DARK_MEDIA)
    blind_chosen = pm.css_block(css, pm.COLOR_BLIND_DARK_CHOSEN)
    assert blind_system == blind_chosen
    # The light colour-blind block wins over the default dark ones, so each dark colour-blind block must set every
    # token it sets, or a dark page would get a light colour; except the ones that are the same in both themes.
    light_blind = pm.css_block(css, pm.COLOR_BLIND)
    missing = {name for name in light_blind if not pm.BOTH_THEMES.match(name)} - set(blind_chosen)
    assert not missing, f"set for color blindness in light but not in dark: {sorted(missing)}"
    for block in (dark_system, blind_system):
        assert not [name for name in block if pm.BOTH_THEMES.match(name)], "the page's colours and the error message's are the same in both themes"
    assert {name for name in pm.css_block(css, pm.ROOT) if pm.BOTH_THEMES.match(name)} == {
        "--paper-ok", "--paper-check", "--paper-differs", "--paper-accent", "--toast-error",
    }


def _hue(value: str) -> float:
    """OKLCH hue from -180 to 180, so a red just under 0 reads as near 0."""
    hue = pm.oklch(value)[2]
    return hue - 360 if hue > 180 else hue


@pytest.mark.parametrize(("ui", "colors", "theme"), pm.LOOKS)
def test_critical_is_red_and_high_is_amber(ui, colors, theme):
    """Critical looked orange: #c2361f and the dark #ff7a66 are at 30-32°, where red turns to vermilion. High and
    warnings are an amber; with the colors for color blindness they may go on to a yellow (the light warning words,
    #806c00, are at 96°). In the workspace the red and amber boxes drawn on a page, and the error message, have
    colours of their own (the same in both themes), and they are the same red and amber."""
    tokens = pm.tokens_of(ui, colors, theme)
    own = ui == "workspace"
    reds = {name: round(_hue(tokens[name]), 1) for name in ("--imp-critical", "--danger", *(("--paper-differs", "--toast-error") if own else ()))}
    ambers = {name: round(_hue(tokens[name]), 1) for name in ("--imp-high", "--imp-high-fill", "--warn", *(("--paper-check",) if own else ()))}
    assert all(-10 <= hue <= 25 for hue in reds.values()), reds
    assert all(70 <= hue <= (100 if colors == "color-blind" else 95) for hue in ambers.values()), ambers


@pytest.mark.parametrize(("ui", "theme"), [("workspace", "light"), ("workspace", "dark"), ("classic", "light")])
def test_colour_blind_levels_stay_apart(ui, theme):
    """With the colors for color blindness, each pair that carries meaning is at least 20 apart (CIEDE2000) with
    normal vision and as protanopia, deuteranopia and tritanopia see it."""
    tokens = pm.tokens_of(ui, "color-blind", theme)
    close = {
        f"{a}/{b}": {vision: round(value, 1) for vision, value in row.items() if value < 20}
        for (a, b), row in pm.pair_distances(ui, tokens).items()
        if min(row.values()) < 20
    }
    assert not close, close


# The standard colors' smallest distance with simulated color blindness, rounded down: a change that brings two levels
# closer has to change this table on purpose.
# The boxes on a page are the same in both themes (it is white paper in dark mode too).
PAPER_FLOORS = {("paper-check", "paper-differs"): 21, ("paper-check", "paper-ok"): 17, ("paper-differs", "paper-ok"): 10}
DEFAULT_FLOORS = {
    ("workspace", "light"): {("critical", "high"): 18, ("danger", "warn"): 7, **PAPER_FLOORS},
    ("workspace", "dark"): {("critical", "high"): 14, ("danger", "warn"): 11, **PAPER_FLOORS},
    ("classic", "light"): {("critical", "high"): 14, ("danger", "warn"): 7},
}


@pytest.mark.parametrize(("ui", "theme"), list(DEFAULT_FLOORS))
def test_default_levels_stay_apart(ui, theme):
    """The standard colors: Critical and High (and danger and warnings) are far apart to everyone with normal vision.
    Today's were 21-30 apart, a red-orange against an orange."""
    tokens = pm.tokens_of(ui, "standard", theme)
    pairs = pm.pair_distances(ui, tokens)
    for pair in (("critical", "high"), ("danger", "warn"), *((("paper-check", "paper-differs"),) if ui == "workspace" else ())):
        assert pairs[pair]["normal"] >= 34, (pair, round(pairs[pair]["normal"], 1))
    for pair, floor in DEFAULT_FLOORS[(ui, theme)].items():
        assert min(pairs[pair].values()) >= floor, (pair, {v: round(d, 1) for v, d in pairs[pair].items()})
    if ui == "classic":
        # Medium and Low pills: a gold Medium sat too near the amber High; the blue one must not sit on top of Low.
        low = pm.rule_colour(pm.CLASSIC_CSS, ".pill.low", "background", tokens)
        assert pm.delta_e(tokens["--imp-medium"], low) >= 15


@pytest.mark.parametrize(("ui", "colors", "theme"), pm.LOOKS)
def test_every_colour_that_carries_meaning_passes_contrast(ui, colors, theme):
    tokens = pm.tokens_of(ui, colors, theme)
    failing = [
        f"{fg} on {bg}: {ratio:.2f} < {need} ({where})"
        for kind, fg, bg, ratio, need, where in pm.role_contrasts(ui, tokens)
        if ratio < need
    ]
    assert not failing, "\n".join(failing)


def test_meaning_colours_live_in_tokens():
    """The colours that switch with the palette are tokens, not written into the rules, so the colors for color
    blindness reach every one of them."""
    retired = ("#b05a1f", "#c46b2d", "#8a4b12", "#9b2335", "#7a1b29", "#fbf1f2", "#fbeff1", "#8f1d14", "#2e7d4f", "#b26a00")
    for path in (pm.WORKSPACE_CSS, pm.CLASSIC_CSS):
        text = path.read_text(encoding="utf-8").lower()
        assert not [value for value in retired if re.search(re.escape(value) + r"\b", text)], path.name
    # The model's dots and words on the navy: set in the tokens only (the side bar's Local model box kept the green
    # written into its rule, so with the colors for color blindness it differed from the Ask CloseDesk dot beside it).
    token_blocks = {pm.ROOT, pm.COLOR_BLIND}
    css = re.sub(r"/\*.*?\*/", "", pm.CLASSIC_CSS.read_text(encoding="utf-8"), flags=re.S).lower()
    written = {
        f"{selector} {{ {body.strip()} }}"
        for media, selector, body in pm._rules(css)
        if not (not media and selector in token_blocks)
        for value in ("#6fcf8a", "#d9b77c", "#48e0cf")
        if value in body
    }
    assert not written, written


def test_the_model_dot_on_ask_closedesk_follows_the_colors():
    """The classic Ask CloseDesk button's dot was a green written into its rule, so it stayed green with the colors
    for color blindness while the workspace's turned blue-green. It sits on the navy button, so it follows the
    workspace's dark theme colour for done."""
    for colors in ("standard", "color-blind"):
        dot = pm.rule_colour(pm.CLASSIC_CSS, ".chat-fab .dot.on", "background", pm.classic(colors))
        workspace = pm.workspace("dark", colors)["--ok"]
        apart = abs(pm.oklch(dot)[2] - pm.oklch(workspace)[2])
        assert min(apart, 360 - apart) <= 12, (colors, dot, workspace)
        # The side bar's Local model box shows the same thing on the same page, in the same colors.
        tokens = pm.classic(colors)
        assert pm.rule_colour(pm.CLASSIC_CSS, ".model-chip.on b::before", "background", tokens) == dot, colors
        ring = pm.rule_colour(pm.CLASSIC_CSS, ".chat-fab .dot", "box-shadow", tokens)
        assert pm.rule_colour(pm.CLASSIC_CSS, ".model-chip.off b", "color", tokens) == ring, colors


def test_the_classic_amber_stands_apart_from_the_colours_beside_it():
    """On the classic pages the new amber sat close to the old gold beside it: Today's "Due today" edge and tag
    against "Due Fri" in gold, the inbox's High pill against the khaki "Script draft" and "Needs model" flags on
    the same row, and on AP coding "Suggested" (a code to confirm) against "No code found" (needs a code), which
    became two beige pills with brown words, 1.5 apart. Before, Due today and High were an orange. Each pair is at
    least 20 apart for every vision."""
    for colors in ("standard", "color-blind"):
        tokens = pm.classic(colors)

        def rule(selector: str, prop: str) -> str:
            return pm.rule_colour(pm.CLASSIC_CSS, selector, prop, tokens, "--card")

        edge = "border-left-color"
        assert rule(".focus-item.due_soon", edge) == rule(".focus-item.due_week", edge)
        assert rule(".tag.due_soon", "background") == rule(".tag.due_week", "background")
        pairs = {
            "Today's due-today edge / due later this week": (rule(".focus-item.due_today", edge), rule(".focus-item.due_week", edge)),
            "due later this week / new, needs a look": (rule(".focus-item.due_week", edge), rule(".focus-item.decide", edge)),
            "due later this week / overdue": (rule(".focus-item.due_week", edge), rule(".focus-item.overdue", edge)),
            "due later this week / an email opened": (rule(".focus-item.due_week", edge), rule(".focus-item.is-open", "border")),
            "the due-today tag / due later this week": (rule(".tag.due_today", "background"), rule(".tag.due_week", "background")),
            "the High pill / the model's flags": (rule(".pill.high", "background"), rule(".flag.needs_model", "background")),
            "AP coding: Suggested / No code found": (rule(".coding-state.suggested", "background"), rule(".coding-state.unmatched", "background")),
            "AP coding: their words": (rule(".coding-state.suggested", "color"), rule(".coding-state.unmatched", "color")),
        }
        close = {
            name: {vision: round(pm.delta_e(a, b, vision), 1) for vision in pm.VISIONS}
            for name, (a, b) in pairs.items()
            if min(pm.delta_e(a, b, vision) for vision in pm.VISIONS) < 20
        }
        assert not close, (colors, close)


def test_a_payment_to_check_first_is_flagged_in_the_amber_of_a_warning():
    """The classic lists showed "Payment Caution" in the same plain beige as any other label, beside the red of a
    held email's flags; the workspace shows it as an amber "Check before paying", and the fraud check as an amber
    Caution."""
    for colors in ("standard", "color-blind"):
        tokens = pm.classic(colors)
        plain = pm.rule_colour(pm.CLASSIC_CSS, ".flag", "background", tokens)
        for flag in ("payment_caution", "bank_change_caution"):
            fill = pm.rule_colour(pm.CLASSIC_CSS, f".flag.{flag}", "background", tokens)
            assert fill == tokens["--warn"], (colors, flag, fill)
            assert min(pm.delta_e(fill, plain, vision) for vision in pm.VISIONS) >= 20, (colors, flag)
