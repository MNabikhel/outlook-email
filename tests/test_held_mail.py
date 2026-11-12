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


def test_trusting_a_taught_sender_releases_all_their_mail(store, settings):
    held = _taught_sender(store, settings)
    record_fraud_verdict(store, settings, held[0], verdict="safe", scope="sender")
    assert not any(attachments_locked(store.get_email(email_id)) for email_id in held)
