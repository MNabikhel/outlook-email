from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
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
    TriageBin,
)


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
    triage_bin TEXT,
    summary TEXT,
    highlights TEXT,
    ai_source TEXT,
    source TEXT,
    conversation_id TEXT,
    internet_message_id TEXT,
    writeback_status TEXT,
    created_at TEXT
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

CREATE INDEX IF NOT EXISTS idx_emails_received ON emails(received_at);
CREATE INDEX IF NOT EXISTS idx_emails_importance ON emails(importance);
CREATE INDEX IF NOT EXISTS idx_emails_category ON emails(category);
CREATE INDEX IF NOT EXISTS idx_emails_bin ON emails(triage_bin);
CREATE INDEX IF NOT EXISTS idx_actions_status ON action_items(status);
CREATE INDEX IF NOT EXISTS idx_actions_due ON action_items(due_date);
CREATE INDEX IF NOT EXISTS idx_att_hash ON attachments(sha256);
"""

# Columns added after the first release. SQLite has no "ADD COLUMN IF NOT EXISTS",
# so we diff against PRAGMA table_info and add what is missing. This keeps older
# on-disk databases working without a reset.
_EMAIL_MIGRATIONS = {
    "triage_bin": "ALTER TABLE emails ADD COLUMN triage_bin TEXT",
    "summary": "ALTER TABLE emails ADD COLUMN summary TEXT",
    "highlights": "ALTER TABLE emails ADD COLUMN highlights TEXT",
    "ai_source": "ALTER TABLE emails ADD COLUMN ai_source TEXT",
}


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
            self._migrate(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(emails)")}
        for column, ddl in _EMAIL_MIGRATIONS.items():
            if column not in existing:
                conn.execute(ddl)

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
                    importance_reasons, flags, extracted, triage_bin, summary,
                    highlights, ai_source, source, conversation_id,
                    internet_message_id, writeback_status, created_at
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
                    triage_bin=excluded.triage_bin,
                    summary=excluded.summary,
                    highlights=excluded.highlights,
                    ai_source=excluded.ai_source,
                    source=excluded.source,
                    conversation_id=excluded.conversation_id,
                    internet_message_id=excluded.internet_message_id,
                    writeback_status=excluded.writeback_status
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
                    email.triage_bin.value,
                    email.summary,
                    _dumps(email.highlights),
                    email.ai_source,
                    email.source,
                    email.conversation_id,
                    email.internet_message_id,
                    email.writeback_status,
                    email.created_at,
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
            existing_open = {
                row["title"]
                for row in conn.execute(
                    "SELECT title FROM action_items WHERE email_id = ? AND status != 'done'",
                    (email.id,),
                )
            }
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
                if action.title in existing_open and action.id:
                    pass
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
        triage_bin: str | None = None,
        flag: str | None = None,
        q: str | None = None,
        limit: int = 200,
    ) -> list[EmailRecord]:
        clauses = ["1=1"]
        params: list[Any] = []
        if importance:
            clauses.append("importance = ?")
            params.append(importance)
        if category:
            clauses.append("category = ?")
            params.append(category)
        if triage_bin:
            clauses.append("triage_bin = ?")
            params.append(triage_bin)
        if q:
            clauses.append(
                "(subject LIKE ? OR sender_email LIKE ? OR sender_name LIKE ? OR body_preview LIKE ?)"
            )
            like = f"%{q}%"
            params.extend([like, like, like, like])
        sql = f"SELECT * FROM emails WHERE {' AND '.join(clauses)} ORDER BY received_at DESC LIMIT ?"
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
        return {
            "emails": emails,
            "high_importance": critical,
            "open_actions": open_actions,
            "attachments": attachments,
            "fraud_alerts": fraud,
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

    def bin_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT triage_bin, COUNT(*) AS n FROM emails GROUP BY triage_bin"
            ).fetchall()
        return {(row["triage_bin"] or "fyi"): row["n"] for row in rows}

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

    def list_digests(self, *, limit: int = 60) -> list[dict[str, Any]]:
        """History of stored digests (newest first) with a few headline KPIs."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT period_date, generated_at, payload FROM digests "
                "ORDER BY period_date DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            payload = _loads(row["payload"], {}) if row["payload"] else {}
            kpis = payload.get("kpis", {}) if isinstance(payload, dict) else {}
            out.append(
                {
                    "period_date": row["period_date"],
                    "generated_at": row["generated_at"],
                    "kpis": kpis,
                }
            )
        return out

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


def _row_get(row: sqlite3.Row, key: str) -> Any:
    """sqlite3.Row has no .get(); some callers build rows without newer columns."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _coerce_stored_bin(value: Any) -> TriageBin:
    if not value:
        return TriageBin.FYI
    try:
        return TriageBin(value)
    except ValueError:
        return TriageBin.FYI


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
        triage_bin=_coerce_stored_bin(_row_get(row, "triage_bin")),
        summary=_row_get(row, "summary") or "",
        highlights=_loads(_row_get(row, "highlights"), []),
        ai_source=_row_get(row, "ai_source") or "rules",
        source=row["source"] or "graph",
        conversation_id=row["conversation_id"] or "",
        internet_message_id=row["internet_message_id"] or "",
        writeback_status=row["writeback_status"] or "skipped",
        created_at=row["created_at"] or datetime.utcnow().isoformat(),
        attachments=attachments,
        actions=[_action_from_row(item) for item in action_rows],
    )
