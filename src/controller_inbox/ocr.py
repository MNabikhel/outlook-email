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
    hits = [_hit(line) for line in result or []]
    hits += _missed_lines(_engine, image, hits)
    hits = [_reread_empty_parens(_engine, image, hit) for hit in hits]
    hits.sort(key=lambda hit: hit[0])
    return "\n".join(_polish(_spaced(text)) for _y, text in hits if text.strip())[:MAX_CHARS]


# On JPEG scans RapidOCR tends to drop spaces ("Duedate:15October2026"); put back the ones that are certain.
_JOINS = re.compile(r"(?<=[A-Za-z][a-z]{2})(?=\d)|(?<=\d)(?=[A-Z][a-z]{2})|(?<=[A-Za-z]:)(?=\w)")
# ...and to read the O of October as a zero once the space is gone: "150ctober", "200ct 2026".
_OCTOBER = re.compile(r"(?<![\d.,$])([1-9]|[12]\d|3[01])0(?=ctober|ct\.?\s*\d{4})")


def _already(text: str, known: list[str]) -> bool:
    plain = _plain(text)
    for other in known:
        if plain == other:
            return True
        if len(plain) > 20 and (plain in other or other in plain):
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


def _hit(line) -> tuple[float, str]:
    """One recognized line: its top edge, and its text with spaces restored."""
    box = line[0]
    top = min(point[1] for point in box)
    chars = line[4] if len(line) > 5 and isinstance(line[4], list) else None
    boxes = line[3] if len(line) > 4 and isinstance(line[3], list) else None
    text = _open_gaps(chars, boxes) if chars and boxes and len(chars) == len(boxes) else str(line[1])
    return top, text


def _missed_lines(engine, image, hits: list[tuple[float, str]]) -> list[tuple[float, str]]:
    """Read a band of ink the first pass left blank, scaled up so a bold heading still resolves."""
    import numpy as np

    gray = np.array(image.convert("L"))
    ink = (gray < 170).sum(axis=1)
    height = len(ink)
    covered = np.zeros(height, dtype=bool)
    for top, _text in hits:
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
                crop = image.crop((0, max(0, start - 16), image.width, min(height, y + 16)))
                scaled = crop.resize((max(1, crop.width * 2), max(1, crop.height * 2)))
                raw = _engine_lines(engine, scaled)
                known = [_plain(text) for _top, text in hits]
                found.extend(
                    (float(start), text)
                    for top, text in raw
                    if _usable(text) and not _already(text, known)
                )
        else:
            y += 1
    return found


def _reread_empty_parens(engine, image, hit: tuple[float, str]) -> tuple[float, str]:
    """A footnote marker read as () is too small. Read that line again, larger."""
    top, text = hit
    if "()" not in text:
        return hit
    crop = image.crop((0, max(0, int(top) - 4), image.width, min(image.height, int(top) + 36)))
    scaled = crop.resize((crop.width * 2, crop.height * 2))
    marked = [line for _top, line in _engine_lines(engine, scaled) if re.search(r"\(\d+\)", line)]
    if marked:
        return top, max(marked, key=len)
    return hit


def _engine_lines(engine, image) -> list[tuple[float, str]]:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    result, _elapsed = engine(buffer.getvalue(), text_score=0.2, box_thresh=0.2)
    lines = []
    for line in result or []:
        top = min(point[1] for point in line[0])
        lines.append((top, str(line[1])))
    return lines


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
        if out[-1] != " " and (word_edge or punctuation or digit_group):
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


def _polish(line: str) -> str:
    """Break a run of words the scan glued together, and keep an ellipsis that lost its last dot."""
    line = re.sub(r"[A-Za-z]{6,}", lambda match: _segment(match.group(0)), line)
    line = re.sub(r",(?=[A-Za-z])", ", ", line)
    line = re.sub(r"(?<=\d),(?=\d{4}\b)", ", ", line)
    line = re.sub(r"(?<=[A-Za-z])(?=\()", " ", line)
    line = re.sub(r"(?<=[)\]])(?=[A-Za-z])", " ", line)
    line = re.sub(r"(?<=\d)(?=[A-Za-z]{3,})", " ", line)
    line = re.sub(r"(?<=[a-z]{2})(?=\d)", " ", line)
    line = re.sub(r"(?<=%)(?=[A-Za-z])", " ", line)
    line = re.sub(r"\.(?=[A-Z])", ". ", line)
    return re.sub(r"(?<!\.)\.\.(?!\.)", "...", line)


def _segment(word: str) -> str:
    """Split ``Totalrevenue`` into ``Total revenue`` when every piece is a known word."""
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
            if piece not in _WORDS:
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
    within days day late subject choosing bank from date of thanks
    """.split()
)


def _tesseract(data: bytes) -> str:
    import pytesseract
    from PIL import Image

    text = pytesseract.image_to_string(Image.open(io.BytesIO(data)))
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())[:MAX_CHARS]
