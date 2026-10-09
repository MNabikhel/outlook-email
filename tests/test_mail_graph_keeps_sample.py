"""The sample mailbox is cleared when the first real message arrives through the drop folder, but not when it
arrives through Outlook (Graph). The sample's made-up fraud alerts and invoices then sit beside real mail for good:
real_mail_count() is no longer 0, so nothing ever clears them."""
from datetime import datetime, timezone

from controller_inbox.models import RawMessage
from controller_inbox.pipeline import ingest_demo, ingest_mailbox


class FakeGraph:
    def __init__(self, messages):
        self.messages = messages

    def list_messages(self, received_after=None):
        yield from self.messages

    def get_attachments(self, message_id):
        return []

    def apply_categories(self, message_id, categories, flag):
        return "skipped"


def test_first_outlook_sync_replaces_sample(store, settings, as_of_now):
    ingest_demo(store, settings, now=as_of_now)
    assert store.counts()["emails"] > 0 and store.real_mail_count() == 0
    now = datetime(2026, 10, 9, 13, 0, tzinfo=timezone.utc)
    real = RawMessage(
        id="AAMkAGI2-real-1",
        subject="Q3 accruals",
        sender_name="Maya Chen",
        sender_email="maya@taz.com",
        received_at=now,
        body_text="Accrual schedule is in the shared folder.",
        body_preview="",
        has_attachments=False,
        source="graph",
    )
    ingest_mailbox(FakeGraph([real]), store, settings, received_after=None, now=now)
    sources = {e.source for e in store.list_emails(limit=-1)}
    assert sources == {"graph"}, sources
