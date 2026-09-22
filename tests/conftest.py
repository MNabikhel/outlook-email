from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from controller_inbox.config import Settings
from controller_inbox.pipeline import ingest_demo
from controller_inbox.store import Store


@pytest.fixture
def as_of_now() -> datetime:
    return datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path, inbox_dir=tmp_path / "inbox", _env_file=None)


@pytest.fixture
def store(settings: Settings) -> Store:
    return Store(settings.db_path)


@pytest.fixture
def loaded(store: Store, settings: Settings, as_of_now: datetime) -> Store:
    ingest_demo(store, settings, now=as_of_now)
    return store
