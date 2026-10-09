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

    partial = pick_sources(store, "The trip in Portugal last year", settings=settings)[0]
    assert partial[0].id == budget.id and "FW: Acme quote" in [email.subject for email in partial], "a match on one word goes after"
    assert pick_sources(store, "Acme quote", settings=settings)[0][0].subject == "FW: Acme quote", "every word matches: it leads"

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


def test_a_new_index_with_as_many_vectors_is_searched_not_the_old_one(store, settings, mail, server, monkeypatch):
    budget = mail["Q4 budget draft"]
    semantic.index_mail(store, settings)
    assert [email.id for email in semantic.search(store, settings, "team trip in Portugal")] == [budget.id]
    # The mail's vectors are cleared (loading the sample mailbox does it) and the same rows indexed again with a
    # model that places ideas differently: as many rows, the same last row number, other vectors.
    with store.connect() as conn:
        conn.execute("DELETE FROM embeddings")
    original = semantic.httpx.post

    def other_model(url, json=None, **kwargs):
        response = original(url, json=json, **kwargs)
        data = response.json()["data"]
        for row in data:
            v = row["embedding"]
            row["embedding"] = [v[2], v[3], v[0], v[1], *v[4:]]
        return httpx.Response(200, request=response.request, json={"data": data})

    monkeypatch.setattr(semantic.httpx, "post", other_model)
    semantic.index_mail(store, settings)
    assert [email.id for email in semantic.search(store, settings, "team trip in Portugal")] == [budget.id]


def test_a_model_swapped_in_under_the_same_name_is_indexed_again(store, settings, mail, server, monkeypatch):
    budget, quote = mail["Q4 budget draft"], mail["FW: Acme quote"]
    semantic.index_mail(store, settings)
    rows = len(store.embedding_keys(MODEL))
    # Another embedding model is loaded under the same name: its vectors have twice as many numbers.
    original = semantic.httpx.post

    def bigger(url, json=None, **kwargs):
        response = original(url, json=json, **kwargs)
        data = response.json()["data"]
        for row in data:
            row["embedding"] = row["embedding"] * 2
        return httpx.Response(200, request=response.request, json={"data": data})

    monkeypatch.setattr(semantic.httpx, "post", bigger)
    quote.body_text += " Remittance advice to follow."
    store.upsert_email(quote)
    assert semantic.index_mail(store, settings) == rows, "the old model's vectors are made again"
    assert semantic.index_mail(store, settings) == 0
    assert [email.id for email in semantic.search(store, settings, "team trip in Portugal")] == [budget.id]


def test_search_compares_only_vectors_the_size_of_the_question(store, settings, mail, server):
    budget = mail["Q4 budget draft"]
    semantic.index_mail(store, settings)
    # One stray row from another model sorts first; it mustn't hide the rest.
    store.save_embeddings(MODEL, [("email:stray", "stray", "x", semantic._unit([1.0] * 12).tobytes())])
    with store.connect() as conn:
        conn.execute("UPDATE embeddings SET rowid = -1 WHERE key = 'email:stray'")
    assert [email.id for email in semantic.search(store, settings, "team trip in Portugal")] == [budget.id]


def test_sections_a_file_no_longer_has_are_dropped_from_the_index(store, settings, mail, server):
    budget = mail["Q4 budget draft"]
    memo = next(att for att in budget.attachments if att.filename == "Offsite memo.docx")
    memo.extracted_text = "[page 1]\nOpen item: confirm the deposit.\n\n[page 2]\nThe offsite moves to Lisbon on 14 November."
    store.upsert_email(budget)
    semantic.index_mail(store, settings)
    assert [email.id for email in semantic.search(store, settings, "Porto Portugal")] == [budget.id]

    # The file is read again and page 2 is gone: nothing in the mail mentions Lisbon now.
    memo.extracted_text = "[page 1]\nOpen item: confirm the deposit."
    store.upsert_email(budget)
    semantic.index_mail(store, settings)
    assert [key for key, _t, _b in store.embedding_rows(MODEL, prefix=f"file:{memo.id}:")] == [f"file:{memo.id}:page 1"]
    assert semantic.search(store, settings, "Porto Portugal") == []


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


def test_the_pages_show_what_is_indexed_and_index_on_request(store, settings, mail, server):
    import time

    from fastapi.testclient import TestClient

    from controller_inbox import web

    budget, scam = mail["Q4 budget draft"], mail["Updated remittance details"]
    app = web.create_app(settings, store)
    client = TestClient(app)

    setup = client.get("/settings").text
    assert "Search by meaning" in setup and MODEL in setup and "Index all mail now" in setup and "Last indexed <b>never</b>" in setup
    page = client.get(f"/inbox/{budget.id}").text
    assert "Index for search" in page and page.count("Not indexed yet") == 2 and "✓ Indexed" not in page
    assert set(semantic.file_states(store, settings, scam).values()) == {"locked"}
    assert "Index for search" not in client.get(f"/inbox/{scam.id}").text, "flagged mail's files are never indexed"

    done = client.post(f"/inbox/{budget.id}/index", follow_redirects=False)
    assert done.status_code == 303 and done.headers["location"] == f"/inbox/{budget.id}?notice=indexed#files"
    page = client.get(f"/inbox/{budget.id}").text
    assert page.count("✓ Indexed") == 2 and "Index for search" not in page, "no button once everything is indexed"
    assert "✓ Indexed" in client.get(f"/inbox/{budget.id}/files/1").text
    coverage = semantic.coverage(store, settings)
    assert coverage["emails_done"] == 1 and coverage["waiting"] == coverage["emails"] - 1 and coverage["indexed_at"] == ""

    started = client.post("/settings/index", follow_redirects=False)
    assert started.headers["location"] == "/settings?notice=indexing#search"
    deadline = time.monotonic() + 60  # the job runs in the background; a slow machine needs more than a few seconds
    while app.state.job.snapshot()["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert app.state.job.snapshot()["result"]["kind"] == "index"
    setup = client.get("/settings").text
    assert "✓ Everything is indexed" in setup and "Index all mail now" not in setup and "Last indexed <b>never</b>" not in setup
    coverage = semantic.coverage(store, settings)
    assert coverage["waiting"] == 0 and coverage["sections_done"] == coverage["sections"] > 0

    memo = next(att for att in budget.attachments if att.filename == "Offsite memo.docx")
    memo.extracted_text += "\nA late addition."
    store.upsert_email(budget)
    page = client.get(f"/inbox/{budget.id}").text
    assert "Changed since indexed" in page and "Index for search" in page


def test_a_long_file_is_read_where_it_matches_in_meaning(store, settings, mail, server):
    budget = mail["Q4 budget draft"]
    memo = next(att for att in budget.attachments if att.filename == "Offsite memo.docx")
    memo.filename = "Notes.docx"
    memo.extracted_text = "\n\n".join(
        f"[page {n}]\n" + ("The retreat is booked in Porto for May. " if n == 25 else "Routine section. ") * 60 for n in range(1, 31)
    )
    store.upsert_email(budget)
    budget = store.get_email(budget.id)
    question = "Which country is the travel in?"

    ws = agent.Workspace(store=store, settings=settings, sources=[budget], question=question, current_id=budget.id)
    blocks = agent.file_context(ws, "Notes.docx: " + question, 6_000)
    assert "Porto" not in blocks[budget.id], "not indexed: the file is read from the start"
    assert any("from the start" in read for read in ws.reads)

    semantic.index_mail(store, settings)
    ws = agent.Workspace(store=store, settings=settings, sources=[budget], question=question, current_id=budget.id)
    blocks = agent.file_context(ws, "Notes.docx: " + question, 6_000)
    assert "The retreat is booked in Porto" in blocks[budget.id], "page 25 is picked by meaning"
    assert any(read.startswith("Read ") and "sections of Notes.docx" in read and read.endswith("(picked by meaning)") for read in ws.reads)


def test_the_chat_says_how_it_searched(store, settings, mail, server):
    report: dict = {}
    pick_sources(store, "Where is the team trip in Portugal?", settings=settings, report=report)
    assert report == {"how": "no_index", "meaning_hits": 0}
    assert "isn't indexed for meaning yet" in semantic.search_step(report["how"], settings, 0)

    semantic.index_mail(store, settings)
    pick_sources(store, "Where is the team trip in Portugal?", settings=settings, report=report)
    assert report == {"how": "meaning", "meaning_hits": 1}
    assert semantic.search_step("meaning", settings, 1) == f"Searched your mail by words and meaning ({MODEL}): 1 email found by meaning"
