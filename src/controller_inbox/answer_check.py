"""Check a chat answer's numbers and page/cell citations against what the model was given.

Small local models copy most figures correctly but get arithmetic and page numbers wrong
now and then. After the answer is written:

- a number that isn't in the emails, files, tool results or question is flagged, and when
  the sentence says it was worked out from the figures beside it, it is recomputed and
  corrected. A number that is in that material is never changed;
- "page N" or a cell like Summary!B12 or "cell B12" is corrected when the figures in that
  sentence are not on the cited page or in the cited cell but on one other page or cell.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from itertools import permutations

NUMBER_RE = re.compile(
    r"(?<![\w.,\[])(?P<cur>[$€£])?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<suf>%| ?percent\b| ?(?:k|K|m|M|bn)\b| (?:thousand|million|billion)\b)?(?![\w\]])"
)
_SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "bn": 1e9, "billion": 1e9}
_REFERENCE_BEFORE = re.compile(r"(?:\bpages?|\bp\.|\bsections?|\brows?|\bsheets?|\bsteps?|\bitems?|\bfinding|#|\bno\.)\s*$", re.I)
_SENTENCE_RE = re.compile(r"(?:[^\n.!?]|[.!?](?!\s|$))+(?:[.!?]+|\n|$)")
PAGE_RE = re.compile(r"\b(?P<word>pages?|p\.)\s?(?P<page>\d{1,4})\b", re.I)
CELL_RE = re.compile(r"(?:(?P<sheet>'[^'\n]+'|\"[^\"\n]+\"|[A-Za-z][\w]*)!)?\b(?P<cell>[A-Z]{1,3}\d{1,6})\b")
_PAGE_SPLIT = re.compile(r"^\[page (\d+)\]\s*$", re.M)
_SHEET_SPLIT = re.compile(r'^\[sheet "([^"]+)"[^\]]*\]\s*$', re.M)
_CELL_VALUE = re.compile(r"(?:^|\| )([A-Z]{1,3}\d{1,6})(?: \((?:[^()\n]|\([^()\n]*\))*\))?: ([^|\n]*)")
_WORKED_OUT_BEFORE = re.compile(
    r"\b(?:increase|decrease|difference|change|total|sum|gap|variance|rise|drop|growth|up|down|rose|fell|grew|shrank|"
    r"more|less|higher|lower)\b(?: \w+){0,2}\s*(?:of|by|is|was|=|:)?\s*[−-]?$",
    re.I,
)
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_MONTH_AFTER = re.compile(r"^(?:st|nd|rd|th)?\s+(?:of\s+)?" + _MONTH + r"\b", re.I)
_MONTH_BEFORE = re.compile(r"\b" + _MONTH + r"\s+$", re.I)
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z-]{5,}")
_ISO_DATE = re.compile(r"(?<!\d)\d{4}-\d{1,2}-\d{1,2}(?!\d)")
_RELATIVE = 0.15


@dataclass
class Number:
    shown: str
    value: float
    tolerance: float
    start: int
    end: int
    percent: bool = False
    money: bool = False


@dataclass
class Review:
    text: str
    checks: list[str] = field(default_factory=list)

    def changed(self, original: str) -> bool:
        return self.text != original


def numbers_in(text: str) -> list[Number]:
    found = []
    for match in NUMBER_RE.finditer(text):
        digits = match["num"].replace(",", "")
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        suffix = (match["suf"] or "").strip().lower()
        scale = _SCALE.get(suffix, 1.0)
        value = float(digits) * scale
        found.append(
            Number(
                shown=match.group(0),
                value=value,
                tolerance=0.5 * 10 ** (-decimals) * scale,
                start=match.start(),
                end=match.end(),
                percent=suffix in {"%", "percent"},
                money=bool(match["cur"]),
            )
        )
    return found


class Grounding:
    """Every number in the material the model was given, for quick "is this figure there?" checks."""

    def __init__(self, texts: list[str]):
        values = set()
        for text in texts:
            for number in numbers_in(text or ""):
                values.add(number.value)
        self.values = sorted(values)

    def has(self, number: Number) -> bool:
        low, high = number.value - number.tolerance, number.value + number.tolerance
        if self._any(low, high):
            return True
        return number.percent and self._any(low / 100, high / 100)

    def _any(self, low: float, high: float) -> bool:
        index = bisect.bisect_left(self.values, low - 1e-9)
        return index < len(self.values) and self.values[index] <= high + 1e-9


def _is_claim(text: str, number: Number) -> bool:
    """Figures worth checking: money, percentages, decimals, and whole numbers from 10 up that aren't years or references."""
    if _REFERENCE_BEFORE.search(text[max(0, number.start - 12): number.start]):
        return False
    if any(m.start() <= number.start and number.end <= m.end() for m in _ISO_DATE.finditer(text, max(0, number.start - 8), number.end + 6)):
        return False  # a piece of a date such as 2026-10-05, not a figure
    if number.value <= 31 and not (number.money or number.percent) and (
        _MONTH_AFTER.search(text[number.end: number.end + 16]) or _MONTH_BEFORE.search(text[max(0, number.start - 12): number.start])
    ):
        return False
    line_start = text.rfind("\n", 0, number.start) + 1
    if not text[line_start: number.start].strip(" -*•") and text[number.end: number.end + 1] in {".", ")"}:
        return False
    if number.money or number.percent or "." in number.shown or "," in number.shown:
        return True
    return number.value >= 10 and not 1900 <= number.value <= 2100


def _format_like(value: float, like: Number) -> str:
    shown = like.shown
    decimals = len(re.sub(r"[^\d.]", "", shown).split(".")[1]) if "." in re.sub(r"[^\d.]", "", shown) else 0
    suffix = re.search(r"(%| ?percent| ?(?:k|K|m|M|bn)| (?:thousand|million|billion))$", shown)
    scale = _SCALE.get(suffix.group(1).strip().lower(), 1.0) if suffix else 1.0
    if like.percent and round(value, decimals) != round(value, max(decimals, 1)):
        decimals = max(decimals, 1)
    body = f"{value / scale:,.{decimals}f}" if "," in shown or value / scale >= 10_000 else f"{value / scale:.{decimals}f}"
    return (shown[0] if shown[0] in "$€£" else "") + body + (suffix.group(1) if suffix else "")


def _worked_out(target: Number, operands: list[Number]) -> tuple[float, str] | None:
    """The one sum, difference or percentage of the sentence's own figures that ``target`` was meant to be, if clear."""
    near: dict[float, str] = {}
    exact = False
    pairs = list(permutations(operands[:6], 2))
    for x, y in pairs:
        candidates = [(x.value + y.value, f"{x.shown} + {y.shown}"), (x.value - y.value, f"{x.shown} − {y.shown}")]
        if y.value:
            if target.percent:
                candidates += [
                    (100 * x.value / y.value, f"{x.shown} ÷ {y.shown}"),
                    (100 * (x.value - y.value) / y.value, f"({x.shown} − {y.shown}) ÷ {y.shown}"),
                ]
        for value, how in candidates:
            if value <= 0 and target.value > 0:
                continue
            if abs(value - target.value) <= target.tolerance + 1e-9:
                exact = True
            elif abs(value - target.value) <= _RELATIVE * max(abs(target.value), 1e-9):
                near.setdefault(round(value, 6), how)
    if exact or len(near) != 1:
        return None
    value, how = next(iter(near.items()))
    return value, how


def check_numbers(answer: str, grounding: Grounding) -> Review:
    text = answer
    checks: list[str] = []
    for sentence in reversed(list(_sentences(answer))):
        start, chunk = sentence
        numbers = [n for n in numbers_in(chunk) if _is_claim(chunk, n)]
        grounded = [n for n in numbers if grounding.has(n)]
        for number in reversed(numbers):
            # A figure that is in what the model read is never rewritten, even beside words like "total":
            # "$10,800 total from $9,000 and $1,750" may leave out a fee the file adds.
            if number in grounded:
                continue
            # Only a figure the sentence says was worked out ("an increase of", "rose", "total") is recomputed.
            worked_out = bool(_WORKED_OUT_BEFORE.search(chunk[max(0, number.start - 30): number.start]))
            fix = _worked_out(number, [n for n in grounded if n is not number]) if worked_out else None
            if fix is None:
                fix = _list_total(answer, start + number.start, number, grounding)
                if fix == "matches":
                    continue
            if fix is not None:
                value, how = fix
                right = _format_like(value, number)
                if right != number.shown:
                    at = start + number.start
                    text = text[:at] + right + text[start + number.end:]
                    checks.insert(0, f"Corrected {number.shown} to {right} ({how}).")
                    continue
            if number not in grounded and not any(_derivable(number, grounded)):
                checks.insert(0, f"{number.shown} isn't in the emails or files I read; check it before relying on it.")
    return Review(text=text, checks=checks)


_TOTAL_LINE = re.compile(r"^[\s*_#>|-]*(?:grand\s+)?total\b[^\n]*$", re.I)
_ITEM_LINE = re.compile(r"^\s*(?:[-*•]|\d{1,2}[.)])\s+")
_MAX_ITEMS = 8


def _list_total(answer: str, at: int, target: Number, grounding: Grounding):
    """A "Total" line under a list of amounts: its total is the sum of the amounts listed above it.

    Returns "matches" when the figure is the sum of some of them (the model may have meant only those),
    the sum of all of them with how it was worked out when it is none of those sums, and None when there is
    no such list, or one of its amounts wasn't in what was read."""
    line_start = answer.rfind("\n", 0, at) + 1
    if not _TOTAL_LINE.match(answer[line_start:at]):
        return None
    lines = answer[:line_start].split("\n")[:-1]
    while lines and not lines[-1].strip():
        lines.pop()
    amounts: list[Number] = []
    while lines and _ITEM_LINE.match(lines[-1]):
        line = lines.pop()
        body = line[_ITEM_LINE.match(line).end():]
        figures = [n for n in numbers_in(body) if (n.money if target.money else ("," in n.shown or "." in n.shown))]
        if not figures or not grounding.has(figures[0]):
            return None
        amounts.insert(0, figures[0])
    if not 2 <= len(amounts) <= _MAX_ITEMS:
        return None
    values = [n.value for n in amounts]
    for mask in range(1, 2 ** len(values)):
        if abs(sum(v for i, v in enumerate(values) if mask >> i & 1) - target.value) <= target.tolerance + 1e-9:
            return "matches"
    return sum(values), f"the sum of the {len(values)} amounts listed above it"


def _derivable(target: Number, operands: list[Number]):
    for x, y in permutations(operands[:6], 2):
        for value in (x.value + y.value, x.value - y.value, 100 * x.value / y.value if y.value else None,
                      100 * (x.value - y.value) / y.value if y.value else None):
            if value is not None and abs(value - target.value) <= target.tolerance + 1e-9:
                yield True
                return
    if len(operands) >= 3 and abs(sum(n.value for n in operands) - target.value) <= target.tolerance + 1e-9:
        yield True


def _sentences(text: str):
    for match in _SENTENCE_RE.finditer(text):
        if match.group(0).strip():
            yield match.start(), match.group(0)


# Citations ------------------------------------------------------------------------------------


def pages_of(text: str) -> dict[int, str]:
    pieces = _PAGE_SPLIT.split(text or "")
    return {int(pieces[i]): pieces[i + 1] for i in range(1, len(pieces) - 1, 2)}


def cells_of(text: str) -> dict[tuple[str, str], str]:
    cells: dict[tuple[str, str], str] = {}
    pieces = _SHEET_SPLIT.split(text or "")
    for i in range(1, len(pieces) - 1, 2):
        sheet = pieces[i]
        for line in pieces[i + 1].splitlines():
            for cell, value in _CELL_VALUE.findall(line):
                cells.setdefault((sheet, cell), value.split(" (=")[0].strip())
    return cells


def check_citations(answer: str, files: list[tuple[str, str]]) -> Review:
    text = answer
    checks: list[str] = []
    paged = [(name, pages_of(body)) for name, body in files]
    paged = [(name, pages) for name, pages in paged if pages]
    books = [(name, cells_of(body)) for name, body in files]
    books = [(name, cells) for name, cells in books if cells]
    for start, chunk in reversed(list(_sentences(answer))):
        fixed = _fix_page(chunk, paged) or _fix_cell(chunk, books)
        if fixed:
            new_chunk, note = fixed
            text = text[:start] + new_chunk + text[start + len(chunk):]
            checks.insert(0, note)
        elif note := _other_file(chunk, paged, files):
            checks.insert(0, note)
    return Review(text=text, checks=checks)


def _other_file(chunk: str, paged: list[tuple[str, dict[int, str]]], files: list[tuple[str, str]]) -> str:
    """A page of one file cited for figures that are only in another file."""
    named = _named(chunk, paged)
    if len(list(PAGE_RE.finditer(chunk))) != 1 or len(named) != 1:
        return ""
    name, pages = named[0]
    figures = [n for n in numbers_in(chunk) if _is_claim(chunk, n)]
    if not figures or any(Grounding(list(pages.values())).has(figure) for figure in figures):
        return ""
    holders = [other for other, body in files if other != name and all(Grounding([body]).has(figure) for figure in figures)]
    if len(holders) != 1:
        return ""
    shown = ", ".join(figure.shown for figure in figures)
    return f"{shown} is in {holders[0]}, not {name}: check where that figure comes from."


def _named(chunk: str, files: list[tuple[str, object]]):
    lowered = chunk.lower()
    named = [item for item in files if item[0].lower() in lowered or item[0].rsplit(".", 1)[0].lower() in lowered]
    return named or (files if len(files) == 1 else [])


def _fix_page(chunk: str, paged: list[tuple[str, dict[int, str]]]):
    cited = list(PAGE_RE.finditer(chunk))
    named = _named(chunk, paged)
    if len(cited) != 1 or len(named) != 1:
        return None
    name, pages = named[0]
    page = int(cited[0]["page"])
    figures = [n.shown.lstrip("$€£") for n in numbers_in(chunk) if _is_claim(chunk, n)]
    words = {w.lower() for w in _WORD_RE.findall(chunk)} - {name.lower()}
    if not figures and len(words) < 3:
        return None

    def score(body: str) -> tuple[int, int]:
        lowered = body.lower()
        return sum(f in body for f in figures), sum(w in lowered for w in words)

    scores = {number: score(body) for number, body in pages.items()}
    best = max(scores.values())
    leaders = [number for number, value in scores.items() if value == best]
    here = scores.get(page, (0, 0))
    if len(leaders) != 1 or leaders[0] == page:
        return None
    if figures and best[0] < len(figures):
        return None
    if figures and here[0] >= len(figures):
        # The cited page has every figure: it is a right citation, even if another page repeats them.
        return None
    if here[0] >= best[0] and best[1] < here[1] + 2:
        return None
    right = leaders[0]
    word = cited[0]["word"]
    new = chunk[: cited[0].start()] + f"{word} {right}" + chunk[cited[0].end():]
    return new, f"Corrected the page for {name}: that is on page {right}, not page {page}."


_CELL_WORD_BEFORE = re.compile(r"\bcells?\s+(?:[A-Z]{1,3}\d{1,6}\s*(?:,|and|to|:)\s*)*$", re.I)


def _is_cell_ref(chunk: str, match: re.Match) -> bool:
    """A cell reference, not a word shaped like one ("H1 revenue", "Q3 actual"): it has a sheet
    prefix (Summary!D2) or the word "cell" before it (cell D2, cells B2 and C2)."""
    return bool(match["sheet"]) or bool(_CELL_WORD_BEFORE.search(chunk[max(0, match.start() - 40): match.start()]))


def _fix_cell(chunk: str, books: list[tuple[str, dict[tuple[str, str], str]]]):
    named = _named(chunk, books)
    if not named or len(named) > 1:
        return None
    name, cells = named[0]
    refs = [
        m for m in CELL_RE.finditer(chunk)
        if _is_cell_ref(chunk, m) and any(cell == m["cell"] for _sheet, cell in cells)
    ]
    if len(refs) != 1:
        return None
    ref = refs[0]
    sheet = ref["sheet"].strip("'\"") if ref["sheet"] else None
    figures = [n for n in numbers_in(chunk) if _is_claim(chunk, n) and not (ref.start() <= n.start < ref.end())]
    if not figures:
        return None
    in_scope = {key: value for key, value in cells.items() if sheet is None or key[0] == sheet}
    current = [value for (_sheet, cell), value in in_scope.items() if cell == ref["cell"]]
    if any(Grounding([value]).has(figure) for value in current for figure in figures):
        return None
    figure = min(figures, key=lambda n: min(abs(n.start - ref.end()), abs(ref.start() - n.end)))
    holders = [key for key, value in in_scope.items() if Grounding([value]).has(figure)]
    if len(holders) != 1:
        return None
    right_sheet, right_cell = holders[0]
    label = ref.group(0).replace(ref["cell"], right_cell)
    new = chunk[: ref.start()] + label + chunk[ref.end():]
    where = f"{right_sheet}!{right_cell}"
    return new, f"Corrected the cell for {figure.shown} in {name}: it is in {where}, not {ref['cell']}."


def review(answer: str, *, material: list[str], files: list[tuple[str, str]]) -> Review:
    """The answer with clear corrections made, and notes on figures that couldn't be found."""
    numbers = check_numbers(answer, Grounding(material + [body for _name, body in files]))
    cited = check_citations(numbers.text, files)
    return Review(text=cited.text, checks=numbers.checks + cited.checks)
