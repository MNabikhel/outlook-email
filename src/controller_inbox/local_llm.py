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
        ids = [str(item.get("id")) for item in response.json().get("data", []) if item.get("id")]
        status.reachable = True
        status.models = ids
        status.model = _pick_model(settings.llm_model, ids)
    except (httpx.HTTPError, ValueError, AttributeError, TypeError) as exc:
        status.error = _short_error(exc)
    _status_cache[key] = (time.monotonic(), status)
    return status


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


def _pick_model(requested: str, ids: list[str]) -> str:
    if requested in ids:
        return requested
    if not ids:
        return "" if requested in {"", "local-model"} else requested
    usable = [item for item in ids if "embed" not in item.lower()]
    return (usable or ids)[0]


class ModelUnavailable(RuntimeError):
    pass


@dataclass
class ReaderStats:
    read: int = 0
    failed: int = 0
    seconds: float = 0.0
    stopped_reason: str = ""

    @property
    def average_seconds(self) -> float:
        return self.seconds / self.read if self.read else 0.0


class LocalReader:
    """One reader per run. Holds the resolved model and stops on a dead server."""

    def __init__(self, settings: Settings, *, model: str | None = None, client: httpx.Client | None = None):
        self.settings = settings
        self.model = model or resolve_model(settings)
        self.client = client or httpx.Client(timeout=settings.llm_timeout)
        self.stats = ReaderStats()
        self._structured = True
        self._consecutive_failures = 0

    @property
    def stopped(self) -> bool:
        return bool(self.stats.stopped_reason)

    def read(self, packet: dict) -> dict | None:
        if self.stopped:
            return None
        started = time.monotonic()
        try:
            content = self._complete(build_prompt(packet, budget=self.settings.llm_max_prompt_chars))
        except ModelUnavailable as exc:
            self.stats.stopped_reason = str(exc)
            return None
        except httpx.HTTPError as exc:
            return self._failed(_short_error(exc))
        parsed = parse_json_object(content)
        if not parsed or ("category" not in parsed and "folder" not in parsed):
            return self._failed("reply was not the JSON shape")
        self._consecutive_failures = 0
        self.stats.read += 1
        self.stats.seconds += time.monotonic() - started
        return parsed

    def _failed(self, reason: str) -> None:
        self.stats.failed += 1
        self._consecutive_failures += 1
        if self._consecutive_failures >= 3:
            self.stats.stopped_reason = f"three messages in a row failed ({reason})"
        return None

    def _complete(self, user: str) -> str:
        url = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": self.settings.llm_max_tokens,
            "messages": [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user},
            ],
        }
        if self._structured:
            payload["response_format"] = {"type": "json_schema", "json_schema": _schema()}
        try:
            response = self.client.post(url, json=payload, headers=_headers(self.settings))
            if self._structured and response.status_code in {400, 404, 415, 422, 500, 501}:
                # Older LM Studio / Ollama builds reject response_format. Ask again, plain.
                self._structured = False
                payload.pop("response_format", None)
                response = self.client.post(url, json=payload, headers=_headers(self.settings))
            response.raise_for_status()
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError) as exc:
            raise ModelUnavailable(f"the local model server stopped answering ({_short_error(exc)})") from exc
        except httpx.ReadTimeout as exc:
            if self._consecutive_failures >= 1:
                raise ModelUnavailable(
                    f"the model took longer than {self.settings.llm_timeout:.0f}s twice in a row"
                ) from exc
            raise
        try:
            return strip_thinking(str(response.json()["choices"][0]["message"]["content"] or ""))
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise httpx.DecodingError(f"unexpected reply: {exc}") from exc


def _chat_request(settings: Settings, messages: list[dict], max_tokens: int, *, stream: bool) -> tuple[str, dict]:
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": check_model(settings).model or settings.llm_model,
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


def _content(choice: dict, key: str) -> str:
    part = choice.get(key)
    text = part.get("content") if isinstance(part, dict) else None
    return text if isinstance(text, str) else ""


_THINK_OPEN, _THINK_CLOSE = "<think>", "</think>"
_THINK_RE = re.compile(r"<think>.*?(?:</think>|\Z)", re.S)


def strip_thinking(text: str) -> str:
    """Reasoning models (Qwen3, DeepSeek-R1) put their scratch work in <think> tags."""
    return _THINK_RE.sub("", text or "").strip()


class ThinkFilter:
    """``strip_thinking`` for a stream, where a tag can arrive split across pieces."""

    def __init__(self) -> None:
        self.buffer = ""
        self.inside = False
        self.started = False

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
            self.inside = True
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


def complete_text(settings: Settings, messages: list[dict], *, max_tokens: int = 400) -> str:
    """One plain-text answer. Raises ``httpx.HTTPError`` when the server fails."""
    url, payload = _chat_request(settings, messages, max_tokens, stream=False)
    response = httpx.post(url, json=payload, headers=_headers(settings), timeout=settings.llm_timeout)
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as exc:
        raise httpx.DecodingError(f"unexpected reply: {exc}") from exc
    return strip_thinking(_content(_first_choice(data), "message"))


def stream_text(settings: Settings, messages: list[dict], *, max_tokens: int = 500):
    """Yield the answer as it is written. Servers that ignore ``stream`` send it in one piece."""
    url, payload = _chat_request(settings, messages, max_tokens, stream=True)
    timeout = httpx.Timeout(settings.llm_timeout, connect=5.0)
    with httpx.stream("POST", url, json=payload, headers=_headers(settings), timeout=timeout) as response:
        response.raise_for_status()
        if "text/event-stream" not in response.headers.get("content-type", ""):
            try:
                data = json.loads(response.read() or b"{}")
            except ValueError as exc:
                raise httpx.DecodingError(f"unexpected reply: {exc}") from exc
            text = strip_thinking(_content(_first_choice(data), "message"))
            if text:
                yield text
            return
        thinking = ThinkFilter()
        for line in response.iter_lines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                choice = _first_choice(json.loads(chunk))
            except ValueError:
                continue
            piece = thinking.feed(_content(choice, "delta") or _content(choice, "message"))
            if piece:
                yield piece
        rest = thinking.flush()
        if rest:
            yield rest


def read_packet(settings: Settings, packet: dict) -> dict | None:
    """One-off read. Runs that read many messages should share a LocalReader."""
    if not llm_active(settings):
        return None
    return LocalReader(settings).read(packet)


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
    text = (content or "").strip()
    if not text:
        return None
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.M)
    start = text.find("{")
    while start != -1:
        chunk = _balanced(text, start)
        if chunk:
            for candidate in (chunk, re.sub(r",\s*([}\]])", r"\1", chunk)):
                try:
                    data = json.loads(candidate)
                except json.JSONDecodeError:
                    continue
                if isinstance(data, dict):
                    return data
        start = text.find("{", start + 1)
    return None


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
