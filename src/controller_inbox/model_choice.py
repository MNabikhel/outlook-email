"""CloseDesk choosing the model that answers by itself, and suggesting a better fit.

At the start, when LM Studio answers with no chat model loaded and none is named in the settings file (.env),
CloseDesk has it load one: the one used last if it is still downloaded, else the largest downloaded chat model of
up to about 10 GB (an 8B) that fits this computer's memory, or the smallest that fits when none is that small. Any
size can still be chosen in Settings; the automatic choice only prefers one that fits. A model the user loaded is
never unloaded or replaced without their click.

When the model answering is too big for this computer's memory (with the context it was loaded with), or slow at
reading mail (over ``SLOW_SECONDS`` an email), and a smaller downloaded one would fit, Settings and Today suggest it once: "Load" switches, "Keep" stops asking
about that model.
"""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Callable

from controller_inbox import local_llm, model_roles
from controller_inbox.local_llm import ModelStatus, check_model, document_reader, remembered_model, use_chat_model

if TYPE_CHECKING:
    from controller_inbox.config import Settings
    from controller_inbox.store import Store

# Seconds an email above which reading mail counts as slow: the middle time of at least SPEED_MIN_EMAILS read in one
# go, so the first email waiting for LM Studio to load the model is outweighed by the others.
SLOW_SECONDS = 45.0
SPEED_MIN_EMAILS = 3
SPEED_KEY = "model_speed"  # {model: {"seconds": average an email, "emails": how many}}, the last reading of each
KEPT_KEY = "model_kept"  # models the user chose to keep when another was suggested: not suggested against again
# The largest download the automatic choice prefers: an 8B at Q8 is about 9 GB. A bigger one is chosen only when
# none this small fits; any size can still be chosen in Settings.
AUTO_LARGEST = 10 * 1024**3


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
    """The one of ``keys`` to choose for this computer: the largest that runs well on it and is at most
    ``AUTO_LARGEST`` (a 3B to 8B model is plenty, and answers faster), else the smallest that runs well; with none
    that runs well on a PC's graphics card, the same among those that load at all. With none that fits with the
    context Setup asks for, the same among the small ones with the shortest context CloseDesk would ask for. A general chat model before a
    coding one. "" when none fits or this can't be told (no sizes, memory unknown, LM Studio on another computer)."""
    budget = model_roles.model_budget()
    if not status.chat_sizes or not budget or not model_roles.on_this_computer(settings, status):
        return ""
    # With Setup's context first; else with the shorter one CloseDesk would ask for (an 8 GB computer).
    tries = [{}, {"shorter": True}]
    if budget.gpu:
        tries += [{"at_all": True}, {"at_all": True, "shorter": True}]
    for how in tries:
        fitting = [
            key for key in keys if model_roles.fits(settings, status, key, budget, **how)
            # A big model is never chosen by itself to run with less than Setup's context: only a small one is.
            and (not how.get("shorter") or status.chat_sizes[key] <= AUTO_LARGEST)
        ]
        if fitting:
            break
    else:
        return ""
    small = [key for key in fitting if status.chat_sizes[key] <= AUTO_LARGEST]
    if small:
        return max(small, key=lambda key: (not _coder(key), status.chat_sizes[key]))
    return min(fitting, key=lambda key: (_coder(key), status.chat_sizes[key]))


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
    return _load(settings, key, on_progress)


def ready_to_read(settings: Settings, model: str = "", on_progress: Callable[[str, int, int, str], None] | None = None) -> dict:
    """Before the model reads mail (any reading: Process new mail, Sync now, the automatic sync, the scheduled run,
    watch): when LM Studio has unloaded the model that reads it (``model``, else the one CloseDesk would ask for), it
    is loaded first, with the context Setup asks for, and a load LM Studio has under way is waited for. Otherwise the
    first email's own request has LM Studio load it, inside the time an email may take and with LM Studio's default
    context. With no model to ask for, nothing is loaded: only the start chooses one by itself (``load_at_start``).
    Returns what ``load_at_start`` does."""
    status = check_model(settings, use_cache=False)
    if status.mode == "off" or not status.reachable or not status.lm_studio:
        return {"model": "", "message": ""}
    # With just-in-time loading off, LM Studio lists only what is loaded: the model used last is still the one to load.
    remembered = remembered_model(settings)
    if remembered not in status.chat_models or pinned(settings) or local_llm.load_failed_lately(remembered):
        remembered = ""
    key = model or status.model or remembered
    if not key:  # nothing to ask for: Settings says to choose a model (the start chooses one itself)
        return {"model": "", "message": ""}
    if any(key in (instance, loaded) for instance, (loaded, _context) in status.instances.items()):
        return {"model": "", "message": ""}
    if key in local_llm._pending_loads or local_llm.model_busy(key):
        # LM Studio is loading it already (for a chat question, or a load CloseDesk stopped waiting for): another
        # load request beside it could load it twice.
        if on_progress:
            on_progress("model", 0, 0, f"{key}: LM Studio is loading it")
        if local_llm.wait_for_load(settings, key):
            return {"model": key, "message": ""}
        return {"model": "", "message": f"LM Studio was still loading {key}. The mail is filed without a model for now.", "failed": True}
    if key not in status.chat_models:  # not a model LM Studio has downloaded under this name: left to LM Studio
        return {"model": "", "message": ""}
    return _load(settings, key, on_progress)


def _load(settings: Settings, key: str, on_progress: Callable[[str, int, int, str], None] | None) -> dict:
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


def faster_model(settings: Settings, status: ModelStatus, current: str) -> str:
    """A downloaded chat model smaller than ``current`` that fits this computer (the most capable such), or ""."""
    size = status.chat_sizes.get(current, 0)
    smaller = [key for key in _chat_keys(status) if key != current and 0 < status.chat_sizes.get(key, 0) < size]
    return _best_fit(settings, status, smaller)


def recommendation(settings: Settings, store: Store, status: ModelStatus | None = None) -> dict | None:
    """A better fit for this computer than the model answering, or None. ``current`` (the model answering),
    ``suggested`` (a downloaded chat model that fits and is smaller), ``reason`` ("slow" or "too_big") and
    ``message``, the line Settings and Today show above [Load] and [Keep]."""
    status = status or check_model(settings)
    if not status.active or not status.lm_studio or pinned(settings):
        return None
    current, loaded_context = status.instances.get(status.model, (status.model, 0))
    if not current or kept(store, current):
        return None
    seconds = seconds_an_email(store, status.model, current)
    slow = seconds > SLOW_SECONDS
    size = status.chat_sizes.get(current, 0)
    # The memory it takes with the context LM Studio loaded it with (Setup's when LM Studio doesn't say).
    tokens = loaded_context or model_roles.context_for(settings, status, current)
    needed = model_roles.memory_needed(size, tokens)
    budget = model_roles.model_budget()
    too_big = bool(size and budget and model_roles.on_this_computer(settings, status) and needed > budget.runs_well)
    if not (slow or too_big) or not size:
        return None
    suggested = faster_model(settings, status, current)
    if not suggested:
        return None
    if slow:
        message = f"{current} takes about {_whole(seconds)} an email on this computer. {suggested} would fit and answer faster."
    else:
        # It loaded and answers: only what is known is said, not that it will fail or that the other is faster.
        context = f" with a {tokens:,}-token context" if tokens else ""
        runs_on_card = budget.gpu and model_roles.fits(settings, status, suggested, budget)
        where = "on its graphics card" if runs_on_card else "in its memory"
        message = (
            # Rounded up, as budget.said() rounds down: never "needs about 21 GB" and "can give about 21 GB".
            f"{current} needs about {math.ceil(needed / 1024**3)} GB of memory{context} and {budget.said()}, so the "
            f"computer may slow down while it answers. {suggested} fits {where}."
        )
    return {"current": current, "suggested": suggested, "reason": "slow" if slow else "too_big", "message": message}
