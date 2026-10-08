"""The Page tab: a PDF page or a picture shown as it is, with a box over each piece of text read there, how sure the
reading is, and what the vision model read in the same place. A file held as possible payment fraud is never drawn."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from controller_inbox import ocr, page_view, vision, web
from controller_inbox.folder_mail import ingest_folder
from msgfactory import PDF, write_msg
from pdffactory import Text, build_pdf, sheet_rows

PAGE = {"X-CloseDesk": "1"}
OVIS = "ovisocr2-0.85b"

ROWS = [["Item", "Qty", "Amount"], ["Steel beams", "4", "1,250.00"], ["Bolts M12", "200", "310.50"], ["Total", "", "1,560.50"]]


def _invoice(pages: int = 1) -> bytes:
    first = [Text(72, 760, "INVOICE INV-1042", size=16, bold=True), Text(72, 740, "Harbor Steel LLC", size=11)]
    first += sheet_rows([(72, "left"), (400, "right"), (500, "right")], ROWS)
    rest = [[Text(72, 740, f"Terms and conditions, page {page}", size=11)] for page in range(2, pages + 1)]
    return build_pdf([first, *rest])


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
