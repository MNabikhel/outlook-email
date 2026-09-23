from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from controller_inbox.digest import build_digest, digest_window
from controller_inbox.web import create_app

NY = ZoneInfo("America/New_York")


def test_window_is_previous_working_day():
    tuesday = date(2026, 9, 22)
    start, end = digest_window(tuesday, NY)
    assert start == datetime(2026, 9, 21, 0, 0, tzinfo=NY)
    assert end == datetime(2026, 9, 23, 0, 0, tzinfo=NY)

    monday_start, _ = digest_window(date(2026, 9, 21), NY)
    assert monday_start.date() == date(2026, 9, 18), "Monday's digest covers the weekend back to Friday"
    sunday_start, _ = digest_window(date(2026, 9, 20), NY)
    assert sunday_start.date() == date(2026, 9, 18)


def test_focus_puts_fraud_first_and_one_row_per_email(loaded, settings, as_of_now):
    payload = build_digest(loaded, as_of=date(2026, 9, 22), generated_at=as_of_now, tz=settings.tz)
    focus = payload["focus"]
    assert 1 <= len(focus) <= 7
    assert focus[0]["email_id"] == "demo-bec-wire"
    assert focus[0]["kind"] == "fraud"
    assert [row["rank"] for row in focus] == list(range(1, len(focus) + 1))
    assert len({row["email_id"] for row in focus}) == len(focus)
    assert all(row["summary"] or row["subject"] for row in focus)
    scores = [row["score"] for row in focus]
    assert scores == sorted(scores, reverse=True)
    assert payload["headline"].startswith(f"{payload['kpis']['need_you']} of 13 emails since Mon Sep 21 need you")
    assert "payment-change warning" in payload["headline"]


def test_new_mail_is_grouped_by_folder_with_summaries(loaded, settings, as_of_now):
    payload = build_digest(loaded, as_of=date(2026, 9, 22), generated_at=as_of_now, tz=settings.tz)
    new = payload["new_mail"]
    assert set(new) == {"important", "informational", "reference"}
    assert sum(len(rows) for rows in new.values()) == payload["kpis"]["emails"] == 13
    assert any(row["id"] == "demo-newsletter" for row in new["informational"])
    assert all(row["summary"] for rows in new.values() for row in rows)
    md = payload["markdown"]
    assert "## Your focus today" in md
    assert "## What came in since Mon Sep 21" in md
    assert "### Needs you" in md and "### Worth knowing" in md and "### Filed for reference" in md
    assert "Your day, Tuesday, September 22, 2026" in payload["html"]


def test_done_tasks_leave_focus_and_old_tasks_carry_over(loaded, settings, as_of_now):
    bec = loaded.get_email("demo-bec-wire")
    for action in bec.actions:
        loaded.set_action_status(action.id, "done")
    later = as_of_now + timedelta(days=3)
    payload = build_digest(loaded, as_of=date(2026, 9, 25), generated_at=later, tz=settings.tz)
    assert payload["kpis"]["emails"] == 0, "nothing new arrived after Tuesday"
    ids = [row["email_id"] for row in payload["focus"]]
    assert "demo-bec-wire" not in ids
    assert payload["focus"], "open tasks from earlier mail still show up"
    assert any(row["kind"] in {"overdue", "due_today", "due_soon", "due_week"} for row in payload["focus"])
    assert payload["critical_alerts"] == []


def test_fraud_alert_carries_over_until_verified(loaded, settings, as_of_now):
    later = as_of_now + timedelta(days=5)
    payload = build_digest(loaded, as_of=date(2026, 9, 27), generated_at=later, tz=settings.tz)
    assert [row["id"] for row in payload["critical_alerts"]] == ["demo-bec-wire"]


def test_digest_history_is_kept_and_browsable(loaded, settings, as_of_now):
    build_digest(loaded, as_of=date(2026, 9, 21), generated_at=as_of_now - timedelta(days=1), tz=settings.tz)
    build_digest(loaded, as_of=date(2026, 9, 22), generated_at=as_of_now, tz=settings.tz)
    history = loaded.list_digests()
    assert [row["period_date"] for row in history] == ["2026-09-22", "2026-09-21"]
    assert history[0]["headline"]
    assert history[0]["kpis"]["emails"] == 13

    client = TestClient(create_app(settings, loaded))
    page = client.get("/digests")
    assert page.status_code == 200
    assert "2026-09-21" in page.text and "2026-09-22" in page.text

    older = client.get("/digest?date=2026-09-21")
    assert older.status_code == 200
    assert "2026-09-22 →" in older.text

    printable = client.get("/digest/2026-09-22.html")
    assert printable.status_code == 200
    assert "Your focus today" in printable.text

    assert client.get("/digest?date=2001-01-01").status_code == 404


def test_today_page_leads_with_focus(loaded, settings):
    client = TestClient(create_app(settings, loaded))
    home = client.get("/")
    assert home.status_code == 200
    assert "Your focus today" in home.text
    assert "need you" in home.text
    assert "What came in since" in home.text
    assert "Process new mail" in home.text
    assert home.text.index("Do not process") < home.text.index("Your focus today")


def test_old_digest_payloads_still_render(store, settings):
    store.save_digest(
        "2026-09-01",
        datetime(2026, 9, 1, tzinfo=timezone.utc).isoformat(),
        "# old",
        "<p>old</p>",
        {
            "date": "2026-09-01",
            "kpis": {"emails": 3, "open_actions": 1, "overdue_actions": 0, "waiting_on_bionic": 0},
            "critical_alerts": [],
            "overdue_actions": [],
            "due_today": [],
            "due_this_week": [],
            "invoices_to_enter": [],
            "cash_to_apply": [],
            "close_items": [],
        },
    )
    client = TestClient(create_app(settings, store))
    assert client.get("/digest?date=2026-09-01").status_code == 200
    assert client.get("/digests").status_code == 200
