"""Ask CloseDesk with no model running: the straight lookup answers from the open email, its conversation, its files
and the meeting asked about, and shows what it looked at."""

from __future__ import annotations

import re

from controller_inbox import agent
from controller_inbox.assistant import answer_stream, pick_sources
from controller_inbox.models import AttachmentRecord, DocumentType, EmailRecord, ExtractedFields, Importance


def _events(store, settings, question: str, email_id: str | None = None, today: str = "2026-09-22") -> tuple[dict, str]:
    events = list(answer_stream(store, settings, question, email_id=email_id, today=today))
    sources = next(event for event in events if event["type"] == "sources")
    return sources, "".join(event["text"] for event in events if event["type"] == "delta")


def _cited(text: str) -> set[int]:
    return {int(n) for n in re.findall(r"\[(\d+)\]", text)}


def _record(id_: str, subject: str, body: str, *, sender: str = "Jo Park", address: str = "jo@clearwater-supply.com",
            received: str = "2026-11-10T15:00:00+00:00", mid: str = "", reply_to: str = "", files=(), flags=(),
            category: DocumentType = DocumentType.OTHER) -> EmailRecord:
    return EmailRecord(
        id=id_, subject=subject, sender_name=sender, sender_email=address, received_at=received, body_text=body,
        body_preview=body[:200], has_attachments=bool(files), outlook_importance="normal", is_read=False, category=category,
        category_confidence=0, importance=Importance.LOW, importance_score=0, flags=list(flags), extracted=ExtractedFields(),
        internet_message_id=mid, threading={"in_reply_to": reply_to, "references": [reply_to]} if reply_to else {}, source="folder",
        attachments=[
            AttachmentRecord(id=f"{id_}:{n}", email_id=id_, filename=name, content_type="application/pdf", size_bytes=len(text), sha256="",
                             extracted_text=text, document_type=DocumentType.OTHER, document_confidence=0, extracted_fields=ExtractedFields())
            for n, (name, text) in enumerate(files)
        ],
    )


# 1. "What happened on this so far?" walks the conversation, and lists every email it looked at ----------------------
def test_what_happened_so_far_walks_the_conversation_oldest_first(loaded, settings):
    sources, text = _events(loaded, settings, "What happened on this so far?", "demo-inv-10482-dup")
    numbers = {card["id"]: card["n"] for card in sources["sources"]}
    assert set(numbers) == {"demo-inv-10482", "demo-inv-10482-dup"}
    first, resend = text.find("**Invoice INV-10482"), text.find("**FW: Invoice INV-10482")
    assert 0 <= first < resend, "the conversation is told oldest first"
    assert "Please see attached invoice INV-10482" in text and "Resending invoice INV-10482" in text, "each step's first line"
    assert "Hello AP team" not in text, "a greeting is no first line"
    assert "Northwind Billing" in text and "Sep 22" in text, "who and when"
    assert _cited(text) == set(numbers.values()), "every email it says it looked at is listed"


# 2. The open email's body (and its conversation's) is searched, not only its files ---------------------------------
def test_a_line_in_the_open_emails_body_is_quoted(store, settings):
    reply = _record(
        "vendor-reply", "RE: Clearwater order 5521",
        "Hi Dan,\n\nThanks for confirming the order.\nPayment schedule: 50% on December 1, 50% on December 15.\n\nBest,\nJo",
        files=[("Quote 5521.pdf", "[page 1]\n[notes]\nClearwater Supply\nQuote 5521\nShelving units 40 x $120.00\nTotal $4,800.00")],
    )
    store.upsert_email(reply)
    _sources, text = _events(store, settings, "what is the payment schedule?", reply.id)
    assert "Payment schedule: 50% on December 1, 50% on December 15" in text


def test_a_line_in_an_earlier_email_of_the_conversation_is_quoted(store, settings):
    first = _record(
        "vendor-first", "Clearwater order 5521",
        "Hi Dan,\nThe order is confirmed.\nPayment schedule: 50% on December 1, 50% on December 15.\nJo",
        mid="<order-5521@clearwater-supply.com>", received="2026-11-09T15:00:00+00:00",
    )
    later = _record(
        "vendor-later", "RE: Clearwater order 5521", "Dan,\nThe shelving ships on Monday.\nJo",
        mid="<order-5521-2@clearwater-supply.com>", reply_to="<order-5521@clearwater-supply.com>",
    )
    store.upsert_email(first)
    store.upsert_email(later)
    sources, text = _events(store, settings, "what is the payment schedule?", later.id)
    numbers = {card["id"]: card["n"] for card in sources["sources"]}
    assert set(numbers) == {later.id, first.id}, "the open email and its conversation, not the whole inbox"
    assert f"Payment schedule: 50% on December 1, 50% on December 15. [{numbers[first.id]}]" in text
    assert _cited(text) == set(numbers.values())


def test_held_mail_in_the_conversation_is_not_quoted(store, settings):
    held = _record(
        "held-first", "Clearwater order 5521",
        "Payment schedule changed: wire 100% today to account 5566778899.",
        mid="<order-5521@clearwater-pay.net>", address="billing@clearwater-pay.net", flags=["fraud_risk"],
        received="2026-11-09T15:00:00+00:00",
    )
    later = _record("held-later", "RE: Clearwater order 5521", "Dan,\nThe shelving ships on Monday.\nJo",
                    reply_to="<order-5521@clearwater-pay.net>")
    store.upsert_email(held)
    store.upsert_email(later)
    _sources, text = _events(store, settings, "What happened on this so far?", later.id)
    assert "5566778899" not in text and "wire 100%" not in text
    assert "1 email of this conversation is held as possible payment fraud and isn't shown here." in text, "said, not left out in silence"
    assert "Clearwater order 5521**" not in text.replace("RE: Clearwater order 5521**", ""), "nothing of it, its subject included"
    _sources, text = _events(store, settings, "what is the payment schedule?", later.id)
    assert "5566778899" not in text and "wire 100%" not in text


# 3. A file's snippet is its text, not a layout marker --------------------------------------------------------------
def test_a_files_snippet_skips_layout_markers(loaded):
    invoice = loaded.get_email("demo-inv-10482")
    outline = agent.file_matches(invoice, "What happened on this so far?")
    assert outline and all(not re.search(r":\s*\[\w+\]\s*$", line) for line in outline), outline
    assert "Northwind Supplies" in outline[0]
    terms = agent.file_matches(invoice, "what are the payment terms?")
    assert terms and "Terms Net 15" in terms[0] and "[notes]" not in terms[0] and "[facts]" not in terms[0], terms


def test_an_outline_starts_each_page_with_its_text_not_its_layout_label():
    from controller_inbox import documents

    text = "[page 1]\n[notes]\nNorthwind Supplies\nInvoice INV-10482\n[page 2]\n[table]\nItem | Amount\nFreight | $850.00"
    assert documents.outline(text) == ["page 1: Northwind Supplies", "page 2: Item | Amount"]


# 4. A payment question on an open invoice stays on it, with no phone warning ---------------------------------------
def test_a_payment_question_on_the_open_invoice_stays_on_it(loaded, settings):
    sources, text = _events(loaded, settings, "what is the payment schedule?", "demo-inv-10482")
    ids = [card["id"] for card in sources["sources"]]
    assert ids[0] == "demo-inv-10482" and set(ids) <= {"demo-inv-10482", "demo-inv-10482-dup"}, ids
    assert "warning" not in sources and "Verify by phone" not in text
    assert "Payment due October 5, 2026" in text, "the body's own line about payment"
    # A question that asks about other mail still searches it, and an email that changes bank details still warns.
    others, _ = pick_sources(loaded, "any other emails about the payment schedule?", email_id="demo-inv-10482")[:2]
    assert len(others) > 2
    held, _text = _events(loaded, settings, "what is the payment schedule?", "demo-bec-wire")
    assert held.get("warning")


# 5. "Ask about this meeting" answers from the mail about that meeting, never raw invitation text --------------------
def test_asking_about_a_meeting_reads_the_mail_about_it(loaded, settings):
    question = "What do I need to know before “Northwind invoice INV-10482 review” (Tuesday, September 22 · 2:00 PM–2:30 PM)?"
    sources, text = _events(loaded, settings, question)
    ids = [card["id"] for card in sources["sources"] if not card["calendar"]]
    assert "demo-meeting" not in ids, "another meeting's invitation is not mail about this one"
    assert ids and set(ids) <= {"demo-inv-10482", "demo-inv-10482-dup", "demo-po"}, ids
    assert "BEGIN:VCALENDAR" not in text and "DTSTART" not in text
    assert "Northwind invoice INV-10482 review" in text and "Tuesday" in text and "2:00 PM" in text


def test_asking_about_a_meeting_with_its_invitation_shows_the_meeting_not_the_ics(loaded, settings):
    question = "What do I need to know before “Q4 planning review” (Thursday, September 24 · 2:00 PM–3:00 PM)?"
    sources, text = _events(loaded, settings, question, "demo-meeting")
    ids = [card["id"] for card in sources["sources"] if not card["calendar"]]
    assert ids[0] == "demo-meeting" and "demo-inv-10482" not in ids, ids
    assert "BEGIN:VCALENDAR" not in text and "DTSTART" not in text and "20260924T180000Z" not in text
    assert "Q4 planning review" in text and "Thursday" in text and "2:00 PM" in text
    assert _cited(text) == {card["n"] for card in sources["sources"]}


def test_an_invitation_file_is_never_shown_as_raw_text(loaded):
    invite = loaded.get_email("demo-meeting")
    for question in ("when is the planning review?", "summarize this"):
        lines = agent.file_matches(invite, question)
        assert lines and all("BEGIN:VCALENDAR" not in line and "DTSTART" not in line for line in lines), lines
        assert any("Q4 planning review" in line for line in lines)


def test_held_mail_of_a_matter_or_about_a_meeting_is_no_source(store, settings):
    """A held reply in the conversation, or a held email about a meeting, never reaches the model."""
    from controller_inbox import assistant
    from controller_inbox.models import DocumentType
    from test_links import _record

    store.upsert_email(_record("root", sender="ar@northwind.com", mid="<root@northwind.com>", threading={"to": ["ap@taz.com"]}))
    held = _record("held", sender="ar@northwlnd.com", threading={"in_reply_to": "root@northwind.com"}, received="2026-10-06T09:00:00+00:00",
                   flags=["fraud_risk"])
    held.subject = "Budget review: new dial-in and bank details"
    held.category = DocumentType.PAYMENT_INSTRUCTION_CHANGE
    store.upsert_email(held)
    sources, thread, _held = assistant._matter(store, store.get_email("root"))
    assert "held" not in {email.id for email in sources} and "held" not in thread
    assert "held" not in {email.id for email in assistant._conversation(store, store.get_email("root"))}
    meeting = {"id": "m1", "uid": "u1", "subject": "Budget review", "start": "2026-10-07T15:00:00+00:00", "end": "2026-10-07T16:00:00+00:00",
               "email_id": "held", "organizer": "ar@northwlnd.com", "attendees": [], "all_day": False, "location": "", "source": "invite", "cancelled": False, "recurring": False, "busy": ""}
    assert "held" not in {email.id for email in assistant.meeting_sources(store, settings, meeting)}


def test_a_meetings_mail_isnt_quoted_twice_when_its_summary_is_its_first_line(settings):
    from controller_inbox import assistant

    invite = _record("inv", "Invitation: Q4 budget walkthrough", "Let's walk through the Q4 budget draft together. Bring the variance notes.",
                     sender="Maya Chen", address="maya@taz.com")
    invite.summary = "Meeting / calendar: Let's walk through the Q4 budget draft together."
    meeting = {"subject": "Q4 budget walkthrough", "start": "2026-11-12T11:00:00+00:00", "end": "2026-11-12T12:00:00+00:00", "all_day": False,
               "location": "", "organizer": "", "cancelled": False}
    question = "What do I need to know before “Q4 budget walkthrough” (Thursday, November 12 · 6am–7am)?"
    text = assistant._meeting_answer("A lookup.", question, [invite], meeting, {"inv": 1}, settings.tz)
    assert text.count("Let's walk through the Q4 budget draft together.") == 1, text
