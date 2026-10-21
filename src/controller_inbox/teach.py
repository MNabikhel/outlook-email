"""Teach a field: drag a box round a value on a file's page and say what it is (the invoice number, the total, the due
date...). CloseDesk uses that value for the email at once, and remembers the label printed beside it ("Invoice No.",
"Amount due") for that sender, so their next invoice is read the same way.

How a later file is read: its text is searched for the same label, and the value is taken from just after it on the
same line, or from the line below when the label was printed above the value. Only a value that reads as that kind
of field is taken (a date for a date, an amount for the total), so a label the sender prints elsewhere doesn't
fill a field with the wrong thing.

A taught field is a page fix of kind "field" (``fixes.py``): ``now`` is the field, ``was`` the value as read, and
``anchor`` holds the label and where the value is from it. Taking the fix back (Undo) forgets it: the email, and the
sender's other emails that learnt it, go back to what was read, or to what their files still teach.
"""

from __future__ import annotations

import math
import re
from dataclasses import replace
from datetime import date
from typing import TYPE_CHECKING

from controller_inbox.extract import parse_amount, parse_due_date
from controller_inbox.models import TAUGHT_LISTS, ActionItem, ActionStatus, ExtractedFields

if TYPE_CHECKING:
    from controller_inbox.models import EmailRecord
    from controller_inbox.store import Store

FIELDS = {
    "invoice_number": "Invoice number",
    "total": "Total",
    "due_date": "Due date",
    "invoice_date": "Invoice date",
    "po_number": "PO number",
    "vendor": "Vendor",
}
MAX_LABEL = 60
# Two boxes are on the same line when their middles are within this much of a line's height.
SAME_LINE = 0.6
# A label above a value is at most this far up the page (a fraction of its height).
ABOVE = 0.06

# An id as printed: a few letters, then a figure and what follows it ("KP-55120", "INV-7", "7741").
_ID = re.compile(r"(?<![\w-])([A-Za-z]{0,6}[#\-/]?\d[\w\-/.]{0,28})(?![\w-])")
# Words a label for each field usually has: a label with one of them beats a nearer one without.
_HINTS = {
    "invoice_number": re.compile(r"(?i)\b(invoice|inv|ref|reference|number|no|doc|document|bill)\b|#|№"),
    "po_number": re.compile(r"(?i)\b(p\.?o|purchase|order)\b"),
    "total": re.compile(r"(?i)\b(total|amount|balance|due|payable|pay)\b"),
    "due_date": re.compile(r"(?i)\b(due|pay|payable|by)\b"),
    "invoice_date": re.compile(r"(?i)\b(date|dated|issued)\b"),
    "vendor": re.compile(r"(?i)\b(vendor|supplier|from|remit|company|payee)\b"),
}
# An amount, written 1,234.56 or 1.234,56 (group 4 is the thousands mark), with its currency when printed. Not a
# figure that is part of a longer one, a date ("11/30/2026") or a percentage ("20%").
_CURRENCY = r"(?:[$€£]|\b(?:USD|CAD|EUR|GBP)\b)"
_MONEY = re.compile(
    r"(?<![\w.,/-])\(?-?\s*(" + _CURRENCY + r"\s*)?-?\s*"
    r"(\d{1,3}(?:([.,])\d{3})+(?:(?!\3)[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
    r"\)?(?![\w/-]|\s*%|[.,]\d)"
)
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE = re.compile(
    r"(?i)\b(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|"
    r"" + _MONTH + r"\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}|"
    r"\d{1,2}(?:st|nd|rd|th)?[\s-]+" + _MONTH + r",?[\s-]+\d{4})\b"
)


# Where a value starts in a box that also holds its label ("Total due $4,210.00", "Pay by Nov 15, 2026").
_VALUE_AT = {
    "total": _MONEY,
    "due_date": _DATE,
    "invoice_date": _DATE,
    "invoice_number": re.compile(r"[#№]?\s*[A-Za-z]{0,6}[#\-/]?\d"),
    "po_number": re.compile(r"[#№]?\s*[A-Za-z]{0,6}[#\-/]?\d"),
}


def _amounts(text: str) -> list[tuple[float, bool]]:
    """The amounts in ``text``, in order: (amount, printed as money: with a currency or a thousands mark or cents)."""
    out = []
    for found in _MONEY.finditer(_DATE.sub(" ", text)):
        figure, thousands = found.group(2), found.group(3)
        if thousands:
            figure = figure.replace(thousands, "")
        amount = parse_amount(figure.replace(",", "."))
        if amount is not None and amount > 0:
            out.append((amount, bool(found.group(1) or thousands or re.search(r"[.,]\d{1,2}$", figure))))
    return out


def value_of(field: str, text: str, *, as_of: date) -> str | None:
    """The field's value from ``text`` as CloseDesk keeps it (an ISO date, an amount as a number, an id as printed),
    or None when the words don't read as one."""
    text = " ".join((text or "").split())
    if not text:
        return None
    if field in ("due_date", "invoice_date"):
        found = _DATE.search(text)
        return parse_due_date(found.group(1), as_of=as_of) if found else None
    if field == "total":
        # The first amount printed as money ("$4,210.00 incl. 20% VAT" is 4,210.00), else the first figure.
        amounts = _amounts(text)
        best = next((amount for amount, as_money in amounts if as_money), None)
        if best is None and amounts:
            best = amounts[0][0]
        return str(best) if best is not None else None
    if field in ("invoice_number", "po_number"):
        # An amount ("$2,118.40") isn't an id: its figures are left out first.
        words = _MONEY.sub(lambda m: " " if m.group(1) or m.group(3) else m.group(0), text)
        for found in _ID.finditer(words):
            token = found.group(1).strip(" .#")
            if len(token) < 2 or _DATE.search(token) or not re.search(r"\d", token):
                continue
            if re.fullmatch(r"\d+[.,]\d{2}", token):
                continue
            return token
        return None
    if field == "vendor":
        words = re.sub(r"\s+", " ", text).strip(" :-,")
        return words[:120] if re.search(r"[A-Za-z]{2}", words) else None
    raise ValueError(field)


def shown(field: str, value: str) -> str:
    """The value as the page and messages show it."""
    if field == "total":
        try:
            return f"${float(value):,.2f}"
        except ValueError:
            return value
    return value


# On the page -------------------------------------------------------------------------------------------


def _overlap_share(region: dict, box: dict) -> float:
    """How much of the region lies inside the box."""
    x0, y0 = max(region["x"], box["x"]), max(region["y"], box["y"])
    x1 = min(region["x"] + region["w"], box["x"] + box["w"])
    y1 = min(region["y"] + region["h"], box["y"] + box["h"])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    area = max(region["w"] * region["h"], 1e-9)
    return (x1 - x0) * (y1 - y0) / area


def _label_words(text: str) -> str:
    """A label as it's matched on later files: its words, without figures, at most a few of them."""
    words = [word for word in re.sub(r"\S*\d\S*", " ", text or "").split() if re.search(r"[A-Za-z]", word) or word in ("#", "№")]
    while words and not re.search(r"[A-Za-z]", words[0]):
        words.pop(0)
    return " ".join(words[-6:]).strip()[:MAX_LABEL]


def field_fix(found: list[dict], box: dict, field: str, *, as_of: date) -> dict:
    """A taught field at ``box`` (fractions of the page) on a page whose boxes are ``found``. ValueError (with a
    sentence to show) when the words there don't read as that field."""
    if field not in FIELDS:
        raise ValueError("Choose what this is: invoice number, total, due date, invoice date, PO number or vendor.")
    try:
        box = {key: float(box[key]) for key in ("x", "y", "w", "h")}
    except (TypeError, KeyError, ValueError):
        raise ValueError("Drag a box round the value.") from None
    if not all(math.isfinite(v) for v in box.values()):
        raise ValueError("Drag a box round the value.")
    box = {key: min(max(v, 0.0), 1.0) for key, v in box.items()}
    inside = sorted((r for r in found if _overlap_share(r, box) >= 0.3), key=lambda r: (round(r["y"], 3), r["x"]))
    words = " ".join(r["text"] for r in inside)
    value = value_of(field, words, as_of=as_of)
    if value is None:
        what = FIELDS[field].lower()
        raise ValueError(f"The words in that box ({words[:60] or 'none'}) don't read as {'an' if what[0] in 'aeiou' else 'a'} {what}. Drag across just the {what}.")
    label, position = _label(found, inside, box, field)
    return {
        "kind": "field",
        **box,
        "was": shown(field, value),
        "now": field,
        "anchor": {"label": label, "position": position, "value": value},
    }


def _label(found: list[dict], inside: list[dict], box: dict, field: str) -> tuple[str, str]:
    """The label printed beside the value: in the same box as it ("Total due $4,210.00"), before it on its line
    ("Invoice No:  7741"), or on the line above. Of several, one with a word that fits the field ("Amount Due" for a
    total) beats a nearer one without; then the nearest. ("", "") when there's none.

    A label is never the value's own words. A vendor is words itself, so a box round it holds no label, and only a
    label that says it is one ("From", "Supplier") is learnt for it: a heading beside the name isn't."""
    starts = _VALUE_AT.get(field)
    for region in inside if starts is not None else []:
        at = starts.search(region["text"])
        lead = _label_words(region["text"][: at.start()]) if at else ""
        if lead and region["text"].strip().startswith(lead.split()[0]):
            return lead, "right"
    middle = box["y"] + box["h"] / 2
    height = max(box["h"], 0.008)
    hint = _HINTS.get(field)
    candidates: list[tuple[bool, float, str, str]] = []
    for r in found:
        words = _label_words(r["text"])
        if r in inside or not words:
            continue
        fits = bool(hint and hint.search(words))
        if field == "vendor" and not fits:
            continue
        same_line = abs((r["y"] + r["h"] / 2) - middle) <= height * SAME_LINE + 0.004
        if same_line and r["x"] + r["w"] <= box["x"] + 0.01:
            gap = box["x"] - (r["x"] + r["w"])
            if gap <= 0.35:
                candidates.append((not fits, gap, words, "right"))
        elif r["y"] + r["h"] <= box["y"] + 0.002 and box["y"] - (r["y"] + r["h"]) <= ABOVE:
            if r["x"] < box["x"] + box["w"] and r["x"] + r["w"] > box["x"] - 0.02:
                aligned = abs(r["x"] - box["x"]) <= 0.05 or abs((r["x"] + r["w"]) - (box["x"] + box["w"])) <= 0.05
                gap = (box["y"] - (r["y"] + r["h"])) * (2 if aligned else 4)
                candidates.append((not fits, gap, words, "below"))
    if not candidates:
        return "", ""
    _unfit, _gap, words, position = min(candidates, key=lambda c: (c[0], c[1]))
    return words, position


# On the email ------------------------------------------------------------------------------------------


def with_taught(fields: ExtractedFields, field: str, value: str, *, by: str, label: str = "") -> ExtractedFields:
    """``fields`` with the taught value put first, and noted as taught. The list as the scripts read it is kept
    (``as_read``) the first time, so Undo can put it back."""
    taught = {**fields.taught, field: {"value": value, "by": by, "label": label}}
    updated = ExtractedFields.from_dict({**fields.to_dict(), "taught": taught})
    name = TAUGHT_LISTS.get(field)
    if name is None:
        return updated
    if field not in updated.as_read:
        updated.as_read = {**updated.as_read, field: list(getattr(updated, name))}
    first: str | float = float(value) if field == "total" else value
    keep = 6 if field in ("due_date", "vendor") else 8
    setattr(updated, name, [first, *[v for v in getattr(updated, name) if v != first]][:keep])
    return updated


def without_taught(fields: ExtractedFields, field: str) -> ExtractedFields:
    """``fields`` with a taught value taken back (Undo): no longer preferred, and its list back as the scripts read
    it."""
    taught = {key: entry for key, entry in fields.taught.items() if key != field}
    updated = ExtractedFields.from_dict({**fields.to_dict(), "taught": taught})
    if field in updated.as_read:
        setattr(updated, TAUGHT_LISTS[field], list(updated.as_read[field]))
        updated.as_read = {key: items for key, items in updated.as_read.items() if key != field}
    return updated


def _email_taught(email: EmailRecord, field: str) -> None:
    """The email's field as its files now teach it: what you showed on one of them, else what was learnt, else
    what was read."""
    entries = [a.extracted_fields.taught.get(field) or {} for a in email.attachments]
    entries = [entry for entry in entries if entry.get("value")]
    entry = next((e for e in entries if e.get("by") == "you"), entries[0] if entries else None)
    email.extracted = without_taught(email.extracted, field)
    if entry is not None:
        email.extracted = with_taught(email.extracted, field, entry["value"], by=entry.get("by", "you"), label=entry.get("label", ""))


def templates(store: Store, sender: str, *, not_attachment: str = "") -> list[dict]:
    """What was taught on this sender's files, newest first, one per field: {field, label, position}."""
    seen: set[str] = set()
    out = []
    for fix in store.sender_fixes(sender, not_attachment=not_attachment):
        if fix["kind"] != "field" or fix["now"] in seen:
            continue
        anchor = fix.get("anchor") or {}
        if not anchor.get("label"):
            continue
        seen.add(fix["now"])
        out.append({"field": fix["now"], "label": anchor["label"], "position": anchor.get("position") or "right"})
    return out


def read_with(text: str, learnt: list[dict], *, as_of: date) -> dict[str, dict]:
    """The fields read from ``text`` with what was taught on the sender's other files: {field: {value, label}}."""
    lines = (text or "").splitlines()
    out: dict[str, dict] = {}
    for template in learnt:
        pattern = _label_pattern(template["label"])
        if pattern is None:
            continue
        field = template["field"]
        for index, line in enumerate(lines):
            value = None
            for found in pattern.finditer(line):
                rest = line[found.start("after"):]
                if found.group("after") == " " and not _starts_value(field, rest[1:]):
                    continue  # the start of a longer label: "Total" in "Total VAT"
                if template["position"] == "below":
                    after = next((ln for ln in lines[index + 1 : index + 3] if ln.strip()), "")
                    value = _below(field, line, after, found.start(), as_of=as_of)
                else:
                    value = value_of(field, rest[:120], as_of=as_of)
                if value is not None:
                    break
            if value is not None:
                out[field] = {"value": value, "label": template["label"]}
                break
    return out


def _label_pattern(label: str) -> re.Pattern | None:
    # A label's closing mark isn't part of it: one learnt as "No." is found as "No:" on the next file.
    words = [re.escape(word.rstrip(":.,;") or word) for word in label.split() if word.strip(":.,;")]
    if not words or len("".join(label.split())) < 3:
        return None
    # The whole label, not the start of a longer word ("Ref" isn't "Reference"). What follows it is ``after``.
    return re.compile(r"(?i)(?<![A-Za-z])" + r"\W{0,3}".join(words) + r"(?![A-Za-z])(?P<after>\W{0,4})")


def _starts_value(field: str, text: str) -> bool:
    """Whether ``text`` starts with a value of the field, not another word of a longer label."""
    if field == "vendor":
        return True
    if field in ("due_date", "invoice_date"):
        return bool(_DATE.match(text))
    if field == "total":
        return bool(re.match(r"(?i)\(?-?\s*" + _CURRENCY + r"?\s*-?\d", text))
    return bool(re.match(r"[#№]?\s*[A-Za-z]{0,6}[#\-/]?\d", text))


def _below(field: str, heading: str, line: str, column: int, *, as_of: date) -> str | None:
    """The value on the line under a label: in the same cell when the page's rows were read as cells ("Date | Due |
    Total" over "10/01/2026 | 11/15/2026 | $1,250.00"), else the one starting nearest the label's column."""
    if " | " in heading and " | " in line:
        cells = line.split(" | ")
        cell = heading[:column].count(" | ")
        if cell < len(cells):
            value = value_of(field, cells[cell], as_of=as_of)
            if value is not None or len(cells) == heading.count(" | ") + 1:
                return value
    best = None
    for found in re.finditer(r"\S+(?:\s\S+){0,3}", line):
        value = value_of(field, found.group(0), as_of=as_of)
        if value is None:
            continue
        distance = abs(found.start() - column)
        if best is None or distance < best[0]:
            best = (distance, value)
    if best is None:
        return value_of(field, line, as_of=as_of)
    return best[1]


def apply_learnt(store: Store, email: EmailRecord, *, as_of: date) -> list[str]:
    """Read each of the email's files with what its sender's earlier files taught. Returns the fields filled.
    A field you taught on this file yourself stays as you taught it."""
    if not email.sender_email:
        return []
    filled: list[str] = []
    merged = email.extracted
    for att in email.attachments:
        learnt = templates(store, email.sender_email, not_attachment=att.id)
        if not learnt:
            continue
        found = read_with(att.extracted_text or "", learnt, as_of=as_of)
        for field, got in found.items():
            if (att.extracted_fields.taught.get(field) or {}).get("by") == "you":
                continue
            att.extracted_fields = with_taught(att.extracted_fields, field, got["value"], by="learnt", label=got["label"])
            if (merged.taught.get(field) or {}).get("by") != "you":
                merged = with_taught(merged, field, got["value"], by="learnt", label=got["label"])
            filled.append(field)
    email.extracted = merged
    return list(dict.fromkeys(filled))


# Saving it -------------------------------------------------------------------------------------------


def refile(store: Store, settings, email: EmailRecord, *, ctx=None, codes: bool = True) -> EmailRecord:
    """Save the email with its fields as now taught, and file it again from them: due dates, tasks, priority and
    the AP cost code follow. Tasks already done stay done. ``ctx`` is the fraud check's trust context, when the
    caller files several emails; ``codes=False`` leaves the cost codes for the caller to refresh."""
    from controller_inbox import cost_codes, fraud
    from controller_inbox.pipeline import rescore_stored

    before = list(email.actions)
    read = (email.folder, email.summary, email.model_status) if email.model_status != "script_draft" else None
    check = fraud.assess_email(store, ctx or fraud.trust_context(store, settings), email)
    email = rescore_stored(store, settings, email, check)
    email.actions = _same_tasks(before, email.actions, keep_dropped=read is not None)
    if read is not None:
        # The local model's reading (its folder, summary and tasks) stays: a field shown on the page doesn't undo it.
        email.folder, email.summary, email.model_status = read
    store.upsert_email(email)
    if codes:
        cost_codes.refresh(store, settings, email_ids=[email.id])
    return store.get_email(email.id) or email


def _same_tasks(before: list[ActionItem], after: list[ActionItem], *, keep_dropped: bool) -> list[ActionItem]:
    """The tasks after filing again, with what the user did with them kept. A task's title carries the amount
    and the invoice number ("Code and enter invoice KP-55120 ($2,118.40)"), so a taught field gives it a new
    title: the one task of that kind it had before is the same task, and keeps its id and status (done stays
    done, and no second open task is made for it). With ``keep_dropped`` (the local model read the email) the
    tasks filing again didn't make, the model's own among them, are kept too."""
    old_ids = {item.id for item in before}
    new = [item for item in after if item.id not in old_ids]
    claimed: dict[str, ActionItem] = {}
    for item in new:
        same = [b for b in before if b.source == item.source]
        if len(same) != 1 or sum(1 for n in new if n.source == item.source) != 1:
            continue
        old = same[0]
        if old.status == ActionStatus.OPEN and any(a.id == old.id for a in after):
            continue  # still asked for under its own title
        claimed[item.id] = old
    taken = {old.id for old in claimed.values()}
    out = []
    for item in after:
        if item.id in claimed:
            old = claimed[item.id]
            out.append(replace(item, id=old.id, status=old.status, created_at=old.created_at))
        elif item.id not in taken:
            out.append(item)
    if keep_dropped:
        held = {item.id for item in out}
        out += [item for item in before if item.id not in held]
    return out


def teach_file(store: Store, settings, email: EmailRecord, n: int, field: str, value: str, *, label: str) -> EmailRecord:
    """The field you showed on the email's file ``n``: used for the file and the email from now on."""
    att = email.attachments[n - 1]
    att.extracted_fields = with_taught(att.extracted_fields, field, value, by="you", label=label)
    email.extracted = with_taught(email.extracted, field, value, by="you", label=label)
    return refile(store, settings, email)


def untaught(store: Store, settings, email: EmailRecord, n: int, field: str, *, as_of: date) -> EmailRecord:
    """A taught field taken back (Undo; its fix is already gone): the file and the email go back to what was read,
    or to an earlier fix of the same field on the file when there is one, or to what the sender's other files
    still teach. The sender's other emails that had learnt it from this file are read again the same way."""
    from controller_inbox.pipeline import sent_date

    att = email.attachments[n - 1]
    att.extracted_fields = without_taught(att.extracted_fields, field)
    kept = [fix for fix in store.page_fixes(att.id) if fix["kind"] == "field" and fix["now"] == field]
    if kept:
        anchor = kept[-1].get("anchor") or {}
        if anchor.get("value"):
            att.extracted_fields = with_taught(att.extracted_fields, field, str(anchor["value"]), by="you", label=anchor.get("label") or "")
    apply_learnt(store, email, as_of=sent_date(email.received_at, settings, fallback=as_of))
    _email_taught(email, field)
    email = refile(store, settings, email)
    spread(store, settings, email.sender_email, not_email=email.id, as_of=as_of, forget=field)
    return store.get_email(email.id) or email


def spread(store: Store, settings, sender: str, *, not_email: str, as_of: date, forget: str = "", limit: int = 200) -> int:
    """Read the sender's other stored emails again with what was just taught. Returns how many changed. With
    ``forget`` (a field taken back), what they had learnt of that field goes first, so only what the sender's
    files still teach is used. An email held as suspected fraud is left as it is."""
    from controller_inbox import cost_codes, fraud
    from controller_inbox.pipeline import sent_date

    if not sender:
        return 0
    with store.connect() as conn:
        ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM emails WHERE lower(sender_email) = ? AND id != ? ORDER BY received_at DESC LIMIT ?",
                (sender.strip().lower(), not_email, limit),
            ).fetchall()
        ]
    ctx = None
    changed: list[str] = []
    for email_id in ids:
        email = store.get_email(email_id)
        if email is None or not email.attachments or fraud.attachments_locked(email):
            continue
        before = {key: (entry or {}).get("value") for key, entry in email.extracted.taught.items()}
        if forget:
            for att in email.attachments:
                if (att.extracted_fields.taught.get(forget) or {}).get("by") == "learnt":
                    att.extracted_fields = without_taught(att.extracted_fields, forget)
        apply_learnt(store, email, as_of=sent_date(email.received_at, settings, fallback=as_of))
        if forget and (email.extracted.taught.get(forget) or {}).get("by") != "you":
            _email_taught(email, forget)
        after = {key: (entry or {}).get("value") for key, entry in email.extracted.taught.items()}
        if after != before:
            # One trust context for the batch: building it reads every verdict and trusted sender.
            ctx = ctx or fraud.trust_context(store, settings)
            refile(store, settings, email, ctx=ctx, codes=False)
            changed.append(email.id)
    if changed:
        cost_codes.refresh(store, settings, email_ids=changed)
    return len(changed)
