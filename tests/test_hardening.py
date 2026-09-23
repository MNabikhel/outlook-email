from __future__ import annotations

import json
import sqlite3
from email import message_from_bytes
from email.utils import getaddresses
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from controller_inbox import assistant, local_llm, web
from controller_inbox.assistant import answer_stream, draft_reply, pick_sources
from controller_inbox.config import Settings
from controller_inbox.folder_mail import ingest_folder
from controller_inbox.local_llm import ThinkFilter, complete_text, stream_text, strip_thinking
from controller_inbox.profile import active_profile
from controller_inbox.store import Store
from controller_inbox.web import _host_name, allowed_hosts, create_app

PAGE = {"X-CloseDesk": "1"}


def _events(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _answer(events: list[dict]) -> str:
    return "".join(event["text"] for event in events if event["type"] == "delta")


def _fake_server(monkeypatch, handler) -> None:
    """Point the model calls at an in-process OpenAI-style server."""
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(local_llm, "_chat_request", lambda s, m, t, *, stream: ("http://model.test/v1/chat/completions", {"stream": stream}))
    monkeypatch.setattr(local_llm.httpx, "post", lambda url, **kw: httpx.Client(transport=transport).post(url, json=kw.get("json")))
    monkeypatch.setattr(
        local_llm.httpx, "stream", lambda method, url, **kw: httpx.Client(transport=transport).stream(method, url, json=kw.get("json"))
    )


def _sse(*pieces: str) -> httpx.Response:
    lines = [f"data: {json.dumps({'choices': [{'delta': {'content': piece}}]})}\n\n" for piece in pieces]
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content="".join(lines) + "data: [DONE]\n\n")


# ---------- Opening files ----------


def test_open_in_outlook_never_launches_a_non_mail_file(settings: Settings, store: Store, monkeypatch):
    settings.ensure_data_dir()
    (settings.inbox_incoming / "run-me.bat").write_text("@echo off\necho hi", encoding="utf-8")
    [record] = ingest_folder(store, settings)
    assert store.get_email(record.id).source_path.endswith(".bat")
    opened: list[Path] = []
    monkeypatch.setattr(web, "open_file", opened.append)
    client = TestClient(create_app(settings, store))
    result = client.post(f"/inbox/{record.id}/open", headers=PAGE).json()
    assert result["ok"] is False and result["download"].endswith("/original")
    assert opened == []
    download = client.get(f"/inbox/{record.id}/original")
    assert download.headers["content-disposition"].startswith("attachment")


def test_requests_for_another_hostname_are_refused(settings: Settings, loaded: Store):
    app = create_app(settings, loaded)
    assert TestClient(app).get("/").status_code == 200
    assert TestClient(app, base_url="http://localhost:8765").get("/").status_code == 200
    rebound = TestClient(app, base_url="http://attacker.example")
    assert rebound.get("/").status_code == 400
    assert rebound.post("/chat", headers=PAGE, json={"message": "hi"}).status_code == 400


def test_host_parsing_and_lan_mode():
    assert _host_name("[::1]:8765") == "[::1]"
    assert _host_name("LocalHost:8765") == "localhost"
    assert _host_name("127.0.0.1") == "127.0.0.1"
    assert allowed_hosts("0.0.0.0") == {"*"}
    assert "192.168.1.20" in allowed_hosts("192.168.1.20")


def test_rebuilt_eml_keeps_a_comma_in_the_sender_name(settings: Settings, loaded: Store):
    email = loaded.get_email("demo-question")
    email.sender_name = "Chen, Maya"
    loaded.upsert_email(email)
    message = message_from_bytes(TestClient(create_app(settings, loaded)).get("/inbox/demo-question/original").content)
    assert getaddresses([message["From"]]) == [("Chen, Maya", email.sender_email)]


# ---------- Chat and drafts with a real (fake) model server ----------


def test_stream_text_strips_thinking_and_ignores_bad_chunks(settings: Settings, monkeypatch):
    def handler(_request):
        body = "data: {not json}\n\ndata: {\"choices\": [\"oops\"]}\n\n"
        body += "".join(
            f"data: {json.dumps({'choices': [{'delta': {'content': p}}]})}\n\n" for p in ["<thi", "nk>scratch</th", "ink>\n\nHello ", "[1]."]
        )
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body + "data: [DONE]\n\n")

    _fake_server(monkeypatch, handler)
    assert "".join(stream_text(settings, [])) == "Hello [1]."


def test_complete_text_handles_odd_replies(settings: Settings, monkeypatch):
    replies = iter(
        [
            httpx.Response(200, json={"choices": [{"message": {"content": "<think>hmm</think>Sure, Friday works."}}]}),
            httpx.Response(200, json={"choices": ["not a dict"]}),
            httpx.Response(200, json=["no", "choices"]),
        ]
    )
    _fake_server(monkeypatch, lambda _request: next(replies))
    assert complete_text(settings, []) == "Sure, Friday works."
    assert complete_text(settings, []) == ""
    assert complete_text(settings, []) == ""


def test_think_filter_across_any_split():
    text = "<think>plan {json}</think>\n\nThe answer is [1]. <think>x</think>Done."
    for size in range(1, 9):
        f = ThinkFilter()
        out = "".join(f.feed(text[i : i + size]) for i in range(0, len(text), size)) + f.flush()
        assert out == "The answer is [1]. Done."
    assert strip_thinking("<think>never closed") == ""


def test_malformed_model_reply_falls_back_to_lookup(settings: Settings, loaded: Store, monkeypatch):
    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    _fake_server(monkeypatch, lambda _request: httpx.Response(200, headers={"content-type": "text/event-stream"}, content='data: {"choices": [7]}\n\n'))
    events = list(answer_stream(loaded, settings, "anything from Maya?"))
    assert "Quick question on the Q4 headcount numbers" in _answer(events)
    assert events[-1] == {"type": "done"}

    def explode(*_args, **_kwargs):
        raise AttributeError("'str' object has no attribute 'get'")
        yield  # pragma: no cover

    monkeypatch.setattr(assistant, "stream_text", explode)
    events = list(answer_stream(loaded, settings, "anything from Maya?"))
    assert any(event["type"] == "mode" and "didn't answer" in event["note"] for event in events)
    assert "Quick question on the Q4 headcount numbers" in _answer(events)


def test_model_chat_end_to_end_through_the_page(settings: Settings, loaded: Store, monkeypatch):
    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    _fake_server(monkeypatch, lambda _request: _sse("<think>which one?</think>", "Maya needs the headcount ", "[1]."))
    response = TestClient(create_app(settings, loaded)).post("/chat", headers=PAGE, json={"message": "What needs a reply?"})
    events = _events(response.text)
    assert events[0]["mode"] == "model"
    assert _answer(events) == "Maya needs the headcount [1]."


def test_draft_strips_thinking(settings: Settings, loaded: Store, monkeypatch):
    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    _fake_server(
        monkeypatch,
        lambda _request: httpx.Response(200, json={"choices": [{"message": {"content": "<think>short</think>Hi Maya,\n\nWill do by Thursday.\n\nBest,"}}]}),
    )
    draft = draft_reply(settings, loaded.get_email("demo-question"))
    assert draft["mode"] == "model" and draft["text"].startswith("Hi Maya")


def test_chat_stream_always_finishes(settings: Settings, loaded: Store, monkeypatch):
    def broken(*_args, **_kwargs):
        yield {"type": "sources", "sources": [], "mode": "lookup"}
        raise RuntimeError("boom")

    monkeypatch.setattr(web, "answer_stream", broken)
    response = TestClient(create_app(settings, loaded)).post("/chat", headers=PAGE, json={"message": "hi"})
    events = _events(response.text)
    assert [event["type"] for event in events] == ["sources", "error", "done"]


# ---------- Search and chat intent ----------


def test_bill_is_a_name_not_an_invoice_intent():
    assert not any(pattern.search("anything from Bill?") for pattern, _ in assistant._INTENTS)


def test_help_words_still_search_when_there_is_more_to_go_on(settings: Settings, loaded: Store):
    _sources, _today, found = pick_sources(loaded, "anything about the payroll export?")
    assert found
    assert "Setup" not in _answer(list(answer_stream(loaded, settings, "anything about the payroll export?")))
    assert "Setup" in _answer(list(answer_stream(loaded, settings, "How do I export from Outlook?")))


def test_search_treats_percent_and_underscore_literally(loaded: Store):
    assert loaded.list_emails(q="a%b") == []

    def text(e):
        return " ".join([e.subject, e.sender_email, e.sender_name, e.summary, e.body_text] + [a.filename + a.extracted_text for a in e.attachments])

    underscores = loaded.list_emails(q="_", limit=50)
    assert len(underscores) < loaded.counts()["emails"]
    assert all("_" in text(e) for e in underscores)
    assert all("%" in text(e).lower() for e in loaded.search_ranked(["%"]))


# ---------- Ingest and upgrade ----------


def test_a_bad_file_does_not_clear_the_sample(settings: Settings, loaded: Store):
    settings.ensure_data_dir()
    (settings.inbox_incoming / "broken.msg").write_bytes(b"this is not an outlook file")
    report: dict = {}
    ingest_folder(loaded, settings, report=report)
    assert len(report["failed"]) == 1
    assert "sample_cleared" not in report
    assert loaded.counts()["emails"] == 19


def test_existing_databases_keep_the_finance_profile(settings: Settings, store: Store):
    settings.ensure_data_dir()
    message = settings.inbox_incoming / "old.eml"
    message.write_bytes(b"Subject: Invoice 1\nFrom: a@example.com\n\nInvoice attached.\n")
    ingest_folder(store, settings)
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute("ALTER TABLE emails DROP COLUMN source_path")
        conn.execute("DELETE FROM sync_state WHERE key = 'profile'")
    assert active_profile(settings, Store(settings.db_path)) == "finance"


def test_new_databases_start_general(settings: Settings, loaded: Store):
    assert active_profile(settings, Store(settings.db_path)) == "general"


@pytest.mark.parametrize("path", ["/", "/inbox", "/settings"])
def test_pages_ship_the_fixed_chat_script(settings: Settings, loaded: Store, path: str):
    client = TestClient(create_app(settings, loaded))
    assert client.get(path).status_code == 200
    script = client.get("/static/app.js").text
    assert "chatRound" in script and 'event.type === "error"' in script and "if (busy) return;" in script


def test_a_name_search_prefers_whole_words(settings: Settings, store: Store):
    settings.ensure_data_dir()
    (settings.inbox_incoming / "billing.eml").write_bytes(
        b"Subject: Updated banking details\nFrom: Northwind Billing <billing@northwind.example>\n\n"
        b"Our bank details have changed. Please send the payment to our new account.\n"
    )
    (settings.inbox_incoming / "lunch.eml").write_bytes(
        b"Subject: Lunch Thursday?\nFrom: Bill Turner <bill@partner.example>\n\nAre you free for lunch Thursday?\n"
    )
    ingest_folder(store, settings)
    assert [e.subject for e in store.search_ranked(["bill"])] == ["Lunch Thursday?"]
    assert len(store.search_ranked(["north"])) == 1
    _sources, _today, found = pick_sources(store, "anything from Bill?")
    assert [store.get_email(i).subject for i in found] == ["Lunch Thursday?"]


def test_header_says_local_ai_is_off_instead_of_waiting(settings: Settings, loaded: Store, monkeypatch):
    client = TestClient(create_app(settings, loaded))
    page = client.get("/").text
    assert "Local AI" in page and "Waiting on model" not in page
    monkeypatch.setattr(web, "check_model", lambda *_a, **_k: local_llm.ModelStatus(mode="auto", reachable=True, model="llama-3.2-3b"))
    assert "Waiting on model" in client.get("/").text


def test_setup_page_reassures_when_no_model_is_running(settings: Settings, loaded: Store):
    settings.llm = None
    settings.llm_base_url = "http://127.0.0.1:9/v1"
    page = TestClient(create_app(settings, loaded)).get("/settings").text
    assert "No local model is running." in page and "all work without one" in page
    assert "Start server" in page
