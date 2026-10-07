"""Ask CloseDesk conversations are saved, can be reopened and searched, hold files, and inform later answers."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from controller_inbox import assistant, chats
from controller_inbox.web import create_app

PAGE = {"X-CloseDesk": "1"}
ROSTER = b"Employee,Department,Manager\nJonathan Reyes,,Priya Raman\nLi Wei,Finance,Dana Cole\n"


def _events(response) -> list[dict]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


@pytest.fixture
def client(settings, loaded) -> TestClient:
    return TestClient(create_app(settings, loaded))


def test_a_conversation_is_saved_reopened_searched_and_deleted(client, loaded, monkeypatch):
    first = _events(client.post("/chat", json={"message": "What's urgent today?"}, headers=PAGE))
    chat_id = first[0]["id"]
    assert chats.valid_id(chat_id) and first[0]["title"] == "What's urgent today?"

    seen = {}
    real = assistant.answer_stream

    def spy(*args, **kwargs):
        seen["history"] = kwargs["history"]
        yield from real(*args, **kwargs)

    monkeypatch.setattr("controller_inbox.web.answer_stream", spy)
    again = _events(client.post("/chat", json={"message": "Anything waiting for my approval?", "chat_id": chat_id}, headers=PAGE))
    assert again[0] == {"type": "chat", "id": chat_id, "title": "What's urgent today?"}
    assert [turn["role"] for turn in seen["history"]] == ["user", "assistant"], "earlier turns come from the saved conversation"
    assert seen["history"][0]["text"] == "What's urgent today?"

    saved = client.get(f"/chats/{chat_id}").json()
    assert [turn["role"] for turn in saved["turns"]] == ["user", "assistant", "user", "assistant"]
    assert saved["turns"][1]["sources"] and saved["turns"][1]["text"], "answers keep their sources to show again"

    other = _events(client.post("/chat", json={"message": "Who sent the Northwind invoice?"}, headers=PAGE))[0]["id"]
    listed = client.get("/chats").json()["chats"]
    assert [item["id"] for item in listed] == [other, chat_id] and listed[1]["questions"] == 2
    assert [item["id"] for item in client.get("/chats", params={"q": "approval"}).json()["chats"]] == [chat_id]

    assert client.post(f"/chats/{chat_id}/delete").status_code == 403, "only the page's own scripts"
    assert client.post(f"/chats/{chat_id}/delete", headers=PAGE).json() == {"ok": True}
    assert client.get(f"/chats/{chat_id}").status_code == 404
    assert [item["id"] for item in client.get("/chats").json()["chats"]] == [other]


def test_files_added_to_a_conversation_are_read_and_answered_from(client, settings, loaded):
    chat_id = client.post("/chats", headers=PAGE).json()["id"]
    assert client.get("/chats").json()["chats"] == [], "an empty conversation isn't listed"
    added = client.post(
        f"/chats/{chat_id}/files",
        headers=PAGE,
        files=[("files", ("staff list.csv", ROSTER, "text/csv")), ("files", ("setup.exe", b"MZ", "application/octet-stream"))],
    ).json()
    assert [file["name"] for file in added["files"]] == ["staff list.csv"] and added["files"][0]["text"]
    assert "can't be added" in added["problems"][0]
    assert (settings.inbox_extracted / f"chat-{chat_id}" / "staff list.csv").read_bytes() == ROSTER

    page = client.get(added["files"][0]["href"])
    assert page.status_code == 200 and "Jonathan Reyes" in page.text and "Added to an Ask CloseDesk conversation" in page.text

    events = _events(client.post("/chat", json={"message": "Which department is Jonathan Reyes in?", "chat_id": chat_id}, headers=PAGE))
    sources = next(event for event in events if event["type"] == "sources")["sources"]
    assert sources[0]["id"] == f"chat-{chat_id}" and sources[0]["chat"] and sources[0]["files"][0]["name"] == "staff list.csv"
    answer = "".join(event["text"] for event in events if event["type"] == "delta")
    assert "Jonathan Reyes" in answer, "the lookup answer quotes the added file"
    assert client.get("/chats").json()["chats"][0]["files"] == 1

    left = client.post(f"/chats/{chat_id}/files/1/delete", headers=PAGE).json()
    assert left["files"] == [] and not (settings.inbox_extracted / f"chat-{chat_id}" / "staff list.csv").exists()
    client.post(f"/chats/{chat_id}/delete", headers=PAGE)
    assert not (settings.inbox_extracted / f"chat-{chat_id}").exists()


def test_the_model_reads_added_files_and_what_earlier_conversations_found(settings, loaded, monkeypatch):
    earlier = chats.new_id()
    loaded.create_chat(earlier, "Jonathan's department")
    loaded.add_chat_turn(earlier, "user", "Which department is Jonathan Reyes in?")
    loaded.add_chat_turn(earlier, "assistant", "The staff list leaves Jonathan Reyes's department blank [1].", {"mode": "model"})
    failed = chats.new_id()
    loaded.create_chat(failed)
    loaded.add_chat_turn(failed, "user", "Who manages Jonathan Reyes now?")
    loaded.add_chat_turn(failed, "assistant", "Jonathan Reyes reports to Bob.\n\n(The local model stopped answering partway.)", {"mode": "model", "failed": True})
    loaded.add_chat_turn(failed, "user", "Jonathan Reyes manager?")
    loaded.add_chat_turn(failed, "assistant", "Emails mentioning Jonathan Reyes: …", {"mode": "lookup"})
    current = chats.new_id()
    loaded.create_chat(current)
    chats.add_file(loaded, settings, current, "staff list.csv", "text/csv", ROSTER)

    asked = []

    def fake_stream(_settings, messages, *, max_tokens):
        asked.append(messages[-1]["content"])
        yield "Jonathan Reyes has no department listed; his manager is Priya Raman (staff list.csv) [1]."

    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "needs_more_context", lambda _s: False)
    monkeypatch.setattr(assistant, "stream_text", fake_stream)
    past = chats.past_context(loaded, "Who manages Jonathan Reyes?", exclude=current)
    assert "leaves Jonathan Reyes's department blank" in past and "[1]" not in past
    assert "Bob" not in past and "Emails mentioning" not in past, "failed and lookup replies are not learned from"
    list(
        assistant.answer_stream(
            loaded, settings, "Who manages Jonathan Reyes?", uploads=chats.chat_mail(loaded, current), past=past, email_id="demo-question"
        )
    )
    prompt = asked[0]
    assert "[1] Files the user added to this chat (not an email)\nFiles: 1. staff list.csv (CSV table)" in prompt, "added files lead when the question names nothing on screen"
    assert "A2 (Employee): Jonathan Reyes | B2 (Department): not listed | C2 (Manager): Priya Raman" in prompt
    assert "From earlier conversations with this person" in prompt
    assert chats.past_context(loaded, "What is the weather like?", exclude=current) == ""


def test_the_pop_out_window_is_the_chat_alone(client):
    page = client.get("/chat/window").text
    assert 'class="chat-window"' in page and 'id="chat"' in page and "chat-fab" not in page and 'class="rail"' not in page
    home = client.get("/").text
    assert 'data-action="popout-chat"' in home and 'data-action="chat-history"' in home and 'data-action="attach-file"' in home


def test_each_file_past_the_upload_limit_is_reported(client):
    chat_id = client.post("/chats", headers=PAGE).json()["id"]
    files = [("files", (f"f{i}.csv", f"a,b\n{i},2\n".encode(), "text/csv")) for i in range(chats.MAX_FILES + 2)]
    added = client.post(f"/chats/{chat_id}/files", headers=PAGE, files=files).json()
    assert [file["name"] for file in added["files"]] == [f"f{i}.csv" for i in range(chats.MAX_FILES)]
    assert added["problems"] == [
        f"f{i}.csv wasn't added: up to {chats.MAX_FILES} files can be added at once." for i in (chats.MAX_FILES, chats.MAX_FILES + 1)
    ], "the page shows these, so no file goes missing without a word"


def test_a_long_file_name_is_shortened_but_keeps_its_extension(client, settings):
    chat_id = client.post("/chats", headers=PAGE).json()["id"]
    long_name = "Quarterly reconciliation of intercompany balances " * 3 + ".csv"
    added = client.post(f"/chats/{chat_id}/files", headers=PAGE, files=[("files", (long_name, ROSTER, "text/csv"))]).json()
    assert added["problems"] == []
    [card] = added["files"]
    assert card["name"] == long_name[:146] + ".csv" and len(card["name"]) == 150 and card["text"]
    assert (settings.inbox_extracted / f"chat-{chat_id}" / card["name"]).read_bytes() == ROSTER
    assert "Jonathan Reyes" in client.get(card["href"]).text
    assert client.post(f"/chats/{chat_id}/files/1/delete", headers=PAGE).json()["files"] == []
    assert not (settings.inbox_extracted / f"chat-{chat_id}" / card["name"]).exists()
    assert chats._kept_name("C:\\Users\\me\\" + "x" * 200 + ".pdf") == "x" * 146 + ".pdf"
    assert chats._kept_name("short name.xlsx") == "short name.xlsx"
