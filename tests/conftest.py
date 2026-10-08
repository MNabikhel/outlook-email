from __future__ import annotations

import io
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from openpyxl import Workbook

from controller_inbox.config import Settings
from controller_inbox.demo import make_pdf
from controller_inbox.folder_mail import ingest_folder
from controller_inbox.pipeline import ingest_demo
from controller_inbox.store import Store
from msgfactory import DOCX, PDF, XLSX, build_message, write_msg


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("CONTROLLER_INBOX_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CONTROLLER_INBOX_LLM", "false")


@pytest.fixture(autouse=True)
def _fresh_model_memory():
    """What a run learned about the loaded model must not leak between tests."""
    from controller_inbox import local_llm, semantic

    caches = (
        local_llm._status_cache,
        local_llm._reasoning_seen,
        local_llm._effort_rejected,
        local_llm._tools_rejected,
        local_llm._context_raised,
        local_llm._reader_failed,
        semantic._model_cache,
        semantic._vector_cache,
    )
    for cache in caches:
        cache.clear()
    yield
    for cache in caches:
        cache.clear()


@pytest.fixture
def as_of_now() -> datetime:
    return datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # A model server nobody listens on: a test that forgets to fake a model call fails fast, here and in CI.
    return Settings(
        data_dir=tmp_path, inbox_dir=tmp_path / "inbox", timezone="America/New_York", llm_base_url="http://127.0.0.1:9/v1", _env_file=None
    )


@pytest.fixture
def store(settings: Settings) -> Store:
    return Store(settings.db_path)


@pytest.fixture
def loaded(store: Store, settings: Settings, as_of_now: datetime) -> Store:
    ingest_demo(store, settings, now=as_of_now)
    return store


def _budget_xlsx() -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = "Budget"
    sheet.append(["Line", "Q3", "Q4", "Change"])
    sheet.append(["Ads", 1000, 1500, "=C2-B2"])
    sheet.append(["Travel", 250, 300, "=C3-B3"])
    sheet.append(["Total", "=SUM(B2:B3)", "=SUM(C2:C3)", "=D2+D3"])
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def _memo_docx() -> bytes:
    from docx import Document

    doc = Document()
    doc.add_heading("Offsite plan (draft)", 1)
    doc.add_paragraph("The offsite moves to Lisbon on 14 November. Budget is capped at $42,000.")
    doc.add_paragraph("Open item: confirm the venue deposit with Finance.")
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


@pytest.fixture
def mail(store, settings):
    """Three real .msg files: a budget from a colleague, a forwarded quote, and a bank-change scam with a file."""
    settings.trusted_domains = "taz.com"
    settings.ensure_data_dir()
    write_msg(
        settings.inbox_incoming / "budget.msg",
        "Q4 budget draft",
        "Hi, the Q4 budget draft is attached. Can you check the totals before Friday?",
        sender_name="Maya Chen",
        sender_email="maya@taz.com",
        attachments=[
            ("Q4 budget.xlsx", _budget_xlsx(), XLSX),
            ("Offsite memo.docx", _memo_docx(), DOCX),
        ],
    )
    quote = build_message(
        "Acme quote",
        "Our quote is attached.",
        sender_name="Acme Sales",
        sender_email="sales@acme.com",
        attachments=[("quote.pdf", make_pdf([["Acme quote Q-881", ["Item", "Amount"], ["Support plan", "$9,600.00"]]]), PDF)],
    )
    write_msg(
        settings.inbox_incoming / "fw quote.msg",
        "FW: Acme quote",
        "Is this in line with last year?",
        sender_name="Priya Raman",
        sender_email="priya@taz.com",
        forwarded=[("Acme quote.msg", quote)],
    )
    write_msg(
        settings.inbox_incoming / "scam.msg",
        "Updated remittance details",
        "Our bank details have changed. Please use the following account for all payments from today.",
        sender_name="Acme Billing",
        sender_email="billing@acme-payments.net",
        attachments=[("new bank letter.pdf", make_pdf([["Remit to account 5566778899 routing 021000021"]]), PDF)],
    )
    records = {email.subject: email for email in ingest_folder(store, settings)}
    return records
