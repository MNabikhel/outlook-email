"""Local model reader. LM Studio (Bionic) speaks this API on port 1234.

Scripts already extracted text, amounts, dates, and file types. This call
asks the model to decide the category, folder, importance, one-line summary,
and action items. It is off unless CONTROLLER_INBOX_LLM=true, so an overnight
run with no model still leaves a script draft in the right folder.
"""

from __future__ import annotations

import json
import re

import httpx

from controller_inbox.config import Settings
from controller_inbox.models import FOLDERS, DocumentType, Importance


SYSTEM = (
    "You are the overnight reader for CloseDesk, a local inbox assistant. "
    "Fast scripts already extracted text, amounts, dates, invoice numbers, and file types. "
    "Do not invent amounts, dates, or account numbers that are not in the packet. "
    "Do not re-read raw files; the packet is the document. "
    "Reply with JSON only, no markdown: "
    '{"category":"...","folder":"important|informational|reference","importance":"critical|high|medium|low",'
    '"summary":"one sentence","actions":[{"title":"...","due":"YYYY-MM-DD or null","priority":"high"}],'
    '"why":"one sentence"}. '
    "important = needs a decision or a task. informational = FYI, no task. "
    "reference = keep the file, not an overnight task. "
    "Never put a payment-instruction change or a fraud warning in informational or reference. "
    "When a saved correction matches the sender, follow it unless this message is a new payment-instruction change. "
    "Use only the category ids you are given."
)


def read_packet(settings: Settings, packet: dict) -> dict | None:
    if not settings.llm:
        return None
    allowed = [item.value for item in DocumentType]
    folders = ", ".join(FOLDERS)
    levels = ", ".join(item.value for item in Importance)
    corrections = packet.get("saved_corrections_for_sender") or []
    correction_lines = [
        f"- {row.get('category')}: {row.get('reason')} (subject {row.get('subject')})"
        for row in corrections
    ]
    user = "\n".join(
        [
            "Allowed categories: " + ", ".join(allowed),
            "Allowed folders: " + folders,
            "Allowed importance: " + levels,
            "Saved corrections for this sender:",
            *(correction_lines or ["- none"]),
            "",
            "Packet:",
            json.dumps(packet, ensure_ascii=False)[:14000],
        ]
    )
    model = resolve_model(settings)
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user},
        ],
    }
    headers = {"Content-Type": "application/json"}
    if settings.openai_api_key and "openai.com" in settings.llm_base_url:
        headers["Authorization"] = f"Bearer {settings.openai_api_key}"
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    try:
        response = httpx.post(url, json=payload, headers=headers, timeout=90)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
        return None
    parsed = _parse_json(content)
    if not parsed or "category" not in parsed and "folder" not in parsed:
        return None
    return parsed


def resolve_model(settings: Settings) -> str:
    """Use the configured model, or the one LM Studio currently has loaded."""
    requested = settings.llm_model
    url = settings.llm_base_url.rstrip("/") + "/models"
    try:
        response = httpx.get(url, timeout=3)
        response.raise_for_status()
        ids = [item.get("id") for item in response.json().get("data", []) if item.get("id")]
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        return requested
    if requested in ids:
        return requested
    if ids and (requested in {"", "local-model"} or requested not in ids):
        return str(ids[0])
    return requested


def _parse_json(content: str) -> dict | None:
    text = (content or "").strip()
    fence = re.search(r"\{.*\}", text, re.S)
    if fence:
        text = fence.group(0)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
