"""Overnight runs and watch (overnight.py, cli.py): one run at a time, the digest window and sending it once."""

from __future__ import annotations

import copy
import subprocess
import sys
import textwrap
import time
from datetime import date, datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from controller_inbox import cli
from controller_inbox.cli import DIGEST_SENT_KEY, watch_tick
from controller_inbox.config import Settings
from controller_inbox.digest import build_digest, digest_window
from controller_inbox.overnight import RunBusy, run_lock, run_overnight
from controller_inbox.store import Store


def test_the_digest_counts_every_email_in_its_window_past_a_thousand(loaded: Store, settings: Settings):
    base = loaded.list_emails(limit=1)[0]
    for i in range(1005):
        email = copy.deepcopy(base)
        email.id, email.flags, email.attachments, email.actions = f"busy-{i}", [], [], []
        email.received_at = f"2030-01-08T{10 + i // 3600:02d}:{i // 60 % 60:02d}:{i % 60:02d}+00:00"
        loaded.upsert_email(email)
    morning = datetime(2030, 1, 9, 7, 0, tzinfo=timezone.utc)
    payload = build_digest(loaded, as_of=date(2030, 1, 9), generated_at=morning, tz=timezone.utc, save=False)
    assert payload["kpis"]["emails"] == 1005


NY = ZoneInfo("America/New_York")


def _eml(path: Path, *, subject: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = "Dana <dana@vendor.example>"
    message["Date"] = "Tue, 22 Sep 2026 09:00:00 -0400"
    message.set_content(body)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(message.as_bytes())


# watch emails the digest once a day, even when another run saved it first, and rebuilds after new mail.
def test_watch_sends_the_digest_once_even_if_overnight_saved_it(settings: Settings, store: Store, monkeypatch):
    settings.ensure_data_dir()
    settings.digest_to, settings.azure_client_id, settings.digest_hour = "me@taz.com", "client-id", 7
    monkeypatch.setattr(cli, "_graph_mailbox", lambda _settings: object())
    monkeypatch.setattr(cli, "ingest_mailbox", lambda *a, **k: [])
    morning = datetime(2026, 9, 22, 8, 0, tzinfo=settings.tz)
    build_digest(store, as_of=morning.date(), generated_at=morning, tz=settings.tz)  # the 06:30 overnight run

    first = watch_tick(settings, store, now=morning)
    assert first["send"] is True and first["digest"] is not None
    store.set_state(DIGEST_SENT_KEY, morning.date().isoformat())

    quiet = watch_tick(settings, store, now=morning.replace(hour=9))
    assert quiet["send"] is False and quiet["digest"] is None

    _eml(settings.inbox_incoming / "late.eml", subject="Late invoice", body="Invoice INV-77 for $90.00.")
    later = watch_tick(settings, store, now=morning.replace(hour=10))
    assert later["send"] is False
    assert later["digest"] is not None, "new mail rebuilds today's digest"
    assert "Late invoice" in store.get_digest(morning.date().isoformat())["markdown"]


def test_watch_records_the_day_it_sent(settings: Settings, store: Store, monkeypatch):
    settings.ensure_data_dir()
    settings.digest_to, settings.azure_client_id, settings.digest_hour = "me@taz.com", "client-id", 0
    monkeypatch.setattr(cli, "_graph_mailbox", lambda _settings: object())
    monkeypatch.setattr(cli, "ingest_mailbox", lambda *a, **k: [])
    sent = []
    monkeypatch.setattr(cli, "_send_digest", lambda _s, payload, period: sent.append(period))
    assert cli._watch(settings, store, once=True) == 0
    assert sent and store.get_state(DIGEST_SENT_KEY) == sent[0]


# One failing check does not stop watch.
def test_watch_keeps_running_after_a_failed_check(settings: Settings, store: Store, monkeypatch, capsys):
    calls = []

    def tick(*_a, **_k):
        calls.append(1)
        if len(calls) == 1:
            raise ConnectionError("graph is down")
        return {"records": [], "read": 0, "digest": None, "send": False, "busy": False}

    class Stop(Exception):
        pass

    def sleep(_seconds):
        if len(calls) >= 3:
            raise Stop

    monkeypatch.setattr(cli, "watch_tick", tick)
    monkeypatch.setattr(cli.time, "sleep", sleep)
    with pytest.raises(Stop):
        cli._watch(settings, store, once=False)
    assert len(calls) == 3
    assert "this check failed (ConnectionError" in capsys.readouterr().out
    calls.clear()
    assert cli._watch(settings, store, once=True) == 1


# One run at a time across processes; WAL and a busy timeout on every connection.
def test_a_second_run_is_skipped_while_one_holds_the_lock(settings: Settings, store: Store, capsys, monkeypatch):
    settings.ensure_data_dir()
    _eml(settings.inbox_incoming / "one.eml", subject="Vendor call", body="Can we talk tomorrow?")
    with run_lock(settings) as first:
        assert first
        with run_lock(settings) as second:
            assert not second
        with pytest.raises(RunBusy):
            run_overnight(store, settings, sync_graph=False)
        busy = watch_tick(settings, store)
        assert busy["busy"] and busy["records"] == []
        assert store.counts()["emails"] == 0
    assert watch_tick(settings, store)["records"]


def test_the_lock_holds_across_processes(settings: Settings):
    settings.ensure_data_dir()
    ready = settings.data_dir / "ready"
    release = settings.data_dir / "release"
    # The other process holds the lock until this test has tried it and says to let go.
    script = textwrap.dedent(
        f"""
        import time
        from pathlib import Path
        from controller_inbox.config import Settings
        from controller_inbox.overnight import run_lock
        settings = Settings(data_dir={str(settings.data_dir)!r}, inbox_dir={str(settings.inbox_dir)!r}, _env_file=None)
        with run_lock(settings) as got:
            Path({str(ready)!r}).write_text(str(got))
            deadline = time.monotonic() + 60
            while not Path({str(release)!r}).exists() and time.monotonic() < deadline:
                time.sleep(0.02)
        """
    )
    child = subprocess.Popen([sys.executable, "-c", script])
    try:
        # Starting a second Python can take several seconds on a slow machine (CI's Windows).
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if ready.exists() and ready.read_text():
                break
            time.sleep(0.05)
        assert ready.read_text() == "True"
        with run_lock(settings) as got:
            assert not got
    finally:
        release.write_text("go")
        child.wait(timeout=20)
    with run_lock(settings) as got:
        assert got, "released when the other process finished"


# Working-day lookback, literal LIKE patterns, and a complete sample clear-out.
def test_lookback_counts_working_days():
    monday = date(2026, 9, 21)
    assert digest_window(monday, NY, 1)[0].date() == date(2026, 9, 18)
    assert digest_window(monday, NY, 2)[0].date() == date(2026, 9, 17)
    assert digest_window(date(2026, 9, 23), NY, 3)[0].date() == date(2026, 9, 18)
