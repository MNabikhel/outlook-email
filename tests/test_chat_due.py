"""What falls due in a period, asked in the chat: every email due then, not the mail that has the period's words.
The sample's today is Tuesday September 22, 2026."""

from __future__ import annotations

from datetime import date

from controller_inbox import assistant, services

TODAY = "2026-09-22"


def _ask(store, settings, question, email_id=None):
    focus = services.digest_now(store, settings, date.fromisoformat(TODAY))["focus"]
    events = list(assistant.answer_stream(store, settings, question, email_id=email_id, focus=focus, today=TODAY))
    return "".join(e.get("text", "") for e in events if e["type"] == "delta")


def test_due_next_week_lists_what_falls_due_then(loaded, settings):
    text = _ask(loaded, settings, "What is due next week?")
    # September 28 to October 4: the close calendar, the Chase statement and Harbor's invoice (Northwind's is October 5).
    for subject in ("Close calendar", "checking statement", "Harbor Packaging"):
        assert subject in text, (subject, text)
    assert "newsletter" not in text.lower() and "This week in accounting" not in text, text


def test_due_this_month_lists_every_email_due_by_its_end(loaded, settings):
    text = _ask(loaded, settings, "What is due this month?")
    for subject in ("PBC list", "Approval needed", "Q4 headcount", "Close calendar", "checking statement", "Payroll register"):
        assert subject in text, (subject, text)
    assert "CP2000" not in text, "due October 12"


def test_the_focus_answer_keeps_to_the_period_asked_about(loaded, settings):
    overdue = _ask(loaded, settings, "What's overdue?")
    assert "Nothing is overdue" in overdue, overdue
    tomorrow = _ask(loaded, settings, "What's due tomorrow?")
    assert "Nothing is due tomorrow" in tomorrow, tomorrow
    week = _ask(loaded, settings, "What is due this week?")
    assert "PBC list" in week and "Payroll register" in week and "Close calendar" not in week, week
