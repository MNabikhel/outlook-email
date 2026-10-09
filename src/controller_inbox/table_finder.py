"""Finding the tables on a page from its picture, with a small layout model that runs on this computer.

The Page tab outlines the tables on a page. On a scan, CloseDesk used to find them only where OCR's boxes line up in
columns, which misses a table whose columns don't line up cleanly. A layout model trained on document pages finds
tables by how they look instead.

The model is RapidAI's YOLOv8n "general6" layout model (from the RapidLayout project, Apache-2.0): 12 MB, about a
quarter of a second a page on a laptop's processor, run with ONNX Runtime, which the OCR add-on already installs.
Measured on 84 scanned pages it never saw (18 invoices, statements and finance schedules; 66 annual-report pages)
against the cells known to be in each table, it put 99.9% and 98% of those cells inside a table it found, where OCR's
boxes lining up found 47% and 60%; on 12 letters with no table it found none (README, "Finding tables on a page").
Seven layout models were compared; larger ones did no better.

The model isn't part of the download: it is fetched once (12 MB, checked against its SHA-256) the first time a page
is shown, into the data folder. Until it is there, or without ONNX Runtime, tables are found as before.
"""

from __future__ import annotations

import hashlib
import io
import logging
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from controller_inbox.config import Settings

log = logging.getLogger(__name__)

NAME = "yolov8n_layout_general6.onnx"
URL = "https://www.modelscope.cn/models/RapidAI/RapidLayout/resolve/v1.2.0/onnx/360/yolov8n_layout_general6.onnx"
SHA256 = "927b6edc268e896e6a170f7d78980591b408e04b3908f54d58eb69efd018c95"
LABELS = ("Text", "Title", "Figure", "Table", "Caption", "Equation")
TABLE = LABELS.index("Table")
SIDE = 640  # the model looks at the page squeezed to 640 x 640
# A table is kept from this score on (0.5 missed a few plain invoices' tables; 0.25 found them and nothing more on
# letters); two found tables overlapping this much are one.
SURE = 0.25
OVERLAP = 0.5
# What a page's tables were found with: the model and how sure it had to be. Tables kept from another are found again.
VERSION = f"{SHA256[:16]}-{SURE}"
# After a failed download, wait this long before trying again.
RETRY_SECONDS = 3600

_session = None
_session_lock = threading.Lock()
_fetch_lock = threading.Lock()
_fetching = False
_last_try = 0.0


def model_path(settings: Settings) -> Path:
    return settings.data_dir / "models" / NAME


def runtime() -> bool:
    """ONNX Runtime is installed (it comes with the OCR add-on)."""
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def ready(settings: Settings) -> bool:
    return runtime() and model_path(settings).is_file()


def fetch_later(settings: Settings) -> None:
    """Fetch the model in the background, once, when it isn't here yet; a failed fetch is tried again an hour on."""
    global _fetching, _last_try
    if model_path(settings).is_file() or not runtime():
        return
    with _fetch_lock:
        if _fetching or time.monotonic() - _last_try < RETRY_SECONDS and _last_try:
            return
        _fetching, _last_try = True, time.monotonic()

    def work() -> None:
        global _fetching
        try:
            _download(model_path(settings))
            log.info("Fetched the table finder (%s)", NAME)
        except Exception as exc:  # noqa: BLE001 - without it, tables are found as before
            log.warning("Couldn't fetch the table finder: %s", exc)
        finally:
            with _fetch_lock:
                _fetching = False

    threading.Thread(target=work, name="table-finder-fetch", daemon=True).start()


def _download(target: Path) -> None:
    _fetch(target)


def _fetch(target: Path) -> None:
    """The model from its pinned address, kept only when its SHA-256 is the one expected."""
    import httpx

    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(".part")
    digest = hashlib.sha256()
    with httpx.stream("GET", URL, follow_redirects=True, timeout=httpx.Timeout(60.0, connect=15.0)) as response:
        response.raise_for_status()
        with open(part, "wb") as out:
            for chunk in response.iter_bytes():
                digest.update(chunk)
                out.write(chunk)
    if digest.hexdigest() != SHA256:
        part.unlink(missing_ok=True)
        raise ValueError("the file fetched isn't the table finder expected (its SHA-256 differs)")
    part.replace(target)


def _model(settings: Settings):
    global _session
    with _session_lock:
        if _session is None:
            import onnxruntime

            options = onnxruntime.SessionOptions()
            options.log_severity_level = 3
            _session = onnxruntime.InferenceSession(str(model_path(settings)), options, providers=["CPUExecutionProvider"])
        return _session


def find(settings: Settings, png: bytes) -> list[dict] | None:
    """The tables on the page: [{x, y, w, h (fractions of the page), score}], top to bottom; None when the model
    isn't here (it is then fetched for next time) or couldn't run."""
    if not ready(settings):
        fetch_later(settings)
        return None
    try:
        return _find(_model(settings), png)
    except Exception:  # noqa: BLE001 - a page the model can't look at keeps the tables found as before
        log.exception("The table finder couldn't look at a page")
        return None


def _find(session, png: bytes) -> list[dict]:
    import numpy as np
    from PIL import Image

    with Image.open(io.BytesIO(png)) as image:
        picture = image.convert("RGB")
        width, height = picture.size
        squeezed = picture.resize((SIDE, SIDE), Image.BILINEAR)
    pixels = np.asarray(squeezed, dtype=np.float32)[:, :, ::-1] / 255.0  # as the model was run in its project: BGR
    batch = pixels.transpose(2, 0, 1)[np.newaxis, ...]
    output = session.run(None, {session.get_inputs()[0].name: batch})[0]
    predictions = np.squeeze(output).T  # one row per candidate: centre x, centre y, width, height, a score per label
    scores = predictions[:, 4:]
    best = scores.argmax(axis=1)
    keep = (best == TABLE) & (scores[:, TABLE] >= SURE)
    if not keep.any():
        return []
    boxes = predictions[keep, :4]
    chances = scores[keep, TABLE]
    cx, cy, w, h = (boxes[:, i] / SIDE for i in range(4))
    found = [
        {"x": float(max(x - bw / 2, 0.0)), "y": float(max(y - bh / 2, 0.0)), "w": float(min(bw, 1.0)), "h": float(min(bh, 1.0)), "score": float(score)}
        for x, y, bw, bh, score in zip(cx, cy, w, h, chances)
    ]
    kept: list[dict] = []
    for box in sorted(found, key=lambda item: -item["score"]):
        if all(_overlap(box, other) < OVERLAP for other in kept):
            kept.append(box)
    for box in kept:
        box["w"], box["h"] = min(box["w"], 1.0 - box["x"]), min(box["h"], 1.0 - box["y"])
        for key in ("x", "y", "w", "h", "score"):
            box[key] = round(box[key], 5)
    return sorted(kept, key=lambda item: (item["y"], item["x"]))


def _overlap(a: dict, b: dict) -> float:
    x0, y0 = max(a["x"], b["x"]), max(a["y"], b["y"])
    x1, y1 = min(a["x"] + a["w"], b["x"] + b["w"]), min(a["y"] + a["h"], b["y"] + b["h"])
    shared = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    whole = a["w"] * a["h"] + b["w"] * b["h"] - shared
    return shared / whole if whole > 0 else 0.0
