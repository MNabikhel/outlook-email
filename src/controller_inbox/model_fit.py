"""CloseDesk fitting itself to the chat model loaded: the context it asks LM Studio for, and how long it waits.

The context. Setup's minimum (``min_context_tokens``) is what a small model gets. A model too big for this computer's
memory with that much is asked for the largest of Setup's steps (``agent.CONTEXT_STEPS``) that still fits, never less
than 4,096 tokens (or the most the model supports): the minimum is "at most" for a big model. Loading at the start,
Load and use in Settings, and the chat raising the context all ask for this. A load that still runs out of memory
steps back (``local_llm.step_back_context``).

The wait. How long the chat waits for the model's first word (it reads the whole prompt before it writes one), and
how long the reading of mail waits between words. Worked out per model: from how fast it read prompts and wrote
answers on this computer, as measured from the chat's own questions (``record_speed``); else from how long it took
an email in the last reading (``model_choice.seconds_an_email``); else from its size (about 2 minutes for an 8B, 6 to
10 for a 27B to 32B, half as long again for one that thinks first). A wait the user set (in Settings, or the chat's
"Save and ask again") wins over the worked-out one, for that model.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from controller_inbox import local_llm, model_choice, model_roles
from controller_inbox.local_llm import ModelStatus, check_model

if TYPE_CHECKING:
    from controller_inbox.config import Settings
    from controller_inbox.store import Store

GB = 1024**3
SMALLEST_CONTEXT = 4096
# The worked-out wait stays between these. A user's own wait is from 1 to 120 minutes.
WAIT_FLOOR = 120.0
WAIT_CEILING = 1800.0
# Not less than 2: every call waits at least settings.llm_timeout (90 seconds) whatever the wait set.
MIN_MINUTES, MAX_MINUTES = 2, 120
# Seconds of wait for each GB of the model, when nothing is measured yet: 2 minutes for an 8B (about 5 GB at Q4),
# about 7 minutes for a 30B (about 18 GB). A model that thinks before it answers gets half as long again.
SECONDS_PER_GB = 24.0
THINKING_SHARE = 1.5
# A measured speed is stretched this much for the wait (a busy laptop reads slower), plus a little more.
MARGIN = 1.5
SPARE_SECONDS = 30.0
# What reading one email asks of the model (its prompt and its reply), in tokens: a speed known only as seconds an
# email becomes tokens a second with this.
EMAIL_TOKENS = 2000
# The chat's own calls: each prompt read and each answer written moves the speeds kept for the model this much.
NEW_WEIGHT = 0.5
# A prompt read this many times faster than the model was measured reading isn't kept: LM Studio had read most of it
# before (the server doesn't always say), and kept, one such question would cut the wait for the next to a fraction.
FASTEST_JUMP = 4.0
# But this many such readings in a row are the model's own speed (the one kept was slow: its first question waited
# behind another request, say), and the slowest of them is kept instead, when the server said how much of the prompt
# it had read before: follow-up questions about the same mail look just as fast when it doesn't.
FAST_IN_A_ROW = 3
# When it doesn't say (LM Studio often doesn't), the same many readings count when their prompts are of quite
# different sizes (a follow-up's prompt is the last one and a little more), or this many in a row however they come,
# so one slow first reading isn't kept for good.
APART = 1.25
GUESSED_IN_A_ROW = 8

WAIT_KEY = "model_wait"  # {model: seconds} the user chose
RATES_KEY = "model_rates"  # {model: {"read": prompt tokens a second, "write": tokens written a second}}


def model_key(status: ModelStatus, model: str = "") -> str:
    """LM Studio's key for the model answering (or ``model``), as Settings lists it; its own name elsewhere."""
    model = model or status.model
    return status.instances.get(model, (model, 0))[0] or model


def _steps() -> tuple[int, ...]:
    from controller_inbox import agent  # it imports the store and the rest

    return agent.CONTEXT_STEPS


def auto_context(settings: Settings, status: ModelStatus, key: str, *, want: int | None = None) -> int:
    """The context CloseDesk asks LM Studio for ``key``: ``want`` (Setup's minimum) and no more than the model
    supports, or for a model too big for that on this computer, the largest of Setup's steps that fits its memory
    (never under 4,096 tokens or the model's own most). 0 when Setup asks for none (the model as LM Studio loads it).
    Setup's minimum as it is when the model's size, this computer's memory, or where LM Studio runs can't be told."""
    target, most = _wanted(settings, status, key, want)
    if not target:
        return 0
    size = status.chat_sizes.get(key, 0)
    budget = model_roles.model_budget()
    if not size or budget is None or not model_roles.on_this_computer(settings, status):
        return target
    floor = min(SMALLEST_CONTEXT, most or SMALLEST_CONTEXT, target)
    sizes = [target, *sorted((step for step in _steps() if floor <= step < target), reverse=True)]
    if floor not in sizes:
        sizes.append(floor)
    # What runs at speed (a PC's graphics card); a model that doesn't at any size, what loads at all.
    room = model_roles.room_for(size, floor, budget)
    for tokens in sizes:
        if model_roles.memory_needed(size, tokens) <= room:
            return tokens
    return floor


def _wanted(settings: Settings, status: ModelStatus, key: str, want: int | None = None) -> tuple[int, int]:
    """Setup's minimum (or ``want``) no more than ``key`` supports, and the most it supports (0 when not told)."""
    want = settings.min_context_tokens if want is None else want
    most = status.chat_models.get(key, 0) or (status.max_context if key and key == status.key else 0)
    return (min(want, most) if want and most else want), most


def fits_with(settings: Settings, status: ModelStatus, key: str, tokens: int) -> bool:
    """``key`` fits this computer with a ``tokens`` context, judged against the same memory ``auto_context`` fits it
    into. True when that can't be told (its size, this computer's memory, or where LM Studio runs isn't known)."""
    size = status.chat_sizes.get(key, 0)
    budget = model_roles.model_budget()
    if not size or budget is None or not model_roles.on_this_computer(settings, status):
        return True
    target, most = _wanted(settings, status, key)
    floor = min(SMALLEST_CONTEXT, most or SMALLEST_CONTEXT, target) if target else 0
    return model_roles.memory_needed(size, tokens) <= model_roles.room_for(size, floor, budget)


def _read(store: Store | None, key: str) -> dict:
    if store is None:
        return {}
    try:
        value = json.loads(store.get_state(key) or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def speeds(store: Store | None, *names: str) -> dict[str, float]:
    """How fast the model under any of ``names`` read prompts (``read``) and wrote (``write``) in tokens a second, as
    measured here; empty when it hasn't been."""
    rates = _read(store, RATES_KEY)
    for name in names:
        entry = rates.get(name) if name else None
        if isinstance(entry, dict):
            return {kind: float(value) for kind, value in entry.items() if isinstance(value, (int, float)) and value > 0}
    return {}


def record_speed(store: Store, model: str, *, prompt_tokens: int, read_seconds: float, written: int = 0,
                 write_seconds: float = 0.0, counted: bool = False) -> None:
    """Keep how fast ``model`` read a prompt of ``prompt_tokens`` (its first word came ``read_seconds`` after the
    question went) and wrote ``written`` tokens in ``write_seconds``, so the next estimate and wait are nearer. A
    reading ``FASTEST_JUMP`` times faster than the one kept isn't kept (a prompt LM Studio had read before), unless
    ``FAST_IN_A_ROW`` come in a row that the server ``counted`` (it said how much of the prompt it had read before,
    so ``prompt_tokens`` are the ones it read anew): then the slowest of them replaces the one kept. Readings it
    didn't count do the same when ``FAST_IN_A_ROW`` of them had prompts of quite different sizes, or
    ``GUESSED_IN_A_ROW`` came in a row."""
    if not model:
        return
    rates = _read(store, RATES_KEY)
    entry = rates.get(model) if isinstance(rates.get(model), dict) else {}
    found = {}
    if prompt_tokens >= 200 and read_seconds > 0:
        found["read"] = prompt_tokens / read_seconds
    if written >= 20 and write_seconds > 0:
        found["write"] = written / write_seconds
    if not found:
        return
    for kind, rate in found.items():
        before = entry.get(kind)
        rate = min(rate, 1e6)
        if kind == "read" and isinstance(before, (int, float)) and rate > before * FASTEST_JUMP:
            if not counted:  # most likely a prompt it had read before: not kept unless enough such come in a row
                guessed = _guessed(entry) + [[prompt_tokens, rate]]
                apart = []  # those whose prompts are of quite different sizes
                for tokens, value in guessed:
                    if all(max(tokens, other) > min(tokens, other) * APART for other, _value in apart):
                        apart.append([tokens, value])
                taken = apart if len(apart) >= FAST_IN_A_ROW else guessed if len(guessed) >= GUESSED_IN_A_ROW else None
                if taken is None:
                    entry["read_guessed"] = guessed  # lists: speeds() leaves them out
                    continue
                rate = min(value for _tokens, value in taken)
            else:
                fast = [value for value in entry.get("read_fast", []) if isinstance(value, (int, float))] + [rate]
                if len(fast) < FAST_IN_A_ROW:
                    entry["read_fast"] = fast
                    continue
                rate = min(fast)
            entry.pop("read_fast", None)
            entry.pop("read_guessed", None)
            entry[kind] = round(rate, 2)
            continue
        if kind == "read":
            entry.pop("read_fast", None)
            entry.pop("read_guessed", None)
        entry[kind] = round(rate if not isinstance(before, (int, float)) else before * (1 - NEW_WEIGHT) + rate * NEW_WEIGHT, 2)
    rates[model] = entry
    store.set_state(RATES_KEY, json.dumps(rates))


def _guessed(entry: dict) -> list[list[float]]:
    """The fast readings kept so far that the server didn't count, as [prompt tokens, tokens a second]."""
    found = entry.get("read_guessed")
    return [
        [item[0], item[1]] for item in (found if isinstance(found, list) else [])
        if isinstance(item, list) and len(item) == 2 and all(isinstance(value, (int, float)) for value in item)
    ]


def _thinks(settings: Settings, status: ModelStatus, model: str) -> bool:
    from controller_inbox import local_llm

    options = status.reasoning if status.model == model else status.reasoning_options.get(model, [])
    return model in local_llm._reasoning_seen or "on" in options


def _context_tokens(settings: Settings, status: ModelStatus, key: str) -> int:
    """The longest prompt the chat sends this model: the context it is loaded with, or would be."""
    loaded = status.context_length if key == model_key(status) else 0
    return loaded or auto_context(settings, status, key) or SMALLEST_CONTEXT


def automatic_wait(settings: Settings, status: ModelStatus, key: str, store: Store | None = None) -> float:
    """The wait CloseDesk works out for ``key`` in seconds (see the module's note), between ``WAIT_FLOOR`` and
    ``WAIT_CEILING``; 0 when nothing is known about the model (its size isn't given and it hasn't been measured)."""
    from controller_inbox import model_choice
    from controller_inbox.local_llm import THINKING_ROOM

    instance = status.model if key == model_key(status) else key
    thinks = _thinks(settings, status, instance) or _thinks(settings, status, key)
    rates = speeds(store, key, instance)
    tokens = _context_tokens(settings, status, key)
    seconds = 0.0
    if rates.get("read"):
        seconds = tokens / rates["read"] * MARGIN + SPARE_SECONDS
        if thinks and rates.get("write"):  # a call that isn't streamed sends nothing until its thinking is done too
            seconds += THINKING_ROOM / rates["write"]
    elif store is not None and (per_email := model_choice.seconds_an_email(store, instance, key)):
        seconds = per_email * tokens / EMAIL_TOKENS * MARGIN + SPARE_SECONDS
    elif size := status.chat_sizes.get(key, 0):
        seconds = size / GB * SECONDS_PER_GB * (THINKING_SHARE if thinks else 1.0)
    if seconds <= 0:
        return 0.0
    return min(WAIT_CEILING, max(WAIT_FLOOR, seconds))


def chosen_wait(store: Store | None, *names: str) -> float:
    """The wait the user set for the model under any of ``names``, in seconds, or 0."""
    waits = _read(store, WAIT_KEY)
    for name in names:
        value = waits.get(name) if name else None
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            return float(value)
    return 0.0


def parse_minutes(value) -> int:
    """A wait typed in minutes, as a whole number from 2 to 120. Raises ``ValueError`` with what to type instead."""
    text = str(value if value is not None else "").strip().replace(",", ".")
    try:
        number = float(text)
    except ValueError:
        raise ValueError(f"Type a number of minutes, from {MIN_MINUTES} to {MAX_MINUTES}.") from None
    if not math.isfinite(number) or number != round(number):
        raise ValueError(f"Type a whole number of minutes, from {MIN_MINUTES} to {MAX_MINUTES}.")
    if not MIN_MINUTES <= number <= MAX_MINUTES:
        raise ValueError(f"Type a number of minutes from {MIN_MINUTES} to {MAX_MINUTES}.")
    return int(number)


def set_wait(store: Store, model: str, minutes: int | None) -> None:
    """Keep the user's wait for ``model`` (``minutes``), or with None go back to the worked-out one."""
    if not model:
        raise ValueError("No model is answering now, so there is no wait to set.")
    waits = _read(store, WAIT_KEY)
    if minutes is None:
        waits.pop(model, None)
    else:
        waits[model] = float(parse_minutes(minutes) * 60)
    store.set_state(WAIT_KEY, json.dumps(waits))


@dataclass(frozen=True)
class Wait:
    """How long the chat waits for ``model``: ``seconds``, and whether the user set it (``yours``)."""

    model: str
    seconds: float
    yours: bool

    @property
    def minutes(self) -> int:
        return max(1, round(self.seconds / 60))


def chat_wait(store: Store | None, settings: Settings, status: ModelStatus | None = None) -> Wait:
    """How long the chat waits for the model answering to start (and between words): the user's own wait for it,
    else the worked-out one, never less than the chat's usual 5 minutes (``CHAT_TIMEOUT``)."""
    from controller_inbox import local_llm

    status = status or check_model(settings)
    key = model_key(status)
    if yours := chosen_wait(store, key, status.model):
        # Never said as less than every call waits anyway (a wait of 1 minute kept from before).
        return Wait(key, max(yours, settings.llm_timeout), True)
    return Wait(key, max(local_llm.CHAT_TIMEOUT, automatic_wait(settings, status, key, store)), False)


def reading_wait(store: Store | None, settings: Settings, model: str, status: ModelStatus | None = None) -> float:
    """How long the reading of mail waits between the words of ``model`` (an instance id or key) at least: the user's
    wait for it, else the worked-out one (0 when nothing is known: the reading's own waits apply)."""
    status = status or check_model(settings)
    key = model_key(status, model)
    return chosen_wait(store, key, model) or automatic_wait(settings, status, key, store)


def minutes_said(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


def describe(store: Store | None, settings: Settings, status: ModelStatus | None = None) -> dict | None:
    """What Settings shows for the model answering: ``model``, ``context`` (the tokens it is loaded with, or when it
    isn't loaded the tokens CloseDesk asks for; 0 when that isn't known), ``minutes`` (its wait), ``yours`` (the user
    set it) and ``line``, in words. None with no model."""
    from controller_inbox import local_llm  # it imports this module

    status = status or check_model(settings)
    if not status.active:
        return None
    wait = chat_wait(store, settings, status)
    key = wait.model
    # The context the server reports, whichever it is; only LM Studio is asked to load the model with another.
    loaded = status.context_length
    context = loaded or (auto_context(settings, status, key) if status.lm_studio else 0)
    name = key.split("/")[-1]
    server = "LM Studio" if status.lm_studio else "the model server"
    held = f"{context:,}-token context" if context else f"the context {server} loaded it with"
    # Loaded with less than CloseDesk asks for, and the next question reloads it: the line says both.
    wanted = auto_context(settings, status, key) if loaded and status.lm_studio else 0
    if (
        0 < loaded < wanted
        and not local_llm.raise_tried(status)
        and local_llm.context_fits(settings, status)
    ):
        held += f" ({wanted:,} from the next question)"
    how = "set by you" if wait.yours else "set automatically"
    return {
        "model": key,
        "context": context,
        "minutes": wait.minutes,
        "yours": wait.yours,
        "line": f"{name}: {held}, waits up to {minutes_said(wait.seconds)} ({how}).",
    }


def suggested_minutes(
    store: Store | None, settings: Settings, status: ModelStatus, *, prompt_tokens: int, waited: float, elapsed: float,
    written: int = 0,
) -> int:
    """How many minutes to suggest for the model answering after it ran out of time on a question: from its measured
    prompt-reading speed and this question's prompt (``prompt_tokens``) when both are known, else from its size, else
    a bit over the ``elapsed`` seconds it had taken when it was cut off. ``written``: the tokens the call that ran out
    might write (thinking included) when it isn't streamed, so nothing came back until it was all written: the time
    to write them is added (from the measured writing speed, else half as long again). More than the ``waited`` it
    had, in whole minutes from 2 to 120: so not more when it had 120 already (the chat then offers no more time)."""
    key = model_key(status)
    rates = speeds(store, key, status.model)
    if rates.get("read") and prompt_tokens:
        seconds = prompt_tokens / rates["read"] * MARGIN + SPARE_SECONDS
        if written > 0:
            seconds = seconds + written / rates["write"] if rates.get("write") else seconds * THINKING_SHARE
    elif size := status.chat_sizes.get(key, 0):
        thinks = _thinks(settings, status, status.model) or _thinks(settings, status, key)
        seconds = size / GB * SECONDS_PER_GB * (THINKING_SHARE if thinks else 1.0)
    else:
        seconds = elapsed * MARGIN
    minutes = max(math.ceil(seconds / 60), max(1, round(waited / 60)) + 1)  # more than the minutes it was given
    return min(MAX_MINUTES, max(MIN_MINUTES, minutes))


def more_time_text(model: str, waited: float, suggest: int) -> str:
    """What the chat says above its minutes box when the model ran out of time."""
    name = model.split("/")[-1] or "The model"
    return (
        f"The local model needed more time than CloseDesk gives it ({minutes_said(waited)}). {name} usually needs "
        f"about {minutes_said(suggest * 60)} for a question like this on this computer."
    )


def estimate(store: Store | None, *names: str, tokens: int) -> float | None:
    """About how many seconds the model under any of ``names`` takes to read a prompt of ``tokens``, from its measured
    speed here; None when it hasn't been measured."""
    rate = speeds(store, *names).get("read")
    return tokens / rate if rate and tokens > 0 else None


def estimate_call(store: Store | None, *names: str, tokens: int, written: int) -> float | None:
    """About how many seconds a call that isn't streamed takes the model under any of ``names``: reading a prompt of
    ``tokens`` and writing up to ``written`` tokens (thinking included), since nothing comes back until both are
    done. None when either speed hasn't been measured here."""
    rates = speeds(store, *names)
    if not rates.get("read") or not rates.get("write") or tokens <= 0:
        return None
    return tokens / rates["read"] + max(0, written) / rates["write"]


def about(seconds: float | None) -> str:
    """An estimate in words: "about 40 seconds", "about 3 minutes"; "" for none."""
    if not seconds or seconds <= 0:
        return ""
    if seconds < 60:
        return f"about {max(5, round(seconds / 5) * 5)} seconds"
    return f"about {minutes_said(seconds)}"


def context_line(settings: Settings, model: ModelStatus) -> dict:
    """The line under the model in Settings (both pages) saying whether its context window holds whole attachments:
    ``shown``, ``tokens``, ``label``, ``tone`` ("ok" or "short") and ``advice``, in plain words."""
    from controller_inbox.agent import RECOMMENDED_CONTEXT  # it imports the store and the rest
    from controller_inbox.local_llm import context_capped, context_fits, context_target, needs_more_context

    window = model.context_length or settings.chat_context_tokens
    target = context_target(settings, model)
    recommended = RECOMMENDED_CONTEXT
    fits = context_fits(settings, model)
    capped = context_capped(settings, model)
    short = False  # the window can't be used as it is, however long it is
    # The model used last, not loaded now: LM Studio loads it when it is asked, and CloseDesk then sizes it.
    waiting = bool(model.lm_studio and model.model and not model.key and model.model in model.chat_models)
    if waiting:
        asked = auto_context(settings, model, model.model)
        supported, _most = _wanted(settings, model, model.model)
        if asked:
            most = " (the most that fits this computer)" if asked < supported else ""
            advice = (
                f"{model.model} isn't loaded in LM Studio right now. It is loaded when you ask something, and "
                f"CloseDesk asks for {asked:,} tokens{most}."
            )
            if asked < recommended:
                advice += " Long attachments are then read a section at a time."
        else:
            advice = f"{model.model} isn't loaded in LM Studio right now. It is loaded when you ask something, with the context set in LM Studio."
        window = asked
    elif not window:
        advice = (
            "The model server doesn't say how long its context is. For whole workbooks and PDFs, load the model "
            f"there with a context length of {recommended:,} or more."
        )
    elif (
        model.key and model.context_length > target > 0
        and local_llm._context_given.get(model.key) != model.context_length
        and not fits_with(settings, model, model.key, model.context_length)
    ):
        # Loaded in LM Studio with more than fits this computer: CloseDesk leaves a model the user loaded as it is.
        advice = (
            f"{model.key} with {window:,} tokens likely doesn't fit in this computer's memory, so its answers may run "
            f"out of memory. To fix it, reload it in LM Studio with Context Length {target:,} (My Models → the "
            "model's settings → Context Length), the most that fits here."
        )
        short = True
    elif needs_more_context(settings) and fits:
        why = "the most that fits this computer" if capped else "the minimum set below"
        advice = (
            f"The next time you ask something, CloseDesk reloads {model.key} in LM Studio with {target:,} tokens "
            f"({why}). If your computer runs short of memory, it goes back to the current size."
        )
    elif window >= recommended:
        advice = "Enough to read whole attachments when you ask about them."
    else:
        advice = "Ask CloseDesk reads long attachments a section at a time with this."
        ran_out = local_llm._context_stepped.get(model.key, 0) if model.key else 0
        if ran_out > window:
            advice += (
                f" {model.key} ran out of memory answering with {ran_out:,} tokens, so CloseDesk loaded it back with "
                f"{window:,}. A smaller model has room for more."
            )
        elif model.key and model.context_length and window < target and not fits:
            advice += (
                f" {model.key} with {target:,} tokens wouldn't fit in this computer's memory, so CloseDesk keeps it "
                "as LM Studio loaded it. Pick a smaller model, or a smaller minimum below."
            )
        elif model.key and model.context_length and window < target:
            advice += (
                f" LM Studio couldn't load {model.key} with {target:,} tokens. "
                "Close other apps or pick a smaller minimum below, then Save to try again."
            )
        elif model.key and capped and model_choice.small(model, model.key):
            # A 3B to 8B model: the size the guide recommends, so no smaller one is pointed to.
            advice += (
                f" A longer context for {model.key} needs more memory than this computer can give a model, so "
                f"CloseDesk asks LM Studio for {target:,} tokens."
            )
        elif model.key and capped:
            advice += (
                f" {model.key} is too big for this computer's memory with a longer context, so CloseDesk asks LM "
                f"Studio for {target:,} tokens. A smaller model reads whole workbooks and PDFs."
            )
        elif model.key and model.max_context and model.max_context < recommended:
            advice += f" {model.key} supports at most {model.max_context:,} tokens. For whole workbooks and PDFs, load a model with a longer context."
        elif model.key:
            advice += " For whole workbooks and PDFs, move the minimum below to 16k or more and Save."
        else:
            advice += (
                f" For whole workbooks and PDFs, reload the model in LM Studio with Context Length {recommended:,} or more "
                "(My Models → the model's settings → Context Length)."
            )
    return {
        "shown": model.active,
        "tokens": window,
        "label": "not loaded yet" if waiting else f"{window:,} tokens" if window else "not reported",
        "tone": "ok" if window >= recommended and not short else "short",
        "advice": advice,
    }

