"""Local-LLM enrichment (LM Studio / Ollama / Bionic / any OpenAI-compatible server).

CloseDesk stays rules-first: the deterministic classifier decides the finance
category, importance, fraud flags, and action items. This module *adds* a
plain-English summary and can refine the soft triage bin using a model that runs
entirely on the user's own machine. Nothing here ever downgrades a fraud alert or
drops a real action item, and every network call fails soft back to the
deterministic result so the pipeline keeps working with no model running.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import httpx

from controller_inbox.config import Settings
from controller_inbox.models import (
    DOCUMENT_LABELS,
    IMPORTANCE_LABELS,
    EmailRecord,
    TriageBin,
)
from controller_inbox.triage import (
    coerce_bin,
    derive_triage_bin,
    deterministic_highlights,
    deterministic_summary,
    reconcile_bin,
)

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

SYSTEM_PROMPT = (
    "You are CloseDesk, an assistant for a corporate assistant-controller's email inbox. "
    "You receive one email that has already been classified by deterministic finance rules. "
    "Write a tight, factual summary and decide which triage bin it belongs in. "
    "Never invent amounts, dates, invoice numbers, or senders. "
    "Respond with a single JSON object and nothing else."
)

_BIN_GUIDE = (
    "action_required = the reader must do something (pay, approve, reconcile, respond); "
    "review = worth a look but no clear task yet; "
    "fyi = informational, no action; "
    "read_later = low-value reading such as newsletters."
)


@dataclass
class Enrichment:
    summary: str
    triage_bin: TriageBin
    highlights: list[str] = field(default_factory=list)
    suggested_actions: list[str] = field(default_factory=list)
    source: str = "rules"
    model: str = ""

    @property
    def used_llm(self) -> bool:
        return self.source.startswith("llm")


class LocalLLMClient:
    """Minimal OpenAI-compatible chat client aimed at local model servers."""

    def __init__(self, settings: Settings, *, http_client: httpx.Client | None = None):
        self.base_url = settings.llm_endpoint
        self.model = settings.llm_model
        self.api_key = settings.llm_key
        self.timeout = settings.llm_timeout
        self._http = http_client

    def _client(self) -> httpx.Client:
        return self._http or httpx.Client(timeout=self.timeout)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def list_models(self) -> list[str]:
        """GET /models — used by the `llm-check` connectivity probe."""
        client = self._client()
        try:
            resp = client.get(f"{self.base_url}/models", headers=self._headers())
            resp.raise_for_status()
            data = resp.json()
            return [item.get("id", "") for item in data.get("data", []) if item.get("id")]
        finally:
            if self._http is None:
                client.close()

    def chat_json(self, system: str, user: str) -> dict:
        """One chat completion, parsed leniently into a JSON object."""
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "max_tokens": 500,
            "stream": False,
        }
        client = self._client()
        try:
            resp = client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            resp.raise_for_status()
            body = resp.json()
        finally:
            if self._http is None:
                client.close()
        content = body["choices"][0]["message"]["content"]
        return parse_json_object(content)


def parse_json_object(content: str) -> dict:
    """Local models often wrap JSON in prose or ```json fences; recover the object."""
    if not content:
        raise ValueError("empty completion")
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = _JSON_OBJECT_RE.search(content)
    if match:
        return json.loads(match.group(0))
    raise ValueError("no JSON object in completion")


def build_prompt(record: EmailRecord) -> str:
    """Compact, fact-dense prompt so small local models stay grounded."""
    fields = record.extracted
    lines = [
        f"Subject: {record.subject}",
        f"From: {record.sender_name} <{record.sender_email}>",
        f"Rule-based category: {DOCUMENT_LABELS.get(record.category, record.category.value)}",
        f"Rule-based importance: {IMPORTANCE_LABELS.get(record.importance, record.importance.value)}",
    ]
    if record.flags:
        lines.append(f"Rule flags: {', '.join(record.flags)}")
    facts = []
    if fields.primary_invoice:
        facts.append(f"invoice {fields.primary_invoice}")
    if fields.primary_amount is not None:
        facts.append(f"amount ${fields.primary_amount:,.2f}")
    if fields.primary_due:
        facts.append(f"due {fields.primary_due}")
    if facts:
        lines.append("Extracted facts: " + ", ".join(facts))
    if record.attachments:
        names = ", ".join(
            f"{att.filename} [{DOCUMENT_LABELS.get(att.document_type, att.document_type.value)}]"
            for att in record.attachments
        )
        lines.append(f"Attachments: {names}")
    body = (record.body_text or "").strip()
    if body:
        lines.append("Body:\n" + body[:2500])
    for att in record.attachments:
        text = (att.extracted_text or "").strip()
        if text:
            lines.append(f"Attachment text ({att.filename}):\n" + text[:1200])
    lines.append("")
    lines.append(
        "Return JSON with keys: summary (<=2 sentences, plain English), "
        "bin (one of action_required, review, fyi, read_later), "
        "highlights (array of <=4 short strings), "
        "suggested_actions (array of <=3 short imperative strings). "
        f"Bin guide: {_BIN_GUIDE}"
    )
    return "\n".join(lines)


def enrich_email(
    record: EmailRecord,
    settings: Settings,
    *,
    client: LocalLLMClient | None = None,
) -> Enrichment:
    """Return summary + triage bin for one email, using the local model when enabled."""
    has_actions = bool(record.actions)
    rules_bin = derive_triage_bin(
        category=record.category,
        importance=record.importance,
        flags=record.flags,
        has_actions=has_actions,
    )
    fallback = Enrichment(
        summary=deterministic_summary(record),
        triage_bin=rules_bin,
        highlights=deterministic_highlights(record),
        suggested_actions=[a.title for a in record.actions][:3],
        source="rules",
        model="",
    )
    if not settings.llm_configured:
        return fallback

    llm = client or LocalLLMClient(settings)
    try:
        data = llm.chat_json(SYSTEM_PROMPT, build_prompt(record))
    except Exception:
        # Model offline, timed out, or returned junk: keep the deterministic result.
        fallback.source = "rules_llm_unreachable"
        return fallback

    summary = str(data.get("summary") or "").strip() or fallback.summary
    llm_bin = coerce_bin(data.get("bin"))
    final_bin = reconcile_bin(rules_bin, llm_bin, has_actions=has_actions)
    highlights = _string_list(data.get("highlights")) or fallback.highlights
    suggested = _string_list(data.get("suggested_actions")) or fallback.suggested_actions
    return Enrichment(
        summary=summary[:600],
        triage_bin=final_bin,
        highlights=highlights[:4],
        suggested_actions=suggested[:3],
        source="llm",
        model=llm.model,
    )


def _string_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        text = str(item).strip()
        if text:
            out.append(text[:200])
    return out
