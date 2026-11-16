"""CloseDesk choosing the model that answers by itself, and suggesting a better fit.

At the start, when LM Studio answers with no chat model loaded and none is named in the settings file (.env),
CloseDesk has it load one: the one used last if it is still downloaded, else the most capable downloaded chat model
that fits this computer's memory. Any size can still be chosen in Settings; the automatic choice only prefers one
that fits. A model the user loaded is never unloaded or replaced without their click.

When the model answering is too big for this computer, or slow at reading mail (``SLOW_SECONDS`` an email or more),
and a smaller downloaded one would fit, Settings and Today suggest it once: "Load" switches, "Keep" stops asking
about that model.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Callable

from controller_inbox import local_llm, model_roles
from controller_inbox.local_llm import ModelStatus, check_model, document_reader, remembered_model, use_chat_model

if TYPE_CHECKING:
    from controller_inbox.config import Settings
    from controller_inbox.store import Store

# Seconds an email above which reading mail counts as slow, from at least SPEED_MIN_EMAILS read in one go.
SLOW_SECONDS = 45.0
SPEED_MIN_EMAILS = 2
SPEED_KEY = "model_speed"  # {model: {"seconds": average an email, "emails": how many}}, the last reading of each
KEPT_KEY = "model_kept"  # models the user chose to keep when another was suggested: not suggested against again


def pinned(settings: Settings) -> bool:
    """The settings file names the model that answers: loading another wouldn't change which one answers."""
    return settings.llm_model not in {"", "local-model"}


def _coder(key: str) -> bool:
    return "coder" in key.lower()


def _chat_keys(status: ModelStatus) -> list[str]:
    """LM Studio's downloaded chat models: never a document reader or an embedding model."""
    return [key for key in status.chat_models if not document_reader(key) and "embed" not in key.lower()]


def loaded_chat(status: ModelStatus) -> list[str]:
    """The chat models LM Studio has loaded now (by key)."""
    return [key for instance, (key, _context) in status.instances.items() if not document_reader(key) and not document_reader(instance)]


def _best_fit(settings: Settings, status: ModelStatus, keys: list[str]) -> str:
    """The most capable of ``keys`` that fits this computer: the largest that does, a general chat model before a
    coding one. "" when none fits or this can't be told (no sizes, memory unknown, LM Studio on another computer)."""
    memory = model_roles.computer_memory()
    if not status.chat_sizes or not memory or not model_roles.on_this_computer(settings, status):
        return ""
    fitting = [key for key in keys if model_roles.fits(settings, status, key, memory)]
    if not fitting:
        return ""
    return max(fitting, key=lambda key: (not _coder(key), status.chat_sizes[key]))


def auto_choice(settings: Settings, status: ModelStatus) -> str:
    """The chat model to load at the start, or "" to leave LM Studio as it is."""
    if status.mode == "off" or not status.reachable or not status.lm_studio or pinned(settings) or loaded_chat(status):
        return ""
    keys = _chat_keys(status)
    remembered = remembered_model(settings)
    if remembered in keys and not local_llm.load_failed_lately(remembered):
        return remembered
    return _best_fit(settings, status, [key for key in keys if not local_llm.load_failed_lately(key)])


def load_at_start(settings: Settings, on_progress: Callable[[str, int, int, str], None] | None = None) -> dict:
    """Have LM Studio load a chat model when none is loaded (``auto_choice``). Returns ``model`` (the one loaded, ""
    when none was) and ``message``: what happened in plain words, "" when nothing was done."""
    status = check_model(settings, use_cache=False)
    key = auto_choice(settings, status)
    if not key:
        return {"model": "", "message": ""}
    if on_progress:
        on_progress("model", 0, 0, f"{key}: this can take a minute")
    problem = use_chat_model(settings, key, only_if_none=True)
    if problem and loaded_chat(check_model(settings, use_cache=False)):
        # The user loaded one meanwhile: it answers, and nothing was loaded beside it.
        return {"model": "", "message": ""}
    if problem:
        return {
            "model": "",
            "message": f"CloseDesk couldn't load {key} in LM Studio. {problem} The mail is filed without a model "
            "for now; choose another in Settings → Local model.",
            "failed": True,
        }
    return {"model": key, "message": f"Loaded {key} in LM Studio: it answers your questions and reads the mail."}


def _read_json(store: Store, key: str, default):
    try:
        value = json.loads(store.get_state(key) or "null")
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def record_speed(store: Store, model: str, seconds: float, emails: int) -> None:
    """Keep how long ``model`` took an email in this reading (when it read enough to tell)."""
    if not model or emails < SPEED_MIN_EMAILS or seconds <= 0:
        return
    speeds = _read_json(store, SPEED_KEY, {})
    speeds[model] = {"seconds": round(seconds, 1), "emails": int(emails)}
    store.set_state(SPEED_KEY, json.dumps(speeds))


def seconds_an_email(store: Store, *names: str) -> float:
    """The last measured seconds an email for the model under any of ``names`` (instance id, key), or 0."""
    speeds = _read_json(store, SPEED_KEY, {})
    for name in names:
        entry = speeds.get(name) if name else None
        if isinstance(entry, dict) and isinstance(entry.get("seconds"), (int, float)):
            return float(entry["seconds"])
    return 0.0


def keep(store: Store, model: str) -> None:
    """The user keeps ``model``: it isn't suggested against again."""
    kept = _read_json(store, KEPT_KEY, [])
    if model and model not in kept:
        kept.append(model)
        store.set_state(KEPT_KEY, json.dumps(kept))


def kept(store: Store, model: str) -> bool:
    return model in _read_json(store, KEPT_KEY, [])


def _whole(seconds: float) -> str:
    return f"{round(seconds)} s"


def recommendation(settings: Settings, store: Store, status: ModelStatus | None = None) -> dict | None:
    """A better fit for this computer than the model answering, or None. ``current`` (the model answering),
    ``suggested`` (a downloaded chat model that fits and is smaller), ``reason`` ("slow" or "too_big") and
    ``message``, the line Settings and Today show above [Load] and [Keep]."""
    status = status or check_model(settings)
    if not status.active or not status.lm_studio or pinned(settings):
        return None
    current = status.instances.get(status.model, (status.model, 0))[0]
    if not current or kept(store, current):
        return None
    seconds = seconds_an_email(store, status.model, current)
    slow = seconds > SLOW_SECONDS
    too_big = model_roles.too_big(settings, status).get(current, "")
    size = status.chat_sizes.get(current, 0)
    if not (slow or too_big) or not size:
        return None
    smaller = [key for key in _chat_keys(status) if key != current and 0 < status.chat_sizes.get(key, 0) < size]
    suggested = _best_fit(settings, status, smaller)
    if not suggested:
        return None
    if slow:
        message = f"{current} takes about {_whole(seconds)} an email on this computer. {suggested} would fit and answer faster."
    else:
        memory = model_roles.computer_memory()
        needed = model_roles.memory_needed(size, model_roles.context_for(settings, status, current))
        message = (
            f"{current} needs about {round(needed / 1024**3)} GB of memory and this computer has "
            f"{round(memory / 1024**3)} GB, so LM Studio will likely run out of memory with it. {suggested} would fit and answer faster."
        )
    return {"current": current, "suggested": suggested, "reason": "slow" if slow else "too_big", "message": message}
