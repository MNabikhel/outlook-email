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
