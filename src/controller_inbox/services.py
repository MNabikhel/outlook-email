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
from controller_inbox.models import IMPORTANCE_LABELS
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
        if isinstance(payload, dict):
            _give_focus_rows_their_importance(store, payload)
            _say_which_are_held(store, payload)
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


def _give_focus_rows_their_importance(store: Store, payload: dict) -> None:
    """Each focus row's email as it is now: whether it is held as possible payment fraud, so one reported as fraud
    since the digest was saved says "Held for fraud" and has the held edge, as on Today (and one cleared since its
    importance again); and, on a digest saved before its focus rows said their email's importance, that too, so each
    says it when the importance words are asked for and a Critical or High one has the edge of its importance. A row
    whose email is gone stays as it was saved."""
    rows = [row for row in payload.get("focus") or [] if isinstance(row, dict)]
    emails = store.get_emails([str(row.get("email_id") or "") for row in rows])
    for row in rows:
        email = emails.get(str(row.get("email_id") or ""))
        if email is None:
            continue
        if not row.get("importance"):
            row["importance"] = email.importance.value
            row["importance_label"] = IMPORTANCE_LABELS.get(email.importance, email.importance.value)
        row["locked"] = fraud.attachments_locked(email)


def _say_which_are_held(store: Store, payload: dict) -> None:
    """Whether each email of a digest is held as possible payment fraud, worked out from the email as it is now: one
    reported as fraud since the digest was saved is held on it too, as on every other page (and one cleared since is
    not), and a digest saved before its emails said so says it. Where it says an email's importance, a held one says
    "Held for fraud" instead. Every list of emails (the "Do not process" box, what came in, Invoices to enter, Cash to
    apply, Close / reconcile, and any added later): its tasks have no importance word. An email that is gone keeps
    what was saved."""
    new_mail = payload.get("new_mail")
    lists = [value for value in payload.values() if isinstance(value, list)]
    if isinstance(new_mail, dict):
        lists += [items for items in new_mail.values() if isinstance(items, list)]
    items = [item for items in lists for item in items if isinstance(item, dict) and item.get("importance_label")]
    emails = store.get_emails([str(item.get("email_id") or item.get("id") or "") for item in items])
    for item in items:
        email = emails.get(str(item.get("email_id") or item.get("id") or ""))
        if email is not None:
            item["locked"] = fraud.attachments_locked(email)
        else:
            item.setdefault("locked", False)


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
    """Ask the running job to stop: "stopping" (reading scans stops after its page), "stopping-model" (the model was
    hung up on: a file's summary or a related-mail question), or "" when nothing is running."""
    model = bool((job.snapshot() or {}).get("can_stop"))
    if not job.request_stop():
        return ""
    return "stopping-model" if model else "stopping"
