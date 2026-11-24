"""A run that found nothing new said "Read 0 new files." in the workspace's toast, on the classic page and in the
launcher window. It now says "No new files."; one or more is still "Read 1 new file." or "Read 3 new files.", and the
rest of each message is as it was. The toast is checked in a real Chromium (skipped without it).

The count is of the drop folder's files only: mail Outlook sync brings in is filed apart from it, so a run that
copied 3 new emails from Outlook counts 0. "No new files." there said nothing new came in, next to "Outlook synced:
3 new.". Such a run leaves the count unsaid (or, when dropped files were already filed, says it is the drop folder's).
The `ingest` command words its count the same way.

The workspace's toast never said files that were already filed, as the classic page and the launcher window do: someone
who dropped 2 files again read only "No new files.". It now says "No new files, 2 already filed.", worded as
cli.files_read_said words it."""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from email.message import EmailMessage

import pytest
from fastapi.testclient import TestClient

from controller_inbox import cli, outlook_desktop, outlook_sync, overnight, web
from liveserver import serving

COUNTS = [
    (0, 0, "No new files."),
    (0, 3, "No new files, 3 already filed."),
    (1, 0, "Read 1 new file."),
    (4, 1, "Read 4 new files, 1 already filed."),
]

# A run that copied 3 new emails from Outlook, with nothing in the drop folder.
FROM_OUTLOOK = {"outlook": {"new": 3, "updated": 0, "moved": 0, "removed": 0, "failed": []}, "outlook_message": "Outlook synced: 3 new."}


def _printed(capsys, **result) -> str:
    """The line of the launcher window (and of the overnight command) that says what the run read."""
    run = {
        "headline": "Tuesday's digest", "ingested": 0, "read_by_bionic": 0, "waiting_on_bionic": 0,
        "folders": {"important": 0, "informational": 0, "reference": 0}, "digest_path": "digest.md", "log_path": "run.log",
    }
    cli._print_overnight({**run, **result})
    lines = capsys.readouterr().out.splitlines()
    return lines[2] if result.get("outlook_message") else lines[1]


def test_the_launcher_window_says_no_new_files(capsys):
    assert _printed(capsys, ingested=0) == "No new files. The local model read 0."
    for ingested, already, said in COUNTS:
        assert _printed(capsys, ingested=ingested, already_read=already) == f"{said} The local model read 0."
    assert _printed(capsys, ingested=2, model="gemma-4-12b", read_by_bionic=2, avg_seconds=40) == (
        "Read 2 new files. The local model (gemma-4-12b) read 2 (about 40s each)."
    )


def test_the_launcher_window_says_nothing_of_the_drop_folder_when_outlook_brought_mail(capsys):
    assert _printed(capsys, ingested=0, model="gemma-4-12b", read_by_bionic=3, **FROM_OUTLOOK) == (
        "The local model (gemma-4-12b) read 3."
    )
    # Dropped files that were already filed are still said, as the drop folder's.
    assert _printed(capsys, ingested=0, already_read=2, **FROM_OUTLOOK) == (
        "No new files in the drop folder, 2 already filed. The local model read 0."
    )
    assert _printed(capsys, ingested=1, **FROM_OUTLOOK) == "Read 1 new file. The local model read 0."
    # A sync that found nothing new: nothing new came in at all.
    nothing = {"outlook": {"new": 0, "updated": 2}, "outlook_message": "Outlook synced: 2 updated."}
    assert _printed(capsys, ingested=0, **nothing) == "No new files. The local model read 0."


def _finish(job) -> None:
    deadline = time.monotonic() + 5
    while job.snapshot()["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.02)


def _classic_job(client, result: dict) -> str:
    job = client.app.state.job
    assert job.start(lambda progress: result)
    _finish(job)
    section = re.search(r'<section class="job[^"]*">(.*?)</section>', client.get("/").text, re.S)[1]
    return " ".join(re.sub(r"<[^>]+>", " ", section).split())


@pytest.mark.parametrize(("ingested", "already", "said"), COUNTS)
def test_the_classic_page_says_no_new_files(client, ingested, already, said):
    words = _classic_job(client, {"ingested": ingested, "already_read": already, "model": "", "read_by_bionic": 0})
    assert words.startswith(f"Processed. {said} No local model was running"), words


def test_the_classic_page_says_nothing_of_the_drop_folder_when_outlook_brought_mail(client):
    words = _classic_job(client, {"ingested": 0, "already_read": 0, "model": "", "read_by_bionic": 0, **FROM_OUTLOOK})
    assert words.startswith("Processed. No local model was running"), words
    words = _classic_job(client, {"ingested": 0, "already_read": 2, "model": "", "read_by_bionic": 0, **FROM_OUTLOOK})
    assert words.startswith("Processed. No new files in the drop folder, 2 already filed. No local model"), words


def test_a_run_that_copies_mail_from_outlook_never_says_no_new_files(store, settings, monkeypatch, capsys):
    """A real run: Outlook sync copies 3 new emails and the drop folder is empty, so the count is 0."""
    from test_outlook_sync import NOW, FakeOutlook

    store.set_state(outlook_sync.CONSENT_KEY, "2026-09-01T00:00:00+00:00")
    store.set_state(outlook_sync.AUTO_KEY, "1")
    fake = FakeOutlook()
    for index in range(3):
        fake.add(f"e{index}", f"Invoice INV-{index} from Northwind", minutes=index + 1)

    @contextmanager
    def connect(**_options):
        yield fake

    monkeypatch.setattr(outlook_desktop, "available", lambda: "")
    monkeypatch.setattr(outlook_desktop, "connect", connect)
    result = overnight.run_overnight(store, settings, sync_graph=False, now=NOW)
    assert result["ingested"] == 0 and result["outlook"]["new"] == 3
    capsys.readouterr()

    cli._print_overnight(result)
    printed = capsys.readouterr().out
    assert "Outlook synced: 3 new." in printed
    assert "No new files" not in printed, printed
    assert "The local model read 0." in printed.splitlines()

    words = _classic_job(TestClient(web.create_app(settings, store)), result)
    assert words.startswith("Processed. No local model was running"), words


def _eml(path, subject: str) -> None:
    message = EmailMessage()
    message["From"] = "Dana <dana@vendor.example>"
    message["To"] = "ap@taz.com"
    message["Subject"] = subject
    message["Date"] = "Mon, 19 Oct 2026 09:00:00 -0400"
    message["Message-ID"] = f"<{subject.replace(' ', '-')}@vendor.example>"
    message.set_content("Please see the attached statement for October.")
    path.write_bytes(bytes(message))


def test_the_ingest_command_says_no_new_files(settings, monkeypatch, capsys):
    monkeypatch.setenv("CONTROLLER_INBOX_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("CONTROLLER_INBOX_INBOX_DIR", str(settings.inbox_dir))
    settings.ensure_data_dir()
    assert cli.main(["ingest"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"Nothing new. Drop .msg or .eml files in {settings.inbox_incoming}", lines
    assert not any("Read 0" in line or "file(s) from" in line for line in lines), lines

    _eml(settings.inbox_incoming / "statement.eml", "October statement")
    assert cli.main(["ingest"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"Read 1 new file from {settings.inbox_incoming}", lines

    _eml(settings.inbox_incoming / "one.eml", "Remittance one")
    _eml(settings.inbox_incoming / "two.eml", "Remittance two")
    assert cli.main(["ingest"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == f"Read 2 new files from {settings.inbox_incoming}"


TOASTS = [
    ({"ingested": 0}, "Processed. No new files."),
    ({"ingested": 1}, "Processed. Read 1 new file."),
    ({"ingested": 3}, "Processed. Read 3 new files."),
    # Outlook brought the new mail: its line says what came, and none from the drop folder goes unsaid.
    ({"ingested": 0, **FROM_OUTLOOK}, "Processed. Outlook synced: 3 new."),
    ({"ingested": 0, "files_summarized": 1, **FROM_OUTLOOK}, "Processed. Summarized 1 file. Outlook synced: 3 new."),
    ({"ingested": 2, **FROM_OUTLOOK}, "Processed. Read 2 new files. Outlook synced: 3 new."),
    (
        {"ingested": 0, "outlook": {"new": 0, "updated": 0, "moved": 0, "removed": 0}, "outlook_message": "Outlook synced: nothing new."},
        "Processed. No new files. Outlook synced: nothing new.",
    ),
    # Dropped files that were already filed are said, as on the classic page and in the launcher window: someone who
    # just dropped 2 files and read "No new files." alone had no hint why nothing was read.
    ({"ingested": 0, "already_read": 2}, "Processed. No new files, 2 already filed."),
    ({"ingested": 4, "already_read": 1}, "Processed. Read 4 new files, 1 already filed."),
    (
        {"ingested": 0, "already_read": 2, **FROM_OUTLOOK},
        "Processed. No new files in the drop folder, 2 already filed. Outlook synced: 3 new.",
    ),
]


@pytest.mark.parametrize(("run", "said"), TOASTS)
def test_the_toast_counts_files_as_the_launcher_window_does(run, said):
    """The toast's count of the drop folder's files is worded as cli.files_read_said words it."""
    files = cli.files_read_said(run)
    synced = f" {run['outlook_message']}" if run.get("outlook_message") else ""
    summarized = " Summarized 1 file." if run.get("files_summarized") else ""
    assert said == f"Processed{'. ' + files if files else '.'}{summarized}{synced}"


@pytest.mark.parametrize(("run", "said"), TOASTS)
def test_process_new_mail_says_no_new_files(settings, store, page, monkeypatch, run, said):
    def processed(*_args, **_kw):
        time.sleep(1)  # the page sees the run going, then finish
        return {"model": "", "read_by_bionic": 0, **run}

    monkeypatch.setattr(overnight, "run_overnight", processed)
    with serving(web.create_app(settings, store)) as base:
        page.set_viewport_size({"width": 1280, "height": 900})
        page.goto(f"{base}/app")
        page.wait_for_selector("body.ready")
        page.click("#process-btn")
        toast = page.wait_for_selector(".toast:has-text('Processed') .toast-text", timeout=30000)
        assert toast.text_content() == said  # as written, so a doubled space shows too
