"""Mail held as possible payment fraud is not interacted with: the model never reads it (its text no more than its
files, whether asked about on screen, found by a search, opened by the model or read overnight), and it doesn't
open in a mail app or download, where its links and files would work. Marking it safe undoes all of that."""

from __future__ import annotations

import html

import pytest
from fastapi.testclient import TestClient

from controller_inbox import agent, assistant, overnight
from controller_inbox.fraud import attachments_locked, record_fraud_verdict
from helpers import PAGE

HELD = "demo-bec-wire"
SAYS = ("9988776612", "previous account", "rest of the day")  # what only the held email's text says


@pytest.fixture
def model(monkeypatch):
    """A model that records every prompt it is given."""
    asked: list[str] = []

    def stream(_settings, messages, **_kwargs):
        asked.append("\n".join(str(m.get("content")) for m in messages))
        yield "Answer [1]."

    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "stream_text", stream)
    monkeypatch.setattr(assistant, "chat_with_tools", lambda *_a, **_k: (_ for _ in ()).throw(assistant.ToolsUnsupported("no tools")))
    return asked


def _answer(loaded, settings, question, email_id=None) -> str:
    events = list(assistant.answer_stream(loaded, settings, question, email_id=email_id, today="2026-09-22"))
    return "".join(e["text"] for e in events if e["type"] == "delta")


def test_asked_about_while_it_is_open_the_answer_says_why_it_is_held_and_the_model_isnt_asked(loaded, settings, model):
    assert attachments_locked(loaded.get_email(HELD))
    text = _answer(loaded, settings, "What does this email want me to do?", HELD)
    assert "held as possible payment fraud" in text and "phone" in text and "Not fraud" in text
    assert not any(word in text.lower() for word in SAYS) and not model, "neither the model nor the answer reads it"


def test_found_among_other_mail_the_model_is_told_it_is_held_and_never_what_it_says(loaded, settings, model):
    events = list(assistant.answer_stream(loaded, settings, "Any emails about updated wiring instructions or bank details?", today="2026-09-22"))
    assert HELD in {card["id"] for e in events if e["type"] == "sources" for card in e["sources"]}, "it is listed, to be verified"
    assert model and not any(word in prompt.lower() for prompt in model for word in SAYS)
    assert any("held as possible payment fraud" in prompt for prompt in model)


def test_the_models_tools_dont_open_or_list_it(loaded, settings):
    ws = agent.Workspace(loaded, settings, [loaded.get_email(HELD)], question="what is this")
    assert agent.run_tool(ws, "open_email", {"email": 1}, limit=4000) == agent.HELD
    found = agent.run_tool(ws, "search_mail", {"query": "wiring instructions"}, limit=4000)
    assert not any(word in found.lower() for word in SAYS) and "held as possible payment fraud" in found


def test_the_overnight_reading_passes_it_by(loaded):
    waiting = overnight.waiting_for_reading(loaded, 50)
    assert HELD not in {email.id for email in waiting}
    with loaded.connect() as conn:
        held_waiting = conn.execute("SELECT model_status FROM emails WHERE id = ?", (HELD,)).fetchone()[0] == "script_draft"
    if held_waiting:
        assert loaded.counts()["waiting_on_bionic"] == len(overnight.waiting_for_reading(loaded, 10_000))


def test_it_doesnt_open_in_a_mail_app_or_download_until_it_is_marked_safe(loaded, settings):
    from controller_inbox.web import create_app

    client = TestClient(create_app(settings, loaded), headers=PAGE)
    assert client.get(f"/inbox/{HELD}/original").status_code == 403
    assert client.post(f"/inbox/{HELD}/open").status_code == 403
    detail = client.get(f"/api/mail/{HELD}").json()
    assert detail["locked"] and not detail["has_original"] and not detail["original_downloads"]
    record_fraud_verdict(loaded, settings, HELD, verdict="safe")
    assert not attachments_locked(loaded.get_email(HELD))
    assert client.get(f"/inbox/{HELD}/original").status_code == 200


def test_an_earlier_answer_quoting_its_text_is_left_out_once_it_is_held_again(loaded, settings):
    from controller_inbox import chats

    record_fraud_verdict(loaded, settings, HELD, verdict="safe")
    assert not loaded.get_email(HELD).attachments, "an email with no files: only its text is held"
    loaded.create_chat("c1")
    loaded.add_chat_turn("c1", "user", "What account does the wiring email say to use?")
    loaded.add_chat_turn(
        "c1", "assistant", "It says to use account 9988776612 [1].", {"mode": "model", "sources": [{"n": 1, "id": HELD}]}
    )
    record_fraud_verdict(loaded, settings, HELD, verdict="fraud")
    history = chats.history_for_model(loaded, loaded.chat_turns("c1"))
    assert not any("9988776612" in turn["text"] for turn in history)
    assert chats.LOCKED_ANSWER in [turn["text"] for turn in history]
    assert "9988776612" not in chats.past_context(loaded, "Which account does the wiring email say to use?", exclude="c2")


def _taught_sender(store, settings) -> list[str]:
    """A sender whose statement you corrected to a payment-instruction change, and two of their later emails: held
    because of what you taught, with a fraud check that found nothing."""
    from controller_inbox.folder_mail import ingest_folder
    from controller_inbox.learn import record_correction
    from msgfactory import write_msg

    settings.ensure_data_dir()
    kwargs = {"sender_name": "Vendor AR", "sender_email": "ar@vendor-x.com"}
    write_msg(settings.inbox_incoming / "a.msg", "Statement for September", "Please see the statement.", message_id="<a@vendor-x.com>", **kwargs)
    first = ingest_folder(store, settings)[0].id
    record_correction(store, settings, email_id=first, corrected_category="payment_instruction_change", reason="they always try a bank switch")
    later = []
    for n in (2, 3):
        write_msg(settings.inbox_incoming / f"m{n}.msg", f"Invoice 55{n}", f"Invoice 55{n}, due October 30.", message_id=f"<m{n}@vendor-x.com>", **kwargs)
        later.append(ingest_folder(store, settings)[0].id)
    return later


def test_mail_held_by_what_you_taught_says_so_and_offers_not_fraud(store, settings):
    from controller_inbox.web import create_app

    held = _taught_sender(store, settings)[0]
    email = store.get_email(held)
    assert attachments_locked(email) and (store.fraud_check(held) or {}).get("level") == "none"
    client = TestClient(create_app(settings, store), headers=PAGE)
    panel = client.get(f"/api/mail/{held}").json()["fraud"]
    assert panel["held"] and "taught" in panel["held_reason"], "the panel says why it is held, though the check found nothing"
    page = html.unescape(client.get(f"/inbox/{held}").text)
    assert 'value="safe:email"' in page and panel["held_reason"] in page, "the classic page offers Not fraud"
    answer = assistant.held_answer(store, email)
    assert "taught" in answer and "in Settings" not in answer and "Fraud check" in answer
    store.save_fraud_check(held, 10, "none", [{"key": "urgency", "label": "Urgency or secrecy", "points": 10}], "2026-09-22T12:00:00+00:00")
    assert "Urgency" not in assistant.held_answer(store, email), "a signal that didn't hold it is no reason"


def test_the_held_answer_gives_the_strongest_reasons_and_none_that_count_for_it(loaded):
    signals = [
        {"key": "first_contact", "label": "First email from this address", "points": 10},
        {"key": "urgency", "label": "Urgency or secrecy", "points": 10},
        {"key": "reply_to_mismatch", "label": "Replies go to another address", "points": 15},
        {"key": "trusted_domain", "label": "Trusted domain", "points": -30},
        {"key": "attachment_link", "label": "Link to a file", "points": 10},
        {"key": "display_name", "label": "Display name names someone else", "points": 10},
        {"key": "lookalike_domain", "label": "Sender domain looks like a known one", "points": 40},
    ]
    loaded.save_fraud_check(HELD, 95, "high", signals, "2026-09-22T12:00:00+00:00")
    answer = assistant.held_answer(loaded, loaded.get_email(HELD))
    assert "Trusted domain" not in answer
    because = answer.split("It was held because: ", 1)[1]
    assert because.startswith("Sender domain looks like a known one; Replies go to another address;")


def _quoting_reply(store, settings) -> dict[str, str]:
    """A vendor's invoice, a lookalike's held reply asking for a new account, and a colleague's reply that quotes the
    held one under "-----Original Message-----", as Outlook does."""
    from datetime import datetime, timezone

    from controller_inbox.folder_mail import ingest_folder
    from msgfactory import write_msg

    settings.trusted_domains = "taz.com"
    settings.ensure_data_dir()
    day = lambda n: datetime(2026, 9, n, 14, tzinfo=timezone.utc)  # noqa: E731
    write_msg(settings.inbox_incoming / "a.msg", "Invoice INV-55120", "Hi, invoice INV-55120 for September consulting is attached. Dana",
              sender_name="Dana Ortiz", sender_email="dana@northwind.com", sent=day(20), message_id="<inv1@northwind.com>")
    write_msg(settings.inbox_incoming / "b.msg", "RE: Invoice INV-55120",
              "QUOKKAWIRE Please note our bank details have changed; please pay INV-55120 to account 5566778899.",
              sender_name="Dana Ortiz", sender_email="dana@northwind-payments.net", sent=day(21), message_id="<inv2@x.net>",
              headers=["In-Reply-To: <inv1@northwind.com>", "References: <inv1@northwind.com>"])
    write_msg(settings.inbox_incoming / "c.msg", "RE: Invoice INV-55120",
              "Dana, can you confirm the PO for INV-55120? Thanks, Priya\n\n-----Original Message-----\n"
              "From: Dana Ortiz <dana@northwind-payments.net>\nSubject: RE: Invoice INV-55120\n\n"
              "QUOKKAWIRE Please note our bank details have changed; please pay INV-55120 to account 5566778899.",
              sender_name="Priya Raman", sender_email="priya@taz.com", sent=day(22), message_id="<inv3@taz.com>",
              headers=["In-Reply-To: <inv2@x.net>", "References: <inv1@northwind.com> <inv2@x.net>"])
    got = {email.sender_email: email.id for email in ingest_folder(store, settings)}
    assert attachments_locked(store.get_email(got["dana@northwind-payments.net"]))
    assert not attachments_locked(store.get_email(got["priya@taz.com"]))
    return got


def test_a_reply_quoting_held_mail_gives_the_model_only_its_own_words(store, settings, model, monkeypatch):
    from array import array

    from controller_inbox import reading, semantic, tools

    reply = _quoting_reply(store, settings)["priya@taz.com"]
    _answer(store, settings, "What happened so far?", reply)
    assert model and any("confirm the PO" in prompt for prompt in model)
    assert not any("QUOKKAWIRE" in prompt for prompt in model), "the chat's prompt"
    ws = agent.Workspace(store, settings, [store.get_email(reply)], question="what is this")
    opened = agent.run_tool(ws, "open_email", {"email": 1}, limit=4000)
    assert "confirm the PO" in opened and "QUOKKAWIRE" not in opened and "held as possible payment fraud" in opened
    packets = []
    monkeypatch.setattr(tools, "build_packet", lambda email, corrections=None: packets.append(reading.build_packet(email, corrections)) or {})
    tools.prepare_queue(store, limit=10)
    assert any(packet["email_id"] == reply for packet in packets) and "QUOKKAWIRE" not in str(packets), "the overnight reading"
    embedded: list[str] = []
    monkeypatch.setattr(semantic, "embedding_model", lambda _s: "text-embedding-test")
    monkeypatch.setattr(semantic, "embed", lambda _s, texts, query=False: embedded.extend(texts) or [array("f", [1.0, 0.0]) for _ in texts])
    semantic.index_mail(store, settings)
    assert any("confirm the PO" in text for text in embedded) and not any("QUOKKAWIRE" in text for text in embedded)
    assert semantic.coverage(store, settings)["waiting"] == 0, "Setup counts what was indexed as indexed"


def test_the_embedding_model_is_given_its_subject_and_sender_only(loaded, settings, monkeypatch):
    from array import array

    from controller_inbox import semantic

    embedded: list[str] = []
    monkeypatch.setattr(semantic, "embedding_model", lambda _s: "text-embedding-test")
    monkeypatch.setattr(semantic, "embed", lambda _s, texts, query=False: embedded.extend(texts) or [array("f", [1.0, 0.0]) for _ in texts])
    semantic.index_mail(loaded, settings)
    subject = loaded.get_email(HELD).subject
    assert any(text.startswith(subject) for text in embedded), "it is still found by its subject"
    assert not any(word in text.lower() for text in embedded for word in SAYS)


def test_a_lookalike_held_when_the_vendors_invoice_is_read_keeps_the_check_that_holds_it(store, settings):
    # The drop folder reads files by name: the lookalike's "resent as a PDF" first, then the invoice it answers.
    from datetime import datetime, timezone

    from controller_inbox.demo import make_pdf
    from controller_inbox.folder_mail import ingest_folder
    from msgfactory import PDF, write_msg

    settings.ensure_data_dir()
    inbox = settings.inbox_incoming
    day = lambda n: datetime(2026, 9, n, 14, tzinfo=timezone.utc)  # noqa: E731
    vendor = {"sender_name": "Dana Ortiz", "sender_email": "dana@northwind.com"}
    invoice = [["Northwind Traders Invoice INV-55120", ["Item", "Amount"], ["Consulting", "$12,450.00"]]]
    write_msg(inbox / "0 hello.msg", "September statement coming", "Our statement follows next week. Dana", sent=day(10), message_id="<hello@northwind.com>", **vendor)
    ingest_folder(store, settings)
    write_msg(inbox / "a reply.msg", "RE: Invoice INV-55120", "Resending the invoice as a PDF. Thanks, Dana",
              sender_name="Dana Ortiz", sender_email="dana@northwlnd.com", sent=day(21), message_id="<r@northwlnd.com>",
              headers=["In-Reply-To: <inv1@northwind.com>", "References: <inv1@northwind.com>"],
              attachments=[("INV-55120.pdf", make_pdf([*invoice, ["Our bank details have changed. Please remit to our new account 5566778899."]]), PDF)])
    write_msg(inbox / "b invoice.msg", "Invoice INV-55120", "Hi, invoice INV-55120 is attached. Thanks, Dana", sent=day(20),
              message_id="<inv1@northwind.com>", attachments=[("INV-55120.pdf", make_pdf(invoice), PDF)], **vendor)
    lookalike = next(email.id for email in ingest_folder(store, settings) if email.sender_email == "dana@northwlnd.com")
    assert attachments_locked(store.get_email(lookalike))
    check = store.fraud_check(lookalike)
    assert check["level"] == "high" and "lookalike_domain" in {signal["key"] for signal in check["signals"]}
    assert "looks like a known one" in assistant.held_answer(store, store.get_email(lookalike))
    assert any(row.get("email_id") == lookalike and row.get("level") == "high" for row in store.fraud_log(limit=50)), "logged"


def test_mail_held_while_the_overnight_reading_runs_isnt_read_later_in_the_batch(loaded, settings):
    from controller_inbox.local_llm import ReaderStats

    later = overnight.waiting_for_reading(loaded, 50)[2].id

    class Reader:
        model, stopped, stats = "fake", False, ReaderStats()
        seen: list[str] = []

        def read(self, packet):
            if not self.seen:  # meanwhile the user clicks "This is fraud" on a later email of the batch
                record_fraud_verdict(loaded, settings, later, verdict="fraud")
            self.seen.append(packet["email_id"])
            return None

    reader = Reader()
    overnight.read_queue(loaded, settings, reader=reader, limit=50)
    assert len(reader.seen) > 3 and later not in reader.seen


def test_the_answer_check_doesnt_count_its_text_as_read(loaded, settings):
    # The model is given only its header, so a figure only its text holds is no figure the model read.
    ws = agent.Workspace(loaded, settings, [loaded.get_email("demo-inv-10482"), loaded.get_email(HELD)], question="how much is the Apex wire?")
    assert "48,500" not in str(assistant.build_messages("how much is the Apex wire?", ws.sources, today="2026-09-22"))
    draft = "The Apex wire is $48,500.00 [2]."
    review = assistant.answer_check.review(draft, **assistant._read_material(ws, history=None, today="2026-09-22"))
    assert review.checks, "flagged as not in what was read"
    assert not assistant._grounded(ws, draft, history=None, today="2026-09-22")


def test_the_classic_pages_dont_show_its_files_text(store, settings, mail):
    """As the workspace and the file page already don't: no excerpt on its page, in its preview or on Attachments,
    and no "Read text" link that would open a page refusing it."""
    from controller_inbox.web import create_app

    scam = mail["Updated remittance details"]
    assert attachments_locked(store.get_email(scam.id))
    said = " ".join(store.get_email(scam.id).attachments[0].extracted_text.split()[:6])
    assert said
    client = TestClient(create_app(settings, store), headers=PAGE)
    for path in (f"/inbox/{scam.id}", f"/inbox/{scam.id}/preview", "/attachments"):
        page = html.unescape(client.get(path).text)
        assert said not in " ".join(page.split()), path
        assert f'href="/inbox/{scam.id}/files/1"' not in page, path
    record_fraud_verdict(store, settings, scam.id, verdict="safe")
    assert said in " ".join(html.unescape(client.get(f"/inbox/{scam.id}").text).split()), "shown once it is marked safe"


def test_a_keyword_search_doesnt_find_it_by_its_files(store, settings, mail):
    """Its files decide nothing the chat or the mail list finds, as in search by meaning: a word only its locked
    file holds finds nothing (and the model isn't told one held email has it)."""
    from controller_inbox.web import create_app

    scam = mail["Updated remittance details"]
    att = store.get_email(scam.id).attachments[0]
    word = next(w for w in att.extracted_text.split() if len(w) > 5 and w.isalpha() and w.lower() not in (scam.subject + scam.body_text).lower())
    assert store.search_ranked([word]) == [] and store.list_emails(q=word) == []
    ws = agent.Workspace(store, settings, [], question=word)
    assert "held" not in agent.run_tool(ws, "search_mail", {"query": word}, limit=4000)
    assert TestClient(create_app(settings, store), headers=PAGE).get("/api/mail", params={"q": word}).json()["items"] == []
    record_fraud_verdict(store, settings, scam.id, verdict="safe")
    assert [email.id for email in store.search_ranked([word])] == [scam.id], "found by its files once it is cleared"


def test_the_model_keeps_no_note_on_it(loaded, settings):
    """It was never shown to the model, so a note "from reading it" would be made up, and would come back to the
    model as read once the email is cleared."""
    ws = agent.Workspace(loaded, settings, [loaded.get_email(HELD)], question="what should I pay Apex Vendor")
    assert agent.run_tool(ws, "open_email", {"email": 1}, limit=4000) == agent.HELD
    said = agent.run_tool(ws, "note", {"text": "Apex Vendor confirmed: pay the new account today", "email": 1}, limit=4000)
    assert said != "Noted." and "held as possible payment fraud" in said
    assert agent.run_tool(ws, "note", {"text": "Pay the new account today"}, limit=4000) != "Noted."
    assert loaded.findings(HELD) == [] and ws.notes == []
    assert HELD not in ws.opened


def test_however_much_mail_is_held_the_overnight_reading_reaches_the_rest(loaded):
    # Held mail is never read, so it waits for good, and it is filed Important: it sorts first in the queue.
    from dataclasses import replace

    held = loaded.get_email(HELD)
    for n in range(520):
        loaded.upsert_email(replace(held, id=f"held-{n}", internet_message_id=f"<held-{n}@example.com>", actions=[]))
    waiting = overnight.waiting_for_reading(loaded, 10)
    assert len(waiting) == min(10, loaded.counts()["waiting_on_bionic"]) > 0


def test_the_digest_doesnt_count_it_as_waiting_for_the_model(loaded, settings):
    from datetime import date

    from controller_inbox import services

    with loaded.connect() as conn:
        conn.execute("UPDATE emails SET model_status = 'script_draft' WHERE id = ?", (HELD,))
    kpis = services.digest_now(loaded, settings, date(2026, 9, 22))["kpis"]
    record_fraud_verdict(loaded, settings, HELD, verdict="safe")
    with loaded.connect() as conn:
        conn.execute("UPDATE emails SET model_status = 'script_draft' WHERE id = ?", (HELD,))
    cleared = services.digest_now(loaded, settings, date(2026, 9, 22))["kpis"]
    assert cleared["waiting_on_bionic"] == kpis["waiting_on_bionic"] + 1, "waiting once it can be read, not while held"


def test_trusting_a_taught_sender_releases_all_their_mail(store, settings):
    held = _taught_sender(store, settings)
    record_fraud_verdict(store, settings, held[0], verdict="safe", scope="sender")
    assert not any(attachments_locked(store.get_email(email_id)) for email_id in held)
