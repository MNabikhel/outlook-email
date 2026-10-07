"""Regressions for the ingest and document-reading fixes (one or more tests per bug)."""

from __future__ import annotations

import io
import os
import time
import zipfile
from datetime import date, datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import pytest

from controller_inbox import documents, folder_mail
from controller_inbox.config import Settings
from controller_inbox.extract import extract_fields, extract_text_from_bytes, parse_due_date, redact_financial_secrets
from controller_inbox.folder_mail import collect_messages, ingest_folder, reread_attachments
from controller_inbox.graph import GraphMailbox
from controller_inbox.models import RawAttachment, RawMessage
from controller_inbox.pipeline import ingest_demo, ingest_mailbox, process_message, rescore_stored
from controller_inbox.store import Store

AS_OF = date(2026, 10, 6)
NOW = datetime(2026, 10, 6, 14, 0, tzinfo=timezone.utc)


def _eml(settings: Settings, name: str, body: str = "See attached.", *, message_id: str = "<m1@x>", attach=(), date_header="Mon, 05 Oct 2026 10:00:00 -0400") -> Path:
    msg = EmailMessage()
    msg["From"] = "Vendor AP <ap@vendor.com>"
    msg["To"] = "me@co.com"
    msg["Subject"] = name
    msg["Message-ID"] = message_id
    msg["Date"] = date_header
    msg.set_content(body)
    for filename, data in attach:
        msg.add_attachment(data, maintype="text", subtype="plain", filename=filename)
    path = settings.inbox_incoming / f"{name}.eml"
    path.write_bytes(bytes(msg))
    _age(path)
    return path


def _age(path: Path) -> None:
    """Files written just now look like they are still being copied; date them a minute back."""
    old = time.time() - 60
    os.utime(path, (old, old))


def _raw(body: str, *, received: datetime, attachments=None, preview: str = "") -> RawMessage:
    return RawMessage(
        id="raw-1",
        subject="Docs",
        sender_name="A Vendor",
        sender_email="a@vendor.com",
        received_at=received,
        body_text=body,
        body_preview=preview,
        has_attachments=bool(attachments),
        source="folder",
        attachments=attachments or [],
    )


# 1. Account masking needs digits ---------------------------------------------------------------


def test_masking_leaves_ordinary_words_alone_and_masks_real_numbers():
    text = "Review the account statement, ask the account manager. Accountability. account number 123456789012"
    masked = redact_financial_secrets(text)
    assert "account statement" in masked and "account manager" in masked and "Accountability" in masked
    assert "123456789012" not in masked and "account ****9012" in masked
    assert "acct no. 55512345" not in redact_financial_secrets("acct no. 55512345")


def test_account_last4_comes_only_from_digits():
    words = extract_fields("Your account balance is overdue", as_of=AS_OF)
    assert words.account_last4 == [] and not words.mentions_routing_or_account
    real = extract_fields("Account: AB12345678", as_of=AS_OF)
    assert real.account_last4 == ["5678"] and real.mentions_routing_or_account


# 2. Whole-dollar amounts ------------------------------------------------------------------------


def test_whole_dollar_amounts_are_read():
    fields = extract_fields("Please wire $48,000 today. Fee USD 500. Total: 5,000", as_of=AS_OF)
    assert {48000.0, 500.0, 5000.0} <= set(fields.amounts)
    assert extract_fields("Pay $1,2345 now", as_of=AS_OF).amounts == []
    assert extract_fields("total 3 items", as_of=AS_OF).amounts == []


# 3. Repeated attachment names -------------------------------------------------------------------


def test_two_attachments_with_one_name_both_ingest_with_their_own_text(settings: Settings, store: Store):
    settings.ensure_data_dir()
    _eml(settings, "Two invoices", attach=[("invoice.csv", b"Item,Amount\nFirst,100\n"), ("invoice.csv", b"Item,Amount\nSecond,200\n")])
    report: dict = {}
    [record] = ingest_folder(store, settings, report=report)
    assert report["failed"] == []
    by_name = {att.filename: att for att in record.attachments}
    assert sorted(by_name) == ["invoice (2).csv", "invoice.csv"]
    assert by_name["invoice.csv"].id == f"{record.id}:invoice.csv"  # an unrepeated name keeps its old id
    folder = settings.inbox_extracted / record.id
    assert b"First" in (folder / "invoice.csv").read_bytes() and b"Second" in (folder / "invoice (2).csv").read_bytes()

    store.set_state(folder_mail.READER_KEY, "old")
    reread_attachments(store, settings)
    texts = {att.filename: att.extracted_text for att in store.get_email(record.id).attachments}
    assert "First" in texts["invoice.csv"] and "Second" not in texts["invoice.csv"]
    assert "Second" in texts["invoice (2).csv"]


def test_repeated_names_are_numbered_the_same_way_every_time(settings: Settings):
    settings.ensure_data_dir()
    _eml(settings, "Repeat", attach=[("a.txt", b"1"), ("A.TXT", b"2"), ("a.txt", b"3")])
    first = [att.filename for att in collect_messages(settings)[0][0].attachments]
    second = [att.filename for att in collect_messages(settings)[0][0].attachments]
    assert first == second == ["a.txt", "A (2).TXT", "a (3).txt"]


def test_zip_entries_with_one_name_get_their_own_ids(store: Store, settings: Settings):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("jan/report.txt", "January report")
        archive.writestr("feb/report.txt", "February report")
    zipped = RawAttachment(id="pack.zip", filename="pack.zip", content_type="application/zip", size_bytes=1, content=buffer.getvalue())
    record = process_message(_raw("files", received=NOW, attachments=[zipped]), store, settings, now=NOW)
    ids = [att.id for att in record.attachments]
    assert len(ids) == 2 and len(set(ids)) == 2


# 4. Preview is masked ---------------------------------------------------------------------------


def test_preview_does_not_show_account_numbers(store: Store, settings: Settings):
    raw = _raw("Please update our account number 123456789012 routing 021000021", received=NOW)
    record = process_message(raw, store, settings, now=NOW)
    stored = store.get_email(record.id)
    assert "123456789012" not in stored.body_preview and "021000021" not in stored.body_preview
    assert "123456789012" not in " ".join(a.title + a.detail for a in stored.actions)
    assert stored.extracted.mentions_routing_or_account


# 5. Folder imports don't move the Graph cursor -------------------------------------------------


def test_folder_import_and_sample_leave_the_graph_cursor_alone(settings: Settings, store: Store):
    settings.ensure_data_dir()
    ingest_demo(store, settings, now=NOW)
    assert store.get_state("last_sync_at") is None
    _eml(settings, "Cursor")
    assert ingest_folder(store, settings, now=NOW)
    assert store.get_state("last_sync_at") is None
    assert store.get_state("last_folder_ingest")


# 6. Graph item attachments and a bad message ----------------------------------------------------


class _FakeClient:
    def __init__(self, mime: bytes = b""):
        self.mime = mime
        self.params: list[dict | None] = []

    def _user_root(self) -> str:
        return "/me"

    def get_json(self, path, params=None):
        self.params.append(params)
        if path.endswith("/attachments"):
            return {"value": [{"@odata.type": "#microsoft.graph.itemAttachment", "id": "ITEM1", "name": "Old thread"}]}
        return {"value": []}

    def get_bytes(self, path):
        assert path.endswith("/attachments/ITEM1/$value")
        return self.mime


def test_graph_item_attachment_is_read_as_mime():
    inner = EmailMessage()
    inner["From"] = "Supplier <s@supplier.com>"
    inner["Subject"] = "Old thread"
    inner.set_content("Our bank details changed.")
    inner.add_attachment(b"Invoice text", maintype="text", subtype="plain", filename="inv.txt")
    found = GraphMailbox(_FakeClient(bytes(inner))).get_attachments("MSG1")
    assert [att.filename for att in found] == ["Old thread.txt", "Old thread › inv.txt"]
    assert b"Our bank details changed." in found[0].content
    assert len({att.id for att in found}) == 2


class _Mailbox:
    def __init__(self, raws):
        self.raws = raws

    def list_messages(self, received_after=None):
        return iter(self.raws)

    def get_attachments(self, message_id):
        if message_id == "bad":
            raise ValueError("Graph sent something odd")
        return []

    def apply_categories(self, message_id, categories, flag):
        return "skipped"


def test_one_bad_graph_message_does_not_stop_the_sync(store: Store, settings: Settings):
    good = _raw("hello", received=NOW)
    good.id, good.source = "good", "graph"
    bad = _raw("broken", received=NOW, attachments=[])
    bad.id, bad.source, bad.has_attachments = "bad", "graph", True
    report: dict = {}
    records = ingest_mailbox(_Mailbox([bad, good]), store, settings, now=NOW, report=report)
    assert [r.id for r in records] == ["good"]
    assert report["failed"][0]["id"] == "bad"
    assert store.get_state("last_sync_at") == NOW.isoformat()


# 7. Relative dates read against the day the mail was sent --------------------------------------


def test_relative_dates_anchor_to_the_received_date(store: Store, settings: Settings):
    received = datetime(2026, 9, 28, 14, 0, tzinfo=timezone.utc)
    record = process_message(_raw("Please send the W-9 by tomorrow.", received=received), store, settings, now=NOW)
    assert record.extracted.due_dates == ["2026-09-29"]
    task = next(a for a in record.actions if "W-9" in a.title)
    assert task.due_date == "2026-09-29"

    class Check:
        level = "none"

    again = rescore_stored(store, settings, record, Check(), now=NOW)
    assert again.extracted.due_dates == ["2026-09-29"]
    assert next(a for a in again.actions if "W-9" in a.title).due_date == "2026-09-29"


# 8. A message named like the drop folder --------------------------------------------------------


def test_a_message_named_incoming_does_not_take_the_loose_files(settings: Settings):
    settings.ensure_data_dir()
    (settings.inbox_incoming / "incoming.eml").write_bytes(b"From: a@b.com\r\nSubject: x\r\n\r\nhi\r\n")
    (settings.inbox_incoming / "unrelated.pdf").write_bytes(b"%PDF-1.4")
    batches = dict((p.name, [s.name for s in sidecars]) for p, sidecars in folder_mail.collect_batches(settings))
    assert batches["incoming.eml"] == [] and "unrelated.pdf" in batches


def test_a_folder_named_after_the_message_still_holds_its_files(settings: Settings):
    settings.ensure_data_dir()
    (settings.inbox_incoming / "invoice.eml").write_bytes(b"From: a@b.com\r\nSubject: x\r\n\r\nhi\r\n")
    (settings.inbox_attachments / "invoice").mkdir()
    (settings.inbox_attachments / "invoice" / "scan.pdf").write_bytes(b"%PDF-1.4")
    batches = dict((p.name, [s.name for s in sidecars]) for p, sidecars in folder_mail.collect_batches(settings))
    assert batches["invoice.eml"] == ["scan.pdf"]


# 9. A forwarded email inside an .eml ------------------------------------------------------------


def test_forwarded_eml_is_an_attachment_not_part_of_the_body(tmp_path: Path):
    inner = EmailMessage()
    inner["From"] = "fraud@evil.com"
    inner["Subject"] = "old thread"
    inner.set_content("INNER BODY: new bank details")
    inner.add_attachment(b"letter", maintype="text", subtype="plain", filename="letter.txt")
    outer = EmailMessage()
    outer["From"] = "boss@co.com"
    outer["Subject"] = "fwd"
    outer.set_content("OUTER BODY: see forwarded")
    outer.add_attachment(inner)
    path = tmp_path / "o.eml"
    path.write_bytes(bytes(outer))
    raw = folder_mail._parse_eml(path)
    assert "INNER BODY" not in raw.body_text and "OUTER BODY" in raw.body_text
    assert [att.filename for att in raw.attachments] == ["old thread.txt", "old thread › letter.txt"]
    assert b"INNER BODY" in raw.attachments[0].content and b"fraud@evil.com" in raw.attachments[0].content


# 10. Graph filter order -------------------------------------------------------------------------


@pytest.mark.parametrize("after", [None, datetime(2026, 10, 1, tzinfo=timezone.utc)])
def test_graph_filter_names_the_sort_property_first(after):
    client = _FakeClient()
    list(GraphMailbox(client).list_messages(received_after=after))
    params = client.params[0]
    assert params["$orderby"].startswith("receivedDateTime")
    assert params["$filter"].startswith("receivedDateTime ge ")


# 11. compare_columns and three-letter headers ---------------------------------------------------


def test_compare_columns_prefers_a_header_over_column_letters():
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "P"
    for row in (["Account", "Jan", "Feb"], ["Rent", 100, 120], ["Fees", 50, 40]):
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    out = documents.compare_columns(buffer.getvalue(), "P", "Jan", "Feb")
    assert "Rent (row 2): 100 → 120" in out
    assert "Rent (row 2): 100 → 120" in documents.compare_columns(buffer.getvalue(), "P", "B", "C")


# 12. Content the readers used to drop -----------------------------------------------------------


def test_docx_reads_text_inside_content_controls():
    from docx import Document
    from docx.oxml import parse_xml

    doc = Document()
    doc.add_paragraph("Visible")
    doc.element.body.insert(0, parse_xml(
        '<w:sdt xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:sdtContent>'
        "<w:p><w:r><w:t>Amount due $9,999.00</w:t></w:r></w:p></w:sdtContent></w:sdt>"
    ))
    buffer = io.BytesIO()
    doc.save(buffer)
    assert documents.docx_text(buffer.getvalue()) == "Amount due $9,999.00\nVisible"


def test_pptx_reads_grouped_shapes():
    from pptx import Presentation
    from pptx.util import Inches

    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    group = slide.shapes.add_group_shape()
    group.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1)).text_frame.text = "Budget 1.2M in a group"
    buffer = io.BytesIO()
    deck.save(buffer)
    assert "Budget 1.2M in a group" in documents.pptx_text(buffer.getvalue())


def test_utf16_csv_from_excel_is_read():
    data = "Name\tAmount\r\nAcme\t100\r\n".encode("utf-16")
    text = documents.csv_text(data, "export.csv")
    assert "Acme" in text and "100" in text


def test_last_resort_sample_does_not_split_a_utf8_character():
    data = ("a" * 1999 + "é" + " rest of the log").encode("utf-8")
    assert extract_text_from_bytes("notes.log", "application/octet-stream", data).startswith("aaaa")


# 13. Invoice numbers and year-less due dates ----------------------------------------------------


def test_invoice_numbers_keep_every_segment():
    fields = extract_fields("Invoice No: INV-2024-0457. Again INV-2024-0457. Invoice 12345-due", as_of=AS_OF)
    assert fields.invoice_numbers == ["INV-2024-0457", "12345"]


def test_due_dates_without_a_year():
    assert extract_fields("Payment due October 15.", as_of=AS_OF).due_dates == ["2026-10-15"]
    assert parse_due_date("October 15th", as_of=AS_OF) == "2026-10-15"
    assert parse_due_date("January 5", as_of=date(2026, 12, 20)) == "2027-01-05"
    assert parse_due_date("Oct 15, 24", as_of=AS_OF) == "2024-10-15"


# 14. Drop folder: files still copying, locked files, the sample ---------------------------------


def test_a_file_still_being_copied_waits_for_the_next_run(settings: Settings, store: Store, monkeypatch):
    settings.ensure_data_dir()
    path = settings.inbox_incoming / "growing.txt"
    path.write_bytes(b"first half")

    def still_writing(_seconds):
        with path.open("ab") as handle:
            handle.write(b" second half")

    monkeypatch.setattr(folder_mail.time, "sleep", still_writing)
    report: dict = {}
    assert ingest_folder(store, settings, report=report) == []
    assert report["waiting"] == ["growing.txt"] and path.exists()

    _age(path)
    assert len(ingest_folder(store, settings)) == 1 and not path.exists()


def test_a_freshly_written_finished_file_is_read_now(settings: Settings, store: Store):
    settings.ensure_data_dir()
    (settings.inbox_incoming / "done.txt").write_bytes(b"complete")
    assert len(ingest_folder(store, settings)) == 1


def test_a_locked_file_is_left_and_the_batch_goes_on(settings: Settings, store: Store, monkeypatch):
    settings.ensure_data_dir()
    _eml(settings, "A locked", message_id="<a@x>")
    _eml(settings, "B fine", message_id="<b@x>")
    real = folder_mail._parse_message

    def parse(path):
        if path.name.startswith("A"):
            raise PermissionError("in use by another process")
        return real(path)

    monkeypatch.setattr(folder_mail, "_parse_message", parse)
    report: dict = {}
    records = ingest_folder(store, settings, report=report)
    assert [r.subject for r in records] == ["B fine"]
    assert report["waiting"] == ["A locked.eml"] and report["failed"] == []
    assert (settings.inbox_incoming / "A locked.eml").exists()


def test_a_failed_move_to_quarantine_does_not_stop_the_batch(settings: Settings, store: Store, monkeypatch):
    settings.ensure_data_dir()
    (settings.inbox_incoming / "a-broken.msg").write_bytes(b"not an outlook file")
    _age(settings.inbox_incoming / "a-broken.msg")
    _eml(settings, "b-fine", message_id="<b@x>")
    real_move = folder_mail.shutil.move

    def move(src, dst):
        if "broken" in src:
            raise PermissionError("locked")
        return real_move(src, dst)

    monkeypatch.setattr(folder_mail.shutil, "move", move)
    report: dict = {}
    records = ingest_folder(store, settings, report=report)
    assert [r.subject for r in records] == ["b-fine"]
    assert len(report["failed"]) == 1


def test_a_message_that_fails_after_parsing_keeps_the_sample(settings: Settings, loaded: Store, monkeypatch):
    settings.ensure_data_dir()
    before = loaded.counts()["emails"]
    _eml(settings, "Breaks later")

    def boom(*_args, **_kwargs):
        raise RuntimeError("classifier fell over")

    monkeypatch.setattr(folder_mail, "process_message", boom)
    report: dict = {}
    ingest_folder(loaded, settings, report=report)
    assert len(report["failed"]) == 1 and "sample_cleared" not in report
    assert loaded.counts()["emails"] == before and not loaded.real_mail_count()


def test_an_address_in_the_display_name_is_not_the_sender(store: Store, settings: Settings):
    # "Acme Billing <billing@acme.com>" is only the name: the mail comes from acme-payments.net.
    settings.ensure_data_dir()
    settings.trusted_domains = "acme.com"
    path = settings.inbox_incoming / "spoof.eml"
    path.write_bytes(
        b'From: "Acme Billing <billing@acme.com>" <billing@acme-payments.net>\r\nTo: ap@co.com\r\n'
        b"Subject: Updated remittance details\r\nMessage-ID: <sp1@x>\r\nDate: Mon, 05 Oct 2026 10:00:00 -0400\r\n\r\n"
        b"Our bank details have changed. Please use the new account for all payments from today.\r\n"
    )
    _age(path)
    record = ingest_folder(store, settings)[0]
    assert record.sender_email == "billing@acme-payments.net"
    assert store.fraud_check(record.id)["level"] == "high" and "do_not_process" in record.flags
    # Headers the strict parser gives up on still give the address in the brackets.
    assert folder_mail._split_address("Chen, Maya <maya@x.com>") == ("Chen, Maya", "maya@x.com")
    assert folder_mail._split_address("Maya Chen") == ("Maya Chen", "")
