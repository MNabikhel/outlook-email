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


# Every rule that paints Critical, danger, High or a warning, and the token it takes its colour from. The checks above
# read the tokens; a rule written with a colour of its own (the old orange-red #c2361f on the classic Critical pill,
# or an orange on "Due today") passed them all, looked orange again for everyone and stopped changing with the colors
# for color blindness. (selector, property, token)
MEANING_RULES = {
    "classic": [
        ("table.mail tr.critical", "box-shadow", "--imp-critical"),
        ("table.mail tr.high", "box-shadow", "--imp-high"),
        (".pill.critical", "background", "--imp-critical"),
        (".pill.high", "background", "--imp-high-fill"),
        (".pill.item-level.critical", "color", "--danger"),
        (".pill.item-level.held", "color", "--danger"),
        (".pill.item-level.high", "color", "--warn"),
        (".tag.overdue", "background", "--danger"),
        (".tag.fraud", "background", "--danger"),
        (".tag.due_today", "background", "--imp-high-fill"),
        (".tag.caution", "color", "--warn"),
        (".flag.fraud_risk", "background", "--danger"),
        (".flag.do_not_process", "background", "--danger"),
        (".flag.duplicate_invoice", "background", "--warn"),
        (".flag.missing_attachment", "background", "--warn"),
        (".flag.payment_caution", "background", "--warn"),
        (".flag.bank_change_caution", "background", "--warn"),
        (".panel.alert", "background", "--danger"),
        (".panel.alert", "border-color", "--danger-deep"),
        (".focus-item.fraud", "border-left-color", "--danger"),
        (".focus-item.is-held", "border-left-color", "--danger"),
        (".focus-item.imp-critical", "border-left-color", "--imp-critical"),
        (".focus-item.imp-high", "border-left-color", "--imp-high"),
        (".fraud-panel.high", "border-left-color", "--danger"),
        (".job.error", "border-left", "--danger"),
        (".job .job-flag", "color", "--danger"),
        (".kpis .hot", "color", "--danger"),
        (".warn-box", "background", "--danger"),
        (".msg-warn", "background", "--danger"),
        (".msg-check li.warn::before", "color", "--warn"),
        (".msg-check.warn", "border-left-color", "--warn"),
        (".model-state.fallback", "color", "--warn"),
        (".coding-state.unmatched", "color", "--warn"),
        ("button.danger", "background", "--danger"),
    ],
    "workspace": [
        (".item.imp-critical::before", "background", "--imp-critical"),
        (".item.imp-high::before", "background", "--imp-high"),
        (".item.is-locked::before", "background", "--danger"),
        (".plain-list li.imp-critical::before", "background", "--imp-critical"),  # the daily digest's emails
        (".plain-list li.imp-high::before", "background", "--imp-high"),
        (".plain-list li.is-locked::before", "background", "--danger"),
        (".pill.imp-critical", "background", "--imp-critical"),
        (".pill.imp-high", "background", "--imp-high-fill"),
        (".tag-prio-critical", "color", "--danger"),
        (".tag-held", "color", "--danger"),
        (".tag-prio-high", "color", "--warn"),
        (".tag-fraud", "background", "--danger"),
        (".tag-overdue", "color", "--danger"),
        (".tag-due_today", "color", "--warn"),
        (".flag-fraud_risk", "color", "--danger"),
        (".flag-do_not_process", "color", "--danger"),
        (".flag-payment_caution", "color", "--warn"),
        (".flag-duplicate_invoice", "color", "--warn"),
        (".chip-danger", "color", "--danger"),
        (".chip-warn", "color", "--warn"),
        (".badge.warn", "color", "--warn"),
        (".rail-count.danger:not(.zero)", "background", "--danger"),
        (".btn-danger", "background", "--danger"),
        (".msg-warn", "color", "--danger"),
        (".msg-check li.warn .ic", "color", "--warn"),
        (".rd-alert.danger > .ic", "color", "--danger"),
        (".rd-alert.warn > .ic", "color", "--warn"),
        (".kpi.danger b", "color", "--danger"),
        (".kpi.warn b", "color", "--warn"),
        (":root .toast.error", "background", "--toast-error"),
    ],
}
REDS = {"--imp-critical", "--danger", "--danger-deep", "--toast-error"}


@pytest.mark.parametrize(("ui", "colors", "theme"), pm.LOOKS)
def test_the_rules_that_paint_critical_and_high_are_red_and_amber(ui, colors, theme):
    """Each rule takes its colour from its token, so it switches with the colors for color blindness, and the colour it
    paints is the red or the amber the tokens are checked for."""
    sheet, tokens = pm.SHEETS[ui], pm.tokens_of(ui, colors, theme)
    own = {
        f"{selector} {{ {prop}: {pm.rule_value(sheet, selector, prop)} }}": token
        for selector, prop, token in MEANING_RULES[ui]
        if f"var({token})" not in (pm.rule_value(sheet, selector, prop) or "")
    }
    assert not own, f"not on its token in {sheet.name}: {own}"
    off = {}
    for selector, prop, token in MEANING_RULES[ui]:
        hue = _hue(pm.rule_colour(sheet, selector, prop, tokens))
        red = token in REDS
        if not (-10 <= hue <= 25 if red else 70 <= hue <= (100 if colors == "color-blind" else 95)):
            off[f"{selector} {{ {prop} }}"] = (("red" if red else "amber"), round(hue, 1))
    assert not off, off


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


# What is drawn on a page's image, which is white paper in both themes: in the paper tones, which keep their light
# colours in dark mode. With the theme's own (var(--ok), var(--accent)) they were pastel on the white page in dark mode.
PAPER_RULES = [
    (".pv-taught", "border", "--paper-ok"),  # a field taught on the page
    (".pv-taught", "background", "--paper-ok"),
    (".pv-taught-tag", "background", "--paper-ok"),
    (".pv-rubber", "border", "--paper-accent"),  # the box drawn while teaching a field or marking a table
    (".pv-rubber", "background", "--paper-accent"),
]


@pytest.mark.parametrize(("colors", "theme"), [(colors, theme) for ui, colors, theme in pm.LOOKS if ui == "workspace"])
def test_what_is_drawn_on_a_page_is_in_the_paper_tones(colors, theme):
    """A taught field's outline and the box drawn while marking were in the dark theme's green and blue on the white
    page: 1.87:1 and 2.43:1 (1.64:1 with the colors for color blindness), under the 3:1 an outline needs."""
    tokens = pm.tokens_of("workspace", colors, theme)
    off = {
        f"{selector} {{ {prop} }}": pm.rule_value(pm.WORKSPACE_CSS, selector, prop)
        for selector, prop, token in PAPER_RULES
        if f"var({token})" not in (pm.rule_value(pm.WORKSPACE_CSS, selector, prop) or "")
    }
    assert not off, off
    for selector in (".pv-taught", ".pv-rubber"):
        edge = pm.rule_colour(pm.WORKSPACE_CSS, selector, "border", tokens)
        assert pm.contrast(edge, pm.WHITE) >= 3, (selector, edge, round(pm.contrast(edge, pm.WHITE), 2))


def test_meaning_colours_live_in_tokens():
    """The colours that switch with the palette are tokens, not written into the rules, so the colors for color
    blindness reach every one of them."""
    # With the old orange-red Critical (#c2361f, and the dark coral #ff7a66) and the old High (#d38a12, #f0ad45).
    retired = (
        "#b05a1f", "#c46b2d", "#8a4b12", "#9b2335", "#7a1b29", "#fbf1f2", "#fbeff1", "#8f1d14", "#2e7d4f", "#b26a00",
        "#c2361f", "#ff7a66", "#d38a12", "#f0ad45", "#b42318", "#9a5b0b", "#ff8f84", "#f1b75e",
    )
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

        assert rule(".tag.due_soon", "background") == rule(".tag.due_week", "background")
        assert rule(".tag.due_soon", "box-shadow") == rule(".tag.due_week", "box-shadow")
        pairs = {
            "the due-today tag / due later this week": (rule(".tag.due_today", "background"), rule(".tag.due_week", "box-shadow")),
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


# Why an email is on Today (focus_row's kinds in digest.py): said by its tag, never by its edge.
REASONS = ("overdue", "due_today", "due_soon", "due_week", "new_task", "decide")


def test_classic_todays_edge_says_only_the_importance_or_that_an_email_is_held():
    """The edge of a row on classic Today (and the digest) said its importance when it was Critical or High, and
    otherwise its reason, in the same colours: a Medium email due today had the amber of High, and an overdue one the
    red of danger, 2.2 from Critical's. Only Critical, High and a held email (or one to verify by phone) have a colored
    edge, as in the workspace; the reason is the tag's to say."""
    css = re.sub(r"/\*.*?\*/", "", pm.CLASSIC_CSS.read_text(encoding="utf-8"), flags=re.S)
    painted = {
        prelude: body.strip()
        for _media, prelude, body in pm._rules(css)
        if any(f".focus-item.{reason}" in selector for selector in pm._selectors(prelude) for reason in REASONS)
        and re.search(r"(?:^|;)\s*border(?:-left)?(?:-color)?\s*:", body)
    }
    assert not painted, painted
    for colors in ("standard", "color-blind"):
        tokens = pm.classic(colors)
        assert pm.rule_colour(pm.CLASSIC_CSS, ".focus-item", "border-left", tokens) == tokens["--line"]


def test_the_classic_chat_tells_a_correction_from_a_figure_to_check():
    """The classic chat showed a correction in the gold box of a note (#f3ecda, a gold edge), 2.3 from the amber box of
    a figure to check, with edges 3° apart. A correction is in the green of done, as in the workspace: its box and
    tick stay apart from the warning's, for everyone, and for every vision with the colors for color blindness."""
    for colors in ("standard", "color-blind"):
        tokens = pm.classic(colors)

        def rule(selector: str, prop: str) -> str:
            return pm.rule_colour(pm.CLASSIC_CSS, selector, prop, tokens)

        assert (rule(".msg-check", "background"), rule(".msg-check", "border-left")) == (tokens["--ok-soft"], tokens["--green"]), colors
        assert rule(".msg-check li::before", "color") == tokens["--green"], colors
        pairs = {
            "the edges": (rule(".msg-check", "border-left"), rule(".msg-check.warn", "border-left-color")),
            "the signs": (rule(".msg-check li::before", "color"), rule(".msg-check li.warn::before", "color")),
        }
        visions = pm.VISIONS if colors == "color-blind" else ("normal",)
        close = {
            name: {vision: round(pm.delta_e(a, b, vision), 1) for vision in visions}
            for name, (a, b) in pairs.items()
            if min(pm.delta_e(a, b, vision) for vision in visions) < 20
        }
        assert not close, (colors, close)
        fills = (rule(".msg-check", "background"), rule(".msg-check.warn", "background"))
        assert pm.delta_e(*fills, "normal") >= 8, (colors, fills)


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
