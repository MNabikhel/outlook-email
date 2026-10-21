"""What the classic pages (web.py) and the workspace (web_api.py) both do when a button is pressed.

Each change returns the key of the notice that says what happened (``web_common.NOTICES``); the classic routes
redirect with it and the workspace answers with its text. Something that isn't there raises ``Missing``, and a
change that can't be made raises ``Invalid`` (with a notice key, or a message of its own), which each view turns
into its own reply.
"""

from __future__ import annotations

import json
from datetime import date, datetime

from controller_inbox import cost_codes, fraud, vision
from controller_inbox.classify import month_end
from controller_inbox.config import Settings
from controller_inbox.digest import build_digest
from controller_inbox.profile import is_finance
from controller_inbox.store import Store

TASK_STATUSES = {"open", "done"}
CODING_STATUSES = {"review", "suggested", "unmatched", "confirmed", "all"}


class Missing(LookupError):
    """The email or task isn't stored (any more)."""


class Invalid(ValueError):
    """The change can't be made. ``notice`` is the key of the notice that says why, when there is one; else the
    message is the reason."""

    def __init__(self, message: str = "", *, notice: str = "") -> None:
        super().__init__(message)
        self.notice = notice


# The board -------------------------------------------------------------------------------------------------


def today_fields(as_of: date, *, sample: bool) -> dict:
    """The date line both views head their pages with: today (the sample's day while only the sample is loaded),
    in words, and how long until the month closes."""
    close = month_end(as_of)
    return {
        "today": as_of.isoformat(),
        "today_long": f"{as_of.strftime('%A')}, {as_of.strftime('%B')} {as_of.day}, {as_of.year}",
        "days_to_close": (close - as_of).days,
        "close_date": close.isoformat(),
        "is_sample": sample,
    }


def digest_now(store: Store, settings: Settings, as_of: date) -> dict:
    """Today's digest as of ``as_of``, built fresh and not saved: the Today page, the focus list and the chat's."""
    return build_digest(
        store,
        as_of=as_of,
        generated_at=datetime.now(settings.tz),
        tz=settings.tz,
        lookback_days=settings.digest_lookback_days,
        save=False,
        finance=is_finance(settings, store),
    )


def digest_view(store: Store, as_of: date, wanted: str = "", *, history: int = 30) -> dict:
    """A saved digest: the one for ``wanted`` (a date), else today's, else the latest. With the dates of the last
    ``history`` saved, and the one just newer and older than it to step to. Raises ``Missing`` when there is none
    saved for ``wanted``."""
    row = store.get_digest(wanted) if wanted else (store.get_digest(as_of.isoformat()) or store.latest_digest())
    if wanted and row is None:
        raise Missing(f"No digest saved for {wanted}")
    payload = None
    if row and row.get("payload"):
        payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
    saved = store.list_digests(limit=history)
    dates = [item["period_date"] for item in saved]
    current = row["period_date"] if row else ""
    index = dates.index(current) if current in dates else -1
    return {
        "row": row,
        "payload": payload,
        "saved": saved,
        "dates": dates,
        "current": current,
        "newer": dates[index - 1] if index > 0 else "",
        "older": dates[index + 1] if 0 <= index < len(dates) - 1 else "",
    }


def coding_rows(store: Store, settings: Settings, status: str, q: str = "") -> tuple[str, list[dict]]:
    """The AP invoices the Coding page lists: (the status shown, an unknown one being "review"; its rows)."""
    cost_codes.refresh_if_changed(store, settings)
    status = status if status in CODING_STATUSES else "review"
    return status, store.cost_codings(status=None if status == "all" else status, q=q or None)


# Changes ---------------------------------------------------------------------------------------------------


def mark_done(store: Store, email_id: str, done: bool) -> None:
    if not store.has_email(email_id):
        raise Missing(email_id)
    store.set_done(email_id, done)


def set_task_status(store: Store, action_id: str, status: str) -> None:
    """Tick a task off ("done") or put it back ("open")."""
    if status not in TASK_STATUSES:
        raise Invalid("A task is either open or done.")
    if not store.set_action_status(action_id, status):
        raise Missing(action_id)


def fraud_verdict(store: Store, settings: Settings, email_id: str, choice: str, note: str = "", *, now=None) -> str:
    """Record the user's fraud verdict. ``choice`` is the button's value: "safe" or "fraud", with a scope after a
    colon ("safe:domain"); the email alone without one."""
    verdict, _, scope = choice.partition(":")
    try:
        fraud.record_fraud_verdict(store, settings, email_id, verdict=verdict, scope=scope or "email", note=note, now=now)
    except KeyError:
        raise Missing(email_id) from None
    except ValueError as exc:
        raise Invalid(notice="fraud-freemail" if "free email" in str(exc) else "fraud-invalid") from None
    return "fraud-safe" if verdict == "safe" else "fraud-reported"


def correct_category(store: Store, settings: Settings, email_id: str, category: str, reason: str) -> str:
    from controller_inbox.learn import record_correction

    try:
        record_correction(store, settings, email_id=email_id, corrected_category=category, reason=reason)
    except KeyError:
        raise Missing(email_id) from None
    except ValueError as exc:
        raise Invalid(str(exc)) from None
    return "corrected"


def code_invoice(store: Store, settings: Settings, email_id: str, codes: list[str] | None = None) -> str:
    """Confirm the suggested cost code (``codes`` None), or change it to ``codes`` and confirm that."""
    if not store.has_email(email_id):
        raise Missing(email_id)
    try:
        if codes is None:
            cost_codes.confirm(store, settings, email_id)
        else:
            cost_codes.revise(store, settings, email_id, codes)
    except ValueError as exc:
        raise Invalid(str(exc)) from None
    return "coding-confirmed" if codes is None else "coding-revised"


def clear_findings(store: Store, email_id: str) -> str:
    """Forget the notes Ask CloseDesk kept on an email's files."""
    if not store.has_email(email_id):
        raise Missing(email_id)
    store.clear_findings(email_id)
    return "findings-cleared"


def start_processing(store: Store, settings: Settings, job) -> str:
    """Process new mail in the background: "processing", or "busy" ("busy-vision" while a scan is being read)
    when something is running already."""
    from controller_inbox.overnight import run_overnight

    started = job.start(
        lambda progress: run_overnight(
            store, settings, sync_graph=False, on_progress=progress, vision_minutes=vision.process_minutes(settings),
            should_stop=lambda: job.stopping,
        )
    )
    if started:
        return "processing"
    return "busy-vision" if (job.snapshot()["about"] or {}).get("kind") == "vision" else "busy"


def stop_processing(job) -> str:
    """Ask the running job to stop: "stopping", or "" when nothing is running."""
    return "stopping" if job.request_stop() else ""
