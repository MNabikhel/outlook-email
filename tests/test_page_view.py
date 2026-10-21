"""The Page tab: a PDF page or a picture shown as it is, with a box over each piece of text read there, how sure the
reading is, and what the vision model read in the same place. A file held as possible payment fraud is never drawn."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from controller_inbox import fixes, ocr, page_details, page_view, vision, web
from controller_inbox.folder_mail import ingest_folder
from helpers import PAGE
from msgfactory import PDF, write_msg
from pdffactory import Stroke, Text, build_pdf, sheet_rows

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
    # The server didn't say how sure the model was: "sure" is None throughout.
    assert found["1,250.00"] == {"text": "Steel beams | 4 | 1250.00", "agrees": True, "sure": None}
    assert found["310.50"] == {"text": "Bolts M12 | 200 | 301.50", "agrees": False, "sure": None}
    assert found["1,560.50"]["agrees"] is True
    assert found["Steel beams"] == {"text": "Steel beams", "agrees": True, "sure": None}
    assert found["Harbor Steel LLC"] == {"text": None, "agrees": None, "sure": None}, "not in its reading"
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


def _tiff(*lines: str) -> bytes:
    import io

    from PIL import Image, ImageDraw, ImageFont

    pages = []
    for line in lines:
        image = Image.new("L", (1200, 400), 255)
        try:
            font = ImageFont.truetype("DejaVuSans.ttf", 48)
        except OSError:
            font = ImageFont.load_default(size=48)
        ImageDraw.Draw(image).text((50, 150), line, fill=0, font=font)
        pages.append(image)
    buf = io.BytesIO()
    pages[0].save(buf, "TIFF", save_all=True, append_images=pages[1:])
    return buf.getvalue()


def test_each_page_of_a_tiff_is_counted_drawn_and_read(settings):
    """A fax or a scanner's batch is one TIFF of several pages: each is a page to show, to offer the vision model
    and to read, not only the first."""
    data = _tiff("Invoice 5521 page one", "Total due 9,876.54")
    assert page_view.page_count(data, "fax.tif") == 2
    assert vision.wanted_pages(data, "fax.tif", "") == [1, 2]
    first, second = (page_view.page_png(settings, data, "fax.tif", page) for page in (1, 2))
    assert first != second
    with pytest.raises(ValueError):
        page_view.page_png(settings, data, "fax.tif", 3)
    assert page_view.page_count(_tiff("One page"), "scan.tif") == 1
    if not ocr.engine_name():
        pytest.skip("no OCR engine")
    text = ocr.image_text(data)
    assert text.startswith("[page 1]") and "Invoice 5521" in text and "[page 2]" in text and "9,876.54" in text, text


def _post(client, path: str, body: dict, status: int = 200):
    response = client.post(path, json=body, headers=PAGE)
    assert response.status_code == status, (path, response.status_code, response.text[:300])
    return response.json()


def _fixes_path(email, name: str, page: int = 1) -> str:
    return f"/api/mail/{email.id}/files/{_n(email, name)}/pages/{page}/fixes"


def _index(view: dict, text: str) -> int:
    return next(i for i, region in enumerate(view["regions"]) if region["text"] == text)


@pytest.mark.usefixtures("harbor_ocr")
def test_a_box_fixed_shows_at_once_and_reaches_the_files_text(client, store, files):
    email = files["Invoice INV-1042"]
    att = email.attachments[_n(email, "delivery note.png") - 1]
    store.set_attachment_text(att.id, "Harb0r Steel LLC\nDescription Amount\nSteel beams 1,250.00\nBolts M12 310.50")
    view = _regions(client, email, "delivery note.png")
    reply = _post(client, _fixes_path(email, "delivery note.png"), {"kind": "text", "region": _index(view, "Harb0r Steel LLC"), "now": "Harbor Steel LLC"})
    assert "later files" in reply["message"]
    view = _regions(client, email, "delivery note.png")
    fixed = view["regions"][0]
    assert fixed["text"] == "Harbor Steel LLC" and fixed["fixed"] == {"id": reply["id"], "was": "Harb0r Steel LLC", "by": "you"}
    assert view["fixes"] == 1
    # What the chat, summaries and search read: the file's text, with the fix.
    text = store.get_email(email.id).attachments[_n(email, "delivery note.png") - 1].extracted_text
    assert "Harbor Steel LLC" in text and "Harb0r" not in text

    # A figure fixed is put right on this file only.
    reply = _post(client, _fixes_path(email, "delivery note.png"), {"kind": "text", "region": _index(view, "310.50"), "now": "301.50"})
    assert "later files" not in reply["message"]
    assert "Bolts M12 301.50" in store.get_email(email.id).attachments[_n(email, "delivery note.png") - 1].extracted_text

    # Taken back, the page and the text are as read.
    _post(client, f"{_fixes_path(email, 'delivery note.png')}/{reply['id']}/undo", {})
    assert _regions(client, email, "delivery note.png")["regions"][_index(view, "310.50")]["text"] == "310.50"
    assert "Bolts M12 310.50" in store.get_email(email.id).attachments[_n(email, "delivery note.png") - 1].extracted_text
    _post(client, f"{_fixes_path(email, 'delivery note.png')}/{reply['id']}/undo", {}, status=404)


@pytest.mark.usefixtures("harbor_ocr")
def test_a_word_fixed_is_put_right_on_the_senders_next_file_but_a_figure_is_not(client, files):
    first, later = files["Invoice INV-1042"], files["Invoice HS-10482"]
    view = _regions(client, first, "delivery note.png")
    _post(client, _fixes_path(first, "delivery note.png"), {"kind": "text", "region": _index(view, "Harb0r Steel LLC"), "now": "Harbor Steel LLC"})
    _post(client, _fixes_path(first, "delivery note.png"), {"kind": "text", "region": _index(view, "1,250.00"), "now": "1,205.00"})
    next_view = _regions(client, later, "HS-10482 signed.png")
    name = next_view["regions"][0]
    assert name["text"] == "Harbor Steel LLC" and name["fixed"]["by"] == "learnt" and name["fixed"]["was"] == "Harb0r Steel LLC"
    assert any(region["text"] == "1,250.00" and "fixed" not in region for region in next_view["regions"]), "an amount is never carried over"
    # A PDF's own text is exact: nothing learnt is put on it.
    assert all("fixed" not in region for region in _regions(client, later, "HS-10482.pdf")["regions"])

    # Right as read on this file: kept as read here, and still learnt for others.
    _post(client, _fixes_path(later, "HS-10482 signed.png"), {"kind": "text", "region": 0, "now": "Harb0r Steel LLC"})
    assert _regions(client, later, "HS-10482 signed.png")["regions"][0]["text"] == "Harb0r Steel LLC"


@pytest.mark.usefixtures("harbor_ocr")
def test_a_table_drawn_on_the_page_is_made_from_what_was_read_inside_and_learnt(client, files):
    first, later = files["Invoice INV-1042"], files["Invoice HS-10482"]
    view = _regions(client, first, "delivery note.png")
    assert view["tables"] == [], "two columns aren't enough to call it a table"
    width, height = view["width"], view["height"]
    box = {"x": 40 / width, "y": 110 / height, "w": 1020 / width, "h": 160 / height}
    reply = _post(client, _fixes_path(first, "delivery note.png"), {"kind": "table", "box": box})
    [table] = _regions(client, first, "delivery note.png")["tables"]
    assert table["fixed"] == {"id": reply["id"], "by": "you"} and table["label"] == "Your table"
    assert table["columns"] == ["Description", "Amount"]
    assert [[cell["text"] for cell in row] for row in table["rows"]] == [["Steel beams", "1,250.00"], ["Bolts M12", "310.50"], ["Delivery", "185.00"]]
    assert all(cell["region"] is not None for row in table["rows"] for cell in row)

    # The sender's next scan has the same table where the same headings are printed.
    [learnt] = _regions(client, later, "HS-10482 signed.png")["tables"]
    assert learnt["fixed"]["by"] == "learnt" and learnt["columns"] == ["Description", "Amount"] and len(learnt["rows"]) == 3

    # Not a table there after all: gone from that page.
    _post(client, _fixes_path(later, "HS-10482 signed.png"), {"kind": "not_table", "box": learnt["box"]})
    assert _regions(client, later, "HS-10482 signed.png")["tables"] == []
    assert len(_regions(client, first, "delivery note.png")["tables"]) == 1, "the page it was drawn on keeps it"


@pytest.mark.usefixtures("harbor_ocr")
def test_a_fix_that_doesnt_make_sense_is_refused(client, files):
    email = files["Invoice INV-1042"]
    path = _fixes_path(email, "delivery note.png")
    assert "box isn't on this page" in _post(client, path, {"kind": "text", "region": 999, "now": "x"}, status=400)["detail"]
    assert "Type what the page says" in _post(client, path, {"kind": "text", "region": 0, "now": "  "}, status=400)["detail"]
    assert _post(client, path, {"kind": "table", "box": {"x": 0.1, "y": 0.1, "w": 0.001, "h": 0.5}}, status=400)
    assert _post(client, path, {"kind": "table", "box": "everything"}, status=400)
    response = client.post(path, content=b'{"kind": "table", "box": {"x": NaN, "y": 0.1, "w": 0.5, "h": 0.5}}', headers={**PAGE, "Content-Type": "application/json"})
    assert response.status_code == 400
    assert _post(client, path, {"kind": "erase"}, status=400)
    # Without the workspace's header, nothing is saved.
    assert client.post(path, json={"kind": "text", "region": 0, "now": "x"}).status_code in {400, 403}


@pytest.mark.usefixtures("harbor_ocr")
def test_a_suspected_fraud_emails_pages_cant_be_fixed(client, files):
    scam = files["Updated remittance details"]
    response = client.post(f"/api/mail/{scam.id}/files/1/pages/1/fixes", json={"kind": "text", "region": 0, "now": "x"}, headers=PAGE)
    assert response.status_code == 403


@pytest.mark.usefixtures("harbor_ocr")
def test_fixes_are_exported_as_training_examples(client, store, settings, files, tmp_path):
    email = files["Invoice INV-1042"]
    view = _regions(client, email, "delivery note.png")
    _post(client, _fixes_path(email, "delivery note.png"), {"kind": "text", "region": _index(view, "Harb0r Steel LLC"), "now": "Harbor Steel LLC"})
    _post(client, _fixes_path(email, "delivery note.png"), {"kind": "table", "box": {"x": 0.03, "y": 0.04, "w": 0.95, "h": 0.2}})
    counts = fixes.export(store, settings, tmp_path / "out")
    assert counts == {"pages": 1, "tables": 1, "boxes": 1}
    [line] = (tmp_path / "out" / "tables.jsonl").read_text().splitlines()
    page = json.loads(line)
    assert (tmp_path / "out" / page["page"]).read_bytes().startswith(b"\x89PNG") and len(page["tables"]) == 1
    [label] = list((tmp_path / "out" / "labels").iterdir())
    assert label.read_text().startswith("0 ")
    [text] = (tmp_path / "out" / "text.jsonl").read_text().splitlines()
    assert json.loads(text)["read"] == "Harb0r Steel LLC" and json.loads(text)["says"] == "Harbor Steel LLC"
    assert "Nothing here was used to train" in (tmp_path / "out" / "README.md").read_text()


@pytest.mark.usefixtures("harbor_ocr")
def test_fixes_in_the_files_text_land_on_their_own_page():
    text = "[page 1]\nTotal 310.50\n\n[page 2]\nTotal 310.50\n"
    fix = {"kind": "text", "page": 2, "was": "310.50", "now": "301.50", "anchor": {}}
    assert fixes.apply_to_text(text, [fix]) == "[page 1]\nTotal 310.50\n\n[page 2]\nTotal 301.50\n"
    # OCR's words run together in the text ("Harb0rSteel") are still found.
    assert fixes.apply_to_text("Harb0r  Steel\nLLC", [{**fix, "page": 1, "was": "Harb0r Steel LLC", "now": "Harbor Steel LLC"}]) == "Harbor Steel LLC"
    # The vision model's own misreading of a figure is fixed too, when that's what the text shows.
    vision_fix = {**fix, "page": 1, "anchor": {"also": ["301.05"]}}
    assert fixes.apply_to_text("[page 1]\nTotal 301.05\n", [vision_fix]) == "[page 1]\nTotal 301.50\n"


@pytest.mark.usefixtures("harbor_ocr")
def test_what_is_learnt_is_words_not_figures():
    assert fixes.learnable("Harb0r Steel LLC", "Harbor Steel LLC")
    assert not fixes.learnable("1,250.00", "1,205.00")
    assert not fixes.learnable("Invoice 10482", "Invoice 10428"), "a number in words is still a number"
    assert fixes.learnable("BilI to", "Bill to")
    assert fixes.learnable("Steelplate 1/4in,4x8ft", "Steel plate 1/4 in, 4 x 8 ft"), "OCR's run-together words"
    assert not fixes.learnable("Harbor Steel", " Harbor  Steel "), "nothing changed"


@pytest.mark.usefixtures("harbor_ocr")
def test_a_learnt_table_is_found_by_its_first_lines_words_not_its_numbers():
    def region(x, y, text):
        return {"x": x, "y": y, "w": 0.12, "h": 0.015, "text": text, "confidence": 0.9}

    drawn_on = [region(0.6, 0.10, "Invoice no."), region(0.8, 0.10, "NW-20931"), region(0.6, 0.13, "Due date"), region(0.8, 0.13, "10/30/2026")]
    box = {"x": 0.58, "y": 0.09, "w": 0.36, "h": 0.07}
    fix = {"id": "f1", "kind": "table", **box, "anchor": fixes.anchor_for(drawn_on, box)}
    later = [region(0.6, 0.12, "Invoice no."), region(0.8, 0.12, "NW-21077"), region(0.6, 0.15, "Due date"), region(0.8, 0.15, "11/04/2026")]
    [table] = fixes.tables(later, [], [], [fix])
    assert table["fixed"]["by"] == "learnt"
    assert [[cell["text"] for cell in row] for row in table["rows"]] == [["Invoice no.", "NW-21077"], ["Due date", "11/04/2026"]]


def _box(region: dict, pad: float = 0.004) -> dict:
    return {"x": region["x"] - pad, "y": region["y"] - pad, "w": region["w"] + 2 * pad, "h": region["h"] + 2 * pad}


@pytest.mark.usefixtures("harbor_ocr")
def test_undoing_a_figure_fix_after_the_email_is_filed_again_puts_the_figure_back(client, store, files):
    """A fix is applied to the text when the email is loaded; filing the email again (teaching a field) saves that
    shown text as the stored text, so the fix can never be taken back."""
    email = files["Invoice INV-1042"]
    note = _n(email, "delivery note.png")
    att = email.attachments[note - 1]
    store.set_attachment_text(att.id, "Harb0r Steel LLC\nDescription Amount\nSteel beams 1,250.00\nBolts M12 310.50")
    view = _regions(client, email, "delivery note.png")
    reply = _post(client, _fixes_path(email, "delivery note.png"), {"kind": "text", "region": _index(view, "310.50"), "now": "301.50"})
    # Teach a field on the invoice: the email is filed again.
    pdf = _n(email, "invoice.pdf")
    page = _get(client, f"/api/mail/{email.id}/files/{pdf}/pages/1/regions").json()
    total = max((r for r in page["regions"] if r["text"] == "1,560.50"), key=lambda r: r["y"])
    taught = client.post(f"/api/mail/{email.id}/files/{pdf}/pages/1/fixes", headers=PAGE, json={"kind": "field", "field": "total", "box": _box(total)})
    assert taught.status_code == 200, taught.text
    _post(client, f"{_fixes_path(email, 'delivery note.png')}/{reply['id']}/undo", {})
    text = store.get_email(email.id).attachments[note - 1].extracted_text
    assert "Bolts M12 310.50" in text, text


# Pictures: a phone photo's orientation ---------------------------------------------------------------
@pytest.mark.usefixtures("harbor_ocr")
def test_a_phone_photo_turned_by_its_exif_tag_is_read_upright():
    """A phone stores a photo's pixels as the sensor saw them and says in its EXIF tag how to turn them. The Page tab
    turns them (vision.render), but the text read as the email arrives (ocr.image_text) is read from the pixels
    sideways, so the line items lose their figures."""
    import io

    from PIL import Image

    from controller_inbox import ocr, page_view, vision

    if ocr.engine_name() != "RapidOCR":
        pytest.skip("RapidOCR isn't installed")
    upright = Image.open(io.BytesIO(vision.render(_invoice(), "invoice.pdf", 1, reader=page_view.VIEW))).convert("RGB")
    exif = Image.Exif()
    exif[0x0112] = 6  # turn 90 degrees clockwise to view
    out = io.BytesIO()
    upright.transpose(Image.Transpose.ROTATE_90).save(out, "JPEG", quality=92, exif=exif.tobytes())
    text = ocr.image_text(out.getvalue())
    assert "Bolts M12 200 310.50" in text and "Total 1,560.50" in text, text


# A figure fixed on a scan never reaches the email's amount ------------------------------------------------
@pytest.mark.usefixtures("harbor_ocr")
def test_a_total_fixed_on_a_scan_is_the_emails_amount(store, settings, monkeypatch):
    """OCR misreads a scanned invoice's total ($1,860.50 for $1,560.50). The user fixes the box on the Page tab; the
    file's text has the fix, but the email's amount (and its task) stay the misread figure."""
    from fastapi.testclient import TestClient

    from controller_inbox import ocr, page_view, vision, web
    from controller_inbox.folder_mail import ingest_folder
    from msgfactory import write_msg

    read = [(60, 40, 330, 70, "Harbor Steel LLC"), (60, 120, 330, 140, "Invoice HS-2201"), (60, 400, 300, 420, "Total due"), (700, 400, 900, 420, "$1,860.50")]
    monkeypatch.setattr(ocr, "engine_name", lambda: "RapidOCR")
    from controller_inbox import extract

    misread = lambda _data: "Harbor Steel LLC\nInvoice HS-2201\nTotal due $1,860.50"  # noqa: E731
    monkeypatch.setattr(ocr, "image_text", misread)
    monkeypatch.setattr(extract, "image_text", misread)
    monkeypatch.setattr(
        ocr, "line_boxes",
        lambda _png: [{"left": float(l), "top": float(t), "right": float(r), "bottom": float(b), "text": x, "score": 0.9} for l, t, r, b, x in read],
    )
    settings.trusted_domains = "taz.com"
    settings.ensure_data_dir()
    scan = vision.render(_invoice(), "invoice.pdf", 1, reader=page_view.VIEW)
    write_msg(settings.inbox_incoming / "scan.msg", "Invoice HS-2201", "Scan attached.", sender_name="Harbor Steel",
              sender_email="ar@taz.com", attachments=[("HS-2201.png", scan, "image/png")])
    [email] = ingest_folder(store, settings)
    assert email.extracted.primary_amount == 1860.50
    client = TestClient(web.create_app(settings, store))
    view = client.get(f"/api/mail/{email.id}/files/1/pages/1/regions", headers=PAGE).json()
    index = next(i for i, r in enumerate(view["regions"]) if r["text"] == "$1,860.50")
    reply = client.post(f"/api/mail/{email.id}/files/1/pages/1/fixes", headers=PAGE, json={"kind": "text", "region": index, "now": "$1,560.50"})
    assert reply.status_code == 200, reply.text
    stored = store.get_email(email.id)
    assert "$1,560.50" in stored.attachments[0].extracted_text
    assert stored.extracted.primary_amount == 1560.50, stored.extracted.amounts


# PDF forms -------------------------------------------------------------------------------------------
def _filled_form_pdf() -> bytes:
    """An invoice filled in as a PDF form: the amount due is a text field's value (with its appearance, as Acrobat
    and every form filler save it), not text on the page."""
    content = (
        b"BT /F1 12 Tf 72 720 Td (Harbor Steel LLC - Invoice INV-5521) Tj ET\n"
        b"BT /F1 11 Tf 72 680 Td (Amount due:) Tj ET\n"
        b"BT /F1 11 Tf 72 660 Td (Please remit by the due date shown on this form.) Tj ET"
    )
    ap = b"/Tx BMC q BT /F1 11 Tf 2 5 Td ($4,210.00) Tj ET Q EMC"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [6 0 R] /DR << /Font << /F1 5 0 R >> >> /DA (/F1 11 Tf 0 g) >> >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> /Annots [6 0 R] >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (amount_due) /V ($4,210.00) /Rect [160 674 300 692] /P 3 0 R /F 4 "
        b"/DA (/F1 11 Tf 0 g) /AP << /N 7 0 R >> >>",
        b"<< /Type /XObject /Subtype /Form /BBox [0 0 140 18] /Resources << /Font << /F1 5 0 R >> >> /Length %d >>\nstream\n" % len(ap)
        + ap + b"\nendstream",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer << /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


@pytest.mark.usefixtures("harbor_ocr")
def test_a_filled_pdf_forms_values_are_read():
    from controller_inbox.documents import pdf_text

    text = pdf_text(_filled_form_pdf())
    assert "4,210.00" in text, text


@pytest.mark.usefixtures("harbor_ocr")
def test_a_filled_pdf_forms_values_are_drawn_on_the_page():
    import io

    from PIL import Image

    from controller_inbox import page_view, vision

    image = Image.open(io.BytesIO(vision.render(_filled_form_pdf(), "form.pdf", 1, reader=page_view.VIEW))).convert("L")
    scale = image.width / 612
    field = image.crop((int(160 * scale), int((792 - 692) * scale), int(300 * scale), int((792 - 674) * scale)))
    assert field.getextrema()[0] < 128, "the field's value $4,210.00 isn't drawn: the page shows an empty box"


# A page fix taken back after the model read the email (the same root cause as the first undo test) ------------------
def _reader(settings, handler):
    import httpx

    from controller_inbox.local_llm import LocalReader

    return LocalReader(settings, model="tiny-3b", client=httpx.Client(transport=httpx.MockTransport(handler)))


def _reply(obj: dict):
    import json

    import httpx

    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(obj)}, "finish_reason": "stop"}]})


@pytest.mark.usefixtures("harbor_ocr")
def test_a_page_fix_taken_back_after_the_model_read_the_email_is_really_taken_back(loaded, settings):
    from controller_inbox.overnight import read_queue

    email = loaded.get_email("demo-inv-10482")
    att = email.attachments[0]
    assert "Horizon Goods LLC" in att.extracted_text
    fix_id = loaded.add_page_fix({
        "attachment_id": att.id, "sha256": att.sha256, "page": 1, "kind": "text",
        "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.05, "was": "Horizon Goods LLC", "now": "Horizon Foods LLC", "sender": email.sender_email,
    })
    assert "Horizon Foods LLC" in loaded.get_email(email.id).attachments[0].extracted_text

    reading = {"category": "ap_invoice", "folder": "important", "importance": "high",
               "summary": "Invoice INV-10482 to enter.", "why": "Vendor bill.", "actions": []}
    read_queue(loaded, settings, limit=50, reader=_reader(settings, lambda _r: _reply(reading)))
    assert loaded.get_email(email.id).model_status == "bionic"
    # The stored (first) reading of the file must stay as it was read: the fix is laid over it when shown.
    assert "Horizon Goods LLC" in loaded.stored_text(att.id), "the user's fix was written into the file's stored text"

    assert loaded.remove_page_fix(att.id, fix_id)  # Undo on the Page tab
    shown = loaded.get_email(email.id).attachments[0].extracted_text
    assert "Horizon Goods LLC" in shown, "the fix was taken back but the chat and summaries still read the fixed text"


# Undoing a figure fixed on a scan puts the email's amount back ----------------------------------------------------
@pytest.mark.usefixtures("harbor_ocr")
def test_undoing_a_total_fixed_on_a_scan_puts_the_emails_amount_back(store, settings, monkeypatch):
    from fastapi.testclient import TestClient

    from controller_inbox import extract, page_view, vision, web
    from controller_inbox.folder_mail import ingest_folder
    from msgfactory import write_msg

    read = [(60, 40, 330, 70, "Harbor Steel LLC"), (60, 120, 330, 140, "Invoice HS-2201"), (60, 400, 300, 420, "Total due"), (700, 400, 900, 420, "$1,860.50")]
    misread = lambda _data: "Harbor Steel LLC\nInvoice HS-2201\nTotal due $1,860.50"  # noqa: E731
    monkeypatch.setattr(ocr, "engine_name", lambda: "RapidOCR")
    monkeypatch.setattr(ocr, "image_text", misread)
    monkeypatch.setattr(extract, "image_text", misread)
    monkeypatch.setattr(ocr, "line_boxes", lambda _png: [{"left": float(l), "top": float(t), "right": float(r), "bottom": float(b), "text": x, "score": 0.9} for l, t, r, b, x in read])
    settings.trusted_domains = "taz.com"
    settings.ensure_data_dir()
    write_msg(settings.inbox_incoming / "scan.msg", "Invoice HS-2201", "Scan attached.", sender_name="Harbor Steel", sender_email="ar@taz.com",
              attachments=[("HS-2201.png", vision.render(_invoice(), "invoice.pdf", 1, reader=page_view.VIEW), "image/png")])
    [email] = ingest_folder(store, settings)
    client = TestClient(web.create_app(settings, store))
    view = client.get(f"/api/mail/{email.id}/files/1/pages/1/regions", headers=PAGE).json()
    index = next(i for i, r in enumerate(view["regions"]) if r["text"] == "$1,860.50")
    reply = client.post(f"/api/mail/{email.id}/files/1/pages/1/fixes", headers=PAGE, json={"kind": "text", "region": index, "now": "$1,560.50"}).json()
    assert "$1,560.50" in reply["message"], reply
    assert store.get_email(email.id).extracted.primary_amount == 1560.50
    undone = client.post(f"/api/mail/{email.id}/files/1/pages/1/fixes/{reply['id']}/undo", headers=PAGE)
    assert undone.status_code == 200, undone.text
    assert store.get_email(email.id).extracted.primary_amount == 1860.50


# Preview: a TIFF's pages are counted without waiting on page 1's OCR ------------------------------------------------
@pytest.mark.usefixtures("harbor_ocr")
def test_a_tiffs_page_count_comes_with_the_file(store, settings, monkeypatch):
    from fastapi.testclient import TestClient

    from controller_inbox import web
    from controller_inbox.folder_mail import ingest_folder
    from msgfactory import write_msg

    monkeypatch.setattr(ocr, "image_text", lambda _data: "")
    settings.ensure_data_dir()
    write_msg(settings.inbox_incoming / "fax.msg", "Faxed invoice", "Fax attached.", sender_name="Harbor Steel", sender_email="ar@taz.com",
              attachments=[("fax.tif", _tiff("Invoice 5521 page one", "Total due 9,876.54"), "image/tiff")])
    [email] = ingest_folder(store, settings)
    got = TestClient(web.create_app(settings, store)).get(f"/api/mail/{email.id}/files/1", headers=PAGE).json()
    assert got["file"]["show"] == "pages" and got["file"]["pages"] == 2, got["file"]
