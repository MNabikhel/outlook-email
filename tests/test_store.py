"""The database (store.py): search, duplicate invoices, task order, columns added by an update."""

from __future__ import annotations

import sqlite3

import pytest

from controller_inbox.models import ActionItem, AttachmentRecord, DocumentType, EmailRecord, ExtractedFields, Importance
from controller_inbox.store import Store, _add_column


def _email(email_id: str, subject: str, **kw) -> EmailRecord:
    base = dict(
        id=email_id, subject=subject, sender_name="Vendor", sender_email="ap@vendor.example",
        received_at="2026-09-20T10:00:00+00:00", body_text="", body_preview="", has_attachments=False,
        outlook_importance="normal", is_read=False, category=DocumentType.OTHER, category_confidence=0.5,
        importance=Importance.LOW, importance_score=10,
    )
    base.update(kw)
    return EmailRecord(**base)


# --- Search ignored case only for A-Z: "Müller" missed "MÜLLER" ---------------------------------------------------
def test_list_emails_search_ignores_case_of_accented_letters(store):
    store.upsert_email(_email("e1", "Rechnung MÜLLER GMBH"))
    assert [e.id for e in store.list_emails(q="Müller")] == ["e1"]
    assert [e.id for e in store.list_emails(q="müller")] == ["e1"]


def test_search_ranked_ignores_case_of_accented_letters(store):
    store.upsert_email(_email("e1", "FACTURE ÉLECTRICITÉ septembre"))
    assert [e.id for e in store.search_ranked(["électricité"])] == ["e1"]


def test_chat_search_ignores_case_of_accented_letters(store):
    store.create_chat("c1")
    store.touch_chat("c1", title="Müller invoice question")
    store.add_chat_turn("c1", "user", "What does ÖKO GmbH owe?")
    assert [c["id"] for c in store.list_chats("MÜLLER")] == ["c1"]
    assert [c["id"] for c in store.list_chats("öko")] == ["c1"]


# --- An invoice number matched any extracted field (a PO number, an account ending) as a duplicate ----------------
def test_duplicate_invoice_only_matches_invoice_numbers(store):
    store.upsert_email(_email("po", "PO 4500123 issued", extracted=ExtractedFields(po_numbers=["4500123"])))
    store.upsert_email(_email("acct", "Statement", extracted=ExtractedFields(account_last4=["1234"])))
    assert store.find_duplicate_invoices("4500123", "new") == []
    assert store.find_duplicate_invoices("1234", "new") == []


def test_duplicate_invoice_matches_the_email_or_a_file_ignoring_case(store):
    store.upsert_email(_email("body", "Invoice", extracted=ExtractedFields(invoice_numbers=["INV-77"])))
    pdf = AttachmentRecord(
        id="att", email_id="file", filename="invoice.pdf", content_type="application/pdf", size_bytes=10,
        sha256="0" * 64, extracted_text="", document_type=DocumentType.AP_INVOICE, document_confidence=0.9,
        extracted_fields=ExtractedFields(invoice_numbers=["INV-88"]),
    )
    store.upsert_email(_email("file", "Invoice attached", attachments=[pdf], has_attachments=True))
    assert store.find_duplicate_invoices("inv-77", "new") == ["body"]
    assert store.find_duplicate_invoices("INV-88", "new") == ["file"]
    assert store.find_duplicate_invoices("INV-88", "file") == []


# --- An email's tasks were sorted by priority text: high, low, medium ---------------------------------------------
def test_get_email_orders_tasks_by_priority_not_alphabet(store):
    actions = [
        ActionItem(id=f"a-{p.value}", email_id="e1", title=p.value, detail="", due_date="2026-09-30", priority=p)
        for p in (Importance.LOW, Importance.MEDIUM, Importance.HIGH)
    ]
    store.upsert_email(_email("e1", "Invoice", actions=actions))
    assert [a.priority.value for a in store.get_email("e1").actions] == ["high", "medium", "low"]


def test_list_emails_with_a_very_long_search_word_does_not_crash(loaded):
    # A pasted blob in the search box is one "word" of the query; SQLite refuses LIKE patterns over 50,000 bytes.
    assert loaded.list_emails(q="x" * 60_000) == []


def test_search_ranked_with_a_very_long_term_does_not_crash(loaded):
    assert loaded.search_ranked(["x" * 60_000]) == []


def test_two_processes_adding_the_same_column_after_an_update_dont_crash(tmp_path):
    path = tmp_path / "old.db"
    first, second = sqlite3.connect(path), sqlite3.connect(path)
    first.execute("CREATE TABLE emails (id TEXT PRIMARY KEY)")
    first.commit()
    assert _add_column(first, "ALTER TABLE emails ADD COLUMN folder TEXT DEFAULT ''")
    first.commit()
    assert not _add_column(second, "ALTER TABLE emails ADD COLUMN folder TEXT DEFAULT ''"), "the other process added it"
    with pytest.raises(sqlite3.OperationalError):
        _add_column(second, "ALTER TABLE no_such_table ADD COLUMN x TEXT")


def test_store_uses_wal_and_a_busy_timeout(store: Store):
    with store.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 10_000


def test_like_patterns_treat_wildcards_as_text(loaded: Store):
    a, b = loaded.list_emails(limit=2)
    # Not INV-0001: separators don't count in an invoice number, so that one is the same invoice.
    a.extracted.invoice_numbers = ["INVX0001"]
    b.extracted.invoice_numbers = ["INV_0001"]
    loaded.upsert_email(a)
    loaded.upsert_email(b)
    assert a.id not in loaded.find_duplicate_invoices("INV_0001", exclude_email_id=b.id)
    loaded.create_chat("c1")
    loaded.add_chat_turn("c1", "user", "plain question")
    assert loaded.list_chats("%") == []
    assert loaded.list_chats("_") == []
    assert [c["id"] for c in loaded.list_chats("plain")] == ["c1"]
