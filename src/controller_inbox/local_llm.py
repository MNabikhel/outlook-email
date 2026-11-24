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
import socket
import sqlite3
import statistics
import threading
import weakref
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from http.cookiejar import CookieJar
from typing import TYPE_CHECKING

import httpx

from controller_inbox.config import Settings
from controller_inbox.models import FOLDERS, DocumentType, Importance
from controller_inbox.money import fmt_money

if TYPE_CHECKING:
    from controller_inbox.store import Store

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

# httpx's own one-off calls, each of which sets up a client of its own (about 50 ms of SSL setup, and a new
# connection): the calls here go through one client kept for the app's life instead, with the same timeouts and
# headers per request. A replaced function (a test's fake server) is called as before.
_ONE_OFF = {"get": httpx.get, "post": httpx.post, "stream": httpx.stream}
_shared: httpx.Client | None = None
_shared_lock = threading.Lock()


class _NoCookies(CookieJar):
    """A cookie jar that keeps none, as a one-off call's client throws its away."""

    def extract_cookies(self, response, request) -> None:
        pass

    def set_cookie(self, cookie) -> None:
        pass


def _no_cookies() -> CookieJar:
    return _NoCookies()


def shared_client() -> httpx.Client:
    """The client every model-server call goes through (thread-safe)."""
    global _shared
    if _shared is None:
        with _shared_lock:
            if _shared is None:
                # An idle connection is dropped well before LM Studio's own 5-second keep-alive closes it, so a
                # request never goes out on a connection the server is closing; a failed connect is tried once more.
                _shared = httpx.Client(
                    cookies=_no_cookies(),
                    limits=httpx.Limits(keepalive_expiry=2.0),
                    transport=httpx.HTTPTransport(retries=1),
                )
    return _shared


def http_method(name: str):
    """``httpx.get``/``post``/``stream`` through the shared client."""
    call = getattr(httpx, name)
    return call if call is not _ONE_OFF[name] else getattr(shared_client(), name)


class HangUp:
    """The connections to the model that one chat answer has open, to hang up on at once when the user stops it or
    closes the page (``now``). LM Studio stops reading a prompt only once the connection closes; until then the next
    question waits behind an answer nobody will read, and that wait is kept as the model's speed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._streams: list = []
        self.done = False

    def trace(self, event: str, info: dict) -> None:
        """httpx's trace of each request: notes the connection once it is made (hung up on at once if already done)."""
        if event != "connection.connect_tcp.complete":
            return
        stream = info.get("return_value")
        with self._lock:
            if not self.done:
                self._streams.append(stream)
                return
        _shut(stream)

    def now(self) -> None:
        with self._lock:
            self.done = True
            streams, self._streams = self._streams, []
        for stream in streams:
            _shut(stream)


def _shut(stream) -> None:
    """Close a connection under a request still waiting on it: the worker thread reading it is woken (closing the
    socket alone wouldn't wake it) and gets an error."""
    sock = stream.get_extra_info("socket") if stream is not None else None
    try:
        if sock is not None:
            sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass  # already closed


_hang_up: ContextVar[HangUp | None] = ContextVar("closedesk_hang_up", default=None)


@contextmanager
def hanging_up(handle: HangUp | None):
    """The chat answer's model calls made inside go out on connections of their own, hung up on by ``handle.now()``."""
    token = _hang_up.set(handle)
    try:
        yield
    finally:
        _hang_up.reset(token)


def _model_http(name: str):
    """``http_method`` for a call that asks the model for an answer: within ``hanging_up``, on a connection of its
    own that the user's Stop can close (the shared client's connections are shared with other calls)."""
    call = http_method(name)
    handle = _hang_up.get()
    if handle is None or call != getattr(shared_client(), name):
        return call
    trace = {"trace": handle.trace}
    if name == "stream":

        @contextmanager
        def stream(method: str, url: str, **options):
            with httpx.Client(cookies=_no_cookies(), transport=httpx.HTTPTransport(retries=1)) as client, client.stream(method, url, extensions=trace, **options) as response:
                yield response

        return stream

    def post(url: str, **options) -> httpx.Response:
        with httpx.Client(cookies=_no_cookies(), transport=httpx.HTTPTransport(retries=1)) as client:
            return client.post(url, extensions=trace, **options)

    return post


def lm_studio_root(base: str) -> str:
    """The server's root from its OpenAI-style base ("http://127.0.0.1:1234/v1" is "http://127.0.0.1:1234"): LM
    Studio's own API, which lists, loads and unloads models, is under ``/api/v1`` there."""
    return base[: -len("/v1")] if base.endswith("/v1") else base


_STATUS_TTL_SECONDS = 30.0
# How long a server that answered stays answering when a check then times out: it is busy (loading a model, reading
# a long prompt on a laptop), not gone.
BUSY_GRACE_SECONDS = 600.0
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
    server: str = ""  # "lm_studio", "ollama", "llama.cpp" or "" (another OpenAI-compatible server), for advice
    instances: dict[str, tuple[str, int]] = field(default_factory=dict)
    # LM Studio's downloaded models that can answer (not document readers) by key, with the longest context each
    # supports (0 when it doesn't say): the ones Setup offers to load.
    chat_models: dict[str, int] = field(default_factory=dict)
    # How big each of those is on disk, in bytes, when LM Studio says (it is about what loading it takes).
    chat_sizes: dict[str, int] = field(default_factory=dict)
    # How many parameters each has, as LM Studio puts it ("8B"), when it says.
    chat_params: dict[str, str] = field(default_factory=dict)
    # LM Studio's loaded embedding models, by instance id and by key (kept apart from ``instances``: none answers).
    embeddings: set[str] = field(default_factory=set)

    @property
    def active(self) -> bool:
        if self.mode == "off":
            return False
        return self.reachable and bool(self.model)

    def describe(self) -> str:
        if self.mode == "off":
            return "The local model is turned off in CloseDesk's settings file (.env)."
        if self.active and self.lm_studio and not loaded_now(self, self.model):
            # The model used last, downloaded but unloaded (LM Studio's idle unload, a restart): not "ready".
            return f"Local model: {self.model} at {self.base_url}. It isn't loaded now: LM Studio loads it when you ask something."
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
        response = http_method("get")(base + "/models", headers=auth_headers(settings), timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        listed = payload.get("data", []) if isinstance(payload, dict) else []
        ids = [str(item.get("id")) for item in listed if isinstance(item, dict) and item.get("id")]
        status.reachable = True
        status.models = ids
        listing = _lm_studio_models(settings, base, timeout)
        if listing.busy and cached and cached[1].reachable and time.monotonic() - cached[0] < BUSY_GRACE_SECONDS:
            # LM Studio answered, then took too long to list its models: busy loading one or reading a long prompt.
            return ModelStatus(**{**cached[1].__dict__, "mode": mode})
        loaded, reasoning, contexts, reloadable, seeing = (
            listing.loaded, listing.reasoning, listing.contexts, listing.reloadable, listing.seeing
        )
        status.loaded = loaded
        status.lm_studio = listing.route == "v1"
        status.server = "lm_studio" if listing.route else ""
        # LM Studio (or a server that didn't say what it has loaded) lists every model it could load.
        knows_loaded = bool(listing.route) or listing.busy
        # A remembered model LM Studio just couldn't load isn't asked for again (LM Studio would try and fail again).
        remembered = remembered_model(settings)
        remembered = "" if load_failed_lately(remembered) else remembered
        status.model = _pick_model(
            settings.llm_model, ids, loaded, lm_studio=knows_loaded, remembered=remembered,
            reader=(settings.vision_model or "").strip(), keys={item: reloadable.get(item, (item, 0))[0] for item in loaded},
        )
        if listing.route and status.model in loaded:
            remember_model(settings, reloadable.get(status.model, (status.model, 0))[0])
        status.reasoning = reasoning.get(status.model, [])
        status.reasoning_options = reasoning
        status.context_length = contexts.get(status.model, 0)
        status.key, status.max_context = reloadable.get(status.model, ("", 0))
        status.instances = {instance: (reloadable.get(instance, (instance, 0))[0], contexts.get(instance, 0)) for instance in loaded}
        status.chat_models = listing.chat if status.lm_studio else {}
        status.chat_sizes = listing.sizes if status.lm_studio else {}
        status.chat_params = listing.params if status.lm_studio else {}
        status.embeddings = listing.embeddings
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
            if props:
                status.server = "llama.cpp"
        if not listing.route and status.model and not status.context_length:
            # Ollama cuts a prompt longer than the context it loaded the model with, from the start and without a
            # word: that length, not the one the model was trained with, is what a prompt has to fit.
            status.context_length = _ollama_context(settings, base, timeout, status.model)
            if status.context_length:
                status.server = "ollama"
    except (httpx.HTTPError, ValueError, AttributeError, TypeError) as exc:
        if isinstance(exc, httpx.TimeoutException) and cached and cached[1].reachable and time.monotonic() - cached[0] < BUSY_GRACE_SECONDS:
            # It answered a moment ago: a model server loading a model or reading a long prompt can take longer than
            # this check allows. It is busy, not gone, and the chat still uses it (the next check asks again).
            return ModelStatus(**{**cached[1].__dict__, "mode": mode})
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
    # No route answered because one took too long (LM Studio busy), not because the server hasn't got them.
    busy: bool = False
    chat: dict[str, int] = field(default_factory=dict)  # see ModelStatus.chat_models
    embeddings: set[str] = field(default_factory=set)  # see ModelStatus.embeddings
    sizes: dict[str, int] = field(default_factory=dict)  # see ModelStatus.chat_sizes
    params: dict[str, str] = field(default_factory=dict)  # see ModelStatus.chat_params


def _lm_studio_models(settings: Settings, base: str, timeout: float) -> _Listing:
    """Loaded models, their reasoning options, the context length each was loaded with, each one's model key
    and longest context, the loaded models that can look at pictures, and every downloaded one that can (by key:
    LM Studio loads it when a request names it).

    With just-in-time loading on, ``/v1/models`` lists every downloaded model, so
    picking from it can make LM Studio load a second, bigger model. Other servers
    don't have these routes; they get empty results.
    """
    root = lm_studio_root(base)
    busy = False
    for path in ("/api/v1/models", "/api/v0/models"):
        try:
            response = http_method("get")(root + path, headers=auth_headers(settings), timeout=timeout)
            data = response.json() if response.status_code == 200 else None
        except httpx.TimeoutException:
            busy = True
            continue
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
        chat: dict[str, int] = {}
        sizes: dict[str, int] = {}
        params: dict[str, str] = {}
        embeddings: set[str] = set()
        if isinstance(data.get("models"), list):
            for item in data["models"]:
                if isinstance(item, dict) and item.get("type") == "embedding":
                    instances = [str(i["id"]) for i in item.get("loaded_instances") or [] if isinstance(i, dict) and i.get("id")]
                    if instances:
                        embeddings.update(instances)
                        embeddings.add(str(item.get("key") or instances[0]))
                    continue
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
                    if not document_reader(str(item["key"])):
                        chat[str(item["key"])] = _int(item.get("max_context_length"))
                        if _int(item.get("size_bytes")) > 0:
                            sizes[str(item["key"])] = _int(item.get("size_bytes"))
                        if isinstance(item.get("params_string"), str) and item["params_string"].strip():
                            params[str(item["key"])] = item["params_string"].strip()
                for instance in item.get("loaded_instances") or []:
                    if isinstance(instance, dict) and instance.get("id"):
                        loaded.append(str(instance["id"]))
                        reasoning[str(instance["id"])] = options
                        config = instance.get("config") if isinstance(instance.get("config"), dict) else {}
                        contexts[str(instance["id"])] = _int(config.get("context_length"))
                        reloadable[str(instance["id"])] = (str(item.get("key") or instance["id"]), _int(item.get("max_context_length")))
                        if sees:
                            seeing.add(str(instance["id"]))
            return _Listing("v1", loaded, reasoning, contexts, reloadable, seeing, downloaded, chat=chat, embeddings=embeddings, sizes=sizes, params=params)
        # LM Studio's older route says of each model whether it is loaded; a server that answers it with a plain list
        # of the models it serves isn't LM Studio.
        if isinstance(data.get("data"), list) and any(isinstance(item, dict) and "state" in item for item in data["data"]):
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
    return _Listing(busy=busy)


def _ollama_context(settings: Settings, base: str, timeout: float, model: str) -> int:
    """The context Ollama loaded ``model`` with (``/api/ps``), or 0 from other servers or when it isn't loaded."""
    try:
        response = http_method("get")(lm_studio_root(base) + "/api/ps", headers=auth_headers(settings), timeout=timeout)
        data = response.json() if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return 0
    for item in (data or {}).get("models") or [] if isinstance(data, dict) else []:
        if isinstance(item, dict) and model in {str(item.get("name")), str(item.get("model"))}:
            return _int(item.get("context_length"))
    return 0


def _llama_cpp_props(settings: Settings, base: str, timeout: float) -> dict:
    """llama.cpp's server ``/props``: whether it was started with a vision projector (``--mmproj``, in
    ``modalities``) and the context each request gets (``default_generation_settings.n_ctx``). {} from other servers."""
    root = lm_studio_root(base)
    try:
        response = http_method("get")(root + "/props", headers=auth_headers(settings), timeout=timeout)
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


def document_reader(model: str) -> bool:
    """A model made only for reading document pages (``vision.READERS``, the one list of them): LM Studio loads one
    beside the chat model to read scans, and it is never taken for the chat model."""
    from controller_inbox import vision  # vision reads pages through this module

    return vision.reader_for(model) is not vision.GENERAL


def _pick_model(
    requested: str,
    ids: list[str],
    loaded: list[str] | None = None,
    *,
    lm_studio: bool = False,
    remembered: str = "",
    reader: str = "",
    keys: dict[str, str] | None = None,
) -> str:
    """The model to ask: the one set in the settings, else the chat model loaded now. With several loaded, the chat
    model last seen loaded (``remembered``), else not the page reader chosen in Setup (``reader``, loaded beside the
    chat model to read a scan): LM Studio's own order would make it the chat model. ``keys`` gives each loaded
    instance's model key. With nothing loaded, LM Studio (``lm_studio``) lists every downloaded model and loads
    whichever a request names, so the first on its list (a coding model, say) would be loaded though nobody chose it:
    only the remembered chat model is asked for, or none, and Setup says to load one. Other servers list only what
    they serve."""
    if requested in ids:
        return requested
    chat = [item for item in loaded or [] if not document_reader(item)]
    if chat:
        names = {item: {item, (keys or {}).get(item, item)} for item in chat}
        mine = [item for item in chat if remembered and remembered in names[item]]
        others = [item for item in chat if not reader or reader not in names[item]]
        return (mine or others or chat)[0]
    if lm_studio:
        return remembered if remembered in ids and not document_reader(remembered) else ""
    if not ids:
        return "" if requested in {"", "local-model"} else requested
    others = [item for item in ids if not document_reader(item)]
    usable = [item for item in others if "embed" not in item.lower()]
    return (usable or others or [""])[0]


# The chat model last seen loaded in LM Studio, kept in the data folder: asked for (and so loaded) when nothing is.
LAST_MODEL_FILE = "chat_model.txt"


def remembered_model(settings: Settings) -> str:
    try:
        return (settings.data_dir / LAST_MODEL_FILE).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return ""


def remember_model(settings: Settings, model: str) -> None:
    """Kept only where CloseDesk keeps its data already (the data folder exists)."""
    if not model or document_reader(model) or model == remembered_model(settings):
        return
    try:
        if settings.data_dir.is_dir():
            (settings.data_dir / LAST_MODEL_FILE).write_text(model, encoding="utf-8")
    except OSError:
        pass


class ModelUnavailable(RuntimeError):
    pass


class EmptyReply(RuntimeError):
    """The server answered 200 but wrote no answer, usually because the model only reasoned."""


class ThinksTooLong(EmptyReply):
    """A model that can't be told not to think used all its room thinking, even with twice the usual room."""


# Reasoning models (Qwen3.x, DeepSeek-R1, gpt-oss) think before answering. That
# needs far more than the usual token budget unless thinking can be turned off.
THINKING_ROOM = 2048
THINKING_TIMEOUT = 300.0
# The chat's calls send a long prompt (an email with its files, or a file's tables): a laptop can take minutes to read
# it before the first word comes back, longer when LM Studio loads the model first.
CHAT_TIMEOUT = 300.0
_RETRYABLE = {400, 404, 415, 422, 500, 501}
# A server that refuses a parameter says so with one of these; a 500 or 404 can be a busy or reloading server.
_REFUSES = {400, 415, 422, 501}
_reasoning_seen: set[str] = set()
# Models whose chat template opens <think> in the prompt, on a server that doesn't separate the reasoning: a reply of
# theirs once closed a think tag it never opened. Their replies are read as reasoning until that "</think>", so
# reasoning cut off before it is never taken for the answer.
_opens_think: set[str] = set()
# Models that can't be told not to think and ran out of twice the usual room thinking in the chat (``ThinksTooLong``):
# each later question gives them that room at once, and only once, rather than the usual room first and then twice it.
_thinks_too_long: set[str] = set()
# What CloseDesk learned of each model's thinking (the three sets above), kept in the database so the next start
# doesn't learn it all over again: ``remember_thinking`` reads it back and keeps what is learned from then on there.
THINKING_KEY = "model_thinking"
_thinking_store = None
_effort_rejected: set[str] = set()
# Models whose server refused ``logprobs`` (how sure the model was of each word it wrote): asked without it from then on.
_logprobs_rejected: set[str] = set()
# Models whose server refused ``stream_options`` (asking for the token counts at the end of a stream).
_usage_rejected: set[str] = set()


@dataclass
class Reply:
    content: str
    reasoning: str = ""
    finish: str = ""
    # A streamed reply's timing (time.perf_counter(), which on Windows moves finer than time.monotonic()'s 15 ms): when its first piece came (the model had read the prompt) and its
    # last, how many pieces (about a token each, thinking included) and the token counts the server gave, if any.
    first_at: float = 0.0
    last_at: float = 0.0
    pieces: int = 0
    usage: dict = field(default_factory=dict)
    # The content closed a think tag it never opened (``_opens_think``); or, read as opening with one, it never closed
    # it and ended as usual: it didn't think, and what it wrote was the answer.
    lone_close: bool = False
    unclosed: bool = False

    def why_unusable(self) -> str:
        if not self.content and self.reasoning and self.finish == "length":
            return "the model used its whole reply budget thinking"
        if not self.content and self.reasoning:
            return "the model sent only its reasoning, no answer"
        if not self.content:
            return "the model sent an empty reply"
        snippet = " ".join(self.content.split())[:60]
        return f"reply was not the JSON shape: “{snippet}”"


class Thought:
    """A piece of the model's reasoning as it streams (``text``), kept apart from its answer: ``stream_text(thoughts=
    True)`` yields these between the answer's pieces, for the chat to show it thinking. Not a string, so it can't be
    added to the answer's text by mistake."""

    __slots__ = ("text",)

    def __init__(self, text: str):
        self.text = text


class WasThinking:
    """Yielded by ``stream_text(thoughts=True)`` when the reply closed a think tag it never opened: what it yielded as
    the answer until then was the model's reasoning (it comes again next, as a ``Thought``), and the answer starts
    after the tag."""

    __slots__ = ()


class AskedAgain:
    """Yielded by ``stream_text(thoughts=True)`` when the model ran out of room thinking and is asked again with more:
    the chat says so, and the second try's reasoning starts apart from the first's."""

    __slots__ = ()


def _reasoning_text(part: dict, *, spaces: bool = False) -> str:
    """LM Studio calls it ``reasoning_content``; OpenRouter and newer builds ``reasoning``. ``spaces``: a piece that is
    only a line break or a space is kept too (once the reasoning has begun, streamed a token at a time)."""
    for key in ("reasoning_content", "reasoning"):
        value = part.get(key)
        if isinstance(value, str) and (value.strip() or (spaces and value)):
            return value
    return ""


def _reply_from(data, *, opened: bool = False) -> Reply:
    """``opened``: the model's template opens <think> in the prompt (``_opens_think``), so a reply cut off before its
    "</think>" is all reasoning."""
    choice = _first_choice(data)
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    raw = message.get("content") if isinstance(message.get("content"), str) else ""
    reasoning = _reasoning_text(message)
    tagged = "".join(_THINK_RE.findall(raw))
    finish = str(choice.get("finish_reason") or "")
    lone = _THINK_CLOSE in raw and _THINK_OPEN not in raw.split(_THINK_CLOSE, 1)[0]
    if lone:
        tagged = raw.split(_THINK_CLOSE, 1)[0] + tagged
    elif opened and finish == "length" and _THINK_OPEN not in raw:
        return Reply(content="", reasoning=(reasoning + raw).strip(), finish=finish)
    return Reply(
        content=strip_thinking(raw),
        reasoning=(reasoning + tagged).strip(),
        finish=finish,
        lone_close=lone,
    )


def _learn_think_tags(model: str, reply: Reply) -> None:
    """A reply that closed a think tag it never opened: the model's template opens one (``_opens_think``), so it
    thinks, and needs room for it. One read that way that never closed it didn't think this time: read as usual
    again."""
    if reply.lone_close:
        _learned(_opens_think, model)
        _learned(_reasoning_seen, model)
    elif reply.unclosed:
        _learned(_opens_think, model, False)


def remember_thinking(store) -> None:
    """Read back what earlier runs learned of each model's thinking (``THINKING_KEY``), and keep what this run learns
    in ``store`` too. What can't be read is learned again, as before."""
    global _thinking_store
    _thinking_store = store
    try:
        saved = json.loads(store.get_state(THINKING_KEY) or "{}")
    except (ValueError, sqlite3.Error):
        return
    saved = saved if isinstance(saved, dict) else {}
    for name, kept in _thinking_sets().items():
        found = saved.get(name)
        kept.update(model for model in (found if isinstance(found, list) else []) if isinstance(model, str) and model)


def _thinking_sets() -> dict[str, set[str]]:
    return {"reasoning": _reasoning_seen, "opens_think": _opens_think, "thinks_too_long": _thinks_too_long}


def _learned(kept: set[str], model: str, add: bool = True) -> None:
    """Add ``model`` to (or take it from) one of the sets of what is learned of a model's thinking, and keep the change
    in the database for the next start."""
    if not model or (model in kept) == add:
        return
    if add:
        kept.add(model)
    else:
        kept.discard(model)
    if _thinking_store is None:
        return
    try:
        _thinking_store.set_state(THINKING_KEY, json.dumps({name: sorted(found) for name, found in _thinking_sets().items()}))
    except (sqlite3.Error, OSError):
        pass  # learned again next time


def reasoning_effort(settings: Settings, model: str) -> str | None:
    """The lightest ``reasoning_effort`` this model accepts, or None to leave it alone.

    LM Studio 0.4.8+ reports each model's options on ``/api/v1/models``; for
    older builds, a model is treated as a thinker once it has sent reasoning.
    """
    if model in _effort_rejected:
        return None
    options = _reasoning_options(settings, model)
    if "off" in options:
        return "none"
    if "low" in options:
        return "low"
    if model in _reasoning_seen and not options:  # listed options without "off" or "low": it can't be turned down
        return "none"
    return None


def _reasoning_options(settings: Settings, model: str) -> list[str]:
    """The ``reasoning_effort`` values LM Studio lists for ``model`` (empty when it lists none)."""
    status = check_model(settings)
    return list(status.reasoning if status.model == model else status.reasoning_options.get(model, []))


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
    """A laptop writing 2,048 tokens of thinking can take minutes, and twice the room takes twice as long: a model
    that can't stop thinking, given 4,096 tokens, is waited for 10 minutes rather than taken for one still loading."""
    if budget < THINKING_ROOM:
        return settings.llm_timeout
    return max(settings.llm_timeout, THINKING_TIMEOUT * budget / THINKING_ROOM)


def _post_chat(post, url: str, payload: dict, settings: Settings, **options) -> httpx.Response:
    """POST, dropping ``reasoning_effort`` if this server build refuses it.

    The model is only remembered as refusing ``reasoning_effort`` when the same request
    without it goes through. When that retry fails too, something else (``response_format``,
    ``tools``) was the problem: the first answer comes back and the payload keeps its effort.
    A busy server (500, 404) is asked once more as it was, with the effort kept: without it, a model that can turn
    thinking off would think again with the same room. A model out of memory isn't asked again at all.

    A request that runs out of time carries how long it waited (``waited``, in seconds) and how many tokens it might
    write (``written``): a thinking model's call waits longer than the chat's wait, and the chat's offer of more time
    says and suggests from these.
    """
    try:
        return _post_chat_once(post, url, payload, settings, **options)
    except httpx.TimeoutException as exc:
        timeout = options.get("timeout")
        exc.waited = float(timeout.read or 0) if isinstance(timeout, httpx.Timeout) else float(timeout or 0)
        exc.written = int(payload.get("max_tokens") or 0)
        raise


def _post_chat_once(post, url: str, payload: dict, settings: Settings, **options) -> httpx.Response:
    with _using(str(payload.get("model")), options.get("timeout")):
        response = post(url, json=payload, headers=auth_headers(settings), **options)
        if response.status_code >= 400 and _OVERFLOW_RE.search(response.text[:600]):
            # A prompt too long for the context is no shorter without the effort (or the schema): not sent again.
            _raise_for(response)
        if "reasoning_effort" in payload and response.status_code in _REFUSES:
            effort = payload.pop("reasoning_effort")
            retry = post(url, json=payload, headers=auth_headers(settings), **options)
            if retry.status_code < 400:
                # Remembered only when the request without it goes through.
                _effort_rejected.add(str(payload.get("model")))
                return retry
            payload["reasoning_effort"] = effort
        elif "reasoning_effort" in payload and response.status_code in _RETRYABLE and not out_of_memory(response.text):
            retry = post(url, json=payload, headers=auth_headers(settings), **options)
            if retry.status_code < 400:
                return retry
        return response


@dataclass
class ReaderStats:
    read: int = 0
    failed: int = 0
    seconds: float = 0.0
    stopped_reason: str = ""
    last_error: str = ""
    times: list[float] = field(default_factory=list)  # seconds each email read took, in order
    too_slow: bool = False  # stopped because the model sent nothing for longer than even the longer wait allows

    @property
    def average_seconds(self) -> float:
        return self.seconds / self.read if self.read else 0.0

    @property
    def typical_seconds(self) -> float:
        """The middle of the times each email took: one email that waited for LM Studio to load the model (the first,
        when nothing was loaded) doesn't make the model look slow."""
        return statistics.median(self.times) if self.times else self.average_seconds


class LocalReader:
    """One reader per run. Holds the resolved model and stops on a dead server."""

    def __init__(
        self, settings: Settings, *, model: str | None = None, client: httpx.Client | None = None, store: Store | None = None
    ):
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
        self._memory_failures = 0  # of those, the ones that ran out of memory
        # The reply is streamed, so the time limit is how long the model may go without a word, not how long the whole
        # reply may take: a big model writing slowly is never cut off. A server that refuses streaming is asked for
        # the whole reply in one piece instead.
        self._streams = True
        self._longer = 0.0  # the longer wait, once a model needed it: kept for the rest of the run
        # How long this model may need, worked out for it (a big model on a laptop reads an email for minutes before it
        # writes a word) or set by the user: the reading waits at least that long between words.
        self._model_wait = 0.0
        if store is not None and self.model:
            from controller_inbox import model_fit  # it imports this module

            self._model_wait = model_fit.reading_wait(store, settings, self.model)
        self._partial = Reply(content="")

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
            if not ran_out_of_memory(exc):
                return self._failed(_short_error(exc))
            offer = step_back_offer(self.settings)
            self._failed("the local model ran out of memory", memory=True)
            if offer and not self.stats.stopped_reason:
                # A context CloseDesk gave the model: it isn't reloaded smaller without the user's yes. The rest of the
                # run's mail is filed without the model, and Settings offers the smaller context.
                article = "an" if str(offer["loaded"]).startswith("8") else "a"
                self.stats.stopped_reason = (
                    f"the local model ran out of memory with {article} {offer['loaded']:,}-token context. Settings → Local "
                    f"model can reload {offer['model']} with {offer['size']:,}."
                )
            return None
        except ContextOverflow as exc:
            return self._failed(str(exc))
        if parsed is None:
            if isinstance(stuck := _empty_reply(self.settings, self.model, self._max_tokens, reply), ThinksTooLong):
                # Every email would go the same way, minutes each: the run stops, and says why once.
                self.stats.stopped_reason = str(stuck)
            return self._failed(reply.why_unusable())
        self._consecutive_failures = self._memory_failures = 0
        took = time.monotonic() - started
        self.stats.read += 1
        self.stats.seconds += took
        self.stats.times.append(took)
        return parsed

    def _failed(self, reason: str, *, memory: bool = False) -> None:
        self.stats.failed += 1
        self.stats.last_error = reason
        self._consecutive_failures += 1
        self._memory_failures = self._memory_failures + 1 if memory else 0
        if self._consecutive_failures >= 3 and not self.stats.stopped_reason:
            if self._memory_failures >= 3:
                # Not LM Studio's "Server error '500 ...' for url ...": what happened, and what to do about it.
                self.stats.stopped_reason = (
                    f"the local model ran out of memory on three messages in a row. {memory_advice(self.settings)}"
                )
            else:
                self.stats.stopped_reason = f"three messages in a row failed ({reason})"
        return None

    def _adapt(self, reply: Reply) -> bool:
        """After a reasoning model's unusable reply: settings for one more try, if any changed."""
        if reply.content and not reply.reasoning:
            return False
        if reply.reasoning:
            _learned(_reasoning_seen, self.model)
        changed = False
        if self._effort is None and self.model not in _effort_rejected:
            self._effort, changed = "none", True
        if self._max_tokens < THINKING_ROOM:
            self._max_tokens, changed = THINKING_ROOM, True
        elif reply.finish == "length" and reply.reasoning and not reply.content and self._max_tokens < 2 * THINKING_ROOM:
            # All its room went on thinking: a model that can't turn thinking off (DeepSeek-R1 allows only ``on``)
            # thinks as long with the effort turned down, so it gets twice the room, once, for the rest of the run.
            self._max_tokens, changed = self._max_tokens * 2, True
        if self._structured and not reply.content:
            # LM Studio can route schema-constrained output into the reasoning
            # stream and leave the answer empty; plain prompts answer normally.
            self._structured, changed = False, True
        return changed

    def _complete(self, user: str) -> Reply:
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        loads_waited = 0
        while True:
            try:
                return self._ask(url, user)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError) as exc:
                raise ModelUnavailable(f"the local model server stopped answering ({_short_error(exc)})") from exc
            except httpx.TimeoutException as exc:
                # Nothing came for the whole wait: asked again once the reason is known, or the reading stops.
                loads_waited += self._after_timeout(exc, loads_waited)

    def _after_timeout(self, exc: httpx.TimeoutException, loads_waited: int) -> int:
        """Why the model sent nothing in time, and what to do: returns 1 after waiting for LM Studio to load the model,
        0 when it is to be asked again with the longer wait. Raises ``ModelUnavailable`` to stop the reading."""
        root = lm_studio_root(self.settings.llm_base_url.rstrip("/"))
        if _gone(self.settings, root):
            raise ModelUnavailable(f"the local model server stopped answering ({_short_error(exc)})") from exc
        status = check_model(self.settings, use_cache=False)
        if status.lm_studio and not _is_loaded(self.settings, self.model) and loads_waited < 2:
            # LM Studio unloaded the model (idle auto-unload, a restart) and this request had it load it again: the
            # load goes on after the request stopped waiting. Waited for, then the email is asked again.
            if wait_for_load(self.settings, self.model):
                return 1
            raise ModelUnavailable(
                f"LM Studio was still loading {self.model} after {LOAD_WAIT_SECONDS / 60:.0f} minutes. Check LM Studio, "
                "and process the mail again once it shows the model loaded"
            ) from exc
        if not self._longer:
            # A big model reading a long email can take a while to start. Given longer, for the rest of the run.
            self._longer = max(self._wait() * 2, CHAT_TIMEOUT)
            return 0
        self.stats.too_slow = True
        raise ModelUnavailable(f"{self.model} takes over {self._wait():.0f} s an email on this computer") from exc

    def _wait(self) -> float:
        """How long the model may go without sending a word: longer with room to think, longer still once needed."""
        return max(_timeout(self.settings, self._max_tokens), self._longer, self._model_wait)

    def _ask(self, url: str, user: str) -> Reply:
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": self._max_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user},
            ],
        }
        if self._streams:
            payload["stream"] = True
        if self._structured:
            payload["response_format"] = {"type": "json_schema", "json_schema": _schema()}
        if self._effort:
            payload["reasoning_effort"] = self._effort
        response = _post_chat(self._send, url, payload, self.settings, timeout=httpx.Timeout(self._wait(), connect=5.0))
        # A busy moment (a 500 the same request without the effort got past) is not a refusal: the effort and the
        # schema are only dropped for the rest of the run when the server said no to them.
        if self.model in _effort_rejected:
            self._effort = None
        if self._structured and response.status_code in _RETRYABLE:
            # Older LM Studio / Ollama builds reject response_format. Ask again, plain.
            refused = response.status_code in _REFUSES
            payload.pop("response_format", None)
            response = _post_chat(self._send, url, payload, self.settings, timeout=httpx.Timeout(self._wait(), connect=5.0))
            if refused and response.status_code < 400:
                self._structured = False
            if self.model in _effort_rejected:
                self._effort = None
        if self._streams and response.status_code in _REFUSES:
            # A server that doesn't stream: the whole reply in one piece, from then on.
            payload.pop("stream", None)
            response = _post_chat(self._send, url, payload, self.settings, timeout=httpx.Timeout(self._wait(), connect=5.0))
            if response.status_code < 400:
                self._streams = False
        response.raise_for_status()
        reply = self._partial
        if reply.reasoning:
            _learned(_reasoning_seen, self.model)
        return reply

    def _send(self, url: str, *, json: dict, headers: dict, **options) -> httpx.Response:
        """One request, read as it comes (``_post_chat`` sends it, and again without what the server refused). The
        reply goes into ``self._partial``; a refusal is returned read, so its text can be looked at."""
        reply = self._partial = Reply(content="")
        raw: list[str] = []
        with self.client.stream("POST", url, json=json, headers=headers, **options) as response:
            if response.status_code >= 400:
                response.read()
                return response
            whole = "text/event-stream" not in response.headers.get("content-type", "")
            pieces = list(_stream_pieces(response, reply, raw=raw))
        if raw:
            # Streamed: the content as sent, think tags and all, read as a reply in one piece is.
            sent = "".join(raw)
            reasoning = "" if reply.reasoning == _THINK_OPEN else reply.reasoning
            reply.content = strip_thinking(sent)
            reply.reasoning = (reasoning + "".join(_THINK_RE.findall(sent))).strip()
        else:
            reply.content = "".join(pieces)
            if whole and not reply.content and not reply.reasoning and not reply.finish:
                raise httpx.DecodingError("unexpected reply: no choices")
        return response


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

    def __init__(self, *, opened: bool = False, lone: bool = False) -> None:
        """``opened``: the reply starts inside a think tag, opened in the prompt (``_opens_think``). ``lone``: a closing
        tag that comes before any opening one ends the reasoning there, though nothing said the reply began with it:
        what was passed on as the answer until then was reasoning (``relabelled`` says so, once)."""
        self.buffer = ""
        self.inside = opened
        self.started = False
        self.saw = opened
        self.closed = False  # a closing tag was read
        self.lone = lone and not opened
        self.relabelled = False
        self._thought: list[str] = []  # the reasoning between the tags, as it comes (``take_thought``)

    def take_thought(self) -> str:
        """The reasoning read inside think tags since the last call."""
        thought, self._thought = "".join(self._thought), []
        return thought

    def feed(self, piece: str) -> str:
        self.buffer += piece
        out: list[str] = []
        while True:
            if self.inside:
                cut = self.buffer.find(_THINK_CLOSE)
                if cut < 0:
                    keep = len(_THINK_CLOSE) - 1  # a closing tag may be arriving split
                    self._thought.append(self.buffer[:-keep])
                    self.buffer = self.buffer[-keep:]
                    break
                self._thought.append(self.buffer[:cut])
                self.buffer = self.buffer[cut + len(_THINK_CLOSE) :]
                self.inside = False
                self.closed = True
                continue
            cut = self.buffer.find(_THINK_OPEN)
            close = self.buffer.find(_THINK_CLOSE) if self.lone and not self.saw else -1
            if close >= 0 and (cut < 0 or close < cut):
                # The reasoning ends here: all of it so far, this piece's included, was passed on or held as the answer.
                out.clear()
                self.buffer = self.buffer[close + len(_THINK_CLOSE) :]
                self.saw = self.closed = self.relabelled = True
                self.started = False
                continue
            if cut < 0:
                tags = (_THINK_OPEN, _THINK_CLOSE) if self.lone and not self.saw else (_THINK_OPEN,)
                keep = max(
                    next((k for k in range(len(tag) - 1, 0, -1) if self.buffer.endswith(tag[:k])), 0) for tag in tags
                )  # a tag may be arriving split
                out.append(self.buffer[: len(self.buffer) - keep])
                self.buffer = self.buffer[len(self.buffer) - keep :]
                break
            out.append(self.buffer[:cut])
            self.buffer = self.buffer[cut + len(_THINK_OPEN) :]
            self.inside = self.saw = True
        return self._lead("".join(out))

    def flush(self) -> str:
        rest = "" if self.inside else self.buffer
        if self.inside:  # the reasoning stopped before its closing tag: the characters held back are reasoning too
            self._thought.append(self.buffer)
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
    if model in _thinks_too_long:
        return model, effort, max(max_tokens, 2 * THINKING_ROOM)
    return model, effort, max(max_tokens, THINKING_ROOM) if _thinks(settings, model, effort) else max_tokens


def reply_budget(settings: Settings, max_tokens: int) -> int:
    """Tokens a chat reply may use, including room to think on reasoning models."""
    return _chat_plan(settings, max_tokens)[2]


def _retry_plan(
    settings: Settings, model: str, effort: str | None, budget: int, reply: Reply
) -> tuple[str | None, int] | None:
    """A reasoning model wrote no answer: turn thinking down and give it room, once. One that ran out of room
    thinking gets twice the room: a model that can't turn thinking off (DeepSeek-R1 allows only ``on``) thinks as long
    with the effort turned down, so the same budget would only run out again; it isn't sent an effort it doesn't
    allow. One that ran out of twice the room before (``_thinks_too_long``) was given that at once: not asked again."""
    if reply.content or not reply.reasoning:
        return None
    if model in _thinks_too_long and budget >= 2 * THINKING_ROOM:
        return None
    _learned(_reasoning_seen, model)
    options = _reasoning_options(settings, model)
    fixed = bool(options) and not {"off", "low"} & set(options)  # LM Studio lists no way to turn it down
    new_effort = effort or (None if model in _effort_rejected or fixed else "none")
    new_budget = max(budget * 2 if reply.finish == "length" else budget, THINKING_ROOM)
    if (new_effort, new_budget) == (effort, budget):
        return None
    return new_effort, new_budget


def _empty_reply(settings: Settings, model: str, budget: int, reply: Reply) -> EmptyReply:
    """What to raise for a reply with no answer. A model that can't be told not to think (LM Studio lists no "off"
    for it: DeepSeek-R1 distills, QwQ) that used all of twice the usual room thinking will go on doing so: that is
    said plainly, with what to do, rather than only that it ran out of room."""
    if reply.content or not reply.reasoning or reply.finish != "length" or budget < 2 * THINKING_ROOM:
        return EmptyReply(reply.why_unusable())
    options = _reasoning_options(settings, model)
    if not options or "off" in options:
        return EmptyReply(reply.why_unusable())
    _learned(_thinks_too_long, model)
    # Not the family of the model that failed (a Qwen3 Thinking build, or a DeepSeek-R1 distilled from Qwen3).
    example = "Gemma 3" if "qwen" in model.lower() else "Qwen3"
    return ThinksTooLong(
        f"{model} thinks before every answer and can't be told not to, and it ran out of room even with {budget:,} "
        f"tokens. Choose one that doesn't have to think first, like {example}, or a smaller one in Settings"
    )


def complete_text(
    settings: Settings, messages: list[dict], *, max_tokens: int = 400, temperature: float | None = None, think: bool = False,
    limit: float | None = None,
) -> str:
    """One plain-text answer. Raises ``httpx.HTTPError`` or ``EmptyReply`` when there is none. ``temperature``:
    other than the usual 0.2, for a second, differently worded try; ``think``: see ``_chat_plan``. ``limit``: how
    long the model may take, worked out for it (``model_fit.chat_wait``), in place of the usual ``CHAT_TIMEOUT``."""
    model, effort, budget = _chat_plan(settings, max_tokens, think=think)
    for _attempt in range(2):
        url, payload = _chat_request(settings, messages, budget, stream=False)
        sent = budget
        if temperature is not None:
            payload["temperature"] = temperature
        if effort:
            payload["reasoning_effort"] = effort
        response = _post_chat(_model_http("post"), url, payload, settings, timeout=max(_timeout(settings, budget), limit or CHAT_TIMEOUT))
        _raise_for(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise httpx.DecodingError(f"unexpected reply: {exc}") from exc
        reply = _reply_from(data, opened=model in _opens_think)
        _learn_think_tags(model, reply)
        if reply.content:
            return reply.content
        plan = _retry_plan(settings, model, payload.get("reasoning_effort"), budget, reply)
        if plan is None:
            break
        effort, budget = plan
    raise _empty_reply(settings, model, sent, reply)


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
    token_probs: list | None = None,
    limit: float | None = None,
    thoughts: bool = False,
):
    """Yield the answer as it is written. Servers that ignore ``stream`` send it in one piece.

    Raises ``EmptyReply`` when the model wrote nothing, so callers never show a blank answer. ``wait``: how long
    the model may go quiet (looking at a picture first can take minutes on a laptop); ``temperature``: other than
    the usual 0.2; ``finished``: given a dict, its "reason" is set to why the reply ended ("length": cut off) and
    "thought" to whether the model reasoned first; ``sampling``: more settings sent as they are (top_p, top_k,
    presence_penalty); ``model``: another model than the chat one (LM Studio loads it when asked); ``token_probs``:
    given a list, filled with (piece of text, log probability) for each token of the answer, when the server sends
    them (llama.cpp does; a server that doesn't leaves it empty). ``limit``: how long the model may go quiet, worked
    out for it (``model_fit.chat_wait``), in place of the usual ``CHAT_TIMEOUT``. ``thoughts``: the model's reasoning
    is yielded too as it comes, as ``Thought`` pieces (never added to the answer), ``WasThinking`` when a reply that
    closes a think tag it never opened shows what came before as its reasoning, and ``AskedAgain`` before a second
    try with more room. ``finished`` is also given the model asked (``model``) and the
    reply's timing: ``read_seconds`` (until its first piece), ``write_seconds``, ``written`` (tokens, thinking
    included) and ``prompt_tokens`` when the server counted them.
    """
    model, effort, budget = _chat_plan(settings, max_tokens, model=model)
    # The first attempt that got going timed the reading of the prompt. One asked again finds the prompt still read
    # (LM Studio keeps it) and starts at once, which would look like the model reading thousands of tokens a second.
    read_timed = False
    for _attempt in range(2):
        reply, sent = Reply(content=""), budget
        if token_probs is not None:
            token_probs.clear()
        started = time.perf_counter()
        try:
            for piece in _stream_once(
                settings, messages, budget, effort, reply, wait=wait, temperature=temperature, sampling=sampling, model=model,
                token_probs=token_probs, limit=limit, thoughts=thoughts,
            ):
                if isinstance(piece, Thought):
                    yield piece
                    continue
                if isinstance(piece, WasThinking):
                    reply.content = ""  # that was its reasoning: the answer starts after the tag
                    if token_probs is not None:
                        token_probs.clear()
                    yield piece
                    continue
                reply.content += piece
                yield piece
        except httpx.TimeoutException as exc:
            # Went quiet after it had started: still writing (or thinking), not loading. ``failure_note`` says which.
            if reply.content or reply.reasoning:
                exc.reply_started = "writing" if reply.content else "thinking"
            raise
        _learn_think_tags(model, reply)
        if finished is not None:
            finished["reason"], finished["thought"], finished["model"] = reply.finish, bool(reply.reasoning), model
            if reply.first_at:
                finished["write_seconds"] = reply.last_at - reply.first_at
                finished["written"] = _int(reply.usage.get("completion_tokens")) or reply.pieces
            if reply.first_at and not read_timed:
                read_timed = True
                finished["read_seconds"] = reply.first_at - started
                if "prompt_tokens" in reply.usage:
                    finished["prompt_tokens"] = _prompt_read(reply.usage)
                    finished["prompt_counted"] = _cache_said(reply.usage)
        if reply.content:
            return
        if _attempt:  # it was asked again already: no third try, so nothing says it is asked again
            break
        plan = _retry_plan(settings, model, effort if model not in _effort_rejected else None, budget, reply)
        if plan is None:
            break
        effort, budget = plan
        if thoughts:
            yield AskedAgain()
    raise _empty_reply(settings, model, sent, reply)


def _prompt_read(usage: dict) -> int:
    """The prompt tokens the model read for this reply, from the server's counts. LM Studio keeps
    the start of a prompt it read before and doesn't read it again: those tokens (``cached_tokens``) aren't counted,
    or a prompt asked again would make the model look many times faster than it reads."""
    details = usage.get("prompt_tokens_details")
    cached = _int(details.get("cached_tokens")) if isinstance(details, dict) else 0
    return max(0, _int(usage.get("prompt_tokens")) - cached)


def _cache_said(usage: dict) -> bool:
    """The server said how much of the prompt it had read before (``cached_tokens``, 0 included), so the tokens
    ``_prompt_read`` gives are the ones it read for this reply. LM Studio doesn't always say."""
    details = usage.get("prompt_tokens_details")
    return isinstance(details, dict) and isinstance(details.get("cached_tokens"), int)


def loaded_now(status: ModelStatus, model: str) -> bool:
    """``model`` (an instance id or key) is loaded in LM Studio now, by ``status``; True when that can't be told (not
    LM Studio)."""
    if not status.lm_studio:
        return True
    return any(model in (instance, key) for instance, (key, _context) in status.instances.items())


def _stream_once(
    settings: Settings,
    messages: list[dict],
    budget: int,
    effort: str | None,
    reply: Reply,
    *,
    rejected_effort: str = "",
    rejected_probs: str = "",
    wait: float | None = None,
    temperature: float | None = None,
    sampling: dict | None = None,
    model: str | None = None,
    token_probs: list | None = None,
    again: bool = False,
    limit: float | None = None,
    thoughts: bool = False,
):
    """``rejected_effort`` names the model whose ``reasoning_effort`` the last try sent; it is
    remembered as refusing it only if this try, without it, is accepted. ``rejected_probs``: the same for ``logprobs``.
    ``again``: this is the one more try a busy server gets."""
    url, payload = _chat_request(settings, messages, budget, stream=True, model=model)
    if effort:
        payload["reasoning_effort"] = effort
    if temperature is not None:
        payload["temperature"] = temperature
    if sampling:
        payload.update(sampling)
    asks_probs = token_probs is not None and str(payload.get("model")) not in _logprobs_rejected
    if asks_probs:
        # Each token's own chance; llama.cpp lists 20 runners-up for each unless told, which only makes the reply big.
        payload["logprobs"], payload["top_logprobs"] = True, 1
    asks_usage = str(payload.get("model")) not in _usage_rejected
    if asks_usage:
        # The token counts at the end, which a stream leaves out unless asked: with them the reading speed is
        # measured from the tokens the model read, and from those it read anew when the server says so.
        payload["stream_options"] = {"include_usage": True}
    # Nothing comes until the model has read the whole prompt (and LM Studio has loaded it, if it wasn't), so a laptop
    # reading a long prompt isn't taken for a model that stopped.
    timeout = httpx.Timeout(max(settings.llm_timeout, limit or CHAT_TIMEOUT, wait or 0.0), connect=5.0)
    without_probs = without_usage = busy = False
    with _using(str(payload.get("model")), timeout), _model_http("stream")("POST", url, json=payload, headers=auth_headers(settings), timeout=timeout) as response:
        if response.status_code >= 400:
            response.read()
            if _OVERFLOW_RE.search(response.text[:600]):
                # A prompt too long for the context: not a refusal of logprobs or the effort, and no shorter without them.
                _raise_for(response)
        if asks_usage and response.status_code in _REFUSES and re.search(r"stream_options|include_usage", response.text[:600]):
            # A server that names ``stream_options`` as what it refused: asked without it from then on.
            _usage_rejected.add(str(payload.get("model")))
            rejected = False
            without_usage = True
        elif asks_probs and response.status_code in _REFUSES:
            # A server that doesn't know ``logprobs``: the reading matters, how sure it was is a nice-to-have.
            rejected = False
            without_probs = True
        elif effort and response.status_code in _REFUSES:
            rejected = True
        elif effort and response.status_code in _RETRYABLE and not again and not out_of_memory(response.text):
            # A busy server: asked once more as it was. Without the effort, a model that can turn thinking off would
            # think again with the same room; a model out of memory isn't asked again at all.
            rejected, busy = False, True
        else:
            rejected = False
            _raise_for(response)
            if rejected_effort:
                _effort_rejected.add(rejected_effort)
            if rejected_probs:
                _logprobs_rejected.add(rejected_probs)
            yield from _stream_pieces(
                response, reply, token_probs if asks_probs else None, thoughts=thoughts,
                opened=str(payload.get("model")) in _opens_think,
            )
    if without_usage:
        yield from _stream_once(
            settings, messages, budget, effort, reply,
            rejected_effort=rejected_effort, rejected_probs=rejected_probs, wait=wait, temperature=temperature,
            sampling=sampling, model=model, token_probs=token_probs, again=again, limit=limit, thoughts=thoughts,
        )
    elif without_probs:
        # Remembered only when the request without logprobs goes through, as the effort is.
        yield from _stream_once(
            settings, messages, budget, effort, reply,
            rejected_effort=rejected_effort, rejected_probs=str(payload.get("model")), wait=wait, temperature=temperature,
            sampling=sampling, model=model, limit=limit, thoughts=thoughts,
        )
    elif rejected:
        # Remembered as refusing the effort only if the request without it goes through.
        yield from _stream_once(
            settings, messages, budget, None, reply,
            rejected_effort=str(payload.get("model")), rejected_probs=rejected_probs, wait=wait,
            temperature=temperature, sampling=sampling, model=model, token_probs=token_probs, limit=limit, thoughts=thoughts,
        )
    elif busy:
        yield from _stream_once(
            settings, messages, budget, effort, reply,
            rejected_effort=rejected_effort, rejected_probs=rejected_probs, wait=wait,
            temperature=temperature, sampling=sampling, model=model, token_probs=token_probs, again=True, limit=limit,
            thoughts=thoughts,
        )


def _token_probs(choice: dict, into: list | None) -> None:
    """Add the tokens a reply's choice carries with their log probabilities (OpenAI's ``logprobs.content``) to
    ``into``; a choice without them adds nothing."""
    if into is None:
        return
    found = choice.get("logprobs")
    items = found.get("content") if isinstance(found, dict) else None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        token, logprob = item.get("token"), item.get("logprob")
        if isinstance(token, str) and isinstance(logprob, (int, float)) and not isinstance(logprob, bool):
            into.append((token, float(logprob)))


def _stream_pieces(
    response: httpx.Response, reply: Reply, token_probs: list | None = None, raw: list | None = None, *, thoughts: bool = False,
    opened: bool = False,
):
    """The answer's pieces as they come. ``raw``: given a list, filled with the content as sent, think tags and all.
    ``thoughts``: the reasoning is yielded too, as ``Thought`` pieces, whether in its own field or in think tags.
    ``opened``: the model's template opens <think> in the prompt (``_opens_think``): what comes before its "</think>"
    is reasoning."""
    if "text/event-stream" not in response.headers.get("content-type", ""):
        try:
            data = json.loads(response.read() or b"{}")
        except ValueError as exc:
            raise httpx.DecodingError(f"unexpected reply: {exc}") from exc
        whole = _reply_from(data, opened=opened)
        reply.reasoning, reply.finish, reply.lone_close = whole.reasoning, whole.finish, whole.lone_close
        if whole.content:
            if not whole.reasoning:
                _token_probs(_first_choice(data), token_probs)
            yield whole.content
        return
    thinking = ThinkFilter(opened=opened, lone=thoughts)
    held: list[str] = []  # read as reasoning because the template opened the tag: the answer if it never closes
    sent = ""  # the content as sent, think tags and all
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
        if isinstance(data, dict) and isinstance(data.get("usage"), dict):
            reply.usage = data["usage"]
        choice = _first_choice(data)
        part = choice.get("delta") if isinstance(choice.get("delta"), dict) else choice.get("message")
        part = part if isinstance(part, dict) else {}
        # A line break or a space between the reasoning's words is kept, for the Thinking box to show as sent.
        reasoned = _reasoning_text(part, spaces=bool(reply.reasoning))
        if reasoned:
            reply.reasoning += reasoned
        if choice.get("finish_reason"):
            reply.finish = str(choice["finish_reason"])
        content = part.get("content") if isinstance(part.get("content"), str) else ""
        if content or reasoned:
            # Timed for the estimates: the first piece comes once the model has read the prompt.
            reply.last_at = time.perf_counter()
            reply.first_at = reply.first_at or reply.last_at
            reply.pieces += 1
        if raw is not None and content:
            raw.append(content)
        sent += content
        piece = thinking.feed(content)
        if thinking.saw and not reply.reasoning:
            reply.reasoning = "<think>"
        tagged = thinking.take_thought()
        if thinking.relabelled:
            # A "</think>" it never opened: what came before it, passed on as the answer, was its reasoning.
            thinking.relabelled = False
            yield WasThinking()
            tagged = sent.split(_THINK_CLOSE, 1)[0].strip() + tagged
        if opened and not thinking.closed:
            held.append(tagged)
        if thoughts and (thought := reasoned + tagged):
            yield Thought(thought)
        if piece:
            # Only the answer's tokens: not its reasoning, which comes in its own field or between think tags.
            _token_probs(choice, token_probs)
            yield piece
    rest = thinking.flush()
    tagged = thinking.take_thought()
    reply.lone_close = not opened and _THINK_CLOSE in sent and _THINK_OPEN not in sent.split(_THINK_CLOSE, 1)[0]
    if opened and not thinking.closed and reply.finish != "length":
        # Read as reasoning, but it never closed the tag and ended as usual: this time it didn't think, and that was
        # the answer (shown in the Thinking box as it came, then as the answer).
        reply.unclosed = True
        reply.reasoning = "" if reply.reasoning == _THINK_OPEN else reply.reasoning
        rest = "".join(held) + tagged + rest
    elif thoughts and tagged:
        yield Thought(tagged)
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
        error = error.get("message") or error.get("detail") or json.dumps(error)
    said = " ".join(str(error or "unknown error").split())
    text = said[:200]
    if _OVERFLOW_RE.search(said):
        raise ContextOverflow(text)
    failed = httpx.HTTPError(f"the model server stopped with an error ({text})")
    failed.said = said  # all of it: a memory error can be named only past the first 200 characters
    raise failed


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
# The context each model CloseDesk raised was loaded with by it, by instance: when LM Studio has loaded it again since
# with another (it unloaded it while idle, the user ejected it, and a question had it loaded with LM Studio's own
# default), it is a new instance, and the longer context is tried again.
_context_tried: dict[str, int] = {}
# The context CloseDesk itself gave each chat model (raising it, or loading it when chosen in Settings), by LM Studio's
# key. A model the user loaded at a size they chose isn't here, nor is one they loaded again since: it is left alone.
_context_given: dict[str, int] = {}
# The context a model CloseDesk raised had before, by key: it is loaded back with it when the longer one runs out of
# memory, or when the minimum is lowered to it.
_context_before: dict[str, int] = {}
# The context a model CloseDesk raised last answered with, by key: when the longer one runs out of memory it is loaded
# back with this (after a second raise, the size that worked rather than the user's first one).
_context_back: dict[str, int] = {}
# The context each model CloseDesk loaded back ran out of memory answering with, by key, for Settings to say so (not
# that LM Studio couldn't load it). Forgotten when the minimum is changed or the model is loaded afresh.
_context_stepped: dict[str, int] = {}
# The models of ``_context_stepped`` loaded back with a size they had before (that they answered with, or the user's
# own), rather than the next size down: Settings says "back" for these and "again" for the rest.
_context_stepped_back: set[str] = set()
# The context a model CloseDesk loaded ran out of memory answering with when there was no smaller size to load it with
# (4,096 tokens), by key: it doesn't fit this computer, and Settings says so. Forgotten as ``_context_stepped`` is.
_context_no_smaller: dict[str, int] = {}
# The context each model CloseDesk sized ran out of memory with, by key, for Settings to offer loading it again smaller
# (the user is asked first). Only while it is still loaded with that context.
_context_ran_out: dict[str, int] = {}
# The context a model the user loaded in LM Studio (not CloseDesk) ran out of memory answering with, by key: Settings
# says so and names the Context Length to reload it with there, and once it is, CloseDesk doesn't raise it back to
# that size or more. Forgotten when the model is chosen again in Settings.
_theirs_ran_out: dict[str, int] = {}
_context_lock = threading.Lock()

# Models answering a request right now (a chat answer, the overnight reading, a page read), by the name the request
# gave, with when each request started and how long it may take: none of them is unloaded or reloaded under the
# request. A request may take as long as it waits for the model (up to 2 hours, or 4 for the reading's longer wait)
# and IN_USE_SECONDS more to write; past that it no longer counts (a stream left unclosed must not keep the model
# "busy" for good).
_in_use: dict[str, list[tuple[float, float]]] = {}
_in_use_lock = threading.Lock()
IN_USE_SECONDS = 1800.0


def _wait_of(timeout) -> float:
    """The seconds a request waits for the model, from its ``timeout`` (an httpx.Timeout or a number)."""
    if isinstance(timeout, httpx.Timeout):
        timeout = timeout.read
    return float(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else 0.0


@contextmanager
def _using(model: str, timeout=None):
    entry = (time.monotonic(), IN_USE_SECONDS + _wait_of(timeout))
    with _in_use_lock:
        _in_use.setdefault(model, []).append(entry)
    try:
        yield
    finally:
        with _in_use_lock:
            requests = _in_use.get(model, [])
            if entry in requests:
                requests.remove(entry)
            if not requests:
                _in_use.pop(model, None)


def model_busy(*names: str) -> bool:
    """A request is being answered by one of these models (instance ids or keys) right now."""
    now = time.monotonic()
    with _in_use_lock:
        return any(now - started < allowed for name in names if name for started, allowed in _in_use.get(name, []))


# A big model can take LM Studio longer to load than CloseDesk waits for the answer to its load request. The load goes
# on, using the memory, so a load that timed out isn't taken for one that failed: LM Studio's list of loaded models is
# watched for it for up to LOAD_WAIT_SECONDS, and no other model is loaded (or loaded back) while it may still be going.
LOAD_WAIT_SECONDS = 600.0
LOAD_POLL_SECONDS = 5.0
_pending_loads: dict[str, float] = {}  # model key -> when CloseDesk stopped waiting for its load
# How long a chat question waits for another load to finish before it goes on without the longer context.
CONTEXT_WAIT_SECONDS = 5.0


class StillLoading(RuntimeError):
    """LM Studio may still be loading a model CloseDesk asked for: no other load may start until it is in."""

    def __init__(self, key: str):
        super().__init__(
            f"LM Studio is still loading {key}: it took longer than CloseDesk waits ({LOAD_WAIT_SECONDS / 60:.0f} minutes). "
            f"No other model is loaded until it is in, so two big models don't load at once. Check LM Studio, and try "
            "again once it shows the model loaded."
        )
        self.key = key


def _is_loaded(settings: Settings, key: str) -> bool:
    status = check_model(settings, use_cache=False)
    return any(key in (instance, model) for instance, (model, _size) in status.instances.items())


def _gone(settings: Settings, root: str) -> bool:
    """LM Studio refuses connections (it stopped). One too busy loading to answer in time is not gone."""
    try:
        http_method("get")(root + "/api/v1/models", headers=auth_headers(settings), timeout=httpx.Timeout(10.0, connect=5.0))
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return True
    except httpx.HTTPError:
        pass
    return False


def _no_load_pending(settings: Settings) -> None:
    """Raise ``StillLoading`` while a load that timed out may still be going on in LM Studio."""
    for key, since in list(_pending_loads.items()):
        if time.monotonic() - since > LOAD_WAIT_SECONDS or _is_loaded(settings, key):
            _pending_loads.pop(key, None)
        else:
            raise StillLoading(key)


def _load(settings: Settings, root: str, key: str, context: int, timeout: httpx.Timeout) -> None:
    """Have LM Studio load ``key`` (with ``context`` tokens, or its default when 0). Raises ``httpx.HTTPError`` when
    LM Studio refused or never got the request, ``StillLoading`` when it may still be loading it."""
    _no_load_pending(settings)
    body = {"model": key, **({"context_length": context} if context else {})}
    try:
        http_method("post")(root + "/api/v1/models/load", json=body, headers=auth_headers(settings), timeout=timeout).raise_for_status()
        return
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        raise  # the request never reached LM Studio: nothing is loading
    except httpx.TransportError as exc:  # it took the request and didn't answer in time: the load may still be going
        deadline = time.monotonic() + LOAD_WAIT_SECONDS
        while True:
            if _is_loaded(settings, key):
                return
            if _gone(settings, root):
                raise  # LM Studio itself stopped, and its load with it
            if time.monotonic() >= deadline:
                break
            time.sleep(LOAD_POLL_SECONDS)
        _pending_loads[key] = time.monotonic()
        raise StillLoading(key) from exc


def wait_for_load(settings: Settings, key: str, seconds: float | None = None) -> bool:
    """Wait for a load LM Studio has under way (one a request started, or one CloseDesk stopped waiting for) to finish,
    for up to ``seconds`` (``LOAD_WAIT_SECONDS``): True once the model is in, False when LM Studio stopped or the time
    ran out."""
    root = lm_studio_root(settings.llm_base_url.rstrip("/"))
    deadline = time.monotonic() + (LOAD_WAIT_SECONDS if seconds is None else seconds)
    while True:
        if _is_loaded(settings, key):
            _pending_loads.pop(key, None)
            return True
        if time.monotonic() >= deadline or _gone(settings, root):
            return False
        time.sleep(LOAD_POLL_SECONDS)


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


def _fitting_context(settings: Settings, status: ModelStatus, key: str, *, want: int | None = None) -> int:
    from controller_inbox import model_fit  # it imports this module

    return model_fit.auto_context(settings, status, key, want=want)


def context_target(settings: Settings, status: ModelStatus | None = None) -> int:
    """The context the model should have: ``min_context_tokens``, but no more than the model supports, and for a
    model too big for that on this computer, the most that fits (``model_fit.auto_context``)."""
    status = status or check_model(settings)
    if status.key:
        return _fitting_context(settings, status, status.key)
    want = settings.min_context_tokens
    return min(want, status.max_context) if want and status.max_context else want


def context_capped(settings: Settings, status: ModelStatus | None = None) -> bool:
    """The model answering is asked for less than Setup's minimum (and the most it supports) because it is too big for
    this computer's memory with more."""
    status = status or check_model(settings)
    want = settings.min_context_tokens
    supported = min(want, status.max_context) if want and status.max_context else want
    return bool(status.key and want and context_target(settings, status) < supported)


def raise_tried(status: ModelStatus) -> bool:
    """CloseDesk has tried to raise the context of the model loaded now: not again, unless LM Studio has loaded it
    again since with another size than CloseDesk gave it."""
    if status.model not in _context_raised:
        return False
    given = _context_tried.get(status.model)
    return not given or given == status.context_length


def needs_more_context(settings: Settings) -> bool:
    """LM Studio loaded the model with less than the minimum and CloseDesk hasn't tried to raise it yet (nor has the
    model run out of memory answering with that much, as the user loaded it)."""
    status = check_model(settings)
    target = context_target(settings, status) if status.key else 0
    return bool(
        status.active
        and status.key
        and 0 < status.context_length < target
        and not raise_tried(status)
        and not 0 < _theirs_ran_out.get(status.key, 0) <= target
    )


def set_min_context(settings: Settings, tokens: int) -> str:
    """Change the minimum while running; a model that couldn't be raised before is tried again at the new size.

    A model CloseDesk loaded or raised with more than the new minimum is reloaded with it now (one it raised, with no
    less than it had before; at Off, with what it had before). A model the user loaded at a size they chose is left as
    it is. Returns what happened ("" when nothing did)."""
    settings.min_context_tokens = tokens
    _context_raised.clear()
    _context_tried.clear()
    _context_stepped.clear()
    _context_stepped_back.clear()
    _context_no_smaller.clear()
    _context_ran_out.clear()
    if not _context_given:  # nothing CloseDesk sized (at the start, always): LM Studio isn't asked
        return ""
    # Another load holds the lock for as long as LM Studio takes: say so rather than wait for it.
    if not _context_lock.acquire(timeout=CONTEXT_WAIT_SECONDS):
        return "Left the model as it is for now: LM Studio is loading a model. Save again once it is done."
    try:
        return _fit_to_minimum(settings, tokens)
    finally:
        _context_lock.release()


def _fit_to_minimum(settings: Settings, tokens: int) -> str:
    status = check_model(settings, use_cache=False)
    said = []
    for instance, (key, size) in status.instances.items():
        if not size or _context_given.get(key) != size:
            continue  # the user's own, or loaded again in LM Studio since
        before = _context_before.get(key, 0)
        # The new minimum, or what fits of it for a model too big for it here; never less than the user had.
        want = max(_fitting_context(settings, status, key, want=tokens), before) if tokens else before
        most = status.chat_models.get(key, 0)
        want = min(want, most) if want and most else want
        if not 0 < want < size:
            continue
        if model_busy(instance, key):
            said.append(f"Left {key} at {size:,} tokens for now: it is answering another request. Save again once it is done.")
            continue
        problem = _reload_at(settings, status, instance, key, want)
        if problem:
            said.append(problem)
            continue
        if want == before:  # as the user had it: theirs again
            _context_given.pop(key, None)
            _context_before.pop(key, None)
            _context_back.pop(key, None)
        else:
            _context_given[key] = want
            if _context_back.get(key, 0) >= want:
                _context_back.pop(key, None)
        said.append(f"Reloaded {key} in LM Studio with {'an' if str(want).startswith('8') else 'a'} {want:,}-token context.")
    return " ".join(said)


def _reload_at(settings: Settings, status: ModelStatus, instance: str, key: str, size: int) -> str:
    """Unload ``instance`` and load ``key`` again with ``size`` tokens. Returns what went wrong, or ""."""
    root = lm_studio_root(status.base_url)
    timeout = httpx.Timeout(max(settings.llm_timeout, 120.0), connect=5.0)
    try:
        _no_load_pending(settings)
        http_method("post")(root + "/api/v1/models/unload", json={"instance_id": instance}, headers=auth_headers(settings), timeout=timeout).raise_for_status()
    except StillLoading as exc:
        return str(exc)
    except httpx.HTTPError as exc:
        return f"Couldn't reload {key} with a {size:,}-token context ({_short_error(exc)})."
    try:
        _load(settings, root, key, size, timeout)
    except StillLoading as exc:
        return str(exc)
    except httpx.HTTPError as exc:
        _context_given.pop(key, None)
        _context_before.pop(key, None)
        _context_back.pop(key, None)
        return f"LM Studio couldn't load {key} again with a {size:,}-token context ({_lm_studio_said(exc)}). Load it again in LM Studio."
    finally:
        _status_cache.clear()
    return ""


def context_fits(settings: Settings, status: ModelStatus | None = None) -> bool:
    """Whether the loaded model still fits in this computer's memory with the context CloseDesk would raise it to, by
    model_roles' estimate. True when that can't be told: LM Studio doesn't give the model's size, it runs on another
    computer, or this computer's memory can't be read."""
    from controller_inbox import model_fit  # it imports this module

    status = status or check_model(settings)
    if not status.key:
        return True
    # Against the memory model_fit fits it into: on a PC, its graphics card when it runs there (a context past the
    # card's memory slows every answer), else the computer's memory, as Load and use loads it. With the context
    # CloseDesk asks for: Setup's minimum, or what fits of it for a big model.
    return model_fit.fits_with(settings, status, status.key, context_target(settings, status))


def _smaller_context(size: int) -> int:
    """The next of Setup's steps below ``size``, no smaller than 4,096 tokens; 0 when there is none."""
    from controller_inbox import model_fit  # it imports this module

    smaller = [step for step in model_fit._steps() if model_fit.SMALLEST_CONTEXT <= step < size]
    return max(smaller, default=0)


class StepBackRefused(Exception):
    """The model can't be loaded back smaller now: what to say instead (it changed, it is busy, LM Studio refused)."""


def _step_back_size(key: str, loaded: int) -> tuple[int, bool]:
    """The context a model CloseDesk sized at ``loaded`` tokens goes back to after running out of memory, and whether
    that is the size it last answered with: one CloseDesk raised goes back to the size it last answered with (rather
    than a step down that may not fit either); one CloseDesk loaded itself (at the start, or with Load and use), or one
    already loaded back, to the next smaller of Setup's steps, down to 4,096 tokens and never below the size the user
    had it at. Never under 4,096 tokens: a size the user had under that goes back to 4,096. 0 when there is none."""
    from controller_inbox import model_fit  # it imports this module

    floor = model_fit.SMALLEST_CONTEXT
    before = _context_before.get(key, 0)
    back = _context_back.get(key, 0)
    if 0 < before < floor:
        before = floor
    if 0 < back < floor:
        back = floor
    answered = 0 < back < loaded
    if not answered:
        back = max(_smaller_context(loaded), before)
    return (back if 0 < back < loaded else 0), answered


def step_back_offer(settings: Settings) -> dict | None:
    """An answer or a reading ran out of memory: the offer to load the model again with a smaller context, for the user
    to accept or not (``step_back_context`` does it). ``model``, ``loaded`` (the context that ran out), ``size`` (the
    one offered) and ``text``. None when the model's context isn't CloseDesk's doing (the user loaded it as it is:
    ``memory_advice`` says what to reload it with in LM Studio) or there is no smaller size to offer (at 4,096 tokens
    Settings then says it doesn't fit at all). Kept for Settings to offer the same until the model is loaded again."""
    try:
        status = check_model(settings, use_cache=False)
    except Exception:  # noqa: BLE001 - no offer, the memory advice still stands
        return None
    key, loaded = status.key, status.context_length
    if not (key and loaded > 0 and _context_given.get(key) == loaded):
        if key and loaded > 0:  # the user's own size: Settings says what to reload it with in LM Studio
            _theirs_ran_out[key] = loaded
        return None
    size, answered = _step_back_size(key, loaded)
    if not size:
        if not answered and not _smaller_context(loaded):  # already the smallest: it doesn't fit at all
            _context_no_smaller[key] = loaded
        return None
    _context_ran_out[key] = loaded
    return {"model": key, "loaded": loaded, "size": size, "text": step_back_text(key, loaded, size)}


def step_back_text(key: str, loaded: int, size: int) -> str:
    """The offer in words: what ran out of memory, and what reloading it smaller means."""
    from controller_inbox.agent import RECOMMENDED_CONTEXT  # it imports this module

    article = "an" if str(loaded).startswith("8") else "a"
    text = f"{key} ran out of memory with {article} {loaded:,}-token context. Reload it with {size:,}?"
    if size < RECOMMENDED_CONTEXT:
        text += " Long attachments are then read a section at a time."
    return text


def settings_step_back(settings: Settings, status: ModelStatus) -> dict | None:
    """The offer Settings shows after the model loaded now ran out of memory with the context CloseDesk gave it, and
    it hasn't been loaded again since; None otherwise."""
    key, loaded = status.key, status.context_length
    if not (key and loaded > 0 and _context_ran_out.get(key) == loaded and _context_given.get(key) == loaded):
        return None
    size, _answered = _step_back_size(key, loaded)
    if not size:
        return None
    return {"model": key, "loaded": loaded, "size": size, "text": step_back_text(key, loaded, size)}


def step_back_context(settings: Settings, key: str, size: int) -> str:
    """The user accepted the offer (``step_back_offer``): ``key``, which ran out of memory with the context CloseDesk
    gave it, is loaded again with ``size`` tokens. It isn't raised again until the minimum is changed in Settings.
    Returns what happened; raises ``StepBackRefused`` with what to say when the model loaded now isn't the one offered
    at the context that ran out, it is answering another request, or LM Studio couldn't load it."""
    if not _context_lock.acquire(timeout=CONTEXT_WAIT_SECONDS):
        raise StepBackRefused("LM Studio is loading a model right now. Try again once it is done.")
    try:
        status = check_model(settings, use_cache=False)
        loaded = status.context_length
        if status.key != key or not (loaded > 0 and _context_given.get(key) == loaded):
            raise StepBackRefused(f"{key} isn't loaded in LM Studio as it was, so nothing was reloaded. Ask again to see how it answers now.")
        back, answered = _step_back_size(key, loaded)
        if not back or back != size:
            article = "an" if str(loaded).startswith("8") else "a"
            raise StepBackRefused(f"{key} is loaded with {article} {loaded:,}-token context now, so nothing was reloaded. Ask again to see how it answers now.")
        if model_busy(status.model, key):  # unloading it would cut off another answer or the overnight reading
            raise StepBackRefused(f"{key} is answering another request. Once it is done, try again.")
        _context_raised.add(status.model)
        _context_tried.pop(status.model, None)  # ran out of memory: not raised again, however it is loaded next
        problem = _reload_at(settings, status, status.model, key, back)
        if problem:
            raise StepBackRefused(problem)
        _context_back.pop(key, None)
        _context_ran_out.pop(key, None)
        _context_stepped[key] = loaded
        before = _context_before.get(key, 0)
        if answered or back == before:
            _context_stepped_back.add(key)
        else:
            _context_stepped_back.discard(key)
        if back == before:  # as the user had it: theirs again
            _context_given.pop(key, None)
            _context_before.pop(key, None)
        else:
            _context_given[key] = back
        how = "back" if answered else "again"
        article = "an" if str(back).startswith("8") else "a"
        return f"Loaded {key} {how} with {article} {back:,}-token context: {loaded:,} didn't fit in this computer's memory."
    finally:
        _context_lock.release()


def step_back_asked(settings: Settings, data: dict) -> str:
    """The pages' "Reload and ask again" (the chat) and "Reload with ..." (Settings): ``data`` names the model and the
    context offered. Raises ValueError for a model or size the pages never send, and ``StepBackRefused`` as
    ``step_back_context`` does."""
    from controller_inbox import model_fit  # it imports this module

    key = str(data.get("model") or "").strip()
    size = data.get("size")
    if not key or len(key) > 200 or any(ch in key for ch in "\r\n\x00"):
        raise ValueError("Choose a model.")
    if not isinstance(size, int) or isinstance(size, bool) or size < model_fit.SMALLEST_CONTEXT:
        raise ValueError("Unknown context size.")
    if size not in model_fit._steps() and not _offered_now(settings, key, size):
        # Not one of Setup's steps: only the size the user had the model at, when that is what is offered.
        raise ValueError("Unknown context size.")
    return step_back_context(settings, key, size)


def _offered_now(settings: Settings, key: str, size: int) -> bool:
    """Whether ``size`` is the context offered for ``key`` as it is loaded now."""
    try:
        status = check_model(settings)
    except Exception:  # noqa: BLE001 - can't tell: not offered
        return False
    loaded = status.context_length
    return status.key == key and loaded > 0 and _step_back_size(key, loaded)[0] == size


def ensure_context(settings: Settings) -> str:
    """Reload LM Studio's model with at least ``min_context_tokens`` when it was loaded with less.

    Tried once per loaded instance, so a machine without the memory for it isn't asked again and again.
    If the longer load fails, the model is loaded back as it was. Returns what happened ("" when nothing did).
    """
    # Another model's load (a page reader's, a model chosen in Setup) can hold the lock for minutes while LM Studio is
    # watched for it: the question goes on with the context as it is rather than wait with no word.
    if not _context_lock.acquire(timeout=CONTEXT_WAIT_SECONDS):
        return (
            f"Left {check_model(settings).key} as it is for now: LM Studio is loading another model. "
            "The longer context is tried on a later question."
        )
    try:
        if not needs_more_context(settings):
            return ""
        return _reload_with_context(settings)
    finally:
        _context_lock.release()


def _reload_with_context(settings: Settings) -> str:
    status = check_model(settings)
    want = context_target(settings, status)
    if not context_fits(settings, status):
        _context_raised.add(status.model)  # said once
        _context_tried.pop(status.model, None)
        return f"Kept {status.key} at its {status.context_length:,}-token context: a {want:,}-token one wouldn't fit in this computer's memory."
    if model_busy(status.model, status.key):
        # Unloading it would cut off the overnight reading, a page read or another chat partway; tried again later.
        return f"Left {status.key} as it is for now: it is answering another request. The longer context is tried on a later question."
    try:
        _no_load_pending(settings)
    except StillLoading as exc:
        return str(exc)
    _context_raised.add(status.model)
    _context_tried.pop(status.model, None)
    root = lm_studio_root(status.base_url)
    timeout = httpx.Timeout(max(settings.llm_timeout, 120.0), connect=5.0)
    try:
        http_method("post")(root + "/api/v1/models/unload", json={"instance_id": status.model}, headers=auth_headers(settings), timeout=timeout).raise_for_status()
    except httpx.HTTPError as exc:
        return f"Couldn't reload {status.key} with a longer context ({_short_error(exc)})."
    errors = []
    try:
        for size in (want, status.context_length):
            try:
                _load(settings, root, status.key, size, timeout)
            except httpx.HTTPError as exc:
                errors.append(_short_error(exc))
                continue
            break
    except StillLoading as exc:  # not loaded again at the old size beside it
        return str(exc)
    finally:
        _status_cache.clear()
    if not errors:
        # The earliest: the size the user had it at. A model CloseDesk loaded itself (sized by it, with no size of the
        # user's kept) has none, so lowering the minimum or running out of memory goes on sizing it as CloseDesk's.
        ours = _context_given.get(status.key) == status.context_length and status.key not in _context_before
        if not ours:
            _context_before.setdefault(status.key, status.context_length)
        _context_back[status.key] = status.context_length  # the latest: the size it answered with before this
        _context_given[status.key] = want
        _context_tried[status.model] = want
        supported = min(settings.min_context_tokens, status.max_context) if status.max_context else settings.min_context_tokens
        most = (
            " (the most that fits this computer)" if want < supported
            else " (the most it supports)" if want < settings.min_context_tokens else ""
        )
        from controller_inbox.agent import RECOMMENDED_CONTEXT  # it imports the store and the rest

        article = "an" if str(want).startswith("8") else "a"
        # Whole attachments fit only with the recommended context or more; below it they are still read in sections.
        why = (
            " so whole attachments fit." if want >= RECOMMENDED_CONTEXT
            else ". Long attachments are still read a section at a time."
        )
        return f"Reloaded {status.key} in LM Studio with {article} {want:,}-token context{most}{why}"
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
        root = lm_studio_root(status.base_url)
        timeout = httpx.Timeout(max(settings.llm_timeout, 120.0), connect=5.0)
        key = status.instances[instance][0] if instance else model
        try:
            _load(settings, root, key, context, timeout)
        except StillLoading as exc:  # the shorter one isn't unloaded either: it is the one that is surely in
            return str(exc)
        except httpx.HTTPError as exc:
            problem = f"LM Studio couldn't load {key} with a {context:,}-token context ({_short_error(exc)})."
            _reader_failed[model] = (time.monotonic(), problem)
            return problem
        finally:
            _status_cache.clear()
        _reader_failed.pop(model, None)
        if instance:  # the shorter one, now that the longer one is loaded
            try:
                http_method("post")(root + "/api/v1/models/unload", json={"instance_id": instance}, headers=auth_headers(settings), timeout=timeout)
            except httpx.HTTPError:
                pass
        return ""


def reader_load_problem(model: str) -> str:
    """Why LM Studio couldn't load this page reader lately (it is asked again after ``READER_RETRY_SECONDS``), or ""."""
    failed = _reader_failed.get(model)
    return failed[1] if failed and time.monotonic() - failed[0] < READER_RETRY_SECONDS else ""


def forget_reader_failures() -> None:
    """Setup was saved: a model LM Studio couldn't load is asked again (memory may have been freed)."""
    _reader_failed.clear()


def _server_words(exc: httpx.HTTPError) -> str:
    """All of what the model server said about a failure, or "" when it said nothing: ``{"error": "..."}``,
    ``{"error": {"message": "..."}}``, ``{"message": "..."}`` (llama.cpp) or ``{"detail": "..."}``, a reply in plain
    text, or the error it wrote into a stream."""
    if isinstance(getattr(exc, "said", None), str):
        return exc.said
    if not isinstance(exc, httpx.HTTPStatusError):
        return ""
    try:
        data = exc.response.json()
    except (ValueError, httpx.StreamError):  # not JSON, or a streamed reply that wasn't read
        data = None
    if isinstance(data, dict):
        error = data.get("error")
        for said in (error.get("message") if isinstance(error, dict) else error, data.get("message"), data.get("detail")):
            if isinstance(said, str) and said.strip():
                return said.strip()
        return ""
    try:
        text = exc.response.text.strip()
    except httpx.StreamError:
        return ""
    return "" if data is not None or text.startswith("<") else text  # a web page isn't words to show


def _lm_studio_said(exc: httpx.HTTPError) -> str:
    """LM Studio's own reason for refusing, such as running short of memory, rather than the bare status code."""
    said = _server_words(exc)
    return said[:200] if said else _short_error(exc)


# Chat models LM Studio couldn't load lately, with when: not asked for again on CloseDesk's own (the start's automatic
# load, LM Studio loading the remembered model when a request names it) for a while. Choosing one in Settings still
# tries it.
_chat_load_failed: dict[str, float] = {}


def load_failed_lately(key: str) -> bool:
    failed = _chat_load_failed.get(key) if key else None
    return failed is not None and time.monotonic() - failed < READER_RETRY_SECONDS


def chat_load_size(settings: Settings, key: str) -> int:
    """The context to have LM Studio load ``key`` with before a question's first call, when it isn't loaded: Setup's
    minimum, or what fits of it here. Left to LM Studio otherwise, which loads it with its own default when the question
    names it (and the next question then unloads it and loads it again with the minimum: two loads, minutes each for a
    big model). 0 when it is left to LM Studio: not LM Studio, not one of its downloaded chat models, another chat model
    loaded, a load already under way, one that failed lately, or no minimum."""
    status = check_model(settings)
    if not (status.lm_studio and key in status.chat_models) or load_failed_lately(key):
        return 0
    reader = (settings.vision_model or "").strip()
    if any(
        not document_reader(model) and not document_reader(instance) and reader not in (model, instance)
        for instance, (model, _size) in status.instances.items()
    ):
        return 0
    if key in _pending_loads or model_busy(key):
        return 0
    return _fitting_context(settings, status, key)


def use_chat_model(settings: Settings, key: str, *, only_if_none: bool = False) -> str:
    """Make ``key`` (a downloaded model's key in LM Studio) the model that answers, chosen in Setup: the other chat
    models loaded are unloaded first, so two don't share the memory, then it is loaded with the context Setup asks
    for. Document readers, the page reader chosen in Setup and embedding models stay loaded. If LM Studio can't load it, the ones unloaded are loaded
    back. ``only_if_none`` (CloseDesk choosing one by itself): a chat model loaded meanwhile is left as it is, and
    nothing is loaded beside it. Returns what went wrong, or ""."""
    with _context_lock:
        status = check_model(settings, use_cache=False)
        if not status.reachable:
            return "LM Studio's server isn't answering. Start it, then try again."
        if not status.lm_studio:
            return "Only LM Studio loads models when CloseDesk asks. Load the model in your model server instead."
        if key not in status.chat_models:
            return f"LM Studio doesn't have {key} downloaded."
        root = lm_studio_root(status.base_url)
        timeout = httpx.Timeout(max(settings.llm_timeout, CHAT_TIMEOUT), connect=5.0)
        # The page reader chosen in Setup (a general vision model, not only a document reader) stays loaded too.
        reader = (settings.vision_model or "").strip() or None

        def load(model: str, size: int) -> None:
            _load(settings, root, model, size, timeout)

        def load_back(unloaded: list[tuple[str, int]]) -> str:
            back = []
            for model, size in unloaded:
                try:
                    load(model, size)
                    back.append(model)
                except httpx.HTTPError:
                    pass
                except StillLoading as exc:  # the rest aren't loaded beside it
                    return f" {exc}"
            if back:
                return f" {', '.join(back)} was loaded back."
            return " Load a model in LM Studio." if unloaded else ""

        others = [
            (instance, model, size)
            for instance, (model, size) in status.instances.items()
            if model != key and not document_reader(model) and not document_reader(instance) and reader not in (model, instance)
        ]
        if only_if_none and others:
            return f"{others[0][1]} was loaded in LM Studio meanwhile, so it was left as it is."
        busy = [model for instance, model, _size in others if model_busy(instance, model)]
        if busy:
            return f"CloseDesk is using {', '.join(busy)} for an answer or a reading right now. Try again once it is done."
        try:
            _no_load_pending(settings)
        except StillLoading as exc:
            return str(exc)
        unloaded: list[tuple[str, int]] = []
        try:
            for instance, model, size in others:
                try:
                    http_method("post")(
                        root + "/api/v1/models/unload", json={"instance_id": instance}, headers=auth_headers(settings), timeout=timeout
                    ).raise_for_status()
                except httpx.HTTPError as exc:
                    return f"LM Studio couldn't unload {model} to make room ({_lm_studio_said(exc)}).{load_back(unloaded)}"
                unloaded.append((model, size))
            if not any(model == key for model, _size in status.instances.values()):
                # Setup's minimum, or for a model too big for that on this computer the most that fits.
                size = _fitting_context(settings, status, key)
                try:
                    load(key, size)
                except StillLoading as exc:  # nothing is loaded back beside it
                    return str(exc)
                except httpx.HTTPError as exc:
                    _chat_load_failed[key] = time.monotonic()
                    said = _lm_studio_said(exc)
                    advice = f" {MEMORY_ADVICE}" if out_of_memory(said) else ""
                    return f"LM Studio couldn't load {key} ({said}).{load_back(unloaded)}{advice}"
                # Loaded by CloseDesk: lowering the minimum reloads it smaller. There is no size of the user's to go back to.
                _context_before.pop(key, None)
                _context_back.pop(key, None)
                if size:
                    _context_given[key] = size
                else:
                    _context_given.pop(key, None)
        finally:
            _status_cache.clear()
        _context_raised.discard(key)  # loaded afresh: a longer context may be tried again
        _context_tried.pop(key, None)
        _context_stepped.pop(key, None)
        _context_stepped_back.discard(key)
        _context_no_smaller.pop(key, None)
        _context_ran_out.pop(key, None)
        _theirs_ran_out.pop(key, None)
        _chat_load_failed.pop(key, None)
        remember_model(settings, key)
        return ""


def chat_with_tools(
    settings: Settings, messages: list[dict], tools: list[dict], *, max_tokens: int = 500, limit: float | None = None
) -> ToolReply:
    """One non-streamed turn that may ask for tools. Raises ``ToolsUnsupported`` when tools are refused. ``limit``: as
    for ``complete_text``."""
    model, effort, budget = _chat_plan(settings, max_tokens)
    if model in _tools_rejected:
        raise ToolsUnsupported(model)
    reply = ToolReply(content="")
    # Email and file text can't bring in a <tool_call> for the model to quote back as if it were its own.
    messages = [
        {**m, "content": _CALL_TAG_RE.sub(r"<\1tool call>", m["content"])}
        if m.get("role") in {"user", "tool"} and isinstance(m.get("content"), str) else m
        for m in messages
    ]
    for _attempt in range(2):
        url, payload = _chat_request(settings, messages, budget, stream=False)
        sent = budget
        payload["tools"] = tools
        if effort:
            payload["reasoning_effort"] = effort
        response = _post_chat(_model_http("post"), url, payload, settings, timeout=max(_timeout(settings, budget), limit or CHAT_TIMEOUT))
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
        plan = _retry_plan(settings, model, payload.get("reasoning_effort"), budget, Reply(reply.content, reply.reasoning, reply.finish))
        if plan is None:
            break
        effort, budget = plan
    raise _empty_reply(settings, model, sent, Reply(reply.content, reply.reasoning, reply.finish))


_TEXT_CALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*(?:</tool_call>|\Z)", re.S)
_CALL_TAG_RE = re.compile(r"<(/?)tool_call>", re.I)


def _arguments(text: str) -> dict:
    """Tool arguments are JSON text, and some servers encode that text a second time, as a JSON string."""
    value = text
    for _attempt in range(2):
        try:
            value = json.loads(value)
        except ValueError:
            break
        if not isinstance(value, str):
            break
    if isinstance(value, dict):
        return value
    return parse_json_object(value if isinstance(value, str) else text) or {}


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
            arguments = _arguments(arguments)
        calls.append({"id": str(item.get("id") or f"call_{index}"), "name": str(function["name"]), "arguments": arguments or {}})
    content = base.content
    if not calls and "<tool_call>" in content:
        # Some chat templates leave the call in the text instead of tool_calls.
        for index, raw in enumerate(_TEXT_CALL_RE.findall(content)):
            parsed = parse_json_object(raw) or {}
            if parsed.get("name"):
                arguments = parsed.get("arguments") or parsed.get("parameters") or {}
                if isinstance(arguments, str):
                    arguments = _arguments(arguments)
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
        bits.append("amounts " + ", ".join(fmt_money(float(a)) for a in extracted["amounts"][:5]))
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


def auth_headers(settings: Settings) -> dict[str, str]:
    """The headers every request to the model server sends: its API key, when it has one."""
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


# How LM Studio (llama.cpp, MLX) and other servers say the model ran short of memory: LM Studio's bare "Compute error"
# is a Mac's graphics memory running out partway through an answer, most often with a model too big for it. With a
# large model: llama.cpp's "llama_decode returned -3" (the answer couldn't be worked out in the memory there was),
# Vulkan's "ErrorOutOfDeviceMemory", MLX's "greater than the maximum allowed buffer size", C++'s "bad_alloc", and
# LM Studio's "has crashed" or an exit code near 2^64 (a Windows crash code, most often memory running out).
_MEMORY_RE = re.compile(
    r"compute\W*error|out\W*of\W*(?:device\W*|gpu\W*|host\W*)?memory|insufficient (?:system |gpu )?memory|"
    r"not enough (?:system |gpu )?memory|failed to allocate|unable to allocate|cannot allocate|alloc(?:ation)? failed|"
    r"memory (?:is )?exhausted|kiogpucommandbuffercallbackerror|insufficient system resources|"
    r"requires? more (?:system |gpu )?memory|llama_decode returned -3\b|maximum allowed buffer size|bad_alloc|"
    r"model has crashed|exit code:?\s*18446744\d{12}",
    re.I,
)
MEMORY_ADVICE = (
    "The model is likely too big for this computer's memory. CloseDesk only needs a small one: a 3B to 8B model is "
    "plenty and answers faster. Choose one in Settings, or lower the minimum context there to 8k: a model CloseDesk "
    "loaded is reloaded with it (one you loaded in LM Studio, reload there with a smaller Context Length)."
)


def memory_advice(settings: Settings, offer: dict | None = None) -> str:
    """``MEMORY_ADVICE`` fitted to the model loaded in LM Studio: a shorter minimum context is offered only when it
    would reload the model with less (one CloseDesk sized), and one the user loaded is told the Context Length to
    reload it with in LM Studio (the most that fits here, when CloseDesk can tell). With ``offer`` (the chat's offer
    to load it again smaller, ``step_back_offer``, shown below the note) it points to that, and to nothing else that
    makes the same fix: the model may well fit with the smaller context."""
    if offer:
        return (
            f"CloseDesk can load {offer['model']} again with a smaller context: see below. Or choose a smaller model in "
            "Settings: a 3B to 8B model is plenty and answers faster."
        )
    try:
        status = check_model(settings)
    except Exception:  # noqa: BLE001 - the advice is said all the same
        return MEMORY_ADVICE
    key, loaded = status.key, status.context_length
    if not (status.lm_studio and key and loaded > 0):
        return MEMORY_ADVICE
    first = (
        "The model is likely too big for this computer's memory. CloseDesk only needs a small one: a 3B to 8B model "
        "is plenty and answers faster."
    )
    if _context_given.get(key) == loaded:  # sized by CloseDesk: lowering the minimum reloads it, when that shortens it
        before = _context_before.get(key, 0)
        for step in (8192, 4096):
            if 0 < max(_fitting_context(settings, status, key, want=step), before) < loaded:
                return (
                    f"{first} Choose one in Settings, or lower the minimum context there to {step // 1024}k: CloseDesk "
                    f"reloads {key} with it."
                )
        return f"{first} Choose one in Settings."
    target = context_target(settings, status)
    if 0 < target < loaded:
        return f"{first} Choose one in Settings, or reload {key} in LM Studio with Context Length {target:,}, the most that fits here."
    # Loaded with no more than CloseDesk's estimate says fits, and it ran out all the same: the next size down.
    if smaller := _smaller_context(loaded):
        return f"{first} Choose one in Settings, or reload {key} in LM Studio with Context Length {smaller:,}."
    return f"{first} Choose one in Settings."


def out_of_memory(text: str) -> bool:
    """Whether a model server's error says it ran short of memory."""
    return bool(_MEMORY_RE.search(text or ""))


def ran_out_of_memory(exc: Exception) -> bool:
    """Whether a model's failure was the server running short of memory, by what it said (all of it: a long Metal or
    CUDA message can name the memory only at its end)."""
    said = _server_words(exc) if isinstance(exc, httpx.HTTPError) else ""
    return out_of_memory(said) or out_of_memory(getattr(exc, "said", "")) or out_of_memory(str(exc))


def failure_note(exc: Exception, settings: Settings | None = None, offer: dict | None = None) -> str:
    """Why the chat's model didn't answer (it wrote nothing), in words a person can act on. With ``settings``, the
    memory advice is fitted to the model loaded and to ``offer`` (``memory_advice``)."""
    if ran_out_of_memory(exc):
        advice = memory_advice(settings, offer) if settings is not None else MEMORY_ADVICE
        return "The local model ran out of memory before it could answer. " + advice
    if isinstance(exc, ThinksTooLong):
        return f"The local model didn't answer. {exc}."
    if isinstance(exc, httpx.TimeoutException) and getattr(exc, "reply_started", ""):
        doing = "still thinking" if exc.reply_started == "thinking" else "still writing its answer"
        return (
            f"The local model was {doing} when it went quiet for longer than CloseDesk waits between words: a big "
            "model on a busy computer can pause. Ask again; on a slow computer, give it longer in Settings → Local model."
        )
    if isinstance(exc, httpx.TimeoutException):
        return (
            "The local model took too long to start answering: LM Studio may still be loading it or reading a long "
            "prompt. Ask again in a minute; on a slow computer, give it longer in Settings → Local model."
        )
    # The server's own words (LM Studio's "Model is reloading"), not httpx's "Server error '500 ...' for url ...".
    said = _lm_studio_said(exc) if isinstance(exc, httpx.HTTPError) else str(exc)
    return f"The local model didn't answer ({' '.join(said.split())[:120]})."
