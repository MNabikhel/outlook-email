"""Ask CloseDesk the hard-file questions and see what reaches the model, or what a real model answers.

``evidence`` runs a question the way the chat box does, with the model calls recorded instead of sent: the
answer counts as within reach when one of its right answers is in what CloseDesk put in front of the model (the
file passages, the figures it worked out, the tables it offered), inside the prompt budget of a 16k context. A
model can only answer from what it is given, so this is the part CloseDesk controls.

``live`` asks a real model (any OpenAI-compatible server: LM Studio, Ollama, llama.cpp) and checks its answer.
"""

from __future__ import annotations

import json
import mimetypes
import re
from email.message import EmailMessage
from pathlib import Path

from controller_inbox import assistant
from controller_inbox.folder_mail import ingest_folder
from controller_inbox.local_llm import ToolReply

FILES = Path(__file__).parent / "files"
CONTEXT = 16384


def cases() -> list[dict]:
    return json.loads((FILES / "cases.json").read_text(encoding="utf-8"))


def case_id(case: dict) -> str:
    return re.sub(r"[^a-z0-9]+", "-", case["question"].lower()).strip("-")[:60]


def mail_with(store, settings, filename: str):
    """The file as an attachment on an email dropped in the drop folder, read the way any mail is."""
    message = EmailMessage()
    message["From"] = "Alex Rivera <alex@taz-holdings.example>"
    message["To"] = "controller@taz-holdings.example"
    message["Subject"] = f"{Path(filename).stem} for review"
    message["Date"] = "Mon, 02 Nov 2026 09:15:00 -0500"
    message["Message-ID"] = f"<{case_slug(filename)}@taz-holdings.example>"
    message.set_content(f"Hi, {filename} is attached. Can you take a look?\n\nAlex")
    kind = (mimetypes.guess_type(filename)[0] or "application/octet-stream").split("/")
    message.add_attachment((FILES / filename).read_bytes(), maintype=kind[0], subtype=kind[1], filename=filename)
    settings.ensure_data_dir()
    (settings.inbox_incoming / f"{case_slug(filename)}.eml").write_bytes(bytes(message))
    records = ingest_folder(store, settings)
    assert records, f"{filename} was not read"
    return store.get_email(records[0].id)


def case_slug(filename: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", filename.lower()).strip("-")


def found(text: str, answers: list[str]) -> bool:
    """One of the answers in ``text``: as written, or with the thousands separators left out or swapped."""
    flat = _flat(text)
    return any(answer.lower() in text.lower() or _flat(answer) in flat for answer in answers)


def _flat(text: str) -> str:
    return re.sub(r"(?<=\d)[,.\s](?=\d{3}\b)", "", text.lower())


def evidence(store, settings, email, question: str, monkeypatch, *, sql: str = "") -> dict:
    """What the model is given for ``question``, with every model call recorded and answered with nothing new.
    ``sql``: the query the model writes when asked for one (the right one, to see its result reach the answer);
    none by default."""
    prompts: list[str] = []

    def record(messages) -> None:
        prompts.append("\n".join(str(m.get("content") or "") for m in messages))

    def tools(_settings, messages, _tools, *, max_tokens):
        record(messages)
        return ToolReply("I could not find it.")

    def stream(_settings, messages, *, max_tokens, **_k):
        record(messages)
        yield "I could not find it."

    def complete(_settings, messages, **_k):
        record(messages)
        return f"Table: the one that holds it.\nSQL: {sql}" if sql else "SQL: NONE"

    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "context_length", lambda _s: CONTEXT)
    monkeypatch.setattr(assistant, "needs_more_context", lambda _s: False)
    monkeypatch.setattr(assistant, "chat_with_tools", tools)
    monkeypatch.setattr(assistant, "stream_text", stream)
    monkeypatch.setattr(assistant, "complete_text", complete)
    events = list(assistant.answer_stream(store, settings, question, email_id=email.id))
    answer = "".join(e["text"] for e in events if e["type"] == "delta")
    return {"prompts": prompts, "first": prompts[0] if prompts else "", "answer": answer, "events": events}


def live(store, settings, email, question: str) -> list[dict]:
    """The chat box's events for ``question``, answered by the real model ``settings`` points at."""
    return list(assistant.answer_stream(store, settings, question, email_id=email.id))


def resolved(sql: str, filename: str) -> str:
    """``sql`` with each ``{word}`` replaced by the name of the file's table whose title holds that word."""
    from controller_inbox.documents import extract_document
    from controller_inbox.table_query import Tables

    text = extract_document(filename, mimetypes.guess_type(filename)[0] or "application/octet-stream", (FILES / filename).read_bytes())
    tables = Tables([(filename, text)])

    def table(match: re.Match) -> str:
        named = [sheet.name for sheet in tables.sheets if not sheet.of and match.group(1) in sheet.title]
        assert named, f"no table titled with {match.group(1)!r}: {[sheet.title for sheet in tables.sheets]}"
        return named[0]

    return re.sub(r"\{([^}]+)\}", table, sql), tables
