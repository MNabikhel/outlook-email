import json

import httpx
import pytest

from controller_inbox import local_llm
from controller_inbox.local_llm import LocalReader, build_prompt, check_model, llm_active, parse_json_object
from controller_inbox.overnight import read_queue
from controller_inbox.reading import build_packet, overlay_reading

GOOD = {
    "category": "ap_invoice",
    "folder": "important",
    "importance": "high",
    "summary": "Northwind invoice to enter.",
    "actions": [],
    "why": "Vendor bill.",
}


@pytest.fixture(autouse=True)
def _fresh_status_cache():
    local_llm._status_cache.clear()
    yield
    local_llm._status_cache.clear()


def _reply(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def _reader(settings, handler) -> LocalReader:
    return LocalReader(settings, model="tiny-3b", client=httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize(
    "content",
    [
        json.dumps(GOOD),
        "```json\n" + json.dumps(GOOD) + "\n```",
        "Sure! Here is the reading:\n" + json.dumps(GOOD) + "\nLet me know if you need more.",
        json.dumps(GOOD)[:-1] + ",}",
        '{"note": "a {brace} in a string", ' + json.dumps(GOOD)[1:],
    ],
)
def test_parse_json_object_tolerates_small_model_habits(content):
    parsed = parse_json_object(content)
    assert parsed is not None
    assert parsed["category"] == "ap_invoice"


def test_parse_json_object_rejects_garbage():
    assert parse_json_object("") is None
    assert parse_json_object("I think this is an invoice.") is None
    assert parse_json_object("{not json at all") is None


def test_prompt_stays_inside_the_budget(loaded):
    email = loaded.get_email("demo-inv-10482")
    email.body_text = "Please pay. " * 3000
    for att in email.attachments:
        att.extracted_text = "Line item. " * 3000
    packet = build_packet(email, [])
    prompt = build_prompt(packet, budget=4000)
    assert len(prompt) <= 4300
    assert email.subject in prompt
    assert "Facts the scripts found:" in prompt and "INV-10482" in prompt
    assert "…[cut]" in prompt
    assert prompt.rstrip().endswith("}")


def test_reader_falls_back_when_server_rejects_structured_output(settings):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append("response_format" in body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": "response_format not supported"})
        return httpx.Response(200, json=_reply(json.dumps(GOOD)))

    reader = _reader(settings, handler)
    assert reader.read({"subject": "a"}) == GOOD
    assert reader.read({"subject": "b"}) == GOOD
    assert seen == [True, False, False], "asks plainly after the first rejection"
    assert reader.stats.read == 2


def test_reader_stops_when_the_server_is_down(settings):
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("connection refused")

    reader = _reader(settings, handler)
    assert reader.read({"subject": "a"}) is None
    assert reader.stopped
    assert "stopped answering" in reader.stats.stopped_reason
    assert reader.read({"subject": "b"}) is None
    assert len(calls) == 1, "no more calls after the server went away"


def test_reader_stops_after_three_unusable_replies(settings):
    def handler(request):
        return httpx.Response(200, json=_reply("I am not sure what this is."))

    reader = _reader(settings, handler)
    for _ in range(3):
        assert reader.read({"subject": "x"}) is None
    assert reader.stopped
    assert reader.stats.failed == 3


def test_read_queue_leaves_mail_filed_when_the_model_dies(loaded, settings):
    replies = iter([json.dumps(GOOD)])

    def handler(request):
        try:
            return httpx.Response(200, json=_reply(next(replies)))
        except StopIteration:
            raise httpx.ConnectError("gone") from None

    result = read_queue(loaded, settings, limit=10, reader=_reader(settings, handler))
    assert len(result["read_ids"]) == 1
    assert "Stopped reading" in result["note"]
    assert loaded.counts()["waiting_on_bionic"] == 13
    assert all(email.folder for email in loaded.list_emails())


def test_auto_mode_uses_a_model_only_when_one_is_answering(settings, monkeypatch):
    def down(*args, **kwargs):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(local_llm.httpx, "get", down)
    assert settings.llm_mode == "off"  # conftest pins tests to off
    settings.llm = None
    assert settings.llm_mode == "auto"
    assert llm_active(settings) is False
    assert "No local model answering" in check_model(settings).describe()

    local_llm._status_cache.clear()

    def up(url, **kwargs):
        return httpx.Response(
            200,
            json={"data": [{"id": "text-embedding-nomic"}, {"id": "qwen2.5-3b-instruct"}]},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(local_llm.httpx, "get", up)
    status = check_model(settings)
    assert status.active and status.model == "qwen2.5-3b-instruct", "skips embedding models"
    assert llm_active(settings) is True

    settings.llm = False
    assert llm_active(settings) is False


def test_env_value_auto_parses(monkeypatch):
    from controller_inbox.config import Settings

    monkeypatch.setenv("CONTROLLER_INBOX_LLM", "auto")
    assert Settings(_env_file=None).llm_mode == "auto"
    monkeypatch.setenv("CONTROLLER_INBOX_LLM", "true")
    assert Settings(_env_file=None).llm_mode == "on"


def test_invented_amount_keeps_the_script_summary(loaded):
    email = loaded.get_email("demo-inv-10482")
    script_line = email.summary
    overlay_reading(email, {**GOOD, "summary": "Pay Northwind $99,999.00 today."})
    assert email.summary == script_line
    assert any(r.startswith("Guard:") and "$99,999.00" in r for r in email.importance_reasons)


def test_real_amount_summary_is_kept(loaded):
    email = loaded.get_email("demo-inv-10482")
    amount = email.extracted.primary_amount
    line = f"Northwind invoice INV-10482 for ${amount:,.2f} needs entering."
    overlay_reading(email, {**GOOD, "summary": line})
    assert email.summary == line
    assert not any(r.startswith("Guard:") for r in email.importance_reasons)


def test_made_up_due_date_is_dropped(loaded):
    email = loaded.get_email("demo-inv-10482")
    known = email.extracted.primary_due
    overlay_reading(
        email,
        {
            **GOOD,
            "actions": [
                {"title": "Enter the invoice", "due": known, "priority": "high"},
                {"title": "Chase the approver", "due": "2031-01-01", "priority": "medium"},
            ],
        },
    )
    by_title = {a.title: a for a in email.actions}
    assert by_title["Enter the invoice"].due_date == known
    assert by_title["Chase the approver"].due_date == known, "the message has one date, so that one is used"
    assert "2031-01-01" in by_title["Chase the approver"].detail
    assert any("Replaced 1 due date" in r and "own date" in r for r in email.importance_reasons)


def test_made_up_due_date_on_undated_mail_is_dropped(loaded):
    email = next(
        e for e in loaded.list_emails(limit=100)
        if not e.extracted.due_dates and not any(a.extracted_fields.due_dates for a in e.attachments)
    )
    overlay_reading(email, {**GOOD, "actions": [{"title": "Reply", "due": "2031-01-01", "priority": "low"}]})
    action = next(a for a in email.actions if a.title == "Reply")
    assert action.due_date is None
    assert "2031-01-01" in action.detail


def test_task_due_this_week_stays_in_important(loaded):
    email = loaded.get_email("demo-pbc")
    assert email.extracted.primary_due == "2026-09-25"
    overlay_reading(
        email,
        {
            **GOOD,
            "category": "audit_request",
            "folder": "informational",
            "importance": "low",
            "actions": [{"title": "Send the PBC list", "due": "2026-09-25", "priority": "medium"}],
        },
    )
    assert email.folder == "important"
    assert email.importance.value == "medium"
    assert any("Kept in Important" in r and "2026-09-25" in r for r in email.importance_reasons)


def test_model_can_file_mail_without_pressing_tasks_as_fyi(loaded):
    email = loaded.get_email("demo-expense")
    overlay_reading(email, {**GOOD, "category": "expense_report", "folder": "informational", "importance": "low"})
    assert email.folder == "informational"


def test_fraud_summary_always_warns(loaded):
    email = loaded.get_email("demo-bec-wire")
    overlay_reading(email, {**GOOD, "category": "ap_invoice", "summary": "Vendor updated their bank account."})
    assert email.folder == "important"
    assert "verify by phone" in email.summary.lower()


def test_model_treating_a_bank_change_as_routine_is_overruled(loaded):
    email = loaded.get_email("demo-bec-wire")
    overlay_reading(
        email,
        {
            **GOOD,
            "category": "other",
            "folder": "informational",
            "importance": "medium",
            "summary": "Vendor has new bank details; update the vendor record before the next payment run.",
            "actions": [
                {"title": "Update the vendor bank account in the ERP", "due": None, "priority": "medium"},
                {"title": "Call the vendor on the known number to verify", "due": None, "priority": "high"},
            ],
        },
    )
    assert (email.folder, email.importance.value) == ("important", "critical")
    assert email.summary.startswith("Possible payment-instruction fraud")
    titles = [a.title for a in email.actions]
    assert "Update the vendor bank account in the ERP" not in titles
    assert "Call the vendor on the known number to verify" in titles
    guards = [r for r in email.importance_reasons if r.startswith("Guard:")]
    assert any("Kept in Important as critical" in g and "informational" in g for g in guards)
    assert any("Removed 1 task" in g for g in guards)
    assert any("fraud warning" in g for g in guards)
