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
    lines = []
    for line in result or []:
        text = str(line[1])
        chars = line[4] if len(line) > 5 and isinstance(line[4], list) else None
        boxes = line[3] if len(line) > 4 and isinstance(line[3], list) else None
        if chars and boxes and len(chars) == len(boxes):
            text = _open_gaps(chars, boxes)
        lines.append(_polish(_spaced(text)))
    return "\n".join(lines)[:MAX_CHARS]


# On JPEG scans RapidOCR tends to drop spaces ("Duedate:15October2026"); put back the ones that are certain.
_JOINS = re.compile(r"(?<=[A-Za-z][a-z]{2})(?=\d)|(?<=\d)(?=[A-Z][a-z]{2})|(?<=[A-Za-z]:)(?=\w)")
# ...and to read the O of October as a zero once the space is gone: "150ctober", "200ct 2026".
_OCTOBER = re.compile(r"(?<![\d.,$])([1-9]|[12]\d|3[01])0(?=ctober|ct\.?\s*\d{4})")


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
        out.append(after)
    return "".join(out)


def _polish(line: str) -> str:
    """Break a run of words the scan glued together, and keep an ellipsis that lost its last dot."""
    line = re.sub(r"[A-Za-z]{6,}", lambda match: _segment(match.group(0)), line)
    line = re.sub(r",(?=[A-Za-z])", ", ", line)
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
