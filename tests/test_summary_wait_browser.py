"""The workspace while a file's summary waits on a slow model, and after: how long it has taken, Stop, and the run's
note for someone who wasn't watching (the end-to-end check of the summaries' wait, in a real Chromium)."""

from __future__ import annotations

import threading
import time

from controller_inbox import local_llm, web
from controller_inbox.web_common import NOTICES
from liveserver import serving

NOTE = (
    "Stopped summarizing files: gemma-4-12b took longer than 5 minutes on Q3 pack.pdf. CloseDesk waits up to 10 "
    "minutes next time, or Settings → Local model → Wait up to … minutes can give it longer now."
)


def _finish(job) -> None:
    deadline = time.monotonic() + 10
    while job.snapshot()["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert job.snapshot()["state"] == "done"


def test_the_top_bar_says_how_long_a_summary_has_taken_and_stop_hangs_up_on_it(settings, loaded, page):
    """The bar sat on "Summarizing attachments · 1 of 2 · Q3 pack.pdf" for the whole wait, with no Stop."""
    app = web.create_app(settings, loaded)
    job = app.state.job
    now = [1000.0]
    job.clock = lambda: now[0]
    inside = threading.Event()

    def run(progress):
        with local_llm.stoppable() as stop:
            progress("summarizing", 1, 2, "Q3 pack.pdf (up to 12 minutes)")
            inside.set()
            deadline = time.monotonic() + 20
            while not stop.done and time.monotonic() < deadline:
                time.sleep(0.02)
        return {"ingested": 0, "files_summarized": 0, "model": "gemma-4-12b", "model_note": ""}

    with serving(app) as base:
        assert job.start(run)
        assert inside.wait(5)
        now[0] += 200
        page.set_viewport_size({"width": 1280, "height": 900})
        page.goto(f"{base}/app")
        page.wait_for_selector(".top-job .job")
        line = page.get_attribute(".top-job .job-text", "title")
        assert line == "Summarizing attachments · 1 of 2 · Q3 pack.pdf (up to 12 minutes) · 3 minutes so far", line
        page.click(".top-job button:has-text('Stop')")
        page.wait_for_selector(f".toast:has-text(\"{NOTICES['stopping-model']}\")")
        _finish(job)


def test_the_workspace_opened_after_a_run_says_what_stopped_it_once(settings, loaded, page):
    """A run at the start that nobody watched: the classic page and the launcher window said the summaries had
    stopped, the workspace nothing (its toast is shown only to a page that saw the run end)."""
    app = web.create_app(settings, loaded)
    job = app.state.job
    result = {"ingested": 2, "files_summarized": 1, "model": "gemma-4-12b", "read_by_bionic": 2, "model_note": NOTE}
    assert job.start(lambda progress: result, about={"kind": "startup", "auto": True})
    _finish(job)
    with serving(app) as base:
        page.set_viewport_size({"width": 1280, "height": 900})
        page.goto(f"{base}/app")
        toast = page.wait_for_selector(".toast.error:has-text('Stopped summarizing files')")
        assert toast.inner_text().startswith("Processed. Read 2 new files. Summarized 1 file. Stopped summarizing files:")
        page.reload()
        page.wait_for_selector("body.ready")
        page.wait_for_timeout(500)
        assert page.query_selector(".toast:has-text('Stopped summarizing files')") is None, "said once, not on every visit"


def test_a_run_with_nothing_wrong_isnt_said_again_when_the_workspace_opens(settings, loaded, page):
    app = web.create_app(settings, loaded)
    job = app.state.job
    assert job.start(lambda progress: {"ingested": 2, "model": "gemma-4-12b", "read_by_bionic": 2, "model_note": ""})
    _finish(job)
    with serving(app) as base:
        page.goto(f"{base}/app")
        page.wait_for_selector("body.ready")
        page.wait_for_timeout(500)
        assert page.query_selector(".toast:has-text('Processed.')") is None
