"""PDFs drawn the ways real exporters draw them: letter-spaced text, one character at a time,
columns placed by position, bold headers, text drawn twice to look bold."""

from __future__ import annotations

from dataclasses import dataclass

# Helvetica's widths (per 1000 units of font size) for the characters these tests use.
_WIDTHS = {
    " ": 278, "!": 278, "$": 556, "%": 889, "&": 667, "(": 333, ")": 333, ",": 278, "-": 333, ".": 278, "/": 278, ":": 278,
    **{d: 556 for d in "0123456789"},
    "A": 667, "B": 667, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778, "H": 722, "I": 278, "J": 500, "K": 667,
    "L": 556, "M": 833, "N": 722, "O": 778, "P": 667, "Q": 778, "R": 722, "S": 667, "T": 611, "U": 722, "V": 667,
    "W": 944, "X": 667, "Y": 667, "Z": 611,
    "a": 556, "b": 556, "c": 500, "d": 556, "e": 556, "f": 278, "g": 556, "h": 556, "i": 222, "j": 222, "k": 500,
    "l": 222, "m": 833, "n": 556, "o": 556, "p": 556, "q": 556, "r": 333, "s": 500, "t": 278, "u": 556, "v": 500,
    "w": 722, "x": 500, "y": 500, "z": 500,
}


def width(text: str, size: float) -> float:
    return sum(_WIDTHS.get(ch, 556) for ch in text) * size / 1000


@dataclass
class Text:
    """``text`` drawn at (x, y). ``spacing``: extra points after every character (letter-spacing).
    ``glyphs``: each character placed on its own, words separated by position only.
    ``placed``: every character moved into place on its own (report writers, some bank statements).
    ``kern``: with ``glyphs``, this much extra room (thousandths of the size) between letters, as
    justified or tracked text is written.
    ``right``: x is where the text ends (right-aligned numbers).
    ``scale``: horizontal scaling in percent, how report writers fake a condensed font."""

    x: float
    y: float
    text: str
    size: float = 10
    bold: bool = False
    spacing: float = 0
    glyphs: bool = False
    placed: bool = False
    kern: float = 0
    right: bool = False
    twice: bool = False
    turn: int = 0
    scale: float = 100


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _ops(item: Text) -> list[str]:
    font = "/F2" if item.bold else "/F1"
    if item.turn:
        # Text turned on its side. 90 reads upward from y; -90 reads downward from y.
        matrix = "0 1 -1 0" if item.turn > 0 else "0 -1 1 0"
        return [f"BT {font} {item.size} Tf {item.spacing} Tc {matrix} {item.x:.2f} {item.y:.2f} Tm ({_escape(item.text)}) Tj ET"]
    x = item.x - width(item.text, item.size) - item.spacing * len(item.text) if item.right else item.x
    ops = [f"BT {font} {item.size} Tf {item.spacing} Tc 1 0 0 1 {x:.2f} {item.y} Tm"]
    if item.scale != 100:
        ops[0] = ops[0].replace(" Tc ", f" Tc {item.scale} Tz ")
    if item.placed:
        at = x
        for ch in item.text:
            if ch != " ":
                ops.append(f"1 0 0 1 {at:.2f} {item.y} Tm ({_escape(ch)}) Tj")
            at += width(ch, item.size) + item.spacing
    elif item.glyphs:
        parts = []
        for index, ch in enumerate(item.text):
            if ch == " ":
                parts.append(f"{-width(' ', item.size) * 1000 / item.size - item.kern:.0f}")
            else:
                if index and item.kern and item.text[index - 1] != " ":
                    parts.append(f"{-item.kern:.0f}")
                parts.append(f"({_escape(ch)})")
        ops.append(f"[{' '.join(parts)}] TJ")
    else:
        ops.append(f"({_escape(item.text)}) Tj")
    ops.append("ET")
    if item.twice:
        ops += [f"BT {font} {item.size} Tf {item.spacing} Tc 1 0 0 1 {x + 0.4:.2f} {item.y} Tm", f"({_escape(item.text)}) Tj", "ET"]
    return ops


def build_pdf(pages: list[list[Text]]) -> bytes:
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    kids = []
    for items in pages:
        stream = "\n".join(op for item in items for op in _ops(item)).encode("latin-1")
        page = len(objects) + 1
        kids.append(f"{page} 0 R")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {page + 1} 0 R "
            "/Resources << /Font << /F1 {regular} 0 R /F2 {bold} 0 R >> >> >>".encode()
        )
        objects.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    regular, bold = len(objects) + 1, len(objects) + 2
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode()
    objects = [o.replace(b"{regular}", str(regular).encode()).replace(b"{bold}", str(bold).encode()) for o in objects]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer << /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return out


def sheet_rows(columns: list[tuple[float, str]], rows: list[list[str]], *, top: float = 720, pitch: float = 15, size: float = 10) -> list[Text]:
    """A sheet saved as PDF: ``columns`` are (x, "left"|"right"); the first row is a bold header; "" is a blank cell."""
    items = []
    for r, row in enumerate(rows):
        for (x, align), value in zip(columns, row):
            if value:
                items.append(Text(x, top - r * pitch, value, size=size, bold=r == 0, right=align == "right" and r > 0))
    return items
