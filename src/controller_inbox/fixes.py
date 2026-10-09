"""What you fixed on a file's page, put right there at once and learnt for that sender's later files.

The Page tab shows a box over each piece of text CloseDesk read and an outline round each table it found. Any of it
can be fixed there:

- **A box read wrong**: type what the page says. The box shows your text, and so does the file's text that answers,
  summaries and the chat are written from.
- **A table missed**: draw a box round it. CloseDesk makes it a table from the text read inside, like one it found.
- **An outline that isn't a table**: say so, and it goes.

Each fix is learnt for later files from the same sender (by their address):

- a word or name read wrong the same way on their next file is put right there too, marked as learnt. A figure
  never is: an amount or a date is different on every invoice, so one fixed figure says nothing about the next;
- a table drawn on one of their pages is looked for on their next page in the same place, measured from the first
  line printed in it (its headings), so a page that starts lower down still finds it;
- an outline said not to be a table isn't shown on their later pages where the same first line is.

Every fix is also kept as a labelled example: ``closedesk export-fixes`` writes the pages with what was fixed on
them, for training or testing a table finder or a reading model on a computer with a graphics card (see that
folder's README). Nothing is trained on this computer.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from controller_inbox import page_details, vision
from controller_inbox.page_details import _lines
from controller_inbox.page_view import _plain

if TYPE_CHECKING:
    from controller_inbox.config import Settings
    from controller_inbox.store import Store

KINDS = ("text", "table", "not_table")
# Two boxes are the same place on the page from this much overlap (shared area over their combined area).
SAME = 0.5
# A learnt table is only placed where its first line is printed at most this far from where it was (a fraction of the
# page): a heading printed in a different part of the page is a different table.
DRIFT = 0.25
MAX_TEXT = 500


overlap = page_details._overlap
_inside = page_details._inside


def _box(fix: dict) -> dict:
    return {"x": fix["x"], "y": fix["y"], "w": fix["w"], "h": fix["h"]}


def learnable(was: str, now: str) -> bool:
    """A fix worth carrying to the sender's next file: words, not figures (an amount, a date or a number is
    different on every file)."""
    if not was.strip() or not now.strip() or vision.figures(was) or vision.figures(now):
        return False
    if re.search(r"\d", was + now) and not re.search(r"[A-Za-z]{3}", now):
        return False
    return " ".join(was.split()) != " ".join(now.split())


# Boxes -------------------------------------------------------------------------------------------


def apply(found: list[dict], own: list[dict], learnt: list[dict]) -> list[dict]:
    """The page's boxes with the fixes made on them: a fixed box shows the fix as its text and carries ``fixed``:
    {id, was, by: "you" | "learnt"}. ``own``: this page's fixes; ``learnt``: the sender's fixes on other files."""
    texts = [fix for fix in own if fix["kind"] == "text"]
    known = {}
    for fix in learnt:
        if fix["kind"] == "text" and learnable(fix["was"], fix["now"]):
            known.setdefault(_plain(fix["was"]), fix)  # newest first: the latest fix of a word wins
    out = []
    for region in found:
        mine = max(texts, key=lambda fix: overlap(region, _box(fix)), default=None)
        if mine is not None and overlap(region, _box(mine)) >= SAME:
            out.append({**region, "text": mine["now"], "fixed": {"id": mine["id"], "was": region["text"], "by": "you"}})
            continue
        taught = known.get(_plain(region["text"]))
        if taught is not None:
            out.append({**region, "text": taught["now"], "fixed": {"id": taught["id"], "was": region["text"], "by": "learnt"}})
            continue
        out.append(region)
    return out


# Tables ------------------------------------------------------------------------------------------


def anchor_for(found: list[dict], box: dict) -> dict:
    """The first line printed inside a table's box (its headings, usually), and where the box is from it: how a later
    page from the same sender finds the table again."""
    inside = [index for index, region in enumerate(found) if _inside(region, box)]
    lines = _lines([found[index] for index in inside])
    if not lines:
        return {}
    first = [found[inside[i]] for i in lines[0]]
    head = first[0]
    return {
        "text": " ".join(region["text"] for region in first)[:MAX_TEXT],
        "dx": round(box["x"] - head["x"], 5),
        "dy": round(box["y"] - head["y"], 5),
        "at": [round(head["x"], 5), round(head["y"], 5)],
    }


def _words(text: str) -> str:
    """A line's words without its figures and numbers: an invoice number or a date printed beside a heading changes
    from file to file."""
    return _plain(re.sub(r"\S*\d\S*", " ", text or ""))


def _anchored(found: list[dict], fix: dict) -> dict | None:
    """Where a table drawn on another file from this sender is on this page: found by its first line's words, or
    None."""
    anchor = fix.get("anchor") or {}
    words = _words(anchor.get("text") or "")
    if len(words) < 3:
        return None
    for line in _lines(found):
        regions = [found[i] for i in line]
        if _words(" ".join(region["text"] for region in regions)) != words:
            continue
        head = regions[0]
        was_x, was_y = (anchor.get("at") or [head["x"], head["y"]])[:2]
        if abs(head["x"] - was_x) > DRIFT or abs(head["y"] - was_y) > DRIFT:
            continue
        x, y = head["x"] + float(anchor.get("dx") or 0), head["y"] + float(anchor.get("dy") or 0)
        x0, y0 = min(max(x, 0.0), 1.0), min(max(y, 0.0), 1.0)
        return {"x": x0, "y": y0, "w": min(fix["w"], 1.0 - x0), "h": min(fix["h"], 1.0 - y0)}
    return None


def tables(found: list[dict], on_page: list[dict], own: list[dict], learnt: list[dict]) -> list[dict]:
    """The page's tables with the fixes made: outlines said not to be tables gone, tables drawn added (``fixed``:
    {id, by}). A table from the sender's other files is added where its first line is printed and no table was
    found."""
    gone = [_box(fix) for fix in own if fix["kind"] == "not_table"]
    taught_gone = [fix for fix in learnt if fix["kind"] == "not_table"]
    kept = []
    for table in on_page:
        box = table.get("box")
        if box and any(overlap(box, other) >= SAME for other in gone):
            continue
        if box and any((spot := _anchored(found, fix)) and overlap(box, spot) >= SAME for fix in taught_gone):
            continue
        kept.append(table)
    out = [dict(table, id=f"t{number}") for number, table in enumerate(kept, start=1)]

    def add(box: dict, fix: dict, by: str) -> None:
        if any(table.get("box") and overlap(table["box"], box) >= SAME for table in out):
            return
        made = page_details.table_from_box(found, box, len(out) + 1, "Your table" if by == "you" else "Table (learnt)")
        if made and by == "learnt" and any(overlap(made["box"], other) >= SAME for other in gone):
            return  # said not to be a table on this page
        if made:
            made["fixed"] = {"id": fix["id"], "by": by}
            out.append(made)

    for fix in own:
        if fix["kind"] == "table":
            add(_box(fix), fix, "you")
    seen: set[str] = set()
    for fix in learnt:
        key = _words((fix.get("anchor") or {}).get("text") or "")
        if fix["kind"] != "table" or not key or key in seen:
            continue
        seen.add(key)
        spot = _anchored(found, fix)
        if spot:
            add(spot, fix, "learnt")
    return out


# The file's text ---------------------------------------------------------------------------------

_PAGE = re.compile(r"^\[page (\d+)\]\s*$", re.M)
_ANY_MARK = re.compile(r"^\[(?:page \d+|sheet \"[^\"\n]*\"[^\]\n]*|slide \d+|part \d+)\]\s*$", re.M)


def _loose(was: str) -> re.Pattern:
    """The text as OCR may have it in the file's text: its words with any spacing between them."""
    words = [re.escape(word) for word in was.split()]
    return re.compile(r"(?<![\w.,])" + r"\s*".join(words) + r"(?![\w])")


def apply_to_text(text: str, fixes: list[dict]) -> str:
    """The file's text with each box fixed by the user put right on its page: the first place on that page where
    what was read there (OCR's reading, or the vision model's) is written."""
    text = text or ""
    for fix in fixes:
        if fix["kind"] != "text" or not fix["now"]:
            continue
        olds = [fix["was"], *((fix.get("anchor") or {}).get("also") or [])]
        start, end = _page_span(text, int(fix["page"]))
        for old in olds:
            if not old.strip():
                continue
            found = _loose(old).search(text, start, end)
            if found:
                text = text[: found.start()] + fix["now"] + text[found.end():]
                break
    return text


def _page_span(text: str, page: int) -> tuple[int, int]:
    """Where page ``page`` is in the file's text; the whole text when it has no page marks (a picture)."""
    marks = list(_ANY_MARK.finditer(text))
    if not marks:
        return 0, len(text)
    for i, mark in enumerate(marks):
        found = _PAGE.match(mark.group(0))
        if found and int(found.group(1)) == page:
            return mark.end(), marks[i + 1].start() if i + 1 < len(marks) else len(text)
    return len(text), len(text)


# Saving a fix ------------------------------------------------------------------------------------


def text_fix(region: dict, now: str) -> dict:
    """A fix of one box: where it is, what was read there (OCR's and, where it read another figure, the vision
    model's), and what the page says."""
    now = " ".join((now or "").split())[:MAX_TEXT]
    also = []
    model = region.get("model") or {}
    if model.get("agrees") is False and model.get("text"):
        mine = vision.figures(region["text"])
        theirs = {value: written for value, written in vision.figures(model["text"]).items() if value not in mine}
        if len(mine) == 1 and len(theirs) == 1:
            also = [next(iter(theirs.values()))[0]]
    return {
        "kind": "text",
        "x": region["x"], "y": region["y"], "w": region["w"], "h": region["h"],
        "was": region.get("fixed", {}).get("was") or region["text"],
        "now": now,
        "anchor": {"also": also} if also else {},
    }


def table_fix(found: list[dict], box: dict, kind: str) -> dict:
    """A table drawn (``kind`` "table") or an outline said not to be one ("not_table"), at ``box`` (fractions of
    the page). ValueError for a box that isn't one."""
    values = {key: float(box[key]) for key in ("x", "y", "w", "h")}
    if not all(math.isfinite(value) for value in values.values()):
        raise ValueError("not a box on the page")
    box = {key: min(max(value, 0.0), 1.0) for key, value in values.items()}
    box["w"], box["h"] = min(box["w"], 1.0 - box["x"]), min(box["h"], 1.0 - box["y"])
    return {"kind": kind, **box, "was": "", "now": "", "anchor": anchor_for(found, box)}


# Training examples --------------------------------------------------------------------------------


README = """# Your fixes as training examples

CloseDesk wrote these from the fixes you made in the Page tab. Nothing here was used to train anything on your
computer: training a model needs a graphics card and a few hundred examples. They're here so you, or whoever looks
after CloseDesk, can:

- **test** whether a new table finder or reading model does better on your own files before switching to it;
- **train** one, once there are enough examples.

## What's here

- `pages/` — each page you fixed, as a PNG (about 150 DPI).
- `tables.jsonl` — one line per page: the tables on it as `[x, y, w, h]` (fractions of the page, from its top
  left), after your fixes: the ones CloseDesk found and you left, plus the ones you drew.
- `labels/` — the same tables in YOLO's format (`0 centre-x centre-y width height`), for training a table finder
  such as the layout models RapidLayout ships.
- `text.jsonl` — one line per box you fixed: the page, where the box is, what was read and what the page says.
  With `crops/`, the box cut from the page: pairs for testing or fine-tuning a reading model.

Bank account numbers are masked in what CloseDesk read; they may still be visible in the page pictures, so
treat this folder like the files themselves.
"""


def export(store: Store, settings: Settings, out: Path) -> dict[str, int]:
    """Writes every page with a fix on it, and what was fixed, as training examples (see README above). Returns how
    many pages, tables and fixed boxes were written."""
    from controller_inbox import page_details, page_view

    out.mkdir(parents=True, exist_ok=True)
    for sub in ("pages", "labels", "crops"):
        (out / sub).mkdir(exist_ok=True)
    by_page: dict[tuple[str, int], list[dict]] = {}
    for fix in store.all_page_fixes():
        by_page.setdefault((fix["attachment_id"], int(fix["page"])), []).append(fix)
    counts = {"pages": 0, "tables": 0, "boxes": 0}
    with open(out / "tables.jsonl", "w", encoding="utf-8") as table_lines, open(out / "text.jsonl", "w", encoding="utf-8") as text_lines:
        for (attachment_id, page), fixes in sorted(by_page.items()):
            found_file = _original(store, settings, attachment_id)
            if found_file is None:
                continue
            att, data = found_file
            try:
                png = page_view.page_png(settings, data, att.filename, page)
                read = page_view.regions(settings, data, att.filename, page)
            except Exception:  # noqa: BLE001 - a page that can't be drawn any more is left out
                continue
            name = f"{attachment_id[:12]}-{att.sha256[:8]}-p{page}"
            (out / "pages" / f"{name}.png").write_bytes(png)
            boxes = read["regions"]
            reading = store.page_readings(att.id, att.sha256).get(page)
            model_text = (reading.get("model_text") or "") if reading else None
            found_tables = page_details.page_tables(boxes, read["source"], att.extracted_text or "", page, model_text)
            shown = tables(boxes, found_tables, fixes, [])
            spots = [[table["box"][key] for key in ("x", "y", "w", "h")] for table in shown if table.get("box")]
            table_lines.write(json.dumps({"page": f"pages/{name}.png", "tables": spots}) + "\n")
            (out / "labels" / f"{name}.txt").write_text(
                "".join(f"0 {x + w / 2:.5f} {y + h / 2:.5f} {w:.5f} {h:.5f}\n" for x, y, w, h in spots), encoding="utf-8"
            )
            counts["pages"] += 1
            counts["tables"] += len(spots)
            for number, fix in enumerate(fix for fix in fixes if fix["kind"] == "text"):
                crop = f"crops/{name}-{number + 1}.png"
                _crop(png, _box(fix), out / crop)
                text_lines.write(json.dumps({"page": f"pages/{name}.png", "crop": crop, "box": [fix["x"], fix["y"], fix["w"], fix["h"]], "read": fix["was"], "says": fix["now"]}) + "\n")
                counts["boxes"] += 1
    (out / "README.md").write_text(README, encoding="utf-8")
    return counts


def _crop(png: bytes, box: dict, target: Path) -> None:
    import io

    from PIL import Image

    with Image.open(io.BytesIO(png)) as image:
        w, h = image.size
        pad = 4
        left, top = max(0, int(box["x"] * w) - pad), max(0, int(box["y"] * h) - pad)
        right, bottom = min(w, int((box["x"] + box["w"]) * w) + pad), min(h, int((box["y"] + box["h"]) * h) + pad)
        image.crop((left, top, max(right, left + 1), max(bottom, top + 1))).save(target)


def _original(store: Store, settings: Settings, attachment_id: str) -> tuple[Any, bytes] | None:
    """The attachment and its bytes as they arrived, or None when either is gone (or its email is held as fraud)."""
    from controller_inbox import fraud

    email = store.email_for_attachment(attachment_id)
    if email is None or fraud.attachments_locked(email):
        return None
    att = next((item for item in email.attachments if item.id == attachment_id), None)
    if att is None:
        return None
    data = vision.original_bytes(settings, email, att)
    return (att, data) if data is not None else None
