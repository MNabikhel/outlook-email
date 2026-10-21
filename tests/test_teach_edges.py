"""Teach a field (teach.py) in the cases found in review: undo, spread, done tasks, amounts and dates as printed,
labels above a column or at the start of a longer label, short ids, the vendor, and taking a field back."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from controller_inbox import teach, web
from controller_inbox.folder_mail import ingest_folder
from controller_inbox.models import ActionItem, ActionStatus, Importance
from pdffactory import Text, build_pdf
from test_teach_and_preview import PAGE, SENDER, _box, _drop, _region, _teach, client, mail  # noqa: F401  (fixtures)

AS_OF = date(2026, 10, 20)


def _undo(client, email, fix_id):
    reply = client.post(f"/api/mail/{email.id}/files/1/pages/1/fixes/{fix_id}/undo", headers=PAGE)
    assert reply.status_code == 200, reply.text
    return reply.json()


# Undo -------------------------------------------------------------------------------------------------


def test_undo_of_a_taught_invoice_number_goes_back_to_what_was_read(client, store, mail):
    """with_taught moves the value to the front of invoice_numbers; without_taught never takes it out again."""
    email = mail["Your Keystone invoice"]
    assert store.get_email(email.id).extracted.primary_invoice is None, "the scripts read no invoice number"
    reply = _teach(client, email, "invoice_number", "KP-55120")
    assert "isn't learnt" in _undo(client, email, reply["id"])["message"]
    stored = store.get_email(email.id)
    assert "invoice_number" not in stored.extracted.taught
    assert stored.extracted.primary_invoice is None, f"still {stored.extracted.primary_invoice!r} after Undo"


def test_undo_of_a_taught_due_date_goes_back_to_what_was_read(client, store, mail):
    email = mail["Your Keystone invoice"]
    assert store.get_email(email.id).extracted.primary_due is None
    reply = _teach(client, email, "due_date", "11/15/2026")
    _undo(client, email, reply["id"])
    stored = store.get_email(email.id)
    assert stored.extracted.primary_due is None, f"still due {stored.extracted.primary_due!r} after Undo"
    assert all(a.due_date != "2026-11-15" for a in stored.actions if a.status == ActionStatus.OPEN)


def test_undo_takes_back_what_was_spread_to_the_senders_other_emails(client, store, mail):
    """Undo says the field "isn't learnt for this sender", yet the other email keeps the learnt value."""
    email, other = mail["Your Keystone invoice"], mail["Another Keystone invoice"]
    reply = _teach(client, email, "total", "$2,118.40")
    assert store.get_email(other.id).extracted.taught["total"]["by"] == "learnt"
    _undo(client, email, reply["id"])
    assert teach.templates(store, SENDER) == []
    after = store.get_email(other.id)
    assert "total" not in after.extracted.taught, f"still learnt: {after.extracted.taught}"
    assert after.extracted.primary_amount == 10000.0


def test_undo_of_the_second_teach_of_a_field_keeps_the_first(client, store, mail):
    """Teaching the total twice leaves two field fixes; undoing the newer one forgets the field altogether."""
    email = mail["Your Keystone invoice"]
    first = _teach(client, email, "total", "$2,118.40")
    second = _teach(client, email, "total", "$10,000.00")
    _undo(client, email, second["id"])
    view = client.get(f"/api/mail/{email.id}/files/1/pages/1/regions", headers=PAGE).json()
    assert [t["id"] for t in view["taught"]] == [first["id"]], "the first fix is still drawn on the page"
    assert teach.templates(store, SENDER)[0]["label"] == "Balance payable", "and still learnt"
    stored = store.get_email(email.id)
    assert stored.extracted.primary_amount == 2118.40, f"email uses {stored.extracted.primary_amount} with a fix for 2118.40 still kept"


# Refiling -----------------------------------------------------------------------------------------


def test_a_task_marked_done_is_not_reopened_by_spread(client, store, mail):
    """refile promises "tasks already done stay done", but the task title carries the amount, so a new amount
    makes a fresh open task on the other email the user had already finished."""
    email, other = mail["Your Keystone invoice"], store.get_email(mail["Another Keystone invoice"].id)
    assert [a.status for a in other.actions] == [ActionStatus.OPEN]
    store.set_action_status(other.actions[0].id, "done")
    _teach(client, email, "total", "$2,118.40")
    after = store.get_email(other.id)
    reopened = [a.title for a in after.actions if a.status == ActionStatus.OPEN]
    assert reopened == [], f"done task came back as {reopened}"


def test_teaching_keeps_the_local_models_tasks(client, store, mail):
    """refile keeps a model reading's folder and summary but replaces its tasks with the script's."""
    email = store.get_email(mail["Your Keystone invoice"].id)
    model_task = ActionItem(
        id="model-task-1", email_id=email.id, title="Call Keystone about the short-shipped cases", detail="",
        due_date="2026-10-25", priority=Importance.HIGH, status=ActionStatus.OPEN, source="model",
        created_at="2026-10-20T12:00:00+00:00",
    )
    email.actions = [model_task]
    email.folder, email.summary, email.model_status = "important", "Keystone short-shipped; call them.", "bionic"
    store.upsert_email(email)
    _teach(client, email, "total", "$2,118.40")
    titles = [a.title for a in store.get_email(email.id).actions]
    assert "Call Keystone about the short-shipped cases" in titles, titles


# Reading later files with a learnt label ----------------------------------------------------------


def test_a_learnt_label_isnt_matched_at_the_start_of_a_longer_label():
    """"Total" learnt; the next invoice prints "Total Net" and "Total VAT" above "Total"."""
    text = "Total Net | £1,000.00\nTotal VAT | £200.00\nTotal | £1,200.00"
    got = teach.read_with(text, [{"field": "total", "label": "Total", "position": "right"}], as_of=AS_OF)
    assert got["total"]["value"] == "1200.0", got


def test_a_learnt_total_ignores_a_date_later_on_its_line():
    """The last positive number on the rest of the line wins, so a date's year is taken as the total."""
    text = "Amount due | $640.00 | by 11/30/2026"
    got = teach.read_with(text, [{"field": "total", "label": "Amount due", "position": "right"}], as_of=AS_OF)
    assert got["total"]["value"] == "640.0", got


@pytest.mark.parametrize(
    "field, want",
    [("due_date", "2026-11-15"), ("total", "1250.0")],
)
def test_a_label_above_a_column_reads_that_column(field, want):
    """pdf_layout joins a row's cells with " | ", so the text columns don't line up with the labels'; a short
    heading row reads the first cell(s) instead."""
    text = "[page 1]\n[notes]\nDate | Due | Total\n10/01/2026 | 11/15/2026 | $1,250.00"
    label = {"due_date": "Due", "total": "Total"}[field]
    got = teach.read_with(text, [{"field": field, "label": label, "position": "below"}], as_of=AS_OF)
    assert got[field]["value"] == want, got


# Value parsing ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field, text, want",
    [
        ("total", "EUR 1.234,56", "1234.56"),
        ("total", "€1.234,56", "1234.56"),
        ("total", "$4,210.00 incl. 20% VAT", "4210.0"),
        ("invoice_number", "$2,118.40", None),
        ("invoice_number", "KP-1", "KP-1"),
        ("invoice_number", "INV-7", "INV-7"),
        ("due_date", "15-Nov-2026", "2026-11-15"),
        ("due_date", "2026/11/15", "2026-11-15"),
    ],
)
def test_value_of_reads_the_value_as_printed(field, text, want):
    assert teach.value_of(field, text, as_of=AS_OF) == want


def _header_invoice(ref: str) -> bytes:
    items = [Text(54, 740, "Keystone Paper Co", size=17, bold=True)]
    items += [Text(54, 650, "Invoice No", size=10, bold=True), Text(330, 650, "Amount Due", size=10, bold=True)]
    items += [Text(54, 634, ref, size=10), Text(330, 634, "$1,250.00", size=10)]
    return build_pdf([items])


def test_a_short_invoice_number_can_be_taught(store, settings):
    settings.trusted_domains = "keystonepaper.com"
    settings.ensure_data_dir()
    _drop(settings, "short", "Keystone KP-7", _header_invoice("KP-7"), "KP-7.pdf")
    ingest_folder(store, settings)
    email = store.list_emails(limit=5)[0]
    reply = _teach(TestClient(web.create_app(settings, store)), email, "invoice_number", "KP-7", status=200)
    assert store.get_email(email.id).extracted.primary_invoice == "KP-7", reply


# Input validation ---------------------------------------------------------------------------------


def test_a_bad_box_gets_a_sentence_not_pythons_error(client, mail):
    email = mail["Your Keystone invoice"]
    reply = client.post(
        f"/api/mail/{email.id}/files/1/pages/1/fixes", headers=PAGE,
        json={"kind": "field", "field": "total", "box": {"x": "abc", "y": 0, "w": 1, "h": 1}},
    )
    assert reply.status_code == 400
    assert "convert" not in reply.json()["detail"], reply.json()["detail"]


# New files on a model-read email -----------------------------------------------------------------


def test_a_file_added_to_a_model_read_email_is_read_with_what_was_learnt(client, store, settings, mail):
    """process_message's _add_files path (an email the model read, or the user corrected) skips _read_as_taught."""
    from test_teach_and_preview import _invoice

    _teach(client, mail["Your Keystone invoice"], "total", "$2,118.40")
    other = store.get_email(mail["Another Keystone invoice"].id)
    other.model_status = "bionic"
    store.upsert_email(other)
    _drop(settings, "c", "Another Keystone invoice", _invoice("KP-55190", "$640.00", "11/20/2026"), "KP-55190.pdf",
          more=[("KP-55191.pdf", _invoice("KP-55191", "$310.00", "11/22/2026"), "application/pdf")])
    ingest_folder(store, settings)
    after = store.get_email(other.id)
    added = next(a for a in after.attachments if a.filename == "KP-55191.pdf")
    assert added.extracted_fields.taught.get("total", {}).get("value") == "310.0", added.extracted_fields.taught


# Reviewer concerns ------------------------------------------------------------------------------


def test_a_label_learnt_with_a_full_stop_is_found_with_a_colon():
    text = "Invoice No: KP-7"
    got = teach.read_with(text, [{"field": "invoice_number", "label": "Invoice No.", "position": "right"}], as_of=AS_OF)
    assert got == {"invoice_number": {"value": "KP-7", "label": "Invoice No."}}


def test_spread_leaves_a_fraud_locked_email_alone(client, store, mail, monkeypatch):
    from controller_inbox import fraud

    email, other = mail["Your Keystone invoice"], mail["Another Keystone invoice"]
    monkeypatch.setattr(fraud, "attachments_locked", lambda e: e.id == other.id)
    reply = _teach(client, email, "total", "$2,118.40")
    assert "other email" not in reply["message"]
    assert "total" not in store.get_email(other.id).extracted.taught


def test_spread_builds_the_trust_context_once(store, settings, mail, monkeypatch):
    from controller_inbox import fraud

    from test_teach_and_preview import _invoice

    email = mail["Your Keystone invoice"]
    for name in ("b", "d"):
        _drop(settings, name, f"Keystone {name}", _invoice(f"KP-600{name}", "$310.00", "11/22/2026"), f"{name}.pdf")
    ingest_folder(store, settings)
    store.add_page_fix({"kind": "field", "x": 0.6, "y": 0.6, "w": 0.1, "h": 0.02, "was": "$2,118.40", "now": "total",
                        "anchor": {"label": "Balance payable", "position": "right", "value": "2118.4"},
                        "attachment_id": email.attachments[0].id, "sha256": "", "page": 1, "sender": SENDER})
    calls = []
    real = fraud.trust_context
    monkeypatch.setattr(fraud, "trust_context", lambda *a, **k: calls.append(1) or real(*a, **k))
    assert teach.spread(store, settings, SENDER, not_email=email.id, as_of=AS_OF) == 3
    assert len(calls) == 1


# Teaching the vendor --------------------------------------------------------------------------------


def _vendor_invoice(ref: str) -> bytes:
    """Keystone's invoice with the vendor's name as a big heading of its own, and "INVOICE" beside it."""
    items = [Text(54, 740, "Acme Industrial Supply", size=20, bold=True), Text(470, 740, "INVOICE", size=20, bold=True)]
    items += [Text(54, 718, "Acme AR, 12 Mill Road, Albany NY", size=9.5)]
    items += [Text(380, 700, "Ref #", size=10, bold=True), Text(558, 700, ref, size=10, right=True)]
    items += [Text(380, 560, "Balance payable", size=10, bold=True), Text(558, 560, "$2,118.40", size=10, bold=True, right=True)]
    return build_pdf([items])


def test_the_vendor_isnt_learnt_as_its_own_label_and_undo_puts_it_back(store, settings):
    settings.trusted_domains = "keystonepaper.com"
    settings.ensure_data_dir()
    _drop(settings, "v", "Keystone V-1", _vendor_invoice("KP-7001"), "KP-7001.pdf")
    ingest_folder(store, settings)
    email = store.list_emails(limit=5)[0]
    was = list(email.extracted.vendor_candidates)
    client = TestClient(web.create_app(settings, store))
    reply = _teach(client, email, "vendor", "Acme Industrial Supply")
    assert "Acme Industrial Supply”" not in reply["message"] and "for this file only" in reply["message"], reply["message"]
    assert teach.templates(store, SENDER) == [], "nothing printed beside it to read the next invoice by"
    stored = store.get_email(email.id)
    assert stored.extracted.vendor_candidates[0] == "Acme Industrial Supply"
    assert "isn't learnt" in _undo(client, email, reply["id"])["message"]
    after = store.get_email(email.id)
    assert "vendor" not in after.extracted.taught and after.extracted.vendor_candidates == was, after.extracted.vendor_candidates


def test_a_label_in_the_same_box_stops_where_the_value_starts():
    found = [{"x": 0.1, "y": 0.2, "w": 0.4, "h": 0.02, "text": "Pay by Nov 15, 2026"}]
    fix = teach.field_fix(found, {"x": 0.09, "y": 0.195, "w": 0.42, "h": 0.03}, "due_date", as_of=AS_OF)
    assert fix["anchor"]["label"] == "Pay by", fix["anchor"]


# On the page, in a browser ---------------------------------------------------------------------------

from test_workspace import _harbor, _open_page_tab, browser, page  # noqa: E402,F401  (fixtures)


def test_a_field_taught_inside_a_table_can_be_taken_back_with_the_mouse(settings, store, page):
    from liveserver import serving
    from msgfactory import PDF
    from test_page_view import _detailed_invoice

    email = _harbor(settings, store, ("HS-10482.pdf", _detailed_invoice(), PDF))
    app = web.create_app(settings, store)
    _teach(TestClient(app), email, "total", "1,874.40")
    with serving(app) as base:
        _open_page_tab(page, base, email)
        mark = page.locator(".pv-taught")
        assert mark.count() == 1 and page.locator(".pv-tbl").count() >= 1
        page.evaluate("() => document.querySelector('.pv-taught').scrollIntoView({ block: 'center' })")
        # Its tag isn't hidden under the table's, and nothing of the table's covers the box.
        on_top = page.evaluate(
            """() => [".pv-taught-tag", ".pv-taught"].map((sel) => {
                const r = document.querySelector(sel).getBoundingClientRect();
                const el = document.elementFromPoint(r.left + Math.min(r.width / 2, 8), r.top + r.height / 2);
                return el && el.closest(".pv-taught") ? true : (el ? el.className : "none");
            })"""
        )
        assert on_top == [True, True]
        # Drawn at the table's top left corner, its tag is over the table's own tag ("Line items"), not under it.
        over = page.evaluate(
            """() => {
                const mark = document.querySelector(".pv-taught"), outline = document.querySelector(".pv-tbl");
                const before = [mark.style.left, mark.style.top];
                [mark.style.left, mark.style.top] = [outline.style.left, outline.style.top];
                mark.scrollIntoView({ block: "center" });
                const r = mark.querySelector(".pv-taught-tag").getBoundingClientRect();
                const el = document.elementFromPoint(r.left + 6, r.top + r.height / 2);
                [mark.style.left, mark.style.top] = before;
                return el && el.closest(".pv-taught") ? true : (el ? el.className : "none");
            }"""
        )
        assert over is True, over
        # The tag reads at 4.5:1 or better in both themes.
        for scheme in ("light", "dark"):
            page.emulate_media(color_scheme=scheme)
            ratio = page.evaluate(
                """() => {
                    const style = getComputedStyle(document.querySelector(".pv-taught-tag"));
                    const lum = (rgb) => {
                        const [r, g, b] = rgb.match(/[\\d.]+/g).slice(0, 3).map((v) => v / 255).map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
                        return 0.2126 * r + 0.7152 * g + 0.0722 * b;
                    };
                    const [a, b] = [lum(style.color), lum(style.backgroundColor)].sort((x, y) => y - x);
                    return (a + 0.05) / (b + 0.05);
                }"""
            )
            assert ratio >= 4.5, (scheme, ratio)
        page.emulate_media(color_scheme="light")
        # A scroll closes the tip, so the box is brought into view (and the scroll settled) before it's clicked.
        mark.scroll_into_view_if_needed()
        page.evaluate("() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))")
        mark.click()
        page.locator(".pv-tip:not([hidden]) button:text-is('Take it back')").click()
        page.wait_for_function("() => !document.querySelector('.pv-taught')")
    assert "total" not in store.get_email(email.id).extracted.taught
