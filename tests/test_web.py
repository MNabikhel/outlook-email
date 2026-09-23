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
    assert "Waiting on Bionic" in important.text

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
