from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from controller_inbox.classify import classify_email
from controller_inbox.cli import load_sample, make_digest
from controller_inbox.config import Settings
from controller_inbox.digest import build_digest
from controller_inbox.extract import extract_fields
from controller_inbox.models import DocumentType
from controller_inbox.profile import active_profile, is_finance, set_profile
from controller_inbox.reading import lead_sentence
from controller_inbox.store import Store
from controller_inbox.web import create_app

AS_OF = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def _classify(subject: str, body: str, sender: str = "Maya Chen <maya.chen@horizongoods.example>") -> DocumentType:
    return classify_email(
        subject=subject,
        body=body,
        sender=sender,
        outlook_importance="normal",
        attachments=[],
        fields=extract_fields(f"{subject}\n{body}", as_of=AS_OF.date()),
        as_of=AS_OF.date(),
        finance=False,
    ).document_type


@pytest.mark.parametrize(
    ("subject", "body", "expected"),
    [
        ("Q4 headcount", "Hi,\nCould you send me the updated headcount numbers before Thursday?\nThanks", DocumentType.REPLY_NEEDED),
        ("Approval needed: offer letter", "Please approve the offer letter by Sep 24.", DocumentType.APPROVAL_REQUEST),
        ("Invitation: Q4 planning review @ Thu", "Jordan Lee has invited you to Q4 planning review.", DocumentType.MEETING),
        ("Parking garage closed Friday", "Heads up: the garage is closed Friday. No action needed.", DocumentType.INTERNAL_FYI),
    ],
)
def test_everyday_mail_gets_everyday_categories(subject, body, expected):
    assert _classify(subject, body) == expected


def test_automated_notice_is_a_notification_not_a_reply():
    category = _classify(
        "Your password expires in 5 days",
        "Your password will expire in 5 days. Could you reset it? This is an automated message.",
        sender="IT Service Desk <no-reply@it.example>",
    )
    assert category == DocumentType.NOTIFICATION


def test_lead_sentence_skips_greetings_filler_and_quoted_thread():
    body = (
        "Hi Sam,\n\nHope you're well.\nThe Q3 forecast is 4% under plan. Details below.\n\n"
        "-----Original Message-----\nFrom: someone\nOld text that should not appear."
    )
    assert lead_sentence(body) == "The Q3 forecast is 4% under plan."
    assert lead_sentence("Team — kicking off Q3 interim testing. More soon.") == "Kicking off Q3 interim testing."
    assert lead_sentence("") == ""


def test_sample_everyday_mail_is_filed_without_a_model(loaded: Store):
    rows = {e.id: e for e in loaded.list_emails(limit=100)}
    assert rows["demo-question"].folder == "important"
    assert rows["demo-question"].summary.startswith("Reply needed: Could you send me")
    assert rows["demo-approval"].folder == "important"
    assert any(a.title.startswith("Approve or decline") for a in rows["demo-approval"].actions)
    assert rows["demo-meeting"].category == DocumentType.MEETING
    assert rows["demo-notification"].folder == "reference"
    assert rows["demo-fyi"].folder == "informational"


def test_profile_defaults_to_general_and_setup_choice_wins(settings: Settings, store: Store):
    assert active_profile(settings, store) == "general"
    assert not is_finance(settings, store)
    set_profile(store, "finance")
    assert is_finance(settings, store)
    with pytest.raises(ValueError):
        set_profile(store, "astronaut")
    env_finance = Settings(data_dir=settings.data_dir, profile="FINANCE", _env_file=None)
    assert env_finance.profile == "finance"
    assert Settings(data_dir=settings.data_dir, profile="nonsense", _env_file=None).profile == "general"


def test_general_digest_has_no_month_end_but_keeps_invoices(loaded: Store):
    general = build_digest(loaded, as_of=AS_OF.date(), generated_at=AS_OF, save=False)
    assert general["finance"] is False
    assert general["close_items"] == []
    assert general["invoices_to_enter"]
    general = build_digest(loaded, as_of=AS_OF.date(), generated_at=AS_OF)
    assert "Month-end" not in general["markdown"]
    assert "month-end" not in general["html"].lower()
    assert "Invoices & payments" in general["markdown"]

    finance = build_digest(loaded, as_of=AS_OF.date(), generated_at=AS_OF, finance=True)
    assert finance["close_items"]
    assert "## Month-end" in finance["markdown"]
    assert "month-end" in finance["html"].lower()


def test_loading_the_sample_keeps_the_chosen_profile(settings: Settings, store: Store):
    set_profile(store, "finance")
    load_sample(store, settings)
    assert is_finance(settings, store)
    payload = make_digest(store, settings, as_of=AS_OF.date(), now=AS_OF)
    assert payload["finance"] is True


def test_setup_page_switches_profile_and_month_end_chip(settings: Settings, loaded: Store):
    client = TestClient(create_app(settings, loaded))
    page = client.get("/").text
    assert "Days to month-end" not in page
    assert "emails sorted" in page

    response = client.post("/settings/profile", data={"profile": "finance"}, follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/settings?notice=profile").text.count('value="finance" checked') == 1
    assert "Days to month-end" in client.get("/").text
    assert client.post("/settings/profile", data={"profile": "pirate"}).status_code == 400
