from datetime import datetime, timezone

import pytest

from controller_inbox.config import Settings
from controller_inbox.llm import Enrichment, enrich_email, parse_json_object
from controller_inbox.models import DocumentType, Importance, TriageBin
from controller_inbox.pipeline import ingest_demo, process_message
from controller_inbox.store import Store


class FakeClient:
    """Stand-in for a local LM Studio / Ollama / Bionic server."""

    model = "fake-local"

    def __init__(self, response: dict | Exception):
        self.response = response
        self.calls = 0

    def chat_json(self, system: str, user: str) -> dict:
        self.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _record(store: Store, settings: Settings, as_of_now, email_id: str):
    ingest_demo(store, settings, now=as_of_now)
    return store.get_email(email_id)


def test_parse_json_object_handles_fences_and_prose():
    assert parse_json_object('{"a": 1}') == {"a": 1}
    assert parse_json_object("```json\n{\"a\": 2}\n```") == {"a": 2}
    assert parse_json_object('Sure! Here you go: {"a": 3} hope that helps') == {"a": 3}
    with pytest.raises(ValueError):
        parse_json_object("no json here")


def test_enrich_falls_back_to_rules_when_llm_disabled(store, settings, as_of_now):
    rec = _record(store, settings, as_of_now, "demo-inv-10482")
    out = enrich_email(rec, settings)  # settings.llm defaults False
    assert isinstance(out, Enrichment)
    assert out.source == "rules"
    assert out.triage_bin == TriageBin.ACTION_REQUIRED
    assert out.summary


def test_enrich_uses_llm_summary_and_soft_bin(store, settings, as_of_now):
    rec = _record(store, settings, as_of_now, "demo-po")  # purchase order, no hard action
    llm_settings = Settings(data_dir=settings.data_dir, llm=True, _env_file=None)
    client = FakeClient(
        {
            "summary": "Purchase order PO-77821 to Northwind for $12,850.",
            "bin": "fyi",
            "highlights": ["PO-77821", "$12,850"],
            "suggested_actions": ["File the PO"],
        }
    )
    out = enrich_email(rec, llm_settings, client=client)
    assert client.calls == 1
    assert out.source == "llm"
    assert out.summary.startswith("Purchase order PO-77821")
    # No rule action on this email, so the model may move it to a soft bin.
    assert out.triage_bin == TriageBin.FYI


def test_llm_cannot_override_fraud(store, settings, as_of_now):
    rec = _record(store, settings, as_of_now, "demo-bec-wire")
    llm_settings = Settings(data_dir=settings.data_dir, llm=True, _env_file=None)
    client = FakeClient({"summary": "Looks routine.", "bin": "fyi"})
    out = enrich_email(rec, llm_settings, client=client)
    assert out.triage_bin == TriageBin.FRAUD_REVIEW


def test_llm_cannot_downgrade_real_action(store, settings, as_of_now):
    rec = _record(store, settings, as_of_now, "demo-inv-10482")
    llm_settings = Settings(data_dir=settings.data_dir, llm=True, _env_file=None)
    client = FakeClient({"summary": "Just an invoice.", "bin": "read_later"})
    out = enrich_email(rec, llm_settings, client=client)
    assert out.triage_bin == TriageBin.ACTION_REQUIRED


def test_enrich_survives_llm_error(store, settings, as_of_now):
    rec = _record(store, settings, as_of_now, "demo-inv-10482")
    llm_settings = Settings(data_dir=settings.data_dir, llm=True, _env_file=None)
    client = FakeClient(RuntimeError("connection refused"))
    out = enrich_email(rec, llm_settings, client=client)
    assert out.source == "rules_llm_unreachable"
    assert out.triage_bin == TriageBin.ACTION_REQUIRED
    assert out.summary


def test_pipeline_threads_llm_client(store, settings, as_of_now):
    llm_settings = Settings(data_dir=settings.data_dir, llm=True, _env_file=None)
    client = FakeClient(
        {"summary": "AI summary here.", "bin": "review", "highlights": [], "suggested_actions": []}
    )
    from controller_inbox.demo import demo_messages

    raw = {m.id: m for m in demo_messages(as_of_now)}["demo-po"]
    rec = process_message(raw, store, llm_settings, now=as_of_now, llm_client=client)
    assert rec.ai_source == "llm"
    assert rec.summary == "AI summary here."
    assert rec.triage_bin == TriageBin.REVIEW
