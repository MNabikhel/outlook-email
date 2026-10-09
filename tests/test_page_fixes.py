"""Fixing what was read on a file's page (the Page tab): a box read wrong, a table missed, an outline that isn't a
table. A fix shows at once and reaches the file's text, and a word or a table is learnt for the sender's next file;
a figure never is."""

from __future__ import annotations

import json

import pytest

from controller_inbox import fixes, ocr
from test_page_view import PAGE, _get, _n, _regions, client, files  # noqa: F401 - the fixtures are used by name

# What OCR reads on both scans from Harbor Steel (ar@taz.com): the name with a zero for its "o", and a table of
# three lines no outline is found for (only two columns line up).
LINES = [
    (60, 40, 330, 70, "Harb0r Steel LLC"),
    (60, 120, 260, 140, "Description"),
    (900, 120, 1040, 140, "Amount"),
    (60, 160, 300, 180, "Steel beams"),
    (900, 160, 1040, 180, "1,250.00"),
    (60, 200, 300, 220, "Bolts M12"),
    (900, 200, 1040, 220, "310.50"),
    (60, 240, 300, 260, "Delivery"),
    (900, 240, 1040, 260, "185.00"),
    (60, 400, 700, 420, "Thank you for your business."),
]


@pytest.fixture(autouse=True)
def fake_ocr(monkeypatch):
    monkeypatch.setattr(ocr, "engine_name", lambda: "RapidOCR")
    monkeypatch.setattr(
        ocr,
        "line_boxes",
        lambda _png: [{"left": float(l), "top": float(t), "right": float(r), "bottom": float(b), "text": text, "score": 0.9} for l, t, r, b, text in LINES],
    )


def _post(client, path: str, body: dict, status: int = 200):
    response = client.post(path, json=body, headers=PAGE)
    assert response.status_code == status, (path, response.status_code, response.text[:300])
    return response.json()


def _fixes_path(email, name: str, page: int = 1) -> str:
    return f"/api/mail/{email.id}/files/{_n(email, name)}/pages/{page}/fixes"


def _index(view: dict, text: str) -> int:
    return next(i for i, region in enumerate(view["regions"]) if region["text"] == text)


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


def test_a_suspected_fraud_emails_pages_cant_be_fixed(client, files):
    scam = files["Updated remittance details"]
    response = client.post(f"/api/mail/{scam.id}/files/1/pages/1/fixes", json={"kind": "text", "region": 0, "now": "x"}, headers=PAGE)
    assert response.status_code == 403


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


def test_fixes_in_the_files_text_land_on_their_own_page():
    text = "[page 1]\nTotal 310.50\n\n[page 2]\nTotal 310.50\n"
    fix = {"kind": "text", "page": 2, "was": "310.50", "now": "301.50", "anchor": {}}
    assert fixes.apply_to_text(text, [fix]) == "[page 1]\nTotal 310.50\n\n[page 2]\nTotal 301.50\n"
    # OCR's words run together in the text ("Harb0rSteel") are still found.
    assert fixes.apply_to_text("Harb0r  Steel\nLLC", [{**fix, "page": 1, "was": "Harb0r Steel LLC", "now": "Harbor Steel LLC"}]) == "Harbor Steel LLC"
    # The vision model's own misreading of a figure is fixed too, when that's what the text shows.
    vision_fix = {**fix, "page": 1, "anchor": {"also": ["301.05"]}}
    assert fixes.apply_to_text("[page 1]\nTotal 301.05\n", [vision_fix]) == "[page 1]\nTotal 301.50\n"


def test_what_is_learnt_is_words_not_figures():
    assert fixes.learnable("Harb0r Steel LLC", "Harbor Steel LLC")
    assert not fixes.learnable("1,250.00", "1,205.00")
    assert not fixes.learnable("Invoice 10482", "Invoice 10428"), "a number in words is still a number"
    assert fixes.learnable("BilI to", "Bill to")
    assert fixes.learnable("Steelplate 1/4in,4x8ft", "Steel plate 1/4 in, 4 x 8 ft"), "OCR's run-together words"
    assert not fixes.learnable("Harbor Steel", " Harbor  Steel "), "nothing changed"


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
