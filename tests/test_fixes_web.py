"""Regression tests for the dashboard, store, CLI and launcher fixes (one block per bug)."""

from __future__ import annotations

import copy
import importlib.util
import subprocess
import sys
import textwrap
import time
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from controller_inbox import cli
from controller_inbox.cli import DIGEST_SENT_KEY, load_sample, watch_tick
from controller_inbox.config import Settings
from controller_inbox.digest import build_digest, digest_window
from controller_inbox.overnight import RunBusy, run_lock, run_overnight
from controller_inbox.store import Store
from controller_inbox.web import allowed_hosts, create_app

ROOT = Path(__file__).resolve().parent.parent
NY = ZoneInfo("America/New_York")
SAME = {"origin": "http://testserver"}


def _eml(path: Path, *, subject: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = "Dana <dana@vendor.example>"
    message["Date"] = "Tue, 22 Sep 2026 09:00:00 -0400"
    message.set_content(body)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(message.as_bytes())


# 1. Reloading the sample keeps Setup choices, the trust list and conversations; refused while processing.


def test_sample_reload_keeps_setup_trust_and_chats(settings: Settings, store: Store):
    settings.ensure_data_dir()
    load_sample(store, settings)
    client = TestClient(create_app(settings, store), follow_redirects=False)
    assert client.post("/settings/timezone", data={"zone": "Asia/Tokyo"}, headers=SAME).status_code == 303
    assert client.post("/settings/context", data={"step": "0"}, headers=SAME).status_code == 303
    assert client.post("/settings/profile", data={"profile": "finance"}, headers=SAME).status_code == 303
    store.set_trust("domain", "taz.com", "safe", source="you")
    store.create_chat("c1")
    store.add_chat_turn("c1", "user", "What is due?")
    store.set_state("last_overnight_at", "2026-09-22T10:00:00+00:00")

    assert client.post("/demo/reload", headers=SAME).status_code == 303
    assert store.get_state("timezone") == "Asia/Tokyo"
    assert store.get_state("min_context_tokens") == "0"
    assert store.get_state("profile") == "finance"
    assert [row["value"] for row in store.trust_entries()] == ["taz.com"]
    assert [chat["id"] for chat in store.list_chats()] == ["c1"]
    assert store.get_state("last_overnight_at") is None, "mail bookkeeping starts over"
    assert store.counts()["emails"] == 19


def test_sample_reload_is_refused_while_mail_is_processing(settings: Settings, store: Store):
    settings.ensure_data_dir()
    app = create_app(settings, store)
    client = TestClient(app, follow_redirects=False)
    app.state.job.state = "running"
    assert client.post("/demo/reload", headers=SAME).headers["location"] == "/settings?notice=sample-busy"
    app.state.job.state = "idle"
    with run_lock(settings):  # another process's run holds the lock
        assert client.post("/demo/reload", headers=SAME).headers["location"] == "/settings?notice=sample-busy"
    assert store.counts()["emails"] == 0
    assert client.post("/demo/reload", headers=SAME).headers["location"] == "/"


# 2. The flag filter is applied before the row limit.


def test_flag_filter_finds_old_mail_past_the_limit(loaded: Store):
    fraud = [e for e in loaded.list_emails(limit=500) if "fraud_risk" in e.flags]
    assert fraud
    base = loaded.list_emails(limit=1)[0]
    for i in range(210):
        email = copy.deepcopy(base)
        email.id, email.flags, email.attachments, email.actions = f"new-{i}", [], [], []
        email.received_at = f"2030-01-01T00:{i // 60:02d}:{i % 60:02d}+00:00"
        loaded.upsert_email(email)
    assert {e.id for e in loaded.list_emails(flag="fraud_risk")} == {e.id for e in fraud}
    assert loaded.list_emails(flag="fraud") == [], "a whole flag, not part of one"


# 3. watch emails the digest once a day, even when another run saved it first, and rebuilds after new mail.


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


# 4. One failing check does not stop watch.


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


# 5. The README says where conversations are kept.


def test_readme_does_not_claim_chats_vanish_with_the_tab():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "cleared when the tab closes" not in text
    assert "Past conversations" in text


# 6. Binding beyond loopback never allows every hostname, and warns there is no login.


def test_lan_bind_refuses_rebinding_and_foreign_origins(settings: Settings, loaded: Store):
    settings.host = "0.0.0.0"
    assert "*" not in allowed_hosts("0.0.0.0", "*")
    app = create_app(settings, loaded)
    rebound = TestClient(app, base_url="http://attacker.example:8765")
    assert rebound.get("/chats").status_code == 400
    assert rebound.post("/settings/profile", data={"profile": "general"}, headers={"origin": "http://attacker.example:8765"}).status_code == 400
    local = TestClient(app, follow_redirects=False)
    assert local.post("/settings/profile", data={"profile": "general"}, headers={"origin": "http://attacker.example"}).status_code == 403
    assert local.post("/settings/profile", data={"profile": "general"}, headers=SAME).status_code == 303


def test_extra_allowed_hosts_and_startup_warning(settings: Settings, store: Store, monkeypatch, capsys):
    settings.allowed_hosts = "closedesk.corp"
    settings.host = "0.0.0.0"
    client = TestClient(create_app(settings, store), base_url="http://closedesk.corp:8765")
    assert client.get("/health").status_code == 200
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    cli._serve(settings, store, host="0.0.0.0", port=8765)
    out = capsys.readouterr().out
    assert "WARNING" in out and "no login" in out and "closedesk.corp" in out
    cli._serve(settings, store, host="127.0.0.1", port=8765)
    assert "WARNING" not in capsys.readouterr().out


# 7. The launcher's dependency check notices an outdated package, not only a missing one.


def _check_deps():
    spec = importlib.util.spec_from_file_location("check_deps", ROOT / "scripts" / "check_deps.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_check_deps_reports_missing_and_outdated_packages():
    deps = _check_deps()
    assert deps.problem("fastapi>=0.1") == ""
    assert deps.problem("fastapi>=999").startswith("Outdated: fastapi")
    assert deps.problem("surely-not-installed-pkg>=1").startswith("Missing")
    assert deps.problem("surely-not-installed-pkg>=1; sys_platform == 'plan9'") == ""
    assert deps.main() == 0


# 8. One run at a time across processes; WAL and a busy timeout on every connection.


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
    script = textwrap.dedent(
        f"""
        import time
        from pathlib import Path
        from controller_inbox.config import Settings
        from controller_inbox.overnight import run_lock
        settings = Settings(data_dir={str(settings.data_dir)!r}, inbox_dir={str(settings.inbox_dir)!r}, _env_file=None)
        with run_lock(settings) as got:
            Path({str(ready)!r}).write_text(str(got))
            time.sleep(3)
        """
    )
    child = subprocess.Popen([sys.executable, "-c", script])
    try:
        for _ in range(200):
            if ready.exists() and ready.read_text():
                break
            time.sleep(0.05)
        assert ready.read_text() == "True"
        with run_lock(settings) as got:
            assert not got
    finally:
        child.wait(timeout=20)
    with run_lock(settings) as got:
        assert got, "released when the other process finished"


def test_store_uses_wal_and_a_busy_timeout(store: Store):
    with store.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 10_000


# 9. Working-day lookback, literal LIKE patterns, and a complete sample clear-out.


def test_lookback_counts_working_days():
    monday = date(2026, 9, 21)
    assert digest_window(monday, NY, 1)[0].date() == date(2026, 9, 18)
    assert digest_window(monday, NY, 2)[0].date() == date(2026, 9, 17)
    assert digest_window(date(2026, 9, 23), NY, 3)[0].date() == date(2026, 9, 18)


def test_like_patterns_treat_wildcards_as_text(loaded: Store):
    a, b = loaded.list_emails(limit=2)
    a.extracted.invoice_numbers = ["INV-0001"]
    b.extracted.invoice_numbers = ["INV_0001"]
    loaded.upsert_email(a)
    loaded.upsert_email(b)
    assert a.id not in loaded.find_duplicate_invoices("INV_0001", exclude_email_id=b.id)
    loaded.create_chat("c1")
    loaded.add_chat_turn("c1", "user", "plain question")
    assert loaded.list_chats("%") == []
    assert loaded.list_chats("_") == []
    assert [c["id"] for c in loaded.list_chats("plain")] == ["c1"]


def test_clear_sample_removes_every_trace_of_sample_mail(loaded: Store):
    email = loaded.get_email("demo-inv-10482")
    att = email.attachments[0]
    loaded.save_embeddings("m", [("k1", email.id, "t", b"\x00" * 8), ("chat-k", "chat-c1", "t", b"\x00" * 8)])
    loaded.save_file_summary(att.id, "key", "summary")
    loaded.log_fraud({"event": "flagged", "email_id": email.id})
    assert loaded.clear_sample() == 19
    with loaded.connect() as conn:
        assert conn.execute("SELECT email_id FROM embeddings").fetchall()[0][0] == "chat-c1"
        assert conn.execute("SELECT COUNT(*) FROM file_summaries").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM fraud_log WHERE email_id = ?", (email.id,)).fetchone()[0] == 0
