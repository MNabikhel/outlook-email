"""Ask CloseDesk and the local model: slow loads never double up, past answers aren't taken as checked material,
fraud-held mail stays out of a chat's history, reasoning isn't shown as the answer, drafts survive a dropped check."""

from __future__ import annotations

import json

import httpx

from controller_inbox import local_llm
from controller_inbox.local_llm import LocalReader
from controller_inbox.models import ActionStatus
from controller_inbox.overnight import read_queue


def _reader(settings, handler) -> LocalReader:
    return LocalReader(settings, model="tiny-3b", client=httpx.Client(transport=httpx.MockTransport(handler)))


def _reply(obj: dict) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(obj)}, "finish_reason": "stop"}]})


# 2. A model load that takes longer than CloseDesk waits is taken for a failed one, and a second model is loaded ---


from test_lm_studio_models import CHAT, CODER, LMStudio, _serve  # noqa: E402


class _SlowLoad(LMStudio):
    """LM Studio loading a big model takes longer than CloseDesk's wait: the request times out, the load goes on."""

    def __init__(self, loaded, slow: set[str]):
        super().__init__(loaded=loaded)
        self.slow_loads = slow

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/models/load":
            payload = json.loads(request.content)
            if payload["model"] in self.slow_loads:
                self.calls.append(("load", payload["model"], payload.get("context_length")))
                self.loaded.append(payload["model"])  # still loading in LM Studio, using the memory
                self.slow_loads.discard(payload["model"])
                raise httpx.ReadTimeout("timed out", request=request)
        return super().__call__(request)


def test_switching_the_chat_model_never_has_two_big_models_loading_at_once(settings, monkeypatch):
    settings.llm = None
    server = _SlowLoad(loaded=[CODER], slow={CHAT})
    _serve(monkeypatch, server)
    local_llm.use_chat_model(settings, CHAT)
    loads = [call for call in server.calls if call[0] == "load"]
    # The slow load of CHAT was taken for a failure and CODER was loaded back beside it.
    assert len(loads) == 1, f"a second model was loaded while the first was still loading: {server.calls}"


def test_raising_the_context_never_loads_the_model_twice(settings, monkeypatch):
    settings.llm = None
    settings.min_context_tokens = 65536
    server = _SlowLoad(loaded=[CHAT], slow={CHAT})
    _serve(monkeypatch, server)
    assert local_llm.needs_more_context(settings)
    local_llm.ensure_context(settings)
    loads = [call for call in server.calls if call[0] == "load"]
    assert len(loads) == 1, f"the model was loaded again while the longer load was still going: {server.calls}"


# 4. A figure the model made up in an earlier conversation counts as "read" when this answer is checked ------------


def test_a_figure_from_an_earlier_answer_is_not_taken_as_checked_against_the_files(loaded, settings):
    from controller_inbox import agent, assistant

    email = loaded.get_email("demo-inv-10482")  # the invoice says "Amount due $12,850.00"
    # An earlier chat's answer, written by the model (it misread the amount), comes back as background.
    past = (
        "From earlier conversations with this person (background only, ...):\n"
        '- 2026-10-01, asked "how much is the Northwind invoice INV-10482?": answered "The amount due is $13,250.00."'
    )
    ws = agent.Workspace(loaded, settings, [email], question="How much do we owe Northwind on INV-10482?", current_id=email.id, past=past)
    answer = "The amount due on INV-10482 is $13,250.00 [1]."
    events = list(assistant._checked(ws, answer, history=[], today="2026-10-20"))
    checks = [item for event in events if event["type"] == "check" for item in event["items"]]
    assert any("13,250.00" in item for item in checks), (
        "the made-up $13,250.00 (the file says $12,850.00) went out unflagged: the earlier answer grounded it"
    )


def test_a_figure_from_a_note_the_model_wrote_earlier_is_not_taken_as_checked_against_the_files(loaded, settings):
    from controller_inbox import agent, assistant

    email = loaded.get_email("demo-inv-10482")
    # A note the model wrote with the note tool on an earlier question, kept with the email.
    loaded.add_finding(email.id, "Northwind INV-10482 amount due is $13,250.00 (INV-10482.pdf page 1)", question="amount due", at="2026-10-01T00:00:00+00:00")
    ws = agent.Workspace(loaded, settings, [email], question="What is the amount due on Northwind INV-10482?", current_id=email.id)
    answer = "The amount due on INV-10482 is $13,250.00 [1]."
    events = list(assistant._checked(ws, answer, history=[], today="2026-10-20"))
    checks = [item for event in events if event["type"] == "check" for item in event["items"]]
    assert any("13,250.00" in item for item in checks), "a figure only the model's own earlier note has went out unflagged"


# 5. The overnight summary of a file is still served after the user fixed a figure on its page --------------------


def test_summarize_this_file_does_not_serve_the_figure_the_user_fixed_on_the_page(loaded, settings, monkeypatch):
    from controller_inbox import agent, assistant

    email = loaded.get_email("demo-inv-10482")
    att = email.attachments[0]
    # A longer invoice (overnight summaries are written for files of 1,500 characters or more).
    att.extracted_text += "\n" + "\n".join(f"Line {n}: Office supplies, carton {n}, see packing list" for n in range(60))
    loaded.upsert_email(email)
    att = loaded.get_email(email.id).attachments[0]
    assert "$12,850.00" in att.extracted_text and len(att.extracted_text) >= agent.SUMMARY_MIN_CHARS
    loaded.save_file_summary(att.id, agent.summary_key(att), "- Amount due $12,850.00, due October 5, 2026 (page 1)")

    # OCR misread the amount: the user fixes it on the Page tab. Same length, so the summary's key doesn't change.
    loaded.add_page_fix({
        "attachment_id": att.id, "sha256": att.sha256, "page": 1, "kind": "text",
        "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.05, "was": "$12,850.00", "now": "$12,580.00", "sender": email.sender_email,
    })
    assert "$12,580.00" in loaded.get_email(email.id).attachments[0].extracted_text

    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    # The model reads the file as shown instead (a stand-in: with no model at all, the lookup answer quotes the
    # email's subject, which has the amount as the sender wrote it).
    monkeypatch.setattr(assistant, "complete_text", lambda *_a, **_k: "SQL: NONE")
    monkeypatch.setattr(assistant, "stream_text", lambda *_a, **_k: iter(["- Amount due $12,580.00, due October 5, 2026 [1]"]))
    events = list(assistant.answer_stream(loaded, settings, "Summarize the attached file", email_id=email.id))
    assert not any(e["type"] == "step" and "written during the overnight reading" in e["text"] for e in events)
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "12,850.00" not in text, f"the summary served still has the figure the user corrected: {text!r}"


# 6. A reasoning model's thinking that quotes the prompt is taken for an echo: its reasoning is shown as the answer --


def test_reasoning_that_quotes_the_instructions_before_a_lone_closing_think_tag_is_not_the_answer(store, settings, mail, monkeypatch):
    from controller_inbox import assistant

    # A chat template that opens <think> in the prompt leaves only "</think>" in the reply (as in test_chat_review).
    reply = (
        "Okay, the user wants where the offsite is. The instructions say: Check your draft answer against the text "
        "above before the user sees it. Maybe it costs $9,999? The memo says Lisbon.\n</think>\n\n"
        "The offsite is in Lisbon on 14 November [1]."
    )
    budget = mail["Q4 budget draft"]
    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "complete_text", lambda *_a, **_k: "SQL: NONE")
    monkeypatch.setattr(assistant, "stream_text", lambda *_a, **_k: iter([reply[i : i + 7] for i in range(0, len(reply), 7)]))
    events = list(assistant.answer_stream(store, settings, "Where is the offsite in this memo?", email_id=budget.id))
    shown = "".join(e["text"] for e in events if e["type"] == "delta")
    revised = [e["text"] for e in events if e["type"] == "revise"]
    final = revised[-1] if revised else shown
    assert "Lisbon on 14 November [1]" in final and "$9,999" not in final, f"the answer shown was the model's reasoning: {final!r}"


# 7. The model server goes away during the check pass: a finished draft is thrown away for half a sentence ----------


def test_a_finished_draft_is_kept_when_the_server_stops_during_the_check_pass(store, settings, mail, monkeypatch):
    from controller_inbox import assistant, local_llm

    budget = mail["Q4 budget draft"]
    draft = "Ads went up by 500, from 1,000 to 1,500, and Travel by 50, so the total change is 550 in Q4 budget.xlsx."
    replies = iter([
        local_llm.ToolReply(content="", calls=[{"id": "c1", "name": "read_file", "arguments": {"email": "1", "file": "Q4 budget.xlsx"}}]),
        local_llm.ToolReply(content=draft),
    ])
    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "complete_text", lambda *_a, **_k: "SQL: NONE")
    monkeypatch.setattr(assistant, "chat_with_tools", lambda *_a, **_k: next(replies))

    def dies(*_a, **_k):
        yield "The total"
        raise httpx.RemoteProtocolError("Server disconnected without sending a response.")

    monkeypatch.setattr(assistant, "stream_text", dies)
    events = list(assistant.answer_stream(store, settings, "How much did the budget change in total from Q3 to Q4?", email_id=budget.id))
    assert any(e["type"] == "step" and "Checking the answer" in e["text"] for e in events), "the check pass ran"
    shown = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "550" in shown, f"the finished draft was dropped for a cut-off check pass: {shown!r}"


# 8. A model that can't stop thinking runs out of room, and is asked again with the same room ----------------------


def test_a_model_that_always_thinks_and_ran_out_of_room_is_asked_again_with_more(settings, monkeypatch):
    from test_reasoning_models import FakeLMStudio, _serve as serve_reasoning

    settings.llm = None
    server = FakeLMStudio(options=["on"], think_tokens=2500)  # DeepSeek-R1 style: thinking can't be turned off
    serve_reasoning(monkeypatch, server)
    try:
        text = "".join(local_llm.stream_text(settings, [{"role": "system", "content": "chat"}, {"role": "user", "content": "hi"}], max_tokens=500))
    except local_llm.EmptyReply:
        text = ""
    budgets = [(call["max_tokens"], call.get("reasoning_effort")) for call in server.calls]
    assert text, f"no answer: the retry was the same request that ran out of room ({budgets})"


# 9. An earlier answer in the same chat that quoted an email's files is given back to the model after it is flagged --


def test_the_same_chats_earlier_answer_from_a_now_flagged_emails_files_is_not_given_to_the_model(store, settings, mail, monkeypatch):
    from fastapi.testclient import TestClient

    from controller_inbox import assistant, chats, web
    from controller_inbox.fraud import attachments_locked

    scam = store.get_email(mail["Updated remittance details"].id)
    assert attachments_locked(scam)
    # Before it was flagged (or while it was marked "Not fraud"), the chat read its file and quoted the account.
    chat_id = chats.new_id()
    store.create_chat(chat_id)
    store.add_chat_turn(chat_id, "user", "What account does the bank letter give?")
    store.add_chat_turn(
        chat_id, "assistant", "The new bank letter.pdf says to remit to account 5566778899, routing 021000021 [1].",
        {"sources": [{"n": 1, "id": scam.id, "subject": scam.subject}]},
    )
    # The same exchange, kept in another conversation, is kept out of the model's background (chats.past_context).
    assert "5566778899" not in chats.past_context(store, "What account does the bank letter give?")

    sent = []
    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "complete_text", lambda *_a, **_k: "SQL: NONE")

    def stream(_settings, messages, **_k):
        sent.append(messages)
        yield "Please verify by phone [1]."

    monkeypatch.setattr(assistant, "stream_text", stream)
    client = TestClient(web.create_app(settings, store))
    reply = client.post("/chat", json={"message": "Should I update the vendor's account?", "chat_id": chat_id, "email_id": scam.id},
                        headers={"x-closedesk": "1"})
    assert reply.status_code == 200 and sent, reply.text
    prompt = json.dumps(sent)
    assert "5566778899" not in prompt, "text from a fraud-held email's file reached the model through the chat history"


# Reviewer concerns -----------------------------------------------------------------------------------------------


def test_the_context_isnt_raised_under_a_request_the_model_is_answering(settings, monkeypatch):
    settings.llm = None
    settings.min_context_tokens = 65536
    server = LMStudio(loaded=[CHAT])
    _serve(monkeypatch, server)
    with local_llm._using(CHAT):  # the overnight reading, a page read or another chat is using it
        said = local_llm.ensure_context(settings)
    assert server.calls == [] and "answering another request" in said
    assert local_llm.needs_more_context(settings), "tried again once the model is free"


def test_the_chat_model_isnt_switched_while_an_answer_is_being_written(settings, monkeypatch):
    settings.llm = None
    server = LMStudio(loaded=[CODER])
    _serve(monkeypatch, server)
    with local_llm._using(CODER):
        said = local_llm.use_chat_model(settings, CHAT)
    assert server.calls == [] and "Try again once it is done" in said


class _NeverShows(LMStudio):
    """A load that times out and doesn't show up in LM Studio's list within CloseDesk's wait."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/models/load":
            self.calls.append(("load", json.loads(request.content)["model"]))
            raise httpx.ReadTimeout("timed out", request=request)
        return super().__call__(request)


def test_a_load_still_going_past_the_wait_is_said_plainly_and_no_other_load_starts(settings, monkeypatch):
    settings.llm = None
    monkeypatch.setattr(local_llm, "LOAD_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(local_llm, "LOAD_POLL_SECONDS", 0.0)
    server = _NeverShows(loaded=[CODER])
    _serve(monkeypatch, server)
    said = local_llm.use_chat_model(settings, CHAT)
    assert "still loading" in said
    assert [call for call in server.calls if call[0] == "load"] == [("load", CHAT)], "the unloaded model wasn't loaded back beside it"
    # While it may still be loading, nothing else is loaded.
    monkeypatch.setattr(local_llm, "LOAD_WAIT_SECONDS", 3600.0)
    assert "still loading" in local_llm.use_chat_model(settings, CODER)
    assert [call for call in server.calls if call[0] == "load"] == [("load", CHAT)]


def test_an_answer_cut_off_for_room_says_so(store, settings, mail, monkeypatch):
    from controller_inbox import assistant

    def stream(*_a, finished=None, **_k):
        yield "The offsite is in Lisbon on"
        if finished is not None:
            finished["reason"] = "length"

    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "complete_text", lambda *_a, **_k: "SQL: NONE")
    monkeypatch.setattr(assistant, "stream_text", stream)
    events = list(assistant.answer_stream(store, settings, "Where is the offsite in this memo?", email_id=mail["Q4 budget draft"].id))
    shown = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "cut off" in shown, shown


def test_the_same_tool_call_asked_again_isnt_run_again(store, settings, mail, monkeypatch):
    from controller_inbox import assistant

    budget = mail["Q4 budget draft"]
    call = {"id": "c1", "name": "read_file", "arguments": {"email": "1", "file": "Q4 budget.xlsx"}}
    replies = iter([
        local_llm.ToolReply(content="", calls=[call]),
        local_llm.ToolReply(content="", calls=[{**call, "id": "c2"}]),
        local_llm.ToolReply(content="Ads went up by 500 in Q4 budget.xlsx [1]."),
    ])
    sent = []

    def tools(_settings, messages, *_a, **_k):
        sent.append([dict(m) for m in messages])
        return next(replies)

    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "complete_text", lambda *_a, **_k: "SQL: NONE")
    monkeypatch.setattr(assistant, "chat_with_tools", tools)
    monkeypatch.setattr(assistant, "stream_text", lambda *_a, **_k: iter(["Ads went up by 500 [1]."]))
    events = list(assistant.answer_stream(store, settings, "How much did the budget change in total from Q3 to Q4?", email_id=budget.id))
    reads = [e for e in events if e["type"] == "step" and "Q4 budget.xlsx" in e["text"] and "Read" in e["text"]]
    assert len(reads) <= 1, reads
    last_tool = [m for m in sent[-1] if m.get("role") == "tool"][-1]
    assert last_tool["content"].startswith("Already done"), last_tool


def test_fields_shown_on_the_page_are_given_to_the_chat_as_the_emails_facts(store, settings, mail):
    from controller_inbox import assistant

    email = mail["Q4 budget draft"]
    email.extracted.taught = {"total": {"value": "4210.0", "by": "you", "label": "Total due"}}
    block = assistant._source_block(1, email, 500, on_screen=True)
    assert "Total due: 4,210.00 (shown by the user)" in block
