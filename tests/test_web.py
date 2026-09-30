from fastapi.testclient import TestClient

from controller_inbox.digest import build_digest
from controller_inbox.pipeline import ingest_demo
from controller_inbox.web import create_app


def test_dashboard_after_demo(store, settings, as_of_now):
    ingest_demo(store, settings, now=as_of_now)
    build_digest(store, as_of=as_of_now.date(), generated_at=as_of_now)
    app = create_app(settings, store)
    client = TestClient(app)

    home = client.get("/")
    assert home.status_code == 200
    assert "CloseDesk" in home.text
    assert "Controller inbox" not in home.text
    assert "Assistant controller" not in home.text
    assert "INV-10482" in home.text
    assert "Updated wiring instructions" in home.text
    assert "Fraud flags" in home.text

    detail = client.get("/inbox/demo-bec-wire")
    assert detail.status_code == 200
    assert "verify" in detail.text.lower()
    assert "Wrong category?" in detail.text
    assert "do not process" in detail.text.lower() or "fraud" in detail.text.lower()

    corrected = client.post(
        "/inbox/demo-newsletter/correct",
        data={"category": "internal_fyi", "reason": "This weekly note is internal, not a vendor newsletter."},
        follow_redirects=True,
    )
    assert corrected.status_code == 200
    assert "correction you saved" in corrected.text.lower() or "user trained" in corrected.text.lower() or "Internal FYI" in corrected.text

    actions = client.get("/actions")
    assert actions.status_code == 200
    assert "Reconcile bank statement" in actions.text or "invoice" in actions.text.lower()

    attachments = client.get("/attachments")
    assert attachments.status_code == 200
    assert "INV-10482.pdf" in attachments.text

    digest = client.get("/digest")
    assert digest.status_code == 200
    assert "Do not process" in digest.text

    important = client.get("/folder/important")
    assert important.status_code == 200
    assert "INV-10482" in important.text
    assert "Script draft" in important.text

    informational = client.get("/folder/informational")
    assert informational.status_code == 200
    assert "accounting" in informational.text.lower() or "newsletter" in informational.text.lower()

    reference = client.get("/folder/reference")
    assert reference.status_code == 200
    assert "statement" in reference.text.lower() or "PO-77821" in reference.text

    missing = client.get("/folder/archive")
    assert missing.status_code == 404

    csv_resp = client.get("/export/actions.csv")
    assert csv_resp.status_code == 200
    assert "priority" in csv_resp.text
    assert "demo-bec-wire" in csv_resp.text

    actions_page = client.get("/actions")
    assert "Verify payment-instruction change" in actions_page.text
    bec = store.get_email("demo-bec-wire")
    assert bec is not None
    action_id = bec.actions[0].id
    done = client.post(f"/actions/{action_id}/complete?next=/actions", follow_redirects=True)
    assert done.status_code == 200
    remaining = {item.id for item, _ in store.list_actions(status="open")}
    assert action_id not in remaining

    health = client.get("/health")
    assert health.json()["ok"] is True
    assert health.json()["emails"] == 19


def test_empty_state_and_reload(store, settings):
    app = create_app(settings, store)
    client = TestClient(app)
    empty = client.get("/")
    assert empty.status_code == 200
    assert "Load sample mailbox" in empty.text
    reloaded = client.post("/demo/reload", follow_redirects=True)
    assert reloaded.status_code == 200
    assert "INV-10482" in reloaded.text


def test_folder_done_and_preview_correction(store, settings, as_of_now):
    ingest_demo(store, settings, now=as_of_now)
    client = TestClient(create_app(settings, store))
    email = store.list_emails(folder="important", limit=1)[0]
    before = store.counts()["important"]

    page = client.get("/folder/important")
    assert f"/inbox/{email.id}/done?next=" in page.text

    done = client.post(f"/inbox/{email.id}/done", params={"next": "/folder/important"}, follow_redirects=False)
    assert done.status_code == 303 and done.headers["location"] == "/folder/important"
    assert store.is_done(email.id)
    assert f'data-email="{email.id}"' not in client.get("/folder/important").text
    assert "Show 1 done" in client.get("/folder/important").text
    assert store.counts()["important"] == before - 1

    shown = client.get("/folder/important?done=1").text
    assert f'data-email="{email.id}"' in shown and "Undo" in shown
    assert "Undo done" in client.get(f"/inbox/{email.id}/preview").text

    client.post(f"/inbox/{email.id}/done", params={"undo": 1, "next": "/folder/important?done=1"})
    assert not store.is_done(email.id)
    assert f'data-email="{email.id}"' in client.get("/folder/important").text
    assert client.post("/inbox/nope/done").status_code == 404

    preview = client.get("/inbox/demo-newsletter/preview", params={"next": "/folder/informational"}).text
    assert "Wrong category?" in preview and 'name="next" value="/folder/informational"' in preview
    fixed = client.post(
        "/inbox/demo-newsletter/correct",
        data={"category": "internal_fyi", "reason": "Internal weekly note.", "next": "/folder/informational"},
        follow_redirects=False,
    )
    assert fixed.headers["location"] == "/folder/informational?notice=corrected"
    assert "Category corrected" in client.get(fixed.headers["location"]).text
