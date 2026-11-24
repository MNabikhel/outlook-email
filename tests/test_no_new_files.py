"""A run that found nothing new said "Read 0 new files." in the workspace's toast, on the classic page and in the
launcher window. It now says "No new files."; one or more is still "Read 1 new file." or "Read 3 new files.", and the
rest of each message is as it was. The toast is checked in a real Chromium (skipped without it)."""

from __future__ import annotations

import re
import time

import pytest

from controller_inbox import cli, overnight, web
from liveserver import serving

COUNTS = [
    (0, 0, "No new files."),
    (0, 3, "No new files, 3 already filed."),
    (1, 0, "Read 1 new file."),
    (4, 1, "Read 4 new files, 1 already filed."),
]


def _printed(capsys, **result) -> str:
    """The line of the launcher window (and of the overnight command) that says what the run read."""
    run = {
        "headline": "Tuesday's digest", "ingested": 0, "read_by_bionic": 0, "waiting_on_bionic": 0,
        "folders": {"important": 0, "informational": 0, "reference": 0}, "digest_path": "digest.md", "log_path": "run.log",
    }
    cli._print_overnight({**run, **result})
    return capsys.readouterr().out.splitlines()[1]


def test_the_launcher_window_says_no_new_files(capsys):
    assert _printed(capsys, ingested=0) == "No new files. The local model read 0."
    for ingested, already, said in COUNTS:
        assert _printed(capsys, ingested=ingested, already_read=already) == f"{said} The local model read 0."
    assert _printed(capsys, ingested=2, model="gemma-4-12b", read_by_bionic=2, avg_seconds=40) == (
        "Read 2 new files. The local model (gemma-4-12b) read 2 (about 40s each)."
    )


def _finish(job) -> None:
    deadline = time.monotonic() + 5
    while job.snapshot()["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.02)


@pytest.mark.parametrize(("ingested", "already", "said"), COUNTS)
def test_the_classic_page_says_no_new_files(client, ingested, already, said):
    job = client.app.state.job
    assert job.start(lambda progress: {"ingested": ingested, "already_read": already, "model": "", "read_by_bionic": 0})
    _finish(job)
    section = re.search(r'<section class="job[^"]*">(.*?)</section>', client.get("/").text, re.S)[1]
    words = " ".join(re.sub(r"<[^>]+>", " ", section).split())
    assert words.startswith(f"Processed. {said} No local model was running"), words


@pytest.mark.parametrize(("ingested", "said"), [(0, "No new files."), (1, "Read 1 new file."), (3, "Read 3 new files.")])
def test_process_new_mail_says_no_new_files(settings, store, page, monkeypatch, ingested, said):
    def processed(*_args, **_kw):
        time.sleep(1)  # the page sees the run going, then finish
        return {"ingested": ingested, "model": "", "read_by_bionic": 0}

    monkeypatch.setattr(overnight, "run_overnight", processed)
    with serving(web.create_app(settings, store)) as base:
        page.set_viewport_size({"width": 1280, "height": 900})
        page.goto(f"{base}/app")
        page.wait_for_selector("body.ready")
        page.click("#process-btn")
        toast = page.wait_for_selector(".toast:has-text('Processed')", timeout=30000)
        assert toast.inner_text() == f"Processed. {said}"
