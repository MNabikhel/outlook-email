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
    #806c00, are at 96°)."""
    tokens = pm.tokens_of(ui, colors, theme)
    reds = {name: round(_hue(tokens[name]), 1) for name in ("--imp-critical", "--danger")}
    ambers = {name: round(_hue(tokens[name]), 1) for name in ("--imp-high", "--imp-high-fill", "--warn")}
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
DEFAULT_FLOORS = {
    ("workspace", "light"): {("critical", "high"): 18, ("danger", "warn"): 7},
    ("workspace", "dark"): {("critical", "high"): 14, ("danger", "warn"): 11},
    ("classic", "light"): {("critical", "high"): 14, ("danger", "warn"): 7},
}


@pytest.mark.parametrize(("ui", "theme"), list(DEFAULT_FLOORS))
def test_default_levels_stay_apart(ui, theme):
    """The standard colors: Critical and High (and danger and warnings) are far apart to everyone with normal vision.
    Today's were 21-30 apart, a red-orange against an orange."""
    tokens = pm.tokens_of(ui, "standard", theme)
    pairs = pm.pair_distances(ui, tokens)
    for pair in (("critical", "high"), ("danger", "warn")):
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
