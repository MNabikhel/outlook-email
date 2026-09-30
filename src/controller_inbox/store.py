from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from controller_inbox.models import (
    ActionItem,
    ActionStatus,
    AttachmentRecord,
    DocumentType,
    EmailRecord,
    ExtractedFields,
    Importance,
)

# Python 3.12 deprecated sqlite3's built-in datetime adapter; this is the same format it wrote.
sqlite3.register_adapter(datetime, lambda value: value.isoformat(" "))


SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    sender_name TEXT,
    sender_email TEXT,
    received_at TEXT,
    body_text TEXT,
    body_preview TEXT,
    has_attachments INTEGER,
    outlook_importance TEXT,
    is_read INTEGER,
    category TEXT,
    category_confidence REAL,
    importance TEXT,
    importance_score INTEGER,
    importance_reasons TEXT,
    flags TEXT,
    extracted TEXT,
    source TEXT,
    conversation_id TEXT,
    internet_message_id TEXT,
    writeback_status TEXT,
    created_at TEXT,
    folder TEXT,
    summary TEXT,
    model_status TEXT,
    source_path TEXT DEFAULT '',
    reply_to TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS attachments (
    id TEXT PRIMARY KEY,
    email_id TEXT NOT NULL,
    filename TEXT,
    content_type TEXT,
    size_bytes INTEGER,
    sha256 TEXT,
    extracted_text TEXT,
    document_type TEXT,
    document_confidence REAL,
    extracted_fields TEXT,
    classification_reasons TEXT,
    FOREIGN KEY(email_id) REFERENCES emails(id)
);

CREATE TABLE IF NOT EXISTS action_items (
    id TEXT PRIMARY KEY,
    email_id TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT,
    due_date TEXT,
    priority TEXT,
    status TEXT,
    source TEXT,
    created_at TEXT,
    FOREIGN KEY(email_id) REFERENCES emails(id)
);

CREATE TABLE IF NOT EXISTS digests (
    period_date TEXT PRIMARY KEY,
    generated_at TEXT,
    markdown TEXT,
    html TEXT,
    payload TEXT
);

CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS corrections (
    id TEXT PRIMARY KEY,
    email_id TEXT,
    previous_category TEXT,
    corrected_category TEXT,
    reason TEXT,
    sender_email TEXT,
    subject TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS fraud_trust (
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    verdict TEXT NOT NULL,
    source TEXT,
    note TEXT,
    created_at TEXT,
    PRIMARY KEY (kind, value)
);

CREATE TABLE IF NOT EXISTS fraud_checks (
    email_id TEXT PRIMARY KEY,
    score INTEGER,
    level TEXT,
    signals TEXT,
    checked_at TEXT
);

CREATE TABLE IF NOT EXISTS fraud_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT,
    email_id TEXT,
    event TEXT,
    level TEXT,
    score INTEGER,
    sender_email TEXT,
    subject TEXT,
    signals TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id TEXT NOT NULL,
    at TEXT,
    question TEXT,
    text TEXT
);

CREATE INDEX IF NOT EXISTS idx_findings_email ON findings(email_id);

CREATE TABLE IF NOT EXISTS embeddings (
    key TEXT NOT NULL,
    model TEXT NOT NULL,
    email_id TEXT NOT NULL,
    text_key TEXT NOT NULL,
    vector BLOB NOT NULL,
    PRIMARY KEY (key, model)
);

CREATE TABLE IF NOT EXISTS file_summaries (
    attachment_id TEXT PRIMARY KEY,
    text_key TEXT NOT NULL,
    summary TEXT NOT NULL,
    model TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_emails_received ON emails(received_at);
CREATE INDEX IF NOT EXISTS idx_emails_importance ON emails(importance);
CREATE INDEX IF NOT EXISTS idx_emails_category ON emails(category);
CREATE INDEX IF NOT EXISTS idx_actions_status ON action_items(status);
CREATE INDEX IF NOT EXISTS idx_actions_due ON action_items(due_date);
CREATE INDEX IF NOT EXISTS idx_att_hash ON attachments(sha256);
"""


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            _migrate(conn)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def reset(self) -> None:
        if self.path.exists():
            self.path.unlink()
        self._init()

    def upsert_email(self, email: EmailRecord) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO emails (
                    id, subject, sender_name, sender_email, received_at, body_text,
                    body_preview, has_attachments, outlook_importance, is_read,
                    category, category_confidence, importance, importance_score,
                    importance_reasons, flags, extracted, source, conversation_id,
                    internet_message_id, writeback_status, created_at,
                    folder, summary, model_status, reply_to
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    subject=excluded.subject,
                    sender_name=excluded.sender_name,
                    sender_email=excluded.sender_email,
                    received_at=excluded.received_at,
                    body_text=excluded.body_text,
                    body_preview=excluded.body_preview,
                    has_attachments=excluded.has_attachments,
                    outlook_importance=excluded.outlook_importance,
                    is_read=excluded.is_read,
                    category=excluded.category,
                    category_confidence=excluded.category_confidence,
                    importance=excluded.importance,
                    importance_score=excluded.importance_score,
                    importance_reasons=excluded.importance_reasons,
                    flags=excluded.flags,
                    extracted=excluded.extracted,
                    source=excluded.source,
                    conversation_id=excluded.conversation_id,
                    internet_message_id=excluded.internet_message_id,
                    writeback_status=excluded.writeback_status,
                    folder=excluded.folder,
                    summary=excluded.summary,
                    model_status=excluded.model_status,
                    reply_to=excluded.reply_to
                """,
                (
                    email.id,
                    email.subject,
                    email.sender_name,
                    email.sender_email,
                    email.received_at,
                    email.body_text,
                    email.body_preview,
                    int(email.has_attachments),
                    email.outlook_importance,
                    int(email.is_read),
                    email.category.value,
                    email.category_confidence,
                    email.importance.value,
                    email.importance_score,
                    _dumps(email.importance_reasons),
                    _dumps(email.flags),
                    _dumps(email.extracted.to_dict()),
                    email.source,
                    email.conversation_id,
                    email.internet_message_id,
                    email.writeback_status,
                    email.created_at,
                    email.folder,
                    email.summary,
                    email.model_status or "script_draft",
                    email.reply_to,
                ),
            )
            conn.execute("DELETE FROM attachments WHERE email_id = ?", (email.id,))
            for att in email.attachments:
                conn.execute(
                    """
                    INSERT INTO attachments (
                        id, email_id, filename, content_type, size_bytes, sha256,
                        extracted_text, document_type, document_confidence,
                        extracted_fields, classification_reasons
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        att.id,
                        email.id,
                        att.filename,
                        att.content_type,
                        att.size_bytes,
                        att.sha256,
                        att.extracted_text,
                        att.document_type.value,
                        att.document_confidence,
                        _dumps(att.extracted_fields.to_dict()),
                        _dumps(att.classification_reasons),
                    ),
                )
            done_titles = {
                row["title"]
                for row in conn.execute(
                    "SELECT title FROM action_items WHERE email_id = ? AND status = 'done'",
                    (email.id,),
                )
            }
            conn.execute(
                "DELETE FROM action_items WHERE email_id = ? AND status != 'done'",
                (email.id,),
            )
            for action in email.actions:
                if action.title in done_titles:
                    continue
                conn.execute(
                    """
                    INSERT INTO action_items (
                        id, email_id, title, detail, due_date, priority, status, source, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        title=excluded.title,
                        detail=excluded.detail,
                        due_date=excluded.due_date,
                        priority=excluded.priority,
                        source=excluded.source
                    """,
                    (
                        action.id,
                        email.id,
                        action.title,
                        action.detail,
                        action.due_date,
                        action.priority.value,
                        action.status.value,
                        action.source,
                        action.created_at,
                    ),
                )

    def get_email(self, email_id: str) -> EmailRecord | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
            if not row:
                return None
            attachments = conn.execute(
                "SELECT * FROM attachments WHERE email_id = ? ORDER BY filename",
                (email_id,),
            ).fetchall()
            actions = conn.execute(
                "SELECT * FROM action_items WHERE email_id = ? ORDER BY due_date IS NULL, due_date, priority",
                (email_id,),
            ).fetchall()
        return _email_from_rows(row, attachments, actions)

    def list_emails(
        self,
        *,
        importance: str | None = None,
        category: str | None = None,
        flag: str | None = None,
        q: str | None = None,
        folder: str | None = None,
        model_status: str | None = None,
        oldest_first: bool = False,
        received_from: str | None = None,
        received_before: str | None = None,
        order: str | None = None,
        limit: int = 200,
    ) -> list[EmailRecord]:
        """List messages.

        ``order`` is ``newest`` (default), ``oldest``, ``score`` (most important
        first), or ``queue`` (the order the local model should read: Important
        first, then by score, oldest first within a tie).
        """
        clauses = ["1=1"]
        params: list[Any] = []
        if received_from:
            clauses.append("received_at >= ?")
            params.append(received_from)
        if received_before:
            clauses.append("received_at < ?")
            params.append(received_before)
        if importance:
            clauses.append("importance = ?")
            params.append(importance)
        if category:
            clauses.append("category = ?")
            params.append(category)
        if folder:
            clauses.append("folder = ?")
            params.append(folder)
        if model_status:
            clauses.append("model_status = ?")
            params.append(model_status)
        for word in (q or "").split()[:8]:
            clauses.append(_MATCH_ANY)
            params.extend([_contains(word)] * _MATCH_ANY.count("?"))
        order = order or ("oldest" if oldest_first else "newest")
        order_sql = {
            "newest": "received_at DESC",
            "oldest": "received_at ASC",
            "score": "importance_score DESC, received_at DESC",
            "queue": "CASE folder WHEN 'important' THEN 0 WHEN 'informational' THEN 1 ELSE 2 END, "
            "importance_score DESC, received_at ASC",
        }.get(order, "received_at DESC")
        sql = f"SELECT * FROM emails WHERE {' AND '.join(clauses)} ORDER BY {order_sql} LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            out: list[EmailRecord] = []
            for row in rows:
                attachments = conn.execute(
                    "SELECT * FROM attachments WHERE email_id = ?",
                    (row["id"],),
                ).fetchall()
                actions = conn.execute(
                    "SELECT * FROM action_items WHERE email_id = ?",
                    (row["id"],),
                ).fetchall()
                email = _email_from_rows(row, attachments, actions)
                if flag and flag not in email.flags:
                    continue
                out.append(email)
        return out

    def search_ranked(self, terms: list[str], *, limit: int = 6) -> list[EmailRecord]:
        """Emails that mention the most of ``terms``; subject and sender hits count more."""
        terms = [t for t in dict.fromkeys(t.lower() for t in terms if t.strip())][:10]
        if not terms:
            return []
        parts, params = [], []
        for term in terms:
            like = _contains(term)
            parts.append(
                f"(CASE WHEN lower(subject) {_LIKE} THEN 3 ELSE 0 END"
                f" + CASE WHEN lower(sender_name) {_LIKE} OR lower(sender_email) {_LIKE} THEN 3 ELSE 0 END"
                f" + CASE WHEN lower(summary) {_LIKE} THEN 2 ELSE 0 END"
                f" + CASE WHEN lower(body_text) {_LIKE} THEN 1 ELSE 0 END"
                f" + CASE WHEN EXISTS (SELECT 1 FROM attachments a WHERE a.email_id = emails.id"
                f" AND (lower(a.filename) {_LIKE} OR lower(a.extracted_text) {_LIKE})) THEN 1 ELSE 0 END)"
            )
            params.extend([like] * 7)
        sql = (
            f"SELECT id, ({' + '.join(parts)}) AS hits FROM emails WHERE hits > 0"
            " ORDER BY hits DESC, importance_score DESC, received_at DESC LIMIT ?"
        )
        with self.connect() as conn:
            rows = [(row["id"], row["hits"]) for row in conn.execute(sql, [*params, max(limit * 4, 20)]).fetchall()]
        # LIKE finds "bill" inside "Billing"; whole words decide the order, and win outright when there are any.
        words = [re.compile(r"(?<![a-z0-9])" + re.escape(term) + r"(?:s|es)?(?![a-z0-9])") for term in terms]
        scored = []
        for position, (email_id, hits) in enumerate(rows):
            email = self.get_email(email_id)
            if email is None:
                continue
            head = f"{email.subject}\n{email.sender_name}\n{email.sender_email}".lower()
            rest = f"{email.summary}\n{email.body_text}".lower()
            whole = sum(4 if rx.search(head) else 1 if rx.search(rest) else 0 for rx in words)
            scored.append((whole, hits, -position, email))
        if any(item[0] for item in scored):
            scored = [item for item in scored if item[0]]
        scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        return [item[3] for item in scored[:limit]]

    def list_actions(
        self,
        *,
        status: str | None = "open",
        due_on_or_before: str | None = None,
        due_on: str | None = None,
        due_from: str | None = None,
        due_to: str | None = None,
    ) -> list[tuple[ActionItem, EmailRecord]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if status:
            clauses.append("a.status = ?")
            params.append(status)
        if due_on_or_before:
            clauses.append("a.due_date IS NOT NULL AND a.due_date <= ?")
            params.append(due_on_or_before)
        if due_on:
            clauses.append("a.due_date = ?")
            params.append(due_on)
        if due_from:
            clauses.append("a.due_date IS NOT NULL AND a.due_date >= ?")
            params.append(due_from)
        if due_to:
            clauses.append("a.due_date IS NOT NULL AND a.due_date <= ?")
            params.append(due_to)
        sql = f"""
            SELECT a.*, e.subject AS email_subject, e.sender_name, e.sender_email,
                   e.category AS email_category, e.importance AS email_importance,
                   e.received_at
            FROM action_items a
            JOIN emails e ON e.id = a.email_id
            WHERE {' AND '.join(clauses)}
            ORDER BY CASE a.priority
                WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END,
                a.due_date IS NULL, a.due_date, e.received_at DESC
        """
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        results: list[tuple[ActionItem, EmailRecord]] = []
        for row in rows:
            action = _action_from_row(row)
            stub = EmailRecord(
                id=row["email_id"],
                subject=row["email_subject"],
                sender_name=row["sender_name"],
                sender_email=row["sender_email"],
                received_at=row["received_at"],
                body_text="",
                body_preview="",
                has_attachments=False,
                outlook_importance="normal",
                is_read=True,
                category=DocumentType(row["email_category"]),
                category_confidence=0,
                importance=Importance(row["email_importance"]),
                importance_score=0,
            )
            results.append((action, stub))
        return results

    def set_action_status(self, action_id: str, status: str) -> bool:
        with self.connect() as conn:
            cur = conn.execute(
                "UPDATE action_items SET status = ? WHERE id = ?",
                (status, action_id),
            )
            return cur.rowcount > 0

    def list_attachments(self, *, document_type: str | None = None) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if document_type:
            clauses.append("a.document_type = ?")
            params.append(document_type)
        sql = f"""
            SELECT a.*, e.subject, e.sender_name, e.sender_email, e.received_at,
                   e.importance, e.category
            FROM attachments a
            JOIN emails e ON e.id = a.email_id
            WHERE {' AND '.join(clauses)}
            ORDER BY e.received_at DESC
        """
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def find_duplicate_invoices(self, invoice_number: str, exclude_email_id: str) -> list[str]:
        if not invoice_number:
            return []
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT e.id
                FROM emails e
                WHERE e.id != ?
                  AND (
                    e.extracted LIKE ?
                    OR EXISTS (
                        SELECT 1 FROM attachments a
                        WHERE a.email_id = e.id AND a.extracted_fields LIKE ?
                    )
                  )
                """,
                (
                    exclude_email_id,
                    f'%"{invoice_number}"%',
                    f'%"{invoice_number}"%',
                ),
            ).fetchall()
        return [row["id"] for row in rows]

    def counts(self) -> dict[str, int]:
        with self.connect() as conn:
            emails = conn.execute("SELECT COUNT(*) AS n FROM emails").fetchone()["n"]
            critical = conn.execute(
                "SELECT COUNT(*) AS n FROM emails WHERE importance IN ('critical','high')"
            ).fetchone()["n"]
            open_actions = conn.execute(
                "SELECT COUNT(*) AS n FROM action_items WHERE status = 'open'"
            ).fetchone()["n"]
            attachments = conn.execute("SELECT COUNT(*) AS n FROM attachments").fetchone()["n"]
            fraud = conn.execute(
                "SELECT COUNT(*) AS n FROM emails WHERE flags LIKE '%fraud_risk%'"
            ).fetchone()["n"]
            folders = {
                name: conn.execute(
                    "SELECT COUNT(*) AS n FROM emails WHERE folder = ?",
                    (name,),
                ).fetchone()["n"]
                for name in ("important", "informational", "reference")
            }
            waiting = conn.execute(
                "SELECT COUNT(*) AS n FROM emails WHERE model_status = 'script_draft'"
            ).fetchone()["n"]
            read_by_model = conn.execute(
                "SELECT COUNT(*) AS n FROM emails WHERE model_status = 'bionic'"
            ).fetchone()["n"]
        return {
            "emails": emails,
            "high_importance": critical,
            "open_actions": open_actions,
            "attachments": attachments,
            "fraud_alerts": fraud,
            "important": folders["important"],
            "informational": folders["informational"],
            "reference": folders["reference"],
            "waiting_on_bionic": waiting,
            "read_by_bionic": read_by_model,
        }

    def category_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT category, COUNT(*) AS n FROM emails GROUP BY category ORDER BY n DESC"
            ).fetchall()
        return {row["category"]: row["n"] for row in rows}

    def attachment_type_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT document_type, COUNT(*) AS n FROM attachments GROUP BY document_type ORDER BY n DESC"
            ).fetchall()
        return {row["document_type"]: row["n"] for row in rows}

    def save_digest(self, period_date: str, generated_at: str, markdown: str, html: str, payload: dict) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO digests (period_date, generated_at, markdown, html, payload)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(period_date) DO UPDATE SET
                    generated_at=excluded.generated_at,
                    markdown=excluded.markdown,
                    html=excluded.html,
                    payload=excluded.payload
                """,
                (period_date, generated_at, markdown, html, _dumps(payload)),
            )

    def get_digest(self, period_date: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM digests WHERE period_date = ?",
                (period_date,),
            ).fetchone()
        return dict(row) if row else None

    def latest_digest(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM digests ORDER BY period_date DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def list_digests(self, limit: int = 90) -> list[dict[str, Any]]:
        """Saved digests, newest first, without the rendered bodies."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT period_date, generated_at, payload FROM digests ORDER BY period_date DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for row in rows:
            payload = _loads(row["payload"], {})
            out.append(
                {
                    "period_date": row["period_date"],
                    "generated_at": row["generated_at"],
                    "headline": payload.get("headline", ""),
                    "kpis": payload.get("kpis", {}),
                    "window": payload.get("window", {}),
                }
            )
        return out

    def real_mail_count(self) -> int:
        """Messages that did not come from the built-in sample mailbox."""
        with self.connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM emails WHERE COALESCE(source, '') != 'demo'"
            ).fetchone()["n"]

    def clear_sample(self) -> int:
        """Remove the sample mailbox (and its digests) so real mail starts on a clean board."""
        sample = "SELECT id FROM emails WHERE source = 'demo'"
        with self.connect() as conn:
            removed = conn.execute("SELECT COUNT(*) AS n FROM emails WHERE source = 'demo'").fetchone()["n"]
            if not removed:
                return 0
            conn.execute(f"DELETE FROM attachments WHERE email_id IN ({sample})")
            conn.execute(f"DELETE FROM action_items WHERE email_id IN ({sample})")
            conn.execute(f"DELETE FROM corrections WHERE email_id IN ({sample})")
            conn.execute(f"DELETE FROM fraud_checks WHERE email_id IN ({sample})")
            conn.execute(f"DELETE FROM findings WHERE email_id IN ({sample})")
            conn.execute("DELETE FROM emails WHERE source = 'demo'")
            if not conn.execute("SELECT COUNT(*) AS n FROM emails").fetchone()["n"]:
                conn.execute("DELETE FROM digests")
        return removed

    def set_source_path(self, email_id: str, path: str) -> None:
        """Where the original .msg/.eml was archived. Kept apart from upsert so re-reads keep it."""
        with self.connect() as conn:
            conn.execute("UPDATE emails SET source_path = ? WHERE id = ?", (path, email_id))

    def attachment_files(self) -> list[tuple[str, str, str, str]]:
        """Every attachment's id, email id, file name and stored text."""
        with self.connect() as conn:
            rows = conn.execute("SELECT id, email_id, filename, extracted_text FROM attachments").fetchall()
        return [(row["id"], row["email_id"], row["filename"] or "", row["extracted_text"] or "") for row in rows]

    def set_attachment_text(self, attachment_id: str, text: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE attachments SET extracted_text = ? WHERE id = ?", (text, attachment_id))

    def get_state(self, key: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO sync_state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def add_correction(self, row: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO corrections (
                    id, email_id, previous_category, corrected_category, reason,
                    sender_email, subject, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["id"],
                    row["email_id"],
                    row["previous_category"],
                    row["corrected_category"],
                    row["reason"],
                    row["sender_email"],
                    row["subject"],
                    row["created_at"],
                ),
            )

    def list_corrections(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM corrections ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def latest_correction(self, *, sender_email: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM corrections
                WHERE lower(sender_email) = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (sender_email.lower(),),
            ).fetchone()
        return dict(row) if row else None

    def correction_count(self) -> int:
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM corrections").fetchone()["n"]

    # Notes the chat kept while reading an email's files -------------------------------------

    def add_finding(self, email_id: str, text: str, *, question: str = "", at: str = "") -> None:
        with self.connect() as conn:
            duplicate = conn.execute(
                "SELECT 1 FROM findings WHERE email_id = ? AND text = ?", (email_id, text)
            ).fetchone()
            if not duplicate:
                conn.execute(
                    "INSERT INTO findings(email_id, at, question, text) VALUES (?, ?, ?, ?)",
                    (email_id, at, question[:300], text[:1000]),
                )

    def findings(self, email_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM findings WHERE email_id = ? ORDER BY id DESC LIMIT ?", (email_id, limit)
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_findings(self, email_id: str) -> int:
        with self.connect() as conn:
            return conn.execute("DELETE FROM findings WHERE email_id = ?", (email_id,)).rowcount

    # File summaries written by the overnight run ------------------------------------------------

    def file_summary(self, attachment_id: str, text_key: str) -> str:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT summary FROM file_summaries WHERE attachment_id = ? AND text_key = ?", (attachment_id, text_key)
            ).fetchone()
        return row["summary"] if row else ""

    def save_file_summary(self, attachment_id: str, text_key: str, summary: str, *, model: str = "", at: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO file_summaries(attachment_id, text_key, summary, model, created_at) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(attachment_id) DO UPDATE SET text_key=excluded.text_key, summary=excluded.summary,
                    model=excluded.model, created_at=excluded.created_at
                """,
                (attachment_id, text_key, summary, model, at),
            )

    def files_to_summarize(self, *, min_chars: int, limit: int) -> list[tuple[str, str]]:
        """(email id, attachment id) of long files with no summary for their current text, most important mail first."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.email_id, a.id FROM attachments a
                JOIN emails e ON e.id = a.email_id
                LEFT JOIN file_summaries f ON f.attachment_id = a.id
                WHERE length(a.extracted_text) >= ?
                  AND (f.attachment_id IS NULL OR f.text_key != a.sha256 || ':' || length(a.extracted_text))
                ORDER BY e.importance_score DESC, e.received_at DESC
                LIMIT ?
                """,
                (min_chars, limit),
            ).fetchall()
        return [(row["email_id"], row["id"]) for row in rows]

    # Vectors for search by meaning ---------------------------------------------------------------

    def embedding_keys(self, model: str) -> dict[str, str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT key, text_key FROM embeddings WHERE model = ?", (model,)).fetchall()
        return {row["key"]: row["text_key"] for row in rows}

    def save_embeddings(self, model: str, rows: list[tuple[str, str, str, bytes]]) -> None:
        """``rows`` are (key, email id, text key, vector bytes)."""
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO embeddings(key, model, email_id, text_key, vector) VALUES (?, ?, ?, ?, ?)",
                [(key, model, email_id, text_key, vector) for key, email_id, text_key, vector in rows],
            )

    def has_embeddings(self) -> bool:
        with self.connect() as conn:
            return conn.execute("SELECT 1 FROM embeddings LIMIT 1").fetchone() is not None

    def embedding_vectors(self, model: str) -> list[tuple[str, bytes]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT email_id, vector FROM embeddings WHERE model = ?", (model,)).fetchall()
        return [(row["email_id"], row["vector"]) for row in rows]

    def embedding_version(self, model: str) -> str:
        """Changes whenever vectors for ``model`` are added or replaced."""
        with self.connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n, MAX(rowid) AS last FROM embeddings WHERE model = ?", (model,)).fetchone()
        return f"{row['n']}:{row['last']}"

    # Fraud checks ------------------------------------------------------------------------------

    def trust_entries(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM fraud_trust ORDER BY kind, value").fetchall()
        return [dict(row) for row in rows]

    def set_trust(self, kind: str, value: str, verdict: str, *, source: str, note: str = "", at: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO fraud_trust(kind, value, verdict, source, note, created_at) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(kind, value) DO UPDATE SET verdict=excluded.verdict, source=excluded.source,
                    note=excluded.note, created_at=excluded.created_at
                """,
                (kind, value.lower(), verdict, source, note, at),
            )

    def remove_trust(self, kind: str, value: str) -> bool:
        with self.connect() as conn:
            return conn.execute("DELETE FROM fraud_trust WHERE kind = ? AND value = ?", (kind, value.lower())).rowcount > 0

    def save_fraud_check(self, email_id: str, score: int, level: str, signals: list[dict], at: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO fraud_checks(email_id, score, level, signals, checked_at) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(email_id) DO UPDATE SET score=excluded.score, level=excluded.level,
                    signals=excluded.signals, checked_at=excluded.checked_at
                """,
                (email_id, score, level, _dumps(signals), at),
            )

    def fraud_check(self, email_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM fraud_checks WHERE email_id = ?", (email_id,)).fetchone()
        if not row:
            return None
        return {**dict(row), "signals": _loads(row["signals"], [])}

    def flagged(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Emails whose latest check is caution or high, highest score first."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT c.email_id, c.level, c.score, c.signals, e.subject, e.sender_name, e.sender_email, e.received_at, e.flags
                FROM fraud_checks c JOIN emails e ON e.id = c.email_id
                WHERE c.level IN ('high', 'caution')
                ORDER BY CASE c.level WHEN 'high' THEN 0 ELSE 1 END, c.score DESC, e.received_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [{**dict(row), "signals": _loads(row["signals"], []), "flags": _loads(row["flags"], [])} for row in rows]

    def log_fraud(self, row: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO fraud_log(at, email_id, event, level, score, sender_email, subject, signals, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("at", ""),
                    row.get("email_id", ""),
                    row["event"],
                    row.get("level", ""),
                    row.get("score", 0),
                    row.get("sender_email", ""),
                    row.get("subject", ""),
                    _dumps(row.get("signals", [])),
                    row.get("note", ""),
                ),
            )

    def fraud_log(self, *, limit: int = 200, events: tuple[str, ...] = ()) -> list[dict[str, Any]]:
        sql = "SELECT * FROM fraud_log"
        params: list[Any] = []
        if events:
            sql += f" WHERE event IN ({', '.join('?' for _ in events)})"
            params.extend(events)
        sql += " ORDER BY id DESC LIMIT ?"
        with self.connect() as conn:
            rows = conn.execute(sql, [*params, limit]).fetchall()
        return [{**dict(row), "signals": _loads(row["signals"], [])} for row in rows]

    def sender_history(self, sender_email: str, *, exclude: str = "") -> int:
        with self.connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM emails WHERE lower(sender_email) = ? AND id != ?",
                ((sender_email or "").lower(), exclude),
            ).fetchone()["n"]

    def email_ids_from(self, *, sender: str = "", domain: str = "") -> list[str]:
        """Emails from one address, or from a domain and its subdomains."""
        with self.connect() as conn:
            if sender:
                rows = conn.execute("SELECT id FROM emails WHERE lower(sender_email) = ?", (sender.lower(),)).fetchall()
            elif domain:
                domain = domain.lower()
                rows = conn.execute(
                    "SELECT id FROM emails WHERE lower(sender_email) LIKE ? OR lower(sender_email) LIKE ?",
                    (f"%@{domain}", f"%.{domain}"),
                ).fetchall()
            else:
                return []
        return [row["id"] for row in rows]

    def sender_domains(self, *, limit: int = 12) -> list[tuple[str, int]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT lower(substr(sender_email, instr(sender_email, '@') + 1)) AS domain, COUNT(*) AS n
                FROM emails WHERE instr(sender_email, '@') > 0 AND COALESCE(source, '') != 'demo'
                GROUP BY domain ORDER BY n DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [(row["domain"], row["n"]) for row in rows]

    def names_at_domains(self, domains: set[str]) -> dict[str, str]:
        """Display names seen from trusted domains, so a stranger borrowing one stands out."""
        if not domains:
            return {}
        with self.connect() as conn:
            rows = conn.execute("SELECT DISTINCT sender_name, sender_email FROM emails WHERE sender_name != ''").fetchall()
        out = {}
        for row in rows:
            address = (row["sender_email"] or "").lower()
            if address.rsplit("@", 1)[-1] in domains:
                out[(row["sender_name"] or "").strip().lower()] = address
        return out


def _action_from_row(row: sqlite3.Row) -> ActionItem:
    return ActionItem(
        id=row["id"],
        email_id=row["email_id"],
        title=row["title"],
        detail=row["detail"] or "",
        due_date=row["due_date"],
        priority=Importance(row["priority"]),
        status=ActionStatus(row["status"]),
        source=row["source"] or "body",
        created_at=row["created_at"] or "",
    )


def _email_from_rows(
    row: sqlite3.Row,
    attachment_rows: list[sqlite3.Row],
    action_rows: list[sqlite3.Row],
) -> EmailRecord:
    attachments = [
        AttachmentRecord(
            id=item["id"],
            email_id=item["email_id"],
            filename=item["filename"],
            content_type=item["content_type"],
            size_bytes=item["size_bytes"] or 0,
            sha256=item["sha256"],
            extracted_text=item["extracted_text"] or "",
            document_type=DocumentType(item["document_type"]),
            document_confidence=item["document_confidence"] or 0,
            extracted_fields=ExtractedFields.from_dict(_loads(item["extracted_fields"], {})),
            classification_reasons=_loads(item["classification_reasons"], []),
        )
        for item in attachment_rows
    ]
    return EmailRecord(
        id=row["id"],
        subject=row["subject"],
        sender_name=row["sender_name"] or "",
        sender_email=row["sender_email"] or "",
        received_at=row["received_at"],
        body_text=row["body_text"] or "",
        body_preview=row["body_preview"] or "",
        has_attachments=bool(row["has_attachments"]),
        outlook_importance=row["outlook_importance"] or "normal",
        is_read=bool(row["is_read"]),
        category=DocumentType(row["category"]),
        category_confidence=row["category_confidence"] or 0,
        importance=Importance(row["importance"]),
        importance_score=row["importance_score"] or 0,
        importance_reasons=_loads(row["importance_reasons"], []),
        flags=_loads(row["flags"], []),
        extracted=ExtractedFields.from_dict(_loads(row["extracted"], {})),
        source=row["source"] or "graph",
        conversation_id=row["conversation_id"] or "",
        internet_message_id=row["internet_message_id"] or "",
        writeback_status=row["writeback_status"] or "skipped",
        created_at=row["created_at"] or datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        folder=_col(row, "folder", ""),
        summary=_col(row, "summary", ""),
        model_status=_col(row, "model_status", "script_draft") or "script_draft",
        source_path=_col(row, "source_path", ""),
        reply_to=_col(row, "reply_to", "") or "",
        attachments=attachments,
        actions=[_action_from_row(item) for item in action_rows],
    )


_LIKE = "LIKE ? ESCAPE '\\'"
_MATCH_ANY = (
    f"(subject {_LIKE} OR sender_email {_LIKE} OR sender_name {_LIKE} OR summary {_LIKE} OR body_text {_LIKE}"
    " OR EXISTS (SELECT 1 FROM attachments a WHERE a.email_id = emails.id"
    f" AND (a.filename {_LIKE} OR a.extracted_text {_LIKE})))"
)


def _contains(word: str) -> str:
    """A LIKE pattern that treats % and _ in what was typed as plain characters."""
    return "%" + re.sub(r"([\\%_])", r"\\\1", word) + "%"


def _col(row: sqlite3.Row, name: str, default: Any) -> Any:
    if name not in row.keys() or row[name] is None:
        return default
    return row[name]


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(emails)")}
    if "folder" not in cols:
        conn.execute("ALTER TABLE emails ADD COLUMN folder TEXT DEFAULT ''")
    if "summary" not in cols:
        conn.execute("ALTER TABLE emails ADD COLUMN summary TEXT DEFAULT ''")
    if "model_status" not in cols:
        conn.execute("ALTER TABLE emails ADD COLUMN model_status TEXT DEFAULT 'script_draft'")
    if "source_path" not in cols:
        conn.execute("ALTER TABLE emails ADD COLUMN source_path TEXT DEFAULT ''")
        # Databases from before profiles existed were all finance boards; keep them that way.
        real = conn.execute("SELECT COUNT(*) FROM emails WHERE COALESCE(source, '') != 'demo'").fetchone()[0]
        if real:
            conn.execute("INSERT OR IGNORE INTO sync_state(key, value) VALUES ('profile', 'finance')")
    if "reply_to" not in cols:
        conn.execute("ALTER TABLE emails ADD COLUMN reply_to TEXT DEFAULT ''")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_folder ON emails(folder)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_model ON emails(model_status)")
