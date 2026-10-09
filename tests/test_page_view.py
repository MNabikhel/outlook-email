"""The Page tab: a PDF page or a picture shown as it is, with a box over each piece of text read there, how sure the
reading is, and what the vision model read in the same place. A file held as possible payment fraud is never drawn."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from controller_inbox import ocr, page_details, page_view, vision, web
from controller_inbox.folder_mail import ingest_folder
from msgfactory import PDF, write_msg
from pdffactory import Stroke, Text, build_pdf, sheet_rows

PAGE = {"X-CloseDesk": "1"}
OVIS = "ovisocr2-0.85b"

ROWS = [["Item", "Qty", "Amount"], ["Steel beams", "4", "1,250.00"], ["Bolts M12", "200", "310.50"], ["Total", "", "1,560.50"]]


def _invoice(pages: int = 1) -> bytes:
    first = [Text(72, 760, "INVOICE INV-1042", size=16, bold=True), Text(72, 740, "Harbor Steel LLC", size=11)]
    first += sheet_rows([(72, "left"), (400, "right"), (500, "right")], ROWS)
    rest = [[Text(72, 740, f"Terms and conditions, page {page}", size=11)] for page in range(2, pages + 1)]
    return build_pdf([first, *rest])


LINES = [("Structural I-beam W8x18, 20 ft", 8, 412.50), ("Steel plate 1/2 in, 4 x 8 ft", 6, 312.40), ("Anchor bolts 3/4 in galvanized", 120, 4.85), ("Delivery and handling", 1, 185.00)]


def _detailed_invoice() -> bytes:
    """An invoice as accounting programs print one: details at the top right, line items, totals under them."""
    items = [Text(54, 740, "Harbor Steel LLC", size=17, bold=True), Text(54, 722, "88 Dockside Way, Tacoma WA 98421", size=9.5)]
    items.append(Text(558, 740, "INVOICE", size=22, bold=True, right=True))
    for i, (label, value) in enumerate([("Invoice no.", "HS-10482"), ("Invoice date", "09/28/2026"), ("Due date", "10/28/2026"), ("PO number", "4500-1163")]):
        items += [Text(400, 712 - 14 * i, label, size=9.5, bold=True), Text(558, 712 - 14 * i, value, size=9.5, right=True)]
    items += [Text(54, 650, "Bill to", size=9.5, bold=True), Text(54, 636, "Taz Construction LLC", size=9.5)]
    top = 570
    items.append(Stroke(54, top + 14, 558, top + 14))
    for x, head, right in [(58, "Description", False), (360, "Qty", True), (460, "Unit price", True), (554, "Amount", True)]:
        items.append(Text(x, top, head, size=9.5, bold=True, right=right))
    items.append(Stroke(54, top - 6, 558, top - 6))
    y = top - 22
    for desc, qty, price in LINES:
        items += [Text(58, y, desc, size=9.5), Text(360, y, f"{qty:,}", size=9.5, right=True), Text(460, y, f"{price:,.2f}", size=9.5, right=True), Text(554, y, f"{qty * price:,.2f}", size=9.5, right=True)]
        y -= 18
    for label, value in [("Subtotal", "$5,941.40"), ("Sales tax 6.5%", "$386.19"), ("Total due", "$6,327.59")]:
        y -= 10
        items += [Text(360, y, label, size=9.5, bold=label == "Total due"), Text(554, y, value, size=9.5, bold=label == "Total due", right=True)]
        y -= 6
    items.append(Text(54, 150, "Payment terms: net 30. Please include the invoice number with your payment.", size=9))
    return build_pdf([items])


def _scan_png() -> bytes:
    """The invoice as a scanner gives it: a picture of the page, no text."""
    return vision.render(_invoice(), "invoice.pdf", 1, reader=page_view.VIEW)


@pytest.fixture
def files(store, settings, monkeypatch):
    # Reading the picture while it arrives isn't what these tests are about.
    monkeypatch.setattr(ocr, "image_text", lambda _data: "")
    settings.trusted_domains = "taz.com"
    settings.ensure_data_dir()
    write_msg(
        settings.inbox_incoming / "invoice.msg",
        "Invoice INV-1042",
        "Our invoice and the signed delivery note are attached.",
        sender_name="Harbor Steel",
        sender_email="ar@taz.com",
        attachments=[("invoice.pdf", _invoice(pages=2), PDF), ("delivery note.png", _scan_png(), "image/png")],
    )
    write_msg(
        settings.inbox_incoming / "harbor.msg",
        "Invoice HS-10482",
        "Please find invoice HS-10482 attached, and a scan of the signed copy.",
        sender_name="Harbor Steel",
        sender_email="ar@taz.com",
        attachments=[
            ("HS-10482.pdf", _detailed_invoice(), PDF),
            ("HS-10482 signed.png", vision.render(_detailed_invoice(), "HS-10482.pdf", 1, reader=page_view.VIEW), "image/png"),
        ],
    )
    write_msg(
        settings.inbox_incoming / "scam.msg",
        "Updated remittance details",
        "Our bank details have changed. Please use the following account for all payments from today.",
        sender_name="Acme Billing",
        sender_email="billing@acme-payments.net",
        attachments=[("new bank letter.pdf", _invoice(), PDF)],
    )
    return {email.subject: email for email in ingest_folder(store, settings)}


@pytest.fixture
def client(store, settings, files):
    return TestClient(web.create_app(settings, store))


def _get(client, path: str):
    response = client.get(path, headers=PAGE)
    assert response.status_code == 200, (path, response.status_code, response.text[:300])
    return response


def _n(email, filename: str) -> int:
    return [att.filename for att in email.attachments].index(filename) + 1


def _regions(client, email, name: str, page: int = 1) -> dict:
    return _get(client, f"/api/mail/{email.id}/files/{_n(email, name)}/pages/{page}/regions").json()


def test_a_pdf_page_and_a_picture_are_drawn_as_pngs(client, files):
    invoice = files["Invoice INV-1042"]
    pdf, png = _n(invoice, "invoice.pdf"), _n(invoice, "delivery note.png")
    for n, page in ((pdf, 1), (pdf, 2), (png, 1)):
        response = _get(client, f"/api/mail/{invoice.id}/files/{n}/pages/{page}.png")
        assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
        assert response.headers["content-type"] == "image/png"
        assert response.headers["x-content-type-options"] == "nosniff"
    assert client.get(f"/api/mail/{invoice.id}/files/{pdf}/pages/3.png", headers=PAGE).status_code == 404
    assert client.get(f"/api/mail/{invoice.id}/files/{png}/pages/2.png", headers=PAGE).status_code == 404
    assert client.get(f"/api/mail/{invoice.id}/files/{pdf}/pages/1.png").status_code == 403, "only CloseDesk's own page"
    detail = _get(client, f"/api/mail/{invoice.id}").json()
    assert [file["preview"] for file in detail["attachments"]] == [True, True]
    assert _get(client, f"/api/mail/{invoice.id}/files/{pdf}").json()["file"]["preview"] is True


def test_a_pdf_page_with_text_is_marked_from_its_own_text(client, files):
    view = _regions(client, files["Invoice INV-1042"], "invoice.pdf")
    assert view["source"] == "text" and view["pages"] == 2 and view["model_name"] == ""
    assert view["width"] > 1000 and view["height"] > view["width"]
    texts = [region["text"] for region in view["regions"]]
    assert {"INVOICE INV-1042", "Steel beams", "1,250.00", "310.50", "1,560.50"} <= set(texts)
    for region in view["regions"]:
        assert region["confidence"] is None and region["model"] is None
        assert 0 <= region["x"] <= region["x"] + region["w"] <= 1 and 0 <= region["y"] <= region["y"] + region["h"] <= 1
    title = view["regions"][texts.index("INVOICE INV-1042")]
    total = view["regions"][texts.index("1,560.50")]
    assert title["y"] < 0.05 and title["x"] < 0.15, "the title is printed at the top left"
    assert total["x"] > 0.7 and total["y"] > title["y"]
    assert [region["text"] for region in _regions(client, files["Invoice INV-1042"], "invoice.pdf", 2)["regions"]] == ["Terms and conditions, page 2"]


def test_a_turned_page_is_marked_where_it_shows():
    data = build_pdf([[Text(72, 760, "INVOICE INV-1042 printed sideways on the page", size=16)]]).replace(b"/MediaBox [0 0 612 792]", b"/MediaBox [0 0 612 792] /Rotate 90")
    [region] = page_view._text_layer(data, 1)
    # Turned clockwise, the top left corner of the page as stored is now at the top right.
    assert region["x"] > 0.9 and region["y"] < 0.2 and region["h"] > region["w"]


def test_a_scanned_picture_is_read_with_ocr_and_says_how_sure(client, files):
    if ocr.engine_name() != "RapidOCR":
        pytest.skip("RapidOCR isn't installed")
    view = _regions(client, files["Invoice INV-1042"], "delivery note.png")
    assert view["source"] == "ocr" and view["pages"] == 1 and view["reason"] == ""
    found = {region["text"]: region for region in view["regions"]}
    assert "1,250.00" in found and "Steel beams" in found
    assert all(0 < region["confidence"] <= 1 for region in view["regions"])
    assert found["1,250.00"]["x"] > 0.7 and 0 < found["1,250.00"]["y"] < 0.2


def test_without_rapidocr_a_scan_still_shows_its_page(client, files, monkeypatch):
    monkeypatch.setattr(ocr, "engine_name", lambda: "")
    invoice = files["Invoice INV-1042"]
    view = _regions(client, invoice, "delivery note.png")
    assert view["regions"] == [] and "RapidOCR isn't installed" in view["reason"]
    _get(client, f"/api/mail/{invoice.id}/files/{_n(invoice, 'delivery note.png')}/pages/1.png")


def test_a_page_is_read_once(client, files, monkeypatch):
    calls = []

    def boxes(png):
        calls.append(len(png))
        return [{"left": 100.0, "top": 50.0, "right": 300.0, "bottom": 80.0, "text": "INVOICE INV-1042", "score": 0.72}]

    monkeypatch.setattr(ocr, "line_boxes", boxes)
    invoice = files["Invoice INV-1042"]
    first = _regions(client, invoice, "delivery note.png")
    again = _regions(client, invoice, "delivery note.png")
    assert calls and len(calls) == 1, "the second look comes from what was kept"
    assert first == again and first["regions"][0]["confidence"] == 0.72
    assert 0 < first["regions"][0]["x"] < first["regions"][0]["x"] + first["regions"][0]["w"] < 1


def test_each_box_says_whether_the_vision_model_read_the_same(client, store, files):
    invoice = files["Invoice INV-1042"]
    att = invoice.attachments[_n(invoice, "invoice.pdf") - 1]
    # OvisOCR2's reading: 1,250.00 written without its comma (the same figure), 310.50 read as 301.50.
    reading = (
        "# INVOICE INV-1042\n\n<table><tr><td>Item</td><td>Qty</td><td>Amount</td></tr>"
        "<tr><td>Steel beams</td><td>4</td><td>1250.00</td></tr><tr><td>Bolts M12</td><td>200</td><td>301.50</td></tr>"
        "<tr><td>Total</td><td></td><td>1,560.50</td></tr></table>"
    )
    store.save_page_reading(att.id, 1, first=att.extracted_text, model_text=reading, model=OVIS, seconds=40, comparison="{}", sha256=att.sha256)
    view = _regions(client, invoice, "invoice.pdf")
    assert view["model_name"] == "OvisOCR2"
    found = {region["text"]: region["model"] for region in view["regions"]}
    assert found["1,250.00"] == {"text": "Steel beams | 4 | 1250.00", "agrees": True}
    assert found["310.50"] == {"text": "Bolts M12 | 200 | 301.50", "agrees": False}
    assert found["1,560.50"]["agrees"] is True
    assert found["Steel beams"] == {"text": "Steel beams", "agrees": True}
    assert found["Harbor Steel LLC"] == {"text": None, "agrees": None}, "not in its reading"
    # The other page wasn't read by the model.
    assert _regions(client, invoice, "invoice.pdf", 2)["regions"][0]["model"] is None


def test_the_files_of_a_suspected_fraud_email_are_never_drawn(client, settings, files):
    scam = files["Updated remittance details"]
    assert _get(client, f"/api/mail/{scam.id}").json()["locked"] is True
    for path in ("pages/1.png", "pages/1/regions"):
        refused = client.get(f"/api/mail/{scam.id}/files/1/{path}", headers=PAGE)
        assert refused.status_code == 403 and "payment fraud" in refused.json()["detail"]
    assert not page_view.folder(settings).exists() or not any(page_view.folder(settings).iterdir())


def test_a_reading_kept_by_an_older_version_is_read_again(settings):
    data = _invoice()
    page_view.folder(settings).mkdir(parents=True, exist_ok=True)
    stale = {"version": 0, "source": "text", "regions": [], "reason": ""}
    (page_view.folder(settings) / f"{page_view.file_hash(data)}-1.json").write_text(json.dumps(stale))
    assert page_view.regions(settings, data, "invoice.pdf", 1)["regions"]


# Tables and key details on the page ------------------------------------------------------------------------


def _cell(view: dict, table: dict, row: int, column: int) -> str | None:
    """The text of the box a table's cell was tied to, or None."""
    index = table["rows"][row][column]["region"]
    return None if index is None else view["regions"][index]["text"]


def test_a_pdf_page_shows_its_tables_cell_by_cell(client, files):
    view = _regions(client, files["Invoice HS-10482"], "HS-10482.pdf")
    tables = {table["label"]: table for table in view["tables"]}
    items = tables["Line items"]
    assert items["columns"] == ["Description", "Qty", "Unit price", "Amount"]
    assert [view["regions"][index]["text"] for index in items["column_regions"]] == items["columns"]
    first = [cell["text"] for cell in items["rows"][0]]
    assert first == ["Structural I-beam W8x18, 20 ft", "8", "412.50", "3,300.00"]
    assert [_cell(view, items, 0, column) for column in range(4)] == first
    # "185.00" is printed twice on the delivery line: each cell is tied to its own column's box.
    delivery = items["rows"][3]
    unit, amount = (view["regions"][delivery[column]["region"]] for column in (2, 3))
    assert unit["text"] == amount["text"] == "185.00" and unit["x"] < amount["x"]
    assert _cell(view, items, 2, 1) == "120"
    # The rows go down the page, and the table's outline holds every cell tied to a box.
    tops = [view["regions"][row[0]["region"] if row[0]["region"] is not None else row[1]["region"]]["y"] for row in items["rows"]]
    assert tops == sorted(tops)
    box = items["box"]
    for row in items["rows"]:
        for cell in row:
            if cell["region"] is not None:
                region = view["regions"][cell["region"]]
                assert box["x"] <= region["x"] and region["x"] + region["w"] <= box["x"] + box["w"] + 1e-6
                assert box["y"] <= region["y"] and region["y"] + region["h"] <= box["y"] + box["h"] + 1e-6
    total = next(row for row in items["rows"] if any(cell["text"] == "Total due" for cell in row))
    assert view["regions"][total[3]["region"]]["text"] == "$6,327.59"


def test_a_pdf_page_shows_its_key_details_where_they_are_printed(client, files):
    view = _regions(client, files["Invoice HS-10482"], "HS-10482.pdf")
    fields = {field["label"]: field for field in view["fields"]}
    assert list(fields) == ["Invoice no.", "Invoice date", "Due date", "PO number", "Vendor", "Subtotal", "Tax", "Total due"]
    for label, value in [("Invoice no.", "HS-10482"), ("Due date", "10/28/2026"), ("Vendor", "Harbor Steel LLC"), ("Tax", "$386.19"), ("Total due", "$6,327.59")]:
        assert fields[label]["value"] == value
        assert view["regions"][fields[label]["region"]]["text"] == value
    number = view["regions"][fields["Invoice no."]["region"]]
    label = next(region for region in view["regions"] if region["text"] == "Invoice no.")
    assert abs(number["y"] - label["y"]) < 0.01 and number["x"] > label["x"], "the value beside its label"


def test_a_page_without_a_table_has_none(client, files):
    view = _regions(client, files["Invoice INV-1042"], "invoice.pdf", 2)
    assert view["tables"] == [] and view["fields"] == []


def test_a_scans_tables_come_from_the_vision_models_reading_tied_to_ocr_boxes(client, store, files):
    if ocr.engine_name() != "RapidOCR":
        pytest.skip("RapidOCR isn't installed")
    email = files["Invoice HS-10482"]
    att = email.attachments[_n(email, "HS-10482 signed.png") - 1]
    # The model read the plate's amount as 1,847.40; OCR read the page's 1,874.40.
    rows = "".join(
        f"<tr><td>{desc}</td><td>{qty}</td><td>{price:,.2f}</td><td>{'1,847.40' if qty == 6 else f'{qty * price:,.2f}'}</td></tr>" for desc, qty, price in LINES
    )
    reading = (
        "# Harbor Steel LLC\n\nInvoice no.: HS-10482\nDue date: 10/28/2026\n\n"
        f"<table><tr><td>Description</td><td>Qty</td><td>Unit price</td><td>Amount</td></tr>{rows}"
        "<tr><td>Subtotal</td><td></td><td></td><td>$5,941.40</td></tr><tr><td>Total due</td><td></td><td></td><td>$6,327.59</td></tr></table>"
    )
    store.save_page_reading(att.id, 1, first="", model_text=reading, model=OVIS, seconds=30, comparison="{}", sha256=att.sha256)
    view = _regions(client, email, "HS-10482 signed.png")
    assert view["source"] == "ocr"
    [items] = view["tables"]
    assert items["label"] == "Line items" and items["columns"] == ["Description", "Qty", "Unit price", "Amount"]
    assert len(items["rows"]) == 6
    assert _cell(view, items, 0, 2) == "412.50" and _cell(view, items, 0, 3) == "3,300.00"
    # The figure the two read differently is tied by its row and column to OCR's box, which shows it differs.
    plate = items["rows"][1][3]
    assert plate["text"] == "1,847.40"
    assert view["regions"][plate["region"]]["text"] == "1,874.40"
    assert view["regions"][plate["region"]]["model"]["agrees"] is False
    fields = {field["label"]: field for field in view["fields"]}
    assert fields["Invoice no."]["value"] == "HS-10482" and fields["Total due"]["value"] == "$6,327.59"
    assert view["regions"][fields["Total due"]["region"]]["text"].endswith("6,327.59")


def test_a_scan_not_read_by_the_model_shows_a_table_only_where_ocr_lines_up(client, files):
    if ocr.engine_name() != "RapidOCR":
        pytest.skip("RapidOCR isn't installed")
    view = _regions(client, files["Invoice HS-10482"], "HS-10482 signed.png")
    assert view["model_name"] == ""
    [items] = view["tables"]
    assert items["label"] == "Line items" and len(items["rows"]) >= 4
    assert any(cell["text"] == "3,300.00" for cell in items["rows"][0])


def test_lines_that_dont_line_up_in_columns_are_not_made_a_table():
    def box(x, y, text, w=0.2):
        return {"x": x, "y": y, "w": w, "h": 0.015, "text": text, "confidence": 0.99}

    # An address block, a two-column list of details, and a letter's paragraphs.
    found = [box(0.08, 0.05 + 0.02 * i, f"Line {i} of the address") for i in range(4)]
    found += [box(0.6, 0.05 + 0.02 * i, label, 0.1) for i, label in enumerate(["Invoice no.", "Due date", "PO number"])]
    found += [box(0.8, 0.05 + 0.02 * i, value, 0.1) for i, value in enumerate(["NW-1", "10/30/2026", "4500-1"])]
    found += [box(0.08, 0.4 + 0.02 * i, "Thank you for your order, it ships next week as agreed.", 0.8) for i in range(6)]
    assert page_details.page_tables(found, "ocr", "", 1, None) == []


def test_a_half_written_kept_page_is_drawn_again(settings):
    """A kept page is written whole or not at all, and one cut short (a crash, a reader racing the writer) is
    drawn again rather than served."""
    data = _invoice()
    name = f"{page_view.file_hash(data)}-1.png"
    page_view.folder(settings).mkdir(parents=True, exist_ok=True)
    (page_view.folder(settings) / name).write_bytes(b"\x89PNG\r\n\x1a\n")
    png = page_view.page_png(settings, data, "invoice.pdf", 1)
    assert page_view.png_size(png)[0] > 100
    assert (page_view.folder(settings) / name).read_bytes() == png
    assert [path.name for path in page_view.folder(settings).iterdir()] == [name], "no temporary file left behind"
    (page_view.folder(settings) / name).write_bytes(png[: len(png) // 2])
    assert page_view.page_png(settings, data, "invoice.pdf", 1) == png, "cut off halfway"
