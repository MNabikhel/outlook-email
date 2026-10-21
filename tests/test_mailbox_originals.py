"""Mail read from a mailbox (Microsoft 365, the sample) keeps its files as they came, as the drop folder and classic
Outlook do: the Preview tab, Download and the page view open the original."""

from __future__ import annotations

from dataclasses import replace

from controller_inbox import agent
from controller_inbox.demo import DemoMailbox
from controller_inbox.pipeline import ingest_demo, ingest_mailbox


def test_the_sample_mailboxs_pdf_can_be_previewed(store, settings):
    ingest_demo(store, settings)
    email = store.get_email("demo-inv-10482")
    att = next(a for a in email.attachments if a.filename == "INV-10482.pdf")
    assert agent.original_file(settings, email, att) is not None


class LazyMailbox(DemoMailbox):
    """As Microsoft Graph lists mail: a message says it has files, which are fetched on their own."""

    def list_messages(self, received_after=None):
        for raw in super().list_messages(received_after):
            yield replace(raw, attachments=[], has_attachments=bool(raw.attachments))


def test_files_fetched_after_listing_are_kept_too(store, settings):
    ingest_mailbox(LazyMailbox(), store, settings, cursor=None)
    email = store.get_email("demo-inv-10482")
    att = next(a for a in email.attachments if a.filename == "INV-10482.pdf")
    path = agent.original_file(settings, email, att)
    assert path is not None and path.read_bytes().startswith(b"%PDF")
