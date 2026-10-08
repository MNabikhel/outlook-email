"""Local model reader. LM Studio (Bionic) and Ollama speak this API.

Scripts already extracted text, amounts, dates, and file types. The model is
asked to decide the category, folder, importance, one-line summary, and action
items from a short, readable packet. Everything here is written for a small
model on a laptop:

- the prompt has a hard character budget, so a 4k-context model is not overrun;
- the loaded model is looked up once per run, not once per message;
- structured JSON output is requested when the server supports it, and loose
  replies (fences, prose around the JSON, trailing commas) are still parsed;
- when the server is down or keeps timing out, the run stops asking instead of
  waiting on every remaining message.
"""

from __future__ import annotations

import json
import re
import threading
import weakref
import time
from dataclasses import dataclass, field

import httpx

from controller_inbox.config import Settings
from controller_inbox.models import FOLDERS, DocumentType, Importance

SYSTEM = (
    "You are the reader for CloseDesk, a local inbox assistant for a busy manager. "
    "Scripts already pulled the text, amounts, dates, invoice numbers, and file types. "
    "Decide what this message is and whether the manager must act. "
    "Use only facts in the message. Never invent amounts, dates, names, or account numbers. "
    "important = needs a decision, a reply, an approval, a payment check, or a task. "
    "informational = worth knowing, no task (most meeting invites, FYIs, newsletters). "
    "reference = keep the file, no task (receipts, statements, automated notices). "
    "A payment-instruction change or a fraud warning is always important, and the only action is to verify by phone. "
    "The summary is one plain sentence a person can read in five seconds. "
    "Reply with one JSON object and nothing else."
)

REPLY_SHAPE = (
    '{"category":"<id>","folder":"important|informational|reference",'
    '"importance":"critical|high|medium|low","summary":"<one sentence>",'
    '"actions":[{"title":"<task>","due":"YYYY-MM-DD or null","priority":"high"}],'
    '"why":"<one sentence>"}'
)

_STATUS_TTL_SECONDS = 30.0
_status_cache: dict[str, tuple[float, "ModelStatus"]] = {}


@dataclass
class ModelStatus:
    mode: str
    reachable: bool
    models: list[str] = field(default_factory=list)
    model: str = ""
    base_url: str = ""
    error: str = ""
    loaded: list[str] = field(default_factory=list)
    reasoning: list[str] = field(default_factory=list)
    context_length: int = 0
    # LM Studio's model key for the loaded instance and the longest context it supports, for reloading it.
    key: str = ""
    max_context: int = 0
    # The loaded model can look at pictures (a page of a scanned PDF), not only read text.
    vision: bool = False
    # Every model on the server that can look at pictures: loaded ones first, then ones LM Studio has downloaded and
    # loads when asked (a small document reader beside the chat model). Each model's reasoning options by name.
    vision_models: list[str] = field(default_factory=list)
    reasoning_options: dict[str, list[str]] = field(default_factory=dict)
    # LM Studio answered its own model routes (it loads and unloads models when asked). Each loaded instance's
    # model key and the context it was loaded with.
    lm_studio: bool = False
    instances: dict[str, tuple[str, int]] = field(default_factory=dict)

    @property
    def active(self) -> bool:
        if self.mode == "off":
            return False
        return self.reachable and bool(self.model)

    def describe(self) -> str:
        if self.mode == "off":
            return "Local model is turned off (CONTROLLER_INBOX_LLM=false)."
        if self.active:
            return f"Local model ready: {self.model} at {self.base_url}"
        if self.reachable:
            return f"The server at {self.base_url} answered, but no model is loaded. Load one in LM Studio."
        return f"No local model answering at {self.base_url}. Start the LM Studio server. ({self.error})"

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "active": self.active,
            "reachable": self.reachable,
            "model": self.model,
            "models": self.models,
            "base_url": self.base_url,
            "error": self.error,
            "context_length": self.context_length,
            "vision": self.vision,
            "message": self.describe(),
        }


def check_model(settings: Settings, *, timeout: float = 2.0, use_cache: bool = True) -> ModelStatus:
    """Ask the local server which models are loaded. Cached briefly so pages stay fast."""
    base = settings.llm_base_url.rstrip("/")
    mode = settings.llm_mode
    if mode == "off":
        return ModelStatus(mode=mode, reachable=False, base_url=base)
    key = f"{base}|{settings.llm_model}"
    cached = _status_cache.get(key)
    if use_cache and cached and time.monotonic() - cached[0] < _STATUS_TTL_SECONDS:
        return ModelStatus(**{**cached[1].__dict__, "mode": mode})
    status = ModelStatus(mode=mode, reachable=False, base_url=base)
    try:
        response = httpx.get(base + "/models", headers=_headers(settings), timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        listed = payload.get("data", []) if isinstance(payload, dict) else []
        ids = [str(item.get("id")) for item in listed if isinstance(item, dict) and item.get("id")]
        status.reachable = True
        status.models = ids
        listing = _lm_studio_models(settings, base, timeout)
        loaded, reasoning, contexts, reloadable, seeing = (
            listing.loaded, listing.reasoning, listing.contexts, listing.reloadable, listing.seeing
        )
        status.loaded = loaded
        status.lm_studio = listing.route == "v1"
        status.model = _pick_model(settings.llm_model, ids, loaded)
        status.reasoning = reasoning.get(status.model, [])
        status.reasoning_options = reasoning
        status.context_length = contexts.get(status.model, 0)
        status.key, status.max_context = reloadable.get(status.model, ("", 0))
        status.instances = {instance: (reloadable.get(instance, (instance, 0))[0], contexts.get(instance, 0)) for instance in loaded}
        status.vision = status.model in seeing
        # By model key, so a model is listed once whether or not it is loaded. A downloaded one when LM Studio loads it
        # when asked (/v1/models lists every downloaded model with just-in-time loading on, the loaded ones only with
        # it off), or it is a document reader, which CloseDesk has LM Studio load (load_for_reading).
        loaded_keys = {key for key, _context in status.instances.values()}
        status.vision_models = list(dict.fromkeys([
            *[status.instances[m][0] for m in loaded if m in seeing],
            *[key for key in listing.downloaded if key in ids or key in loaded_keys or (status.lm_studio and document_reader(key))],
        ]))
        # llama.cpp reports the loaded context on the model itself (meta.n_ctx, a request's share of it), older
        # builds on /props only (below). LM Studio uses its own route.
        if not status.context_length:
            for item in listed:
                if isinstance(item, dict) and str(item.get("id")) == status.model:
                    meta = item.get("meta") if isinstance(item.get("meta"), dict) else {}
                    status.context_length = _int(meta.get("n_ctx"))
                    break
        if not listing.route and status.model:  # llama.cpp, or another server with one model
            props = _llama_cpp_props(settings, base, timeout)
            modalities = props.get("modalities")
            status.vision = isinstance(modalities, dict) and modalities.get("vision") is True
            status.vision_models = [status.model] if status.vision else []
            # Older llama.cpp builds put only n_ctx_train on the model; /props has the context each request gets.
            if not status.context_length and isinstance(props.get("default_generation_settings"), dict):
                status.context_length = _int(props["default_generation_settings"].get("n_ctx"))
    except (httpx.HTTPError, ValueError, AttributeError, TypeError) as exc:
        status.error = _short_error(exc)
    _status_cache[key] = (time.monotonic(), status)
    return status


@dataclass
class _Listing:
    route: str = ""  # LM Studio's own route that answered: "v1" (it loads a model when asked to), "v0", or none
    loaded: list[str] = field(default_factory=list)
    reasoning: dict[str, list[str]] = field(default_factory=dict)
    contexts: dict[str, int] = field(default_factory=dict)
    reloadable: dict[str, tuple[str, int]] = field(default_factory=dict)
    seeing: set[str] = field(default_factory=set)
    downloaded: list[str] = field(default_factory=list)


def _lm_studio_models(settings: Settings, base: str, timeout: float) -> _Listing:
    """Loaded models, their reasoning options, the context length each was loaded with, each one's model key
    and longest context, the loaded models that can look at pictures, and every downloaded one that can (by key:
    LM Studio loads it when a request names it).

    With just-in-time loading on, ``/v1/models`` lists every downloaded model, so
    picking from it can make LM Studio load a second, bigger model. Other servers
    don't have these routes; they get empty results.
    """
    root = base[: -len("/v1")] if base.endswith("/v1") else base
    for path in ("/api/v1/models", "/api/v0/models"):
        try:
            response = httpx.get(root + path, headers=_headers(settings), timeout=timeout)
            data = response.json() if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        loaded: list[str] = []
        reasoning: dict[str, list[str]] = {}
        contexts: dict[str, int] = {}
        reloadable: dict[str, tuple[str, int]] = {}
        seeing: set[str] = set()
        downloaded: list[str] = []
        if isinstance(data.get("models"), list):
            for item in data["models"]:
                if not isinstance(item, dict) or item.get("type") not in {"llm", "vlm"}:
                    continue
                caps = item.get("capabilities") if isinstance(item.get("capabilities"), dict) else {}
                allowed = caps.get("reasoning", {}).get("allowed_options") if isinstance(caps.get("reasoning"), dict) else None
                options = [str(option) for option in allowed] if isinstance(allowed, list) else []
                sees = caps.get("vision") is True or item.get("type") == "vlm"
                if item.get("key"):
                    reasoning[str(item["key"])] = options
                    if sees:
                        downloaded.append(str(item["key"]))
                for instance in item.get("loaded_instances") or []:
                    if isinstance(instance, dict) and instance.get("id"):
                        loaded.append(str(instance["id"]))
                        reasoning[str(instance["id"])] = options
                        config = instance.get("config") if isinstance(instance.get("config"), dict) else {}
                        contexts[str(instance["id"])] = _int(config.get("context_length"))
                        reloadable[str(instance["id"])] = (str(item.get("key") or instance["id"]), _int(item.get("max_context_length")))
                        if sees:
                            seeing.add(str(instance["id"]))
            return _Listing("v1", loaded, reasoning, contexts, reloadable, seeing, downloaded)
        if isinstance(data.get("data"), list):
            for item in data["data"]:
                if isinstance(item, dict) and item.get("id") and item.get("state") == "loaded" and item.get("type") in {"llm", "vlm"}:
                    loaded.append(str(item["id"]))
                    contexts[str(item["id"])] = _int(item.get("loaded_context_length") or item.get("max_context_length"))
                    if item.get("type") == "vlm":
                        seeing.add(str(item["id"]))
            for item in data["data"]:
                if isinstance(item, dict) and item.get("id") and item.get("type") == "vlm":
                    downloaded.append(str(item["id"]))
            return _Listing("v0", loaded, reasoning, contexts, reloadable, seeing, downloaded)
    return _Listing()


def _llama_cpp_props(settings: Settings, base: str, timeout: float) -> dict:
    """llama.cpp's server ``/props``: whether it was started with a vision projector (``--mmproj``, in
    ``modalities``) and the context each request gets (``default_generation_settings.n_ctx``). {} from other servers."""
    root = base[: -len("/v1")] if base.endswith("/v1") else base
    try:
        response = httpx.get(root + "/props", headers=_headers(settings), timeout=timeout)
        data = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _int(value) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def llm_active(settings: Settings) -> bool:
    """On: always try. Off: never. Auto: only when a model is answering right now."""
    if settings.llm_mode == "on":
        return True
    if settings.llm_mode == "off":
        return False
    return check_model(settings).active


def resolve_model(settings: Settings) -> str:
    """Use the configured model, or the one LM Studio currently has loaded."""
    status = check_model(settings, timeout=3, use_cache=False)
    return status.model or settings.llm_model


# Models made only for reading document pages (vision.READERS): LM Studio loads one beside the chat model to read
# scans, and it is never taken for the chat model.
DOCUMENT_READERS = ("ovisocr",)


def document_reader(model: str) -> bool:
    plain = re.sub(r"[^a-z0-9]", "", (model or "").lower())
    return any(name in plain for name in DOCUMENT_READERS)


def _pick_model(requested: str, ids: list[str], loaded: list[str] | None = None) -> str:
    if requested in ids:
        return requested
    chat = [item for item in loaded or [] if not document_reader(item)]
    if chat:
        return chat[0]
    if not ids:
        return "" if requested in {"", "local-model"} else requested
    others = [item for item in ids if not document_reader(item)]
    usable = [item for item in others if "embed" not in item.lower()]
    return (usable or others or [""])[0]


class ModelUnavailable(RuntimeError):
    pass


class EmptyReply(RuntimeError):
    """The server answered 200 but wrote no answer, usually because the model only reasoned."""


# Reasoning models (Qwen3.x, DeepSeek-R1, gpt-oss) think before answering. That
# needs far more than the usual token budget unless thinking can be turned off.
THINKING_ROOM = 2048
THINKING_TIMEOUT = 300.0
_RETRYABLE = {400, 404, 415, 422, 500, 501}
# A server that refuses a parameter says so with one of these; a 500 or 404 can be a busy or reloading server.
_REFUSES = {400, 415, 422, 501}
_reasoning_seen: set[str] = set()
_effort_rejected: set[str] = set()


@dataclass
class Reply:
    content: str
    reasoning: str = ""
    finish: str = ""

    def why_unusable(self) -> str:
        if not self.content and self.reasoning and self.finish == "length":
            return "the model used its whole reply budget thinking"
        if not self.content and self.reasoning:
            return "the model sent only its reasoning, no answer"
        if not self.content:
            return "the model sent an empty reply"
        snippet = " ".join(self.content.split())[:60]
        return f"reply was not the JSON shape: “{snippet}”"


def _reasoning_text(part: dict) -> str:
    """LM Studio calls it ``reasoning_content``; OpenRouter and newer builds ``reasoning``."""
    for key in ("reasoning_content", "reasoning"):
        value = part.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _reply_from(data) -> Reply:
    choice = _first_choice(data)
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    raw = message.get("content") if isinstance(message.get("content"), str) else ""
    reasoning = _reasoning_text(message)
    tagged = "".join(_THINK_RE.findall(raw))
    return Reply(
        content=strip_thinking(raw),
        reasoning=(reasoning + tagged).strip(),
        finish=str(choice.get("finish_reason") or ""),
    )


def reasoning_effort(settings: Settings, model: str) -> str | None:
    """The lightest ``reasoning_effort`` this model accepts, or None to leave it alone.

    LM Studio 0.4.8+ reports each model's options on ``/api/v1/models``; for
    older builds, a model is treated as a thinker once it has sent reasoning.
    """
    if model in _effort_rejected:
        return None
    status = check_model(settings)
    options = status.reasoning if status.model == model else status.reasoning_options.get(model, [])
    if "off" in options:
        return "none"
    if "low" in options:
        return "low"
    if model in _reasoning_seen:
        return "none"
    return None


def thinking_effort(settings: Settings, model: str) -> str | None:
    """The lightest ``reasoning_effort`` that turns thinking on, for a short call worth thinking through, or
    None to leave the model's own setting (a model that thinks by default goes on thinking)."""
    if model in _effort_rejected:
        return None
    status = check_model(settings)
    options = status.reasoning if status.model == model else status.reasoning_options.get(model, [])
    if "low" in options or "on" in options:
        return "low"
    return next((level for level in ("medium", "high") if level in options), None)


def _thinks(settings: Settings, model: str, effort: str | None) -> bool:
    """Whether replies need room for thinking. Models like DeepSeek-R1 only allow ``on``."""
    status = check_model(settings)
    options = status.reasoning if status.model == model else status.reasoning_options.get(model, [])
    return bool(effort) or model in _reasoning_seen or "on" in options


def _timeout(settings: Settings, budget: int) -> float:
    """A laptop writing 2,048 tokens of thinking can take minutes."""
    return max(settings.llm_timeout, THINKING_TIMEOUT) if budget >= THINKING_ROOM else settings.llm_timeout


def _post_chat(post, url: str, payload: dict, settings: Settings, **options) -> httpx.Response:
    """POST, dropping ``reasoning_effort`` if this server build refuses it.

    The model is only remembered as refusing ``reasoning_effort`` when the same request
    without it goes through. When that retry fails too, something else (``response_format``,
    ``tools``) was the problem: the first answer comes back and the payload keeps its effort.
    """
    response = post(url, json=payload, headers=_headers(settings), **options)
    if "reasoning_effort" in payload and response.status_code in _RETRYABLE:
        effort = payload.pop("reasoning_effort")
        retry = post(url, json=payload, headers=_headers(settings), **options)
        if retry.status_code < 400:
            # Remembered only when the server said no to the request itself, not when it was busy for a moment.
            if response.status_code in _REFUSES:
                _effort_rejected.add(str(payload.get("model")))
            return retry
        payload["reasoning_effort"] = effort
    return response


@dataclass
class ReaderStats:
    read: int = 0
    failed: int = 0
    seconds: float = 0.0
    stopped_reason: str = ""
    last_error: str = ""

    @property
    def average_seconds(self) -> float:
        return self.seconds / self.read if self.read else 0.0


class LocalReader:
    """One reader per run. Holds the resolved model and stops on a dead server."""

    def __init__(self, settings: Settings, *, model: str | None = None, client: httpx.Client | None = None):
        self.settings = settings
        self.model = model or resolve_model(settings)
        self.client = client or httpx.Client(timeout=settings.llm_timeout)
        # A client this reader made is closed with it (or when it is garbage-collected); a passed-in one is the caller's.
        self._closer = weakref.finalize(self, self.client.close) if client is None else None
        self.stats = ReaderStats()
        self._structured = True
        self._effort = reasoning_effort(settings, self.model)
        self._max_tokens = settings.llm_max_tokens
        if _thinks(settings, self.model, self._effort):
            self._max_tokens = max(self._max_tokens, THINKING_ROOM)
        self._consecutive_failures = 0

    def close(self) -> None:
        if self._closer is not None:
            self._closer()

    def __enter__(self) -> "LocalReader":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    @property
    def stopped(self) -> bool:
        return bool(self.stats.stopped_reason)

    def read(self, packet: dict) -> dict | None:
        if self.stopped:
            return None
        started = time.monotonic()
        prompt = build_prompt(packet, budget=self.settings.llm_max_prompt_chars)
        try:
            reply = self._complete(prompt)
            parsed = _find_reading(reply)
            if parsed is None and self._adapt(reply):
                reply = self._complete(prompt)
                parsed = _find_reading(reply)
        except ModelUnavailable as exc:
            self.stats.stopped_reason = str(exc)
            return None
        except httpx.HTTPError as exc:
            return self._failed(_short_error(exc))
        if parsed is None:
            return self._failed(reply.why_unusable())
        self._consecutive_failures = 0
        self.stats.read += 1
        self.stats.seconds += time.monotonic() - started
        return parsed

    def _failed(self, reason: str) -> None:
        self.stats.failed += 1
        self.stats.last_error = reason
        self._consecutive_failures += 1
        if self._consecutive_failures >= 3:
            self.stats.stopped_reason = f"three messages in a row failed ({reason})"
        return None

    def _adapt(self, reply: Reply) -> bool:
        """After a reasoning model's unusable reply: settings for one more try, if any changed."""
        if reply.content and not reply.reasoning:
            return False
        if reply.reasoning:
            _reasoning_seen.add(self.model)
        changed = False
        if self._effort is None and self.model not in _effort_rejected:
            self._effort, changed = "none", True
        if self._max_tokens < THINKING_ROOM:
            self._max_tokens, changed = THINKING_ROOM, True
        if self._structured and not reply.content:
            # LM Studio can route schema-constrained output into the reasoning
            # stream and leave the answer empty; plain prompts answer normally.
            self._structured, changed = False, True
        return changed

    def _complete(self, user: str) -> Reply:
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": self._max_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user},
            ],
        }
        if self._structured:
            payload["response_format"] = {"type": "json_schema", "json_schema": _schema()}
        if self._effort:
            payload["reasoning_effort"] = self._effort
        try:
            response = _post_chat(self.client.post, url, payload, self.settings, timeout=_timeout(self.settings, self._max_tokens))
            if "reasoning_effort" not in payload:
                self._effort = None
            if self._structured and response.status_code in _RETRYABLE:
                # Older LM Studio / Ollama builds reject response_format. Ask again, plain.
                self._structured = False
                payload.pop("response_format", None)
                response = _post_chat(
                    self.client.post, url, payload, self.settings, timeout=_timeout(self.settings, self._max_tokens)
                )
                if "reasoning_effort" not in payload:
                    self._effort = None
            response.raise_for_status()
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError) as exc:
            raise ModelUnavailable(f"the local model server stopped answering ({_short_error(exc)})") from exc
        except httpx.ReadTimeout as exc:
            if self._consecutive_failures >= 1:
                raise ModelUnavailable(
                    f"the model took longer than {_timeout(self.settings, self._max_tokens):.0f}s twice in a row"
                ) from exc
            raise
        try:
            data = response.json()
        except ValueError as exc:
            raise httpx.DecodingError(f"unexpected reply: {exc}") from exc
        if not _first_choice(data):
            raise httpx.DecodingError("unexpected reply: no choices")
        reply = _reply_from(data)
        if reply.reasoning:
            _reasoning_seen.add(self.model)
        return reply


def _find_reading(reply: Reply) -> dict | None:
    """The reading JSON from the answer, or from the reasoning when LM Studio put it there."""
    for text in (reply.content, reply.reasoning):
        found = None
        for candidate in _json_objects(text):
            if "category" in candidate or "folder" in candidate:
                found = candidate
        if found is not None:
            return found
    return None


def _json_objects(content: str):
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", (content or "").strip(), flags=re.I | re.M)
    start = text.find("{")
    while start != -1:
        chunk = _balanced(text, start)
        data = None
        if chunk:
            for candidate in (chunk, re.sub(r",\s*([}\]])", r"\1", chunk)):
                try:
                    data = json.loads(candidate)
                    break
                except json.JSONDecodeError:
                    continue
        if isinstance(data, dict):
            yield data
            start = text.find("{", start + len(chunk))
        else:
            start = text.find("{", start + 1)


def _chat_request(
    settings: Settings, messages: list[dict], max_tokens: int, *, stream: bool, model: str | None = None
) -> tuple[str, dict]:
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model or check_model(settings).model or settings.llm_model,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "messages": messages,
        "stream": stream,
    }
    return url, payload


def _first_choice(data) -> dict:
    """The first ``choices`` entry, or ``{}`` when the server sent something else."""
    choices = data.get("choices") if isinstance(data, dict) else None
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return choices[0]
    return {}


_THINK_OPEN, _THINK_CLOSE = "<think>", "</think>"
_THINK_RE = re.compile(r"<think>.*?(?:</think>|\Z)", re.S)


def strip_thinking(text: str) -> str:
    """Reasoning models (Qwen3, DeepSeek-R1) put their scratch work in <think> tags. A template that opens the
    tag in the prompt leaves only its close in the reply: everything before a "</think>" with no "<think>"
    before it is reasoning too."""
    text = text or ""
    if _THINK_CLOSE in text and _THINK_OPEN not in text.split(_THINK_CLOSE, 1)[0]:
        text = text.split(_THINK_CLOSE, 1)[1]
    return _THINK_RE.sub("", text).strip()


class ThinkFilter:
    """``strip_thinking`` for a stream, where a tag can arrive split across pieces."""

    def __init__(self) -> None:
        self.buffer = ""
        self.inside = False
        self.started = False
        self.saw = False

    def feed(self, piece: str) -> str:
        self.buffer += piece
        out: list[str] = []
        while True:
            if self.inside:
                cut = self.buffer.find(_THINK_CLOSE)
                if cut < 0:
                    self.buffer = self.buffer[-(len(_THINK_CLOSE) - 1) :]
                    break
                self.buffer = self.buffer[cut + len(_THINK_CLOSE) :]
                self.inside = False
                continue
            cut = self.buffer.find(_THINK_OPEN)
            if cut < 0:
                keep = next((k for k in range(len(_THINK_OPEN) - 1, 0, -1) if self.buffer.endswith(_THINK_OPEN[:k])), 0)
                out.append(self.buffer[: len(self.buffer) - keep])
                self.buffer = self.buffer[len(self.buffer) - keep :]
                break
            out.append(self.buffer[:cut])
            self.buffer = self.buffer[cut + len(_THINK_OPEN) :]
            self.inside = self.saw = True
        return self._lead("".join(out))

    def flush(self) -> str:
        rest = "" if self.inside else self.buffer
        self.buffer = ""
        return self._lead(rest)

    def _lead(self, text: str) -> str:
        if not self.started:
            text = text.lstrip()
            self.started = bool(text)
        return text


def _chat_plan(
    settings: Settings, max_tokens: int, *, think: bool = False, model: str | None = None
) -> tuple[str, str | None, int]:
    """Model, reasoning effort, and token budget for a chat or draft request. ``think``: let a model that can
    think do so, with room for it, instead of turning thinking down. ``model``: another model than the chat one
    (the one that reads pages)."""
    model = model or check_model(settings).model or settings.llm_model
    effort = thinking_effort(settings, model) if think else reasoning_effort(settings, model)
    return model, effort, max(max_tokens, THINKING_ROOM) if _thinks(settings, model, effort) else max_tokens


def reply_budget(settings: Settings, max_tokens: int) -> int:
    """Tokens a chat reply may use, including room to think on reasoning models."""
    return _chat_plan(settings, max_tokens)[2]


def _retry_plan(model: str, effort: str | None, budget: int, reply: Reply) -> tuple[str | None, int] | None:
    """A reasoning model wrote no answer: turn thinking down and give it room, once."""
    if reply.content or not reply.reasoning:
        return None
    _reasoning_seen.add(model)
    new_effort = effort or (None if model in _effort_rejected else "none")
    new_budget = max(budget, THINKING_ROOM)
    if (new_effort, new_budget) == (effort, budget):
        return None
    return new_effort, new_budget


def complete_text(
    settings: Settings, messages: list[dict], *, max_tokens: int = 400, temperature: float | None = None, think: bool = False
) -> str:
    """One plain-text answer. Raises ``httpx.HTTPError`` or ``EmptyReply`` when there is none. ``temperature``:
    other than the usual 0.2, for a second, differently worded try; ``think``: see ``_chat_plan``."""
    model, effort, budget = _chat_plan(settings, max_tokens, think=think)
    for _attempt in range(2):
        url, payload = _chat_request(settings, messages, budget, stream=False)
        if temperature is not None:
            payload["temperature"] = temperature
        if effort:
            payload["reasoning_effort"] = effort
        response = _post_chat(httpx.post, url, payload, settings, timeout=_timeout(settings, budget))
        _raise_for(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise httpx.DecodingError(f"unexpected reply: {exc}") from exc
        reply = _reply_from(data)
        if reply.content:
            return reply.content
        plan = _retry_plan(model, payload.get("reasoning_effort"), budget, reply)
        if plan is None:
            break
        effort, budget = plan
    raise EmptyReply(reply.why_unusable())


def stream_text(
    settings: Settings,
    messages: list[dict],
    *,
    max_tokens: int = 500,
    wait: float | None = None,
    temperature: float | None = None,
    finished: dict | None = None,
    sampling: dict | None = None,
    model: str | None = None,
):
    """Yield the answer as it is written. Servers that ignore ``stream`` send it in one piece.

    Raises ``EmptyReply`` when the model wrote nothing, so callers never show a blank answer. ``wait``: how long
    the model may go quiet (looking at a picture first can take minutes on a laptop); ``temperature``: other than
    the usual 0.2; ``finished``: given a dict, its "reason" is set to why the reply ended ("length": cut off) and
    "thought" to whether the model reasoned first; ``sampling``: more settings sent as they are (top_p, top_k,
    presence_penalty); ``model``: another model than the chat one (LM Studio loads it when asked).
    """
    model, effort, budget = _chat_plan(settings, max_tokens, model=model)
    for _attempt in range(2):
        reply = Reply(content="")
        for piece in _stream_once(
            settings, messages, budget, effort, reply, wait=wait, temperature=temperature, sampling=sampling, model=model
        ):
            reply.content += piece
            yield piece
        if finished is not None:
            finished["reason"], finished["thought"] = reply.finish, bool(reply.reasoning)
        if reply.content:
            return
        plan = _retry_plan(model, effort if model not in _effort_rejected else None, budget, reply)
        if plan is None:
            break
        effort, budget = plan
    raise EmptyReply(reply.why_unusable())


def _stream_once(
    settings: Settings,
    messages: list[dict],
    budget: int,
    effort: str | None,
    reply: Reply,
    *,
    rejected_effort: str = "",
    wait: float | None = None,
    temperature: float | None = None,
    sampling: dict | None = None,
    model: str | None = None,
):
    """``rejected_effort`` names the model whose ``reasoning_effort`` the last try sent; it is
    remembered as refusing it only if this try, without it, is accepted."""
    url, payload = _chat_request(settings, messages, budget, stream=True, model=model)
    if effort:
        payload["reasoning_effort"] = effort
    if temperature is not None:
        payload["temperature"] = temperature
    if sampling:
        payload.update(sampling)
    timeout = httpx.Timeout(max(settings.llm_timeout, wait or 0.0), connect=5.0)
    with httpx.stream("POST", url, json=payload, headers=_headers(settings), timeout=timeout) as response:
        if effort and response.status_code in _RETRYABLE:
            rejected = True
            refused = response.status_code in _REFUSES
        else:
            rejected = False
            if response.status_code >= 400:
                response.read()
            _raise_for(response)
            if rejected_effort:
                _effort_rejected.add(rejected_effort)
            yield from _stream_pieces(response, reply)
    if rejected:
        # Only a refusal is remembered; a busy server (500) is just tried again without the effort.
        yield from _stream_once(
            settings, messages, budget, None, reply,
            rejected_effort=str(payload.get("model")) if refused else "", wait=wait, temperature=temperature, sampling=sampling,
            model=model,
        )


def _stream_pieces(response: httpx.Response, reply: Reply):
    if "text/event-stream" not in response.headers.get("content-type", ""):
        try:
            data = json.loads(response.read() or b"{}")
        except ValueError as exc:
            raise httpx.DecodingError(f"unexpected reply: {exc}") from exc
        whole = _reply_from(data)
        reply.reasoning, reply.finish = whole.reasoning, whole.finish
        if whole.content:
            yield whole.content
        return
    thinking = ThinkFilter()
    for line in response.iter_lines():
        line = line.strip()
        if line.startswith("error:"):
            _stream_error(line[6:].strip())
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if chunk == "[DONE]":
            break
        try:
            data = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("error") and not data.get("choices"):
            _stream_error(data["error"])
        choice = _first_choice(data)
        part = choice.get("delta") if isinstance(choice.get("delta"), dict) else choice.get("message")
        part = part if isinstance(part, dict) else {}
        if _reasoning_text(part):
            reply.reasoning += _reasoning_text(part)
        if choice.get("finish_reason"):
            reply.finish = str(choice["finish_reason"])
        piece = thinking.feed(part.get("content") if isinstance(part.get("content"), str) else "")
        if thinking.saw and not reply.reasoning:
            reply.reasoning = "<think>"
        if piece:
            yield piece
    rest = thinking.flush()
    if rest:
        yield rest


def _stream_error(error) -> None:
    """A failure the server wrote into the stream after answering 200: llama.cpp sends an "error:" line, LM Studio
    and OpenAI a chunk with "error". Raised, so a reply cut short isn't taken for the whole answer and a prompt too
    long for the context is retried shorter."""
    if isinstance(error, str):
        try:
            error = json.loads(error)
        except ValueError:
            pass
    if isinstance(error, dict):
        error = error.get("error") or error
    if isinstance(error, dict):
        error = error.get("message") or json.dumps(error)
    text = " ".join(str(error or "unknown error").split())[:200]
    if _OVERFLOW_RE.search(text):
        raise ContextOverflow(text)
    raise httpx.HTTPError(f"the model server stopped with an error ({text})")


class ContextOverflow(RuntimeError):
    """The prompt did not fit the context length the model was loaded with."""


class ToolsUnsupported(RuntimeError):
    """This server or model refused the ``tools`` parameter."""


_OVERFLOW_RE = re.compile(
    r"context (?:length|window|size|overflow)|n_ctx|too many tokens|prompt is too long|"
    r"exceeds? (?:the )?(?:model'?s? )?(?:maximum |max )?(?:context|token)",
    re.I,
)
_tools_rejected: set[str] = set()
_context_raised: set[str] = set()
_context_lock = threading.Lock()


def _raise_for(response: httpx.Response) -> None:
    if response.status_code >= 400:
        text = response.text[:600]
        if _OVERFLOW_RE.search(text):
            raise ContextOverflow(" ".join(text.split())[:200])
    response.raise_for_status()


@dataclass
class ToolReply:
    content: str
    calls: list[dict] = field(default_factory=list)
    reasoning: str = ""
    finish: str = ""


def context_length(settings: Settings) -> int:
    """Tokens the loaded model can take (LM Studio reports it), or the Setup value, or 0 when unknown."""
    return check_model(settings).context_length or settings.chat_context_tokens


def context_target(settings: Settings, status: ModelStatus | None = None) -> int:
    """The context the model should have: ``min_context_tokens``, but no more than the model supports."""
    status = status or check_model(settings)
    want = settings.min_context_tokens
    return min(want, status.max_context) if want and status.max_context else want


def needs_more_context(settings: Settings) -> bool:
    """LM Studio loaded the model with less than the minimum and CloseDesk hasn't tried to raise it yet."""
    status = check_model(settings)
    return bool(
        status.active
        and status.key
        and 0 < status.context_length < context_target(settings, status)
        and status.model not in _context_raised
    )


def set_min_context(settings: Settings, tokens: int) -> None:
    """Change the minimum while running; a model that couldn't be raised before is tried again at the new size."""
    settings.min_context_tokens = tokens
    _context_raised.clear()


def ensure_context(settings: Settings) -> str:
    """Reload LM Studio's model with at least ``min_context_tokens`` when it was loaded with less.

    Tried once per loaded instance, so a machine without the memory for it isn't asked again and again.
    If the longer load fails, the model is loaded back as it was. Returns what happened ("" when nothing did).
    """
    with _context_lock:
        if not needs_more_context(settings):
            return ""
        return _reload_with_context(settings)


def _reload_with_context(settings: Settings) -> str:
    status = check_model(settings)
    want = context_target(settings, status)
    _context_raised.add(status.model)
    root = status.base_url[: -len("/v1")] if status.base_url.endswith("/v1") else status.base_url
    timeout = httpx.Timeout(max(settings.llm_timeout, 120.0), connect=5.0)
    try:
        httpx.post(root + "/api/v1/models/unload", json={"instance_id": status.model}, headers=_headers(settings), timeout=timeout).raise_for_status()
    except httpx.HTTPError as exc:
        return f"Couldn't reload {status.key} with a longer context ({_short_error(exc)})."
    errors = []
    for size in (want, status.context_length):
        try:
            reply = httpx.post(
                root + "/api/v1/models/load", json={"model": status.key, "context_length": size}, headers=_headers(settings), timeout=timeout
            )
            reply.raise_for_status()
        except httpx.HTTPError as exc:
            errors.append(_short_error(exc))
            continue
        break
    _status_cache.clear()
    if not errors:
        most = " (the most it supports)" if want < settings.min_context_tokens else ""
        return f"Reloaded {status.key} in LM Studio with a {want:,}-token context{most} so whole attachments fit."
    if len(errors) == 1:
        return f"LM Studio couldn't load {status.key} with a {want:,}-token context ({errors[0]}), so it stays at {status.context_length:,}."
    return f"LM Studio couldn't reload {status.key} ({errors[-1]}). Load it again in LM Studio."


# Models LM Studio couldn't load to read pages: (when, what it said). Not asked again for a while, or until Setup is
# saved, so a machine without the memory isn't asked on every page.
_reader_failed: dict[str, tuple[float, str]] = {}
READER_RETRY_SECONDS = 1800.0


def load_for_reading(settings: Settings, model: str, context: int) -> str:
    """Have LM Studio load ``model`` (a downloaded model's key) with at least ``context`` tokens before pages are
    read with it. A model LM Studio loads on its own when first asked gets its default context, which can be shorter
    than a page and its reading. Checked before every read, so a reader LM Studio unloaded since is loaded again; one
    loaded too short is loaded again longer, the short one unloaded only once the longer one is in. The chat model is
    left as it is. Returns what went wrong, or ""."""
    if not context:
        return ""
    with _context_lock:
        status = check_model(settings, use_cache=False)
        if not status.lm_studio:
            return ""
        mine = [(size, name) for name, (key, size) in status.instances.items() if model in (name, key)]
        size, instance = max(mine) if mine else (0, "")
        if instance and (instance == status.model or not size or size >= context):
            return ""
        failed = _reader_failed.get(model)
        if failed and time.monotonic() - failed[0] < READER_RETRY_SECONDS:
            return failed[1]
        root = status.base_url[: -len("/v1")] if status.base_url.endswith("/v1") else status.base_url
        timeout = httpx.Timeout(max(settings.llm_timeout, 120.0), connect=5.0)
        key = status.instances[instance][0] if instance else model
        try:
            httpx.post(
                root + "/api/v1/models/load", json={"model": key, "context_length": context}, headers=_headers(settings), timeout=timeout
            ).raise_for_status()
        except httpx.HTTPError as exc:
            problem = f"LM Studio couldn't load {key} with a {context:,}-token context ({_short_error(exc)})."
            _reader_failed[model] = (time.monotonic(), problem)
            return problem
        finally:
            _status_cache.clear()
        _reader_failed.pop(model, None)
        if instance:  # the shorter one, now that the longer one is loaded
            try:
                httpx.post(root + "/api/v1/models/unload", json={"instance_id": instance}, headers=_headers(settings), timeout=timeout)
            except httpx.HTTPError:
                pass
        return ""


def forget_reader_failures() -> None:
    """Setup was saved: a model LM Studio couldn't load is asked again (memory may have been freed)."""
    _reader_failed.clear()


def chat_with_tools(settings: Settings, messages: list[dict], tools: list[dict], *, max_tokens: int = 500) -> ToolReply:
    """One non-streamed turn that may ask for tools. Raises ``ToolsUnsupported`` when tools are refused."""
    model, effort, budget = _chat_plan(settings, max_tokens)
    if model in _tools_rejected:
        raise ToolsUnsupported(model)
    reply = ToolReply(content="")
    for _attempt in range(2):
        url, payload = _chat_request(settings, messages, budget, stream=False)
        payload["tools"] = tools
        if effort:
            payload["reasoning_effort"] = effort
        response = _post_chat(httpx.post, url, payload, settings, timeout=_timeout(settings, budget))
        text = response.text[:600]
        if response.status_code in _RETRYABLE and re.search(r"\btools?\b|function", text, re.I) and not _OVERFLOW_RE.search(text):
            # This question goes on without tools. The model is only remembered as having none when the server
            # refused them; a tool call the model wrote badly ("error parsing tool call") is a one-off.
            if response.status_code in _REFUSES and not re.search(r"\bpars(?:e|ing)\b|invalid json|malformed", text, re.I):
                _tools_rejected.add(model)
            raise ToolsUnsupported(" ".join(text.split())[:200])
        _raise_for(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise httpx.DecodingError(f"unexpected reply: {exc}") from exc
        reply = _tool_reply(data)
        if reply.content or reply.calls:
            return reply
        plan = _retry_plan(model, payload.get("reasoning_effort"), budget, Reply(reply.content, reply.reasoning, reply.finish))
        if plan is None:
            break
        effort, budget = plan
    raise EmptyReply(Reply(reply.content, reply.reasoning, reply.finish).why_unusable())


_TEXT_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*(?:</tool_call>|\Z)", re.S)


def _tool_reply(data) -> ToolReply:
    choice = _first_choice(data)
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    base = _reply_from(data)
    calls = []
    for index, item in enumerate(message.get("tool_calls") or []):
        function = item.get("function") if isinstance(item, dict) else None
        if not isinstance(function, dict) or not function.get("name"):
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            arguments = parse_json_object(arguments) or {}
        calls.append({"id": str(item.get("id") or f"call_{index}"), "name": str(function["name"]), "arguments": arguments or {}})
    content = base.content
    if not calls and "<tool_call>" in content:
        # Some chat templates leave the call in the text instead of tool_calls.
        for index, raw in enumerate(_TEXT_CALL_RE.findall(content)):
            parsed = parse_json_object(raw) or {}
            if parsed.get("name"):
                arguments = parsed.get("arguments") or parsed.get("parameters") or {}
                if isinstance(arguments, str):
                    arguments = parse_json_object(arguments) or {}
                calls.append({"id": f"call_{index}", "name": str(parsed["name"]), "arguments": arguments})
        content = _TEXT_CALL_RE.sub("", content).strip() if calls else content
    return ToolReply(content=content, calls=calls, reasoning=base.reasoning, finish=base.finish)


def build_prompt(packet: dict, *, budget: int = 6000) -> str:
    """A short, plain-text packet. Fixed parts first, then body and attachments share what is left."""
    corrections = packet.get("saved_corrections_for_sender") or []
    extracted = packet.get("extracted") or {}
    draft = packet.get("script_draft") or {}
    facts = _facts_line(extracted)
    head = [
        "Allowed categories: " + ", ".join(item.value for item in DocumentType),
        "Allowed folders: " + ", ".join(FOLDERS),
        "Allowed importance: " + ", ".join(item.value for item in Importance),
    ]
    if corrections:
        head.append("The manager corrected this sender before. Follow it unless this is a new payment-instruction change:")
        head += [f"- {row.get('category')}: {row.get('reason')}" for row in corrections[:3]]
    head += [
        "",
        f"From: {packet.get('sender_name') or ''} <{packet.get('sender_email') or ''}>",
        f"Received: {str(packet.get('received_at') or '')[:16]}",
        f"Subject: {packet.get('subject') or ''}",
        f"Facts the scripts found: {facts}",
        "Script guess (a hint, you decide): "
        f"category={draft.get('category')} folder={draft.get('folder')} importance={draft.get('importance')}"
        + (f" flags={','.join(draft.get('flags') or [])}" if draft.get("flags") else ""),
    ]
    tail = ["", "Reply with JSON only, exactly this shape:", REPLY_SHAPE]
    fixed = len("\n".join(head)) + len("\n".join(tail)) + 40
    room = max(600, budget - fixed)

    attachments = [att for att in (packet.get("attachments") or []) if isinstance(att, dict)][:3]
    body_room = room if not attachments else int(room * 0.6)
    att_room = room - body_room
    lines = head + ["", "Body:", _clip(packet.get("body") or "(empty)", body_room)]
    if attachments:
        lines += ["", "Attachments:"]
        per = max(150, att_room // len(attachments))
        for att in attachments:
            text = att.get("text") or att.get("note") or "(no text)"
            lines.append(f"- {att.get('filename')} ({att.get('script_type')}): {_clip(text, per)}")
    elif packet.get("filenames"):
        lines += ["", "Files: " + ", ".join(packet["filenames"][:6])]
    return "\n".join(lines + tail)


def parse_json_object(content: str) -> dict | None:
    """Find the first JSON object in a model reply. Tolerates fences, prose, and trailing commas."""
    return next(_json_objects(content), None)


def _balanced(text: str, start: int) -> str | None:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _facts_line(extracted: dict) -> str:
    bits = []
    if extracted.get("invoice_numbers"):
        bits.append("invoices " + ", ".join(map(str, extracted["invoice_numbers"][:4])))
    if extracted.get("po_numbers"):
        bits.append("POs " + ", ".join(map(str, extracted["po_numbers"][:4])))
    if extracted.get("amounts"):
        bits.append("amounts " + ", ".join(f"${float(a):,.2f}" for a in extracted["amounts"][:5]))
    if extracted.get("due_dates"):
        bits.append("dates " + ", ".join(map(str, extracted["due_dates"][:4])))
    if extracted.get("account_last4"):
        bits.append("account last-4 " + ", ".join(map(str, extracted["account_last4"][:2])))
    return "; ".join(bits) or "none"


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"[ \t]+", " ", str(text)).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut + " …[cut]"


def _schema() -> dict:
    return {
        "name": "closedesk_reading",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["category", "folder", "importance", "summary", "actions", "why"],
            "properties": {
                "category": {"type": "string", "enum": [item.value for item in DocumentType]},
                "folder": {"type": "string", "enum": list(FOLDERS)},
                "importance": {"type": "string", "enum": [item.value for item in Importance]},
                "summary": {"type": "string"},
                "why": {"type": "string"},
                "actions": {
                    "type": "array",
                    "maxItems": 4,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["title", "due", "priority"],
                        "properties": {
                            "title": {"type": "string"},
                            "due": {"type": ["string", "null"]},
                            "priority": {"type": "string", "enum": [item.value for item in Importance]},
                        },
                    },
                },
            },
        },
    }


def _headers(settings: Settings) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    key = settings.llm_api_key
    if not key and settings.openai_api_key and "openai.com" in settings.llm_base_url:
        key = settings.openai_api_key
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _short_error(exc: Exception) -> str:
    text = str(exc) or exc.__class__.__name__
    return text.splitlines()[0][:160]
