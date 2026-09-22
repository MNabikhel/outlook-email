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
    assert "INV-10482" in home.text
    assert "Updated wiring instructions" in home.text
    assert "Fraud flags" in home.text

    detail = client.get("/inbox/demo-bec-wire")
    assert detail.status_code == 200
    assert "verify" in detail.text.lower()
    assert "do not process" in detail.text.lower() or "fraud" in detail.text.lower()

    actions = client.get("/actions")
    assert actions.status_code == 200
    assert "Reconcile bank statement" in actions.text or "invoice" in actions.text.lower()

    attachments = client.get("/attachments")
    assert attachments.status_code == 200
    assert "INV-10482.pdf" in attachments.text

    digest = client.get("/digest")
    assert digest.status_code == 200
    assert "Do not process" in digest.text

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
    assert health.json()["emails"] == 14


def test_bins_board_and_filter(store, settings, as_of_now):
    ingest_demo(store, settings, now=as_of_now)
    app = create_app(settings, store)
    client = TestClient(app)

    bins = client.get("/bins")
    assert bins.status_code == 200
    assert "Do not process" in bins.text
    assert "Action required" in bins.text
    assert "Updated wiring instructions" in bins.text

    filtered = client.get("/inbox?bin=fraud_review")
    assert filtered.status_code == 200
    assert "Updated wiring instructions" in filtered.text
    # An action-required-only email should not show in the fraud bin.
    assert "This week in accounting" not in filtered.text


def test_digest_history(store, settings, as_of_now):
    ingest_demo(store, settings, now=as_of_now)
    build_digest(store, as_of=as_of_now.date(), generated_at=as_of_now)
    app = create_app(settings, store)
    client = TestClient(app)

    history = client.get("/digests")
    assert history.status_code == 200
    assert "2026-09-22" in history.text

    dated = client.get("/digest?date=2026-09-22")
    assert dated.status_code == 200
    assert "Do not process" in dated.text
    # Triage bin counts appear in the digest body.
    assert "Triage bins" in dated.text


def test_store_lists_digests(store, settings, as_of_now):
    ingest_demo(store, settings, now=as_of_now)
    build_digest(store, as_of=as_of_now.date(), generated_at=as_of_now)
    rows = store.list_digests()
    assert rows
    assert rows[0]["period_date"] == "2026-09-22"
    assert rows[0]["kpis"]["emails"] == 14


def test_empty_state_and_reload(store, settings):
    app = create_app(settings, store)
    client = TestClient(app)
    empty = client.get("/")
    assert empty.status_code == 200
    assert "Load demo mailbox" in empty.text
    reloaded = client.post("/demo/reload", follow_redirects=True)
    assert reloaded.status_code == 200
    assert "INV-10482" in reloaded.text
