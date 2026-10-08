"""Which model does which job, for Setup and the workspace's Settings: the model that answers questions, the one that
reads scanned pages (OvisOCR2 by default, the older method when it can't), the one that finds mail by meaning, and
the OCR engine that reads scans the moment they arrive."""

from __future__ import annotations

from typing import TYPE_CHECKING

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
        row["status"] = "Turned off (CONTROLLER_INBOX_LLM=false): answers come from looking things up."
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
    loaded = _loaded(status, model) or model in status.models
    return {"role": "Finds mail by meaning", "model": model, "state": "on", "note": "",
            "status": "Loaded." if loaded else "Downloaded: LM Studio loads it when mail is searched."}


def ocr_row() -> dict:
    from controller_inbox import ocr

    engine = ocr.engine_name()
    if engine:
        return {"role": "Reads scans as they arrive (OCR)", "model": engine, "state": "on", "note": "",
                "status": "On this computer: a scan's text is there in seconds, before the page reader runs."}
    return {"role": "Reads scans as they arrive (OCR)", "model": "", "state": "off", "note": "",
            "status": 'Not installed: scans wait for the page reader. Run pip install -e ".[ocr]" to add it.'}


def chat_choices(settings: Settings, status: ModelStatus) -> dict | None:
    """What Setup's chat model list offers: LM Studio's downloaded chat models (``models``), the one answering now
    (``current``, its key) and the loaded ones (``loaded``). ``pinned`` when the settings name a model, so loading
    another wouldn't change which one answers. None when the server can't load models when asked (not LM Studio)."""
    if status.mode == "off" or not status.lm_studio or not status.chat_models:
        return None
    return {
        "models": list(status.chat_models),
        "current": status.instances.get(status.model, (status.model, 0))[0],
        "loaded": {key for key, _context in status.instances.values()},
        "pinned": settings.llm_model not in {"", "local-model"},
    }


def models_in_use(settings: Settings, *, status: ModelStatus | None = None) -> list[dict]:
    """One row per job: ``role``, ``model`` (its name, "" when none), ``state`` ("on", "fallback" for the older
    method, "off"), ``status`` (loaded, downloaded, or what happens instead) and ``note`` (why, for a fallback)."""
    status = status or check_model(settings)
    return [chat_row(status), reader_row(settings, status), search_row(settings, status), ocr_row()]
