"""Emails read in bulk (list_emails, search_ranked, get_emails) are the same records get_email gives, the one-query
counts match what the separate queries counted, and an existing database gets the lookup indexes."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

from controller_inbox.store import Store

NEW_INDEXES = {
    "idx_att_email",
    "idx_actions_email",
    "idx_emails_message_id",
    "idx_emails_sender",
    "idx_emails_domain",
    "idx_corrections_email",
    "idx_corrections_sender",
    "idx_fraud_log_email",
    "idx_embeddings_email",
}


def test_listed_emails_are_the_records_get_email_gives(loaded: Store):
    listed = loaded.list_emails(limit=-1)
    assert listed
    assert any(len(email.actions) > 1 for email in listed)  # the task order is really compared
    for email in listed:
        assert email == loaded.get_email(email.id)
    assert loaded.get_emails([e.id for e in listed] + ["missing"]) == {e.id: e for e in listed}


def test_files_come_in_the_same_order_however_read(loaded: Store):
    email = next(e for e in loaded.list_emails(limit=-1) if e.attachments)
    first = email.attachments[0]
    email.attachments = [
        replace(first, id=f"{email.id}:{n}", filename=name) for n, name in enumerate(["b.pdf", "a.pdf", "c.pdf", "a.pdf"])
    ]
    loaded.upsert_email(email)
    stored = loaded.get_email(email.id)
    assert [att.id.rsplit(":", 1)[1] for att in stored.attachments] == ["1", "3", "0", "2"]
    assert next(e for e in loaded.list_emails(limit=-1) if e.id == email.id) == stored
    assert loaded.get_emails([email.id])[email.id] == stored


def test_search_ranked_gives_full_records(loaded: Store):
    found = loaded.search_ranked(["invoice"])
    assert found
    for email in found:
        assert email == loaded.get_email(email.id)


def test_flag_filter_still_applies(loaded: Store):
    flagged = loaded.list_emails(flag="fraud_risk", limit=-1)
    assert flagged
    assert all("fraud_risk" in email.flags for email in flagged)
    assert {e.id for e in flagged} == {e.id for e in loaded.list_emails(limit=-1) if "fraud_risk" in e.flags}


def _old_counts(store: Store) -> dict[str, int]:
    with store.connect() as conn:
        one = lambda sql, *args: conn.execute(sql, args).fetchone()[0]  # noqa: E731
        folder = "SELECT COUNT(*) FROM emails WHERE folder = ? AND COALESCE(done_at, '') = ''"
        return {
            "emails": one("SELECT COUNT(*) FROM emails"),
            "high_importance": one("SELECT COUNT(*) FROM emails WHERE importance IN ('critical','high')"),
            "open_actions": one("SELECT COUNT(*) FROM action_items WHERE status = 'open'"),
            "attachments": one("SELECT COUNT(*) FROM attachments"),
            "fraud_alerts": one("SELECT COUNT(*) FROM emails WHERE flags LIKE '%fraud_risk%'"),
            "important": one(folder, "important"),
            "informational": one(folder, "informational"),
            "reference": one(folder, "reference"),
            "waiting_on_bionic": one("SELECT COUNT(*) FROM emails WHERE model_status = 'script_draft'"),
            "read_by_bionic": one("SELECT COUNT(*) FROM emails WHERE model_status = 'bionic'"),
        }


def test_counts_match_the_separate_queries(store: Store, loaded: Store):
    first = loaded.list_emails(folder="important", limit=1)[0]
    loaded.set_done(first.id, True)
    counts = loaded.counts()
    assert counts == _old_counts(loaded)
    assert list(counts) == list(_old_counts(loaded))
    assert counts["fraud_alerts"] and counts["important"]


def test_counts_of_an_empty_store(store: Store):
    assert store.counts() == _old_counts(store)
    assert set(store.counts().values()) == {0}


def test_only_sample_and_has_email(store: Store, loaded: Store):
    assert loaded.only_sample() == (bool(loaded.counts()["emails"]) and not loaded.real_mail_count())
    assert loaded.only_sample()
    some = loaded.list_emails(limit=1)[0]
    assert loaded.has_email(some.id) and not loaded.has_email("nope")
    with loaded.connect() as conn:
        conn.execute("UPDATE emails SET source = 'folder' WHERE id = ?", (some.id,))
    assert not loaded.only_sample()


def test_only_sample_is_false_without_mail(store: Store):
    assert not store.only_sample()


def test_an_existing_database_gets_the_indexes(tmp_path):
    path = tmp_path / "old.db"
    Store(path)
    with sqlite3.connect(path) as conn:
        for name in NEW_INDEXES:
            conn.execute(f"DROP INDEX {name}")
    Store(path)
    with sqlite3.connect(path) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        assert NEW_INDEXES <= names
        plan = " ".join(
            str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN SELECT * FROM attachments WHERE email_id = 'x'")
        )
        assert "idx_att_email" in plan
        plan = " ".join(
            str(row[3])
            for row in conn.execute("EXPLAIN QUERY PLAN SELECT COUNT(*) FROM emails WHERE lower(sender_email) = 'a@b.com'")
        )
        assert "idx_emails_sender" in plan


def test_email_correction_is_the_latest_for_that_email(store: Store):
    def row(n: int, email_id: str, at: str) -> dict:
        return {
            "id": f"c{n}", "email_id": email_id, "previous_category": "other", "corrected_category": "ap_invoice",
            "reason": f"r{n}", "sender_email": "a@b.com", "subject": "s", "created_at": at,
        }

    store.add_correction(row(1, "e1", "2026-01-01T00:00:00+00:00"))
    store.add_correction(row(2, "e1", "2026-03-01T00:00:00+00:00"))
    store.add_correction(row(3, "e2", "2026-04-01T00:00:00+00:00"))
    expected = next(r for r in store.list_corrections() if r["email_id"] == "e1")
    assert store.email_correction("e1") == expected
    assert expected["id"] == "c2"
    assert store.email_correction("e9") is None
