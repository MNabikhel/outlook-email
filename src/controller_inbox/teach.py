"""Teach a field: drag a box round a value on a file's page and say what it is (the invoice number, the total, the due
date...). CloseDesk uses that value for the email at once, and remembers the label printed beside it ("Invoice No.",
"Amount due") for that sender, so their next invoice is read the same way.

How a later file is read: its text is searched for the same label, and the value is taken from just after it on the
same line, or from the line below when the label was printed above the value. Only a value that reads as that kind
of field is taken (a date for a date, an amount for the total), so a label the sender prints elsewhere doesn't
fill a field with the wrong thing.

A taught field is a page fix of kind "field" (``fixes.py``): ``now`` is the field, ``was`` the value as read, and
``anchor`` holds the label and where the value is from it. Taking the fix back (Undo) forgets it.
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING

from controller_inbox.extract import parse_amount, parse_due_date
from controller_inbox.models import ExtractedFields

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

_ID = re.compile(r"(?<![\w-])([A-Za-z]{0,6}[#\-/]?\d[\w\-/.]{1,28})(?![\w-])")
# Words a label for each field usually has: a label with one of them beats a nearer one without.
_HINTS = {
    "invoice_number": re.compile(r"(?i)\b(invoice|inv|ref|reference|number|no|doc|document|bill)\b|#|№"),
    "po_number": re.compile(r"(?i)\b(p\.?o|purchase|order)\b"),
    "total": re.compile(r"(?i)\b(total|amount|balance|due|payable|pay)\b"),
    "due_date": re.compile(r"(?i)\b(due|pay|payable|by)\b"),
    "invoice_date": re.compile(r"(?i)\b(date|dated|issued)\b"),
    "vendor": re.compile(r"(?i)\b(vendor|supplier|from|remit|company|payee)\b"),
}
_MONEY = re.compile(r"(?<![\w.])\(?-?\s*(?:[$€£]|USD|CAD|EUR|GBP)?\s*(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s*\)?(?![\w])")
_DATE = re.compile(
    r"(?i)\b(\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}|"
    r"\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?,?\s+\d{4})\b"
)


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
        amounts = [parse_amount(m.group(1)) for m in _MONEY.finditer(text)]
        amounts = [a for a in amounts if a is not None and a > 0]
        return str(amounts[-1]) if amounts else None
    if field in ("invoice_number", "po_number"):
        for found in _ID.finditer(text):
            token = found.group(1).strip(" .#")
            if _DATE.search(token) or not re.search(r"\d", token):
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
    box = {key: min(max(float(box[key]), 0.0), 1.0) for key in ("x", "y", "w", "h")}
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
    total) beats a nearer one without; then the nearest. ("", "") when there's none."""
    for region in inside:
        lead = _label_words(re.split(r"\S*\d", region["text"], maxsplit=1)[0])
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
    """``fields`` with the taught value put first, and noted as taught."""
    taught = {**fields.taught, field: {"value": value, "by": by, "label": label}}
    updated = ExtractedFields.from_dict({**fields.to_dict(), "taught": taught})
    if field == "invoice_number":
        updated.invoice_numbers = [value, *[v for v in updated.invoice_numbers if v != value]][:8]
    elif field == "po_number":
        updated.po_numbers = [value, *[v for v in updated.po_numbers if v != value]][:8]
    elif field == "total":
        amount = float(value)
        updated.amounts = [amount, *[a for a in updated.amounts if a != amount]][:8]
    elif field == "due_date":
        updated.due_dates = [value, *[d for d in updated.due_dates if d != value]][:6]
    elif field == "vendor":
        updated.vendor_candidates = [value, *[v for v in updated.vendor_candidates if v != value]][:6]
    return updated


def without_taught(fields: ExtractedFields, field: str, value: str) -> ExtractedFields:
    """``fields`` with a taught value taken back (Undo): no longer preferred. The scripts' own reading stays."""
    taught = {key: entry for key, entry in fields.taught.items() if key != field}
    return ExtractedFields.from_dict({**fields.to_dict(), "taught": taught})


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
        for index, line in enumerate(lines):
            found = pattern.search(line)
            if not found:
                continue
            if template["position"] == "below":
                after = next((ln for ln in lines[index + 1 : index + 3] if ln.strip()), "")
                value = _below(template["field"], after, found.start(), as_of=as_of)
            else:
                value = value_of(template["field"], line[found.end():][:120], as_of=as_of)
            if value is not None:
                out[template["field"]] = {"value": value, "label": template["label"]}
                break
    return out


def _label_pattern(label: str) -> re.Pattern | None:
    words = [re.escape(word.strip(":")) for word in label.split() if word.strip(":")]
    if not words or len("".join(label.split())) < 3:
        return None
    # The whole label, not the start of a longer word ("Ref" isn't "Reference").
    return re.compile(r"(?i)(?<![A-Za-z])" + r"\W{0,3}".join(words) + r"(?![A-Za-z])\W{0,4}")


def _below(field: str, line: str, column: int, *, as_of: date) -> str | None:
    """The value on the line under a label: the one starting nearest the label's column."""
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


def refile(store: Store, settings, email: EmailRecord) -> EmailRecord:
    """Save the email with its fields as now taught, and file it again from them: due dates, tasks, priority and
    the AP cost code follow. Tasks already done stay done."""
    from controller_inbox import cost_codes, fraud
    from controller_inbox.pipeline import rescore_stored

    read = (email.folder, email.summary, email.model_status) if email.model_status != "script_draft" else None
    check = fraud.assess_email(store, fraud.trust_context(store, settings), email)
    email = rescore_stored(store, settings, email, check)
    if read is not None:
        # The local model's reading (its folder and summary) stays: a field shown on the page doesn't undo it.
        email.folder, email.summary, email.model_status = read
        store.upsert_email(email)
    cost_codes.refresh(store, settings, email_ids=[email.id])
    return store.get_email(email.id) or email


def teach_file(store: Store, settings, email: EmailRecord, n: int, field: str, value: str, *, label: str) -> EmailRecord:
    """The field you showed on the email's file ``n``: used for the file and the email from now on."""
    att = email.attachments[n - 1]
    att.extracted_fields = with_taught(att.extracted_fields, field, value, by="you", label=label)
    email.extracted = with_taught(email.extracted, field, value, by="you", label=label)
    return refile(store, settings, email)


def untaught(store: Store, settings, email: EmailRecord, n: int, field: str, value: str) -> EmailRecord:
    """A taught field taken back (Undo): the file and the email go back to what was read."""
    att = email.attachments[n - 1]
    att.extracted_fields = without_taught(att.extracted_fields, field, value)
    others = [a.extracted_fields.taught.get(field) for a in email.attachments if (a.extracted_fields.taught.get(field) or {}).get("value")]
    email.extracted = without_taught(email.extracted, field, value)
    if others:
        entry = others[0]
        email.extracted = with_taught(email.extracted, field, entry["value"], by=entry.get("by", "you"), label=entry.get("label", ""))
    return refile(store, settings, email)


def spread(store: Store, settings, sender: str, *, not_email: str, as_of: date, limit: int = 200) -> int:
    """Read the sender's other stored emails again with what was just taught. Returns how many changed."""
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
    changed = 0
    for email_id in ids:
        email = store.get_email(email_id)
        if email is None or not email.attachments:
            continue
        from controller_inbox.pipeline import sent_date

        before = {key: (entry or {}).get("value") for key, entry in email.extracted.taught.items()}
        apply_learnt(store, email, as_of=sent_date(email.received_at, settings, fallback=as_of))
        after = {key: (entry or {}).get("value") for key, entry in email.extracted.taught.items()}
        if after != before:
            refile(store, settings, email)
            changed += 1
    return changed
