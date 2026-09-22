"""Optional local model. Rules still extract amounts, dates, and fraud signals.

Point this at Ollama, or at any other local agent that speaks the OpenAI chat API
(a Bionic-style local agent included). It only suggests a category when the
rules are unsure. It cannot clear a payment-instruction warning.
"""

from __future__ import annotations

import json
import re

import httpx

from controller_inbox.config import Settings
from controller_inbox.models import DocumentType


SYSTEM = (
    "You classify finance email for an inbox assistant. "
    "Reply with JSON only: {\"category\": \"...\", \"why\": \"...\"}. "
    "Use only the category ids you are given. Prefer the user's saved corrections when the sender matches."
)


def suggest_category(
    settings: Settings,
    *,
    subject: str,
    body: str,
    filenames: list[str],
    examples: list[dict],
) -> dict | None:
    if not settings.llm:
        return None
    allowed = [item.value for item in DocumentType]
    example_lines = [
        f"- From {row['sender_email'] or 'unknown'}, subject {row['subject']!r} is {row['corrected_category']} because {row['reason']}"
        for row in examples[:8]
    ]
    user = "\n".join(
        [
            "Allowed categories: " + ", ".join(allowed),
            "Saved corrections:",
            *(example_lines or ["- none yet"]),
            "",
            f"Subject: {subject}",
            "Files: " + ", ".join(filenames or ["none"]),
            "Body:",
            (body or "")[:4000],
        ]
    )
    payload = {
        "model": settings.llm_model,
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
        response = httpx.post(url, json=payload, headers=headers, timeout=25)
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
        return None
    parsed = _parse_json(content)
    if not parsed:
        return None
    category = str(parsed.get("category") or "").strip()
    if category not in allowed:
        return None
    why = str(parsed.get("why") or "local model suggestion").strip()[:300]
    return {"category": category, "why": why}


def _parse_json(content: str) -> dict | None:
    text = content.strip()
    fence = re.search(r"\{.*\}", text, re.S)
    if fence:
        text = fence.group(0)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None
