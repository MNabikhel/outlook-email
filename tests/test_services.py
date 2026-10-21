"""What the classic pages and the workspace share (services.py, web_common.py, mail_identity.py), and the two
views agreeing on it: both 404 an email or task that isn't there, and say the same thing when they do a change."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from controller_inbox import fraud, mail_identity, services
from controller_inbox.models import RawMessage
from controller_inbox.pipeline import ingest_demo
from controller_inbox.web import create_app
from controller_inbox.web_common import NOTICES, attachment_or_404
from helpers import PAGE

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def demo(store, settings):
    ingest_demo(store, settings, now=NOW)
    return store


# Classic routes that changed something that wasn't there now say so, as the workspace's do -------------------


def test_classic_routes_404_an_email_or_task_that_isnt_there(settings, demo):
    client = TestClient(create_app(settings, demo))
    assert client.post("/inbox/no-such-email/findings/clear", follow_redirects=False).status_code == 404
    assert client.post("/actions/no-such-task/complete", follow_redirects=False).status_code == 404
    assert client.post("/actions/no-such-task/reopen", follow_redirects=False).status_code == 404
    api = TestClient(create_app(settings, demo), headers=PAGE)
    assert api.post("/api/mail/no-such-email/findings/clear").status_code == 404
    assert api.post("/api/tasks/no-such-task", json={"status": "done"}).status_code == 404


def test_classic_and_workspace_complete_and_reopen_the_same_task(settings, demo):
    action = demo.list_actions(status="open")[0][0]
    client = TestClient(create_app(settings, demo))
    assert client.post(f"/actions/{action.id}/complete", follow_redirects=False).status_code == 303
    assert demo.list_actions(status="done")[0][0].id == action.id
    api = TestClient(create_app(settings, demo), headers=PAGE)
    assert api.post(f"/api/tasks/{action.id}", json={"status": "open"}).json()["status"] == "open"
    assert api.post(f"/api/tasks/{action.id}", json={"status": "later"}).status_code == 400


def test_the_workspace_says_what_the_classic_notice_says(settings, demo):
    api = TestClient(create_app(settings, demo), headers=PAGE)
    cleared = api.post("/api/mail/demo-bec-wire/findings/clear").json()
    assert cleared["message"] == NOTICES["findings-cleared"]
    verdict = api.post("/api/mail/demo-bec-wire/fraud", json={"choice": "fraud"}).json()
    assert verdict["message"] == NOTICES["fraud-reported"]
    refused = api.post("/api/mail/demo-bec-wire/fraud", json={"choice": "maybe"})
    assert refused.status_code == 400 and refused.json()["detail"] == NOTICES["fraud-invalid"]


# services ---------------------------------------------------------------------------------------------------


def test_services_raise_missing_and_invalid(settings, demo):
    with pytest.raises(services.Missing):
        services.mark_done(demo, "no-such-email", True)
    with pytest.raises(services.Missing):
        services.clear_findings(demo, "no-such-email")
    with pytest.raises(services.Missing):
        services.code_invoice(demo, settings, "no-such-email")
    with pytest.raises(services.Missing):
        services.fraud_verdict(demo, settings, "no-such-email", "safe")
    with pytest.raises(services.Invalid) as refused:
        services.fraud_verdict(demo, settings, "demo-bec-wire", "nonsense")
    assert refused.value.notice == "fraud-invalid"
    with pytest.raises(services.Invalid):
        services.set_task_status(demo, "anything", "snoozed")
    services.mark_done(demo, "demo-bec-wire", True)
    assert demo.is_done("demo-bec-wire")


def test_digest_view_steps_between_saved_digests(store, settings):
    for day in ("2026-09-20", "2026-09-21", "2026-09-22"):
        store.save_digest(day, NOW.isoformat(), f"# {day}", f"<p>{day}</p>", {"day": day})
    view = services.digest_view(store, date(2026, 9, 25), "2026-09-21")
    assert (view["current"], view["newer"], view["older"]) == ("2026-09-21", "2026-09-22", "2026-09-20")
    assert view["payload"] == {"day": "2026-09-21"}
    latest = services.digest_view(store, date(2026, 9, 25))
    assert latest["current"] == "2026-09-22" and latest["newer"] == ""
    with pytest.raises(services.Missing):
        services.digest_view(store, date(2026, 9, 25), "2026-01-01")


def test_coding_rows_falls_back_to_review_for_an_unknown_status(settings, demo):
    assert services.coding_rows(demo, settings, "everything")[0] == "review"
    assert services.coding_rows(demo, settings, "all")[0] == "all"


def test_today_fields():
    fields = services.today_fields(date(2026, 9, 22), sample=True)
    assert fields == {
        "today": "2026-09-22",
        "today_long": "Tuesday, September 22, 2026",
        "days_to_close": 8,
        "close_date": "2026-09-30",
        "is_sample": True,
    }


def test_start_processing_says_busy_while_a_job_runs():
    class Busy:
        stopping = False

        def __init__(self, kind):
            self.kind = kind

        def start(self, target):
            return False

        def snapshot(self):
            return {"about": {"kind": self.kind}}

        def request_stop(self):
            return True

    assert services.start_processing(None, None, Busy("vision")) == "busy-vision"
    assert services.start_processing(None, None, Busy("outlook")) == "busy"
    assert services.stop_processing(Busy("vision")) == "stopping"


# web_common.attachment_or_404 -------------------------------------------------------------------------------


def test_attachment_or_404():
    from controller_inbox.models import DocumentType

    held = SimpleNamespace(id="held", flags=["fraud_risk"], category=DocumentType.OTHER, attachments=["invoice.pdf"])
    store = SimpleNamespace(get_email=lambda email_id: held if email_id == "held" else None)
    assert fraud.attachments_locked(held)
    with pytest.raises(HTTPException) as refused:
        attachment_or_404(store, "held", 1)
    assert refused.value.status_code == 403 and refused.value.detail == fraud.LOCKED_FILES
    assert attachment_or_404(store, "held", 1, allow_locked=True) == (held, "invoice.pdf")
    with pytest.raises(HTTPException) as missing:
        attachment_or_404(store, "held", 2, allow_locked=True)
    assert missing.value.status_code == 404 and missing.value.detail == "No such file on this email"
    with pytest.raises(HTTPException) as gone:
        attachment_or_404(store, "no-such-email", 1, missing="Gone.")
    assert gone.value.status_code == 404 and gone.value.detail == "Gone."


# mail_identity ----------------------------------------------------------------------------------------------


def _raw(**fields) -> RawMessage:
    base = dict(
        id="x", subject="Invoice 7", sender_name="A", sender_email="ap@acme.com", received_at=NOW, body_text="Hi", body_preview="Hi",
        has_attachments=False,
    )
    return RawMessage(**{**base, **fields})


def test_ids_from_a_message_id_ignore_its_brackets_and_case():
    assert mail_identity.stable_id("<ABC@Acme.com>") == mail_identity.stable_id("abc@acme.com")
    assert mail_identity.file_id(b"one").startswith("file-") and mail_identity.file_id(b"one") != mail_identity.file_id(b"two")


def test_another_message_is_told_apart_by_subject_sender_and_time():
    stored = SimpleNamespace(subject="Invoice 7", sender_email="AP@acme.com", received_at=NOW.isoformat())
    assert not mail_identity.another_message(stored, _raw(received_at=NOW + timedelta(seconds=90)))
    assert mail_identity.another_message(stored, _raw(received_at=NOW + timedelta(minutes=3)))
    assert mail_identity.another_message(stored, _raw(subject="Invoice 8"))
    assert mail_identity.another_message(stored, _raw(sender_email="ap@other.com"))
    # A subject or time taken from the copy's file says nothing about the message.
    assert not mail_identity.another_message(stored, _raw(subject="scan.msg", received_at=NOW + timedelta(days=2), from_file=("subject", "sent")))


def test_alternative_id_leaves_out_what_came_from_the_file():
    raw = _raw(internet_message_id="<scan@copier>")
    assert mail_identity.alternative_id(raw) != mail_identity.alternative_id(_raw(internet_message_id="<scan@copier>", subject="Other"))
    from_file = _raw(internet_message_id="<scan@copier>", from_file=("subject",))
    assert mail_identity.alternative_id(from_file) == mail_identity.alternative_id(
        _raw(internet_message_id="<scan@copier>", subject="another name", from_file=("subject",))
    )


def test_same_time_reads_a_time_without_a_zone_as_utc():
    assert mail_identity.same_time(NOW.replace(tzinfo=None), NOW + timedelta(minutes=2))
    assert not mail_identity.same_time(NOW, NOW + timedelta(minutes=2, seconds=1))
