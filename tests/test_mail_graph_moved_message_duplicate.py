"""Graph gives a message a new id when the user moves it to another folder (the default REST id is not immutable).
CloseDesk lists /me/messages (every folder) and keys mail by that id only, never by internetMessageId, so a
`sync` (which re-lists the last 72 hours) after the user filed an invoice reads it again as a new email:
two copies on the board, and the invoice flagged a "possible duplicate" of itself with a review task."""
from datetime import datetime, timedelta, timezone

from controller_inbox.models import RawMessage
from controller_inbox.pipeline import ingest_mailbox


class FakeGraph:
    def __init__(self, messages):
        self.messages = messages

    def list_messages(self, received_after=None):
        yield from self.messages

    def get_attachments(self, message_id):
        return []

    def apply_categories(self, message_id, categories, flag):
        return "skipped"


def _invoice(graph_id):
    return RawMessage(
        id=graph_id,
        subject="Invoice INV-55120 from Northwind",
        sender_name="Northwind AR",
        sender_email="ar@northwind.com",
        received_at=datetime(2026, 10, 8, 15, 0, tzinfo=timezone.utc),
        body_text="Please find invoice INV-55120 for $3,200.00. Payment due: October 30, 2026.",
        body_preview="",
        has_attachments=False,
        internet_message_id="<CAF1234.northwind@mail.northwind.com>",
        conversation_id="AAQkAGI2conv",
        source="graph",
    )


def test_moved_message_is_not_a_second_email(store, settings):
    now = datetime(2026, 10, 9, 13, 0, tzinfo=timezone.utc)
    ingest_mailbox(FakeGraph([_invoice("AAMkAGI2-inbox-id")]), store, settings, now=now)
    # The user files it in Outlook (new id), then runs `sync`, which re-lists the last 72 hours.
    later = now + timedelta(hours=2)
    ingest_mailbox(FakeGraph([_invoice("AAMkAGI2-vendors-folder-id")]), store, settings,
                   received_after=later - timedelta(hours=72), now=later)
    emails = store.list_emails(limit=-1)
    assert len(emails) == 1, [(e.id, e.flags) for e in emails]
