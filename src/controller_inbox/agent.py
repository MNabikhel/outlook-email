"""What Ask CloseDesk reads: attachment passages sized to the model, and read-only tools.

The chat starts from the emails picked for the question. For the email the
question is about, the model also gets an outline of each attached file and
the passages that match the question (or the start of the file for "summarize
this"). A model that can call tools may then search mail, open another email,
read any page or sheet, pull exact cells, trace a formula back to its inputs,
work out sums and dates exactly, and keep notes. Notes are saved with the email, so the next question starts
from what was already found.

Files on an email flagged as possible payment fraud are not read until the
user clears it. Text inside emails and files is passed as data; the system
prompt tells the model not to follow instructions written there.
"""

from __future__ import annotations

import ast
import calendar
import json
import operator
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from controller_inbox import documents
from controller_inbox.config import Settings
from controller_inbox.fraud import attachments_locked
from controller_inbox.models import AttachmentRecord, EmailRecord
from controller_inbox.store import Store

MAX_STEPS = 6
CHARS_PER_TOKEN = 3
DEFAULT_CONTEXT = 4096
RECOMMENDED_CONTEXT = 16384
TOOL_SCHEMA_TOKENS = 900
MAX_FILE_BYTES = 40_000_000

LOCKED = (
    "Locked: this email is flagged as possible payment fraud, so its files are not opened. "
    "If you have confirmed it by phone, mark it “Not fraud” on the email page and ask again."
)

SUMMARY_RE = re.compile(r"\b(summar\w*|overview|gist|tl;?dr|walk me through|what(?:'s| is| does) (?:it|this|the \w+) (?:say|about|cover))\b", re.I)

_STOP = frozenset(
    """
    a about above after all also an and any are as at be been but by can could did do does for from had has have
    how i if in into is it its just me my no not of on or our please show so tell than that the their them then
    there these they this those to was we were what when where which who why will with would you your file files
    attachment attachments document documents doc draft email message sheet page summary summarize summarise
    """.split()
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_mail",
            "description": "Find other emails by words, names, invoice numbers or file names. Returns numbered emails you can cite.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Words to look for"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_email",
            "description": "Read one email's text and see its files with an outline of each.",
            "parameters": {
                "type": "object",
                "properties": {"email": {"type": "string", "description": "The email's number, like 2"}},
                "required": ["email"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read part of an attached file: a page, slide, sheet, or section label from its outline. "
            "Leave part empty for the outline and the first section.",
            "parameters": {
                "type": "object",
                "properties": {
                    "email": {"type": "string", "description": "The email's number, like 1"},
                    "file": {"type": "string", "description": "File name or its number in the file list"},
                    "part": {"type": "string", "description": "For example: page 3, slide 2, Budget, sheet \"Q3\" rows 41–80"},
                },
                "required": ["email", "file"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_in_file",
            "description": "Find the sections of an email's files that mention some words. Leave file empty to look in all of them.",
            "parameters": {
                "type": "object",
                "properties": {
                    "email": {"type": "string"},
                    "query": {"type": "string"},
                    "file": {"type": "string"},
                },
                "required": ["email", "query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_cells",
            "description": "Exact values and formulas from a spreadsheet range such as B2:F20.",
            "parameters": {
                "type": "object",
                "properties": {
                    "email": {"type": "string"},
                    "file": {"type": "string"},
                    "sheet": {"type": "string"},
                    "cells": {"type": "string", "description": "A range like A1:F30"},
                },
                "required": ["email", "file", "cells"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "trace_cell",
            "description": "Show how a spreadsheet cell is calculated: its formula and the cells it uses, two levels down.",
            "parameters": {
                "type": "object",
                "properties": {
                    "email": {"type": "string"},
                    "file": {"type": "string"},
                    "sheet": {"type": "string"},
                    "cell": {"type": "string", "description": "One cell like E12"},
                },
                "required": ["email", "file", "cell"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Exact arithmetic or date math; use it instead of working numbers out yourself. Examples: "
            "55000+36500+24000 · (301500-259400)/259400*100 · 2026-12-31 - 90 days · days between 2026-10-02 and 2026-12-31",
            "parameters": {
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "note",
            "description": "Write down a finding with where it came from (file, page or cell). Notes are kept with the email.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "email": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
]


def prompt_budget(context_tokens: int, reply_tokens: int, *, tools: bool) -> int:
    """Characters of prompt that fit the model's context next to its reply."""
    overhead = TOOL_SCHEMA_TOKENS if tools else 250
    tokens = (context_tokens or DEFAULT_CONTEXT) - reply_tokens - overhead
    return max(2500, tokens * CHARS_PER_TOKEN)


def context_advice(context_tokens: int, left_out: list[str]) -> str:
    if not left_out:
        return ""
    what = ", ".join(dict.fromkeys(left_out))
    if (context_tokens or DEFAULT_CONTEXT) < RECOMMENDED_CONTEXT:
        known = f"{context_tokens:,}-token" if context_tokens else "small (probably 4,096-token)"
        return (
            f"Your model has a {known} context window, so I only read parts of {what}. "
            f"For whole documents, reload the model in LM Studio with Context Length {RECOMMENDED_CONTEXT:,} or more "
            "(My Models → the model's settings → Context Length), then ask again."
        )
    return f"{what.capitalize()} is long, so I read the parts that match your question. Ask about a page or sheet to read more."


@dataclass
class Workspace:
    store: Store
    settings: Settings
    sources: list[EmailRecord]
    question: str = ""
    current_id: str | None = None
    notes: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    left_out: list[str] = field(default_factory=list)
    read_files: bool = False
    last_email: str = ""

    def number(self, email: EmailRecord) -> int:
        for index, item in enumerate(self.sources, start=1):
            if item.id == email.id:
                return index
        self.sources.append(email)
        return len(self.sources)

    def email(self, ref) -> EmailRecord | None:
        text = str(ref or "").strip().strip("[]#() ").lower().removeprefix("email").strip()
        if not text:
            return self.primary()
        if text.isdigit():
            index = int(text) - 1
            return self.sources[index] if 0 <= index < len(self.sources) else None
        email = self.store.get_email(str(ref).strip())
        if email is not None:
            self.number(email)
        return email

    def primary(self) -> EmailRecord | None:
        """The email the question is about: the one on screen, else the best match."""
        if self.current_id:
            for email in self.sources:
                if email.id == self.current_id:
                    return email
        return self.sources[0] if self.sources else None

    def file(self, email: EmailRecord, name) -> AttachmentRecord | None:
        files = email.attachments
        text = str(name or "").strip().strip("\"'").lower()
        if not files:
            return None
        if not text:
            return files[0] if len(files) == 1 else None
        if text.isdigit() and 1 <= int(text) <= len(files):
            return files[int(text) - 1]
        for att in files:
            if att.filename.lower() == text:
                return att
        for att in files:
            if text in att.filename.lower() or Path(att.filename).stem.lower() in text:
                return att
        return None

    def original(self, email: EmailRecord, att: AttachmentRecord) -> bytes | None:
        path = original_file(self.settings, email, att)
        return path.read_bytes() if path is not None else None


def original_file(settings: Settings, email: EmailRecord, att: AttachmentRecord) -> Path | None:
    """The attachment as it arrived, from inbox/extracted/<email id>/ (never outside it)."""
    from controller_inbox.folder_mail import safe_filename

    root = settings.inbox_extracted.resolve()
    path = (root / email.id / safe_filename(att.filename)).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        return None
    return path


# The context the model starts from ---------------------------------------------------------


_KINDS = {
    ".pdf": "PDF",
    ".docx": "Word document",
    ".doc": "Word document",
    ".rtf": "Word document",
    ".xlsx": "Excel workbook",
    ".xlsm": "Excel workbook",
    ".xls": "Excel workbook",
    ".csv": "CSV table",
    ".pptx": "PowerPoint deck",
    ".txt": "text",
    ".html": "web page",
    ".ics": "calendar invite",
}


def file_kind(att: AttachmentRecord) -> str:
    suffix = Path(att.filename).suffix.lower()
    if suffix == ".txt" and (att.extracted_text or "").startswith("From:"):
        return "forwarded email"
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".bmp"}:
        return "picture"
    return _KINDS.get(suffix, suffix.lstrip(".").upper() or "file")


def files_line(email: EmailRecord) -> str:
    if not email.attachments:
        return ""
    names = []
    for index, att in enumerate(email.attachments, start=1):
        empty = "" if (att.extracted_text or "").strip() else ", no readable text"
        names.append(f"{index}. {att.filename} ({file_kind(att)}{empty})")
    locked = " — locked: possible payment fraud, not opened" if attachments_locked(email) else ""
    return "Files: " + "; ".join(names) + locked


def file_context(ws: Workspace, question: str, room: int) -> dict[str, str]:
    """Outline and the passages that matter, for the email(s) the question is about, within ``room`` characters."""
    blocks: dict[str, str] = {}
    targets = [email for email in ws.sources[:2] if email.attachments]
    primary = ws.primary()
    if primary is not None and primary.attachments:
        targets = [primary] + [email for email in targets if email.id != primary.id][:1]
    whole = bool(SUMMARY_RE.search(question))
    for position, email in enumerate(targets):
        share = room if len(targets) == 1 else (room * 2 // 3 if position == 0 else room // 3)
        if attachments_locked(email):
            blocks[email.id] = LOCKED
            continue
        blocks[email.id] = _email_files(ws, email, question, share, whole=whole)
    return blocks


def _email_files(ws: Workspace, email: EmailRecord, question: str, room: int, *, whole: bool) -> str:
    readable = [att for att in email.attachments if (att.extracted_text or "").strip()]
    if not readable:
        return ""
    ws.read_files = True
    named = named_files(readable, question)
    skipped = [att for att in readable if att not in named] if named else []
    readable = named or readable
    lines: list[str] = []
    used = 0
    per_file = max(600, room // len(readable))
    for att in readable:
        text = att.extracted_text
        parts = documents.split_parts(text)
        head = (
            f"── File: {att.filename} ({file_kind(att)}) · {len(parts)} section{'s' if len(parts) != 1 else ''}"
            f" · {len(text):,} characters"
        )
        matches = [] if whole else documents.search_parts(text, question, limit=4, stop=_STOP)
        wanted = {part.label for part in matches}
        block = [head]
        if len(parts) > 1:
            labels = [p.label for p in parts[:12]] + ([f"… {len(parts) - 12} more"] if len(parts) > 12 else [])
            block.append("Sections: " + " / ".join(labels))
        # Matching sections first; the rest of the file fills whatever room is left.
        budget = per_file - len(head) - 200
        picked: dict[str, str] = {}
        cut = False
        for part in matches + [part for part in parts if part.label not in wanted]:
            if budget <= 200:
                break
            body = part.text
            if len(body) > budget:
                body, cut = body[:budget].rsplit("\n", 1)[0] + "\n…", True
            picked[part.label] = body
            budget -= len(body) + len(part.label) + 4
        block += [f"[{att.filename} · {part.label}]\n{picked[part.label]}" for part in parts if part.label in picked]
        if cut or len(picked) < len(parts):
            ws.left_out.append(att.filename)
            block.append(f"(Showing {len(picked)} of {len(parts)} sections{', one cut short' if cut else ''}. Read others with read_file.)")
        piece = "\n".join(block)
        if used + len(piece) > room and lines:
            ws.left_out.append(att.filename)
            lines.append(f"── File: {att.filename} (not shown; read it with read_file)")
            continue
        lines.append(piece)
        used += len(piece)
    lines += [f"── File: {att.filename} ({file_kind(att)}; not asked about, read it with read_file)" for att in skipped]
    return "\n".join(lines)


def named_files(files: list[AttachmentRecord], question: str) -> list[AttachmentRecord]:
    """The files a question mentions by name ("the budget.xlsx", "in Q3 Budget")."""
    text = question.lower()
    named = []
    for att in files:
        name = att.filename.lower()
        stem = Path(name).stem.split(" › ")[-1]
        if name in text or name.split(" › ")[-1] in text or (len(stem) >= 4 and re.search(rf"(?<!\w){re.escape(stem)}(?!\w)", text)):
            named.append(att)
    return named


def earlier_findings(ws: Workspace, email: EmailRecord, limit: int = 5) -> str:
    rows = ws.store.findings(email.id, limit=limit)
    if not rows:
        return ""
    return "Notes from earlier reading of this email:\n" + "\n".join(f"- {row['text']}" for row in reversed(rows))


def file_matches(email: EmailRecord, question: str, *, limit: int = 3, outline: bool = True) -> list[str]:
    """Plain lines for the no-model answer: where the question's words appear in the email's files."""
    if attachments_locked(email):
        return ["Files are locked: this email is flagged as possible payment fraud."] if email.attachments else []
    lines = []
    for att in email.attachments:
        text = att.extracted_text or ""
        if not text.strip():
            continue
        hits = documents.search_parts(text, question, limit=limit, stop=_STOP)
        if not hits:
            continue
        for part in hits[:limit]:
            snippet = _snippet(part.text, documents.terms_of(question, _STOP))
            lines.append(f"{att.filename} · {part.label}: {snippet}")
    if not lines and outline:
        for att in email.attachments[:4]:
            text = att.extracted_text or ""
            if text.strip():
                sections = documents.outline(text, limit=4)
                lines.append(f"{att.filename}: " + " / ".join(sections))
    return lines[: limit * 2]


def _snippet(text: str, words: list[str], width: int = 180) -> str:
    flat = " ".join(text.split())
    low = flat.lower()
    at = min((low.find(word) for word in words if word in low), default=0)
    start = max(0, at - 60)
    piece = flat[start : start + width]
    return ("…" if start else "") + piece + ("…" if start + width < len(flat) else "")


# Tools --------------------------------------------------------------------------------------


def step_label(name: str, args: dict, ws: Workspace) -> str:
    email = ws.email(args.get("email")) if args.get("email") or name not in {"search_mail", "note", "calculate"} else None
    file = ws.file(email, args.get("file")) if email is not None and args.get("file") else None
    fname = file.filename if file else str(args.get("file") or "the files")
    if name == "search_mail":
        return f"Searching mail for “{args.get('query', '')}”"
    if name == "open_email":
        return f"Opening [{ws.number(email)}] {email.subject}" if email else "Opening an email"
    if name == "read_file":
        return f"Reading {fname}" + (f" · {args['part']}" if args.get("part") else "")
    if name == "find_in_file":
        return f"Looking for “{args.get('query', '')}” in {fname}"
    if name == "read_cells":
        return f"Reading cells {_sheet_ref(args)}{args.get('cells', '')} in {fname}"
    if name == "trace_cell":
        return f"Tracing how {_sheet_ref(args)}{args.get('cell', '')} is calculated in {fname}"
    if name == "calculate":
        return f"Working out {str(args.get('expression', ''))[:80]}"
    return name.replace("_", " ")


def _sheet_ref(args: dict) -> str:
    return f"{args['sheet']}!" if args.get("sheet") else ""


def run_tool(ws: Workspace, name: str, args: dict, *, limit: int) -> str:
    """Run one read-only tool and return its text for the model."""
    args = args if isinstance(args, dict) else {}
    try:
        text = _dispatch(ws, name, args)
    except Exception as exc:  # a broken file or odd argument is reported back to the model, not raised
        text = f"That didn't work ({type(exc).__name__}: {str(exc)[:120]})."
    if len(text) > limit:
        text = text[:limit].rsplit("\n", 1)[0] + "\n[Cut here to fit. Ask for a narrower part to see more.]"
    if name not in {"note", "search_mail"}:
        ws.evidence.append(f"{step_label(name, args, ws)}:\n{text}")
    return text


def _dispatch(ws: Workspace, name: str, args: dict) -> str:
    if name == "search_mail":
        return _search_mail(ws, str(args.get("query") or ""))
    if name == "note":
        return _note(ws, str(args.get("text") or ""), args.get("email"))
    if name == "calculate":
        return calculate(str(args.get("expression") or ""))
    email = ws.email(args.get("email"))
    if email is None:
        return "No email with that number. Use a number from the list, like 1."
    ws.last_email = email.id
    if name == "open_email":
        return _open_email(ws, email)
    if attachments_locked(email):
        return LOCKED
    if name == "find_in_file":
        return _find_in_file(ws, email, str(args.get("query") or ""), args.get("file"))
    att = ws.file(email, args.get("file"))
    if att is None:
        return f"No file like {args.get('file')!r} on [{ws.number(email)}]. " + (files_line(email) or "It has no files.")
    ws.read_files = True
    if name == "read_file":
        return _read_file(att, str(args.get("part") or ""))
    if name in {"read_cells", "trace_cell"}:
        if not att.filename.lower().endswith(documents.SPREADSHEET_SUFFIXES):
            return f"{att.filename} isn't an Excel workbook. Use read_file or find_in_file."
        data = ws.original(email, att)
        if data is None:
            return (
                f"The original {att.filename} isn't saved on this computer, so exact cells can't be read. "
                "Use read_file: the workbook text lists each cell with its value and formula."
            )
        if name == "read_cells":
            return documents.read_cells(data, str(args.get("sheet") or ""), str(args.get("cells") or "A1:J40"))
        return documents.trace_cell(data, str(args.get("sheet") or ""), str(args.get("cell") or ""))
    return f"There is no tool called {name}."


def _search_mail(ws: Workspace, query: str) -> str:
    from controller_inbox.assistant import keywords

    terms = keywords(query)
    if not terms:
        return "Give a name, company, invoice number or file name to search for."
    found = ws.store.search_ranked(terms, limit=5)
    if not found:
        return f"No emails mention {query!r}."
    lines = [f"Emails matching {query!r}:"]
    for email in found:
        n = ws.number(email)
        files = ", ".join(att.filename for att in email.attachments[:5])
        lines.append(
            f"[{n}] {email.received_at[:10]} · from {email.sender_name or email.sender_email} · “{email.subject}”"
            + (f" · files: {files}" if files else "")
        )
    return "\n".join(lines)


def _open_email(ws: Workspace, email: EmailRecord) -> str:
    n = ws.number(email)
    body = re.sub(r"\n{3,}", "\n\n", email.body_text or "").strip()
    lines = [
        f"[{n}] {email.received_at[:10]} · from {email.sender_name or email.sender_email} <{email.sender_email}> · “{email.subject}”",
        body[:2500] + (" …" if len(body) > 2500 else ""),
    ]
    if email.attachments:
        lines.append(files_line(email))
        if not attachments_locked(email):
            for att in email.attachments:
                if (att.extracted_text or "").strip():
                    lines.append(f"Outline of {att.filename}: " + " / ".join(documents.outline(att.extracted_text, limit=10)))
    notes = earlier_findings(ws, email)
    if notes:
        lines.append(notes)
    return "\n".join(lines)


def _read_file(att: AttachmentRecord, label: str) -> str:
    text = att.extracted_text or ""
    if not text.strip():
        return f"{att.filename} has no readable text (a scan or picture)."
    parts = documents.split_parts(text)
    if not label.strip():
        outline = documents.outline(text, limit=30)
        first = parts[0]
        return f"{att.filename}: {len(parts)} sections.\nOutline:\n" + "\n".join(outline) + f"\n\n[{first.label}]\n{first.text}"
    part = documents.read_part(text, label)
    if part is None:
        return f"No part called {label!r} in {att.filename}. Sections: " + "; ".join(p.label for p in parts[:30])
    index = next((i for i, p in enumerate(parts) if p.label == part.label), 0)
    after = f"\n(Next: {parts[index + 1].label})" if index + 1 < len(parts) else "\n(End of file.)"
    return f"{att.filename} · [{part.label}] ({index + 1} of {len(parts)})\n{part.text}{after}"


def _find_in_file(ws: Workspace, email: EmailRecord, query: str, file) -> str:
    targets = [ws.file(email, file)] if file else email.attachments
    targets = [att for att in targets if att is not None and (att.extracted_text or "").strip()]
    if not targets:
        return "No readable file to search. " + (files_line(email) or "")
    ws.read_files = True
    hits = []
    for att in targets:
        for part in documents.search_parts(att.extracted_text, query, limit=3, stop=_STOP):
            hits.append(f"{att.filename} · [{part.label}]\n{part.text}")
    if not hits:
        return f"None of the files mention {query!r}. Try other words, or read_file for the outline."
    return "\n\n".join(hits[:4])


def _note(ws: Workspace, text: str, ref) -> str:
    text = " ".join(text.split())[:600]
    if not text:
        return "Nothing to note."
    email = ws.email(ref) if ref else (ws.store.get_email(ws.last_email) if ws.last_email else ws.primary())
    ws.notes.append(text)
    if email is not None:
        at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        ws.store.add_finding(email.id, text, question=ws.question, at=at)
    return "Noted."


_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}
_DATE_FORMATS = ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%m/%d/%Y")
_DATE_STEP = re.compile(r"^(.+?)\s*([+-])\s*(\d{1,5})\s*(day|week|month|year)s?$", re.I)
_BETWEEN = re.compile(r"^(?:(?:number of\s+)?days?\s+)?(?:between|from)\s+(.+?)\s+(?:and|to|until)\s+(.+)$", re.I)
CALC_HELP = "Write numbers with + - * / ( ) and %, or dates like 2026-12-31 - 90 days, or days between 2026-10-02 and 2026-12-31."


def calculate(expression: str) -> str:
    """Arithmetic and date offsets a small model shouldn't do in its head."""
    text = " ".join(expression.split())[:200].rstrip("=? ")
    if not text:
        return CALC_HELP
    step = _DATE_STEP.match(text)
    if step and _parse_date(step.group(1)):
        start = _parse_date(step.group(1))
        count = int(step.group(3)) * (1 if step.group(2) == "+" else -1)
        unit = step.group(4).lower()
        if unit in {"day", "week"}:
            result = start + timedelta(days=count * (7 if unit == "week" else 1))
        else:
            months = start.year * 12 + start.month - 1 + count * (12 if unit == "year" else 1)
            year, month = divmod(months, 12)
            result = date(year, month + 1, min(start.day, calendar.monthrange(year, month + 1)[1]))
        return f"{text} = {result.isoformat()} ({result:%A} {result.day} {result:%B %Y})"
    between = _BETWEEN.match(text)
    if between and _parse_date(between.group(1)) and _parse_date(between.group(2)):
        first, second = _parse_date(between.group(1)), _parse_date(between.group(2))
        return f"{(second - first).days} days from {first.isoformat()} to {second.isoformat()}"
    cleaned = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text.replace("$", "").replace("×", "*").replace("÷", "/"))
    cleaned = re.sub(r"(\d+(?:\.\d+)?)\s*%", r"(\1/100)", cleaned)
    try:
        value = _evaluate(ast.parse(cleaned, mode="eval").body)
    except (SyntaxError, ValueError, TypeError, ZeroDivisionError, OverflowError) as exc:
        return f"Couldn't work that out ({type(exc).__name__}). {CALC_HELP}"
    return f"{text} = {_number(value)}"


def _parse_date(text: str) -> date | None:
    text = text.strip().strip(",.")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _evaluate(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _evaluate(node.left), _evaluate(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 12:
            raise ValueError("exponent too large")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_evaluate(node.operand))
    raise ValueError("only numbers and + - * / ( ) are allowed")


def _number(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return f"{round(value):,}"
    return f"{value:,.4f}".rstrip("0").rstrip(".")


def evidence_text(ws: Workspace, room: int) -> str:
    """What the tools returned, newest last, trimmed to ``room`` characters."""
    kept: list[str] = []
    used = 0
    for item in reversed(ws.evidence):
        piece = item if len(item) <= 1800 else item[:1800].rsplit("\n", 1)[0] + "\n…"
        if used + len(piece) > room:
            break
        kept.append(piece)
        used += len(piece)
    return "\n\n".join(reversed(kept))


def tool_call_message(content: str, calls: list[dict]) -> dict:
    return {
        "role": "assistant",
        "content": content or "",
        "tool_calls": [
            {"id": call["id"], "type": "function", "function": {"name": call["name"], "arguments": json.dumps(call["arguments"])}}
            for call in calls
        ],
    }
