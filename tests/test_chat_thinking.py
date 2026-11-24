"""A model's reasoning, shown as it comes: it never becomes part of the answer, the saved conversation, or what the
model is given of the conversation next time. The fake LM Studio is tests/test_chat_time.py's."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from test_chat_time import PAGE, QUESTION, REASONING, lm  # noqa: F401 - lm is the fixture

from controller_inbox import chats, web


# The thinking, as it comes ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("think", ["field", "tags"])
def test_the_reasoning_streams_as_thinking_and_never_reaches_the_answer(settings, loaded, lm, think):  # noqa: F811
    lm(think=think)
    client = TestClient(web.create_app(settings, loaded))
    lines = [json.loads(line) for line in client.post("/chat", json={"message": QUESTION}, headers=PAGE).text.splitlines()]
    thinking = [line for line in lines if line["type"] == "thinking"]
    assert "".join(line["text"] for line in thinking) == "".join(REASONING)
    kinds = [line["type"] for line in lines]
    assert kinds.index("thinking") < kinds.index("delta"), "the thinking comes before the answer"
    states = [line["state"] for line in lines if line["type"] == "progress"]
    assert states.index("thinking") < states.index("writing")
    answer = "".join(line["text"] for line in lines if line["type"] == "delta")
    assert answer.startswith("Reply to the auditor") and "Look" not in answer and "The user asks" not in answer
    # Not in the saved conversation, nor in what the model is given of it next time.
    turns = loaded.chat_turns(lines[0]["id"])
    assert all("The user asks" not in json.dumps(turn) for turn in turns)
    assert all("The user asks" not in turn["text"] for turn in chats.history_for_model(loaded, turns))


def test_the_reasoning_keeps_its_line_breaks_and_spaces():
    """LM Studio sends the reasoning a token at a time, and a line break or a space often comes as a piece of its
    own. Those were dropped as empty, so the Thinking box ran sentences and words together."""
    import httpx

    from controller_inbox.local_llm import Reply, Thought, _stream_pieces

    parts = ["\n", "First I read", " the invoice.", "\n\n", "Then", " ", "the date."]
    chunks = [{"choices": [{"delta": {"reasoning_content": part}}]} for part in parts]
    chunks.append({"choices": [{"delta": {"content": "Due Friday."}, "finish_reason": "stop"}]})
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    response = httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)
    reply = Reply("")
    pieces = list(_stream_pieces(response, reply, thoughts=True))
    thought = "".join(piece.text for piece in pieces if isinstance(piece, Thought))
    assert thought == "First I read the invoice.\n\nThen the date."
    assert reply.reasoning == thought
    assert [piece for piece in pieces if not isinstance(piece, Thought)] == ["Due Friday."]


def test_a_reply_with_only_blank_reasoning_has_none():
    import httpx

    from controller_inbox.local_llm import Reply, _stream_pieces

    chunks = [{"choices": [{"delta": {"reasoning_content": "\n\n"}}]}, {"choices": [{"delta": {"content": "Due Friday."}}]}]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    reply = Reply("")
    list(_stream_pieces(httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body), reply, thoughts=True))
    assert reply.reasoning == ""


def test_a_retry_s_thinking_is_set_apart_from_the_first_try_s(settings, loaded, monkeypatch):
    """A model that can't stop thinking ran out of room and was asked again with more: the second try's reasoning was
    run straight on from the first's, cut off mid-word, with nothing to say the model was asked again."""
    from test_reasoning_models import THOUGHT, FakeLMStudio
    from test_reasoning_models import _serve as serve_reasoning

    from controller_inbox import local_llm
    from controller_inbox.assistant import answer_stream

    settings.llm = None
    server = FakeLMStudio(options=["on"], think_tokens=2500)  # 2,048 tokens run out; 4,096 are enough
    serve_reasoning(monkeypatch, server)
    local_llm._status_cache.clear()
    events = list(answer_stream(loaded, settings, QUESTION))
    assert [call["max_tokens"] for call in server.calls if call.get("stream")][-2:] == [2048, 4096]
    kinds = [(event["type"], event.get("state")) for event in events]
    first, second = [i for i, kind in enumerate(kinds) if kind == ("progress", "thinking")][-2:]
    between = events[first + 1 : second]
    assert {"type": "step", "text": "The model ran out of room while it thought; asking it again with more room"} in between
    assert any(event["type"] == "progress" and event["state"] == "reading" for event in between), "the thinking ended"
    thought = "".join(event["text"] for event in events if event["type"] == "thinking")
    assert thought == THOUGHT + THOUGHT
    assert "".join(event["text"] for event in events if event["type"] == "delta") == "Start with [1]; it needs you today."


# A model whose chat template opens <think> itself, on a server that doesn't separate the reasoning -----------------


def _lone_close_server(settings, monkeypatch, think_tokens: int):
    """test_reasoning_models' DeepSeek-R1 distill: its reply is reasoning, then a lone "</think>", then the answer, and
    with fewer than ``think_tokens`` tokens it is cut off while it still reasons."""
    from test_reasoning_models import LoneCloseServer
    from test_reasoning_models import _serve as serve_reasoning

    settings.llm = None
    server = LoneCloseServer(think_tokens=think_tokens)
    serve_reasoning(monkeypatch, server)
    return server


def _ask(client, question: str, chat_id: str = "") -> list[dict]:
    """Asked about the Northwind invoice (an answer drawing on the sample's fraud email isn't sent back at all)."""
    body = {"message": question, "email_id": "demo-inv-10482", **({"chat_id": chat_id} if chat_id else {})}
    return [json.loads(line) for line in client.post("/chat", json=body, headers=PAGE).text.splitlines()]


def _shown(lines: list[dict]) -> str:
    """The answer as the page shows it: the pieces as they come, and a revised answer in their place."""
    text = ""
    for line in lines:
        if line["type"] == "delta":
            text += line["text"]
        elif line["type"] == "revise":
            text = line["text"]
    return text


def test_a_reply_that_closes_a_think_tag_it_never_opened_is_split_at_it_as_it_streams(settings, loaded, monkeypatch):
    """The first reply of such a model that closed its tag streamed its reasoning as the answer, under "Writing the
    answer", until the end, when the answer took its place. Now the reasoning moves to the Thinking box when the
    "</think>" comes, and only what follows streams as the answer."""
    _lone_close_server(settings, monkeypatch, think_tokens=100)
    client = TestClient(web.create_app(settings, loaded))
    lines = _ask(client, "When is it due?")
    first = next(i for i, line in enumerate(lines) if line["type"] == "delta" and "Friday" in line["text"])
    assert _shown(lines[:first]) == "", "the reasoning shown so far was taken back before the answer came"
    assert "REASONMARK" in "".join(line["text"] for line in lines[:first] if line["type"] == "thinking")
    after = lines[first:]
    assert not any("REASONMARK" in line.get("text", "") for line in after if line["type"] in ("delta", "revise"))
    states = [line.get("state") for line in lines[:first] if line["type"] == "progress"]
    assert states[-2:] == ["thinking", "writing"], states
    assert _shown(lines).strip() == "It is due on Friday [1]."
    assert "REASONMARK" not in loaded.chat_turns(lines[0]["id"])[-1]["text"]


def test_reasoning_cut_off_before_its_close_is_never_sent_back_to_the_model(settings, loaded, monkeypatch):
    """Cut off at 500 tokens before its "</think>", the reasoning can't be told from an answer and is shown as it came.
    Once the model is known to think this way, that reply is never sent back as what it answered."""
    server = _lone_close_server(settings, monkeypatch, think_tokens=1000)
    client = TestClient(web.create_app(settings, loaded))
    lines = _ask(client, "When is it due?")
    chat_id = lines[0]["id"]
    assert "REASONMARK" in _shown(lines), "kept as it came: nothing said it was reasoning"
    server.think_tokens = 100
    _ask(client, "Who sent it?", chat_id)  # this reply closes the tag: the model is known to think so
    _ask(client, "How much is it?", chat_id)
    sent = server.calls[-1]["messages"]
    assert [m for m in sent if m["role"] == "assistant"], "the conversation so far is sent"
    assert not any("REASONMARK" in m["content"] for m in sent if m["role"] == "assistant"), sent


def test_what_closedesk_learned_about_a_models_thinking_is_kept_for_the_next_start(settings, loaded, monkeypatch):
    """Each morning's launch started over: a model known to open <think> itself, or to think too long, was asked as if
    it were new, cut off while it reasoned, and its reasoning shown as the answer again."""
    from test_reasoning_models import FakeLMStudio, LoneCloseServer
    from test_reasoning_models import _serve as serve_reasoning

    from controller_inbox import local_llm

    _lone_close_server(settings, monkeypatch, think_tokens=100)
    client = TestClient(web.create_app(settings, loaded))
    _ask(client, "When is it due?")
    server = FakeLMStudio(options=["on"], think_tokens=9000)  # thinks past twice the usual room
    serve_reasoning(monkeypatch, server)
    local_llm._status_cache.clear()
    with pytest.raises(local_llm.ThinksTooLong):
        list(local_llm.stream_text(settings, [{"role": "user", "content": "hi"}], max_tokens=500))
    learned = (local_llm._opens_think, local_llm._reasoning_seen, local_llm._thinks_too_long)
    assert LoneCloseServer.MODEL in local_llm._opens_think and local_llm._thinks_too_long
    before = [set(kept) for kept in learned]
    for kept in learned:  # CloseDesk is closed and started again the next morning
        kept.clear()
    web.create_app(settings, loaded)
    assert [set(kept) for kept in learned] == before
