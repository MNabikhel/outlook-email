"""Text from scanned pages and pictures, read on this computer.

RapidOCR (``pip install -e ".[ocr]"``, which the launchers do) needs no separate program.
A Tesseract install with ``pytesseract`` is used when RapidOCR isn't there.
"""

from __future__ import annotations

import importlib.util
import io
import re
import shutil
import threading

MAX_CHARS = 20_000

_engine = None
_engine_lock = threading.Lock()


def engine_name() -> str:
    """Which OCR engine CloseDesk can use, or "" when none is installed."""
    if importlib.util.find_spec("rapidocr_onnxruntime") is not None:
        return "RapidOCR"
    if importlib.util.find_spec("pytesseract") is not None and shutil.which("tesseract"):
        return "Tesseract"
    return ""


def image_text(data: bytes) -> str:
    """The text in a picture, top to bottom, or "" when it has none or no OCR engine is installed."""
    name = engine_name()
    try:
        if name == "RapidOCR":
            return _rapid(data)
        if name == "Tesseract":
            return _tesseract(data)
    except Exception:
        return ""
    return ""


def _rapid(data: bytes) -> str:
    global _engine
    with _engine_lock:
        if _engine is None:
            from rapidocr_onnxruntime import RapidOCR

            _engine = RapidOCR()
        result, _elapsed = _engine(data)
    return "\n".join(_spaced(str(line[1])) for line in result or [])[:MAX_CHARS]


# On JPEG scans RapidOCR tends to drop spaces ("Duedate:15October2026"); put back the ones that are certain.
_JOINS = re.compile(r"(?<=[A-Za-z][a-z]{2})(?=\d)|(?<=\d)(?=[A-Z][a-z]{2})|(?<=[A-Za-z]:)(?=\w)")
# ...and to read the O of October as a zero once the space is gone: "150ctober", "200ct 2026".
_OCTOBER = re.compile(r"(?<![\d.,$])([1-9]|[12]\d|3[01])0(?=ctober|ct\.?\s*\d{4})")


def _spaced(line: str) -> str:
    return _JOINS.sub(" ", _OCTOBER.sub(r"\1 O", line))


def _tesseract(data: bytes) -> str:
    import pytesseract
    from PIL import Image

    text = pytesseract.image_to_string(Image.open(io.BytesIO(data)))
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())[:MAX_CHARS]
