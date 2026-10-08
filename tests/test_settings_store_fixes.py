"""What the codebase sweep found in settings, the database and the digest."""

from __future__ import annotations

import copy
import sqlite3
from datetime import date, datetime, timezone

import pytest

from controller_inbox.config import Settings
from controller_inbox.digest import build_digest
from controller_inbox.store import Store, _add_column


def test_azure_keys_written_in_dotenv_as_the_readme_says_are_read(tmp_path, monkeypatch):
    for name in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_CLIENT_SECRET", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("AZURE_CLIENT_ID=1111-2222\nAZURE_TENANT_ID=\nAZURE_CLIENT_SECRET=s3cret\n")
    settings = Settings()
    assert (settings.azure_client_id, settings.azure_tenant_id, settings.azure_client_secret) == ("1111-2222", "common", "s3cret")
    assert settings.graph_configured
    # The prefixed name wins over the bare one, for every key.
    (tmp_path / ".env").write_text("AZURE_TENANT_ID=contoso\nCONTROLLER_INBOX_AZURE_TENANT_ID=fabrikam\n")
    assert Settings().azure_tenant_id == "fabrikam"
    assert Settings(azure_client_id="x", _env_file=None).azure_client_id == "x"


def test_a_line_left_empty_in_dotenv_means_the_default_and_amounts_may_have_commas(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("CONTROLLER_INBOX_PORT=\nCONTROLLER_INBOX_WRITEBACK=\nCONTROLLER_INBOX_HIGH_AMOUNT=$25,000\n")
    settings = Settings()
    assert (settings.port, settings.writeback, settings.high_amount) == (8765, False, 25000.0)


def test_a_windows_path_in_double_quotes_is_refused_with_a_clear_message():
    with pytest.raises(ValueError, match="single quotes"):
        Settings(data_dir="C:\\Users\nancy\\CloseDesk data", _env_file=None)


def test_two_processes_adding_the_same_column_after_an_update_dont_crash(tmp_path):
    path = tmp_path / "old.db"
    first, second = sqlite3.connect(path), sqlite3.connect(path)
    first.execute("CREATE TABLE emails (id TEXT PRIMARY KEY)")
    first.commit()
    assert _add_column(first, "ALTER TABLE emails ADD COLUMN folder TEXT DEFAULT ''")
    first.commit()
    assert not _add_column(second, "ALTER TABLE emails ADD COLUMN folder TEXT DEFAULT ''"), "the other process added it"
    with pytest.raises(sqlite3.OperationalError):
        _add_column(second, "ALTER TABLE no_such_table ADD COLUMN x TEXT")


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
