"""Build real Outlook .msg files for tests, shaped like the ones Outlook saves.

Two sender shapes matter: mail from outside arrives with Internet headers
(From, Reply-To, Date, Message-ID), and mail from a colleague on the same
Exchange server carries an /O=EXCHANGELABS/… address plus a separate SMTP
address property.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest

msg = pytest.importorskip("aspose.email_foss.msg")

SENDER_SMTP = 0x5D01


def build_message(
    subject: str,
    body: str,
    *,
    sender_name: str = "Maya Chen",
    sender_email: str = "maya@taz.com",
    exchange: bool = False,
    reply_to: str = "",
    sent: datetime | None = None,
    message_id: str = "",
    attachments: list[tuple[str, bytes, str]] | None = None,
    forwarded: list[tuple[str, "msg.MapiMessage"]] | None = None,
    logo: bytes | None = None,
    html: str = "",
):
    sent = sent or datetime(2026, 9, 28, 14, 30, tzinfo=timezone.utc)
    message = msg.MapiMessage.create(subject, body)
    string = msg.PropertyTypeCode.PTYP_STRING
    message.set_property(msg.PropertyId.SENDER_NAME, string, sender_name)
    if exchange:
        message.set_property(msg.PropertyId.SENDER_ADDRESS_TYPE, string, "EX")
        message.set_property(
            msg.PropertyId.SENDER_EMAIL_ADDRESS,
            string,
            f"/O=EXCHANGELABS/OU=EXCHANGE ADMINISTRATIVE GROUP/CN=RECIPIENTS/CN={sender_name.replace(' ', '').lower()}",
        )
        message.set_property(SENDER_SMTP, string, sender_email)
    else:
        headers = [
            f"From: {sender_name} <{sender_email}>",
            f"Date: {format_datetime(sent)}",
            f"Subject: {subject}",
            f"Message-ID: {message_id or '<' + subject.replace(' ', '-').lower() + '@' + sender_email.split('@')[-1] + '>'}",
        ]
        if reply_to:
            headers.append(f"Reply-To: {reply_to}")
        message.set_property(msg.PropertyId.TRANSPORT_MESSAGE_HEADERS, string, "\r\n".join(headers) + "\r\n")
    message.set_property(msg.PropertyId.MESSAGE_DELIVERY_TIME, msg.PropertyTypeCode.PTYP_TIME, sent)
    if html:
        message.body_html = html
    for name, data, mime in attachments or []:
        message.add_attachment(name, data, mime_type=mime)
    if logo is not None:
        message.add_attachment("image001.png", logo, mime_type="image/png", content_id="image001.png@01DB")
    for name, inner in forwarded or []:
        message.add_embedded_message_attachment(inner, filename=name, mime_type="application/vnd.ms-outlook")
    return message


def write_msg(path: Path, subject: str, body: str, **kwargs) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    build_message(subject, body, **kwargs).save(str(path))
    return path


XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PDF = "application/pdf"
