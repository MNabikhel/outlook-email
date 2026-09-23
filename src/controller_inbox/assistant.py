"""The chat box and reply drafts. Both talk to the local model and both work without one.

The chat answers from a handful of numbered emails picked for the question
(the email on screen, the best search matches, and the focus list when the
question is about today). Replies cite them as [1], [2], and the page turns
those into links. With no model running, the same emails come back as a
plain list, so the box is still a quick way to find mail.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

import httpx

from controller_inbox.config import Settings
from controller_inbox.local_llm import complete_text, llm_active, stream_text
from controller_inbox.models import DOCUMENT_LABELS, ActionStatus, DocumentType, EmailRecord
from controller_inbox.reading import _ungrounded_amounts
from controller_inbox.store import Store

MAX_SOURCES = 6
MAX_QUESTION = 1000

_STOP = set(
    """
    a about above after again all am an and any are as at be been before being below between both but by can
    could did do does doing down during each email emails few for from further had has have having he her here
    hers him his how i if in into is it its just me mail message messages more most my no nor not now of off on
    once only or other our out over own please same she should show so some such tell than that the their them
    then there these they this those through to too under until up very was we were what when where which while
    who whom why will with would you your yours anything something everything find get give got let lets list
    any anyone inbox need needs know see say said thanks thank hi hello hey look looking one want wants mean
    means ask asks asking happen happened going think new last recent latest got gotten
    """.split()
)
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9.@&'-]*[A-Za-z0-9]|[A-Za-z0-9]")
_TODAY = re.compile(
    r"\b(today|focus|urgent|priorit\w*|first|overdue|due|to-?do|tasks?|what should|what do i|"
    r"what needs|behind|catch me up|summar\w*|morning|this week)\b",
    re.I,
)
_HELP = re.compile(
    r"\b(set ?up|install\w*|lm studio|ollama|bionic|local model|how do i|how to|export\w*|drop folder|"
    r"profile|launcher|start (?:the )?server)\b",
    re.I,
)

_INTENTS = [
    (re.compile(r"\b(repl(?:y|ies)|respond|answer|get back)\b", re.I), [DocumentType.REPLY_NEEDED]),
    (re.compile(r"\b(approv\w*|sign[- ]?off)\b", re.I), [DocumentType.APPROVAL_REQUEST, DocumentType.EXPENSE_REPORT]),
    (re.compile(r"\b(meetings?|invite\w*|calendar)\b", re.I), [DocumentType.MEETING]),
    (re.compile(r"\b(invoices?|bills?)\b", re.I), [DocumentType.AP_INVOICE]),
    (
        re.compile(r"\b(fraud|scam|phish\w*|suspicious|bank details?|wire change)\b", re.I),
        [DocumentType.PAYMENT_INSTRUCTION_CHANGE],
    ),
]

SYSTEM = (
    "You are the CloseDesk assistant. You run on the user's own computer and help with their email. "
    "Use only the numbered emails you are given. Answer briefly: one to four sentences, or a short list. "
    "Cite the emails you used like [1] or [2]. If the answer is not in them, say you don't see it in the "
    "inbox and suggest a name or word to search for. Never invent amounts, dates, names, or account numbers. "
    "A change to payment or bank details is possible fraud: the only advice is to verify by phone using a "
    "number already on file. Never tell the user to pay, reply with details, or update an account."
)

HELP_TEXT = (
    "Quick help:\n"
    "- **Add mail:** in Outlook, drag emails into the `inbox/incoming` folder, then click **Process new mail**.\n"
    "- **Local model (optional):** open LM Studio, load a small instruct model, and start its server "
    "(Developer tab → Start). CloseDesk finds it on its own; Setup shows the status.\n"
    "- **No model?** Everything still works: mail is sorted, summarized, and the digest is written.\n"
    "- **Inbox type:** Setup → *What kind of inbox is this?* (General or Finance).\n"
    "Ask me things like *what's urgent today*, *emails from Maya*, or *invoice 10482*."
)

FRAUD_WARNING = "One of these emails asks to change payment details. Verify by phone before doing anything."

DRAFT_SYSTEM = (
    "You write short, polite email replies for a busy professional. Plain text only, no subject line. "
    "Under 120 words. Use only facts from the email; write placeholders like [date] for anything unknown. "
    "Never agree to send money, change bank details, or share account numbers. "
    "End with 'Best,' on its own line and '[Your name]' on the next."
)

FRAUD_REPLY = (
    "Don't reply to this thread, and don't send or update any payment details from it.\n\n"
    "Call the sender on a phone number you already have on file (not one from this email) and confirm the "
    "request is real. If it isn't, forward the email to IT or security as phishing."
)


def keywords(text: str) -> list[str]:
    words = []
    for match in _WORD.findall(text or ""):
        word = match.lower().strip(".'-").split("'")[0]
        if len(word) < 3 and not word.isdigit():
            continue
        if word in _STOP:
            continue
        if len(word) > 4 and word.endswith("s") and not word.endswith("ss") and not word[-2].isdigit():
            word = word[:-1]
        words.append(word)
    return list(dict.fromkeys(words))[:10]


def is_fraud(email: EmailRecord) -> bool:
    return "fraud_risk" in email.flags or email.category == DocumentType.PAYMENT_INSTRUCTION_CHANGE


def pick_sources(
    store: Store,
    question: str,
    *,
    email_id: str | None = None,
    focus: list[dict] | None = None,
) -> tuple[list[EmailRecord], bool, set[str]]:
    """The emails the answer may use, best first; whether the question is about today; the search hits."""
    about_today = bool(_TODAY.search(question))
    picked: dict[str, EmailRecord] = {}
    if email_id:
        current = store.get_email(email_id)
        if current is not None:
            picked[current.id] = current
    stripped = _HELP.sub(" ", _TODAY.sub(" ", question) if about_today else question)
    found = []
    for pattern, categories in _INTENTS:
        if pattern.search(question):
            stripped = pattern.sub(" ", stripped)
            for category in categories:
                found += store.list_emails(category=category.value, order="score", limit=4)
    terms = keywords(stripped)
    found += store.search_ranked(terms, limit=MAX_SOURCES)
    found = list({email.id: email for email in found}.values())[:MAX_SOURCES]
    for email in found:
        picked.setdefault(email.id, email)
    if about_today or not terms:
        for row in (focus or [])[:MAX_SOURCES]:
            if row["email_id"] not in picked:
                email = store.get_email(row["email_id"])
                if email is not None:
                    picked[email.id] = email
    cap = MAX_SOURCES + (1 if email_id in picked else 0)
    return list(picked.values())[:cap], about_today, {email.id for email in found}


def build_messages(
    question: str,
    sources: list[EmailRecord],
    *,
    history: list[dict] | None = None,
    focus: list[dict] | None = None,
    today: str = "",
    current_id: str | None = None,
    budget: int = 6000,
) -> list[dict]:
    numbers = {email.id: index for index, email in enumerate(sources, start=1)}
    lines = [f"Today is {today}." if today else ""]
    if focus:
        lines.append("Focus list (most important first):")
        for row in focus[:5]:
            ref = f" [{numbers[row['email_id']]}]" if row["email_id"] in numbers else ""
            lines.append(f"- {row['label']}: {row['title']}{ref}")
    lines.append("")
    lines.append("Emails:" if sources else "Emails: none matched.")
    room = max(1500, budget - len(SYSTEM) - len(question) - 600)
    per = room // max(1, len(sources) + 2)
    for email in sources:
        limit = per * 3 if email.id == current_id else per
        lines.append(_source_block(numbers[email.id], email, limit, on_screen=email.id == current_id))
    messages: list[dict] = [{"role": "system", "content": SYSTEM}]
    for turn in (history or [])[-6:]:
        role = turn.get("role")
        text = str(turn.get("text") or "")[:400]
        if role in {"user", "assistant"} and text:
            messages.append({"role": role, "content": text})
    context = "\n".join(line for line in lines if line is not None).strip()
    messages.append({"role": "user", "content": f"{context}\n\nQuestion: {question}"})
    return messages


def _source_block(number: int, email: EmailRecord, limit: int, *, on_screen: bool) -> str:
    label = DOCUMENT_LABELS.get(email.category, email.category.value)
    head = (
        f"[{number}] {email.received_at[:10]} · from {email.sender_name or email.sender_email} · "
        f"\"{email.subject}\" · {label} · {email.folder or 'unfiled'}" + (" · (open on screen)" if on_screen else "")
    )
    bits = [head]
    if email.summary:
        bits.append(f"Summary: {email.summary}")
    open_tasks = [a for a in email.actions if a.status == ActionStatus.OPEN][:3]
    if open_tasks:
        bits.append(
            "Open tasks: "
            + "; ".join(a.title + (f" (due {a.due_date})" if a.due_date else "") for a in open_tasks)
        )
    if is_fraud(email):
        bits.append("Warning: payment-detail change — verify by phone.")
    body = re.sub(r"\s+", " ", email.body_text or "").strip()
    if body:
        bits.append("Text: " + (body[:limit].rsplit(" ", 1)[0] + " …" if len(body) > limit else body))
    return "\n".join(bits)


def source_cards(sources: list[EmailRecord]) -> list[dict[str, Any]]:
    return [
        {
            "n": index,
            "id": email.id,
            "subject": email.subject,
            "sender": email.sender_name or email.sender_email,
            "fraud": is_fraud(email),
        }
        for index, email in enumerate(sources, start=1)
    ]


def offline_answer(
    question: str,
    sources: list[EmailRecord],
    *,
    about_today: bool,
    focus: list[dict],
    found: set[str] | None = None,
    current_id: str | None = None,
) -> str:
    found = found or set()
    current = next((email for email in sources if email.id == current_id), None)
    numbers = {email.id: index for index, email in enumerate(sources, start=1)}
    if _HELP.search(question) and not found:
        return HELP_TEXT
    note = "The local model isn't running, so this is a straight lookup."
    if current is not None and not found and not about_today:
        tasks = [a.title for a in current.actions if a.status == ActionStatus.OPEN]
        lines = [f"{note} The email on screen [1]:", f"**{current.subject}** from {current.sender_name or current.sender_email}."]
        if current.summary:
            lines.append(current.summary)
        if tasks:
            lines.append("Open tasks: " + "; ".join(tasks[:3]) + ".")
        if is_fraud(current):
            lines.append(FRAUD_WARNING)
        return "\n".join(lines)
    if not found and not about_today and not keywords(question):
        return (
            "Hi! I can find mail and walk you through today. Try *what's urgent today*, a person's name, "
            "or an invoice number. Start LM Studio's server and I'll write full answers."
        )
    if about_today and focus and not found:
        lines = [f"{note} Your focus list:"]
        for row in focus[:6]:
            ref = f" [{numbers[row['email_id']]}]" if row["email_id"] in numbers else ""
            lines.append(f"{row['rank']}. **{row['label']}** — {row['title']}{ref}")
        return "\n".join(lines)
    if found:
        lines = [f"{note} These emails match:"]
        for index, email in enumerate(sources, start=1):
            if email.id not in found and email.id != current_id:
                continue
            summary = f" — {email.summary}" if email.summary else ""
            due = sorted(a.due_date for a in email.actions if a.status == ActionStatus.OPEN and a.due_date)
            when = f" · task due {due[0]}" if due else ""
            lines.append(
                f"- **{email.subject}** · {email.sender_name or email.sender_email}{when}{summary} [{index}]"
            )
        return "\n".join(lines)
    return (
        "I couldn't find emails about that. Try a person's name, a company, or an invoice number.\n"
        "Start LM Studio's server for written answers; Setup shows the model status."
    )


def answer_stream(
    store: Store,
    settings: Settings,
    question: str,
    *,
    history: list[dict] | None = None,
    email_id: str | None = None,
    focus: list[dict] | None = None,
    today: str = "",
) -> Iterator[dict[str, Any]]:
    """Events for the chat box: ``sources``, then ``delta`` pieces, then ``done``."""
    question = (question or "").strip()[:MAX_QUESTION]
    focus = focus or []
    sources, about_today, found = pick_sources(store, question, email_id=email_id, focus=focus)
    if _HELP.search(question) and not found:
        yield {"type": "sources", "sources": [], "mode": "help"}
        yield {"type": "delta", "text": HELP_TEXT}
        yield {"type": "done"}
        return
    use_model = llm_active(settings)
    event: dict[str, Any] = {"type": "sources", "sources": source_cards(sources), "mode": "model" if use_model else "lookup"}
    if any(is_fraud(email) for email in sources):
        event["warning"] = FRAUD_WARNING
    yield event
    if use_model:
        messages = build_messages(
            question,
            sources,
            history=history,
            focus=focus if about_today else None,
            today=today,
            current_id=email_id,
            budget=settings.llm_max_prompt_chars,
        )
        wrote = False
        try:
            for piece in stream_text(settings, messages, max_tokens=settings.chat_max_tokens):
                wrote = True
                yield {"type": "delta", "text": piece}
        except (httpx.HTTPError, ValueError) as exc:
            if wrote:
                yield {"type": "delta", "text": "\n\n(The local model stopped answering partway.)"}
            else:
                yield {"type": "mode", "mode": "lookup", "note": f"The local model didn't answer ({str(exc)[:120]})."}
                yield {"type": "delta", "text": offline_answer(question, sources, about_today=about_today, focus=focus, found=found, current_id=email_id)}
        else:
            if not wrote:
                yield {"type": "delta", "text": offline_answer(question, sources, about_today=about_today, focus=focus, found=found, current_id=email_id)}
    else:
        yield {"type": "delta", "text": offline_answer(question, sources, about_today=about_today, focus=focus, found=found, current_id=email_id)}
    yield {"type": "done"}


def draft_reply(settings: Settings, email: EmailRecord, *, instructions: str = "") -> dict[str, Any]:
    """A reply to copy or open in the mail app. Payment-change emails get safety advice instead."""
    if is_fraud(email):
        return {"mode": "safety", "text": FRAUD_REPLY, "mailto": "", "note": "This looks like a payment-change request."}
    note = ""
    text = ""
    mode = "template"
    if llm_active(settings):
        body = re.sub(r"\n{3,}", "\n\n", email.body_text or "").strip()[:2500]
        ask = f"Email from {email.sender_name or email.sender_email}\nSubject: {email.subject}\n\n{body}\n\n"
        if instructions.strip():
            ask += f"The reply should: {instructions.strip()[:300]}\n"
        ask += "Write the reply."
        try:
            text = complete_text(
                settings,
                [{"role": "system", "content": DRAFT_SYSTEM}, {"role": "user", "content": ask}],
                max_tokens=320,
            )
            mode = "model"
        except (httpx.HTTPError, ValueError) as exc:
            note = f"The local model didn't answer ({str(exc)[:100]}), so this is a starter template."
        if text and _ungrounded_amounts(text, email):
            text, mode = "", "template"
            note = "The model's draft quoted an amount that isn't in the email, so this is a starter template."
    if not text:
        text = template_reply(email)
        mode = "template"
        note = note or "Starter template. Start a local model for a written draft."
    if _automated(email):
        note = "This came from an automated sender; replies usually go nowhere. " + note
    subject = email.subject if email.subject.lower().startswith("re:") else f"Re: {email.subject}"
    mailto = f"mailto:{quote(email.sender_email or '')}?subject={quote(subject)}&body={quote(text[:1500])}"
    return {"mode": mode, "text": text, "mailto": mailto, "note": note}


def template_reply(email: EmailRecord) -> str:
    first = _first_name(email.sender_name)
    invoice = email.extracted.primary_invoice
    middle = {
        DocumentType.REPLY_NEEDED: "Thanks for your note. I'll get back to you on this by [day].",
        DocumentType.APPROVAL_REQUEST: "Approved — thanks for sending this over.\n\n"
        "(Or, if not yet: I have a question before I approve: [question].)",
        DocumentType.MEETING: "Thanks for the invite — that time works for me.",
        DocumentType.AP_INVOICE: f"Thanks — we've received {('invoice ' + invoice) if invoice else 'the invoice'} "
        "and it's in the queue. We'll reach out if we have questions.",
        DocumentType.AUDIT_REQUEST: "Thanks — we're pulling these together and will share them by [date].",
        DocumentType.EXPENSE_REPORT: "Thanks — I'll review the report and get back to you by [day].",
    }.get(email.category, "Thanks for your email. [Your reply here]")
    return f"Hi {first},\n\n{middle}\n\nBest,\n[Your name]"


_COMPANY_WORDS = {
    "accounts", "ap", "ar", "billing", "desk", "department", "finance", "office", "payroll", "reports",
    "service", "services", "shared", "support", "team", "notifications", "it", "no-reply", "noreply",
}


def _automated(email: EmailRecord) -> bool:
    address = (email.sender_email or "").lower()
    return email.category in {DocumentType.NOTIFICATION, DocumentType.NEWSLETTER} or any(
        tag in address for tag in ("no-reply", "noreply", "donotreply", "do-not-reply", "mailer-daemon")
    )


def _first_name(name: str) -> str:
    token = (name or "").replace(",", " ").split()
    if not token:
        return "there"
    first = token[0]
    if not first.isalpha() or {t.lower() for t in token} & _COMPANY_WORDS or first.lower() == "the":
        return "there"
    return first.capitalize()
