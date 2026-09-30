"""Search by meaning with a local embedding model, next to keyword search.

"When does the Northwind contract renew?" should find a file that says "the agreement
renews automatically on 31 December", even though "contract" isn't in it. LM Studio
serves embedding models on the same server as chat models and ships one
(nomic-embed-text), so this uses whichever embedding model it has. The overnight run
stores one vector per email and per file section; with no embedding model, search
stays keyword-only. Nothing leaves the computer.
"""

from __future__ import annotations

import hashlib
import math
import time
from array import array
from collections.abc import Callable
from typing import Any

import httpx

try:
    import numpy as np
except ImportError:
    np = None

from controller_inbox import documents
from controller_inbox.config import Settings
from controller_inbox.fraud import attachments_locked
from controller_inbox.local_llm import _headers
from controller_inbox.models import EmailRecord
from controller_inbox.store import Store

EMBED_CHARS = 1_500
BATCH = 16
MAX_PIECES = 250
# Embedding models score unrelated text well above zero (nomic-embed-text: about 0.45-0.65), so a
# hit must also stand out from the rest of the mail for this query and be close to the best one.
MIN_SCORE = 0.6
MIN_LIFT = 2.5
NEAR_TOP = 0.08
_CACHE_SECONDS = 60.0
_model_cache: dict[str, tuple[float, str]] = {}
_vector_cache: dict[tuple[str, str], tuple[str, list[str], Any]] = {}


def base_url(settings: Settings) -> str:
    return (settings.embedding_base_url or settings.llm_base_url).rstrip("/")


def embedding_model(settings: Settings) -> str:
    """The embedding model to use: the setting, else one the server has. "" when there is none."""
    chosen = settings.embedding_model.strip()
    if chosen.lower() == "off" or settings.llm_mode == "off":
        return ""
    if chosen:
        return chosen
    base = base_url(settings)
    cached = _model_cache.get(base)
    if cached and time.monotonic() - cached[0] < _CACHE_SECONDS:
        return cached[1]
    found = _find_model(settings, base)
    _model_cache[base] = (time.monotonic(), found)
    return found


def _find_model(settings: Settings, base: str) -> str:
    root = base[: -len("/v1")] if base.endswith("/v1") else base
    try:
        data = httpx.get(root + "/api/v1/models", headers=_headers(settings), timeout=2.0).json()
        models = [m for m in data.get("models", []) if isinstance(m, dict) and "embed" in str(m.get("type", "")).lower()]
        models.sort(key=lambda m: not m.get("loaded_instances"))
        if models and models[0].get("key"):
            return str(models[0]["key"])
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        pass
    try:
        data = httpx.get(base + "/models", headers=_headers(settings), timeout=2.0).json()
        ids = [str(m.get("id")) for m in data.get("data", []) if isinstance(m, dict) and "embed" in str(m.get("id", "")).lower()]
        return ids[0] if ids else ""
    except (httpx.HTTPError, ValueError, AttributeError, TypeError):
        return ""


def embed(settings: Settings, texts: list[str], *, query: bool = False) -> list[array] | None:
    """Unit-length vectors for ``texts``, or None when no embedding model answers."""
    model = embedding_model(settings)
    if not model or not texts:
        return None
    if "nomic" in model.lower():
        texts = [("search_query: " if query else "search_document: ") + text for text in texts]
    try:
        response = httpx.post(
            base_url(settings) + "/embeddings",
            json={"model": model, "input": texts},
            headers=_headers(settings),
            timeout=httpx.Timeout(max(settings.llm_timeout, 60.0), connect=5.0),
        )
        response.raise_for_status()
        rows = sorted(response.json()["data"], key=lambda row: row.get("index", 0))
        vectors = [_unit(row["embedding"]) for row in rows]
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None
    return vectors if len(vectors) == len(texts) and all(len(v) for v in vectors) else None


def _unit(values) -> array:
    vector = array("f", (float(x) for x in values))
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return array("f", (x / norm for x in vector))


def _items(email: EmailRecord) -> list[tuple[str, str]]:
    """(key, text) for the email and each section of its files. Files on flagged mail are left out."""
    head = f"{email.subject}\nFrom {email.sender_name or email.sender_email}"
    items = [(f"email:{email.id}", f"{head}\n{(email.body_text or '')[:EMBED_CHARS]}")]
    if attachments_locked(email):
        return items
    for att in email.attachments:
        text = att.extracted_text or ""
        if not text.strip():
            continue
        pieces = [
            (f"file:{att.id}:{part.label}" + (f":{start}" if start else ""), f"{att.filename} · {part.label}\n{part.text[start: start + EMBED_CHARS]}")
            for part in documents.split_parts(text)
            for start in range(0, max(len(part.text), 1), EMBED_CHARS)
        ]
        items += pieces[:MAX_PIECES]
    return items


def _key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def index_mail(
    store: Store,
    settings: Settings,
    *,
    limit: int = 5_000,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> int:
    """Store vectors for emails and file sections that don't have one for their current text. Returns how many were added."""
    model = embedding_model(settings)
    if not model:
        return 0
    have = store.embedding_keys(model)
    todo = []
    for email in store.list_emails(order="newest", limit=limit):
        for key, text in _items(email):
            if have.get(key) != _key(text):
                todo.append((key, email.id, text))
    added = 0
    for start in range(0, len(todo), BATCH):
        batch = todo[start: start + BATCH]
        if on_progress:
            on_progress(min(start + BATCH, len(todo)), len(todo), "")
        vectors = embed(settings, [text for _key_, _id, text in batch])
        if vectors is None:
            break
        store.save_embeddings(model, [(key, email_id, _key(text), v.tobytes()) for (key, email_id, text), v in zip(batch, vectors)])
        added += len(batch)
    return added


def search(store: Store, settings: Settings, query: str, *, limit: int = 5) -> list[EmailRecord]:
    """Emails whose text or files are closest in meaning to ``query``, best first. [] when there's no index or model."""
    if not query.strip() or not store.has_embeddings():
        return []
    model = embedding_model(settings)
    if not model:
        return []
    ids, vectors = _vectors(store, model)
    if not ids:
        return []
    asked = embed(settings, [query], query=True)
    if not asked:
        return []
    scores = _scores(asked[0], vectors)
    mean = sum(scores) / len(scores)
    spread = math.sqrt(sum((s - mean) ** 2 for s in scores) / len(scores))
    best: dict[str, float] = {}
    for email_id, score in zip(ids, scores):
        if score > best.get(email_id, -1.0):
            best[email_id] = score
    top = max(best.values())
    # One section among n can stand at most sqrt(n - 1) deviations out, so small mailboxes ask for less.
    lift = min(MIN_LIFT, 0.75 * math.sqrt(len(scores) - 1))
    ranked = sorted(
        (
            (score, email_id)
            for email_id, score in best.items()
            if score >= MIN_SCORE and score >= top - NEAR_TOP and (not spread or (score - mean) / spread >= lift)
        ),
        reverse=True,
    )
    found = []
    for _score, email_id in ranked:
        email = store.get_email(email_id)
        if email is not None:
            found.append(email)
        if len(found) == limit:
            break
    return found


def find_mail(store: Store, settings: Settings | None, query: str, terms: list[str], *, limit: int) -> tuple[list[EmailRecord], set[str]]:
    """Keyword and meaning search together, best first, and the ids only meaning found.

    An email with every word of the question leads. Otherwise the keyword hits share only some words
    ("team" in an audit report for "team trip in Portugal"), and a close match in meaning goes first.
    """
    by_words = store.search_ranked(terms, limit=limit) if terms else []
    exact = any(_has_all(email, terms) for email in by_words)
    if settings is None or not terms or (exact and len(by_words) >= limit):
        return by_words[:limit], set()
    seen = {email.id for email in by_words}
    by_meaning = [email for email in search(store, settings, query, limit=limit) if email.id not in seen]
    found = by_words + by_meaning if exact else by_meaning + by_words
    return found[:limit], {email.id for email in by_meaning}


def _has_all(email: EmailRecord, terms: list[str]) -> bool:
    text = "\n".join(
        [email.subject, email.sender_name, email.sender_email, email.summary or "", email.body_text or ""]
        + [f"{att.filename}\n{att.extracted_text or ''}" for att in email.attachments]
    ).lower()
    return all(term in text for term in terms)


def _vectors(store: Store, model: str) -> tuple[list[str], Any]:
    """Email ids and their vectors: a numpy matrix when numpy is installed (the OCR add-on brings it), else arrays."""
    version = store.embedding_version(model)
    cache_key = (str(store.path), model)
    cached = _vector_cache.get(cache_key)
    if cached and cached[0] == version:
        return cached[1], cached[2]
    ids: list[str] = []
    vectors: list[array] = []
    for email_id, blob in store.embedding_vectors(model):
        vector = array("f")
        vector.frombytes(blob)
        if vectors and len(vector) != len(vectors[0]):
            continue
        ids.append(email_id)
        vectors.append(vector)
    matrix: Any = vectors
    if np is not None and vectors:
        matrix = np.array(vectors, dtype=np.float32)
    _vector_cache[cache_key] = (version, ids, matrix)
    return ids, matrix


def _scores(query: array, vectors: Any) -> list[float]:
    if np is not None and not isinstance(vectors, list):
        if vectors.shape[1] != len(query):
            return [-1.0] * vectors.shape[0]
        return (vectors @ np.array(query, dtype=np.float32)).tolist()
    return [sum(a * b for a, b in zip(query, v)) if len(v) == len(query) else -1.0 for v in vectors]
