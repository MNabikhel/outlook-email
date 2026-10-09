"""With Outlook writeback on, CloseDesk PATCHes importance=high onto every message it files as High/Critical.
When that message is read again (a `sync` re-lists the last 72 hours), Graph returns importance "high", which
CloseDesk then scores as "Sender marked the message as high importance in Outlook" (+10): its own write is read
back as the sender's signal and the email's score and level climb on every re-read."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from controller_inbox.models import RawMessage
from controller_inbox.pipeline import ingest_mailbox


class FakeGraph:
    """Keeps what CloseDesk wrote to each message, as Outlook would."""

    def __init__(self, message):
        self.message = message
        self.importance = message.outlook_importance

    def list_messages(self, received_after=None):
        yield replace(self.message, outlook_importance=self.importance)

    def get_attachments(self, message_id):
        return []

    def apply_categories(self, message_id, categories, flag):
        if flag:
            self.importance = "high"  # GraphMailbox.apply_categories sends {"importance": "high"}
        return "written"


def test_own_writeback_is_not_read_as_sender_importance(store, settings):
    settings.writeback = True
    now = datetime(2026, 10, 9, 13, 0, tzinfo=timezone.utc)
    raw = RawMessage(
        id="AAMk-approval",
        subject="Approval needed: PO 4500123 for Northwind",
        sender_name="Priya Raman",
        sender_email="priya@taz.com",
        received_at=now - timedelta(hours=1),
        body_text="Hi, please approve PO 4500123 for $18,400.00 by Friday. Thanks",
        body_preview="",
        has_attachments=False,
        outlook_importance="normal",
        source="graph",
    )
    mailbox = FakeGraph(raw)
    first = ingest_mailbox(mailbox, store, settings, now=now)[0]
    assert first.importance.value in {"high", "critical"}  # so CloseDesk flagged it in Outlook
    assert mailbox.importance == "high"
    again = ingest_mailbox(mailbox, store, settings, received_after=now - timedelta(hours=72), now=now + timedelta(minutes=5))[0]
    assert again.importance == first.importance, (first.importance_score, again.importance_score, again.importance_reasons)
    assert again.importance_score == first.importance_score, again.importance_reasons
    assert not any("high importance in Outlook" in r for r in again.importance_reasons)
