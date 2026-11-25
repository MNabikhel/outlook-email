"""The update question asked each time CloseDesk opens: GitHub is asked as it opens (briefly: offline or slow means no
question and nothing in the way), and when a newer version is out and a launcher started CloseDesk, the first page
shown asks "Update now" or "Not now", once a start. The dialog itself, in both UIs, is in
test_update_on_open_browser.py."""

from __future__ import annotations

import socket
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from controller_inbox import cli, updates, version, web
from helpers import PAGE

MINE = "a" * 40
NEWER = "b" * 40
NOW = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)
MERGED = (
    "Merge pull request #73 from MNabikhel/summary-wait\n\n"
    "Wait as long as the model needs for file summaries, and stop instead of looking stuck"
)
REAL_ASK = updates._ask_github
START_WAIT = updates.START_TIMEOUT_SECONDS


def github(sha: str = NEWER, date: str = "2026-11-16T09:12:00Z", message: str = MERGED, *, hold: threading.Event | None = None):
    """GitHub's answer for main, the URLs it was asked and how long CloseDesk said it would wait. ``hold``: the answer
    comes once it is set (GitHub being slow)."""
    asked: list[str] = []
    waits: list[float] = []

    def ask(url: str, timeout: float = updates.TIMEOUT_SECONDS) -> dict:
        asked.append(url)
        waits.append(timeout)
        if hold is not None:
            hold.wait(10)
        return {"sha": sha, "commit": {"committer": {"date": date}, "message": message}}

    ask.asked, ask.waits = asked, waits
    return ask


def offline(url: str, timeout: float = updates.TIMEOUT_SECONDS) -> dict:
    raise OSError("[Errno 8] nodename nor servname provided, or not known")


@pytest.fixture
def download(tmp_path, monkeypatch):
    """This copy as a download of November 13, 2026, started by its launcher."""
    folder = tmp_path / "CloseDesk folder"
    folder.mkdir()
    (folder / ".git_archival.txt").write_text(f"commit: {MINE}\ndate: 2026-11-13\n", encoding="utf-8")
    monkeypatch.setattr(version, "ROOT", folder)
    monkeypatch.setenv(updates.LAUNCHER_ENV, "1")
    return folder


@pytest.fixture(autouse=True)
def no_github(monkeypatch):
    """Nothing here asks the real GitHub."""

    def refuse(url, timeout=None):
        raise AssertionError(f"a test asked the real GitHub: {url}")

    monkeypatch.setattr(updates, "_ask_github", refuse)
    monkeypatch.delenv(updates.LAUNCHER_ENV, raising=False)
    monkeypatch.delenv("CLOSEDESK_REOPEN", raising=False)


def asked_at_start(store, settings, *, failed: bool = False) -> updates.Question:
    """CloseDesk opening: the question, once GitHub has answered (or not)."""
    question = updates.Question()
    question.begin(store, settings, failed=failed)
    deadline = time.monotonic() + 10
    while question.checking():
        assert time.monotonic() < deadline, "the check as CloseDesk opens never ended"
        time.sleep(0.01)
    return question


# Asking GitHub as CloseDesk opens ---------------------------------------------------------------------------


def test_closedesk_asks_github_each_time_it_opens_even_when_it_asked_today(download, settings, store):
    updates.check(store, settings, now=NOW, ask=github(sha=MINE, date="2026-11-13T10:00:00Z"))
    later = github()
    # The once-a-day check wouldn't ask again an hour later...
    updates.check(store, settings, now=NOW + timedelta(hours=1), ask=later)
    assert later.asked == []
    # ...but opening CloseDesk does: a version may have come out since.
    updates.check(store, settings, now=NOW + timedelta(hours=1), ask=later, at_start=True)
    assert len(later.asked) == 1 and updates.status(store, settings)["newer"] is True


def test_the_check_as_closedesk_opens_waits_a_few_seconds_at_most(download, settings, store, monkeypatch):
    ask = github()
    monkeypatch.setattr(updates, "_ask_github", ask)
    asked_at_start(store, settings)
    assert ask.waits == [updates.START_TIMEOUT_SECONDS] and updates.START_TIMEOUT_SECONDS <= 5
    # The daily check keeps its longer wait (nobody is waiting on it).
    updates.check(store, settings, now=datetime.now(timezone.utc) + timedelta(days=2))
    assert ask.waits[-1] == updates.TIMEOUT_SECONDS


def test_a_github_that_doesnt_answer_is_given_up_on_within_the_wait(download, settings, store, monkeypatch):
    # A network that swallows the request (a captive portal, a proxy that holds it): no question, and the page isn't
    # kept waiting for long. A local server that accepts and never answers stands in for GitHub; curl too gives up.
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen()
    try:
        monkeypatch.setattr(updates, "LATEST_URL", f"http://127.0.0.1:{silent.getsockname()[1]}/commits/main")
        monkeypatch.setattr(updates, "_ask_github", REAL_ASK)
        monkeypatch.setattr(updates, "START_TIMEOUT_SECONDS", 1)
        started = time.monotonic()
        question = asked_at_start(store, settings)
        took = time.monotonic() - started
    finally:
        silent.close()
    assert took < 6, f"the check as CloseDesk opens took {took:.1f}s"
    view = question.view(store, settings, "idle")
    assert view["ask"] is False and view["checking"] is False


class Trickle:
    """A stand-in for GitHub on a slow link or proxy: it answers, then sends its answer a byte at a time, each in good
    time for httpx (whose timeout is for each step, not the whole), the whole of it far too late."""

    def __init__(self, gap: float = 0.3) -> None:
        import json

        self.body = json.dumps({"sha": NEWER, "commit": {"committer": {"date": "2026-11-16T09:12:00Z"}, "message": MERGED}}).encode()
        self.gap = gap
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}/commits/main"
        self.closed = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self.closed.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._answer, args=(conn,), daemon=True).start()

    def _answer(self, conn) -> None:
        with conn:
            try:
                conn.recv(65536)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(self.body))
                for byte in self.body:
                    if self.closed.wait(self.gap):
                        return
                    conn.sendall(bytes([byte]))
            except OSError:
                pass

    def close(self) -> None:
        self.closed.set()
        self.sock.close()


@pytest.fixture
def trickle(monkeypatch):
    slow = Trickle()
    monkeypatch.setattr(updates, "LATEST_URL", slow.url)
    monkeypatch.setattr(updates, "_ask_github", REAL_ASK)
    monkeypatch.setattr(updates.shutil, "which", lambda name: None)  # curl's try is the next test's
    yield slow
    slow.close()


def test_an_answer_that_trickles_in_is_given_up_on_at_the_wait(trickle):
    # httpx's timeout is for each step (connect, each read), not the whole: the asking went on for as long as the
    # answer kept coming, a minute or more, and popped the question up over the work.
    started = time.monotonic()
    with pytest.raises(Exception):
        updates._fetch_json(trickle.url, timeout=1)
    assert time.monotonic() - started < 2.5


def test_the_asking_as_closedesk_opens_ends_in_its_few_seconds_whatever_the_link(trickle, download, settings, store, monkeypatch):
    monkeypatch.setattr(updates, "START_TIMEOUT_SECONDS", 1)
    started = time.monotonic()
    question = asked_at_start(store, settings)
    assert time.monotonic() - started < 2.5
    view = question.view(store, settings, "idle")
    assert view["ask"] is False and view["checking"] is False
    # And an answer is put to the page only when it came in those seconds (and curl's after them), not 25 seconds on.
    assert updates.ASK_WAIT_SECONDS <= START_WAIT + 5


def test_curl_is_tried_only_with_time_left(monkeypatch):
    import httpx

    ran: list = []

    def slow(*_a, **_k):
        time.sleep(0.6)
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(updates, "_get_within", slow)
    monkeypatch.setattr(updates.shutil, "which", lambda name: "/usr/bin/curl" if name == "curl" else None)
    monkeypatch.setattr(updates.subprocess, "run", lambda cmd, **kwargs: ran.append(cmd))
    with pytest.raises(httpx.ReadTimeout):
        updates._fetch_json(updates.LATEST_URL, timeout=0.8)
    assert ran == [], "curl was started after the time was up"


def test_no_answer_in_the_few_seconds_is_tried_again_soon_with_longer_to_wait(download, settings, store):
    # A slow network: the brief asking as CloseDesk opens gets nothing. The daily asking tries again at its next tick
    # (a minute after CloseDesk opens), so Settings still learns of a newer version that day; then once a day as before.
    asked: list = []

    def slow(url):
        asked.append(url)
        raise OSError("timed out")

    updates.check(store, settings, now=NOW, ask=slow, at_start=True)
    assert updates.check(store, settings, now=NOW + timedelta(minutes=1), ask=slow)["failed"] == "1"
    assert len(asked) == 2
    updates.check(store, settings, now=NOW + timedelta(hours=2), ask=slow)
    assert len(asked) == 2, "not asked again and again while the internet is down"
    updates.check(store, settings, now=NOW, ask=slow, at_start=True)
    later = github()
    updates.check(store, settings, now=NOW + timedelta(minutes=1), ask=later)
    assert len(later.asked) == 1 and updates.status(store, settings)["newer"] is True


def test_curl_is_given_the_same_short_wait_and_no_window(monkeypatch):
    # The fallback for a company proxy. In the CloseDesk app on Windows there is no console: a console program started
    # without CREATE_NO_WINDOW would flash one up on every open.
    import httpx

    def refused(*_a, **_k):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]")

    ran: list = []

    def curl(cmd, **kwargs):
        ran.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, stdout='{"sha": "x"}', stderr="")

    monkeypatch.setattr(httpx, "stream", refused)
    monkeypatch.setattr(updates.shutil, "which", lambda name: "/usr/bin/curl" if name == "curl" else None)
    monkeypatch.setattr(updates.subprocess, "run", curl)
    updates._fetch_json(updates.LATEST_URL, timeout=4)
    cmd, kwargs = ran[0]
    assert cmd[cmd.index("--max-time") + 1] == "4" and kwargs["timeout"] <= 4 + 5
    assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


@pytest.mark.parametrize(
    "message, said",
    [
        (MERGED, "Wait as long as the model needs for file summaries, and stop instead of looking stuck"),
        ("Merge pull request #9 from x/y\n\nAsk before loading a model smaller\n\nCo-Authored-By: A <a@b.c>", "Ask before loading a model smaller"),
        ("Merge pull request #9 from x/y\n\nCo-Authored-By: A <a@b.c>", ""),
        ("Merge pull request #9 from x/y\n\nFix: the totals on scanned invoices", "Fix: the totals on scanned invoices"),
        ("Say how long a summary may take (#74)\n\n* one\n* two", "Say how long a summary may take"),
        ("Merge branch 'main' into summary-wait", ""),
        ("", ""),
        ("Read scans\x1b[31m faster‮", "Read scans faster"),
        ("A very long title " * 30, None),
    ],
)
def test_whats_new_is_the_latest_changes_title(message, said):
    found = updates.whats_new(message)
    if said is None:
        assert len(found) <= 160 and found.endswith("…") and found.startswith("A very long title")
    else:
        assert found == said


def test_the_check_keeps_the_latest_changes_title(download, settings, store):
    updates.check(store, settings, now=NOW, ask=github())
    shown = updates.status(store, settings)
    assert shown["whats_new"] == "Wait as long as the model needs for file summaries, and stop instead of looking stuck"
    # Not when this copy is the newest.
    updates.check(store, settings, now=NOW, ask=github(sha=MINE, date="2026-11-13T10:00:00Z"), at_start=True)
    assert updates.status(store, settings)["whats_new"] == ""


def test_the_settings_say_closedesk_asks_as_it_opens(download, settings, store):
    note = updates.status(store, settings)["note"]
    assert "When CloseDesk opens, and once a day while it is open" in note and "nothing about your mail is sent" in note


# The question --------------------------------------------------------------------------------------------------


def test_a_newer_version_is_asked_about_once_a_start(download, settings, store, monkeypatch):
    monkeypatch.setattr(updates, "_ask_github", github())
    question = asked_at_start(store, settings)
    view = question.view(store, settings, "idle")
    assert view["ask"] is True and view["checking"] is False and view["newer"] is True and view["failed"] is False
    assert view["title"] == "A new version of CloseDesk is ready"
    assert view["message"] == "CloseDesk 2026.11.16 is out; you have 2026.11.13."
    assert view["whats_new"] == "Wait as long as the model needs for file summaries, and stop instead of looking stuck"
    assert "your mail and settings are kept" in view["how"].lower()
    assert view["commit"] == MINE and view["updating"] == updates.UPDATING
    # Not now: the next page (another tab, a reload, the other UI) doesn't ask again in this run...
    question.answer()
    assert question.view(store, settings, "idle")["ask"] is False
    assert question.view(store, settings, "idle")["ask"] is False
    # ...the next time CloseDesk opens does.
    assert asked_at_start(store, settings).view(store, settings, "idle")["ask"] is True


def test_while_github_is_asked_the_page_waits_for_the_answer(download, settings, store, monkeypatch):
    slow = threading.Event()
    monkeypatch.setattr(updates, "_ask_github", github(hold=slow))
    question = updates.Question()
    question.begin(store, settings)
    try:
        view = question.view(store, settings, "idle")
        assert view["checking"] is True and view["ask"] is False
    finally:
        slow.set()
    deadline = time.monotonic() + 10
    while question.checking():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert question.view(store, settings, "idle")["ask"] is True


def test_begin_returns_at_once_while_github_is_asked(download, settings, store, monkeypatch):
    # Nothing in the way: CloseDesk opens its page while GitHub is still being asked.
    slow = threading.Event()
    monkeypatch.setattr(updates, "_ask_github", github(hold=slow))
    started = time.monotonic()
    question = updates.Question()
    question.begin(store, settings)
    try:
        assert time.monotonic() - started < 0.5
    finally:
        slow.set()


def test_offline_or_github_down_means_no_question_and_no_error(download, settings, store, monkeypatch):
    monkeypatch.setattr(updates, "_ask_github", offline)
    view = asked_at_start(store, settings).view(store, settings, "idle")
    assert view["ask"] is False and view["checking"] is False and view["failed"] is False


def test_this_copy_is_the_newest_no_question(download, settings, store, monkeypatch):
    monkeypatch.setattr(updates, "_ask_github", github(sha=MINE, date="2026-11-13T10:00:00Z"))
    assert asked_at_start(store, settings).view(store, settings, "idle")["ask"] is False


def test_with_the_check_turned_off_github_isnt_asked_and_nothing_is(download, settings, store, monkeypatch):
    ask = github()
    monkeypatch.setattr(updates, "_ask_github", ask)
    settings.update_check = False
    view = asked_at_start(store, settings).view(store, settings, "idle")
    assert ask.asked == [] and view["ask"] is False and view["checking"] is False


def test_started_without_a_launcher_there_is_no_question_but_settings_say_it(download, settings, store, monkeypatch):
    # A developer's `python -m controller_inbox run` of a download: nothing would start CloseDesk again after an update.
    monkeypatch.delenv(updates.LAUNCHER_ENV)
    monkeypatch.setattr(updates, "_ask_github", github())
    view = asked_at_start(store, settings).view(store, settings, "idle")
    assert view["ask"] is False and view["checking"] is False
    shown = updates.status(store, settings)
    assert shown["newer"] is True and shown["launcher"] is False and "CloseDesk-Update" in shown["by_hand"]


def test_a_developers_copy_is_never_asked(settings, store, monkeypatch):
    monkeypatch.setenv(updates.LAUNCHER_ENV, "1")
    ask = github()
    monkeypatch.setattr(updates, "_ask_github", ask)
    view = asked_at_start(store, settings).view(store, settings, "idle")
    assert ask.asked == [] and view["ask"] is False


def test_while_mail_is_processed_the_question_says_so(download, settings, store, monkeypatch):
    monkeypatch.setattr(updates, "_ask_github", github())
    question = asked_at_start(store, settings)
    busy = question.view(store, settings, "running")
    assert busy["ask"] is True and busy["busy"] is True and busy["busy_message"] == updates.BUSY
    assert busy["done_message"] == "The mail is done: CloseDesk can update now."
    assert question.view(store, settings, "done")["busy"] is False


# An update that didn't finish --------------------------------------------------------------------------------


def test_an_update_that_didnt_finish_is_said_when_closedesk_opens_again(download, settings, store, monkeypatch):
    monkeypatch.setattr(updates, "_ask_github", offline)
    updates.remember_update_asked(store)  # as CloseDesk stops to update
    # The launcher's update failed (no internet, a file in use): CloseDesk opens again as the same version.
    failed = updates.update_unfinished(store)
    assert failed is True and updates.update_unfinished(store) is False, "read once"
    view = asked_at_start(store, settings, failed=failed).view(store, settings, "idle")
    assert view["ask"] is True and view["failed"] is True and view["newer"] is False
    assert view["title"] == "CloseDesk wasn't updated"
    assert view["failed_message"] == updates.UNFINISHED and "opened as it was" in updates.UNFINISHED
    assert "mail and settings are kept" in updates.UNFINISHED


def test_an_update_that_didnt_finish_offers_to_try_again_when_github_still_has_it(download, settings, store, monkeypatch):
    monkeypatch.setattr(updates, "_ask_github", github())
    view = asked_at_start(store, settings, failed=True).view(store, settings, "idle")
    assert view["ask"] is True and view["failed"] is True and view["newer"] is True
    assert view["title"] == "CloseDesk wasn't updated" and view["message"] == "CloseDesk 2026.11.16 is out; you have 2026.11.13."


def test_an_update_that_finished_is_not_said(download, settings, store):
    updates.remember_update_asked(store)
    (download / ".git_archival.txt").write_text(f"commit: {NEWER}\ndate: 2026-11-16\n", encoding="utf-8")
    assert updates.update_unfinished(store) is False
    assert updates.update_unfinished(store) is False


# The API the pages ask -----------------------------------------------------------------------------------------


def test_the_pages_ask_the_question_and_answer_not_now(download, settings, store, monkeypatch):
    monkeypatch.setattr(updates, "_ask_github", github())
    app = web.create_app(settings, store)
    client = TestClient(app)
    # Served without CloseDesk opening (it never asked GitHub as it opened): nothing to ask, even with a newer version
    # known from before.
    updates.check(store, settings, now=NOW, ask=github())
    assert updates.status(store, settings)["newer"] is True
    assert client.get("/api/update/question", headers=PAGE).json()["ask"] is False
    app.state.update_question = asked_at_start(store, settings)
    view = client.get("/api/update/question", headers=PAGE).json()
    assert view["ask"] is True and view["message"] == "CloseDesk 2026.11.16 is out; you have 2026.11.13."
    assert client.get("/api/update/question").status_code == 403, "only CloseDesk's own page may ask"
    assert client.post("/api/update/question", headers=PAGE, json={"answer": "maybe"}).status_code == 400
    assert client.post("/api/update/question", json={"answer": "not-now"}).status_code == 403
    assert client.post("/api/update/question", headers=PAGE, json={"answer": "not-now"}).json() == {"ok": True}
    assert client.get("/api/update/question", headers=PAGE).json()["ask"] is False


def test_update_now_is_refused_while_mail_is_processed_and_offered_when_done(download, settings, store, monkeypatch):
    monkeypatch.setattr(updates, "_ask_github", github())
    app = web.create_app(settings, store)
    app.state.update_question = asked_at_start(store, settings)
    stopped: list = []
    app.state.stop_server = lambda: stopped.append(1)
    client = TestClient(app)
    release = threading.Event()
    app.state.job.start(lambda progress: release.wait(10))
    try:
        assert client.get("/api/update/question", headers=PAGE).json()["busy"] is True
        refused = client.post("/api/update", headers=PAGE)
        assert refused.status_code == 409 and refused.json()["detail"] == updates.BUSY
        assert client.get("/api/update/question", headers=PAGE).json()["ask"] is True, "still asked: offered when done"
    finally:
        release.set()
    deadline = time.monotonic() + 10
    while app.state.job.snapshot()["state"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert client.get("/api/update/question", headers=PAGE).json()["busy"] is False
    assert client.post("/api/update", headers=PAGE).status_code == 200 and stopped == [1]


def test_once_an_update_is_asked_for_the_page_that_waits_isnt_asked(download, settings, store, monkeypatch):
    # Settings' Update button, pressed before the question came up: the classic page that waits for CloseDesk to stop
    # (settings_update.html) is a page too, and isn't asked.
    monkeypatch.setattr(updates, "_ask_github", github())
    app = web.create_app(settings, store)
    app.state.update_question = asked_at_start(store, settings)
    app.state.stop_server = lambda: None
    client = TestClient(app)
    assert client.post("/settings/update", headers={"origin": "http://testserver"}).status_code == 200
    assert client.get("/api/update/question", headers=PAGE).json()["ask"] is False


# CloseDesk opening -------------------------------------------------------------------------------------------


def _served(settings, store, monkeypatch, asked=None):
    """Runs cli._serve with the server faked: the app it would have served."""
    monkeypatch.setattr(cli, "_link_stored_mail", lambda *_a: None)
    served: list = []

    def run(app, *, host, port):
        served.append(app)
        if asked:
            asked(app)

    monkeypatch.setattr(cli, "_run_server", run)
    code = cli._serve(settings, store, host="127.0.0.1", port=8765)
    return served[0], code


def test_closedesk_asks_github_as_it_opens(download, settings, store, monkeypatch):
    ask = github()
    monkeypatch.setattr(updates, "_ask_github", ask)
    app, code = _served(settings, store, monkeypatch)
    assert code == 0
    question = app.state.update_question
    deadline = time.monotonic() + 10
    while question.checking():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert ask.waits[:1] == [updates.START_TIMEOUT_SECONDS]
    assert question.view(store, settings, "idle")["ask"] is True


def test_closedesk_remembers_an_update_was_asked_and_says_when_it_didnt_finish(download, settings, store, monkeypatch):
    monkeypatch.setattr(updates, "_ask_github", offline)

    def update_asked(app):
        app.state.update_asked = True

    _, code = _served(settings, store, monkeypatch, asked=update_asked)
    assert code == updates.UPDATE_AND_RESTART
    # The launcher's update didn't finish: CloseDesk opens again as it was, and its first page says so.
    monkeypatch.setenv("CLOSEDESK_REOPEN", "1")
    app, code = _served(settings, store, monkeypatch)
    question = app.state.update_question
    while question.checking():
        time.sleep(0.01)
    view = question.view(store, settings, "idle")
    assert view["ask"] is True and view["failed"] is True
    # Said once: opened again after that, it isn't.
    app, _ = _served(settings, store, monkeypatch)
    while app.state.update_question.checking():
        time.sleep(0.01)
    assert app.state.update_question.view(store, settings, "idle")["ask"] is False


# The question is the opening's: not asked later in the run -------------------------------------------------------------


def test_a_newer_version_found_later_in_the_run_isnt_asked_about_on_a_later_page(download, settings, store, monkeypatch):
    # Offline as CloseDesk opened (the VPN not up yet): nothing asked, as promised. The retry a minute later finds a
    # newer version: the next page opened, minutes into the work, asked "A new version of CloseDesk is ready".
    monkeypatch.setattr(updates, "_ask_github", offline)
    question = asked_at_start(store, settings)
    assert question.view(store, settings, "idle")["ask"] is False
    updates.check(store, settings, ask=github())  # the retry (or the daily check of a CloseDesk left open)
    assert updates.status(store, settings)["newer"] is True, "Settings and the top bar say so"
    view = question.view(store, settings, "idle")
    assert view["ask"] is False and view["newer"] is False and view["message"] == ""


def test_an_answer_as_closedesk_opened_that_came_after_the_page_stopped_waiting_isnt_asked(download, settings, store, monkeypatch):
    # The first page waits about half a minute for the answer, then carries on: one that comes later is the retry's.
    slow = threading.Event()
    monkeypatch.setattr(updates, "_ask_github", github(hold=slow))
    monkeypatch.setattr(updates, "ASK_WAIT_SECONDS", 0.2)
    question = updates.Question()
    question.begin(store, settings)
    time.sleep(0.4)
    slow.set()
    deadline = time.monotonic() + 10
    while question.checking():
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert question.view(store, settings, "idle")["ask"] is False


def test_the_question_says_what_the_check_as_closedesk_opened_found(download, settings, store, monkeypatch):
    monkeypatch.setattr(updates, "_ask_github", github())
    question = asked_at_start(store, settings)
    # The daily check, later in the run, finds this copy the newest after all (main went back): the question asked of
    # a page reloaded meanwhile is the same one.
    later = github(sha=MINE, date="2026-11-13T10:00:00Z")
    updates.check(store, settings, now=datetime.now(timezone.utc) + timedelta(days=2), ask=later)
    assert later.asked and updates.status(store, settings)["newer"] is False
    view = question.view(store, settings, "idle")
    assert view["ask"] is True and view["message"] == "CloseDesk 2026.11.16 is out; you have 2026.11.13."


# Update now from a page that is out of date ---------------------------------------------------------------------------


def test_update_now_from_a_page_shown_before_closedesk_was_updated_is_refused(download, settings, store, monkeypatch):
    # Two tabs showed the question; Update now in one updated CloseDesk. Pressed in the other, it stopped the new
    # CloseDesk for nothing, and its next page said "CloseDesk wasn't updated".
    app = web.create_app(settings, store)
    stopped: list = []
    app.state.stop_server = lambda: stopped.append(1)
    client = TestClient(app)
    reply = client.post("/api/update", headers=PAGE, json={"commit": "c" * 40})
    assert reply.status_code == 409 and reply.json()["detail"] == updates.UPDATED_MEANWHILE and stopped == []
    assert "Reload" in updates.UPDATED_MEANWHILE
    classic = client.post("/settings/update", headers={"origin": "http://testserver"}, data={"commit": "c" * 40})
    assert classic.status_code == 409 and updates.UPDATED_MEANWHILE in classic.text and stopped == []
    # The page that shows this version: as before.
    assert client.post("/api/update", headers=PAGE, json={"commit": MINE}).status_code == 200 and stopped == [1]


def test_an_update_that_found_this_the_newest_isnt_said_to_have_failed(download, settings, store):
    updates.remember_update_asked(store)  # CloseDesk stopped to update...
    (download / ".venv").mkdir()
    (download / ".venv" / updates.NEWEST_NOTE).write_text(MINE + "\n", encoding="utf-8")  # ...and was the newest
    assert updates.update_unfinished(store) is False
    assert not (download / ".venv" / updates.NEWEST_NOTE).exists(), "read once"
    updates.remember_update_asked(store)
    assert updates.update_unfinished(store) is True, "an update that didn't say so did fail"


# The page waiting for CloseDesk to come back ---------------------------------------------------------------------------


def test_each_start_of_closedesk_answers_as_itself(download, settings, store):
    # A failed update starts CloseDesk again as the same version in about a second and a half: the page polling every
    # two seconds never saw it go, nor a new version, and said "Updating CloseDesk" for good.
    first, second = web.create_app(settings, store), web.create_app(settings, store)
    one, two = TestClient(first).get("/health").json(), TestClient(second).get("/health").json()
    assert one["run"] and two["run"] and one["run"] != two["run"]
    first.state.stop_server = lambda: None
    reply = TestClient(first).post("/api/update", headers=PAGE).json()
    assert reply["run"] == one["run"], "the page waits for another start than this one"
    classic = web.create_app(settings, store)
    classic.state.stop_server = lambda: None
    page = TestClient(classic).post("/settings/update", headers={"origin": "http://testserver"}).text
    assert TestClient(classic).get("/health").json()["run"] in page


# What the question says --------------------------------------------------------------------------------------------


def test_refused_while_mail_is_processed_the_question_remembers_it_was_asked(download, settings, store, monkeypatch):
    # The classic Today page loads again when the mail is done, before the question's own look: asked afresh, it said
    # nothing of the update it had been asked for.
    monkeypatch.setattr(updates, "_ask_github", github())
    app = web.create_app(settings, store)
    app.state.update_question = asked_at_start(store, settings)
    app.state.stop_server = lambda: None
    client = TestClient(app)
    assert client.get("/api/update/question", headers=PAGE).json()["held"] is False
    release = threading.Event()
    app.state.job.start(lambda progress: release.wait(10))
    try:
        assert client.post("/api/update", headers=PAGE).status_code == 409
        view = client.get("/api/update/question", headers=PAGE).json()
        assert view["held"] is True and view["busy"] is True
    finally:
        release.set()


def test_the_words_said_while_mail_is_processed_fit_every_page():
    # Only the workspace and the classic Today page have the bar at the top.
    assert "bar" not in updates.BUSY and "processing mail" in updates.BUSY


def test_updating_closedesk_is_said_once_under_its_heading():
    assert "Updating CloseDesk" not in updates.UPDATING and "opens again by itself" in updates.UPDATING
