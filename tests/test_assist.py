from __future__ import annotations

import json
from email import message_from_bytes
from email.message import EmailMessage
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from controller_inbox import assistant, web
from controller_inbox.assistant import answer_stream, draft_reply, keywords, pick_sources
from controller_inbox.config import Settings
from controller_inbox.folder_mail import ingest_folder
from controller_inbox.store import Store
from controller_inbox.web import create_app

PAGE = {"X-CloseDesk": "1"}


def _events(response) -> list[dict]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def _answer(events: list[dict]) -> str:
    return "".join(event["text"] for event in events if event["type"] == "delta")


def _drop_eml(settings: Settings, name: str = "Maya question.eml") -> Path:
    message = EmailMessage()
    message["Subject"] = "Quick question on the offsite budget"
    message["From"] = "Maya Chen <maya@example.com>"
    message["Date"] = "Tue, 22 Sep 2026 09:00:00 -0400"
    message.set_content("Hi,\nCould you confirm the offsite budget by Friday?\nThanks, Maya")
    path = settings.inbox_incoming / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(message.as_bytes())
    return path


@pytest.fixture
def client(settings: Settings, loaded: Store) -> TestClient:
    return TestClient(create_app(settings, loaded))


def test_keywords_drop_filler_and_fold_plurals():
    assert keywords("What's in the invoices from Northwind?") == ["invoice", "northwind"]
    assert keywords("hi there") == []


def test_search_matches_body_summary_and_attachments(loaded: Store):
    assert [e.id for e in loaded.list_emails(q="headcount")] == ["demo-question"]
    assert "demo-expense" in [e.id for e in loaded.list_emails(q="Marriott")]
    assert [e.id for e in loaded.list_emails(q="northwind duplicate")] == ["demo-inv-10482-dup"]


def test_chat_without_a_model_lists_the_focus_and_links_emails(settings: Settings, loaded: Store):
    focus = [{"email_id": "demo-bec-wire", "rank": 1, "label": "Verify by phone", "title": "Verify the change"}]
    events = list(answer_stream(loaded, settings, "What's urgent today?", focus=focus, today="2026-09-22"))
    assert events[0]["type"] == "sources" and events[0]["mode"] == "lookup"
    assert events[0]["sources"][0]["id"] == "demo-bec-wire"
    assert "verify" in events[0]["warning"].lower()
    text = _answer(events)
    assert "local model isn't running" in text
    assert "Verify the change [1]" in text
    assert events[-1]["type"] == "done"


def test_chat_intents_and_help_without_a_model(settings: Settings, loaded: Store):
    reply = _answer(list(answer_stream(loaded, settings, "What needs a reply from me?")))
    assert "Quick question on the Q4 headcount numbers" in reply
    assert "password" not in reply.lower()
    assert "Setup" in _answer(list(answer_stream(loaded, settings, "How do I set up the local model?")))
    assert "couldn't find" in _answer(list(answer_stream(loaded, settings, "zebra migration")))


def test_chat_uses_the_email_on_screen(settings: Settings, loaded: Store):
    sources, _today, _found = pick_sources(loaded, "what does this one want?", email_id="demo-approval")
    assert sources[0].id == "demo-approval"
    text = _answer(list(answer_stream(loaded, settings, "what does this one want?", email_id="demo-approval")))
    assert "offer letter" in text and "Open tasks: Please approve the offer letter" in text
    assert ".." not in text


def test_chat_streams_from_the_local_model_with_sources(settings, loaded, monkeypatch):
    seen = {}

    def fake_stream(_settings, messages, *, max_tokens):
        seen["messages"] = messages
        yield "Maya is waiting on you "
        yield "[1]."

    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "stream_text", fake_stream)
    events = list(answer_stream(loaded, settings, "What needs a reply?", history=[{"role": "user", "text": "hi"}]))
    assert events[0]["mode"] == "model"
    assert events[0]["sources"][0]["id"] == "demo-question"
    assert _answer(events) == "Maya is waiting on you [1]."
    system, *middle, user = seen["messages"]
    assert "verify by phone" in system["content"]
    assert middle == [{"role": "user", "content": "hi"}]
    assert '[1] 2026-09-22 · from Maya Chen · "Quick question on the Q4 headcount numbers"' in user["content"]
    assert user["content"].endswith("Question: What needs a reply?")
    assert len(user["content"]) < settings.llm_max_prompt_chars


def test_chat_falls_back_to_lookup_when_the_model_fails(settings, loaded, monkeypatch):
    def broken(*_args, **_kwargs):
        raise httpx.ConnectError("refused")
        yield  # pragma: no cover

    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "stream_text", broken)
    events = list(answer_stream(loaded, settings, "anything from Maya?"))
    assert any(event["type"] == "mode" and event["mode"] == "lookup" for event in events)
    assert "Quick question on the Q4 headcount numbers" in _answer(events)


def test_draft_reply_template_model_guard_and_fraud(settings, loaded, monkeypatch):
    question = loaded.get_email("demo-question")
    template = draft_reply(settings, question)
    assert template["mode"] == "template"
    assert template["text"].startswith("Hi Maya,")
    assert template["mailto"].startswith("mailto:maya.chen%40horizongoods.example?subject=Re%3A%20Quick")

    fraud = draft_reply(settings, loaded.get_email("demo-bec-wire"))
    assert fraud["mode"] == "safety" and fraud["mailto"] == ""
    assert "phone number you already have" in fraud["text"]

    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "complete_text", lambda *_a, **_k: "Hi Maya,\n\nWill do by Thursday.\n\nBest,\n[Your name]")
    assert draft_reply(settings, question)["mode"] == "model"
    monkeypatch.setattr(assistant, "complete_text", lambda *_a, **_k: "Sure, I'll send the $55,000.00 today.")
    guarded = draft_reply(settings, question)
    assert guarded["mode"] == "template" and "amount that isn't in the email" in guarded["note"]


def test_pages_have_search_chat_and_preview_hooks(client: TestClient):
    home = client.get("/").text
    assert 'class="top-search"' in home and 'id="chat"' in home and 'id="drawer"' in home
    assert '/static/app.js?v=' in home
    assert 'data-email="demo-bec-wire"' in home
    assert client.get("/static/app.js").status_code == 200
    results = client.get("/inbox", params={"q": "headcount"}).text
    assert "Quick question on the Q4 headcount numbers" in results
    assert "Updated wiring instructions" not in results


def test_preview_fragment_and_safe_next(client: TestClient):
    preview = client.get("/inbox/demo-question/preview", params={"next": "/folder/important"})
    assert preview.status_code == 200
    assert "<html" not in preview.text
    assert "Quick question on the Q4 headcount numbers" in preview.text
    assert "Draft a reply" in preview.text and "Open in Outlook" in preview.text
    assert "next=/folder/important" in preview.text
    evil = client.get("/inbox/demo-question/preview", params={"next": "//evil.example"}).text
    assert "evil.example" not in evil
    assert "payment details may have changed" in client.get("/inbox/demo-bec-wire/preview").text.lower()
    assert client.get("/inbox/nope/preview").status_code == 404


def test_page_only_endpoints_need_the_page_header(client: TestClient):
    assert client.post("/chat", json={"message": "hi"}).status_code == 403
    assert client.post("/inbox/demo-question/draft", json={}).status_code == 403
    assert client.post("/inbox/demo-question/open").status_code == 403


def test_chat_endpoint_streams_ndjson(client: TestClient):
    response = client.post("/chat", json={"message": "What's urgent today?"}, headers=PAGE)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = _events(response)
    assert events[0]["type"] == "sources" and events[0]["sources"]
    assert events[0]["sources"][0]["id"] == "demo-bec-wire"
    assert events[-1] == {"type": "done"}
    assert client.post("/chat", json={"message": "  "}, headers=PAGE).status_code == 400
    assert client.post("/chat", content=b"not json", headers=PAGE).status_code == 400


def test_draft_endpoint(client: TestClient):
    data = client.post("/inbox/demo-approval/draft", json={"instructions": "yes"}, headers=PAGE).json()
    assert data["mode"] == "template" and "Approved" in data["text"]
    assert client.post("/inbox/nope/draft", json={}, headers=PAGE).status_code == 404


def test_sample_mail_downloads_as_a_rebuilt_eml(client: TestClient):
    opened = client.post("/inbox/demo-question/open", headers=PAGE).json()
    assert opened["ok"] is False and opened["download"] == "/inbox/demo-question/original"
    download = client.get("/inbox/demo-question/original")
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("message/rfc822")
    assert 'filename="Quick question on the Q4 headcount numbers.eml"' in download.headers["content-disposition"]
    message = message_from_bytes(download.content)
    assert message["Subject"] == "Quick question on the Q4 headcount numbers"
    assert "Maya Chen" in message["From"]
    assert "headcount" in message.get_payload(decode=True).decode()


def test_dropped_mail_remembers_its_file_and_opens_it(settings: Settings, store: Store, monkeypatch):
    settings.ensure_data_dir()
    _drop_eml(settings)
    [record] = ingest_folder(store, settings)
    saved = Path(store.get_email(record.id).source_path)
    assert saved.is_file() and settings.inbox_processed.resolve() in saved.parents

    opened: list[Path] = []
    monkeypatch.setattr(web, "open_file", opened.append)
    client = TestClient(create_app(settings, store))
    assert "Download original" in client.get(f"/inbox/{record.id}").text
    result = client.post(f"/inbox/{record.id}/open", headers=PAGE).json()
    assert result["ok"] is True and opened == [saved]
    download = client.get(f"/inbox/{record.id}/original")
    assert download.content == saved.read_bytes()

    def fails(_path):
        raise OSError("no app for .eml")

    monkeypatch.setattr(web, "open_file", fails)
    fallback = client.post(f"/inbox/{record.id}/open", headers=PAGE).json()
    assert fallback["ok"] is False and fallback["download"].endswith("/original")


def test_open_refuses_files_outside_the_processed_folder(settings: Settings, store: Store, monkeypatch, tmp_path):
    settings.ensure_data_dir()
    _drop_eml(settings)
    [record] = ingest_folder(store, settings)
    outside = tmp_path / "secret.txt"
    outside.write_text("not mail", encoding="utf-8")
    store.set_source_path(record.id, str(outside))
    opened: list[Path] = []
    monkeypatch.setattr(web, "open_file", opened.append)
    client = TestClient(create_app(settings, store))
    assert client.post(f"/inbox/{record.id}/open", headers=PAGE).json()["ok"] is False
    assert opened == []
    assert client.get(f"/inbox/{record.id}/original").content != b"not mail"


def test_redropping_mail_keeps_the_file_path(settings: Settings, store: Store):
    settings.ensure_data_dir()
    _drop_eml(settings)
    [record] = ingest_folder(store, settings)
    first = store.get_email(record.id).source_path
    _drop_eml(settings)
    ingest_folder(store, settings)
    assert store.get_email(record.id).source_path
    assert Path(store.get_email(record.id).source_path).is_file()
    assert first


def test_first_real_mail_replaces_the_sample(settings: Settings, loaded: Store):
    settings.ensure_data_dir()
    loaded.save_digest("2026-09-22", "2026-09-22T12:00:00+00:00", "# sample", "<p>sample</p>", {"date": "2026-09-22"})
    _drop_eml(settings)
    report: dict = {}
    [record] = ingest_folder(loaded, settings, report=report)
    assert report["sample_cleared"] == 19
    assert [e.id for e in loaded.list_emails(limit=50)] == [record.id]
    assert loaded.list_actions(status=None) == [] or all(e.id == record.id for _a, e in loaded.list_actions(status=None))
    assert loaded.list_digests() == []

    _drop_eml(settings, "second.eml")
    second: dict = {}
    ingest_folder(loaded, settings, report=second)
    assert "sample_cleared" not in second
    assert loaded.counts()["emails"] == 1
