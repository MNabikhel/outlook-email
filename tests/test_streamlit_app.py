"""The Streamlit test environment (streamlit/streamlit_app.py) runs every page without an error.

Streamlit isn't one of CloseDesk's dependencies, so this whole module is skipped where it isn't installed (the main
CI). Install streamlit/requirements.txt to run it."""

from __future__ import annotations

from pathlib import Path

import pytest

# Not "streamlit": the repository's streamlit/ folder imports as an empty package under that name.
AppTest = pytest.importorskip("streamlit.testing.v1").AppTest

APP = str(Path(__file__).resolve().parent.parent / "streamlit" / "streamlit_app.py")
PAGES = ["Today", "Mail", "Attachment", "Ask", "Fraud check", "Try your own file"]


@pytest.fixture
def app(monkeypatch):
    """The app, with any network call failing the test: the cloud app never calls a model."""
    import httpx

    def refuse(self, request, *args, **kwargs):
        raise AssertionError(f"The test environment called {request.url}")

    monkeypatch.setattr(httpx.Client, "send", refuse)
    return AppTest.from_file(APP, default_timeout=120).run()


def _clean(at: AppTest) -> None:
    assert not at.exception, [item.value for item in at.exception]
    assert "Test environment" in at.info[0].value


def _go(at: AppTest, page: str) -> AppTest:
    at.sidebar.radio[0].set_value(page).run()
    _clean(at)
    return at


def test_every_page_loads(app):
    _clean(app)
    assert "need you" in " ".join(item.value for item in app.markdown)
    for page in PAGES:
        _go(app, page)


def test_an_email_and_its_pdf_page_view(app):
    box = app.session_state["sample_box"]
    email = next(e for e in box.store.list_emails(limit=-1) if any(a.filename == "INV-10482.pdf" for a in e.attachments))
    app.session_state["mail_pick"] = email.id
    at = _go(app, "Mail")
    assert any("INV-10482" in item.value for item in at.subheader)
    at.session_state["file"] = ("sample", email.id, 1)
    at = _go(at, "Attachment")
    assert at.tabs and len(at.get("image")) == 1, "the page is drawn with its boxes"
    assert any("What was read where" in item.value for item in at.markdown)


def test_a_locked_email_never_shows_its_files(app, monkeypatch):
    from controller_inbox import fraud

    # The sample's suspected-fraud email has no files: hold another one with a PDF as suspected fraud too.
    box = app.session_state["sample_box"]
    locked = next(e for e in box.store.list_emails(limit=-1) if any(a.filename == "PO-77821.pdf" for a in e.attachments))
    real = fraud.attachments_locked
    monkeypatch.setattr(fraud, "attachments_locked", lambda email: email.id == locked.id or real(email))
    app.session_state["file"] = ("sample", locked.id, 1)
    at = _go(app, "Attachment")
    assert at.error and "possible payment fraud" in at.error[0].value
    assert not at.tabs


def test_ask_answers_by_looking_up(app):
    at = _go(app, "Ask")
    at.text_input(key="ask_box").set_value("Which invoices are due this week?")
    at.button[-1].click().run()  # the form's Ask button
    _clean(at)
    text = " ".join(item.value for item in at.markdown)
    assert "Sources" in text and "INV-10482" in text


def test_an_uploaded_pdf_is_read_and_shown_as_an_email(app):
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from pdffactory import Text, build_pdf

    pdf = build_pdf([[Text(72, 740, "INVOICE INV-7731", size=16), Text(72, 700, "Total due $1,250.00", size=11)]])
    at = _go(app, "Try your own file")
    assert "leave your computer" in at.warning[0].value
    box = at.session_state["upload_box"]
    (box.settings.inbox_incoming / "INV-7731.pdf").write_bytes(pdf)
    from controller_inbox.folder_mail import ingest_folder

    records = ingest_folder(box.store, box.settings)
    at.session_state["upload_pick"] = records[0].id
    at.run()
    _clean(at)
    assert any("INV-7731" in item.value for item in at.subheader)
    at.session_state["file"] = ("upload", records[0].id, 1)
    _go(at, "Attachment")
