"""Real Outlook .msg files dropped in the folder: senders, forwarded emails, and every attachment."""

from __future__ import annotations

import io

from openpyxl import Workbook

from controller_inbox.demo import make_pdf
from controller_inbox.folder_mail import ingest_folder
from msgfactory import PDF, XLSX, build_message, write_msg


def _xlsx(rows) -> bytes:
    book = Workbook()
    for row in rows:
        book.active.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def test_a_colleague_on_exchange_is_read_with_their_real_address(store, settings):
    write_msg(
        settings.inbox_incoming / "budget.msg",
        "Q4 budget draft",
        "Can you look over the draft before Friday?",
        sender_name="Maya Chen",
        sender_email="maya@taz.com",
        exchange=True,
        attachments=[("Q4 budget.xlsx", _xlsx([["Line", "Q4"], ["Ads", 1500]]), XLSX)],
    )
    (record,) = ingest_folder(store, settings)
    assert (record.sender_name, record.sender_email) == ("Maya Chen", "maya@taz.com")
    (attachment,) = record.attachments
    assert attachment.filename == "Q4 budget.xlsx"
    assert "A2: Ads | B2: 1,500" in attachment.extracted_text


def test_a_forwarded_email_brings_its_own_attachments(store, settings):
    quote = build_message(
        "Acme quote",
        "Our quote is attached. Valid for 30 days.",
        sender_name="Acme Sales",
        sender_email="sales@acme.com",
        attachments=[
            ("quote.pdf", make_pdf([["Acme quote Q-881", ["Item", "Amount"], ["Support plan", "$9,600.00"]]]), PDF),
        ],
    )
    write_msg(
        settings.inbox_incoming / "fw quote.msg",
        "FW: Acme quote",
        "Is this in line with last year?",
        sender_name="Priya Raman",
        sender_email="priya@taz.com",
        forwarded=[("Acme quote.msg", quote)],
        logo=b"\x89PNG\r\n\x1a\n" + b"0" * 800,
        html='<p>Is this in line with last year?</p><img src="cid:image001.png@01DB">',
    )
    (record,) = ingest_folder(store, settings)
    files = {att.filename: att.extracted_text for att in record.attachments}
    assert set(files) == {"Acme quote.txt", "Acme quote › quote.pdf"}, "the signature logo is not an attachment"
    assert "From: Acme Sales <sales@acme.com>" in files["Acme quote.txt"]
    assert "Attachments: quote.pdf" in files["Acme quote.txt"]
    assert "Date: Mon, 28 Sep 2026 14:30:00" in files["Acme quote.txt"]
    assert "Item: Support plan | Amount: $9,600.00" in files["Acme quote › quote.pdf"]
    saved = {path.name for path in (settings.inbox_extracted / record.id).iterdir()}
    assert saved == {"Acme quote.txt", "Acme quote › quote.pdf"}


def test_reply_to_is_kept_for_the_fraud_check(store, settings):
    write_msg(
        settings.inbox_incoming / "billing.msg",
        "Invoice 7781",
        "Invoice attached.",
        sender_name="Northwind Billing",
        sender_email="billing@northwind.com",
        reply_to="Accounts <accounts@northwind-payments.co>",
    )
    (record,) = ingest_folder(store, settings)
    assert record.reply_to == "accounts@northwind-payments.co"
    assert store.get_email(record.id).reply_to == "accounts@northwind-payments.co"


def test_long_attachments_are_kept_whole(store, settings):
    rows = [[f"Line {n}", n] for n in range(1, 900)]
    write_msg(
        settings.inbox_incoming / "ledger.msg",
        "Ledger export",
        "Full ledger attached.",
        attachments=[("ledger.xlsx", _xlsx(rows), XLSX)],
    )
    (record,) = ingest_folder(store, settings)
    text = store.get_email(record.id).attachments[0].extracted_text
    assert len(text) > 20_000 and "A899: Line 899" in text


def _with_property(message, prop: int, kind: str, value):
    import aspose.email_foss.msg as msg

    message.set_property(prop, getattr(msg.PropertyTypeCode, kind), value)
    return message


def test_mail_written_in_rich_text_is_read_from_its_rtf_body(store, settings):
    settings.ensure_data_dir()
    import compressed_rtf

    rtf = (
        rb"{\rtf1\ansi\ansicpg1252\deff0\nouicompat{\fonttbl{\f0\fnil\fcharset0 Calibri;}}\viewkind4\uc1 \pard\f0\fs22 "
        rb"Please pay invoice INV-7781 for $12,450.00 by October 15, 2026.\par Caf\'e9 Rouge Ltd\par}"
    )
    message = _with_property(build_message("Rich text invoice", ""), 0x1009, "PTYP_BINARY", compressed_rtf.compress(rtf, compressed=True))
    message.save(str(settings.inbox_incoming / "rtf.msg"))
    (record,) = ingest_folder(store, settings)
    assert "INV-7781" in record.body_text and "Café Rouge Ltd" in record.body_text
    assert 12450.0 in record.extracted.amounts


def test_a_body_the_rtf_converter_fails_on_does_not_lose_the_email(store, settings):
    settings.ensure_data_dir()
    import compressed_rtf

    # extract-msg's RTF converter raises KeyError on this font table; the email and its file are still read.
    rtf = rb"{\rtf1\ansi\ansicpg1252\deff0{\fonttbl{\f0 Calibri;}}\f0\fs22 Caf\'e9 total \'2412,450.00.\par}"
    message = build_message("Odd RTF", "", attachments=[("INV-7781.pdf", make_pdf([["Invoice INV-7781", "Total $12,450.00"]]), PDF)])
    _with_property(message, 0x1009, "PTYP_BINARY", compressed_rtf.compress(rtf, compressed=True))
    message.save(str(settings.inbox_incoming / "odd.msg"))
    report: dict = {}
    (record,) = ingest_folder(store, settings, report=report)
    assert report["failed"] == []
    assert [att.filename for att in record.attachments] == ["INV-7781.pdf"]
    assert "Café total $12,450.00." in record.body_text


def test_an_html_only_body_saved_in_windows_1252_keeps_its_accents_and_euro_sign(store, settings):
    settings.ensure_data_dir()
    html = (
        '<html><head><meta http-equiv="Content-Type" content="text/html; charset=windows-1252"></head>'
        "<body><p>Montant dû : 1 250,00 € – café</p></body></html>"
    ).encode("cp1252")
    message = _with_property(build_message("HTML only", ""), 0x1013, "PTYP_BINARY", html)
    message.save(str(settings.inbox_incoming / "html.msg"))
    (record,) = ingest_folder(store, settings)
    assert record.body_text == "Montant dû : 1 250,00 € – café"


def _msg_bytes(tmp_path, message) -> bytes:
    path = tmp_path / "saved.msg"
    message.save(str(path))
    data = path.read_bytes()
    path.unlink()
    return data


def _eml_with_file(name: str, data: bytes, mime: str) -> bytes:
    import base64

    return (
        b"From: Ann <ann@co.com>\r\nTo: ap@co.com\r\nSubject: FW: Vendor bank change\r\nDate: Mon, 21 Sep 2026 10:00:00 +0000\r\n"
        b"Message-ID: <fw1@co.com>\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=XX\r\n\r\n"
        b"--XX\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nSee the forwarded message.\r\n"
        b"--XX\r\nContent-Type: " + mime.encode() + b"\r\nContent-Disposition: attachment; filename=\"" + name.encode() + b"\"\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n" + base64.encodebytes(data) + b"--XX--\r\n"
    )


def test_an_outlook_message_attached_as_a_file_is_read_like_a_forwarded_one(store, settings, tmp_path):
    import zipfile

    settings.ensure_data_dir()
    inner = build_message(
        "Vendor bank change", "Please pay invoice INV-5521 for $48,000.00 to our new account number 99887766.",
        attachments=[("remit.csv", b"invoice,amount\nINV-5521,48000.00\n", "text/csv")],
    )
    data = _msg_bytes(tmp_path, inner)
    # Dragged from Outlook into another mail program, saved as an .eml, and also sent along in a zip.
    (settings.inbox_incoming / "fw.eml").write_bytes(_eml_with_file("Vendor bank change.msg", data, "application/vnd.ms-outlook"))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as packed:
        packed.writestr("mail/Vendor bank change.msg", data)
    (settings.inbox_incoming / "mails.zip").write_bytes(archive.getvalue())
    records = ingest_folder(store, settings)
    assert len(records) == 2
    for record in records:
        files = {att.filename: att.extracted_text for att in record.attachments}
        assert set(files) == {"Vendor bank change.txt", "Vendor bank change › remit.csv"}, record.subject
        assert "INV-5521" in files["Vendor bank change.txt"] and "account ****7766" in files["Vendor bank change.txt"]
        assert "99887766" not in files["Vendor bank change.txt"]
        assert 48000.0 in record.extracted.amounts


def test_an_eml_attached_as_a_file_to_a_msg_is_read_like_a_forwarded_one(store, settings):
    settings.ensure_data_dir()
    eml = (
        b"From: Vendor <ar@vendor.com>\r\nSubject: New bank details\r\nX-Filler: " + b"a" * 2500 + b"\r\n"
        b"Content-Type: text/plain\r\n\r\nPay INV-5521 $48,000.00 to account number 99887766.\r\n"
    )
    write_msg(settings.inbox_incoming / "fw.msg", "FW: New bank details", "see attached",
              attachments=[("New bank details.eml", eml, "message/rfc822")])
    (record,) = ingest_folder(store, settings)
    (attachment,) = record.attachments
    assert attachment.filename == "New bank details.txt"
    assert "Pay INV-5521 $48,000.00 to account ****7766." in attachment.extracted_text


def test_a_message_signed_with_smime_brings_its_files(store, settings):
    import base64

    settings.ensure_data_dir()
    pdf = make_pdf([["Invoice INV-7310", "Amount due $9,850.00"]])
    signed = (
        b"Content-Type: multipart/signed; protocol=\"application/pkcs7-signature\"; micalg=sha-256; boundary=SIG\r\n\r\n"
        b"--SIG\r\nContent-Type: multipart/mixed; boundary=MIX\r\n\r\n--MIX\r\nContent-Type: text/plain\r\n\r\nInvoice attached.\r\n"
        b"--MIX\r\nContent-Type: application/pdf; name=\"INV-7310.pdf\"\r\nContent-Disposition: attachment; filename=\"INV-7310.pdf\"\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n" + base64.encodebytes(pdf) + b"--MIX--\r\n\r\n"
        b"--SIG\r\nContent-Type: application/pkcs7-signature; name=smime.p7s\r\nContent-Disposition: attachment; filename=smime.p7s\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\nMIIB\r\n--SIG--\r\n"
    )
    message = build_message("Signed invoice", "Invoice attached.", attachments=[("smime.p7m", signed, "multipart/signed")])
    message.message_class = "IPM.Note.SMIME.MultipartSigned"
    message.save(str(settings.inbox_incoming / "signed.msg"))
    (record,) = ingest_folder(store, settings)
    assert [att.filename for att in record.attachments] == ["INV-7310.pdf"]
    assert 9850.0 in record.extracted.amounts
