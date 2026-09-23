"""Which kind of inbox this is: ``general`` (anyone) or ``finance`` (adds month-end work)."""

from __future__ import annotations

from controller_inbox.config import PROFILES, Settings
from controller_inbox.store import Store

STATE_KEY = "profile"


def active_profile(settings: Settings, store: Store | None = None) -> str:
    """The Setup page choice wins over CONTROLLER_INBOX_PROFILE."""
    chosen = store.get_state(STATE_KEY) if store is not None else None
    return chosen if chosen in PROFILES else settings.profile


def is_finance(settings: Settings, store: Store | None = None) -> bool:
    return active_profile(settings, store) == "finance"


def set_profile(store: Store, profile: str) -> None:
    if profile not in PROFILES:
        raise ValueError(f"Unknown profile {profile!r}")
    store.set_state(STATE_KEY, profile)
