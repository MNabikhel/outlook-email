"""Emails that belong together: one conversation, the same invoice or PO, the same file, a file's versions."""

from __future__ import annotations

import base64
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone

import pytest

from controller_inbox import links
from controller_inbox.folder_mail import ingest_folder
from controller_inbox.models import AttachmentRecord, DocumentType, EmailRecord, ExtractedFields, Importance

START = datetime(2026, 10, 5, 14, 0, tzinfo=timezone.utc)
PDF = b"%PDF-1.4\n" + b"0" * 4000


def _eml(settings, name: str, subject: str, *, sender: str = "Priya Raman <priya@northwind.com>", mid: str, reply_to_mid: str = "",
         refs: str = "", hours: float = 0, files: list[tuple[str, bytes]] = (), body: str = "See below.", thread_index: str = "") -> None:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "AP Team <ap@taz.com>"
    message["Cc"] = "maya@taz.com"
    message["Subject"] = subject
    message["Date"] = format_datetime(START + timedelta(hours=hours))
    message["Message-ID"] = mid
    if reply_to_mid:
        message["In-Reply-To"] = reply_to_mid
    if refs:
        message["References"] = refs
    if thread_index:
        message["Thread-Index"] = thread_index
    message.set_content(body)
    for filename, data in files:
        message.add_attachment(data, maintype="application", subtype="octet-stream", filename=filename)
    settings.ensure_data_dir()
    (settings.inbox_incoming / name).write_bytes(bytes(message))


def _by_subject(store) -> dict[str, EmailRecord]:
    return {email.subject: email for email in store.list_emails(limit=-1) for email in [store.get_email(email.id)]}


# --- Reading the headers ------------------------------------------------------------------------------------------


def test_the_headers_of_a_reply_are_kept(store, settings):
    _eml(settings, "a.eml", "Invoice 4410 question", mid="<root@northwind.com>")
    _eml(settings, "b.eml", "RE: Invoice 4410 question", sender="AP Team <ap@taz.com>", mid="<r1@taz.com>",
         reply_to_mid="<root@northwind.com>", refs="<root@northwind.com>", hours=2)
    ingest_folder(store, settings)
    reply = _by_subject(store)["RE: Invoice 4410 question"]
    assert reply.threading == {"in_reply_to": "root@northwind.com", "references": ["root@northwind.com"], "to": ["ap@taz.com", "maya@taz.com"]}


def test_outlooks_thread_index_names_the_conversation():
    index = bytes([1]) + bytes(5) + bytes(range(16, 32)) + bytes(10)
    assert links.conversation_of(base64.b64encode(index).decode()) == bytes(range(16, 32)).hex()
    assert links.conversation_of(index) == bytes(range(16, 32)).hex()
    assert links.conversation_of("not base64!!") == ""
    assert links.conversation_of(bytes(30)) == "", "an index of zeros names nothing"


def test_a_msg_without_internet_headers_is_read_from_its_own_properties(store, settings):
    # Mail between colleagues on one Exchange server has no Internet headers: its reply and conversation come from
    # the message's own properties.
    from msgfactory import write_msg

    index = bytes([1]) + bytes(5) + bytes(range(40, 56)) + bytes(5)
    write_msg(settings.inbox_incoming / "a.msg", "RE: Q4 budget", "Updated numbers attached.", exchange=True,
              conversation_index=index, in_reply_to="<first@taz.com>", references="<first@taz.com>")
    ingest_folder(store, settings)
    email = _by_subject(store)["RE: Q4 budget"]
    assert email.threading["in_reply_to"] == "first@taz.com"
    assert email.threading["conversation"] == bytes(range(40, 56)).hex()
    assert ("thread", "mid:first@taz.com") in store.link_keys(email.id)


# --- Keys -----------------------------------------------------------------------------------------------------------


def _record(id_: str, *, sender: str = "billing@northwind.com", invoices=(), pos=(), files=(), mid: str = "", threading=None,
            received: str = "2026-10-05T14:00:00+00:00", flags=()) -> EmailRecord:
    return EmailRecord(
        id=id_, subject=id_, sender_name="", sender_email=sender, received_at=received, body_text="", body_preview="",
        has_attachments=bool(files), outlook_importance="normal", is_read=False, category=DocumentType.OTHER,
        category_confidence=0, importance=Importance.LOW, importance_score=0, flags=list(flags),
        extracted=ExtractedFields(invoice_numbers=list(invoices), po_numbers=list(pos)), internet_message_id=mid,
        threading=threading or {}, source="folder",
        attachments=[
            AttachmentRecord(id=f"{id_}:{n}", email_id=id_, filename=name, content_type="application/pdf", size_bytes=size, sha256=sha,
                             extracted_text="", document_type=DocumentType.OTHER, document_confidence=0, extracted_fields=fields or ExtractedFields())
            for n, (name, sha, size, fields) in enumerate(files)
        ],
    )


def test_keys_leave_out_what_links_everything():
    email = _record(
        "e1",
        invoices=["INV-0010482", "10482"],
        pos=["PO-77821"],
        files=[
            ("INV-10482.pdf", "a" * 64, 40_000, None),
            ("image001.png", "b" * 64, 9_000, None),  # a signature's logo
            ("ATT00001.htm", "c" * 64, 3_000, None),
            ("tiny.pdf", "d" * 64, 200, None),
            ("AP aging.xlsx", "e" * 64, 30_000, ExtractedFields(invoice_numbers=["1001", "1002", "1003"])),  # lists every vendor's
        ],
        mid="<abc@northwind.com>",
    )
    found = set(links.keys(email))
    assert ("invoice", "INV10482") in found and ("invoice", "northwind.com|10482") in found
    assert ("po", "PO77821") in found
    assert ("file", "a" * 64) in found and ("file", "e" * 64) in found
    assert not {("file", "b" * 64), ("file", "c" * 64), ("file", "d" * 64)} & found
    assert not any(kind == "invoice" and value.endswith(("1001", "1002")) for kind, value in found)
    assert ("thread", "mid:abc@northwind.com") in found
    assert ("stem", "inv 10482.pdf") in found and ("stem", "ap aging.xlsx") in found


@pytest.mark.parametrize(
    ("name", "stem"),
    [
        ("Budget v2 FINAL (1).xlsx", "budget.xlsx"),
        ("Budget_v3.xlsx", "budget.xlsx"),
        ("Budget - Copy.xlsx", "budget.xlsx"),
        ("Budget 10-31-2026 revised.xlsx", "budget.xlsx"),
        ("Q3 Close Pack rev 4.pdf", "q3 close pack.pdf"),
        ("scan001.pdf", ""),
        ("Invoice.pdf", ""),
        ("12345.pdf", ""),
        ("forwarded › Lease schedule DRAFT.docx", "lease schedule.docx"),
    ],
)
def test_a_files_name_without_its_version_marks(name, stem):
    assert links.file_stem(name) == stem


# --- Related emails ------------------------------------------------------------------------------------------------


def test_a_conversation_is_found_from_any_of_its_headers(store):
    # The reply names the first; the forward names only the reply (no References); the last carries only
    # Outlook's conversation, shared with the forward. All four are one conversation; the newsletter isn't.
    store.upsert_email(_record("first", mid="<m1@n.com>", received="2026-10-01T09:00:00+00:00"))
    store.upsert_email(_record("reply", sender="ap@taz.com", mid="<m2@taz.com>", threading={"in_reply_to": "m1@n.com", "references": ["m1@n.com"]}, received="2026-10-01T10:00:00+00:00"))
    store.upsert_email(_record("forward", sender="ap@taz.com", mid="<m3@taz.com>", threading={"in_reply_to": "m2@taz.com", "conversation": "ab" * 16}, received="2026-10-02T10:00:00+00:00"))
    store.upsert_email(_record("outlook", sender="maya@taz.com", threading={"conversation": "ab" * 16}, received="2026-10-03T10:00:00+00:00"))
    store.upsert_email(_record("news", sender="news@acct.com", mid="<n1@acct.com>"))
    for start in ("first", "outlook", "reply"):
        found = links.related(store, store.get_email(start))
        assert [e.id for e in found["thread"]] == ["first", "reply", "forward", "outlook"], start
    assert links.related(store, store.get_email("news"))["thread"] == []


def test_the_same_invoice_po_or_file_links_emails_and_says_why(store):
    store.upsert_email(_record("inv", invoices=["INV-10482"], files=[("INV-10482.pdf", "f" * 64, 40_000, None)], received="2026-10-01T09:00:00+00:00"))
    store.upsert_email(_record("again", invoices=["inv 10482"], files=[("INV-10482.pdf", "f" * 64, 40_000, None)], received="2026-10-02T09:00:00+00:00"))
    store.upsert_email(_record("po", sender="buyer@taz.com", pos=["PO-77821"], received="2026-09-20T09:00:00+00:00"))
    store.upsert_email(_record("inv-po", invoices=["INV-20000"], pos=["PO 77821"], received="2026-10-03T09:00:00+00:00"))
    store.upsert_email(_record("other-vendor", sender="ar@acme.com", invoices=["10482"]))
    found = links.related(store, store.get_email("inv"))
    assert [(g["label"], [e.id for e in g["emails"]]) for g in found["groups"]] == [("Same invoice INV-10482", ["again"])]
    found = links.related(store, store.get_email("inv-po"))
    assert [(g["label"], [e.id for e in g["emails"]]) for g in found["groups"]] == [("Same PO PO 77821", ["po"])]


def test_a_number_of_digits_only_is_one_vendors(store):
    store.upsert_email(_record("ours", invoices=["10482"]))
    store.upsert_email(_record("theirs", sender="ar@acme.com", invoices=["10482"]))
    store.upsert_email(_record("ours-again", sender="ap@northwind.com", invoices=["10482"], received="2026-10-06T14:00:00+00:00"))
    assert [e.id for g in links.related(store, store.get_email("ours"))["groups"] for e in g["emails"]] == ["ours-again"]


def test_a_newer_version_of_a_file_is_found_by_its_name(store):
    store.upsert_email(_record("v1", files=[("Lease schedule v1.xlsx", "1" * 64, 20_000, None)], received="2026-09-01T09:00:00+00:00"))
    store.upsert_email(_record("v2", files=[("Lease schedule v2 FINAL.xlsx", "2" * 64, 21_000, None)], received="2026-09-15T09:00:00+00:00"))
    store.upsert_email(_record("same", files=[("Lease schedule v1.xlsx", "1" * 64, 20_000, None)], received="2026-09-02T09:00:00+00:00"))
    groups = links.related(store, store.get_email("v2"))["groups"]
    assert [(g["label"], [e.id for e in g["emails"]]) for g in groups] == [("Another version of Lease schedule v2 FINAL.xlsx", ["same", "v1"])]
    groups = links.related(store, store.get_email("v1"))["groups"]
    assert [(g["kind"], [e.id for e in g["emails"]]) for g in groups] == [("file", ["same"]), ("stem", ["v2"])]


def test_a_key_many_emails_share_links_none_of_them(store):
    # A vendor's W-9 on every one of its emails says nothing about any two of them.
    for n in range(links.COMMON + 2):
        store.upsert_email(_record(f"e{n}", files=[("Northwind W-9 form.pdf", "9" * 64, 50_000, None)], received=f"2026-10-01T{n % 24:02d}:00:00+00:00"))
    assert links.related(store, store.get_email("e0"))["groups"] == []


def test_mail_held_as_fraud_shows_in_its_thread_with_its_files_unlinked(store):
    store.upsert_email(_record("real", mid="<p1@northwind.com>", files=[("Remit form.pdf", "7" * 64, 30_000, None)], received="2026-10-01T09:00:00+00:00",
                               threading={"to": ["ap@taz.com"]}))
    store.upsert_email(_record("ours", sender="ap@taz.com", mid="<p2@taz.com>", threading={"in_reply_to": "p1@northwind.com"}, received="2026-10-01T11:00:00+00:00"))
    # A reply from a lookalike domain slipped into the thread, with a "new bank details" form.
    store.upsert_email(_record("hijack", sender="billing@northwind-supplies.co", threading={"in_reply_to": "p2@taz.com"},
                               files=[("Remit form.pdf", "7" * 64, 30_000, None)], received="2026-10-02T08:00:00+00:00", flags=["fraud_risk"]))
    found = links.related(store, store.get_email("ours"))
    assert [e.id for e in found["thread"]] == ["real", "ours", "hijack"]
    assert found["new_domains"] == {"hijack"}, "the lookalike domain is new to the conversation; the vendor's customer isn't"
    assert links.related(store, store.get_email("hijack"))["groups"] == [], "a held email's files link to nothing"
    assert links.related(store, store.get_email("real"))["groups"] == [], "nor does the real vendor's file link to the held one"


def test_deleting_an_email_removes_its_keys_and_old_mail_is_filed_in_batches(store):
    store.upsert_email(_record("a", invoices=["INV-1"], mid="<a@n.com>"))
    store.upsert_email(_record("b", invoices=["INV-1"]))
    store.delete_emails(["b"])
    assert links.related(store, store.get_email("a"))["groups"] == []
    with store.connect() as conn:
        conn.execute("DELETE FROM email_keys")
    assert store.link_keys("a") == []
    assert store.file_link_keys(batch=1) == 1
    assert store.file_link_keys(batch=1) == 0
    assert ("thread", "mid:a@n.com") in store.link_keys("a")


def test_mail_read_by_an_older_version_gets_its_conversation_from_its_kept_file(store, settings):
    _eml(settings, "a.eml", "Lease renewal", mid="<l1@landlord.com>")
    _eml(settings, "b.eml", "RE: Lease renewal", sender="AP Team <ap@taz.com>", mid="<l2@taz.com>", reply_to_mid="<l1@landlord.com>", hours=3)
    ingest_folder(store, settings)
    reply = _by_subject(store)["RE: Lease renewal"]
    assert reply.source_path, "the dropped file is kept, to read again"
    with store.connect() as conn:  # as an older version stored it
        conn.execute("UPDATE emails SET threading = ''")
        conn.execute("DELETE FROM email_keys")
        conn.execute("DELETE FROM sync_state WHERE key IN ('link_keys_version', 'link_keys_rowid', 'thread_headers_rowid')")
    assert links.related(store, store.get_email(reply.id))["thread"] == []
    done = links.catch_up(store, settings)
    assert done["filed"] == 2 and done["headers"] == 2
    assert [e.subject for e in links.related(store, store.get_email(reply.id))["thread"]] == ["Lease renewal", "RE: Lease renewal"]
    assert links.catch_up(store, settings) == {"filed": 0, "headers": 0}, "once"


def test_an_email_shows_its_conversation_and_related_mail(loaded, settings):
    from fastapi.testclient import TestClient
    from helpers import PAGE

    from controller_inbox.web import create_app

    client = TestClient(create_app(settings, loaded), headers=PAGE)
    related = client.get("/api/mail/demo-inv-10482").json()["related"]
    assert [(row["id"], row["here"]) for row in related["thread"]] == [("demo-inv-10482", True), ("demo-inv-10482-dup", False)]
    assert related["thread"][1]["file_names"] == ["INV-10482.pdf"]
    assert client.get("/api/mail/demo-chase-stmt").json()["related"] == {"thread": [], "groups": [], "can_look": False}


# --- A whole matter ------------------------------------------------------------------------------------------------


def test_a_matter_is_followed_two_links_out_with_why_each_email_is_there(store):
    # The invoice names the PO; the PO's email names the quote's file; the quote is two links from the invoice.
    store.upsert_email(_record("quote", sender="sales@northwind.com", files=[("Northwind quote Q-88.pdf", "q" * 64, 30_000, None)], received="2026-08-20T09:00:00+00:00"))
    store.upsert_email(_record("po", sender="buyer@taz.com", pos=["PO-77821"], files=[("Northwind quote Q-88.pdf", "q" * 64, 30_000, None)], received="2026-09-01T09:00:00+00:00"))
    store.upsert_email(_record("invoice", pos=["PO-77821"], invoices=["INV-10482"], mid="<inv@northwind.com>", received="2026-09-20T09:00:00+00:00"))
    store.upsert_email(_record("reply", sender="ap@taz.com", threading={"in_reply_to": "inv@northwind.com"}, received="2026-09-21T09:00:00+00:00"))
    store.upsert_email(_record("unrelated", sender="news@acct.com"))
    found = links.trace(store, store.get_email("invoice"))
    assert [(record.id, reasons) for record, reasons in found["steps"]] == [
        ("quote", ["Same file: Northwind quote Q-88.pdf"]),
        ("po", ["Same PO PO-77821"]),
        ("invoice", []),
        ("reply", ["In the conversation"]),
    ]


def test_what_changed_between_versions_of_a_workbook_and_a_document():
    old = '[sheet "Budget" A1:C3]\nA1: Line | B1: Q3 | C1: Q4\nA2 (Line): Rent | B2 (Q3): 1,200 | C2 (Q4): 1,200\nA3 (Line): Travel | B3 (Q3): 400 (=SUM(D1:D4)) | C3 (Q4): 450'
    new = '[sheet "Budget" A1:C4]\nA1: Line | B1: Q3 | C1: Q4\nA2 (Line): Rent | B2 (Q3): 1,200 | C2 (Q4): 1,450\nA3 (Line): Travel | B3 (Q3): 400 (=SUM(D1:D5)) | C3 (Q4): 450\nA4 (Line): Fees | B4 (Q3): 90'
    assert links.changes(old, new) == (["Budget!C2 (Q4): 1,200 → 1,450", "Budget!A4 (Line): added Fees", "Budget!B4 (Q3): added 90"], 0)
    listed, more = links.changes("[page 1]\nTerm: 12 months\nRent: $4,000", "[page 1]\nTerm: 24 months\nRent: $4,000", limit=1)
    assert listed == ["Removed: Term: 12 months"] and more == 1


def test_the_versions_along_a_matter_say_what_changed(store):
    v1 = _record("v1", files=[("Lease schedule v1.xlsx", "1" * 64, 20_000, None)], received="2026-09-01T09:00:00+00:00")
    v1.attachments[0].extracted_text = '[sheet "Lease" A1:B2]\nA1: Item | B1: Rent\nA2 (Item): Unit 4 | B2 (Rent): 4,000'
    v2 = _record("v2", files=[("Lease schedule v2.xlsx", "2" * 64, 20_000, None)], received="2026-09-15T09:00:00+00:00")
    v2.attachments[0].extracted_text = '[sheet "Lease" A1:B2]\nA1: Item | B1: Rent\nA2 (Item): Unit 4 | B2 (Rent): 4,250'
    store.upsert_email(v1)
    store.upsert_email(v2)
    found = links.trace(store, store.get_email("v2"))
    [version] = found["versions"]
    assert (version["old"][0].id, version["new"][0].id, version["changes"]) == ("v1", "v2", ["Lease!B2 (Rent): 4,000 → 4,250"])


def test_the_trace_page_s_data(loaded, settings):
    from fastapi.testclient import TestClient
    from helpers import PAGE

    from controller_inbox.web import create_app

    client = TestClient(create_app(settings, loaded), headers=PAGE)
    data = client.get("/api/trace/demo-inv-10482").json()
    assert data["email"]["id"] == "demo-inv-10482"
    assert [(step["id"], step["here"], step["reasons"]) for step in data["steps"]] == [
        ("demo-inv-10482", True, []), ("demo-inv-10482-dup", False, ["In the conversation"])
    ]
    assert client.get("/api/trace/nope").status_code == 404


def test_a_question_about_how_an_email_came_about_reads_its_linked_mail(loaded):
    from controller_inbox import agent
    from controller_inbox.assistant import pick_sources

    for question in ("What happened on this so far?", "Trace this back to where it started", "show me the whole thread"):
        sources, about_today, _found = pick_sources(loaded, question, email_id="demo-inv-10482")
        assert [email.id for email in sources] == ["demo-inv-10482", "demo-inv-10482-dup"] and not about_today, question
    sources, _today, _found = pick_sources(loaded, "What is the amount due?", email_id="demo-inv-10482")
    assert [email.id for email in sources] == ["demo-inv-10482"], "a question about the email itself stays on it"
    ws = agent.Workspace(loaded, None, [loaded.get_email("demo-inv-10482")])
    opened = agent._open_email(ws, ws.sources[0])
    assert "Related emails (open one by its number):\n[2] " in opened and "(same conversation)" in opened
    assert ws.email("2").id == "demo-inv-10482-dup", "the model opens a related email by the number it was given"
