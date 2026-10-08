"""Fixes to reading mail from the drop folder and from Microsoft Graph."""

import base64
import io
import os
import time
import zipfile
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import pytest

from controller_inbox.folder_mail import collect_messages, forwarded_attachments, ingest_folder
from controller_inbox.graph import GraphMailbox
from msgfactory import PDF, build_message, write_msg


def _drop(settings, name: str, data: bytes) -> None:
    settings.inbox_incoming.mkdir(parents=True, exist_ok=True)
    (settings.inbox_incoming / name).write_bytes(data)


# Forwarded-mail names: Path.stem cut subjects with "/" or "." in them ("Statement 09/30" became "30.txt").

FORWARDED = (
    b"From: Bank <stmt@bank.com>\r\nSubject: Statement 09/30\r\nDate: Tue, 30 Sep 2026 10:00:00 +0000\r\n"
    b"Message-ID: <s@bank.com>\r\nContent-Type: text/plain\r\n\r\nYour statement.\r\n"
)


class _ItemClient:
    def _user_root(self):
        return "/me"

    def get_bytes(self, path):
        return FORWARDED


def test_graph_item_attachment_named_with_a_slash_keeps_its_name():
    # Graph names an item attachment after the forwarded email's subject, as Outlook shows it.
    found = GraphMailbox(_ItemClient())._item_attachment("m1", {"id": "a1", "name": "Statement 09/30"})
    assert found[0].filename == "Statement 09_30.txt", [att.filename for att in found]


def test_eml_forwarded_file_named_with_a_dot_keeps_its_name():
    parsed = BytesParser(policy=policy.default).parsebytes(FORWARDED)
    assert forwarded_attachments(parsed, "Q3 accruals v2.1 final")[0].filename == "Q3 accruals v2.1 final.txt"
    assert forwarded_attachments(parsed, "Q3 accruals v2.1 final.eml")[0].filename == "Q3 accruals v2.1 final.txt"


def test_msg_embedded_item_named_with_a_dot_keeps_its_name(settings):
    inner = build_message("Q3 accruals v2.1 final", "See workbook.", attachments=[("accruals.pdf", b"%PDF-1.4 x", PDF)])
    write_msg(settings.inbox_incoming / "fw.msg", "FW: accruals", "fyi", forwarded=[("Q3 accruals v2.1 final", inner)])
    (raw, _owned), = collect_messages(settings)
    assert "Q3 accruals v2.1 final.txt" in [att.filename for att in raw.attachments]


# An image with a Content-ID was dropped as a signature logo even when no HTML body shows it (a phone's photo).

def test_unreferenced_inline_image_in_eml_is_kept(settings):
    data = base64.encodebytes(b"\xff\xd8\xff\xe0" + b"\x00" * 50_000)
    _drop(settings, "receipt.eml", (
        b"From: Jordan <jordan@acme.com>\r\nSubject: Taxi receipt\r\nDate: Tue, 06 Oct 2026 10:00:00 +0000\r\n"
        b"Message-ID: <r1@acme.com>\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=B\r\n\r\n"
        b"--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nReceipt attached.\r\n"
        b"--B\r\nContent-Type: image/jpeg; name=receipt.jpg\r\nContent-Disposition: inline; filename=receipt.jpg\r\n"
        b"Content-ID: <IMG_0412@acme>\r\nContent-Transfer-Encoding: base64\r\n\r\n" + data + b"--B--\r\n"
    ))
    (raw, _owned), = collect_messages(settings)
    assert [a.filename for a in raw.attachments] == ["receipt.jpg"]


def test_logo_shown_in_the_html_body_is_still_dropped(settings):
    logo = base64.encodebytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 3000)
    _drop(settings, "sig.eml", (
        b"From: a@b.com\r\nSubject: Hello\r\nMessage-ID: <l1@b.com>\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: multipart/related; boundary=R\r\n\r\n"
        b'--R\r\nContent-Type: text/html\r\n\r\n<p>Hi</p><img src="cid:image001.png@01DB">\r\n'
        b"--R\r\nContent-Type: image/png; name=image001.png\r\nContent-Disposition: inline; filename=image001.png\r\n"
        b"Content-ID: <image001.png@01DB>\r\nContent-Transfer-Encoding: base64\r\n\r\n" + logo + b"--R--\r\n"
    ))
    (raw, _owned), = collect_messages(settings)
    assert raw.attachments == []


# Outlook's names for some character sets are unknown to Python, so the text came out garbled.

def _charset_eml(charset: str, text: str, codec: str) -> bytes:
    return (
        b"From: a@b.com\r\nSubject: hi\r\nDate: Tue, 06 Oct 2026 10:00:00 +0000\r\nMessage-ID: <c1@b.com>\r\n"
        b"MIME-Version: 1.0\r\nContent-Type: text/plain; charset=" + charset.encode() + b"\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n" + text.encode(codec) + b"\r\n"
    )


def test_thai_windows_874_body(settings):
    _drop(settings, "th.eml", _charset_eml("windows-874", "ใบแจ้งหนี้ 5,000 บาท", "cp874"))
    (raw, _), = collect_messages(settings)
    assert "ใบแจ้งหนี้" in raw.body_text, raw.body_text


def test_hebrew_iso_8859_8_i_body(settings):
    _drop(settings, "he.eml", _charset_eml("iso-8859-8-i", "חשבונית לתשלום", "iso-8859-8"))
    (raw, _), = collect_messages(settings)
    assert "חשבונית" in raw.body_text, raw.body_text


# Files put in inbox/attachments/<name>/ after their message was read were never read, moved or reported.

def test_attachment_folder_without_its_message_is_read_as_loose_files(store, settings, monkeypatch):
    settings.ensure_data_dir()
    (settings.inbox_incoming / "invoice.eml").write_bytes(
        b"From: a@b.com\r\nSubject: Invoice 77\r\nMessage-ID: <i77@b.com>\r\n\r\nInvoice attached.\r\n"
    )
    ingest_folder(store, settings)
    folder = settings.inbox_attachments / "invoice"
    folder.mkdir()
    (folder / "scan.pdf").write_bytes(b"%PDF-1.4 invoice 77")
    # Fresh, it waits for a message that may still be on its way...
    report: dict = {}
    assert ingest_folder(store, settings, report=report) == []
    assert report["waiting"] == ["scan.pdf"]
    # ...and is read on its own once it has sat there for over a day.
    later = time.time() + 25 * 60 * 60
    monkeypatch.setattr("controller_inbox.folder_mail.time.time", lambda: later)
    records = ingest_folder(store, settings)
    assert [r.subject for r in records] == ["scan"]
    assert not (folder / "scan.pdf").exists()


# The CLI says to put a message's files in inbox/attachments/<name>/; read at once, a folder copied in just
# before its message was split from it.

def test_attachments_folder_dropped_before_its_message_waits_for_it(store, settings):
    settings.ensure_data_dir()
    folder = settings.inbox_attachments / "Invoice 77"
    folder.mkdir()
    (folder / "backup.txt").write_text("Support for invoice INV-77: freight $1,300.00", encoding="utf-8")
    first = ingest_folder(store, settings)
    (settings.inbox_incoming / "Invoice 77.eml").write_bytes(
        b"From: ap@vendor.example\r\nSubject: Invoice 77\r\nMessage-ID: <i77@vendor.example>\r\n\r\nInvoice attached.\r\n"
    )
    second = ingest_folder(store, settings)
    assert first == []
    assert [att.filename for att in second[0].attachments] == ["backup.txt"]


# Explorer and robocopy keep a copied file's modified time: a folder copied in just now with week-old files
# still waits for its message.

def test_a_folder_copied_in_with_old_file_dates_still_waits(store, settings):
    settings.ensure_data_dir()
    folder = settings.inbox_attachments / "Invoice 4471"
    folder.mkdir()
    (folder / "scan.pdf").write_bytes(b"%PDF-1.4 invoice 4471")
    week_ago = time.time() - 7 * 24 * 60 * 60
    os.utime(folder / "scan.pdf", (week_ago, week_ago))
    report: dict = {}
    assert ingest_folder(store, settings, report=report) == []
    assert report["waiting"] == ["scan.pdf"]


# Windows and Office litter folders with desktop.ini, Thumbs.db and ~$ lock files; they are not mail.

def test_windows_system_files_are_not_read_as_mail(store, settings):
    _drop(settings, "desktop.ini", b"[.ShellClassInfo]\r\nIconResource=C:\\x.dll,0\r\n")
    _drop(settings, "Thumbs.db", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 500)
    _drop(settings, "~$budget.xlsx", b"\x05Maya Chen" + b"\x00" * 150)
    assert ingest_folder(store, settings) == []


# A signed .eml kept its smime.p7s signature as an attachment.

def test_signed_eml_has_no_signature_attachment(settings):
    _drop(settings, "signed.eml", (
        b"From: Bank <ops@bank.com>\r\nSubject: Wire confirmation\r\nDate: Tue, 06 Oct 2026 10:00:00 +0000\r\n"
        b"Message-ID: <sig1@bank.com>\r\nMIME-Version: 1.0\r\n"
        b'Content-Type: multipart/signed; protocol="application/pkcs7-signature"; micalg=sha-256; boundary=S\r\n\r\n'
        b"--S\r\nContent-Type: multipart/mixed; boundary=M\r\n\r\n"
        b"--M\r\nContent-Type: text/plain\r\n\r\nConfirmation attached.\r\n"
        b"--M\r\nContent-Type: application/pdf; name=conf.pdf\r\nContent-Disposition: attachment; filename=conf.pdf\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\nJVBERi0xLjQK\r\n--M--\r\n"
        b"--S\r\nContent-Type: application/pkcs7-signature; name=smime.p7s\r\n"
        b"Content-Disposition: attachment; filename=smime.p7s\r\nContent-Transfer-Encoding: base64\r\n\r\n"
        b"MIAGCSqGSIb3DQEHAqCAMIACAQEx\r\n--S--\r\n"
    ))
    (raw, _), = collect_messages(settings)
    assert [a.filename for a in raw.attachments] == ["conf.pdf"]


# Graph lists a signature logo as an inline fileAttachment; the Graph reader kept it.

class _AttachmentClient:
    def _user_root(self):
        return "/me"

    def get_json(self, path, params=None):
        logo = b"\x89PNG\r\n\x1a\n" + b"\x00" * 3000
        return {"value": [
            {
                "@odata.type": "#microsoft.graph.fileAttachment", "id": "A1", "name": "invoice.pdf",
                "contentType": "application/pdf", "size": 10, "isInline": False,
                "contentBytes": base64.b64encode(b"%PDF-1.4 x").decode(),
            },
            {
                "@odata.type": "#microsoft.graph.fileAttachment", "id": "A2", "name": "image001.png",
                "contentType": "image/png", "size": len(logo), "isInline": True, "contentId": "image001.png@01DB",
                "contentBytes": base64.b64encode(logo).decode(),
            },
        ]}


def test_graph_drops_inline_signature_logo():
    found = GraphMailbox(_AttachmentClient()).get_attachments("m1")
    assert [a.filename for a in found] == ["invoice.pdf"]


# Writeback PATCHed `categories`, which replaces the whole list: the user's own ("Paid") were erased.

class _Outlook:
    """A fake Graph holding one message's categories, with Graph's PATCH semantics (the list is replaced)."""

    def __init__(self, categories):
        self.categories = list(categories)

    def _user_root(self):
        return "/me"

    def get_json(self, path, params=None):
        return {"id": "m1", "categories": list(self.categories)}

    def request(self, method, path, **kwargs):
        if method == "PATCH" and "categories" in (kwargs.get("json") or {}):
            self.categories = list(kwargs["json"]["categories"])


def test_writeback_keeps_the_users_own_categories():
    outlook = _Outlook(["Paid", "Follow up"])
    GraphMailbox(outlook).apply_categories("m1", ["CloseDesk", "AP-Invoice"], False)
    assert sorted(outlook.categories) == ["AP-Invoice", "CloseDesk", "Follow up", "Paid"]


def test_writeback_replaces_closedesks_own_old_categories():
    # Filed again as a bank statement: the old AP-Invoice and CloseDesk-Important labels go.
    outlook = _Outlook(["Paid", "CloseDesk", "AP-Invoice", "CloseDesk-Important"])
    GraphMailbox(outlook).apply_categories("m1", ["CloseDesk", "Bank-Statement"], False)
    assert sorted(outlook.categories) == ["Bank-Statement", "CloseDesk", "Paid"]


# A second unreadable file with the same name landed as "<stem>-<hash>.msg" but its note replaced the first one's.

def test_each_failed_file_keeps_its_own_note(store, settings):
    settings.ensure_data_dir()
    (settings.inbox_incoming / "statement.msg").write_bytes(b"not an outlook file, first")
    ingest_folder(store, settings)
    (settings.inbox_incoming / "statement.msg").write_bytes(b"not an outlook file, second")
    ingest_folder(store, settings)
    failed = sorted(p.name for p in settings.inbox_failed.iterdir())
    files = [n for n in failed if not n.endswith(".why.txt")]
    notes = [n for n in failed if n.endswith(".why.txt")]
    assert len(files) == 2
    assert sorted(f"{n}.why.txt" for n in files) == notes, failed


# Found by fuzzing.

def _zip_with_name(name: str) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(name, b"%PDF-1.4 not really")
    return out.getvalue()


def _utf8_flag_with_cp1252_name() -> bytes:
    # A zipper that sets the UTF-8 flag but writes the name in Windows-1252 ("Rechnung März.pdf").
    data = bytearray(_zip_with_name("Rechnung M?rz.pdf"))
    central = data.index(b"PK\x01\x02")
    data[central + 9] |= 0x08  # flag 0x800: names are UTF-8
    name_at = data.index(b"M?rz", central)
    data[name_at + 1] = 0xE4
    return bytes(data)


def _version_too_new() -> bytes:
    data = bytearray(_zip_with_name("statement.pdf"))
    central = data.index(b"PK\x01\x02")
    data[central + 6] = 64  # "version needed to extract" 6.4, above what zipfile supports
    return bytes(data)


@pytest.mark.parametrize("make_zip", [_utf8_flag_with_cp1252_name, _version_too_new])
def test_email_with_unreadable_zip_is_still_read(store, settings, make_zip):
    settings.ensure_data_dir()
    message = EmailMessage()
    message["Subject"] = "Invoice INV-555 and backup"
    message["From"] = "Harbor AP <ap@harbor.example>"
    message["Date"] = "Tue, 22 Sep 2026 09:00:00 -0400"
    message.set_content("Invoice INV-555, amount due $2,200.00. Backup zipped.")
    message.add_attachment(make_zip(), maintype="application", subtype="zip", filename="backup.zip")
    path = settings.inbox_incoming / "harbor.eml"
    path.write_bytes(message.as_bytes())
    old = time.time() - 3600
    os.utime(path, (old, old))

    report: dict = {}
    records = ingest_folder(store, settings, report=report)
    # A zip that can't be opened is kept as it came (explode_archives keeps a BadZipFile so); the email is read.
    assert report["failed"] == [], report["failed"]
    assert [r.subject for r in records] == ["Invoice INV-555 and backup"]


def test_eml_with_empty_message_id_is_read_not_failed(store, settings):
    # Some scanners and bulk mailers write an empty "Message-ID: <>". The stdlib's header parser raises
    # IndexError reading it, which sends the whole message to inbox/failed.
    settings.ensure_data_dir()
    path = settings.inbox_incoming / "statement.eml"
    path.write_bytes(
        b"From: Harbor AP <ap@harbor.example>\r\n"
        b"Subject: Invoice INV-555\r\n"
        b"Date: Tue, 22 Sep 2026 09:00:00 -0400\r\n"
        b"Message-ID: <>\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Amount due $2,200.00.\r\n"
    )
    old = time.time() - 3600
    os.utime(path, (old, old))
    report: dict = {}
    records = ingest_folder(store, settings, report=report)
    assert report["failed"] == [], report["failed"]
    assert [r.subject for r in records] == ["Invoice INV-555"]
