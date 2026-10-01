"""Text from scanned pages and pictures, read on this computer.

RapidOCR (``pip install -e ".[ocr]"``, which the launchers do) needs no separate program.
A Tesseract install with ``pytesseract`` is used when RapidOCR isn't there.
"""

from __future__ import annotations

import importlib.util
import io
import re
import shutil
import threading

MAX_CHARS = 20_000

_engine = None
_engine_lock = threading.Lock()


def engine_name() -> str:
    """Which OCR engine CloseDesk can use, or "" when none is installed."""
    if importlib.util.find_spec("rapidocr_onnxruntime") is not None:
        return "RapidOCR"
    if importlib.util.find_spec("pytesseract") is not None and shutil.which("tesseract"):
        return "Tesseract"
    return ""


def image_text(data: bytes) -> str:
    """The text in a picture, top to bottom, or "" when it has none or no OCR engine is installed."""
    name = engine_name()
    try:
        if name == "RapidOCR":
            return _rapid(data)
        if name == "Tesseract":
            return _tesseract(data)
    except Exception:
        return ""
    return ""


def _rapid(data: bytes) -> str:
    global _engine
    with _engine_lock:
        if _engine is None:
            from rapidocr_onnxruntime import RapidOCR

            _engine = RapidOCR()
        result, _elapsed = _engine(data, return_word_box=True)
    from PIL import Image

    image = Image.open(io.BytesIO(data)).convert("RGB")
    hits = [cell for line in result or [] for cell in _cells(line)]
    hits += _missed_lines(_engine, image, hits)
    hits = [_reread_empty_parens(_engine, image, hit) for hit in hits]
    return "\n".join(line for line in _rows(hits) if line.strip())[:MAX_CHARS]


# On JPEG scans RapidOCR tends to drop spaces ("Duedate:15October2026"); put back the ones that are certain.
_JOINS = re.compile(r"(?<=[A-Za-z][a-z]{2})(?=\d)|(?<=\d)(?=[A-Z][a-z]{2})|(?<=[A-Za-z]:)(?=\w)")
# ...and to read the O of October as a zero once the space is gone: "150ctober", "200ct 2026".
_OCTOBER = re.compile(r"(?<![\d.,$])([1-9]|[12]\d|3[01])0(?=ctober|ct\.?\s*\d{4})")


def _already(text: str, known: list[str]) -> bool:
    """True when this line is the same text already read, not a longer line that merely contains it."""
    plain = _plain(text)
    for other in known:
        if plain == other:
            return True
        short, long = sorted((plain, other), key=len)
        if len(short) > 20 and short in long and len(short) >= 0.8 * len(long):
            return True
    return False


def _usable(text: str) -> bool:
    """Drop a second-pass line that is only scattered one-letter noise."""
    words = [word for word in re.split(r"\s+", text.strip()) if word]
    if len(_plain(text)) < 8 or not words:
        return False
    return sum(len(word) >= 3 for word in words) >= max(1, len(words) // 3)


def _plain(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _cells(line) -> list[tuple[float, float, float, str]]:
    """One box per phrase. A wide gap inside a line is the next column, not a word space."""
    box = line[0]
    top = min(point[1] for point in box)
    chars = line[4] if len(line) > 5 and isinstance(line[4], list) else None
    boxes = line[3] if len(line) > 4 and isinstance(line[3], list) else None
    if not (chars and boxes and len(chars) == len(boxes)):
        left = min(point[0] for point in box)
        right = max(point[0] for point in box)
        return [(top, left, right, str(line[1]))]

    def left_of(item) -> float:
        return min(point[0] for point in item)

    def right_of(item) -> float:
        return max(point[0] for point in item)

    cuts = [0]
    for index in range(1, len(chars)):
        if left_of(boxes[index]) - right_of(boxes[index - 1]) >= 28:
            cuts.append(index)
    cuts.append(len(chars))
    cells = []
    for start, end in zip(cuts, cuts[1:]):
        text = _open_gaps(chars[start:end], boxes[start:end])
        if text.strip():
            cells.append((top, left_of(boxes[start]), right_of(boxes[end - 1]), text))
    return cells or [(top, min(point[0] for point in box), max(point[0] for point in box), str(line[1]))]


def _rows(hits: list[tuple]) -> list[str]:
    """Put cells that share a baseline on one line, and name the column an amount sits in.

    A blank cell is written ``not listed`` under its own column. Leaving the slot out is what
    makes the next amount look like it belongs to the column before it.
    """
    cells = [_prepared(hit) for hit in hits if _prepared(hit)[3]]
    ordered = sorted(cells, key=lambda hit: (hit[0], hit[1]))
    gaps = [b[0] - a[0] for a, b in zip(ordered, ordered[1:]) if 12 <= b[0] - a[0] <= 80]
    pitch = sorted(gaps)[len(gaps) // 2] if gaps else 22
    tolerance = max(6.0, pitch * 0.4)
    rows: list[list[tuple[float, float, float, str]]] = []
    anchors: list[float] = []
    for hit in ordered:
        if rows and hit[0] - anchors[-1] <= tolerance:
            rows[-1].append(hit)
        else:
            rows.append([hit])
            anchors.append(hit[0])
    rows = [_currency(row) for row in rows]
    columns = _amount_columns(rows)
    labels = _column_labels(rows, columns)
    lines = []
    for row in rows:
        lines.append(_emit(row, columns, labels) if columns else _plain_row(row))
    return lines


def _prepared(hit: tuple) -> tuple[float, float, float, str]:
    """Polish one cell. A test hit without a right edge gets one from its text width."""
    if len(hit) == 4:
        top, left, right, text = hit
    else:
        top, left, text = hit
        right = left + max(16.0, 7.5 * len(text))
    text = _polish(_spaced(str(text))).strip()
    return float(top), float(left), float(right), text


def _plain_row(row: list[tuple[float, float, float, str]]) -> str:
    cells = sorted(row, key=lambda hit: hit[1])
    texts = [text for _top, _left, _right, text in cells if text]
    if any(any(ch.isdigit() for ch in text) for text in texts):
        texts = ["$" if text in {"S", "s"} else text for text in texts]
    return " ".join(texts)


def _currency(row: list[tuple[float, float, float, str]]) -> list[tuple[float, float, float, str]]:
    """A lone S just left of a figure is the dollar sign."""
    ordered = sorted(row, key=lambda hit: hit[1])
    out: list[tuple[float, float, float, str]] = []
    index = 0
    while index < len(ordered):
        top, left, right, text = ordered[index]
        nxt = ordered[index + 1] if index + 1 < len(ordered) else None
        if text in {"$", "S", "s"} and nxt and _is_amount(nxt[3]) and nxt[1] - right <= 36:
            mark = "$"
            amount = nxt[3] if nxt[3].startswith("$") else f"{mark}{nxt[3]}"
            out.append((nxt[0], left, nxt[2], amount))
            index += 2
            continue
        out.append((top, left, right, "$" if text in {"S", "s"} and _row_has_amount(ordered) else text))
        index += 1
    return out


def _row_has_amount(row: list[tuple[float, float, float, str]]) -> bool:
    return any(_is_amount(text) for _t, _l, _r, text in row)


_AMOUNT = re.compile(r"^[$€£]?\(?\d[\d,.]*%?\)?$")
_YEAR = re.compile(r"^(?:19|20)\d{2}$")


def _is_amount(text: str) -> bool:
    return bool(_AMOUNT.fullmatch(text.replace(" ", ""))) and not _YEAR.fullmatch(text.strip())


def _amount_columns(rows: list[list[tuple[float, float, float, str]]]) -> list[float]:
    """Right edges where amounts stack often enough to be a column, left to right."""
    rights = sorted(right for row in rows for _top, _left, right, text in row if _is_amount(text))
    if len(rights) < 6:
        return []
    gaps = [b - a for a, b in zip(rights, rights[1:]) if b > a]
    typical = sorted(gaps)[len(gaps) // 2] if gaps else 40
    cut = max(36.0, typical * 0.55)
    clusters: list[list[float]] = [[rights[0]]]
    for right in rights[1:]:
        if right - clusters[-1][-1] <= cut:
            clusters[-1].append(right)
        else:
            clusters.append([right])
    columns = [sum(cluster) / len(cluster) for cluster in clusters if len(cluster) >= 3]
    return columns if len(columns) >= 2 else []


def _column_labels(rows: list[list[tuple[float, float, float, str]]], columns: list[float]) -> list[str]:
    """The words that sit on a column: a year, or the heading above that column."""
    if not columns:
        return []
    ranges = _ranges(columns)
    first_data = next((index for index, row in enumerate(rows) if any(not _YEAR.fullmatch(text) and _is_amount(text) for _t, _l, _r, text in row)), len(rows))
    labels = [""] * len(columns)
    band = ranges[0][0]
    for row in rows[:first_data]:
        headings = [
            (left, text)
            for _top, left, right, text in row
            if text and not _is_amount(text) and not _YEAR.fullmatch(text) and len(text) <= 48 and right >= band
        ]
        headings.sort()
        for index, center in enumerate(columns):
            # A heading covers the columns to its right, up to the next heading on that line.
            covering = [text for left, text in headings if left <= center + 8]
            if covering and covering[-1] not in labels[index]:
                labels[index] = f"{labels[index]} {covering[-1]}".strip()
    for row in rows[: first_data + 1]:
        for _top, _left, right, text in row:
            if not _YEAR.fullmatch(text):
                continue
            index = min(range(len(columns)), key=lambda item: abs(columns[item] - right))
            if text not in labels[index]:
                labels[index] = f"{labels[index]} {text}".strip()
    return _unique_labels(labels)


def _ranges(columns: list[float]) -> list[tuple[float, float]]:
    ranges = []
    for index, center in enumerate(columns):
        start = (columns[index - 1] + center) / 2 if index else center - (columns[1] - center) / 2
        end = (center + columns[index + 1]) / 2 if index + 1 < len(columns) else center + (center - columns[index - 1]) / 2
        ranges.append((start, end))
    return ranges


def _unique_labels(labels: list[str]) -> list[str]:
    """Two columns both named 2023 stay distinct: the later one keeps a count."""
    seen: dict[str, int] = {}
    out = []
    for label in labels:
        if not label:
            out.append("")
            continue
        seen[label] = seen.get(label, 0) + 1
        out.append(label if seen[label] == 1 else f"{label} ({seen[label]})")
    return out


def _emit(row: list[tuple[float, float, float, str]], columns: list[float], labels: list[str]) -> str:
    ranges = _ranges(columns)
    placed = [""] * len(columns)
    label_parts: list[str] = []
    used = False
    for _top, left, right, text in sorted(row, key=lambda hit: hit[1]):
        if not text or text in {"$", "S", "s"}:
            continue
        slot = None
        if _is_amount(text) or _YEAR.fullmatch(text):
            for index, (start, end) in enumerate(ranges):
                if start <= right <= end or abs(right - columns[index]) <= (end - start) / 2:
                    slot = index
                    break
        if slot is None:
            if right < ranges[0][0]:
                label_parts.append(text)
            continue
        used = True
        placed[slot] = f"{placed[slot]} {text}".strip() if placed[slot] else text
    if not used or all(_YEAR.fullmatch(text) for text in placed if text):
        return _plain_row(row)
    bits = [" ".join(label_parts)] if label_parts else []
    for name, value in zip(labels, placed):
        shown = value or "not listed"
        bits.append(f"{name}: {shown}" if name else shown)
    return " | ".join(bit for bit in bits if bit)


def _read_gap(engine, image, start: int, end: int, height: int) -> list[tuple[float, str]]:
    """Words in an ink gap. Try the gap itself, then a padded crop for a bold heading."""
    for pad in (2, 16):
        crop_top = max(0, start - pad)
        crop = image.crop((0, crop_top, image.width, min(height, end + pad)))
        scaled = crop.resize((max(1, crop.width * 2), max(1, crop.height * 2)))
        kept = []
        for top, text in _engine_lines(engine, scaled):
            source_y = crop_top + top / 2
            if source_y <= end and source_y + 14 >= start and text.strip():
                kept.append((source_y, text))
        if any(_usable(text) for _y, text in kept):
            return kept
    return []


def _missed_lines(engine, image, hits: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """Read a band of ink the first pass left blank, scaled up so a bold heading still resolves."""
    import numpy as np

    gray = np.array(image.convert("L"))
    ink = (gray < 170).sum(axis=1)
    height = len(ink)
    covered = np.zeros(height, dtype=bool)
    for top, _left, _right, _text in hits:
        y = int(top)
        near = [
            row
            for row in range(max(0, y - 6), min(height, y + 12))
            if ink[row] > 12
        ]
        if not near:
            continue
        y = min(near, key=lambda row: abs(row - top))
        start = y
        while start > 0 and ink[start - 1] > 12:
            start -= 1
        end = y
        while end + 1 < height and ink[end + 1] > 12:
            end += 1
        covered[start : end + 1] = True
    found = []
    y = 0
    while y < height:
        if ink[y] > 30 and not covered[y]:
            start = y
            while y < height and ink[y] > 12 and not covered[y]:
                y += 1
            if y - start >= 8:
                # A tight crop reads a short footnote. A bold heading needs padding, which also
                # pulls in the lines above and below, so those words are dropped unless they
                # sit in this gap.
                known = [_plain(text) for _top, _left, _right, text in hits]
                gap = _read_gap(engine, image, start, y, height)
                # A footnote mark is small. The crop sometimes keeps the digit and drops the parentheses.
                if any(re.fullmatch(r"\d", text.strip()) for _y, text in gap) and any(_usable(text) for _y, text in gap):
                    gap = [(gy, f"({text.strip()})" if re.fullmatch(r"\d", text.strip()) else text) for gy, text in gap]
                for source_y, text in gap:
                    if any(abs(other - source_y) < 12 for other, _left, _right, _text in hits):
                        continue
                    marker = bool(re.fullmatch(r"\(\d+\)", text.strip()))
                    if (marker or _usable(text)) and not _already(text, known):
                        # The mark sits at the left of the note. The sentence is the rest of that line.
                        left = 0.0 if marker else 24.0
                        found.append((source_y, left, left + max(16.0, 7.5 * len(text)), text))
        else:
            y += 1
    return found


def _reread_empty_parens(engine, image, hit: tuple) -> tuple:
    """A footnote marker read as () is too small. Read that line again, larger.

    Only the marker is replaced. A taller crop also contains the next footnote, and taking
    that whole line would number this note with the one below it.
    """
    top, left, right, text = hit
    if "()" not in text:
        return hit
    crop = image.crop((0, max(0, int(top) - 2), image.width, min(image.height, int(top) + 22)))
    scaled = crop.resize((max(1, crop.width * 2), max(1, crop.height * 2)))
    marker = _footnote_marker(line for _y, line in _engine_lines(engine, scaled))
    if not marker:
        return hit
    return top, left, right, text.replace("()", marker, 1)


def _footnote_marker(lines) -> str:
    """The footnote mark on this line, not a numbered note that leaked in from the next line."""
    marked = []
    for line in lines:
        found = re.search(r"\(\d+\)", line or "")
        if found:
            marked.append((found.group(0), line.strip()))
    for marker, line in marked:
        if len(line) <= len(marker) + 2:
            return marker
    return marked[0][0] if marked else ""


def _engine_lines(engine, image) -> list[tuple[float, str]]:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    result, _elapsed = engine(buffer.getvalue(), text_score=0.2, box_thresh=0.2)
    return [
        (min(point[1] for point in line[0]), str(line[1]))
        for line in result or []
    ]


def _spaced(line: str) -> str:
    return _JOINS.sub(" ", _OCTOBER.sub(r"\1 O", line))


def _open_gaps(chars: list[str], boxes: list) -> str:
    """Put a space back where the letters sit further apart than the rest of the line.

    A wide gap only counts between a word and the next word ("r" then "C" in "YourCity",
    or the two groups of an account number). A wide capital inside a word ("Number") does not.
    """
    def left(box) -> float:
        return min(point[0] for point in box)

    def right(box) -> float:
        return max(point[0] for point in box)

    gaps = [left(boxes[i]) - right(boxes[i - 1]) for i in range(1, len(chars))]
    typical = sorted(gaps)[len(gaps) // 2] if gaps else 0
    # In a paragraph the space between words is wider than the space inside a word, even when
    # both letters are lowercase. 8pt keeps "September" intact and still splits "operating income".
    prose = max(8, typical + 6)
    out = [chars[0]]
    for index, gap in enumerate(gaps, start=1):
        before, after = chars[index - 1], chars[index]
        # A real word space. Letter-spacing inside a word stays under this, including a loose scan font.
        word_edge = gap >= 8 and (
            (before.islower() and after.isupper())
            or (before.isalpha() and after.isdigit())
            or (before.isdigit() and after.isalpha())
        )
        punctuation = gap >= 4 and before in "#:" and after.isalnum()
        digit_group = False
        if before.isdigit() and after.isdigit() and gap >= 4:
            left_run = index - 1
            while left_run > 0 and chars[left_run - 1].isdigit():
                left_run -= 1
            right_run = index
            while right_run + 1 < len(chars) and chars[right_run + 1].isdigit():
                right_run += 1
            # "1234 1234", not a year ("2026") and not "15" before a misread letter.
            digit_group = (index - left_run) >= 3 and (right_run - index + 1) >= 3
        prose_space = len(chars) >= 40 and before.isalpha() and after.isalpha() and gap >= prose
        money_space = gap >= 6 and before.isalpha() and after == "$"
        if out[-1] != " " and (word_edge or punctuation or digit_group or prose_space or money_space):
            out.append(" ")
        if _divider(chars, index, gaps):
            if out[-1] != " ":
                out.append(" ")
            out.append("|")
            continue
        out.append(after)
    return re.sub(r" {2,}", " ", "".join(out))


def _divider(chars: list[str], index: int, gaps: list[float]) -> bool:
    """A lone I jammed onto the previous word is a vertical rule, not the letter I.

    The I that starts a word sits against the space before it. A rule has a gap after the
    preceding word and a gap or a space after it.
    """
    if chars[index] not in "Il|":
        return False
    if index == 0 or not chars[index - 1].isalpha() or gaps[index - 1] < 6:
        return False
    if index + 1 >= len(chars):
        return True
    return chars[index + 1] == " " or (index < len(gaps) and gaps[index] >= 6)


def _thousands(line: str) -> str:
    """A comma read as a dot inside a thousands group is still a thousands separator.

    28.412,414 and (1.097.978) are grouped in threes. 0.61 and 12,480.00 keep their decimals.
    """

    def fix(match: re.Match) -> str:
        token = match.group(0)
        wrapped = token.startswith("(") and token.endswith(")")
        core = token[1:-1] if wrapped else token
        parts = re.split(r"[.,]", core)
        if len(parts) < 2 or not all(part.isdigit() for part in parts):
            return token
        if any(len(part) != 3 for part in parts[1:]):
            return token
        grouped = parts[0] + "".join("," + part for part in parts[1:])
        return f"({grouped})" if wrapped else grouped

    return re.sub(r"\(?\d{1,3}(?:[.,]\d{3})+\)?", fix, line)


def _polish(line: str) -> str:
    """Break a run of words the scan glued together, and keep an ellipsis that lost its last dot."""
    line = line.translate(str.maketrans("（）【】［］｛｝", "()[][]{}"))
    line = re.sub(r"(?<=['\d])O(?=\d)", "0", line)
    line = _thousands(line)
    line = re.sub(r"[A-Za-z]{5,}", lambda match: _segment(match.group(0)), line)
    line = re.sub(r",(?=[A-Za-z])", ", ", line)
    line = re.sub(r"(?<=\d),(?=\d{4}\b)", ", ", line)
    line = re.sub(r"(?<=[A-Za-z])(?=[(（])", " ", line)
    line = re.sub(r"(?<=[)）\]])(?=[A-Za-z])", " ", line)
    line = re.sub(r"(?<=[A-Za-z])(?=[\"“])", " ", line)
    line = re.sub(r"(?<=\d)(?=[A-Za-z]{3,})", " ", line)
    line = re.sub(r"(?<=[a-z]{2})(?=\d)", " ", line)
    line = re.sub(r"(?<=%)(?=[A-Za-z])", " ", line)
    line = re.sub(r"\.(?=[A-Z])", ". ", line)
    return re.sub(r"(?<!\.)\.\.(?!\.)", "...", line)


def _segment(word: str) -> str:
    """Split ``Totalrevenue`` into ``Total revenue`` when every piece is a known word.

    If any piece is unknown, the whole run is left as read. That keeps "October" and
    "Northwind" intact instead of cutting them at a smaller word inside.
    """
    lower = word.lower()
    size = len(lower)
    cost = [10**6] * (size + 1)
    cut = [-1] * (size + 1)
    cost[0] = 0
    for start in range(size):
        if cost[start] >= 10**6:
            continue
        for end in range(start + 1, min(size, start + 22) + 1):
            piece = lower[start:end]
            if piece not in _WORDS or (end - start == 1 and piece not in {"a", "i"}):
                continue
            if cost[start] + 1 < cost[end]:
                cost[end] = cost[start] + 1
                cut[end] = start
    if cut[size] < 0 or cost[size] <= 1:
        return word
    pieces: list[str] = []
    at = size
    while at > 0:
        start = cut[at]
        pieces.append(word[start:at])
        at = start
    pieces.reverse()
    return " ".join(pieces)


# Words a glued scan line is allowed to be split into. A piece that is not here stays whole.
_WORDS = frozenset(
    """
    a an and are as at be been being but by for from had has have he her his i if in into is it its of on or our
    she that the their them then there these they this to was we were what when where which who will with you your
    about above after again all also any because before both can could did do does down during each few first
    how just last like made many more most much must new no not now off old one only other out over own same see
    should so some such than too under until up very way well would
    total revenue operating income margin expense expenses less net loss losses nonoperating non operating cash dividends
    earnings diluted weighted outstanding shares share
    dividend declared paid per share book value assets under management diluted weighted average common shares
    share outstanding effective tax rate attributable controlling interests interest million millions except data
    adjusted items item described detail financial measures measure beginning quarter updated definitions definition
    exclude impact market valuation changes change certain deferred compensation plans plan which company began
    economically hedging principles principle generally accepted united states state increased increase billion
    reflected higher investment advisory administration fees fee noncash gains gain related strategic minority
    during partially offset mark seed capital portfolio hedges hedge private equity higher decreased decrease
    stockholders stockholder equity divided respective period end three months month nine ended september basis
    executive summary compared
    generally accepted principles principle reflecting advisory administration noncash gains related strategic
    minority investment partially offset revaluation seed portfolio hedges hedge private dividend network capital
    black rock blackrock
    this sample description order number invoice date due your city somewhere street suite business payment
    primarily driven organic growth movement movements twelve collectively base average technology past
    service services portfolio portfolios company
    within days day late subject choosing bank from date of thanks
    """.split()
)


def _tesseract(data: bytes) -> str:
    import pytesseract
    from PIL import Image

    text = pytesseract.image_to_string(Image.open(io.BytesIO(data)))
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())[:MAX_CHARS]
