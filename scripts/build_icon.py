"""Draws CloseDesk's icon and writes the files made from it. This script is the icon's master: change the drawing
here and run it again, then commit what it wrote.

    python scripts/build_icon.py            write them into src/controller_inbox/static/
    python scripts/build_icon.py FOLDER     write them into FOLDER instead (tests/test_icon.py compares the two)

The icon is the mark CloseDesk's pages already show (static/icon.svg, the "CD" at the top of the sidebar): the two
letters in cream on CloseDesk's navy, in a rounded square. The letters are drawn as shapes, not typed in a font, so
the files come out the same on every computer. Each size is drawn for itself: the strokes and edges sit on whole
pixels (no blur at 32 pixels), and the smallest, 16 pixels, is drawn pixel by pixel.

What it writes:
- closedesk.ico: Windows (the shortcut on the Desktop and in the Start menu, the window and the taskbar). Sizes 16, 24,
  32, 48, 64 and 256, every one an uncompressed 32-bit frame: the window's icon is read by .NET, which has had trouble
  with the compressed (PNG) kind.
- closedesk.icns: macOS, the rounded square with the clear margin and soft shadow of a Mac app's icon (the window's
  Dock icon, and CloseDesk.app on macOS 15 and earlier).
- closedesk-full.icns: macOS 26 and later, which rounds an app's icon itself: the square filled to its edges, with no
  margin. A Mac icon with clear edges would be shrunk onto a grey tile there.
- closedesk.png: 512 pixels, for the guide and anywhere else a picture of the icon is wanted.
- icon.svg: the pages' own icon (the browser tab), the same mark.

Only Pillow is needed, which CloseDesk already depends on.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

STATIC = Path(__file__).resolve().parent.parent / "src" / "controller_inbox" / "static"

# CloseDesk's colours (static/ui/app.css: --brand and --on-brand). The navy is lit a little from the top.
NAVY = (0x1B, 0x36, 0x5D)
NAVY_TOP = (0x26, 0x4A, 0x7C)
NAVY_BOTTOM = (0x16, 0x2D, 0x4F)
CREAM = (0xF3, 0xE3, 0xBD)
# The classic pages' gold (static/app.css), for the double rule under the letters.
GOLD = (0xD4, 0xB0, 0x62)

ICO_SIZES = (16, 24, 32, 48, 64, 256)
# The rounded square's corner, as a share of its side (static/icon.svg's 7 of 32; a Mac icon's is a little more).
CORNER = 7 / 32
MAC_CORNER = 185 / 824
# A Mac icon (macOS 11 to 15): an 824-pixel square in the middle of 1024, its shadow below it.
MAC_BODY = 824 / 1024

# The letters, as shares of the square's side: their height, the thickness of their strokes, the opening of the C
# (degrees each side of its middle), the space between the C and the D, and the length of the D's straight part.
CAP = 0.36
STROKE = 0.088  # up to 64 pixels: a little heavier, to stay clear small
STROKE_LARGE = 0.08
OPENING = 42
SPACE = 0.052
D_FLAT = 0.105
# The double rule under the letters, as under a final total in a ledger: from 48 pixels up (below that it would be a
# smudge). Its lines' thickness and the space between them; the letters move up to make room.
RULE = 0.023
RULE_GAP = 0.02
RULE_FROM = 40
RULE_SPACE = 0.062

# 16 pixels: drawn by hand. "#" is the cream, "+" cream over half the navy, "." the navy.
SIXTEEN = """
................
................
................
................
..+####.#####...
..#####.######..
..##....##..##..
..##....##..##..
..##....##..##..
..##....##..##..
..#####.######..
..+####.#####...
................
................
................
................
""".split()


# The drawing ---------------------------------------------------------------------------------------------------


def _arc(cx: float, cy: float, r: float, start: float, end: float, steps: int = 180) -> list[tuple[float, float]]:
    """Points along a circle, from ``start`` to ``end`` degrees (0 is to the right, angles go clockwise on screen)."""
    return [
        (cx + r * math.cos(math.radians(start + (end - start) * i / steps)),
         cy + r * math.sin(math.radians(start + (end - start) * i / steps)))
        for i in range(steps + 1)
    ]


def letters(side: float, *, snap: bool) -> dict:
    """Where the letters (and the rule under them) go in a square of ``side`` pixels whose top-left corner is 0, 0.
    With ``snap``, the strokes and straight edges fall on whole pixels, so a small icon stays sharp."""
    def at(value: float) -> float:
        return float(round(value)) if snap else value

    cap = at(side * CAP)
    stroke = max(1.0, at(side * (STROKE if side <= 64 else STROKE_LARGE)))
    space = max(1.0, at(side * SPACE))
    flat = at(side * D_FLAT)
    radius = cap / 2
    # The C's width is to the tips of its opening.
    c_width = radius + radius * math.cos(math.radians(OPENING))
    d_width = flat + radius
    width = c_width + space + d_width
    left = at((side - width) / 2)
    rule = gap = below = 0.0
    if side >= RULE_FROM:
        rule, gap, below = max(1.0, at(side * RULE)), max(1.0, at(side * RULE_GAP)), at(side * RULE_SPACE)
    block = cap + (below + 2 * rule + gap if rule else 0)
    top = at((side - block) / 2)
    d_left = at(left + c_width + space)
    rules = []
    if rule:
        first = top + cap + below
        rules = [(left, first, at(left + width), first + rule), (left, first + rule + gap, at(left + width), first + 2 * rule + gap)]
    return {"cap": cap, "stroke": stroke, "radius": radius, "left": left, "top": top, "d_left": d_left, "flat": flat,
            "rules": rules}


def c_shape(g: dict) -> list[tuple[float, float]]:
    cx, cy = g["left"] + g["radius"], g["top"] + g["radius"]
    outer = _arc(cx, cy, g["radius"], OPENING, 360 - OPENING)
    inner = _arc(cx, cy, g["radius"] - g["stroke"], 360 - OPENING, OPENING)
    return outer + inner


def d_shapes(g: dict) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """The D's outline, and the hole in it."""
    x, top, cap, s, r = g["d_left"], g["top"], g["cap"], g["stroke"], g["radius"]
    bend = x + g["flat"]
    outline = [(x, top), (bend, top), *_arc(bend, top + r, r, -90, 90), (bend, top + cap), (x, top + cap)]
    hole = [(x + s, top + s), (bend, top + s), *_arc(bend, top + r, r - s, -90, 90), (bend, top + cap - s), (x + s, top + cap - s)]
    return outline, hole


def _gradient(width: int, height: int) -> Image.Image:
    """The navy, lit a little from the top."""
    column = Image.new("RGB", (1, height))
    for y in range(height):
        t = y / max(1, height - 1)
        column.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(NAVY_TOP, NAVY_BOTTOM)))
    return column.resize((width, height))


def _rounded(size: tuple[int, int], box: tuple[float, float, float, float], corner: float) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=corner, fill=255)
    return mask


def draw(size: int, *, body: float | None = None, corner: float = CORNER, full: bool = False, shadow: bool = False,
         snap: bool | None = None) -> Image.Image:
    """The icon at ``size`` pixels. ``body``: the rounded square's side, centred (the whole picture when not given);
    ``full``: no rounding and no margin (macOS 26 rounds it); ``shadow``: a Mac icon's soft shadow below it. It is
    drawn several times bigger and shrunk, which smooths the curves; whole-pixel edges stay sharp."""
    if size == 16 and body is None and not full:
        return sixteen()
    snap = size <= 64 if snap is None else snap
    fine = 8 if size <= 64 else 4 if size <= 256 else 2
    side = size if full or body is None else body
    offset = (size - side) / 2
    big = size * fine
    picture = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    if shadow:
        cast = _rounded((big, big), _box(offset, side, fine, down=side * 0.012), side * corner * fine)
        cast = cast.filter(ImageFilter.GaussianBlur(side * 0.02 * fine))
        picture.paste((0, 0, 0, 255), (0, 0), cast.point(lambda a: a * 0.32))
    square = Image.new("L", (big, big), 255) if full else _rounded((big, big), _box(offset, side, fine), side * corner * fine)
    navy = _gradient(big, big).convert("RGBA")
    picture.paste(navy, (0, 0), square)
    g = letters(side, snap=snap)
    mark = Image.new("L", (big, big), 0)
    pen = ImageDraw.Draw(mark)
    scaled = lambda points: [((offset + x) * fine, (offset + y) * fine) for x, y in points]  # noqa: E731
    pen.polygon(scaled(c_shape(g)), fill=255)
    outline, hole = d_shapes(g)
    pen.polygon(scaled(outline), fill=255)
    pen.polygon(scaled(hole), fill=0)
    picture.paste(CREAM + (255,), (0, 0), mark)
    if g["rules"]:
        lines = Image.new("L", (big, big), 0)
        for x0, y0, x1, y1 in g["rules"]:
            ImageDraw.Draw(lines).rectangle(
                ((offset + x0) * fine, (offset + y0) * fine, (offset + x1) * fine - 1, (offset + y1) * fine - 1), fill=255)
        picture.paste(GOLD + (255,), (0, 0), lines)
    return picture.reduce(fine)


def _box(offset: float, side: float, fine: int, *, down: float = 0.0) -> tuple[float, float, float, float]:
    return (offset * fine, (offset + down) * fine, (offset + side) * fine - 1, (offset + side + down) * fine - 1)


def sixteen() -> Image.Image:
    """The 16-pixel icon, drawn pixel by pixel on the rounded square: letters a computer would blur at this size."""
    # The square alone, then the letters set pixel by pixel.
    big = 16 * 8
    base = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    square = _rounded((big, big), (0, 0, big - 1, big - 1), 16 * CORNER * 8)
    base.paste(_gradient(big, big).convert("RGBA"), (0, 0), square)
    base = base.reduce(8)
    for y, row in enumerate(SIXTEEN):
        for x, cell in enumerate(row):
            if cell == ".":
                continue
            r, g, b, a = base.getpixel((x, y))
            share = 1.0 if cell == "#" else 0.5
            base.putpixel((x, y), tuple(round(c * (1 - share) + n * share) for c, n in zip((r, g, b), CREAM)) + (a,))
    return base


# The files -----------------------------------------------------------------------------------------------------


def ico(folder: Path) -> Path:
    frames = [draw(size) for size in ICO_SIZES]
    path = folder / "closedesk.ico"
    frames[-1].save(path, format="ICO", sizes=[(s, s) for s in ICO_SIZES], append_images=frames[:-1], bitmap_format="bmp")
    return path


def icns(folder: Path, *, full: bool) -> Path:
    if full:
        sizes = {size: draw(size, full=True) for size in (32, 64, 1024)}
    else:
        sizes = {size: draw(size, body=size * MAC_BODY, corner=MAC_CORNER, shadow=size >= 128) for size in (32, 64, 1024)}
    path = folder / ("closedesk-full.icns" if full else "closedesk.icns")
    sizes[1024].save(path, format="ICNS", append_images=[sizes[32], sizes[64]])
    return path


def png(folder: Path, size: int = 512) -> Path:
    path = folder / "closedesk.png"
    draw(size, body=size * 0.94).save(path, optimize=True)
    return path


def svg(folder: Path) -> Path:
    """The pages' icon (the browser tab): the same mark, as shapes, in a 32 by 32 square."""
    side = 32.0
    g = letters(side, snap=False)
    r, s = g["radius"], g["stroke"]
    cx, cy = g["left"] + r, g["top"] + r

    def point(angle: float, radius: float) -> str:
        return f"{cx + radius * math.cos(math.radians(angle)):.3f} {cy + radius * math.sin(math.radians(angle)):.3f}"

    c_path = (
        f"M{point(OPENING, r)}A{r:.3f} {r:.3f} 0 1 1 {point(-OPENING, r)}"
        f"L{point(-OPENING, r - s)}A{r - s:.3f} {r - s:.3f} 0 1 0 {point(OPENING, r - s)}Z"
    )
    x, top, cap, bend = g["d_left"], g["top"], g["cap"], g["d_left"] + g["flat"]
    d_path = (
        f"M{x:.3f} {top:.3f}H{bend:.3f}A{r:.3f} {r:.3f} 0 0 1 {bend:.3f} {top + cap:.3f}H{x:.3f}Z"
        f"M{x + s:.3f} {top + s:.3f}V{top + cap - s:.3f}H{bend:.3f}A{r - s:.3f} {r - s:.3f} 0 0 0 {bend:.3f} {top + s:.3f}Z"
    )
    hexed = lambda colour: "#%02x%02x%02x" % colour  # noqa: E731
    text = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        f'<defs><linearGradient id="navy" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{hexed(NAVY_TOP)}"/>'
        f'<stop offset="1" stop-color="{hexed(NAVY_BOTTOM)}"/></linearGradient></defs>'
        f'<rect width="32" height="32" rx="{side * CORNER:g}" fill="url(#navy)"/>'
        f'<path fill="{hexed(CREAM)}" fill-rule="evenodd" d="{c_path}{d_path}"/></svg>\n'
    )
    path = folder / "icon.svg"
    path.write_text(text, encoding="utf-8")
    return path


def build(folder: Path) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    return [ico(folder), icns(folder, full=False), icns(folder, full=True), png(folder), svg(folder)]


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else STATIC
    for made in build(out):
        print(f"wrote {made}")
