"""Calendar questions in the chat: which questions use the calendar, the day or period they ask about, and the
lines the model reads. The sample's today is Tuesday September 22, 2026, with a weekday standup."""

from __future__ import annotations

from datetime import date

from controller_inbox import assistant, services
from controller_inbox.calendar_events import Meeting

TODAY = "2026-09-22"  # a Tuesday


def _ask(store, settings, question, email_id=None):
    focus = services.digest_now(store, settings, date.fromisoformat(TODAY))["focus"]
    events = list(assistant.answer_stream(store, settings, question, email_id=email_id, focus=focus, today=TODAY))
    return "".join(e.get("text", "") for e in events if e["type"] == "delta")


def _model_prompt(store, settings, monkeypatch, question, email_id=None):
    asked = []

    def stream(_s, messages, **_k):
        asked.append("\n".join(str(m.get("content")) for m in messages))
        yield "Answer [1]."

    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "stream_text", stream)
    monkeypatch.setattr(assistant, "needs_more_context", lambda _s: False)
    monkeypatch.setattr(assistant, "chat_with_tools", lambda *_a, **_k: (_ for _ in ()).throw(assistant.ToolsUnsupported("no")))
    list(assistant.answer_stream(store, settings, question, email_id=email_id, focus=[], today=TODAY))
    return asked[-1]


def test_questions_that_only_sound_like_meetings_are_not_answered_with_the_calendar(loaded, settings):
    assert not assistant.asks_about_meetings("what's on my to-do list?")
    assert not assistant.asks_about_meetings("which payments are scheduled for Friday?")
    assert not assistant.asks_about_meetings("what's on this invoice?")
    text = _ask(loaded, settings, "what's on this invoice?", "demo-inv-10482")
    assert "meeting" not in text.lower() and "INV-10482" in text, text
    assert "focus list" in _ask(loaded, settings, "what's on my to-do list?").lower()
    assert "standup" not in _ask(loaded, settings, "which payments are scheduled for Friday?").lower()


def test_a_meeting_question_about_the_open_email_is_answered_from_it(loaded, settings):
    text = _ask(loaded, settings, "who organized this meeting?", "demo-meeting")
    assert "Jordan Lee" in text and "meetings in the next seven days" not in text, text
    text = _ask(loaded, settings, "what's on the agenda?", "demo-pbc")
    assert "meetings in the next seven days" not in text, text
    assert "standup" in _ask(loaded, settings, "am I free tomorrow?", "demo-pbc").lower(), "a day's meetings asked with mail open"


def test_calendar_questions_without_a_meeting_word_use_the_calendar(loaded, settings):
    for question in ("What have I got tomorrow?", "What do I have on Thursday?", "Do I have anything on Thursday?", "Am I booked on Thursday?"):
        assert assistant.asks_about_meetings(question), question
    assert "Q4 planning review" in _ask(loaded, settings, "What do I have on Thursday?")
    assert not assistant.asks_about_meetings("What do I have due on Thursday?")


def test_a_word_starting_like_a_month_is_no_month(loaded, settings):
    today = date(2026, 9, 22)
    for question in ("Do I have meetings tomorrow with the 2 marketing leads?", "Am I free tomorrow for the 3 decision makers?",
                     "Any calls with the 2 junior analysts tomorrow?"):
        assert assistant._named_day(question, today) is None, question
    assert "on Wednesday September 23" in _ask(loaded, settings, "Do I have meetings tomorrow with the 2 marketing leads?")
    assert assistant._named_day("Am I free Sept 30?", today) == date(2026, 9, 30)
    assert assistant._named_day("Am I free on 3 December?", today) == date(2026, 12, 3)


def test_a_date_gone_by_is_this_years_when_recent_or_asked_in_the_past_tense(loaded, settings):
    today = date(2026, 9, 22)
    assert assistant._named_day("What meetings did I have on Sep 21?", today) == date(2026, 9, 21)
    assert assistant._named_day("What was on my calendar on the 21st?", today) == date(2026, 9, 21)
    assert assistant._named_day("Is anything due on 9/2?", today) == date(2027, 9, 2), "a day well gone by is next year's"
    assert assistant._named_day("meetings on the 31st", today) == date(2026, 10, 31), "September has no 31st"
    assert "on Monday September 21" in _ask(loaded, settings, "What meetings did I have on Sep 21?")


def test_the_day_or_period_a_question_names(loaded, settings):
    assert "on Thursday September 24" in _ask(loaded, settings, "What meetings do I have the day after tomorrow?")
    assert "on Tuesday September 29" in _ask(loaded, settings, "am I free next Tuesday?")
    assert "on Monday September 21" in _ask(loaded, settings, "what meetings did I have yesterday?")
    assert "on Monday September 21" in _ask(loaded, settings, "what meetings did I have on Monday?")
    weekend = _ask(loaded, settings, "Any meetings this weekend?")
    assert "Nothing on your calendar this weekend" in weekend, weekend
    week = _ask(loaded, settings, "What meetings do I have this week?")
    assert "this week" in week and "Mon Sep 28" not in week and "Fri Sep 25" in week, week
    last = _ask(loaded, settings, "What meetings did I have last week?")
    assert "last week" in last and "Tue Sep 15" in last and "Tue Sep 22" not in last, last
    october = _ask(loaded, settings, "Any meetings in October?")
    assert "in October" in october and "Mon Oct 5" in october and "Wed Sep 23" not in october, october


def test_a_meeting_over_several_days_counts_on_each_of_them(loaded, settings):
    email = loaded.get_email("demo-pbc")
    loaded.save_invites("demo-pbc", [Meeting(uid="u-audit", start="2026-09-29", end="2026-10-03", subject="Audit fieldwork on site",
                                             all_day=True, email_id="demo-pbc")], sender=email.sender_email, received=email.received_at)
    assert "Audit fieldwork" in _ask(loaded, settings, "am I free on Oct 1?")
    calendar = assistant.calendar_source(loaded, settings, TODAY, "am I free on Oct 1?")
    line = next(line for line in calendar.body_text.splitlines() if "Audit fieldwork" in line)
    assert line.startswith("2026-09-29 to 2026-10-02 (all day)"), line


def test_items_shown_as_free_are_not_counted_as_meetings(loaded, settings):
    meeting = Meeting(uid="x1", start="2026-09-26T13:00:00+00:00", end="2026-09-26T13:30:00+00:00", subject="Payday reminder",
                      source="demo", busy="free")
    loaded.replace_meetings("demo", [meeting], start="2026-09-26T00:00:00+00:00", end="2026-09-26T23:59:59+00:00")
    text = _ask(loaded, settings, "Am I free on Saturday?")
    assert "You have 1 meeting" not in text and "shown as free" in text and "Payday reminder" in text, text
    calendar = assistant.calendar_source(loaded, settings, TODAY, "Am I free on Saturday?")
    assert "Payday reminder · shown as free" in calendar.body_text


def test_the_model_reads_the_day_asked_about_however_full_the_calendar(store, settings, monkeypatch):
    monkeypatch.setattr(assistant, "context_length", lambda _s: 4096)
    meetings = [Meeting(uid=f"d{day}-{hour}", start=f"2026-{month:02d}-{day:02d}T{hour}:00:00+00:00",
                        end=f"2026-{month:02d}-{day:02d}T{hour}:30:00+00:00", subject=f"Client call {month}-{day}-{hour}", source="demo",
                        location="Microsoft Teams Meeting", organizer="Maya Chen <maya@horizon-goods.com>")
                for month in (9, 10) for day in range(1, 31) for hour in (13, 15, 18, 20)]
    meetings.append(Meeting(uid="board", start="2026-12-15T15:00:00+00:00", end="2026-12-15T16:00:00+00:00",
                            subject="Board audit committee", source="demo"))
    store.replace_meetings("demo", meetings, start="2026-01-01", end="2027-12-31")
    assert "Board audit committee" in _model_prompt(store, settings, monkeypatch, "Am I free on December 15?")
    assert "Client call 10-8-20" in _model_prompt(store, settings, monkeypatch, "Am I free on October 8?")
    assert "Client call 9-25-20" in _model_prompt(store, settings, monkeypatch, "Am I free on Friday?")
    tomorrow = assistant.calendar_source(store, settings, TODAY, "Am I free tomorrow?")
    assert "Client call 9-24" not in tomorrow.body_text and "Client call 9-23-13" in tomorrow.body_text
    ahead = assistant.calendar_source(store, settings, TODAY, "What meetings do I have?")
    assert "2026-09-15" not in ahead.body_text, "from today on, not a week back"


def test_an_invitation_file_reaches_the_model_as_the_meeting_not_its_raw_text(loaded, settings, monkeypatch):
    from controller_inbox import agent

    prompt = _model_prompt(loaded, settings, monkeypatch, "what does the attachment say?", "demo-meeting")
    assert "DTSTART:" not in prompt and "BEGIN:VCALENDAR" not in prompt, prompt[-1500:]
    assert "Q4 planning review, Thursday September 24, 2:00 PM–3:00 PM" in prompt and "Times are America/New_York" in prompt
    ws = agent.Workspace(loaded, settings, [loaded.get_email("demo-meeting")], question="q")
    for name, args in (("read_file", {"email": 1}), ("find_in_file", {"email": 1, "query": "Q4 planning"}), ("open_email", {"email": 1})):
        said = agent.run_tool(ws, name, args, limit=3000)
        assert "DTSTART" not in said and "2:00 PM" in said, (name, said)
