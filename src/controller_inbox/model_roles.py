"""Which model does which job, for Setup and the workspace's Settings: the model that answers questions, the one that
reads scanned pages (OvisOCR2 by default, the older method when it can't), the one that finds mail by meaning, and
the OCR engine that reads scans the moment they arrive."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from controller_inbox.local_llm import ModelStatus, check_model

if TYPE_CHECKING:
    from controller_inbox.config import Settings


def _loaded(status: ModelStatus, model: str) -> bool:
    """Whether ``model`` (an id or LM Studio's key for it) is loaded on the server now."""
    if not model:
        return False
    if model in status.instances or model in {key for key, _context in status.instances.values()}:
        return True
    return not status.lm_studio and model == status.model


def chat_row(status: ModelStatus) -> dict:
    row = {"role": "Answers your questions", "model": "", "state": "off", "status": "", "note": ""}
    if status.mode == "off":
        row["status"] = "Turned off in CloseDesk's settings file (.env): answers come from looking things up."
    elif status.active:
        row["model"], row["state"] = status.model, "on"
        context = f" with a {status.context_length:,}-token context" if status.context_length else ""
        row["status"] = f"Loaded{context}."
    elif status.reachable:
        row["status"] = "No model loaded: load one in LM Studio (Qwen3.5 9B)."
    else:
        row["status"] = "LM Studio's server isn't answering: answers come from looking things up."
    return row


def reader_row(settings: Settings, status: ModelStatus) -> dict:
    """The page reader, or the older method scans fall back to, and why."""
    from controller_inbox import vision

    row = {"role": "Reads scanned pages and pictures", "model": "", "state": "fallback", "status": "", "note": ""}
    if settings.vision_mode == "off":
        row["state"] = "off"
        row["status"] = "Turned off in Setup: scans are read with OCR only (the older method)."
        return row
    reader = vision.reading_model(settings)
    if reader and vision.trusted_reader(reader) and vision.can_render():
        row["model"], row["state"] = vision.reader_name(reader), "on"
        row["status"] = "Loaded." if _loaded(status, reader) else "Downloaded: LM Studio loads it when a page is read."
        if settings.vision_mode == "ask":
            row["status"] += " Pages are read only when you ask."
        return row
    row["status"] = f"Scans are read with {vision.older_method(settings, reader)}."
    row["note"] = vision.why_not_reader(settings, reader)
    if reader:
        row["model"] = reader
    return row


def search_row(settings: Settings, status: ModelStatus) -> dict:
    from controller_inbox import semantic

    model = semantic.embedding_model(settings) if status.reachable else ""
    if not model:
        return {"role": "Finds mail by meaning", "model": "", "state": "off", "note": "",
                "status": "Off: search matches words only. Load an embedding model in LM Studio (nomic-embed-text)."}
    # LM Studio lists every downloaded model on /v1/models (just-in-time loading): only its loaded instances count.
    loaded = model in status.embeddings if status.lm_studio else _loaded(status, model) or model in status.models
    return {"role": "Finds mail by meaning", "model": model, "state": "on", "note": "",
            "status": "Loaded." if loaded else "Downloaded: LM Studio loads it when mail is searched."}


def ocr_row() -> dict:
    from controller_inbox import ocr

    engine = ocr.engine_name()
    if engine:
        return {"role": "Reads scans as they arrive (OCR)", "model": engine, "state": "on", "note": "",
                "status": "On this computer: a scan's text is there in seconds, before the page reader runs."}
    return {"role": "Reads scans as they arrive (OCR)", "model": "", "state": "off", "note": "",
            "status": "Not installed: scans wait for the page reader. " + ocr.missing_note(why_only=True)}


def chat_choices(settings: Settings, status: ModelStatus) -> dict | None:
    """What Setup's chat model list offers: LM Studio's downloaded chat models (``models``), the one answering now
    (``current``, its key) and the loaded ones (``loaded``). ``pinned`` when the settings name a model, so loading
    another wouldn't change which one answers. ``too_big``: why each model too big for this computer's memory is
    (see ``too_big``). None when the server can't load models when asked (not LM Studio)."""
    if status.mode == "off" or not status.lm_studio or not status.chat_models:
        return None
    return {
        "models": list(status.chat_models),
        "current": status.instances.get(status.model, (status.model, 0))[0],
        "loaded": {key for key, _context in status.instances.values()},
        "pinned": settings.llm_model not in {"", "local-model"},
        "too_big": too_big(settings, status),
    }


# What a model takes beside itself for each token of context it is loaded with (its key-value cache). Models differ
# a lot; this is about a mid-sized one's, enough to tell a model that fits from one that is far too big.
CONTEXT_BYTES_PER_TOKEN = 128 * 1024
# A share of the memory a model can count on: macOS lets the graphics side use about three quarters of it, and the
# rest is the computer's, Outlook's and the browser's.
USABLE_SHARE = 0.75
_THIS_COMPUTER = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def computer_memory() -> int:
    """This computer's memory in bytes, or 0 when it can't be told."""
    try:
        if os.name == "nt":
            import ctypes

            class _Memory(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), *[(name, ctypes.c_ulonglong) for name in (
                    "total", "free", "total_page", "free_page", "total_virtual", "free_virtual", "free_extended")]]

            memory = _Memory()
            memory.length = ctypes.sizeof(_Memory)
            return int(memory.total) if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)) else 0
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, ValueError):
        return 0


def memory_needed(size_bytes: int, context_tokens: int) -> int:
    """About what loading a model of ``size_bytes`` (its download) with ``context_tokens`` takes."""
    return size_bytes + max(0, context_tokens) * CONTEXT_BYTES_PER_TOKEN


def too_big(settings: Settings, status: ModelStatus) -> dict[str, str]:
    """The downloaded chat models too big to load on this computer with the context Setup asks for, each with why.
    Nothing when LM Studio doesn't give the sizes, the memory can't be read, or LM Studio is on another computer."""
    host = urlparse(status.base_url or settings.llm_base_url).hostname or ""
    memory = computer_memory()
    if not status.chat_sizes or not memory or host not in _THIS_COMPUTER:
        return {}
    found = {}
    for key, size in status.chat_sizes.items():
        most = status.chat_models.get(key, 0)
        tokens = min(settings.min_context_tokens, most) if settings.min_context_tokens and most else settings.min_context_tokens
        needed = memory_needed(size, tokens)
        if needed > USABLE_SHARE * memory:
            context = f" with a {tokens:,}-token context" if tokens else ""
            found[key] = (
                f"{key} needs about {round(needed / 1024**3)} GB of memory{context}, and this computer has "
                f"{round(memory / 1024**3)} GB in all, so LM Studio will likely run out of memory with it. CloseDesk "
                "only needs a small model: a 3B to 8B one is plenty and answers faster."
            )
    return found


def models_in_use(settings: Settings, *, status: ModelStatus | None = None) -> list[dict]:
    """One row per job: ``role``, ``model`` (its name, "" when none), ``state`` ("on", "fallback" for the older
    method, "off"), ``status`` (loaded, downloaded, or what happens instead) and ``note`` (why, for a fallback)."""
    status = status or check_model(settings)
    return [chat_row(status), reader_row(settings, status), search_row(settings, status), ocr_row()]
