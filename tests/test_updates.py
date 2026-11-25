"""Saying when a newer CloseDesk is out, and updating from inside it: the daily question to GitHub, what Settings and
the top bar show, the Update button, CloseDesk stopping with the exit code its launchers act on, and the launchers'
part. (tests/launcher_smoke.py runs the real launcher through a whole update.)"""

from __future__ import annotations

import importlib.util
import json
import re
import socket
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller_inbox import cli, updates, version, web
from controller_inbox.config import Settings
from helpers import PAGE

ROOT = Path(__file__).resolve().parent.parent
MINE = "a" * 40
NEWER = "b" * 40
SAME = {"origin": "http://testserver"}
NOW = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)
REAL_ASK = updates._ask_github


def github(sha: str = NEWER, date: str = "2026-11-16T09:12:00Z"):
    """GitHub's answer for main, and the URLs it was asked."""
    asked: list[str] = []

    def ask(url: str, **_waits) -> dict:
        asked.append(url)
        return {"sha": sha, "commit": {"committer": {"date": date}, "message": "anything"}}

    ask.asked = asked
    return ask


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

    def refuse(url, **_waits):
        raise AssertionError(f"a test asked the real GitHub: {url}")

    monkeypatch.setattr(updates, "_ask_github", refuse)
    monkeypatch.delenv(updates.LAUNCHER_ENV, raising=False)
    monkeypatch.delenv("CLOSEDESK_REOPEN", raising=False)


# The daily question ------------------------------------------------------------------------------------------


def test_a_newer_main_is_said_with_both_version_numbers(download, settings, store):
    ask = github()
    found = updates.check(store, settings, now=NOW, ask=ask)
    assert ask.asked == ["https://api.github.com/repos/MNabikhel/outlook-email/commits?sha=main&per_page=1"]
    assert found["sha"] == NEWER and found["date"] == "2026-11-16"
    shown = updates.status(store, settings)
    assert shown["newer"] is True and shown["latest"] == "2026.11.16"
    assert shown["message"] == "A newer CloseDesk is ready (2026.11.16; you have 2026.11.13)."
    assert shown["label"] == "CloseDesk 2026.11.13 (aaaaaaa)"


def test_main_at_this_copys_commit_is_not_newer(download, settings, store):
    updates.check(store, settings, now=NOW, ask=github(sha=MINE, date="2026-11-13T10:00:00Z"))
    shown = updates.status(store, settings)
    assert shown["newer"] is False and shown["message"] == ""


def test_main_older_than_this_copy_is_not_offered(download, settings, store):
    # A copy downloaded from another branch, newer than main: going "up" to main would go back.
    updates.check(store, settings, now=NOW, ask=github(date="2026-11-01T10:00:00Z"))
    assert updates.status(store, settings)["newer"] is False


@pytest.mark.parametrize(
    "answer",
    [
        OSError("no internet"),
        {"message": "API rate limit exceeded"},
        {"sha": "not a commit", "commit": {"committer": {"date": "2026-11-16T00:00:00Z"}}},
        {"sha": NEWER, "commit": {"committer": {"date": "yesterday"}}},
    ],
)
def test_no_answer_or_a_strange_one_is_asked_again_tomorrow(download, settings, store, answer):
    asked: list = []

    def ask(url):
        asked.append(url)
        if isinstance(answer, Exception):
            raise answer
        return answer

    found = updates.check(store, settings, now=NOW, ask=ask)
    assert "sha" not in found and found["checked_at"] == NOW.isoformat()
    assert updates.status(store, settings)["newer"] is False
    updates.check(store, settings, now=NOW + timedelta(hours=3), ask=ask)
    assert len(asked) == 1, "not asked again and again while the internet is down"
    updates.check(store, settings, now=NOW + timedelta(days=1, minutes=1), ask=ask)
    assert len(asked) == 2


def test_github_is_asked_once_a_day_at_most(download, settings, store):
    ask = github()
    updates.check(store, settings, now=NOW, ask=ask)
    updates.check(store, settings, now=NOW + timedelta(hours=23, minutes=59), ask=ask)
    assert len(ask.asked) == 1
    later = github(sha="c" * 40, date="2026-11-17T08:00:00Z")
    updates.check(store, settings, now=NOW + timedelta(days=1), ask=later)
    assert len(later.asked) == 1
    assert updates.status(store, settings)["message"] == "A newer CloseDesk is ready (2026.11.17; you have 2026.11.13)."
    # A clock set back (the last asking seems to be in the future) doesn't stop the asking for good.
    back = github()
    updates.check(store, settings, now=NOW - timedelta(days=3), ask=back)
    assert len(back.asked) == 1


def test_after_an_update_the_old_answer_is_not_offered_and_github_is_asked_again(download, settings, store):
    # 09:00: main is b..., dated today. Later the same day main moves on and the user updates to c..., also dated
    # today. The morning's answer was about the copy before: it isn't offered, and GitHub is asked again soon.
    ask = github(sha=NEWER, date="2026-11-16T09:00:00Z")
    updates.check(store, settings, now=NOW, ask=ask)
    assert updates.status(store, settings)["newer"] is True
    (download / ".git_archival.txt").write_text(f"commit: {'c' * 40}\ndate: 2026-11-16T14:00:00Z\n", encoding="utf-8")
    shown = updates.status(store, settings)
    assert shown["newer"] is False and shown["message"] == ""
    again = github(sha="c" * 40, date="2026-11-16T14:00:00Z")
    updates.check(store, settings, now=NOW + timedelta(hours=5), ask=again)
    assert len(again.asked) == 1, "asked again, though a day hasn't passed: the answer was for the copy before"
    assert updates.status(store, settings)["newer"] is False
    later = github()
    updates.check(store, settings, now=NOW + timedelta(hours=6), ask=later)
    assert later.asked == [], "then once a day again"


def test_an_evening_commit_west_of_utc_has_one_version_number(download, settings, store):
    # A commit made at 22:12 at -06:00 is November 13 in UTC. GitHub says so; the downloaded copy says the same.
    (download / ".git_archival.txt").write_text(f"commit: {NEWER}\ndate: 2026-11-12T22:12:03-06:00\n", encoding="utf-8")
    assert version.installed() == (NEWER, "2026-11-13")
    assert version.label() == "CloseDesk 2026.11.13 (bbbbbbb)"
    assert version.number("2026-11-12T22:12:03-06:00") == "2026.11.13"
    assert version.number("2026-11-13T04:12:03Z") == "2026.11.13"
    assert version.number("2026-11-13") == "2026.11.13"
    archival = (ROOT / ".git_archival.txt").read_text(encoding="utf-8")
    assert "date: $Format:%cI$" in archival, "GitHub writes the commit's date with its offset, not a local day"


def test_github_is_reached_with_the_computers_own_certificates_when_python_cant(monkeypatch):
    # Behind a company proxy that inspects HTTPS, Python's own certificates fail; curl trusts the computer's.
    import subprocess

    import httpx

    def refused(*_a, **_k):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate in certificate chain")

    ran: list = []

    def curl(cmd, **_k):
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"sha": NEWER}), stderr="")

    monkeypatch.setattr(httpx, "stream", refused)
    monkeypatch.setattr(updates.shutil, "which", lambda name: "/usr/bin/curl" if name == "curl" else None)
    monkeypatch.setattr(updates.subprocess, "run", curl)
    assert updates._fetch_json(updates.LATEST_URL) == {"sha": NEWER}
    assert ran and ran[0][0] == "/usr/bin/curl" and updates.LATEST_URL in ran[0]


def _curl_as_on_windows(answer: bytes, ran: list):
    """curl giving GitHub's answer (UTF-8 bytes), with Python's decoding of a program's output as on Windows: text=True
    reads it in the ANSI code page (cp1252), not UTF-8."""
    import subprocess

    def curl(cmd, **kwargs):
        ran.append((cmd, kwargs))
        if kwargs.get("text") or kwargs.get("encoding") or kwargs.get("universal_newlines"):
            return subprocess.CompletedProcess(cmd, 0, stdout=answer.decode(kwargs.get("encoding") or "cp1252"), stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=answer, stderr=b"")

    return curl


@pytest.mark.parametrize("title", ["Colour-blind settings in Setup \u2192 Colors", "Say \u201cprocessed\u201d \u2190 not \u201cread\u201d"])
def test_whats_new_comes_whole_through_curl_on_windows(download, settings, store, monkeypatch, title):
    # The curl fallback read GitHub's UTF-8 answer in Windows' code page: "Setup \u00e2\u2020\u2019 Colors" in the
    # question, and a \u201d (byte 0x9d) stopped the question altogether, with Settings saying GitHub couldn't be reached.
    import httpx

    def refused(*_a, **_k):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate in certificate chain")

    answer = json.dumps(
        [{"sha": NEWER, "commit": {"committer": {"date": "2026-11-16T09:12:00Z"}, "message": title}}], ensure_ascii=False
    ).encode("utf-8")
    ran: list = []
    monkeypatch.setattr(httpx, "stream", refused)
    monkeypatch.setattr(updates.shutil, "which", lambda name: "/usr/bin/curl" if name == "curl" else None)
    monkeypatch.setattr(updates.subprocess, "run", _curl_as_on_windows(answer, ran))
    monkeypatch.setattr(updates, "_ask_github", REAL_ASK)
    saved = updates.check(store, settings, now=NOW)
    assert "failed" not in saved and saved["title"] == title
    assert updates.status(store, settings)["whats_new"] == title


def test_github_is_asked_for_the_newest_commit_alone_not_its_changes(monkeypatch):
    # GitHub's answer for one commit carries every changed file's patch: hundreds of kilobytes for a merge, too much for
    # the few seconds as CloseDesk opens on a slow link. The list of commits, one long, says the same without them.
    from urllib.parse import parse_qs, urlsplit

    parts = urlsplit(updates.LATEST_URL)
    assert parts.path == "/repos/MNabikhel/outlook-email/commits"
    assert parse_qs(parts.query) == {"sha": ["main"], "per_page": ["1"]}
    import httpx

    def refused(*_a, **_k):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]")

    ran: list = []
    monkeypatch.setattr(httpx, "stream", refused)
    monkeypatch.setattr(updates.shutil, "which", lambda name: "/usr/bin/curl" if name == "curl" else None)
    monkeypatch.setattr(updates.subprocess, "run", _curl_as_on_windows(b"[]", ran))
    updates._fetch_json(updates.LATEST_URL, timeout=4)
    assert "--compressed" in ran[0][0], "curl asks for the answer compressed, as httpx does"


def test_githubs_list_of_one_commit_is_read(download, settings, store):
    def ask(url, **_waits):
        return [{"sha": NEWER, "commit": {"committer": {"date": "2026-11-16T09:12:00Z"}, "message": "Faster totals"}}]

    saved = updates.check(store, settings, now=NOW, ask=ask)
    assert saved["sha"] == NEWER and saved["title"] == "Faster totals"
    assert updates.check(store, settings, now=NOW + timedelta(days=2), ask=lambda url, **_w: [])["failed"] == "1"


def test_settings_say_when_github_couldnt_be_reached(download, settings, store):
    def down(url):
        raise OSError("certificate verify failed")

    updates.check(store, settings, now=NOW, ask=down)
    note = updates.status(store, settings)["note"]
    assert "couldn't reach GitHub on November 16" in note and "CloseDesk-Update" in note
    updates.check(store, settings, now=NOW + timedelta(days=1, minutes=1), ask=github())
    assert "couldn't reach" not in updates.status(store, settings)["note"]


def test_turned_off_in_env_nothing_is_asked_or_shown(download, settings, store, monkeypatch):
    monkeypatch.setenv("CONTROLLER_INBOX_UPDATE_CHECK", "false")
    assert Settings(_env_file=None).update_check is False
    updates.check(store, settings, now=NOW, ask=github())  # an answer from before it was turned off
    settings.update_check = False
    ask = github()
    assert updates.check(store, settings, now=NOW + timedelta(days=2), ask=ask) == {} and ask.asked == []
    shown = updates.status(store, settings)
    assert shown["newer"] is False and shown["check_on"] is False
    assert "CONTROLLER_INBOX_UPDATE_CHECK=false" in shown["note"] and "turned off" in shown["note"]


def test_a_developers_copy_never_asks(settings, store):
    assert version.installed() == ("", "")
    ask = github()
    assert updates.check(store, settings, now=NOW, ask=ask) == {} and ask.asked == []
    shown = updates.status(store, settings)
    assert shown["developer"] is True and shown["newer"] is False and "git" in shown["note"]


def test_the_open_closedesk_asks_by_itself(download, settings, store, monkeypatch):
    ask = github()
    monkeypatch.setattr(updates, "_ask_github", ask)
    monkeypatch.setattr(updates, "FIRST_TICK_SECONDS", 0)
    updates.start_checking(store, settings)
    deadline = time.monotonic() + 10
    while not ask.asked and time.monotonic() < deadline:
        time.sleep(0.02)
    assert ask.asked and updates.status(store, settings)["newer"] is True


def test_the_settings_say_what_the_check_does_in_plain_words(download, settings, store):
    note = updates.status(store, settings)["note"]
    assert "When CloseDesk opens, and once a day" in note and "nothing about your mail is sent" in note and "CONTROLLER_INBOX_UPDATE_CHECK=false" in note


# What the pages show -----------------------------------------------------------------------------------------


def test_the_workspace_shows_the_version_the_notice_and_the_button(download, settings, store):
    client = TestClient(web.create_app(settings, store))
    state = client.get("/api/state", headers=PAGE).json()
    assert state["closedesk"] == {"label": "CloseDesk 2026.11.13 (aaaaaaa)", "newer": False, "message": ""}
    updates.check(store, settings, now=NOW, ask=github())
    state = client.get("/api/state", headers=PAGE).json()
    assert state["closedesk"]["newer"] is True and "2026.11.16" in state["closedesk"]["message"]
    found = client.get("/api/settings", headers=PAGE).json()
    assert found["about"]["version"] == "CloseDesk 2026.11.13 (aaaaaaa)"
    assert found["update"]["newer"] is True and found["update"]["launcher"] is True
    assert found["update"]["message"] == "A newer CloseDesk is ready (2026.11.16; you have 2026.11.13)."
    assert client.get("/health").json()["commit"] == MINE


def test_the_classic_setup_page_shows_the_notice_and_the_button(download, settings, store, monkeypatch):
    client = TestClient(web.create_app(settings, store))
    page = client.get("/settings").text
    assert "CloseDesk 2026.11.13 (aaaaaaa)" in page and "A newer CloseDesk" not in page
    assert "When CloseDesk opens, and once a day" in page
    updates.check(store, settings, now=NOW, ask=github())
    page = client.get("/settings").text
    assert "A newer CloseDesk is ready (2026.11.16; you have 2026.11.13)." in page
    assert 'action="/settings/update"' in page
    assert 'href="/settings#update">Update ready</a>' in client.get("/").text, "every classic page says so, quietly"
    # Started without its launcher: no button, but how to update.
    monkeypatch.delenv(updates.LAUNCHER_ENV)
    page = client.get("/settings").text
    assert 'action="/settings/update"' not in page and "double-click CloseDesk-Update" in page


# The Update button -------------------------------------------------------------------------------------------


class Stopper:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> None:
        self.calls += 1


def test_the_update_button_stops_closedesk_for_its_launcher(download, settings, store):
    app = web.create_app(settings, store)
    app.state.stop_server = stop = Stopper()
    client = TestClient(app)
    reply = client.post("/api/update", headers=PAGE)
    assert reply.status_code == 200 and reply.json()["message"] == updates.UPDATING and reply.json()["commit"] == MINE
    assert stop.calls == 1 and app.state.update_asked is True
    assert client.post("/api/update").status_code == 403, "only CloseDesk's own page may ask"


def test_closedesk_started_by_the_window_tells_it_at_once_that_it_stops_to_update(download, settings, store, monkeypatch, tmp_path):
    # CloseDesk takes a few seconds to stop once Update now is answered, and stops answering at once: the window learns
    # from the file it named (closing it meanwhile asks first, and the update still runs).
    mark = tmp_path / "window" / "update-asked"
    monkeypatch.setenv(updates.WINDOW_ASKED_ENV, str(mark))
    app = web.create_app(settings, store)
    app.state.stop_server = stop = Stopper()
    assert TestClient(app).post("/api/update", headers=PAGE).status_code == 200
    assert stop.calls == 1 and mark.is_file()


def test_an_update_refused_tells_the_window_nothing(download, settings, store, monkeypatch, tmp_path):
    mark = tmp_path / "update-asked"
    monkeypatch.setenv(updates.WINDOW_ASKED_ENV, str(mark))
    app = web.create_app(settings, store)
    app.state.stop_server = Stopper()
    release = threading.Event()
    app.state.job.start(lambda progress: release.wait(10))
    try:
        assert TestClient(app).post("/api/update", headers=PAGE).status_code == 409
    finally:
        release.set()
    assert not mark.exists()


def test_the_update_button_waits_while_mail_is_processed(download, settings, store):
    app = web.create_app(settings, store)
    app.state.stop_server = stop = Stopper()
    release = threading.Event()
    app.state.job.start(lambda progress: release.wait(10))
    try:
        reply = TestClient(app).post("/api/update", headers=PAGE)
        assert reply.status_code == 409 and reply.json()["detail"] == updates.BUSY
        classic = TestClient(app).post("/settings/update", headers=SAME)
        assert classic.status_code == 409 and updates.BUSY in classic.text
    finally:
        release.set()
    assert stop.calls == 0 and not getattr(app.state, "update_asked", False)


def test_once_the_update_is_asked_no_new_work_starts(download, settings, store):
    # The automatic Outlook sync (or anything else) starting in the seconds CloseDesk takes to stop would be cut off.
    app = web.create_app(settings, store)
    app.state.stop_server = Stopper()
    assert TestClient(app).post("/api/update", headers=PAGE).status_code == 200
    ran: list = []
    assert app.state.job.start(lambda progress: ran.append(1)) is False
    time.sleep(0.1)
    assert ran == [] and app.state.job.snapshot()["state"] != "running"


def test_work_started_just_before_the_update_holds_it_back(download, settings, store):
    app = web.create_app(settings, store)
    app.state.stop_server = stop = Stopper()
    release = threading.Event()
    app.state.job.start(lambda progress: release.wait(10))
    try:
        # The page saw no work running; by the time the update is asked, a sync has started.
        assert updates.stop_to_update(app.state, "idle") == updates.BUSY
    finally:
        release.set()
    assert stop.calls == 0 and not getattr(app.state, "update_asked", False)


def test_without_a_launcher_the_button_says_to_use_closedesk_update(download, settings, store, monkeypatch):
    monkeypatch.delenv(updates.LAUNCHER_ENV)
    app = web.create_app(settings, store)
    app.state.stop_server = stop = Stopper()
    reply = TestClient(app).post("/api/update", headers=PAGE)
    assert reply.status_code == 409 and "double-click CloseDesk-Update" in reply.json()["detail"]
    assert stop.calls == 0
    assert TestClient(web.create_app(settings, store)).get("/api/settings", headers=PAGE).json()["update"]["launcher"] is False


def test_a_developers_copy_is_not_updated_from_the_page(settings, store, monkeypatch):
    monkeypatch.setenv(updates.LAUNCHER_ENV, "1")
    app = web.create_app(settings, store)
    app.state.stop_server = stop = Stopper()
    reply = TestClient(app).post("/api/update", headers=PAGE)
    assert reply.status_code == 409 and "git" in reply.json()["detail"] and stop.calls == 0


def test_the_classic_update_button_shows_the_waiting_page(download, settings, store):
    app = web.create_app(settings, store)
    app.state.stop_server = stop = Stopper()
    page = TestClient(app).post("/settings/update", headers=SAME)
    assert page.status_code == 200 and updates.UPDATING in page.text and "/health" in page.text
    assert stop.calls == 1
    # The classic pages' own bar, as the question's updating dialog has (not the browser's bright blue <progress>).
    assert "<progress" not in page.text and 'class="update-ask-bar" role="progressbar"' in page.text
    assert TestClient(app).post("/settings/update", headers={"origin": "http://elsewhere.example"}).status_code == 403


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_closedesk_ends_with_the_update_exit_code_when_the_button_asks(download, settings, store, monkeypatch):
    # The real server: the button's request is answered, then CloseDesk stops and _serve says "update and start again".
    monkeypatch.setattr(cli, "_link_stored_mail", lambda *_a: None)
    settings.update_check = False
    port = _free_port()
    ended: list = []
    thread = threading.Thread(target=lambda: ended.append(cli._serve(settings, store, host="127.0.0.1", port=port)), daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    while True:
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=2) as reply:
                assert json.load(reply)["commit"] == MINE
            break
        except OSError:
            assert time.monotonic() < deadline, "the dashboard didn't start"
            time.sleep(0.05)
    asked = urllib.request.Request(f"{base}/api/update", method="POST", data=b"{}", headers={**PAGE, "Content-Type": "application/json"})
    with urllib.request.urlopen(asked, timeout=10) as reply:
        assert json.load(reply)["message"] == updates.UPDATING
    thread.join(timeout=20)
    assert not thread.is_alive() and ended == [updates.UPDATE_AND_RESTART] == [75]


def test_closedesk_stopped_any_other_way_ends_as_before(settings, store, monkeypatch):
    monkeypatch.setattr(cli, "_link_stored_mail", lambda *_a: None)
    monkeypatch.setattr(cli, "_run_server", lambda *a, **k: None)
    assert cli._serve(settings, store, host="127.0.0.1", port=_free_port()) == 0


def test_started_again_after_an_update_no_second_tab_is_opened(settings, store, monkeypatch):
    opened: list = []
    monkeypatch.setattr(cli, "_open_browser_soon", lambda url: opened.append(url))
    monkeypatch.setattr(cli, "_serve", lambda *a, **k: 0)
    args = type("Args", (), {"no_serve": False, "no_browser": False, "limit": 0, "host": "127.0.0.1", "port": _free_port()})()
    assert cli._run(settings, store, args) == 0 and len(opened) == 1
    monkeypatch.setenv("CLOSEDESK_REOPEN", "1")
    cli._remember_port(store, args.port)  # as _serve does when it stops to update
    assert cli._run(settings, store, args) == 0 and len(opened) == 1


def test_started_again_on_another_port_the_browser_is_opened(settings, store, monkeypatch):
    # Before the update CloseDesk was on the next port (another program had the usual one). The page that waits
    # asks that port, so CloseDesk opens there again when it is free, and without a tab.
    monkeypatch.setattr(cli, "_open_browser_soon", lambda url: opened.append(url))
    served: list = []
    monkeypatch.setattr(cli, "_serve", lambda *a, **k: served.append(k["port"]) or 0)
    usual, before = _free_port(), _free_port()
    args = type("Args", (), {"no_serve": False, "no_browser": False, "limit": 0, "host": "127.0.0.1", "port": usual})()
    monkeypatch.setenv("CLOSEDESK_REOPEN", "1")
    opened: list = []
    cli._remember_port(store, before)
    assert cli._run(settings, store, args) == 0
    assert served == [before] and opened == [], "the page waiting on the port before finds it again"
    # That port taken now: it opens on another, and a tab is opened there (the waiting page can't find it).
    cli._remember_port(store, before)
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", before))
        taken.listen()
        assert cli._run(settings, store, args) == 0
    assert served[-1] != before and opened == [f"http://127.0.0.1:{served[-1]}/"]


def test_the_port_is_remembered_when_closedesk_stops_to_update(download, settings, store, monkeypatch):
    monkeypatch.setattr(cli, "_link_stored_mail", lambda *_a: None)
    settings.update_check = False

    def asked(app, *, host, port):
        app.state.update_asked = True

    monkeypatch.setattr(cli, "_run_server", asked)
    port = _free_port()
    assert cli._serve(settings, store, host="127.0.0.1", port=port) == updates.UPDATE_AND_RESTART
    assert cli._reopened_port(store) == port and cli._reopened_port(store) == 0, "read once"


# The launchers' part -----------------------------------------------------------------------------------------


def test_launch_tells_closedesk_a_launcher_started_it(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("launch_for_updates", ROOT / "scripts" / "launch.py")
    launch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launch)
    handed: list = []
    monkeypatch.setattr(launch, "in_venv", lambda: True)
    monkeypatch.setattr(launch, "ensure_packages", lambda: 0)
    monkeypatch.setattr(launch, "_hand_over", lambda cmd: handed.append((cmd, dict(__import__("os").environ))) or 0)
    assert launch.main(["--command", "overnight"]) == 0
    assert updates.LAUNCHER_ENV not in handed[-1][1], "only the dashboard has an Update button"
    assert launch.main(["--no-browser"]) == 0
    assert handed[-1][0][-3:] == ["controller_inbox", "run", "--no-browser"] and handed[-1][1][updates.LAUNCHER_ENV] == "1"
    assert launch.UPDATE == updates.UPDATE_AND_RESTART


def test_the_mac_launcher_updates_and_starts_the_new_file_in_its_place():
    text = (ROOT / "CloseDesk.command").read_text(encoding="utf-8")
    block = text[text.index("    75)") : text.index(";;", text.index("    75)"))]
    assert "scripts/launch.py --update" in block and "export CLOSEDESK_REOPEN=1" in block
    assert re.search(r'exec bash "\$PWD/CloseDesk\.command"', block), "the old file isn't read on after the update replaced it"


def test_the_windows_launcher_updates_and_starts_again_from_one_block_read_whole():
    raw = (ROOT / "CloseDesk.bat").read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""), "cmd.exe needs CRLF line endings"
    text = raw.decode("ascii")
    assert 'if "%CODE%"=="75" goto :update_and_reopen' in text
    part = text[text.index(":update_and_reopen\r\n") :]
    blocks = re.findall(r"\(\r\n(.*?)\r\n\)", part, flags=re.S)[:2]
    assert len(blocks) == 2, "one block with --get-python, one without"
    for block in blocks:
        lines = [line.strip() for line in block.splitlines()]
        # The update replaces this file: the start of the new one, and the end, are in the block cmd has read already.
        # (The drive letter a network folder was lent goes back first: the new file lends itself its own.)
        assert lines[0] == r"call %PY% scripts\launch.py --update" and lines[1] == "popd"
        assert lines[2].startswith('call "%~f0"') and lines[3] == "exit /b"


# Subroutines, reached with call: their exit /b goes back to the line that called them, not out of the file.
BAT_SUBROUTINES = {":try", ":find_python", ":get_uv", ":managed"}


def test_the_windows_launcher_works_from_a_folder_on_a_network_share():
    # cmd can't be in a folder on a network share (\\server\share): "cd /d" there fails, and the launcher would run in
    # C:\Windows, where there is no CloseDesk. Windows names a mapped drive (H:) so to the shortcut and the window, which
    # open CloseDesk.bat by that name when the window can't open. pushd lends the folder a drive letter instead.
    text = (ROOT / "CloseDesk.bat").read_bytes().decode("ascii")
    lines = text.split("\r\n")
    code = [line.strip() for line in lines if line.strip() and not line.strip().lower().startswith("rem")]
    assert not any(line.lower().startswith("cd ") for line in code), "cd can't go into a folder on a network share"
    assert 'pushd "%~dp0"' in code and code.index('pushd "%~dp0"') < code.index("set /a TRIES=0")
    # Every way out of the file gives the drive letter back (popd), or a letter is used up at each start.
    section, ways_out = "", []
    for at, line in enumerate(code):
        if line.startswith(":") and " " not in line:
            section = line
        if "exit /b" in line and section not in BAT_SUBROUTINES:
            ways_out.append(at)
            before = " ".join(code[max(0, at - 3) : at]) + " " + line.split("exit /b")[0]
            assert "popd" in before, f"{line!r} (after {section or 'the start'}) leaves without popd"
    assert len(ways_out) >= 9, ways_out
    # CloseDesk-Update.bat doesn't go into the folder itself: it runs CloseDesk.bat by its whole path, which does.
    update = (ROOT / "CloseDesk-Update.bat").read_bytes().decode("ascii")
    assert "cd /d" not in update and 'call "%~dp0CloseDesk.bat" --update' in update


@pytest.mark.browser
def test_the_workspace_says_an_update_is_ready_and_waits_for_the_new_version(download, settings, loaded, page):
    from liveserver import serving

    updates.check(loaded, settings, now=NOW, ask=github())
    app = web.create_app(settings, loaded)
    app.state.stop_server = stop = Stopper()
    with serving(app) as base:
        page.goto(f"{base}/app")
        page.wait_for_selector("#top-update")
        assert page.inner_text("#top-update") == "Update ready"
        assert page.inner_text(".rail-version") == "CloseDesk 2026.11.13 (aaaaaaa)"
        page.click("#top-update")
        page.wait_for_selector("#set-update")
        assert "A newer CloseDesk is ready (2026.11.16; you have 2026.11.13)." in page.inner_text("#set-update")
        page.click("#set-update button:has-text('Update')")
        page.wait_for_selector("#updating")
        assert updates.UPDATING in page.inner_text("#updating") and stop.calls == 1
        # The launcher has put the new version in place and started it: the page loads again by itself.
        (download / ".git_archival.txt").write_text(f"commit: {NEWER}\ndate: 2026-11-16\n", encoding="utf-8")
        page.wait_for_function("() => (document.querySelector('.rail-version') || {}).textContent === 'CloseDesk 2026.11.16 (bbbbbbb)'", timeout=20000)
        assert page.query_selector("#top-update") is None, "this copy is main's now"
