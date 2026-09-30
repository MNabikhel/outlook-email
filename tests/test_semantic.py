"""Search by meaning: a local embedding model finds mail that says the same thing in other words."""

from __future__ import annotations

import math
import re

import httpx
import pytest

from controller_inbox import agent, semantic
from controller_inbox.assistant import pick_sources

# Each dimension is one idea; the fake model maps words onto them the way a real one maps meaning.
CONCEPTS = [
    {"trip", "offsite", "travel", "retreat"},
    {"portugal", "lisbon", "porto"},
    {"bank", "remittance", "account", "payments"},
    {"quote", "acme", "support"},
    {"budget", "totals", "q4"},
]
MODEL = "text-embedding-nomic-embed-text-v1.5"


class _FakeEmbeddings:
    """LM Studio with a chat model and an embedding model loaded."""

    def __init__(self, *, embedding_loaded: bool = True):
        self.embedding_loaded = embedding_loaded
        self.inputs: list[str] = []
        self.calls = 0

    def get(self, url, **_kwargs):
        request = httpx.Request("GET", url)
        models = [{"type": "llm", "key": "qwen/qwen3-4b", "loaded_instances": [{"id": "qwen/qwen3-4b"}]}]
        if self.embedding_loaded:
            models.append({"type": "embedding", "key": MODEL, "loaded_instances": [{"id": MODEL}]})
        return httpx.Response(200, request=request, json={"models": models})

    def post(self, url, json=None, **_kwargs):
        assert url.endswith("/v1/embeddings") and json["model"] == MODEL
        self.calls += 1
        self.inputs += json["input"]
        data = [{"index": i, "embedding": _vector(text)} for i, text in enumerate(json["input"])]
        return httpx.Response(200, request=httpx.Request("POST", url), json={"data": data})


def _vector(text: str) -> list[float]:
    words = set(re.findall(r"[a-z0-9]+", text.lower()))
    return [float(len(words & concept)) for concept in CONCEPTS] + [0.3]


@pytest.fixture(autouse=True)
def _model_on(settings):
    settings.llm = True


@pytest.fixture
def server(monkeypatch):
    fake = _FakeEmbeddings()
    monkeypatch.setattr(semantic.httpx, "get", fake.get)
    monkeypatch.setattr(semantic.httpx, "post", fake.post)
    return fake


def test_mail_is_found_by_meaning_when_no_words_match(store, settings, mail, server):
    budget, scam = mail["Q4 budget draft"], mail["Updated remittance details"]
    assert store.search_ranked(["team", "trip", "portugal"], limit=5) == [], "keyword search misses it"

    added = semantic.index_mail(store, settings)
    assert added >= 5 and server.inputs[0].startswith("search_document: ")
    assert not any("5566778899" in text for text in server.inputs), "files on flagged mail aren't indexed"

    found = semantic.search(store, settings, "team trip in Portugal")
    assert [email.id for email in found] == [budget.id], "the offsite memo mentions Lisbon, not Portugal"
    assert server.inputs[-1] == "search_query: team trip in Portugal"
    assert [email.id for email in semantic.search(store, settings, "changed bank account")] == [scam.id]
    assert semantic.search(store, settings, "weather forecast") == [], "nothing close enough"

    calls = server.calls
    assert semantic.index_mail(store, settings) == 0 and server.calls == calls, "nothing new to index"
    budget.body_text = "The Q4 budget draft moved to the shared drive."
    store.upsert_email(budget)
    assert semantic.index_mail(store, settings) == 1, "only the changed email is indexed again"


def test_chat_and_the_search_tool_add_mail_close_in_meaning(store, settings, mail, server):
    budget = mail["Q4 budget draft"]
    semantic.index_mail(store, settings)

    sources, _today, _hits = pick_sources(store, "Where is the team trip in Portugal?", settings=settings)
    assert budget.id in [email.id for email in sources]
    assert pick_sources(store, "Where is the team trip in Portugal?")[0] == [], "keyword-only without settings"

    ws = agent.Workspace(store=store, settings=settings, sources=[])
    listing = agent._search_mail(ws, "team trip in Portugal")
    assert "Q4 budget draft" in listing and "related in meaning, not by the same words" in listing
    keyword = agent._search_mail(ws, "Acme quote")
    assert "FW: Acme quote" in keyword.splitlines()[1] and "related in meaning" not in keyword.splitlines()[1]


def test_without_an_embedding_model_search_stays_keyword_only(store, settings, mail, monkeypatch):
    fake = _FakeEmbeddings(embedding_loaded=False)
    monkeypatch.setattr(semantic.httpx, "get", fake.get)
    monkeypatch.setattr(semantic.httpx, "post", fake.post)
    assert semantic.embedding_model(settings) == ""
    assert semantic.index_mail(store, settings) == 0 and fake.calls == 0
    assert semantic.search(store, settings, "team trip in Portugal") == []

    def refuse(*_args, **_kwargs):
        raise httpx.ConnectError("refused")

    semantic._model_cache.clear()
    monkeypatch.setattr(semantic.httpx, "get", refuse)
    assert semantic.index_mail(store, settings) == 0, "no server: nothing indexed, no error"

    settings.embedding_model = "off"
    monkeypatch.setattr(semantic.httpx, "post", refuse)
    assert semantic.search(store, settings, "team trip in Portugal") == []


def test_vectors_are_unit_length_and_a_named_model_skips_the_lookup(store, settings, mail, server, monkeypatch):
    settings.embedding_model = MODEL
    monkeypatch.setattr(semantic.httpx, "get", lambda *_a, **_k: pytest.fail("no lookup when the model is named"))
    vectors = semantic.embed(settings, ["Lisbon offsite", "bank account"])
    assert [round(math.sqrt(sum(x * x for x in v)), 4) for v in vectors] == [1.0, 1.0]
    assert semantic.index_mail(store, settings) > 0 and store.has_embeddings()


def test_every_part_of_a_long_file_is_indexed(store, mail):
    budget = mail["Q4 budget draft"]
    memo = next(att for att in budget.attachments if att.filename == "Offsite memo.docx")
    memo.extracted_text = "\n\n".join(f"[page {n}]\n" + "Routine section. " * 120 for n in range(1, 60))
    memo.extracted_text += "\n\n[page 60]\n" + "Routine section. " * 100 + "FINDING: call-backs were skipped."
    pieces = [text for key, text in semantic._items(budget) if key.startswith(f"file:{memo.id}:")]
    assert pieces[-1].endswith("call-backs were skipped."), "every page, all of each page"
    body = sum(len(text.split("\n", 1)[1]) for text in pieces)
    assert body >= len(memo.extracted_text) * 0.95 and all(len(text.split("\n", 1)[1]) <= semantic.EMBED_CHARS for text in pieces)


def test_the_overnight_run_indexes_mail_for_search(store, settings, mail, server):
    from controller_inbox.overnight import run_overnight
    from test_bionic import AgreeingReader

    settings.overnight_file_summaries = 0
    result = run_overnight(store, settings, limit=1, sync_graph=False, reader=AgreeingReader())
    assert result["indexed_for_search"] >= 5
    assert "added to search by meaning: " in open(result["log_path"], encoding="utf-8").read()
