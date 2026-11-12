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
    assert ("invoice", "northwind.com|10482") in found and not any(value == "INV10482" for _kind, value in found), "INV-0010482 and 10482 are one vendor's"
    assert ("po", "77821") in found, "a PO is matched however it is written (PO-77821, PO 77821, 77821)"
    assert ("file", "a" * 64) in found and ("file", "e" * 64) in found
    assert not {("file", "b" * 64), ("file", "c" * 64), ("file", "d" * 64)} & found
    assert not any(kind == "invoice" and value.endswith(("1001", "1002")) for kind, value in found)
    assert ("msg", "mid:abc@northwind.com") in found, "its own id, linked only through a reply naming it"
    assert ("stem", "northwind.com|inv 10482.pdf") in found and ("stem", "northwind.com|ap aging.xlsx") in found


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
    assert ("msg", "mid:a@n.com") in store.link_keys("a")


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
    assert client.get("/api/mail/demo-chase-stmt").json()["related"] == {"thread": [], "groups": [], "can_look": False, "thread_size": 0}


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



def test_a_held_emails_numbers_count_only_where_its_own_text_names_them(store):
    # The lookalike's body names no invoice; the PDF it carries (held, never opened) says NW-10482. It must not
    # show up as "Same invoice" beside the real vendor's mail.
    store.upsert_email(_record("real", invoices=["NW-10482"]))
    lookalike = _record("bec", sender="ap@northwind-supp1y.com", flags=["fraud_risk"],
                        files=[("remit.pdf", "r" * 64, 30_000, ExtractedFields(invoice_numbers=["NW-10482"]))])
    lookalike.extracted.invoice_numbers = ["NW-10482"]  # merged from its file, as the pipeline does
    store.upsert_email(lookalike)
    assert not any(kind == "invoice" for kind, _value in store.link_keys("bec"))
    assert links.related(store, store.get_email("real"))["groups"] == []
    said = _record("bec2", sender="ap@northwind-supp1y.com", flags=["fraud_risk"], invoices=["NW-10482"])
    said.body_text = "Please pay invoice NW-10482 to our new account."
    store.upsert_email(said)
    assert ("invoice", "NW10482") in store.link_keys("bec2"), "named in its own words, it is linked (and shown held)"


def test_the_model_is_warned_about_held_mail_among_the_related(store):
    from controller_inbox import agent

    store.upsert_email(_record("real", mid="<p1@northwind.com>", received="2026-10-01T09:00:00+00:00", threading={"to": ["ap@taz.com"]}))
    store.upsert_email(_record("hijack", sender="billing@northwind-supplies.co", threading={"in_reply_to": "p1@northwind.com"},
                               received="2026-10-02T08:00:00+00:00", flags=["fraud_risk"]))
    ws = agent.Workspace(store, None, [store.get_email("real")])
    lines = agent.related_lines(ws, ws.sources[0])
    assert "held as possible payment fraud (same conversation): not shown — Warning: verify by phone" in lines
    assert "northwind-supplies" not in lines, "a held email is listed without its words or address"


def test_emails_that_only_share_a_message_id_are_no_conversation(store):
    # A scanner gives every scan the same Message-ID: the scans are not one thread.
    for n in range(3):
        store.upsert_email(_record(f"scan{n}", sender="scanner@taz.com", mid="<scan@printer.local>", received=f"2026-10-0{n + 1}T09:00:00+00:00"))
    assert links.related(store, store.get_email("scan0"))["thread"] == []


def test_a_files_versions_are_one_senders(store):
    # Everyone's "Timesheet.xlsx" is not one file.
    store.upsert_email(_record("a", sender="pat@contractor-a.com", files=[("Timesheet week 40.xlsx", "1" * 64, 20_000, None)]))
    store.upsert_email(_record("b", sender="lee@contractor-b.com", files=[("Timesheet week 40.xlsx", "2" * 64, 20_000, None)]))
    assert links.related(store, store.get_email("a"))["groups"] == []


def test_a_long_conversation_is_shown_around_the_email_opened(store):
    for n in range(200):
        threading = {"in_reply_to": f"m{n - 1}@n.com", "references": ["m0@n.com"]} if n else {}
        store.upsert_email(_record(f"e{n:03d}", mid=f"<m{n}@n.com>", threading=threading, received=f"2026-0{1 + n // 30 % 9}-{1 + n % 28:02d}T09:{n % 60:02d}:00+00:00"))
    for opened in ("e000", "e100", "e199"):
        found = links.related(store, store.get_email(opened))
        ids = [e.id for e in found["thread"]]
        assert opened in ids and len(ids) == links.MAX_THREAD and found["thread_size"] == 200, opened


def test_a_moved_closedesk_folder_still_finds_its_kept_files(tmp_path, settings):
    settings.ensure_data_dir()
    kept = settings.inbox_dir / "processed" / "2026-10" / "a.eml"
    kept.parent.mkdir(parents=True)
    kept.write_text("Subject: x\n\nbody")
    old = tmp_path / "Old place" / "CloseDesk" / "inbox" / "processed" / "2026-10" / "a.eml"
    assert links.kept_file(settings, str(old)) == kept
    assert links.kept_file(settings, str(tmp_path / "elsewhere" / "b.eml")) is None
    assert links.kept_file(settings, "") is None


def test_a_backdated_or_addressless_reply_is_still_new_to_the_conversation(store):
    # The real thread: the vendor writes to AP, AP answers.
    store.upsert_email(_record("root", mid="<root@northwind.com>", received="2026-10-01T09:00:00+00:00", threading={"to": ["ap@taz.com"]}))
    store.upsert_email(_record("ours", sender="ap@taz.com", mid="<r1@taz.com>", received="2026-10-01T11:00:00+00:00",
                               threading={"in_reply_to": "root@northwind.com", "references": ["root@northwind.com"], "to": ["priya@northwind.com"]}))
    # A lookalike's reply, dated before the vendor's first email, copying the vendor to look at home.
    store.upsert_email(_record("hijack", sender="priya@northwnd.com", received="2026-09-01T09:00:00+00:00",
                               threading={"in_reply_to": "r1@taz.com", "references": ["root@northwind.com", "r1@taz.com"], "to": ["ap@taz.com", "priya@northwind.com"]}))
    # A reply with no From address at all, only a Reply-To.
    nobody = _record("nobody", sender="", received="2026-10-02T09:00:00+00:00", threading={"in_reply_to": "r1@taz.com"})
    nobody.reply_to = "priya@northwnd-ap.com"
    store.upsert_email(nobody)
    assert links.related(store, store.get_email("ours"))["new_domains"] == {"hijack", "nobody"}


def test_a_held_emails_number_counts_only_as_a_whole_number_of_its_text(store):
    held = _record("held", sender="ar@nw-remit.net", flags=["fraud_risk"], invoices=["INV2291", "10482"])
    held.subject, held.body_text = "Ref INV-22918", "Call 555 104 8221"
    store.upsert_email(held)
    assert not any(kind == "invoice" for kind, _value in store.link_keys("held"))


def test_a_lookalikes_corrected_copy_is_no_version_and_a_changed_remit_line_is_flagged(store):
    real = _record("real", files=[("Northwind invoice INV-10482.pdf", "1" * 64, 30_000, None)], received="2026-10-01T09:00:00+00:00")
    real.attachments[0].extracted_text = "[page 1]\nInvoice INV-10482\nRemit to: First Bank account ****3333"
    fake = _record("fake", sender="ar@nw-remit.net", files=[("Northwind invoice INV-10482 (1).pdf", "2" * 64, 30_000, None)], received="2026-10-02T09:00:00+00:00", invoices=["INV-10482"])
    fake.attachments[0].extracted_text = "[page 1]\nInvoice INV-10482\nRemit to: Metro Bank account ****7777"
    real.extracted.invoice_numbers = ["INV-10482"]
    store.upsert_email(real)
    store.upsert_email(fake)
    assert links.trace(store, store.get_email("real"))["versions"] == [], "another sender's file is no version"
    newer = _record("v2", files=[("Northwind invoice INV-10482 v2.pdf", "3" * 64, 30_000, None)], received="2026-10-03T09:00:00+00:00")
    newer.attachments[0].extracted_text = "[page 1]\nInvoice INV-10482\nRemit to: Metro Bank account ****7777"
    store.upsert_email(newer)
    [version] = links.trace(store, store.get_email("real"))["versions"]
    assert version["payment"] and version["new"][0].id == "v2"


def test_the_model_is_told_which_related_email_is_from_a_new_domain_and_a_cautioned_email_loses_its_model_links(store):
    from controller_inbox import agent

    store.upsert_email(_record("root", mid="<root@northwind.com>", received="2026-10-01T09:00:00+00:00", threading={"to": ["ap@taz.com"]}))
    store.upsert_email(_record("hijack", sender="priya@northwnd.com", received="2026-10-02T09:00:00+00:00", threading={"in_reply_to": "root@northwind.com"}))
    ws = agent.Workspace(store, None, [store.get_email("root")])
    lines = agent.related_lines(ws, ws.sources[0])
    assert "priya@northwnd.com" in lines and "domain is new to the conversation" in lines
    store.save_ai_link("root", "other", same=True, why="same quote", key="k")
    store.upsert_email(_record("other", sender="billing@harbor-pack.net", flags=["payment_caution"]))
    assert all(g["kind"] != "ai" for g in links.related(store, store.get_email("root"))["groups"])

# --- An invoice's conversation ------------------------------------------------------------------------------------


def _invoice_chain(settings, *, names=("1-invoice.eml", "2-colleague.eml", "3-vendor.eml")) -> None:
    from controller_inbox.demo import make_pdf

    pdf = make_pdf([["Coastal Print Co", "Invoice CP-5521", "Total due $1,840.00", "Due November 30, 2026"]])
    _eml(settings, names[0], "Invoice CP-5521", sender="Coastal Print AR <ar@coastalprint.com>", mid="<cp1@coastalprint.com>",
         files=[("CP-5521.pdf", pdf)], body="Please find invoice CP-5521 for $1,840.00 attached, due November 30.")
    _eml(settings, names[1], "RE: Invoice CP-5521", sender="Maya Chen <maya@taz.com>", mid="<cp2@taz.com>",
         reply_to_mid="<cp1@coastalprint.com>", refs="<cp1@coastalprint.com>", hours=2,
         body="Which PO is invoice CP-5521 for? I can't find it.\n\n> Please find invoice CP-5521 for $1,840.00 attached.")
    _eml(settings, names[2], "RE: Invoice CP-5521", sender="Coastal Print AR <ar@coastalprint.com>", mid="<cp3@coastalprint.com>",
         reply_to_mid="<cp2@taz.com>", refs="<cp1@coastalprint.com> <cp2@taz.com>", hours=4,
         body="Invoice CP-5521 is for PO-88012.\n\n> Which PO is invoice CP-5521 for?")


def _invoice_tasks(store) -> list[tuple[str, str]]:
    return sorted(
        (email.subject, action.title)
        for email in store.list_emails(limit=-1)
        for action in (store.get_email(email.id).actions)
        if action.source in {"invoice", "duplicate_rule"} and action.status.value == "open"
    )


@pytest.mark.parametrize("names", [("1-invoice.eml", "2-colleague.eml", "3-vendor.eml"), ("c-invoice.eml", "a-colleague.eml", "b-vendor.eml")])
def test_replies_about_an_invoice_make_no_second_entry_or_duplicate_task(store, settings, names):
    """Mail about an invoice in its own conversation is no second invoice, in whatever order the files are read."""
    _invoice_chain(settings, names=names)
    ingest_folder(store, settings, now=START + timedelta(hours=5))
    tasks = _invoice_tasks(store)
    assert [title for _subject, title in tasks if title.startswith("Code and enter")] and len(tasks) == 1, tasks
    assert tasks[0][0] == "Invoice CP-5521"
    assert not any("duplicate_invoice" in store.get_email(e.id).flags for e in store.list_emails(limit=-1))


def test_a_lookalike_replying_in_the_thread_with_the_invoice_is_still_a_duplicate(store, settings):
    from controller_inbox.demo import make_pdf

    _invoice_chain(settings)
    pdf = make_pdf([["Coastal Print Co", "Invoice CP-5521", "Total due $1,840.00", "New bank details enclosed"]])
    _eml(settings, "4-lookalike.eml", "RE: Invoice CP-5521", sender="Coastal Print AR <ar@coastalprlnt.com>", mid="<cp4@coastalprlnt.com>",
         reply_to_mid="<cp3@coastalprint.com>", refs="<cp1@coastalprint.com> <cp3@coastalprint.com>", hours=5,
         files=[("CP-5521 corrected.pdf", pdf)], body="Corrected invoice CP-5521 attached.")
    ingest_folder(store, settings, now=START + timedelta(hours=6))
    fake = next(e for e in store.list_emails(limit=-1) if e.sender_email.endswith("coastalprlnt.com"))
    assert "duplicate_invoice" in store.get_email(fake.id).flags


def test_a_po_links_the_buyers_mail_and_the_vendors(store):
    """Purchasing's "PO-88012 approved" and the vendor's invoice naming it are one PO; a short number stays one sender's."""
    store.upsert_email(_record("purchasing", sender="purchasing@taz.com", pos=["88012"]))
    store.upsert_email(_record("vendor", sender="ar@coastalprint.com", pos=["88012"], received="2026-10-06T14:00:00+00:00"))
    store.upsert_email(_record("short-a", sender="purchasing@taz.com", pos=["4410"]))
    store.upsert_email(_record("short-b", sender="ar@coastalprint.com", pos=["4410"]))
    assert [e.id for g in links.related(store, store.get_email("purchasing"))["groups"] for e in g["emails"]] == ["vendor"]
    assert not links.related(store, store.get_email("short-a"))["groups"]


def test_a_lookalikes_resent_pdf_never_takes_the_task_off_the_vendors_invoice(store, settings):
    """The vendor's invoice came as text only; a lookalike replying in the thread with "the PDF" must not take its
    "Code and enter" task, whether it is held, cautioned or only new to the conversation, in either order."""
    from controller_inbox.demo import make_pdf

    pdf = make_pdf([["Coastal Print Co", "Invoice CP-5521", "Total due $1,840.00"]])
    _eml(settings, "1-vendor.eml", "Invoice CP-5521", sender="Coastal Print AR <ar@coastalprint.com>", mid="<cp1@coastalprint.com>",
         body="Invoice CP-5521 for $1,840.00 is due November 30. Thank you.")
    _eml(settings, "2-lookalike.eml", "RE: Invoice CP-5521", sender="Coastal Print AR <ar@coastalprlnt.com>", mid="<cp9@coastalprlnt.com>",
         reply_to_mid="<cp1@coastalprint.com>", refs="<cp1@coastalprint.com>", hours=3, files=[("CP-5521.pdf", pdf)],
         body="Resending invoice CP-5521 as a PDF.")
    ingest_folder(store, settings, now=START + timedelta(hours=4))
    tasks = _invoice_tasks(store)
    assert ("Invoice CP-5521", "Code and enter invoice CP-5521 ($1,840.00)") in tasks, tasks


def test_microsoft_365_replies_are_no_duplicates_and_their_recipients_count(store):
    """Microsoft 365 mail has its conversation and recipients but no reply headers: the earliest stored email
    started it, its recipients took part, and a colleague's reply to the vendor's invoice is no duplicate."""
    invoice = _record("I", sender="ar@coastalprint.com", invoices=["CP-5521"], threading={"to": ["ap@taz.com"]}, received="2026-10-01T09:00:00+00:00")
    invoice.conversation_id = "conv-1"
    invoice.created_at = "2026-10-01T09:05:00+00:00"
    reply = _record("R", sender="maya@taz.com", threading={"to": ["ar@coastalprint.com"]}, received="2026-10-01T10:00:00+00:00")
    reply.conversation_id = "conv-1"
    reply.created_at = "2026-10-01T10:05:00+00:00"
    store.upsert_email(invoice)
    store.upsert_email(reply)
    assert links.related(store, store.get_email("R"))["new_domains"] == set()
    lookalike = _record("L", sender="ar@coastalprlnt.com", threading={"to": ["ap@taz.com"]}, received="2026-09-01T09:00:00+00:00")
    lookalike.conversation_id = "conv-1"
    lookalike.created_at = "2026-10-02T09:00:00+00:00"
    store.upsert_email(lookalike)
    assert links.related(store, store.get_email("I"))["new_domains"] == {"L"}, "backdating its Date doesn't make it the starter"


def test_an_email_claiming_another_domains_message_id_is_new_to_the_conversation():
    real = _record("real", sender="ar@coastalprint.com", mid="<cp1@coastalprint.com>", threading={"to": ["ap@taz.com"]}, received="2026-10-02T09:00:00+00:00")
    real.created_at = "2026-10-02T09:01:00+00:00"
    fake = _record("fake", sender="ar@coastalprlnt.com", mid="<cp1@coastalprint.com>", threading={"to": ["ap@taz.com"]}, received="2026-09-01T09:00:00+00:00")
    fake.created_at = "2026-10-03T09:00:00+00:00"
    reply = _record("reply", sender="ap@taz.com", mid="<r1@taz.com>", threading={"in_reply_to": "cp1@coastalprint.com"}, received="2026-10-02T10:00:00+00:00")
    reply.created_at = "2026-10-02T10:01:00+00:00"
    assert links.new_domains([fake, reply, real]) == {"fake"}


def test_a_po_written_two_ways_is_one_po_and_a_held_emails_own_numbers_count(store):
    store.upsert_email(_record("a", sender="purchasing@taz.com", pos=["PO 77821"]))
    store.upsert_email(_record("b", sender="ar@coastalprint.com", pos=["PO-77821"], received="2026-10-06T14:00:00+00:00"))
    assert [e.id for g in links.related(store, store.get_email("a"))["groups"] for e in g["emails"]] == ["b"]
    assert {"22918", "77821"} <= links._numbers_written("Invoice 22918 for PO-77821")


def test_a_file_name_of_thousands_of_spaces_is_read_in_a_moment():
    import time

    started = time.monotonic()
    links.file_stem("a" + " " * 4000 + "b.pdf")
    links.file_stem("a" + "_" * 4000 + "b copy.pdf")
    assert time.monotonic() - started < 1
    assert links.file_stem("Budget  _ copy (2).xlsx") == "budget.xlsx"


def test_microsoft_365_mail_keeps_who_it_went_to():
    from controller_inbox.graph import GraphMailbox

    item = {"id": "g1", "subject": "RE: Invoice", "from": {"emailAddress": {"address": "maya@taz.com"}},
            "toRecipients": [{"emailAddress": {"address": "AR@CoastalPrint.com"}}], "ccRecipients": [{"emailAddress": {"address": "ap@taz.com"}}],
            "receivedDateTime": "2026-10-01T10:00:00Z", "body": {"contentType": "text", "content": "Which PO?"}, "conversationId": "conv-1"}
    raw = GraphMailbox.__new__(GraphMailbox)._to_raw(item)
    assert raw.threading == {"to": ["ar@coastalprint.com", "ap@taz.com"]}


def test_the_whole_matter_of_a_long_conversation_keeps_the_email_it_was_opened_for(store):
    for n in range(50):
        threading = {"in_reply_to": f"m{n - 1}@taz.com", "references": [f"m{k}@taz.com" for k in range(max(0, n - 20), n)]} if n else {}
        store.upsert_email(_record(f"e{n:02d}", sender="ap@taz.com", mid=f"<m{n}@taz.com>", threading=threading,
                                   received=f"2026-10-{1 + n // 24:02d}T{n % 24:02d}:00:00+00:00"))
    steps = [record.id for record, _why in links.trace(store, store.get_email("e00"))["steps"]]
    assert "e00" in steps and len(steps) == links.MAX_TRACE

def test_a_run_links_stored_mail_for_a_few_seconds_and_a_failure_stops_nothing(store, settings, monkeypatch):
    from controller_inbox import overnight

    calls = []
    monkeypatch.setattr(links, "catch_up", lambda _store, _settings, **kw: (calls.append(kw), (_ for _ in ()).throw(RuntimeError("database is locked")))[1])
    overnight.run_overnight(store, settings, sync_graph=False, sync_outlook=False)
    assert calls and calls[0].get("seconds") == overnight.CATCH_UP_SECONDS


def test_a_new_store_needs_no_refiling(tmp_path):
    from controller_inbox.store import Store

    fresh = Store(tmp_path / "new.sqlite")
    with fresh.connect() as conn:
        assert conn.execute("SELECT value FROM sync_state WHERE key = 'link_keys_version'").fetchone()[0] == links.KEYS_VERSION


def test_the_whole_matter_walks_a_conversation_once(store, monkeypatch):
    for n in range(35):
        threading = {"in_reply_to": f"m{n - 1}@taz.com", "references": ["m0@taz.com", *[f"m{k}@taz.com" for k in range(max(1, n - 19), n)]]} if n else {}
        store.upsert_email(_record(f"e{n:02d}", sender="ap@taz.com", mid=f"<m{n}@taz.com>", threading=threading,
                                   received=f"2026-10-{1 + n // 24:02d}T{n % 24:02d}:00:00+00:00"))
    walks = []
    real = links._conversation
    monkeypatch.setattr(links, "_conversation", lambda *args: (walks.append(args[1]), real(*args))[1])
    steps = links.trace(store, store.get_email("e10"))["steps"]
    assert len(steps) == 35 and len(walks) <= 2, len(walks)


# Guards against: who had taken part in a conversation was counted on the 60 emails shown around the one opened.
# Opened late in a long thread, the first of those was taken for the email it started with: a lookalike's early
# reply passed as the starter, and the real vendor was marked "New in this conversation"; an auditor who had
# written twenty times before was marked new as well.
def test_who_took_part_is_counted_over_the_whole_of_a_long_conversation(store):
    root = "r0@northwind.com"
    store.upsert_email(_record("e00", mid=f"<{root}>", threading={"to": ["ap@taz.com", "jo@auditco.com"]}, received="2026-01-01T00:00:00+00:00"))
    prev, refs = root, [root]
    for n in range(1, 80):
        sender = ("billing@northwind.com", "ap@taz.com", "jo@auditco.com")[n % 3]
        if n == 19:
            sender = "billing@northwlnd.com"
        mid = f"r{n}@{sender.split('@')[1]}"
        to = [x for x in ("ap@taz.com", "billing@northwind.com") if x != sender]
        store.upsert_email(_record(f"e{n:02d}", sender=sender, mid=f"<{mid}>", threading={"in_reply_to": prev, "references": [refs[0], *refs[-19:]], "to": to},
                                   received=f"2026-01-{1 + n // 24:02d}T{n % 24:02d}:00:00+00:00"))
        prev, refs = mid, [*refs, mid]
    for opened in ("e20", "e49", "e50", "e79"):
        found = links.related(store, store.get_email(opened), ai=False)
        assert found["new_domains"] == {"e19"}, opened
        assert found["thread_size"] == 80


# Guards against: one sync or drop stores all its mail at one moment, and among those the Date header decided
# which came first. A lookalike claiming the vendor's Message-ID, dated a month earlier, took the Message-ID over
# and the real vendor's invoice was marked "New in this conversation"; so was the vendor answering our own email
# beside a backdated lookalike. The order the mail was read in counts now, never its Date.
def test_within_one_sync_the_date_header_never_decides_who_started_a_conversation(store):
    batch = "2026-10-03T08:00:00+00:00"
    real = _record("real", sender="ar@coastalprint.com", mid="<cp1@coastalprint.com>", threading={"to": ["ap@taz.com"]}, received="2026-10-02T09:00:00+00:00")
    reply = _record("reply", sender="ap@taz.com", mid="<r1@taz.com>", threading={"in_reply_to": "cp1@coastalprint.com"}, received="2026-10-02T10:00:00+00:00")
    fake = _record("fake", sender="ar@coastalprlnt.com", mid="<cp1@coastalprint.com>", threading={"to": ["ap@taz.com"]}, received="2026-09-01T09:00:00+00:00")
    vendor = _record("vendor", sender="ar@acme.com", mid="<v1@acme.com>", threading={"in_reply_to": "ours@taz.com", "to": ["ap@taz.com"]}, received="2026-10-02T09:00:00+00:00")
    sibling = _record("sibling", sender="ar@acrne.com", mid="<f1@acrne.com>", threading={"in_reply_to": "ours@taz.com", "to": ["ap@taz.com"]}, received="2026-09-01T09:00:00+00:00")
    for record in (real, reply, fake, vendor, sibling):
        record.created_at = batch
        store.upsert_email(record)
    assert links.related(store, store.get_email("reply"), ai=False)["new_domains"] == {"fake"}
    assert links.related(store, store.get_email("vendor"), ai=False)["new_domains"] == {"sibling"}


# Guards against: the Related panel counted the emails listed, so a conversation of 150 read "59 other emails in
# this conversation", with nothing saying only the part around the open email was listed.
def test_the_related_panel_is_told_the_whole_conversations_size(store, settings):
    from fastapi.testclient import TestClient
    from helpers import PAGE

    from controller_inbox.web import create_app

    for n in range(150):
        store.upsert_email(_record(f"m{n:03d}", mid=f"<m{n}@northwind.com>", received=f"2026-10-{1 + n // 24:02d}T{n % 24:02d}:00:00+00:00",
                                   threading={"conversation": "ab" * 16, "to": ["ap@taz.com"]}))
    related = TestClient(create_app(settings, store), headers=PAGE).get("/api/mail/m149").json()["related"]
    assert len(related["thread"]) == links.MAX_THREAD and related["thread_size"] == 150


def _book(title: str, rows: list[list]) -> str:
    import io

    from openpyxl import Workbook

    from controller_inbox.documents import xlsx_text

    book = Workbook()
    book.active.title = title
    for row in rows:
        book.active.append(row)
    data = io.BytesIO()
    book.save(data)
    return xlsx_text(data.getvalue())


def _two_versions(old_text: str, new_text: str, name: str = "Budget.xlsx") -> dict:
    old = _record("old", files=[(name.replace(".", " v1."), "1" * 64, 20_000, None)], received="2026-09-01T09:00:00+00:00")
    new = _record("new", files=[(name.replace(".", " v2."), "2" * 64, 20_000, None)], received="2026-09-15T09:00:00+00:00")
    old.attachments[0].extracted_text, new.attachments[0].extracted_text = old_text, new_text
    [version] = links.versions([old, new])
    return version


# Guards against: a cell whose column heading has brackets ("Amount (USD)") wasn't read at all, so a changed
# amount under it gave "No change in what was read from the two files".
def test_a_changed_cell_under_a_heading_with_brackets_is_listed():
    rows = lambda amount: [["Line", "Amount (USD)", "Change (%)"], ["Rent", 12000, 3], ["Utilities", amount, 5]]  # noqa: E731
    listed, _more = links.changes(_book("Budget", rows(1200)), _book("Budget", rows(1450)))
    assert len(listed) == 1 and listed[0].startswith("Budget!B3 (Amount (USD)): 1") and listed[0].endswith("450"), listed


# Guards against: a CSV's one sheet is named after its file, so two versions ("Accruals v1.csv", "v2.csv") shared
# no cell: every cell was listed as removed and added again, and the one real change was cut off the list.
def test_two_versions_of_a_csv_are_compared_cell_by_cell():
    from controller_inbox.documents import csv_text

    old = csv_text(b"Vendor,Amount\nNorthwind,1200.00\nAcme,500.00\nHarbor,75.00\n", "Accruals v1.csv")
    new = csv_text(b"Vendor,Amount\nNorthwind,1450.00\nAcme,500.00\nHarbor,75.00\n", "Accruals v2.csv")
    assert links.changes(old, new) == (["Accruals v2.csv!B2 (Amount): 1200.00 → 1450.00"], 0)


# Guards against: "Payment details changed between these versions: check them by phone" came up when no bank
# detail changed: when only a sheet's marker line did (a CSV named "Bank Rec Oct v2.csv", a sheet named "Bank Rec"
# that gained a row), and on any changed row of a schedule with a GL "Account" column or an "Account balance" line.
def test_payment_details_changed_only_when_a_bank_detail_did():
    from controller_inbox.documents import csv_text

    old = csv_text(b"Item,Amount\nCoffee,5.00\nRent,4000.00\n", "Bank Rec Oct v1.csv")
    new = csv_text(b"Item,Amount\nCoffee,6.00\nRent,4000.00\n", "Bank Rec Oct v2.csv")
    assert not _two_versions(old, new, "Bank Rec Oct.csv")["payment"]
    rows = [["Item", "Amount"], ["Coffee", 5], ["Rent", 4000]]
    assert not _two_versions(_book("Bank Rec", rows), _book("Bank Rec", [*rows, ["Fees", 12]]), "Bank Rec.xlsx")["payment"]
    gl = lambda amount: [["Account", "Description", "Amount"], [6200, "Utilities", amount], [6300, "Rent", 4000]]  # noqa: E731
    assert not _two_versions(_book("Accruals", gl(1200)), _book("Accruals", gl(1450)), "Accruals.xlsx")["payment"]
    assert not _two_versions("[page 1]\nAccount balance: $5,000.00", "[page 1]\nAccount balance: $6,250.00", "Statement Acme.pdf")["payment"]
    # A bank detail that did change is still flagged, in a workbook and in a letter.
    bank = lambda number: [["Vendor", "Account number", "Routing"], ["Northwind", number, "021000021"]]  # noqa: E731
    assert _two_versions(_book("Vendors", bank("12345678")), _book("Vendors", bank("87654321")), "Vendor list.xlsx")["payment"]
    assert _two_versions("[page 1]\nPay to account no. 12345678", "[page 1]\nPay to account no. 87654321", "Remittance letter.pdf")["payment"]


def _groups(store, email_id: str) -> list[tuple[str, list[str]]]:
    return [(g["label"], [e.id for e in g["emails"]]) for g in links.related(store, store.get_email(email_id), ai=False)["groups"]]


# Guards against: Xero numbers every small vendor's invoices INV-0001, INV-0002…, and only numbers of digits alone
# were one vendor's, so two unrelated vendors' INV-0042 were "Same invoice" and each one's whole matter (and the
# chat's answer about it) took in the other's mail.
def test_two_vendors_invoice_numbered_inv_and_digits_are_two_invoices(store):
    store.upsert_email(_record("a", invoices=["INV-0042"]))
    store.upsert_email(_record("b", sender="ar@acmesteel.com", invoices=["INV-0042"], received="2026-10-06T14:00:00+00:00"))
    store.upsert_email(_record("c", sender="ap@northwind.com", invoices=["0042"], received="2026-10-07T14:00:00+00:00"))
    store.upsert_email(_record("d", sender="ar@coastalprint.com", invoices=["CP-5521"]))
    store.upsert_email(_record("e", sender="maya@taz.com", invoices=["CP-5521"], received="2026-10-07T14:00:00+00:00"))
    assert _groups(store, "a") == [("Same invoice INV-0042", ["c"])]
    assert _groups(store, "d") == [("Same invoice CP-5521", ["e"])], "a vendor's own prefix still links whoever writes it"


# Guards against: a workbook or CSV listing several POs still filed its email under each of them (its numbers
# reached the email's own fields), so an open-PO report tied every vendor's invoice for those POs together.
def test_an_open_po_report_links_none_of_its_pos(store, settings):
    from controller_inbox.models import RawAttachment, RawMessage
    from controller_inbox.pipeline import process_message

    def message(id_, subject, sender, body, files=(), hour=9):
        return RawMessage(id=id_, subject=subject, sender_name="", sender_email=sender, received_at=START + timedelta(hours=hour), body_text=body,
                          body_preview=body[:100], has_attachments=bool(files), source="folder", internet_message_id=f"<{id_}@x>", attachments=list(files))

    rows = "PO Number,Vendor,Open amount\n" + "\n".join(f"PO-45001{n:02d},Vendor {n},{n * 100}.00" for n in range(1, 9)) + "\n"
    report = RawAttachment(id="a1", filename="Open PO report.csv", content_type="text/csv", size_bytes=len(rows), content=rows.encode())
    process_message(message("rep", "Open PO report for the week", "controller@taz.com", "Attached is this week's open PO report.", [report]), store, settings, now=START)
    process_message(message("va", "Invoice CP-5521 for PO 4500103", "ar@coastalprint.com", "Invoice CP-5521 for PO 4500103, $1,200.00 due.", hour=1), store, settings, now=START)
    process_message(message("vb", "Invoice NW-88001 for PO 4500106", "billing@northwind.com", "Invoice NW-88001 for PO 4500106, $900.00 due.", hour=2), store, settings, now=START)
    assert not [key for key in store.link_keys("rep") if key[0] == "po"]
    assert "vb" not in [record.id for record, _why in links.trace(store, store.get_email("va"))["steps"]]


# Guards against: invoice and PO numbers in file names were taken out as if they were dates, so each month's
# separate invoice was "Another version of" the last one, and the whole matter compared two different invoices.
# Excel's and Google's own names for a new file ("Book1.xlsx") made two colleagues' unrelated files versions too.
@pytest.mark.parametrize(("name", "stem"), [
    ("Coastal_Invoice_20481234.pdf", "coastal invoice 20481234.pdf"),
    ("Purchase Order 2026-1101.pdf", "purchase order 2026 1101.pdf"),
    ("Acme quote 1-2-345.pdf", "acme quote 1 2 345.pdf"),
    ("Budget 2026-10-31.xlsx", "budget.xlsx"),
    ("Budget 20261031.xlsx", "budget.xlsx"),
    ("Budget 31.10.2026.xlsx", "budget.xlsx"),
    ("Book1.xlsx", ""), ("Presentation1.pptx", ""), ("New Microsoft Excel Worksheet.xlsx", ""), ("Untitled spreadsheet.xlsx", ""),
    ("Copy of Lease schedule.xlsx", "lease schedule.xlsx"),
])
def test_a_number_or_a_default_name_in_a_files_name_is_no_version_mark(name, stem):
    assert links.file_stem(name) == stem


# Guards against: a number's key drops its leading zeros, and a key under four characters was left out, so
# FreshBooks' "Invoice 0000123" got no invoice key and its reminder never showed as the same invoice.
def test_a_zero_padded_invoice_number_is_kept():
    assert ("invoice", "northwind.com|123") in links.keys(_record("a", invoices=["0000123"]))
    assert not [key for key in links.keys(_record("b", invoices=["123"])) if key[0] == "invoice"]


# Guards against: a file with an ordinary name ("Invoice.pdf", "Scan_0001.pdf") got no "file" key at all, so the
# vendor's invoice and a colleague's forward of the very same PDF weren't shown as the same file.
def test_a_file_with_an_ordinary_name_is_still_the_same_file(store):
    for name in ("Invoice.pdf", "Scan_0001.pdf"):
        sha = (name[0] * 64).lower()
        store.upsert_email(_record(f"vendor-{name}", files=[(name, sha, 80_000, None)]))
        store.upsert_email(_record(f"forward-{name}", sender="maya@taz.com", files=[(name, sha, 80_000, None)], received="2026-10-06T14:00:00+00:00"))
        assert _groups(store, f"vendor-{name}") == [(f"Same file: {name}", [f"forward-{name}"])]
    assert not [key for key in links.keys(_record("x", files=[("ATT00001.htm", "c" * 64, 3_000, None)])) if key[0] == "file"]


# Guards against: a conversation chained by In-Reply-To alone was followed four replies out, so the thread shown
# (and the invoice checks' conversation) depended on which email was opened.
def test_a_chain_of_replies_naming_only_their_parent_is_followed_to_its_ends(store):
    for n in range(8):
        store.upsert_email(_record(f"e{n}", mid=f"<m{n}@n.com>", threading={"in_reply_to": f"m{n - 1}@n.com"} if n else {},
                                   received=f"2026-10-0{1 + n}T09:00:00+00:00"))
    for opened in ("e0", "e7"):
        assert [e.id for e in links.related(store, store.get_email(opened), ai=False)["thread"]] == [f"e{n}" for n in range(8)], opened


# Guards against: a copier gives every scan the same Message-ID, and once a colleague replied to one scan, the
# next round of a conversation took in every scan carrying that id: all ten were "This conversation".
def test_a_reply_to_one_scan_makes_no_conversation_of_all_the_scans(store):
    for n in range(10):
        store.upsert_email(_record(f"scan{n}", sender="scanner@taz.com", mid="<scan@copier.local>", received=f"2026-10-0{1 + n % 9}T0{n % 10}:00:00+00:00"))
    store.upsert_email(_record("reply", sender="maya@taz.com", mid="<r@taz.com>", threading={"in_reply_to": "scan@copier.local"}, received="2026-10-09T12:00:00+00:00"))
    assert {e.id for e in links.related(store, store.get_email("scan0"), ai=False)["thread"]} == {"scan0", "reply"}
    # A lookalike claiming the vendor's Message-ID is still found beside it, to be marked.
    store.upsert_email(_record("real", sender="ar@coastalprint.com", mid="<cp1@coastalprint.com>", threading={"to": ["ap@taz.com"]}))
    store.upsert_email(_record("fake", sender="ar@coastalprlnt.com", mid="<cp1@coastalprint.com>", received="2026-10-06T09:00:00+00:00"))
    store.upsert_email(_record("ours", sender="ap@taz.com", mid="<o@taz.com>", threading={"in_reply_to": "cp1@coastalprint.com"}, received="2026-10-07T09:00:00+00:00"))
    found = links.related(store, store.get_email("ours"), ai=False)
    assert {e.id for e in found["thread"]} == {"real", "fake", "ours"} and found["new_domains"] == {"fake"}


# Guards against: the background catch-up read a batch of emails before it took the write lock. An email held as
# fraud from the web page in between was filed again from what it was before, with the files a held email never
# links, so the real vendor's email showed the held one under "Same file".
def test_catching_up_reads_its_batch_with_the_write_lock_held(store, monkeypatch):
    import sqlite3

    from controller_inbox.store import Store

    store.upsert_email(_record("a", invoices=["INV-1"]))
    store.upsert_email(_record("b", invoices=["INV-2"]))
    with store.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO sync_state(key, value) VALUES ('link_keys_version', 'old')")
    assert store.file_link_keys(batch=1) == 1
    original, writable = Store.get_emails, []

    def read_then_try_to_write(self, ids, *, conn=None):
        other = sqlite3.connect(store.path, timeout=0)
        try:
            other.execute("BEGIN IMMEDIATE")
            writable.append(True)
            other.rollback()
        except sqlite3.OperationalError:
            writable.append(False)
        finally:
            other.close()
        return original(self, ids, conn=conn)

    monkeypatch.setattr(Store, "get_emails", read_then_try_to_write)
    assert store.file_link_keys(batch=500) == 1
    assert writable == [False], "another writer could change the emails between their read and their filing"


# Guards against: a vendor's billing system answering from billing.northwind.com in the thread the vendor started,
# or a colleague from uk.taz.com, was marked "New in this conversation", and the model told to check who it is.
def test_a_subdomain_of_a_party_is_no_newcomer():
    root = _record("root", sender="priya@northwind.com", mid="<root@northwind.com>", threading={"to": ["ap@taz.com"]})
    billing = _record("billing", sender="noreply@billing.northwind.com", mid="<b1@billing.northwind.com>", received="2026-10-05T15:00:00+00:00",
                      threading={"in_reply_to": "root@northwind.com", "to": ["ap@taz.com"]})
    uk = _record("uk", sender="tom@uk.taz.com", mid="<u1@uk.taz.com>", received="2026-10-05T16:00:00+00:00",
                 threading={"in_reply_to": "root@northwind.com", "to": ["priya@northwind.com"]})
    lookalike = _record("fake", sender="priya@northwind.com.billing-desk.net", mid="<f1@billing-desk.net>", received="2026-10-05T17:00:00+00:00",
                        threading={"in_reply_to": "root@northwind.com", "to": ["ap@taz.com"]})
    for n, record in enumerate((root, billing, uk, lookalike)):
        record.created_at = f"2026-10-05T18:0{n}:00+00:00"
    assert links.new_domains([root, billing, uk, lookalike]) == {"fake"}


# Guards against: the Whole matter page listed a lookalike's reply only as "In the conversation", with none of
# the "New in this conversation" warning Related gives it.
def test_the_whole_matter_marks_an_email_new_to_its_conversation(store, settings):
    from fastapi.testclient import TestClient
    from helpers import PAGE

    from controller_inbox.web import create_app

    store.upsert_email(_record("root", sender="priya@northwind.com", mid="<root@northwind.com>", threading={"to": ["ap@taz.com"]}))
    store.upsert_email(_record("hijack", sender="priya@northwnd.com", mid="<h@northwnd.com>", received="2026-10-06T09:00:00+00:00",
                               threading={"in_reply_to": "root@northwind.com", "to": ["ap@taz.com"]}))
    assert links.trace(store, store.get_email("root"))["new_domains"] == {"hijack"}
    steps = TestClient(create_app(settings, store), headers=PAGE).get("/api/trace/root").json()["steps"]
    assert {step["id"]: step["new_domain"] for step in steps} == {"root": False, "hijack": True}
