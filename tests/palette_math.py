"""The colour maths behind tests/test_palette.py, and the stylesheets' colour tokens as the browser combines them.

* WCAG 2.x contrast, CIELAB (D65), CIEDE2000 (Sharma, Wu & Dalal 2005) and OKLCH.
* How a colour looks with protanopia, deuteranopia or tritanopia: the Machado, Oliveira & Fernandes (2009) matrices at
  severity 1.0, applied in linear RGB and clipped.
* The tokens each look sets: the workspace (static/ui/app.css) light or dark, with the standard colors or the colors
  for color blindness, and the classic pages (static/app.css), which are light only.

Only the standard library is used. `python tests/palette_math.py` prints every pair and every use, for the next person
who changes a colour.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "controller_inbox" / "static"
WORKSPACE_CSS = STATIC / "ui" / "app.css"
CLASSIC_CSS = STATIC / "app.css"

# The selectors of the blocks that set the tokens, exactly as the stylesheets write them.
ROOT = ":root"
DARK_SYSTEM = ':root:not([data-theme="light"])'  # inside @media (prefers-color-scheme: dark)
DARK_CHOSEN = ':root[data-theme="dark"]'
COLOR_BLIND = ':root[data-colors="color-blind"]'
COLOR_BLIND_DARK_SYSTEM = ':root[data-colors="color-blind"]:not([data-theme="light"])'  # inside the dark @media
COLOR_BLIND_DARK_CHOSEN = ':root[data-colors="color-blind"][data-theme="dark"]'

# Tokens that are the same in both themes: set by the light blocks only, never by a dark one.
BOTH_THEMES = re.compile(r"^--(paper-[\w-]+|toast-error)$")

# ---------------------------------------------------------------------------------------------------------------------
# Colour maths
# ---------------------------------------------------------------------------------------------------------------------


def hex_to_rgb(value: str) -> tuple[float, float, float]:
    value = value.strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    return tuple(int(value[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def rgb_to_hex(rgb) -> str:
    return "#" + "".join(f"{round(min(1.0, max(0.0, c)) * 255):02x}" for c in rgb)


def to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def to_srgb(c: float) -> float:
    c = min(1.0, max(0.0, c))
    return c * 12.92 if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


def linear(hex_value: str) -> tuple[float, float, float]:
    return tuple(to_linear(c) for c in hex_to_rgb(hex_value))  # type: ignore[return-value]


def from_linear(lin) -> str:
    return rgb_to_hex([to_srgb(c) for c in lin])


# Machado, Oliveira & Fernandes (2009), "A Physiologically-based Model for Simulation of Color Vision Deficiency",
# severity 1.0 (dichromacy). Applied to linear RGB.
MACHADO = {
    "protan": (
        (0.152286, 1.052583, -0.204868),
        (0.114503, 0.786281, 0.099216),
        (-0.003882, -0.048116, 1.051998),
    ),
    "deutan": (
        (0.367322, 0.860646, -0.227968),
        (0.280085, 0.672501, 0.047413),
        (-0.011820, 0.042940, 0.968881),
    ),
    "tritan": (
        (1.255528, -0.076749, -0.178779),
        (-0.078411, 0.930809, 0.147602),
        (0.004733, 0.691367, 0.303900),
    ),
}
VISIONS = ("normal", "protan", "deutan", "tritan")


def simulate(hex_value: str, vision: str) -> str:
    """The colour as someone with this vision sees it ("normal": as it is)."""
    if vision == "normal":
        return hex_value.lower()
    r, g, b = linear(hex_value)
    return from_linear([min(1.0, max(0.0, row[0] * r + row[1] * g + row[2] * b)) for row in MACHADO[vision]])


def luminance(hex_value: str) -> float:
    r, g, b = linear(hex_value)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    """WCAG 2.x contrast ratio, 1 to 21."""
    la, lb = luminance(a), luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


def lab(hex_value: str) -> tuple[float, float, float]:
    r, g, b = linear(hex_value)
    x = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = 0.0193339 * r + 0.1191920 * g + 0.9503041 * b

    def f(t: float) -> float:
        d = 6 / 29
        return t ** (1 / 3) if t > d**3 else t / (3 * d * d) + 4 / 29

    fx, fy, fz = f(x / 0.95047), f(y / 1.0), f(z / 1.08883)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def ciede2000(lab1, lab2) -> float:
    """CIEDE2000 colour difference (Sharma, Wu & Dalal 2005), with kL = kC = kH = 1."""
    l1, a1, b1 = lab1
    l2, a2, b2 = lab2
    cbar = (math.hypot(a1, b1) + math.hypot(a2, b2)) / 2
    g = 0.5 * (1 - math.sqrt(cbar**7 / (cbar**7 + 25**7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if (a1p or b1) else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if (a2p or b2) else 0.0
    dlp = l2 - l1
    dcp = c2p - c1p
    if c1p * c2p == 0:
        dhp = 0.0
    else:
        dhp = h2p - h1p
        if dhp > 180:
            dhp -= 360
        elif dhp < -180:
            dhp += 360
    dbig_hp = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dhp / 2))
    lbarp = (l1 + l2) / 2
    cbarp = (c1p + c2p) / 2
    if c1p * c2p == 0:
        hbarp = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hbarp = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        hbarp = (h1p + h2p + 360) / 2
    else:
        hbarp = (h1p + h2p - 360) / 2
    t = (
        1
        - 0.17 * math.cos(math.radians(hbarp - 30))
        + 0.24 * math.cos(math.radians(2 * hbarp))
        + 0.32 * math.cos(math.radians(3 * hbarp + 6))
        - 0.20 * math.cos(math.radians(4 * hbarp - 63))
    )
    dtheta = 30 * math.exp(-(((hbarp - 275) / 25) ** 2))
    rc = 2 * math.sqrt(cbarp**7 / (cbarp**7 + 25**7))
    sl = 1 + (0.015 * (lbarp - 50) ** 2) / math.sqrt(20 + (lbarp - 50) ** 2)
    sc = 1 + 0.045 * cbarp
    sh = 1 + 0.015 * cbarp * t
    rt = -math.sin(math.radians(2 * dtheta)) * rc
    return math.sqrt((dlp / sl) ** 2 + (dcp / sc) ** 2 + (dbig_hp / sh) ** 2 + rt * (dcp / sc) * (dbig_hp / sh))


def delta_e(a: str, b: str, vision: str = "normal") -> float:
    """How far apart two colours look to someone with this vision (CIEDE2000; about 2 is just noticeable)."""
    return ciede2000(lab(simulate(a, vision)), lab(simulate(b, vision)))


def oklch(hex_value: str) -> tuple[float, float, float]:
    """OKLCH: lightness 0-100, chroma, hue in degrees (red about 20-29, vermilion 35-40, orange 50-60, amber 70-85)."""
    r, g, b = linear(hex_value)
    lv = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    mv = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    sv = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_, m_, s_ = (math.copysign(abs(v) ** (1 / 3), v) for v in (lv, mv, sv))
    big_l = 0.2104542553 * l_ + 0.7936177850 * m_ - 0.0040720468 * s_
    a = 1.9779984951 * l_ - 2.4285922050 * m_ + 0.4505937099 * s_
    bb = 0.0259040371 * l_ + 0.7827717662 * m_ - 0.8086757660 * s_
    return big_l * 100, math.hypot(a, bb), math.degrees(math.atan2(bb, a)) % 360


def oklch_to_hex(big_l: float, chroma: float, hue: float) -> str | None:
    """Back from OKLCH; None when the colour is outside sRGB."""
    a = chroma * math.cos(math.radians(hue))
    b = chroma * math.sin(math.radians(hue))
    big_l /= 100
    l_ = big_l + 0.3963377774 * a + 0.2158037573 * b
    m_ = big_l - 0.1055613458 * a - 0.0638541728 * b
    s_ = big_l - 0.0894841775 * a - 1.2914855480 * b
    lv, mv, sv = l_**3, m_**3, s_**3
    rgb = (
        4.0767416621 * lv - 3.3077115913 * mv + 0.2309699292 * sv,
        -1.2684380046 * lv + 2.6097574011 * mv - 0.3413193965 * sv,
        -0.0041960863 * lv - 0.7034186147 * mv + 1.7076147010 * sv,
    )
    if any(c < -1e-4 or c > 1 + 1e-4 for c in rgb):
        return None
    return from_linear(rgb)


# ---------------------------------------------------------------------------------------------------------------------
# The stylesheets' tokens
# ---------------------------------------------------------------------------------------------------------------------


DARK_MEDIA = "(prefers-color-scheme: dark)"


def _rules(css: str, media: str = "") -> list[tuple[str, str, str]]:
    """(media, selector, body) for each rule, those inside an @media block with its condition."""
    out = []
    i = 0
    while True:
        opening = css.find("{", i)
        if opening < 0:
            return out
        prelude = css[i:opening].strip()
        depth, end = 1, opening + 1
        while depth:
            depth += {"{": 1, "}": -1}.get(css[end], 0)
            end += 1
        body = css[opening + 1 : end - 1]
        if prelude.startswith("@media"):
            out.extend(_rules(body, prelude[len("@media") :].strip()))
        else:
            out.append((media, " ".join(prelude.split()), body))
        i = end


def css_block(path: Path, selector: str, media: str = "") -> dict[str, str]:
    """The custom properties set by the one rule whose selector is exactly ``selector`` ({"--name": "value"}, values
    lower case): a rule at the top level, or inside ``@media <media>``. Fails when there is no such rule, or more than
    one, so renaming a selector can't skip a check."""
    css = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
    found = [body for where, name, body in _rules(css) if where == media and name == selector]
    assert found, f"no rule {selector} {{ … }} in {path.name}" + (f" inside @media {media}" if media else "")
    assert len(found) == 1, f"{len(found)} rules {selector} {{ … }} in {path.name}"
    return {name: value.strip().lower() for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+?)\s*(?:;|$)", found[0])}


def rule_colour(path: Path, selector: str, prop: str, tokens: dict[str, str]) -> str:
    """The colour a rule written on one line gives ``prop`` (a hex, or a token resolved with ``tokens``)."""
    for line in path.read_text(encoding="utf-8").split("\n"):
        if line.lstrip().startswith(selector + " ") or line.lstrip().startswith(selector + "{"):
            m = re.search(re.escape(prop) + r"\s*:\s*[^;]*?(#[0-9a-fA-F]{3,8}|var\(--[\w-]+\))", line)
            if m:
                value = m.group(1).lower()
                return tokens[value[4:-1]] if value.startswith("var(") else value
    raise AssertionError(f"no {selector} {{ {prop} }} in {path.name}")


def workspace(theme: str, colors: str = "standard") -> dict[str, str]:
    """The workspace's tokens in this theme ("light" or "dark") with these colors ("standard" or "color-blind"), as the
    cascade gives them: the colour-blind block comes after the dark ones with the same specificity, so it wins over
    them, and its dark blocks are more specific again."""
    tokens = dict(css_block(WORKSPACE_CSS, ROOT))
    if theme == "dark":
        tokens.update(css_block(WORKSPACE_CSS, DARK_CHOSEN))
    if colors == "color-blind":
        tokens.update(css_block(WORKSPACE_CSS, COLOR_BLIND))
        if theme == "dark":
            tokens.update(css_block(WORKSPACE_CSS, COLOR_BLIND_DARK_CHOSEN))
    return tokens


def classic(colors: str = "standard") -> dict[str, str]:
    """The classic pages' tokens, plus the colours a few rules write on their own that the checks need: the white of
    the Critical pill's words, and the rows' hover and open backgrounds."""
    tokens = dict(css_block(CLASSIC_CSS, ROOT))
    if colors == "color-blind":
        tokens.update(css_block(CLASSIC_CSS, COLOR_BLIND))
    tokens["--on-pill"] = rule_colour(CLASSIC_CSS, ".pill.critical", "color", tokens)
    tokens["--row-hover"] = rule_colour(CLASSIC_CSS, "table.mail tr:hover td", "background", tokens)
    tokens["--row-open"] = rule_colour(CLASSIC_CSS, "table.mail tr.is-open td", "background", tokens)
    return tokens


# Every look there is: (UI, colors, theme).
LOOKS = [
    ("workspace", "standard", "light"),
    ("workspace", "standard", "dark"),
    ("workspace", "color-blind", "light"),
    ("workspace", "color-blind", "dark"),
    ("classic", "standard", "light"),
    ("classic", "color-blind", "light"),
]


def tokens_of(ui: str, colors: str, theme: str) -> dict[str, str]:
    return workspace(theme, colors) if ui == "workspace" else classic(colors)


# What each meaning is called in each UI.
MEANINGS = {
    "workspace": {
        "critical": "--imp-critical",  # list edge and Critical pill
        "high": "--imp-high",  # list edge, the page's amber boxes
        "high-fill": "--imp-high-fill",  # High pill
        "danger": "--danger",
        "warn": "--warn",
        "ok": "--ok",
        "accent": "--accent",
        "medium": "--imp-medium",
    },
    "classic": {
        "critical": "--imp-critical",  # Critical pill and row edge
        "high": "--imp-high",  # High row edge, due-today edge
        "high-fill": "--imp-high-fill",  # High pill, due-today tag
        "danger": "--danger",
        "warn": "--warn",
        "ok": "--green",
        "accent": "--navy",
        "medium": "--imp-medium",
    },
}

# Pairs that carry meaning, which the colors for color blindness keep at least 20 apart for every vision: the two
# levels of one element (the list edges and pills, .tag-prio-critical against .tag-prio-high), the page's amber
# against its red boxes (high/danger), and each status against done.
KEY_PAIRS = {
    "workspace": [
        ("critical", "high"), ("critical", "high-fill"), ("high-fill", "medium"), ("danger", "warn"), ("high", "danger"),
        ("critical", "ok"), ("high", "ok"), ("danger", "ok"), ("warn", "ok"),
    ],
    "classic": [
        ("critical", "high"), ("critical", "high-fill"), ("high-fill", "medium"), ("danger", "warn"),
        ("critical", "ok"), ("high", "ok"), ("danger", "ok"), ("warn", "ok"),
    ],
}

WHITE = "#ffffff"

# Where each colour is used: (kind, foreground, backgrounds, where). Text needs 4.5:1; "ui" (an edge, a box, an
# icon) 3:1. A name starting "--" is a token; anything else is the colour itself.
ROLES = {
    "workspace": [
        ("ui", "--imp-critical", ["--surface", "--surface-2", "--selected"], "list edge .item.imp-critical::before"),
        ("ui", "--imp-high", ["--surface", "--surface-2", "--selected"], "list edge .item.imp-high::before"),
        ("ui", "--danger", ["--surface", "--selected"], "held mail edge .item.is-locked::before"),
        ("text", "--on-pill", ["--imp-critical"], "Critical pill .pill.imp-critical"),
        ("text", "--on-imp-high", ["--imp-high-fill"], "High pill .pill.imp-high"),
        ("text", "--on-pill", ["--imp-medium"], "Medium pill .pill"),
        ("text", "--on-danger", ["--danger"], "filled danger .tag-fraud, .btn-danger, .rail-count.danger"),
        ("text", "--danger", ["--surface", "--bg", "--surface-2", "--danger-soft"], "danger words .tag-prio-critical, .chip-danger, .item-when.late, .msg-warn"),
        ("text", "--warn", ["--surface", "--bg", "--surface-2", "--warn-soft"], "warning words .tag-prio-high, .chip-warn, .badge.warn, .warn-text"),
        ("text", "--ok", ["--surface", "--surface-2", "--ok-soft"], "done words .tag-ok, .badge.ok, .set-status.ok"),
        ("text", "--accent", ["--surface", "--accent-soft", "--selected"], "links, .tag-new_task, .rail-item.on"),
        ("text", "--text-2", ["--warn-soft"], "a chat check to look at .msg-check.warn li"),
        ("ui", "--warn", ["--warn-soft"], "its warning sign .msg-check li.warn .ic, .rd-alert.warn .ic"),
        ("ui", "--danger", ["--danger-soft"], "danger alert icon .rd-alert.danger > .ic"),
        ("ui", "--ok", ["--ok-soft", "--surface"], "tick .msg-check .ic, the model's dot"),
        ("ui", "--paper-ok", [WHITE], "the page's green boxes, on the white page in both themes .pv-box.ok"),
        ("ui", "--paper-check", [WHITE], "the page's amber boxes .pv-box.check"),
        ("ui", "--paper-differs", [WHITE], "the page's red boxes .pv-box.differs"),
        ("ui", "--paper-accent", [WHITE], "a fixed box, a box being drawn .pv-box.fixed, .pv-rubber"),
        ("text", WHITE, ["--paper-ok"], "a taught box's tag .pv-taught-tag"),
        ("text", WHITE, ["--toast-error"], "an error message .toast.error"),
    ],
    "classic": [
        ("ui", "--imp-critical", ["--card", "--row-hover", "--row-open"], "row edge table.mail tr.critical"),
        ("ui", "--imp-high", ["--card", "--row-hover", "--row-open"], "row edge table.mail tr.high, .focus-item.due_today"),
        ("text", "--on-pill", ["--imp-critical"], "Critical pill .pill.critical"),
        ("text", "--on-pill", ["--danger"], "white on danger .tag.fraud, .warn-box, .msg-warn, .flag.fraud_risk, .panel.alert"),
        ("text", "--on-imp-high", ["--imp-high-fill"], "High pill .pill.high, .tag.due_today"),
        ("text", "--ink", ["--imp-medium"], "Medium pill .pill.medium"),
        ("text", WHITE, ["--warn"], "warning flags .flag.duplicate_invoice, .flag.missing_attachment"),
        ("text", "--danger", ["--card", "--paper", "--danger-soft"], "danger words .kpis .hot, b.bad, .points.plus, .job-flag"),
        ("text", "--warn", ["--card", "--warn-soft"], "warning words .model-tip p, .model-state.fallback, .coding-state.unmatched"),
        ("text", "--green", ["--card", "--ok-soft"], "done words .model-state.on, .ai, .points.minus"),
        ("ui", "--warn", ["--warn-soft"], "a chat check to look at .msg-check li.warn::before"),
    ],
}


def colour(tokens: dict[str, str], name: str) -> str:
    return tokens[name] if name.startswith("--") else name


def pair_distances(ui: str, tokens: dict[str, str]) -> dict[tuple[str, str], dict[str, float]]:
    """Each meaning pair's distance for each vision."""
    meanings = MEANINGS[ui]
    return {
        (a, b): {vision: delta_e(tokens[meanings[a]], tokens[meanings[b]], vision) for vision in VISIONS}
        for a, b in KEY_PAIRS[ui]
    }


def role_contrasts(ui: str, tokens: dict[str, str]) -> list[tuple[str, str, str, float, float, str]]:
    """(kind, foreground, background, ratio, needed, where) for each use of each colour."""
    rows = []
    for kind, fg, bgs, where in ROLES[ui]:
        for bg in bgs:
            rows.append((kind, fg, bg, contrast(colour(tokens, fg), colour(tokens, bg)), 4.5 if kind == "text" else 3.0, where))
    return rows


def report() -> str:
    out = []
    for ui, colors, theme in LOOKS:
        tokens = tokens_of(ui, colors, theme)
        out.append(f"\n## {ui} / {colors} / {theme}\n")
        out.append("  meaning      hex      OKLCH hue")
        for name, token in MEANINGS[ui].items():
            out.append(f"  {name:<11}  {tokens[token]}  {oklch(tokens[token])[2]:5.1f}")
        out.append("\n  pair                   normal  protan  deutan  tritan     min")
        for (a, b), row in pair_distances(ui, tokens).items():
            out.append(f"  {a + '/' + b:<20}  " + "  ".join(f"{row[v]:6.1f}" for v in VISIONS) + f"  {min(row.values()):6.1f}")
        out.append("")
        for kind, fg, bg, ratio, need, where in role_contrasts(ui, tokens):
            mark = "ok  " if ratio >= need else "FAIL"
            out.append(f"  {mark} {ratio:5.2f} {kind:<4} {fg} on {bg}  {where}")
    return "\n".join(out)


if __name__ == "__main__":
    print(report())
