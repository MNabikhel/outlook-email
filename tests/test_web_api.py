"""The workspace at /app and the JSON it is built from: what each endpoint returns, that only CloseDesk's own
page can call them, that changes go through the same helpers as the classic forms, and that the files of an
email held as possible payment fraud are never shown."""

from __future__ import annotations

import io
import time

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

from controller_inbox import web
from controller_inbox.folder_mail import ingest_folder
from controller_inbox.pipeline import ingest_demo
from msgfactory import XLSX, write_msg

PAGE = {"X-CloseDesk": "1"}


@pytest.fixture
def demo(store, settings, as_of_now):
    ingest_demo(store, settings, now=as_of_now)
    return store


@pytest.fixture
def client(demo, settings):
    return TestClient(web.create_app(settings, demo))


def api(client: TestClient, path: str, **params):
    response = client.get(path, headers=PAGE, params=params)
    assert response.status_code == 200, (path, response.status_code, response.text[:300])
    return response.json()


def post(client: TestClient, path: str, body: dict | None = None):
    return client.post(path, headers=PAGE, json=body or {})


# Who may call it ----------------------------------------------------------------------------------------


def test_every_api_call_needs_the_page_header(client, demo):
    assert client.get("/api/state").status_code == 403
    assert client.get("/api/mail/demo-payroll").status_code == 403
    assert client.post("/api/mail/demo-payroll/done", json={"done": True}).status_code == 403
    assert not demo.is_done("demo-payroll")
    assert client.get("/api/state", headers=PAGE).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [{"Origin": "https://evil.example"}, {"Origin": "null"}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"}],
)
def test_other_websites_cannot_change_anything_or_read_mail(client, demo, headers):
    task = demo.get_email("demo-payroll").actions[0]
    attempts = [
        ("/api/mail/demo-payroll/done", {"done": True}),
        (f"/api/tasks/{task.id}", {"status": "done"}),
        ("/api/mail/demo-bec-wire/fraud", {"choice": "safe:email"}),
        ("/api/mail/demo-newsletter/category", {"category": "internal_fyi", "reason": "It is internal."}),
        ("/api/process", {}),
    ]
    for path, body in attempts:
        assert client.post(path, json=body, headers={**PAGE, **headers}).status_code == 403, (path, headers)
    assert not demo.is_done("demo-payroll")
    assert demo.get_email("demo-payroll").actions[0].status.value == "open"
    assert "fraud_cleared" not in demo.get_email("demo-bec-wire").flags
    assert client.get("/api/mail/demo-payroll", headers={**PAGE, **headers}).status_code == 403


def test_the_page_itself_can_post(client, demo):
    response = client.post("/api/mail/demo-payroll/done", json={"done": True}, headers={**PAGE, "Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"})
    assert response.status_code == 200 and demo.is_done("demo-payroll")


# What it returns ----------------------------------------------------------------------------------------


def test_state_has_the_counts_the_side_bar_shows_and_changes_its_version(client, demo):
    state = api(client, "/api/state")
    assert state["counts"]["emails"] == demo.counts()["emails"]
    assert state["is_sample"] is True and state["today"] == "2026-09-22"
    assert state["model"]["active"] is False and state["job"]["state"] == "idle"
    assert {"value": "ap_invoice", "label": "AP invoice"} in state["categories"]
    assert post(client, "/api/mail/demo-payroll/done", {"done": True}).status_code == 200
    after = api(client, "/api/state")
    assert after["version"] != state["version"]
    assert after["counts"]["important"] == state["counts"]["important"] - 1


def test_focus_is_the_digest_focus_list_and_done_takes_rows_off_it(client, demo):
    focus = api(client, "/api/focus")
    rows = focus["focus"]
    assert rows and rows[0]["kind"] == "fraud" and rows[0]["email_id"] == "demo-bec-wire" and rows[0]["locked"] is True
    assert "need you" in focus["headline"] and focus["alerts"][0]["id"] == "demo-bec-wire"
    task_row = next(row for row in rows if row["action_id"] and row["kind"] != "fraud")
    done = post(client, f"/api/tasks/{task_row['action_id']}", {"status": "done"})
    assert done.status_code == 200 and done.json()["counts"]["open_actions"] == demo.counts()["open_actions"]
    still = [row for row in api(client, "/api/focus")["focus"] if row.get("action_id") == task_row["action_id"]]
    assert not still
    assert post(client, f"/api/tasks/{task_row['action_id']}", {"status": "open"}).status_code == 200
    assert post(client, f"/api/tasks/{task_row['action_id']}", {"status": "maybe"}).status_code == 400
    assert post(client, "/api/tasks/no-such-task", {"status": "done"}).status_code == 404


def test_a_needs_a_look_row_leaves_the_focus_list_when_its_email_is_done(client, demo):
    email = demo.get_email("demo-inv-10482")
    email.actions = []  # an important email with no task: "New · needs a look"
    demo.upsert_email(email)
    for other in demo.list_emails(limit=100):  # and nothing else ahead of it
        for action in other.actions:
            demo.set_action_status(action.id, "done")
    rows = api(client, "/api/focus")["focus"]
    plain = next(row for row in rows if row["email_id"] == "demo-inv-10482")
    assert plain["kind"] == "decide" and not plain["action_id"]
    assert post(client, f"/api/mail/{plain['email_id']}/done", {"done": True}).status_code == 200
    assert plain["email_id"] not in [row["email_id"] for row in api(client, "/api/focus")["focus"]]


def test_folder_lists_search_and_done(client, demo):
    important = api(client, "/api/mail", folder="important")
    ids = [item["id"] for item in important["items"]]
    assert "demo-inv-10482" in ids and important["title"] == "Important"
    card = next(item for item in important["items"] if item["id"] == "demo-inv-10482")
    assert card["amount_label"] == "$12,850.00" and card["invoice"] == "INV-10482" and card["files"] == 1
    assert card["when"] and card["category_label"] == "AP invoice" and card["done"] is False
    assert post(client, "/api/mail/demo-inv-10482/done", {"done": True}).status_code == 200
    assert "demo-inv-10482" not in [item["id"] for item in api(client, "/api/mail", folder="important")["items"]]
    done = api(client, "/api/mail", folder="important", done=1)
    assert [item["id"] for item in done["items"]] == ["demo-inv-10482"] and done["done_count"] == 1
    assert post(client, "/api/mail/demo-inv-10482/done", {"done": False}).status_code == 200
    assert not demo.is_done("demo-inv-10482")
    found = api(client, "/api/mail", q="headcount")
    assert [item["subject"] for item in found["items"]] == ["Quick question on the Q4 headcount numbers"]
    assert client.get("/api/mail", params={"folder": "archive"}, headers=PAGE).status_code == 404
    assert post(client, "/api/mail/no-such-email/done", {"done": True}).status_code == 404


def test_an_email_has_its_reasons_fields_tasks_files_and_checks(client):
    mail = api(client, "/api/mail/demo-inv-10482")
    assert mail["subject"].startswith("Invoice INV-10482") and mail["reasons"]
    assert "INV-10482" in mail["fields"]["invoices"] and "$12,850.00" in mail["fields"]["amounts"]
    assert mail["tasks"] and {"id", "title", "due", "status", "priority_label"} <= set(mail["tasks"][0])
    [pdf] = mail["attachments"]
    assert pdf["name"] == "INV-10482.pdf" and pdf["has_text"] and "clip" in pdf and pdf["tables"] == 0
    assert mail["fraud"]["level"] in {"none", "caution", "high"} and "signals" in mail["fraud"]
    assert mail["locked"] is False and mail["done"] is False
    payroll = api(client, "/api/mail/demo-payroll")
    assert payroll["attachments"][0]["tables"] == 1
    assert client.get("/api/mail/no-such-email", headers=PAGE).status_code == 404


def test_tasks_fraud_coding_and_digest_lists(client, demo, settings, as_of_now):
    tasks = api(client, "/api/tasks")
    assert tasks["status"] == "open" and len(tasks["items"]) == demo.counts()["open_actions"]
    assert {"email_id", "subject", "due", "overdue", "priority"} <= set(tasks["items"][0])
    assert api(client, "/api/tasks", status="done")["items"] == []
    flagged = api(client, "/api/fraud")
    assert flagged["flagged"][0]["id"] == "demo-bec-wire" and flagged["flagged"][0]["level"] == "high"
    assert flagged["high_at"] > flagged["caution_at"]
    coding = api(client, "/api/coding", status="all")
    assert coding["counts"]["all"] == len(coding["items"]) and coding["items"]
    assert api(client, "/api/digest")["payload"] is None
    from controller_inbox.digest import build_digest

    build_digest(demo, as_of=as_of_now.date(), generated_at=as_of_now)
    digest = api(client, "/api/digest")
    assert digest["date"] == "2026-09-22" and digest["payload"]["focus"] and digest["printable"] == "/digest/2026-09-22.html"
    assert client.get("/api/digest", params={"date": "2020-01-01"}, headers=PAGE).status_code == 404


# Changes ------------------------------------------------------------------------------------------------


def test_correcting_the_category_and_the_fraud_verdict_use_the_same_learning(client, demo):
    bad = post(client, "/api/mail/demo-newsletter/category", {"category": "internal_fyi", "reason": "x"})
    assert bad.status_code == 400
    good = post(client, "/api/mail/demo-newsletter/category", {"category": "internal_fyi", "reason": "This weekly note is internal."})
    assert good.status_code == 200 and "corrected" in good.json()["message"].lower()
    assert demo.get_email("demo-newsletter").category.value == "internal_fyi"
    assert demo.correction_count() == 1
    assert post(client, "/api/mail/demo-bec-wire/fraud", {"choice": "maybe:email"}).status_code == 400
    reported = post(client, "/api/mail/demo-bec-wire/fraud", {"choice": "fraud:email", "note": "Called the CFO; not from us."})
    assert reported.status_code == 200 and "fraud" in reported.json()["message"].lower()
    assert "fraud_confirmed" in demo.get_email("demo-bec-wire").flags
    assert demo.fraud_log(limit=5)[0]["note"].endswith("Called the CFO; not from us.")


def test_coding_confirm_and_revise(client, demo, settings):
    from controller_inbox import cost_codes

    folder = settings.cost_codes_folder
    folder.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    book.active.append(["Description", "Cost Code"])
    book.active.append(["Office supplies", "1100.6110.100"])
    book.active.append(["Raw materials", "1100.5000.200"])
    book.save(folder / "codes.xlsx")
    cost_codes.refresh(demo, settings, force=True)
    rows = api(client, "/api/coding", status="all")["items"]
    assert rows
    email_id = rows[0]["id"]
    detail = api(client, f"/api/mail/{email_id}")
    assert detail["coding"]["codebook_size"] == 2 and len(detail["coding"]["choices"]) == 2
    revised = post(client, f"/api/mail/{email_id}/coding", {"codes": ["1100.5000.200"]})
    assert revised.status_code == 200 and "confirmed" in revised.json()["message"]
    coding = demo.cost_coding(email_id)
    assert coding["status"] == "confirmed" and [item["code"] for item in coding["codes"]] == ["1100.5000.200"]
    assert post(client, f"/api/mail/{email_id}/coding", {"codes": ["9999.0000.000"]}).status_code == 400


def test_process_new_mail_runs_in_the_background(client, settings):
    started = post(client, "/api/process")
    assert started.status_code == 200 and started.json()["started"] is True
    for _ in range(100):
        state = api(client, "/api/state")["job"]
        if state["state"] != "running":
            break
        time.sleep(0.1)
    assert state["state"] == "done", state


# Attachments: tables, and the fraud lock ----------------------------------------------------------------


def _aging_xlsx() -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "AP aging"
    sheet.append(["Vendor", "Current", "31 - 60 Days", "Total"])
    sheet.append(["Harbor Steel LLC", 48500, 22150, 70650])
    sheet.append(["Acme Supply", 1200, 0, 1200])
    # 22,150 + 0 is 22,150: the 31-60 total below is misprinted (or misread).
    sheet.append(["Total", 49700, 21150, 71850])
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


@pytest.fixture
def files_client(store, settings, mail):
    write_msg(
        settings.inbox_incoming / "aging.msg",
        "September AP aging",
        "The aging is attached.",
        sender_name="Maya Chen",
        sender_email="maya@taz.com",
        attachments=[("AP aging.xlsx", _aging_xlsx(), XLSX)],
    )
    for email in ingest_folder(store, settings):
        mail[email.subject] = email
    return TestClient(web.create_app(settings, store))


def test_tables_show_each_grid_and_whether_its_totals_add_up(files_client, mail):
    aging = mail["September AP aging"]
    data = api(files_client, f"/api/mail/{aging.id}/files/1")
    assert data["file"]["name"] == "AP aging.xlsx" and data["file"]["download"] is True and data["file"]["view"] is False
    [table] = data["tables"]
    assert table["labels"] == ["Vendor", "Current", "31 - 60 Days", "Total"]
    assert table["kinds"] == ["text", "figure", "figure", "figure"]
    assert [row["cells"][0] for row in table["rows"]] == ["Harbor Steel LLC", "Acme Supply", "Total"]
    assert [row["total"] for row in table["rows"]] == [False, False, True]
    assert table["rows"][0]["refs"] == ["A2", "B2", "C2", "D2"]
    assert table["check"]["matched"] == 2
    assert table["check"]["mismatched"] == ["Total (31 - 60 Days): printed 21,150, the rows above add to 22,150"]
    # The cell the message is about is marked; the others are not.
    flags = table["rows"][2]["flags"]
    assert flags[2].startswith("Total (31 - 60 Days)") and not any(flags[:2] + flags[3:])
    assert any("Harbor Steel LLC" in part["text"] for part in data["parts"])
    detail = api(files_client, f"/api/mail/{aging.id}")
    assert detail["attachments"][0]["tables"] == 1
    assert files_client.get(f"/api/mail/{aging.id}/files/2", headers=PAGE).status_code == 404


def test_a_table_whose_totals_check_out(client):
    [table] = api(client, "/api/mail/demo-payroll/files/1")["tables"]
    assert table["labels"] == ["Employee", "Gross pay", "Net pay", "Employer taxes"]
    assert table["check"] == {"matched": 3, "mismatched": [], "checked": 3}
    assert table["rows"][-1]["total"] is True and table["rows"][-1]["cells"][1] == "9,300"
    assert not any(any(row["flags"]) for row in table["rows"])


def test_files_of_a_suspected_fraud_email_are_never_shown(files_client, store, mail):
    scam = mail["Updated remittance details"]
    detail = api(files_client, f"/api/mail/{scam.id}")
    assert detail["locked"] is True
    [locked] = detail["attachments"]
    assert locked["name"] == "new bank letter.pdf"
    for key in ("clip", "tables", "download", "view"):
        assert key not in locked, key
    assert "Remit to account" not in str(detail["attachments"])
    refused = files_client.get(f"/api/mail/{scam.id}/files/1", headers=PAGE)
    assert refused.status_code == 403 and "payment fraud" in refused.json()["detail"]
    assert files_client.get(f"/inbox/{scam.id}/files/1/download").status_code == 403
    # Once someone verified it by phone and marked it safe, the file opens.
    assert post(files_client, f"/api/mail/{scam.id}/fraud", {"choice": "safe:email", "note": "Called them."}).status_code == 200
    opened = api(files_client, f"/api/mail/{scam.id}/files/1")
    assert any("Remit to account" in part["text"] for part in opened["parts"])
    assert "clip" in api(files_client, f"/api/mail/{scam.id}")["attachments"][0]
