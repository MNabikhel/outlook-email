"""CloseDesk choosing and loading the model itself, and suggesting a better fit.

With nothing loaded in LM Studio, CloseDesk answered from looking things up until the user found Settings and chose
a model. Now it loads one at its start: the one used last, else the most capable downloaded chat model that fits this
computer. And a model too big or slow for this computer, with a smaller one downloaded, gets a suggestion the user
answers with a click: Load it, or Keep theirs (not asked again).
"""

from __future__ import annotations

import threading

import httpx
import pytest
from fastapi.testclient import TestClient
from test_lm_studio_models import CHAT, CODER, OVIS, LMStudio, _serve

from controller_inbox import cli, local_llm, model_choice, model_roles, web
from controller_inbox.local_llm import check_model, llm_active, remember_model

GB = 1024**3
QWEN8, GEMMA27 = "qwen/qwen3-8b", "google/gemma-3-27b"
PAGE = {"X-CloseDesk": "1"}


@pytest.fixture
def lm(settings, monkeypatch):
    """LM Studio with nothing loaded, on a 16 GB computer (12 GB of it for a model)."""
    settings.llm = None
    settings.ensure_data_dir()
    server = LMStudio(loaded=[])
    server.extra = [QWEN8, GEMMA27]
    _serve(monkeypatch, server)
    monkeypatch.setattr(model_roles, "computer_memory", lambda: 16 * GB)
    return server


def _loads(server) -> list[str]:
    return [call[1] for call in server.calls if call[0] == "load"]


# The automatic choice ------------------------------------------------------------------------------------------


def test_the_model_used_last_is_loaded_at_the_start(settings, lm):
    remember_model(settings, CHAT)  # no sizes from LM Studio: the one used last is still chosen
    loaded = model_choice.load_at_start(settings)
    assert loaded == {"model": CHAT, "message": f"Loaded {CHAT} in LM Studio: it answers your questions and reads the mail."}
    assert lm.calls == [("load", CHAT, settings.min_context_tokens)]
    assert check_model(settings, use_cache=False).model == CHAT and llm_active(settings)


def test_with_none_used_before_the_most_capable_that_fits_is_loaded(settings, lm):
    # 16 GB: the 27B model needs about 19 GB; the coding model is bigger than the general one but not preferred.
    lm.sizes = {CODER: 9 * GB, CHAT: 6 * GB, QWEN8: 5 * GB, GEMMA27: 17 * GB, OVIS: 10 * GB}
    assert model_choice.load_at_start(settings)["model"] == CHAT
    assert _loads(lm) == [CHAT] and local_llm.remembered_model(settings) == CHAT


def test_a_bigger_computer_gets_the_bigger_model(settings, lm, monkeypatch):
    monkeypatch.setattr(model_roles, "computer_memory", lambda: 64 * GB)
    lm.sizes = {CODER: 9 * GB, CHAT: 6 * GB, QWEN8: 5 * GB, GEMMA27: 17 * GB}
    assert model_choice.load_at_start(settings)["model"] == GEMMA27, "not limited to small models"


def test_document_readers_embeddings_and_coders_are_never_the_first_choice(settings, lm):
    # Only the page reader (a document reader) and the search model are bigger than the coding model.
    lm.extra = []
    lm.sizes = {CODER: 4 * GB, CHAT: 19 * GB, OVIS: 3 * GB}
    status = check_model(settings, use_cache=False)
    assert OVIS not in status.chat_models
    assert model_choice.auto_choice(settings, status) == CODER, "a coding model only when no general one fits"
    lm.sizes = {CODER: 4 * GB, CHAT: 5 * GB}
    assert model_choice.auto_choice(settings, check_model(settings, use_cache=False)) == CHAT


def test_nothing_is_loaded_without_sizes_or_a_model_used_before(settings, lm):
    assert model_choice.load_at_start(settings) == {"model": "", "message": ""}
    assert lm.calls == []
    assert check_model(settings, use_cache=False).model == "", "Settings still says to choose one"


def test_a_model_named_in_the_settings_file_is_left_to_answer(settings, lm):
    remember_model(settings, CHAT)
    lm.sizes = {CHAT: 6 * GB}
    settings.llm_model = CODER
    assert model_choice.load_at_start(settings)["model"] == "" and lm.calls == []


def test_lm_studio_on_another_computer_loads_only_the_model_used_before(settings, lm):
    settings.llm_base_url = "http://192.168.1.20:1234/v1"  # its memory isn't this computer's
    lm.sizes = {CHAT: 6 * GB, QWEN8: 5 * GB}
    assert model_choice.load_at_start(settings)["model"] == "" and lm.calls == []
    remember_model(settings, QWEN8)
    assert model_choice.load_at_start(settings)["model"] == QWEN8


def test_a_model_the_user_loaded_is_left_alone(settings, lm):
    remember_model(settings, CHAT)
    lm.sizes = {CODER: 4 * GB, CHAT: 6 * GB}
    lm.loaded = [CODER]
    assert model_choice.load_at_start(settings)["model"] == "" and lm.calls == []
    assert lm.loaded == [CODER]


def test_a_model_loaded_while_the_choice_was_made_is_not_unloaded(settings, lm, monkeypatch):
    remember_model(settings, CHAT)
    real = local_llm.check_model
    seen = []

    def user_loads_one_meanwhile(s, **kw):
        seen.append(1)
        if len(seen) == 1:  # the check inside the load, after CloseDesk chose
            lm.loaded = [CODER]
        return real(s, **kw)

    monkeypatch.setattr(local_llm, "check_model", user_loads_one_meanwhile)
    loaded = model_choice.load_at_start(settings)
    assert loaded == {"model": "", "message": ""}
    assert lm.calls == [] and lm.loaded == [CODER]


def test_a_load_that_fails_says_why_and_the_mail_is_read_without_a_model(settings, store, lm):
    from controller_inbox.overnight import read_queue

    remember_model(settings, CHAT)
    lm.fails_to_load = {CHAT}
    loaded = model_choice.load_at_start(settings)
    assert loaded["model"] == "" and loaded["failed"]
    assert loaded["message"].startswith(f"CloseDesk couldn't load {CHAT} in LM Studio. LM Studio couldn't load {CHAT} (not enough memory")
    assert "filed without a model for now" in loaded["message"]
    # LM Studio isn't asked to load it again on its own for each email: the reading goes on without a model.
    assert check_model(settings, use_cache=False).model == "" and not llm_active(settings)
    assert read_queue(store, settings)["model"] == "" and lm.asked == []
    assert model_choice.load_at_start(settings) == {"model": "", "message": ""}, "not tried again at once"
    # Chosen in Settings, it is tried again (memory may have been freed).
    lm.fails_to_load = set()
    assert local_llm.use_chat_model(settings, CHAT) == ""
    assert not local_llm.load_failed_lately(CHAT)


# The start-up stage --------------------------------------------------------------------------------------------


def test_the_start_loads_the_model_first_and_the_page_says_so(settings, store, lm, monkeypatch):
    from controller_inbox import overnight

    remember_model(settings, CHAT)
    loading, release = threading.Event(), threading.Event()
    real = lm.__call__

    def slow_load(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/models/load":
            loading.set()
            assert release.wait(10)
        return real(request)

    _serve(monkeypatch, slow_load)
    order: list = []

    def reading(_store, _settings, *, on_progress=None, before_reading=None, **_kw):
        order.append(("import", list(lm.loaded)))
        before_reading()
        order.append(("read", list(lm.loaded)))
        return {"headline": "", "ingested": 0, "read_by_bionic": 0, "folders": {"important": 0, "informational": 0, "reference": 0},
                "waiting_on_bionic": 0, "digest_path": "d", "log_path": "l", "model": CHAT, "avg_seconds": 0}

    seen: list = []

    def server(app, **_kw):
        assert loading.wait(10)
        client = TestClient(app)
        seen.append(client.get("/api/state", headers=PAGE).json()["job"])
        seen.append(client.get("/").text)
        release.set()
        # The start-up job ends: its result says the model was loaded.
        for _ in range(200):
            if app.state.job.snapshot()["state"] != "running":
                break
            threading.Event().wait(0.05)
        seen.append(app.state.job.snapshot()["result"])
        seen.append(client.get("/").text)

    monkeypatch.setattr(overnight, "run_overnight", reading)
    monkeypatch.setattr(cli, "_run_server", server)
    monkeypatch.setattr(cli, "_link_stored_mail", lambda *_a: None)
    args = type("Args", (), {"no_serve": False, "no_browser": True, "limit": 0, "host": "127.0.0.1", "port": 0})()
    monkeypatch.setattr(cli, "_port_to_use", lambda host, port: (8765, False))
    assert cli._run(settings, store, args) == 0
    job, classic, result, home = seen
    assert job["stage"] == "model" and job["stage_label"] == "Loading a model in LM Studio" and CHAT in job["note"]
    assert "Loading a model in LM Studio" in classic
    assert order == [("import", []), ("read", [CHAT])], "the mail is read once the model is in"
    assert result["model_load"] == {"message": f"Loaded {CHAT} in LM Studio: it answers your questions and reads the mail.", "failed": False}
    assert f"Loaded {CHAT} in LM Studio" in home


def test_the_start_without_the_page_loads_the_model_too(settings, store, lm, monkeypatch, capsys):
    from controller_inbox import overnight

    lm.sizes = {CHAT: 6 * GB}
    monkeypatch.setattr(overnight, "run_overnight", lambda *_a, before_reading, **_k: before_reading() or {
        "headline": "", "ingested": 0, "read_by_bionic": 0, "folders": {"important": 0, "informational": 0, "reference": 0},
        "waiting_on_bionic": 0, "digest_path": "d", "log_path": "l"})
    args = type("Args", (), {"no_serve": True, "no_browser": True, "limit": 0, "host": "127.0.0.1", "port": 0})()
    assert cli._run(settings, store, args) == 0
    out = capsys.readouterr().out
    assert f"Loading a model in LM Studio: {CHAT}" in out and f"Loaded {CHAT} in LM Studio" in out


def test_the_new_mail_is_imported_before_the_model_loads(settings, store, lm, monkeypatch):
    """Copying mail in needs no model: a load that takes minutes (or hangs) doesn't hold the new mail back."""
    from email.message import EmailMessage

    from controller_inbox import overnight

    remember_model(settings, CHAT)
    message = EmailMessage()
    message["Subject"] = "Invoice 552"
    message["From"] = "Ann <ann@vendor-x.com>"
    message["Date"] = "Mon, 28 Sep 2026 14:30:00 +0000"
    message.set_content("Invoice 552, due October 30.")
    (settings.inbox_incoming / "invoice.eml").write_bytes(bytes(message))
    real = lm.__call__
    at_load: list = []

    def load(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/models/load":
            at_load.append([email.subject for email in store.list_emails(limit=10)])
        return real(request)

    _serve(monkeypatch, load)
    stages: list = []
    monkeypatch.setattr(overnight, "read_queue", lambda *_a, **_k: stages.append(("read", list(lm.loaded))) or {
        "read_ids": [], "model": "", "note": "", "stats": None})
    args = type("Args", (), {"no_serve": True, "no_browser": True, "limit": 0, "host": "127.0.0.1", "port": 0})()
    assert cli._run(settings, store, args) == 0
    assert at_load == [["Invoice 552"]], "the email was in before LM Studio was asked to load the model"
    assert stages == [("read", [CHAT])], "and read once the model is in"


def test_both_pages_name_the_loading_stage():
    from pathlib import Path

    from controller_inbox import web_api

    assert web_api.STAGES["model"] == "Loading a model in LM Studio"
    job = (Path(web.__file__).parent / "templates" / "_job.html").read_text(encoding="utf-8")
    assert "'model': 'Loading a model in LM Studio'" in job


# The suggestion ------------------------------------------------------------------------------------------------


def _answering(lm, settings, key, **sizes):
    lm.loaded = [key]
    lm.sizes = sizes
    local_llm._status_cache.clear()
    return check_model(settings)


def test_a_slow_model_gets_a_faster_one_suggested(settings, store, lm):
    status = _answering(lm, settings, CHAT, **{CHAT: 8 * GB, QWEN8: 5 * GB, CODER: 4 * GB, GEMMA27: 17 * GB})
    assert model_choice.recommendation(settings, store, status) is None, "no speed measured, and it fits"
    model_choice.record_speed(store, CHAT, 70.4, 5)
    tip = model_choice.recommendation(settings, store, status)
    assert tip == {
        "current": CHAT, "suggested": QWEN8, "reason": "slow",
        "message": f"{CHAT} takes about 70 s an email on this computer. {QWEN8} would fit and answer faster.",
    }
    model_choice.record_speed(store, CHAT, 30.0, 5)
    assert model_choice.recommendation(settings, store, status) is None, "30 s an email isn't slow"
    model_choice.record_speed(store, CHAT, 90.0, 1)
    assert model_choice.recommendation(settings, store, status) is None, "one email (and a load) is too few to tell"


def test_a_model_too_big_gets_one_that_fits_suggested(settings, store, lm):
    lm.contexts = {GEMMA27: 16384}
    status = _answering(lm, settings, GEMMA27, **{GEMMA27: 17 * GB, CHAT: 6 * GB, QWEN8: 5 * GB})
    tip = model_choice.recommendation(settings, store, status)
    assert tip["reason"] == "too_big" and tip["suggested"] == CHAT
    # Only what is known: it loaded and answers, so nothing is said about running out of memory or answering faster.
    assert tip["message"] == (
        f"{GEMMA27} needs about 19 GB of memory with a 16,384-token context and this computer has 16 GB, so the "
        f"computer may slow down while it answers. {CHAT} fits in its memory."
    )


def test_the_context_the_model_was_loaded_with_is_the_one_counted(settings, store, lm):
    # Setup asks for 32,768 tokens (13 GB with a 9 GB model: too big for 12 GB), but LM Studio loaded it with 16,384
    # (11 GB): it fits as it is.
    settings.min_context_tokens = 32768
    lm.contexts = {QWEN8: 16384}
    status = _answering(lm, settings, QWEN8, **{QWEN8: 9 * GB, CHAT: 5 * GB})
    assert model_choice.recommendation(settings, store, status) is None
    lm.contexts = {QWEN8: 32768}
    tip = model_choice.recommendation(settings, store, _answering(lm, settings, QWEN8, **{QWEN8: 9 * GB, CHAT: 5 * GB}))
    assert tip["reason"] == "too_big" and "about 13 GB of memory with a 32,768-token context" in tip["message"]


def test_nothing_is_suggested_without_a_smaller_model_that_fits(settings, store, lm):
    status = _answering(lm, settings, QWEN8, **{QWEN8: 5 * GB, CHAT: 6 * GB})
    model_choice.record_speed(store, QWEN8, 80.0, 4)
    assert model_choice.recommendation(settings, store, status) is None, "the only other one is bigger"
    settings.llm_model = QWEN8
    status = _answering(lm, settings, GEMMA27, **{GEMMA27: 17 * GB, CHAT: 6 * GB})
    assert model_choice.recommendation(settings, store, status) is None, "named in the settings file"


def test_the_reading_keeps_how_long_the_model_took_an_email(settings, store):
    from controller_inbox.local_llm import ReaderStats
    from controller_inbox.overnight import read_queue

    class Reader:
        model = CHAT
        stopped = False
        stats = ReaderStats(read=4, seconds=280.0)

    read_queue(store, settings, reader=Reader())
    assert model_choice.seconds_an_email(store, CHAT) == 70.0


def test_a_load_during_the_first_email_does_not_make_the_model_look_slow(settings, store, lm, monkeypatch):
    """LM Studio loads the model while it answers the first email (nothing loaded yet): that minute isn't the
    model's reading speed, so a model that reads at 20 s an email isn't called slow."""
    from types import SimpleNamespace

    clock = [0.0]
    took = iter([80.0, 20.0, 20.0])  # a 60 s load, then 20 s an email

    def answer(_prompt):
        clock[0] += next(took)
        return local_llm.Reply(content="{}")

    monkeypatch.setattr(local_llm, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda _s: None))
    monkeypatch.setattr(local_llm, "_find_reading", lambda _reply: {"folder": "reference"})
    reader = local_llm.LocalReader(settings, model=CHAT, client=httpx.Client(transport=httpx.MockTransport(lm)))
    reader._complete = answer
    reader.read({"subject": "a"})
    reader.read({"subject": "b"})
    model_choice.record_speed(store, CHAT, reader.stats.typical_seconds, reader.stats.read)
    assert model_choice.seconds_an_email(store, CHAT) == 0.0, "two emails, one with a load, are too few to tell"
    reader.read({"subject": "c"})
    model_choice.record_speed(store, CHAT, reader.stats.typical_seconds, reader.stats.read)
    assert model_choice.seconds_an_email(store, CHAT) == 20.0
    status = _answering(lm, settings, CHAT, **{CHAT: 8 * GB, QWEN8: 5 * GB})
    assert model_choice.recommendation(settings, store, status) is None


def test_keep_stops_the_suggestion_for_that_model(settings, store, lm):
    model_choice.record_speed(store, CHAT, 70.0, 5)
    _answering(lm, settings, CHAT, **{CHAT: 8 * GB, QWEN8: 5 * GB})
    client = TestClient(web.create_app(settings, store))
    assert client.get("/api/state", headers=PAGE).json()["model_tip"]["suggested"] == QWEN8
    reply = client.post("/api/settings/chat-model/keep", json={"model": CHAT}, headers=PAGE)
    assert reply.status_code == 200 and reply.json()["message"] == "Kept. CloseDesk won't suggest another model in its place again."
    assert client.get("/api/state", headers=PAGE).json()["model_tip"] is None
    assert client.get("/api/settings", headers=PAGE).json()["chat"]["tip"] is None
    assert lm.calls == [], "nothing switched"
    # Another model that is slow is still suggested against.
    model_choice.record_speed(store, GEMMA27, 70.0, 5)
    assert model_choice.recommendation(settings, store, _answering(lm, settings, GEMMA27, **{GEMMA27: 9 * GB, QWEN8: 5 * GB}))


def test_both_settings_pages_offer_the_suggestion_and_load_it_only_when_clicked(settings, store, lm):
    model_choice.record_speed(store, CHAT, 70.0, 5)
    _answering(lm, settings, CHAT, **{CHAT: 8 * GB, QWEN8: 5 * GB})
    client = TestClient(web.create_app(settings, store))
    tip = client.get("/api/settings", headers=PAGE).json()["chat"]["tip"]
    assert tip["current"] == CHAT and tip["suggested"] == QWEN8 and "70 s an email" in tip["message"]
    page = client.get("/settings").text
    box = page.split('id="model-tip"')[1].split("</div>")[0]
    assert f"{CHAT} takes about 70 s an email on this computer. {QWEN8} would fit and answer faster." in box
    assert f'action="/settings/chat-model" class="inline"><input type="hidden" name="model" value="{QWEN8}">' in box
    assert f"Load {QWEN8}</button>" in box and f"Keep {CHAT}</button>" in box
    assert lm.calls == [], "showing it switches nothing"
    # Keep, on the classic page.
    origin = {"Origin": "http://testserver"}
    reply = client.post("/settings/chat-model/keep", data={"model": CHAT}, headers=origin, follow_redirects=False)
    assert reply.headers["location"] == "/settings?notice=chat-model-kept#chat-model"
    assert 'id="model-tip"' not in client.get(reply.headers["location"]).text
    # Load, from the workspace: the existing switch.
    store.set_state(model_choice.KEPT_KEY, "[]")
    reply = client.post("/api/settings/chat-model", json={"model": QWEN8}, headers=PAGE)
    assert reply.status_code == 200 and lm.loaded == [QWEN8]
    assert client.get("/api/settings", headers=PAGE).json()["chat"]["tip"] is None


def test_the_classic_today_page_offers_the_suggestion_too(settings, loaded, lm):
    model_choice.record_speed(loaded, CHAT, 70.0, 5)
    _answering(lm, settings, CHAT, **{CHAT: 8 * GB, QWEN8: 5 * GB})
    client = TestClient(web.create_app(settings, loaded))
    box = client.get("/").text.split('id="model-tip"')[1].split("</div>")[0]
    assert f"{CHAT} takes about 70 s an email on this computer. {QWEN8} would fit and answer faster." in box
    assert f"Load {QWEN8}</button>" in box and f"Keep {CHAT}</button>" in box
    assert box.count('<input type="hidden" name="next" value="/">') == 2, "both come back to Today"
    assert lm.calls == [], "showing it switches nothing"
    origin = {"Origin": "http://testserver"}
    # Load, from Today: back on Today, saying so.
    reply = client.post("/settings/chat-model", data={"model": QWEN8, "next": "/"}, headers=origin, follow_redirects=False)
    assert reply.headers["location"] == "/?notice=chat-model" and lm.loaded == [QWEN8]
    assert "Loaded in LM Studio. It answers your questions now." in client.get(reply.headers["location"]).text
    # Keep, from Today.
    model_choice.record_speed(loaded, QWEN8, 70.0, 5)
    lm.extra = [*lm.extra, "tiny/model-1b"]
    _answering(lm, settings, QWEN8, **{QWEN8: 5 * GB, "tiny/model-1b": 1 * GB})
    reply = client.post("/settings/chat-model/keep", data={"model": QWEN8, "next": "/"}, headers=origin, follow_redirects=False)
    assert reply.headers["location"] == "/?notice=chat-model-kept"
    assert 'id="model-tip"' not in client.get(reply.headers["location"]).text
    # Another site's address isn't followed.
    reply = client.post("/settings/chat-model/keep", data={"model": QWEN8, "next": "//evil.example"}, headers=origin, follow_redirects=False)
    assert reply.headers["location"] == "/settings?notice=chat-model-kept#chat-model"
