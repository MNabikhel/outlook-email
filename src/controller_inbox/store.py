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
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    title TEXT DEFAULT '',
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS chat_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT DEFAULT '',
    data TEXT DEFAULT '{}',
    at TEXT
);

CREATE INDEX IF NOT EXISTS idx_chat_turns ON chat_turns(chat_id);

CREATE TABLE IF NOT EXISTS chat_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT DEFAULT '',
    size_bytes INTEGER DEFAULT 0,
    sha256 TEXT DEFAULT '',
    text TEXT DEFAULT '',
    added_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_chat_files ON chat_files(chat_id);

CREATE TABLE IF NOT EXISTS cost_codings (
    email_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'unmatched',
    codes TEXT DEFAULT '[]',
    others TEXT DEFAULT '[]',
    unlisted TEXT DEFAULT '[]',
    signature TEXT DEFAULT '',
    decided_at TEXT DEFAULT '',
    checked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_cost_codings_status ON cost_codings(status);

-- A page the vision model read, beside the reading CloseDesk had of it (OCR's, or the PDF's own text): both are
-- kept, and the page is shown as whichever holds up best (see vision.py).
CREATE TABLE IF NOT EXISTS page_readings (
    attachment_id TEXT NOT NULL,
    page INTEGER NOT NULL,
    first TEXT NOT NULL,
    model_text TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    seconds REAL NOT NULL DEFAULT 0,
    comparison TEXT NOT NULL DEFAULT '{}',
    -- The file the page was read from: a reading never shows on another file that took the same name.
    sha256 TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (attachment_id, page)
);

-- A page the vision model couldn't read (the server failed or timed out): after a second failure on the same
-- file it is left for the user to ask for, so one bad page doesn't stop every overnight run.
CREATE TABLE IF NOT EXISTS page_failures (
    attachment_id TEXT NOT NULL,
    page INTEGER NOT NULL,
    sha256 TEXT NOT NULL DEFAULT '',
    tries INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (attachment_id, page)
);
CREATE INDEX IF NOT EXISTS idx_emails_received ON emails(received_at);
CREATE INDEX IF NOT EXISTS idx_emails_importance ON emails(importance);
CREATE INDEX IF NOT EXISTS idx_emails_category ON emails(category);
CREATE INDEX IF NOT EXISTS idx_actions_status ON action_items(status);
CREATE INDEX IF NOT EXISTS idx_actions_due ON action_items(due_date);
CREATE INDEX IF NOT EXISTS idx_att_hash ON attachments(sha256);
"""


# Setup choices kept in sync_state. Loading the sample mailbox keeps these; everything else there is mail bookkeeping.
SETTING_KEYS = frozenset({"timezone", "profile", "min_context_tokens", "vision_mode", "vision_model"})

BUSY_TIMEOUT_SECONDS = 30.0


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _text(value: str | None) -> str:
    """Attachment text as stored: without NUL characters, where SQLite's length() would stop counting."""
    return (value or "").replace("\x00", "")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _init(self) -> None:
        with self.connect() as conn:
            # Readers (the dashboard) never wait on the writer (a background run), and the mode sticks to the file.
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError:
                pass  # a filesystem without shared memory keeps the default journal
            conn.executescript(SCHEMA)
            _migrate(conn)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        # Another process (the scheduled run, the dashboard) may be writing; wait for it instead of failing.
        conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_SECONDS * 1000)}")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def clear_mail(self) -> int:
        """Remove every email and what was worked out from it, for loading the sample mailbox.

        Kept: Setup choices (time zone, profile, context size), the fraud trust list, and Ask CloseDesk
        conversations with their files. Mail bookkeeping (sync cursors, last run) starts over.
        """
        mail = "SELECT id FROM emails"
        with self.connect() as conn:
            removed = conn.execute("SELECT COUNT(*) AS n FROM emails").fetchone()["n"]
            conn.execute(f"DELETE FROM file_summaries WHERE attachment_id IN (SELECT id FROM attachments WHERE email_id IN ({mail}))")
            conn.execute(f"DELETE FROM page_readings WHERE attachment_id IN (SELECT id FROM attachments WHERE email_id IN ({mail}))")
            conn.execute(f"DELETE FROM page_failures WHERE attachment_id IN (SELECT id FROM attachments WHERE email_id IN ({mail}))")
            conn.execute(f"DELETE FROM embeddings WHERE email_id IN ({mail})")
            conn.execute(f"DELETE FROM fraud_log WHERE email_id IN ({mail})")
            conn.execute(f"DELETE FROM findings WHERE email_id IN ({mail})")
            for table in ("attachments", "action_items", "corrections", "fraud_checks", "cost_codings"):
                conn.execute(f"DELETE FROM {table} WHERE email_id IN ({mail})")
            conn.execute("DELETE FROM emails")
            conn.execute("DELETE FROM digests")
            keep = ", ".join("?" for _ in SETTING_KEYS)
            conn.execute(f"DELETE FROM sync_state WHERE key NOT IN ({keep})", sorted(SETTING_KEYS))
        return removed

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
                        _text(att.extracted_text),
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
            readings = _readings_for(conn, [item["id"] for item in attachments])
        return _email_from_rows(row, attachments, actions, readings)

    def list_emails(
        self,
        *,
        importance: str | None = None,
        category: str | None = None,
        flag: str | None = None,
        q: str | None = None,
        folder: str | None = None,
        model_status: str | None = None,
        done: bool | None = None,
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
        if done is not None:
            clauses.append("COALESCE(done_at, '') != ''" if done else "COALESCE(done_at, '') = ''")
        if model_status:
            clauses.append("model_status = ?")
            params.append(model_status)
        if flag:
            # Flags are a JSON list of strings; match the quoted string so "fraud" doesn't match "fraud_risk".
            clauses.append(f"flags {_LIKE}")
            params.append(_json_contains(flag))
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
                # In the order get_email gives them: the overnight run compares an email listed here with the
                # same email read again by get_email, and files in another order would never match.
                attachments = conn.execute(
                    "SELECT * FROM attachments WHERE email_id = ? ORDER BY filename",
                    (row["id"],),
                ).fetchall()
                actions = conn.execute(
                    "SELECT * FROM action_items WHERE email_id = ?",
                    (row["id"],),
                ).fetchall()
                readings = _readings_for(conn, [item["id"] for item in attachments]) if attachments else {}
                email = _email_from_rows(row, attachments, actions, readings)
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
                    e.extracted LIKE ? ESCAPE '\\'
                    OR EXISTS (
                        SELECT 1 FROM attachments a
                        WHERE a.email_id = e.id AND a.extracted_fields LIKE ? ESCAPE '\\'
                    )
                  )
                """,
                (
                    exclude_email_id,
                    _json_contains(invoice_number),
                    _json_contains(invoice_number),
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
                    "SELECT COUNT(*) AS n FROM emails WHERE folder = ? AND COALESCE(done_at, '') = ''",
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

    def ap_invoice_ids(self) -> list[str]:
        """Emails filed as AP invoices or carrying an AP invoice, and any that were coded before."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id FROM emails WHERE category = 'ap_invoice'
                   OR EXISTS (SELECT 1 FROM attachments a WHERE a.email_id = emails.id AND a.document_type = 'ap_invoice')
                UNION SELECT email_id FROM cost_codings
                """
            ).fetchall()
        return [row[0] for row in rows]

    def cost_coding(self, email_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM cost_codings WHERE email_id = ?", (email_id,)).fetchone()
        return _coding_from_row(row) if row else None

    def save_cost_coding(
        self,
        email_id: str,
        *,
        status: str,
        codes: list[dict],
        others: list[dict] | None = None,
        unlisted: list[str] | None = None,
        signature: str = "",
        decided_at: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO cost_codings(email_id, status, codes, others, unlisted, signature, decided_at, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(email_id) DO UPDATE SET status=excluded.status, codes=excluded.codes,
                    others=excluded.others, unlisted=excluded.unlisted, signature=excluded.signature,
                    decided_at=excluded.decided_at, checked_at=excluded.checked_at
                """,
                (email_id, status, _dumps(codes), _dumps(others or []), _dumps(unlisted or []), signature, decided_at, _now()),
            )

    def delete_cost_coding(self, email_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM cost_codings WHERE email_id = ?", (email_id,))

    def cost_codings(self, *, status: str | None = None, q: str | None = None, limit: int = 300) -> list[dict[str, Any]]:
        """AP invoices with their coding, newest first, joined with what the page shows of the email."""
        clauses, params = ["1=1"], []
        if status == "review":
            clauses.append("c.status != 'confirmed'")
        elif status:
            clauses.append("c.status = ?")
            params.append(status)
        for word in (q or "").split()[:8]:
            like = _contains(word)
            clauses.append(
                f"(c.codes {_LIKE} OR e.subject {_LIKE} OR e.sender_name {_LIKE} OR e.sender_email {_LIKE} OR e.extracted {_LIKE})"
            )
            params.extend([like] * 5)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT c.*, e.subject, e.sender_name, e.sender_email, e.received_at, e.extracted
                FROM cost_codings c JOIN emails e ON e.id = c.email_id
                WHERE {' AND '.join(clauses)} ORDER BY e.received_at DESC LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        out = []
        for row in rows:
            item = _coding_from_row(row)
            item.update(
                subject=row["subject"],
                sender_name=row["sender_name"] or "",
                sender_email=row["sender_email"] or "",
                received_at=row["received_at"],
                extracted=ExtractedFields.from_dict(_loads(row["extracted"], {})),
            )
            out.append(item)
        return out

    def coding_counts(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM cost_codings GROUP BY status").fetchall()
        counts = {"suggested": 0, "unmatched": 0, "confirmed": 0, **{row["status"]: row["n"] for row in rows}}
        counts["review"] = counts["suggested"] + counts["unmatched"]
        counts["all"] = counts["review"] + counts["confirmed"]
        return counts

    def last_confirmed_codes(self, sender_email: str, *, exclude: str = "") -> list[dict]:
        """The codes most recently confirmed for another invoice from this sender."""
        if not sender_email:
            return []
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT c.codes FROM cost_codings c JOIN emails e ON e.id = c.email_id
                WHERE c.status = 'confirmed' AND lower(e.sender_email) = ? AND c.email_id != ?
                ORDER BY c.decided_at DESC LIMIT 1
                """,
                (sender_email.lower(), exclude),
            ).fetchone()
        return _loads(row["codes"], []) if row else []

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
            conn.execute(
                f"DELETE FROM file_summaries WHERE attachment_id IN (SELECT id FROM attachments WHERE email_id IN ({sample}))"
            )
            conn.execute(f"DELETE FROM embeddings WHERE email_id IN ({sample})")
            conn.execute(f"DELETE FROM fraud_log WHERE email_id IN ({sample})")
            conn.execute(f"DELETE FROM page_readings WHERE attachment_id IN (SELECT id FROM attachments WHERE email_id IN ({sample}))")
            conn.execute(f"DELETE FROM page_failures WHERE attachment_id IN (SELECT id FROM attachments WHERE email_id IN ({sample}))")
            conn.execute(f"DELETE FROM attachments WHERE email_id IN ({sample})")
            conn.execute(f"DELETE FROM action_items WHERE email_id IN ({sample})")
            conn.execute(f"DELETE FROM corrections WHERE email_id IN ({sample})")
            conn.execute(f"DELETE FROM fraud_checks WHERE email_id IN ({sample})")
            conn.execute(f"DELETE FROM findings WHERE email_id IN ({sample})")
            conn.execute(f"DELETE FROM cost_codings WHERE email_id IN ({sample})")
            conn.execute("DELETE FROM emails WHERE source = 'demo'")
            if not conn.execute("SELECT COUNT(*) AS n FROM emails").fetchone()["n"]:
                conn.execute("DELETE FROM digests")
        return removed

    def set_source_path(self, email_id: str, path: str) -> None:
        """Where the original .msg/.eml was archived. Kept apart from upsert so re-reads keep it."""
        with self.connect() as conn:
            conn.execute("UPDATE emails SET source_path = ? WHERE id = ?", (path, email_id))

    def attachment_files(self) -> list[tuple[str, str, str, str, str]]:
        """Every attachment's id, email id, file name, SHA-256 and stored text, one email's files together."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, email_id, filename, sha256, extracted_text FROM attachments ORDER BY email_id"
            ).fetchall()
        return [
            (row["id"], row["email_id"], row["filename"] or "", row["sha256"] or "", row["extracted_text"] or "")
            for row in rows
        ]

    def set_attachment_text(self, attachment_id: str, text: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE attachments SET extracted_text = ? WHERE id = ?", (_text(text), attachment_id))

    # Pages the vision model read (vision.py) ------------------------------------------------------

    def save_page_reading(
        self,
        attachment_id: str,
        page: int,
        *,
        first: str,
        model_text: str,
        model: str,
        seconds: float,
        comparison: str,
        sha256: str = "",
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO page_readings (attachment_id, page, first, model_text, model, seconds, comparison, sha256, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(attachment_id, page) DO UPDATE SET
                    first=excluded.first, model_text=excluded.model_text, model=excluded.model, seconds=excluded.seconds,
                    comparison=excluded.comparison, sha256=excluded.sha256, created_at=excluded.created_at
                """,
                (attachment_id, page, _text(first), _text(model_text), model, float(seconds), comparison, sha256 or "", _now()),
            )
            conn.execute("DELETE FROM page_failures WHERE attachment_id = ? AND page = ?", (attachment_id, page))

    def note_page_failure(self, attachment_id: str, page: int, *, sha256: str, error: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO page_failures (attachment_id, page, sha256, tries, error, updated_at) VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(attachment_id, page) DO UPDATE SET
                    tries = CASE WHEN page_failures.sha256 = excluded.sha256 THEN page_failures.tries + 1 ELSE 1 END,
                    sha256 = excluded.sha256, error = excluded.error, updated_at = excluded.updated_at
                """,
                (attachment_id, page, sha256 or "", error[:300], _now()),
            )

    def page_failures(self, attachment_id: str, sha256: str = "") -> dict[int, int]:
        """Page -> how many times in a row the vision model failed to read it (this file's, when sha256 is given)."""
        with self.connect() as conn:
            rows = conn.execute("SELECT page, sha256, tries FROM page_failures WHERE attachment_id = ?", (attachment_id,)).fetchall()
        return {int(row["page"]): int(row["tries"]) for row in rows if not sha256 or row["sha256"] in {"", sha256}}

    def stored_text(self, attachment_id: str) -> str | None:
        """The attachment's text as stored (an email's file, or a file added to a conversation: "chat-<id>:<name>"),
        before any vision reading is shown in it."""
        with self.connect() as conn:
            row = conn.execute("SELECT extracted_text FROM attachments WHERE id = ?", (attachment_id,)).fetchone()
            if row is not None:
                return row["extracted_text"] or ""
            if attachment_id.startswith("chat-") and ":" in attachment_id:
                chat_id, _, filename = attachment_id.removeprefix("chat-").partition(":")
                row = conn.execute("SELECT text FROM chat_files WHERE chat_id = ? AND filename = ?", (chat_id, filename)).fetchone()
                if row is not None:
                    return row["text"] or ""
        return None

    def page_readings(self, attachment_id: str, sha256: str = "") -> dict[int, dict]:
        """Page -> the stored reading (first, model_text, model, seconds, comparison, sha256, created_at), of the
        file with this SHA-256 when one is given."""
        with self.connect() as conn:
            rows = _readings_for(conn, [attachment_id]).get(attachment_id, {})
        return _same_file(rows, sha256)

    def chat_readings(self, chat_id: str) -> dict[str, dict[int, dict]]:
        """Attachment id -> page -> reading, for a conversation's files."""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM page_readings WHERE attachment_id {_LIKE} ORDER BY page", (_like_escape(f"chat-{chat_id}:") + "%",)
            ).fetchall()
        out: dict[str, dict[int, dict]] = {}
        for row in rows:
            out.setdefault(row["attachment_id"], {})[int(row["page"])] = {key: row[key] for key in row.keys()}
        return out

    def vision_seconds(self, model: str, *, limit: int = 12) -> list[float]:
        """How long the latest pages took this model to read, newest first."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT seconds FROM page_readings WHERE model = ? AND seconds > 0 ORDER BY created_at DESC LIMIT ?",
                (model, limit),
            ).fetchall()
        return [float(row["seconds"]) for row in rows]

    def vision_pages_read(self) -> int:
        with self.connect() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM page_readings").fetchone()["n"]

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

    def set_done(self, email_id: str, done: bool) -> None:
        """Done: handled, so it leaves its folder's list (it stays in All mail and search)."""
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds") if done else ""
        with self.connect() as conn:
            conn.execute("UPDATE emails SET done_at = ? WHERE id = ?", (stamp, email_id))

    def is_done(self, email_id: str) -> bool:
        with self.connect() as conn:
            row = conn.execute("SELECT COALESCE(done_at, '') AS d FROM emails WHERE id = ?", (email_id,)).fetchone()
        return bool(row and row["d"])

    def done_ids(self) -> set[str]:
        """Every email marked done."""
        with self.connect() as conn:
            rows = conn.execute("SELECT id FROM emails WHERE COALESCE(done_at, '') != ''").fetchall()
        return {row["id"] for row in rows}

    def done_count(self, folder: str) -> int:
        with self.connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) AS n FROM emails WHERE folder = ? AND COALESCE(done_at, '') != ''", (folder,)
            ).fetchone()["n"]

    def findings(self, email_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM findings WHERE email_id = ? ORDER BY id DESC LIMIT ?", (email_id, limit)
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_findings(self, email_id: str) -> int:
        with self.connect() as conn:
            return conn.execute("DELETE FROM findings WHERE email_id = ?", (email_id,)).rowcount

    # Ask CloseDesk conversations ---------------------------------------------------------------

    def create_chat(self, chat_id: str, title: str = "") -> None:
        now = _now()
        with self.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO chats(id, title, created_at, updated_at) VALUES (?, ?, ?, ?)", (chat_id, title, now, now))

    def chat(self, chat_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return dict(row) if row else None

    def list_chats(self, query: str = "", *, limit: int = 100) -> list[dict[str, Any]]:
        """Conversations with something in them, newest first; ``query`` matches titles and what was said."""
        query = query.strip()
        like = _contains(query)
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT c.*, (SELECT COUNT(*) FROM chat_turns t WHERE t.chat_id = c.id AND t.role = 'user') AS questions,
                       (SELECT COUNT(*) FROM chat_files f WHERE f.chat_id = c.id) AS files
                FROM chats c
                WHERE (EXISTS (SELECT 1 FROM chat_turns t WHERE t.chat_id = c.id) OR EXISTS (SELECT 1 FROM chat_files f WHERE f.chat_id = c.id))
                  AND (? = '' OR c.title {_LIKE} OR EXISTS (SELECT 1 FROM chat_turns t WHERE t.chat_id = c.id AND t.text {_LIKE}))
                ORDER BY c.updated_at DESC, c.rowid DESC LIMIT ?
                """,
                (query, like, like, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def touch_chat(self, chat_id: str, *, title: str | None = None) -> None:
        with self.connect() as conn:
            if title is not None:
                conn.execute("UPDATE chats SET updated_at = ?, title = ? WHERE id = ?", (_now(), title, chat_id))
            else:
                conn.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (_now(), chat_id))

    def add_chat_turn(self, chat_id: str, role: str, text: str, data: dict[str, Any] | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO chat_turns(chat_id, role, text, data, at) VALUES (?, ?, ?, ?, ?)",
                (chat_id, role, text, _dumps(data or {}), _now()),
            )
            conn.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (_now(), chat_id))

    def chat_turns(self, chat_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT role, text, data, at FROM chat_turns WHERE chat_id = ? ORDER BY id", (chat_id,)).fetchall()
        return [{**_loads(row["data"], {}), "role": row["role"], "text": row["text"], "at": row["at"]} for row in rows]

    def past_exchanges(self, *, exclude: str = "", limit: int = 2_000) -> list[dict[str, Any]]:
        """Question-and-answer pairs from other conversations, newest first. Only finished answers from the
        model count: failed, stopped and plain-lookup replies are not worth learning from."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT t.chat_id, t.role, t.text, t.data, t.at, c.title FROM chat_turns t JOIN chats c ON c.id = t.chat_id
                WHERE t.chat_id != ? ORDER BY t.id DESC LIMIT ?
                """,
                (exclude, limit),
            ).fetchall()
        pairs: list[dict[str, Any]] = []
        answer: dict[str, Any] | None = None
        for row in rows:
            if row["role"] == "assistant":
                data = _loads(row["data"], {})
                answer = dict(row) if data.get("mode") == "model" and not data.get("failed") else None
            elif answer is not None and answer["chat_id"] == row["chat_id"]:
                pairs.append({"chat_id": row["chat_id"], "title": row["title"], "question": row["text"], "answer": answer["text"], "at": row["at"]})
                answer = None
        return pairs

    def delete_chat(self, chat_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM chat_turns WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM chat_files WHERE chat_id = ?", (chat_id,))
            conn.execute(f"DELETE FROM page_readings WHERE attachment_id {_LIKE}", (_like_escape(f"chat-{chat_id}:") + "%",))
            conn.execute(f"DELETE FROM page_failures WHERE attachment_id {_LIKE}", (_like_escape(f"chat-{chat_id}:") + "%",))
            conn.execute("DELETE FROM findings WHERE email_id = ?", (f"chat-{chat_id}",))
            conn.execute("DELETE FROM chats WHERE id = ?", (chat_id,))

    def add_chat_file(self, chat_id: str, row: dict[str, Any]) -> None:
        """Adds a file to the conversation, replacing one with the same name."""
        with self.connect() as conn:
            conn.execute("DELETE FROM chat_files WHERE chat_id = ? AND filename = ?", (chat_id, row["filename"]))
            conn.execute("DELETE FROM page_readings WHERE attachment_id = ?", (f"chat-{chat_id}:{row['filename']}",))
            conn.execute("DELETE FROM page_failures WHERE attachment_id = ?", (f"chat-{chat_id}:{row['filename']}",))
            conn.execute(
                "INSERT INTO chat_files(chat_id, filename, content_type, size_bytes, sha256, text, added_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chat_id, row["filename"], row.get("content_type", ""), row.get("size_bytes", 0), row.get("sha256", ""), row.get("text", ""), _now()),
            )
            conn.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (_now(), chat_id))

    def chat_files(self, chat_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM chat_files WHERE chat_id = ? ORDER BY id", (chat_id,)).fetchall()
        return [dict(row) for row in rows]

    def remove_chat_file(self, chat_id: str, filename: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM chat_files WHERE chat_id = ? AND filename = ?", (chat_id, filename))
            conn.execute("DELETE FROM page_readings WHERE attachment_id = ?", (f"chat-{chat_id}:{filename}",))
            conn.execute("DELETE FROM page_failures WHERE attachment_id = ?", (f"chat-{chat_id}:{filename}",))

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

    def files_to_summarize(self, *, min_chars: int, limit: int, model: str = "") -> list[tuple[str, str]]:
        """(email id, attachment id) of long files with no summary for their current text, most important mail first.

        Files on mail suspected of fraud (as ``fraud.attachments_locked`` decides) are left out here, so they
        never use up the night's quota. A file the model gave no usable summary for is tried again only when
        another model is loaded.
        """
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT a.email_id, a.id, e.importance_score, e.received_at FROM attachments a
                JOIN emails e ON e.id = a.email_id
                LEFT JOIN file_summaries f ON f.attachment_id = a.id
                WHERE length(a.extracted_text) >= ?
                  AND (e.flags {_LIKE} OR (e.flags NOT {_LIKE} AND e.category != 'payment_instruction_change'))
                  AND NOT EXISTS (SELECT 1 FROM page_readings p WHERE p.attachment_id = a.id)
                  AND (f.attachment_id IS NULL OR f.text_key != a.sha256 || ':' || length(a.extracted_text)
                       OR (f.summary = '' AND COALESCE(f.model, '') != ?))
                ORDER BY e.importance_score DESC, e.received_at DESC
                LIMIT ?
                """,
                (min_chars, _json_contains("fraud_cleared"), _json_contains("fraud_risk"), model, limit),
            ).fetchall()
            # A file with pages the vision model read is summarized from its text as shown (agent.summary_key), so
            # its key is worked out here rather than from the stored text's length.
            seen = conn.execute(
                f"""
                SELECT a.email_id, a.id, a.sha256, a.extracted_text, f.text_key, f.summary, f.model, e.importance_score, e.received_at
                FROM attachments a
                JOIN emails e ON e.id = a.email_id
                LEFT JOIN file_summaries f ON f.attachment_id = a.id
                WHERE EXISTS (SELECT 1 FROM page_readings p WHERE p.attachment_id = a.id)
                  AND (e.flags {_LIKE} OR (e.flags NOT {_LIKE} AND e.category != 'payment_instruction_change'))
                """,
                (_json_contains("fraud_cleared"), _json_contains("fraud_risk")),
            ).fetchall()
            readings = _readings_for(conn, [row["id"] for row in seen])
        found = [(row["importance_score"], row["received_at"] or "", row["email_id"], row["id"]) for row in rows]
        for row in seen:
            text = shown_text(row["extracted_text"] or "", readings.get(row["id"]), row["sha256"] or "")
            key = f"{row['sha256']}:{len(text)}"
            if len(text) >= min_chars and (row["text_key"] is None or row["text_key"] != key or (row["summary"] == "" and (row["model"] or "") != model)):
                found.append((row["importance_score"], row["received_at"] or "", row["email_id"], row["id"]))
        found.sort(key=lambda item: (item[0] or 0, item[1]), reverse=True)
        return [(email_id, attachment_id) for _score, _when, email_id, attachment_id in found[:limit]]

    # Vectors for search by meaning ---------------------------------------------------------------

    def embedding_keys(self, model: str, *, email_id: str | None = None) -> dict[str, str]:
        with self.connect() as conn:
            if email_id is None:
                rows = conn.execute("SELECT key, text_key FROM embeddings WHERE model = ?", (model,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT key, text_key FROM embeddings WHERE model = ? AND email_id = ?", (model, email_id)
                ).fetchall()
        return {row["key"]: row["text_key"] for row in rows}

    def embedding_rows(self, model: str, *, prefix: str) -> list[tuple[str, str, bytes]]:
        """(key, text key, vector bytes) for keys starting with ``prefix``."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT key, text_key, vector FROM embeddings WHERE model = ? AND substr(key, 1, ?) = ?",
                (model, len(prefix), prefix),
            ).fetchall()
        return [(row["key"], row["text_key"], row["vector"]) for row in rows]

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

    def domain_history(self, domain: str, *, exclude: str = "") -> int:
        """Emails from addresses at exactly this domain, other than ``exclude``, that the fraud check let
        through or the user said weren't fraud. A look-alike domain's own first email was flagged, so it
        doesn't make the domain one the user already hears from."""
        if not domain:
            return 0
        with self.connect() as conn:
            return conn.execute(
                f"""SELECT COUNT(*) AS n FROM emails WHERE instr(sender_email, '@') > 0
                AND lower(substr(sender_email, instr(sender_email, '@') + 1)) = ? AND id != ?
                AND (COALESCE(flags, '') {_LIKE} OR (COALESCE(flags, '') NOT {_LIKE} AND COALESCE(flags, '') NOT {_LIKE}
                     AND COALESCE(flags, '') NOT {_LIKE}))""",
                (
                    domain.lower(), exclude, _json_contains("fraud_cleared"), _json_contains("fraud_risk"),
                    _json_contains("payment_caution"), _json_contains("fraud_confirmed"),
                ),
            ).fetchone()["n"]

    def email_ids_from(self, *, sender: str = "", domain: str = "") -> list[str]:
        """Emails from one address, or from a domain and its subdomains."""
        with self.connect() as conn:
            if sender:
                rows = conn.execute("SELECT id FROM emails WHERE lower(sender_email) = ?", (sender.lower(),)).fetchall()
            elif domain:
                domain = domain.lower()
                rows = conn.execute(
                    f"SELECT id FROM emails WHERE lower(sender_email) {_LIKE} OR lower(sender_email) {_LIKE}",
                    (f"%@{_like_escape(domain)}", f"%.{_like_escape(domain)}"),
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


def _coding_from_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "email_id": row["email_id"],
        "status": row["status"],
        "codes": _loads(row["codes"], []),
        "others": _loads(row["others"], []),
        "unlisted": _loads(row["unlisted"], []),
        "signature": row["signature"] or "",
        "decided_at": row["decided_at"] or "",
        "checked_at": row["checked_at"] or "",
    }


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


def _readings_for(conn: sqlite3.Connection, attachment_ids: list[str]) -> dict[str, dict[int, dict]]:
    """Attachment id -> page -> its stored vision reading."""
    if not attachment_ids:
        return {}
    marks = ", ".join("?" for _ in attachment_ids)
    rows = conn.execute(f"SELECT * FROM page_readings WHERE attachment_id IN ({marks}) ORDER BY page", attachment_ids).fetchall()
    out: dict[str, dict[int, dict]] = {}
    for row in rows:
        out.setdefault(row["attachment_id"], {})[int(row["page"])] = {key: row[key] for key in row.keys()}
    return out


def _same_file(readings: dict[int, dict], sha256: str) -> dict[int, dict]:
    """The readings of the file with this SHA-256 (all of them when it isn't known)."""
    if not sha256:
        return readings
    return {page: row for page, row in readings.items() if not row.get("sha256") or row["sha256"] == sha256}


def shown_text(text: str, readings: dict[int, dict] | None, sha256: str = "") -> str:
    """The attachment's text with the pages the vision model read shown as vision.py writes them."""
    readings = _same_file(readings or {}, sha256)
    if not readings:
        return text
    from controller_inbox import vision

    return vision.shown_text(text, readings)


def _email_from_rows(
    row: sqlite3.Row,
    attachment_rows: list[sqlite3.Row],
    action_rows: list[sqlite3.Row],
    readings: dict[str, dict[int, dict]] | None = None,
) -> EmailRecord:
    readings = readings or {}
    attachments = [
        AttachmentRecord(
            id=item["id"],
            email_id=item["email_id"],
            filename=item["filename"],
            content_type=item["content_type"],
            size_bytes=item["size_bytes"] or 0,
            sha256=item["sha256"],
            extracted_text=shown_text(item["extracted_text"] or "", readings.get(item["id"]), item["sha256"] or ""),
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
    f" AND (a.filename {_LIKE} OR a.extracted_text {_LIKE}))"
    # The cost code an AP invoice was coded to, and its description, find it too.
    f" OR EXISTS (SELECT 1 FROM cost_codings c WHERE c.email_id = emails.id AND c.codes {_LIKE}))"
)


def _like_escape(text: str) -> str:
    """``text`` with LIKE's wildcards (and the escape character) made literal, for ``ESCAPE '\\'``."""
    return re.sub(r"([\\%_])", r"\\\1", text)


def _contains(word: str) -> str:
    """A LIKE pattern that treats % and _ in what was typed as plain characters."""
    return "%" + _like_escape(word) + "%"


def _json_contains(value: str) -> str:
    """A LIKE pattern for ``value`` as a whole JSON string inside a stored JSON document."""
    return "%" + _like_escape(_dumps(value)) + "%"


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
    if "done_at" not in cols:
        conn.execute("ALTER TABLE emails ADD COLUMN done_at TEXT DEFAULT ''")
    if "reply_to" not in cols:
        conn.execute("ALTER TABLE emails ADD COLUMN reply_to TEXT DEFAULT ''")
    if not conn.execute("SELECT 1 FROM sync_state WHERE key = 'attachment_text_without_nul'").fetchone():
        # Text stored before NUL characters were dropped: summaries keyed by its length never matched.
        rows = conn.execute("SELECT id, extracted_text FROM attachments WHERE instr(extracted_text, char(0)) > 0").fetchall()
        conn.executemany("UPDATE attachments SET extracted_text = ? WHERE id = ?", [(_text(row[1]), row[0]) for row in rows])
        conn.execute("INSERT OR REPLACE INTO sync_state(key, value) VALUES ('attachment_text_without_nul', '1')")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_folder ON emails(folder)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_model ON emails(model_status)")
