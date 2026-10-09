"""Finding a page's tables from its picture with the layout model (table_finder.py), and using what it finds in the
Page tab: a table it sees is made from the OCR boxes inside it; without the model, tables are found as before."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from controller_inbox import page_details, table_finder

np = pytest.importorskip("numpy")


class FakeSession:
    """The layout model's output for a 640 x 640 picture: one row per label score, one column per candidate."""

    def __init__(self, candidates):
        self.candidates = candidates
        self.seen = None

    def get_inputs(self):
        return [type("Input", (), {"name": "images"})()]

    def run(self, _names, feed):
        self.seen = feed["images"]
        out = np.zeros((1, 4 + len(table_finder.LABELS), len(self.candidates)), dtype=np.float32)
        for i, (cx, cy, w, h, label, score) in enumerate(self.candidates):
            out[0, :4, i] = (cx, cy, w, h)
            out[0, 4 + table_finder.LABELS.index(label), i] = score
        return [out]


def _png(width=1275, height=1650) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, "PNG")
    return buf.getvalue()


def test_only_tables_the_model_is_sure_of_are_kept_once_each():
    session = FakeSession(
        [
            (320, 320, 512, 128, "Table", 0.91),
            (322, 318, 500, 130, "Table", 0.80),  # the same table, found twice
            (320, 100, 400, 30, "Title", 0.95),
            (320, 520, 512, 64, "Table", 0.10),  # not sure enough
        ]
    )
    [table] = table_finder._find(session, _png())
    assert session.seen.shape == (1, 3, 640, 640) and session.seen.max() <= 1.0
    assert table == {"x": 0.1, "y": 0.4, "w": 0.8, "h": 0.2, "score": pytest.approx(0.91, abs=1e-4)}


def test_without_the_model_nothing_is_found_and_it_is_fetched_for_next_time(settings, monkeypatch):
    fetched = []
    monkeypatch.setattr(table_finder, "runtime", lambda: True)
    monkeypatch.setattr(table_finder, "fetch_later", lambda s: fetched.append(s))
    assert table_finder.find(settings, _png()) is None and fetched == [settings]


def test_a_fetched_file_that_isnt_the_model_is_thrown_away(tmp_path, monkeypatch):
    import httpx

    class Stream:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        def iter_bytes(self):
            yield b"not the model"

    monkeypatch.setattr(httpx, "stream", lambda *a, **kw: Stream())
    target = tmp_path / "models" / table_finder.NAME
    with pytest.raises(ValueError, match="SHA-256"):
        table_finder._fetch(target)
    assert not target.exists() and not target.with_suffix(".part").exists()


def _region(x, y, text, w=0.15):
    return {"x": x, "y": y, "w": w, "h": 0.015, "text": text, "confidence": 0.95}


# A scan where OCR's boxes don't line up in three columns: two columns, so OCR alone finds no table.
FOUND = [
    _region(0.08, 0.05, "Harbor Steel LLC", w=0.3),
    _region(0.08, 0.20, "Description"),
    _region(0.75, 0.20, "Amount"),
    _region(0.08, 0.23, "Steel beams"),
    _region(0.75, 0.23, "1,250.00"),
    _region(0.08, 0.26, "Bolts M12"),
    _region(0.75, 0.26, "310.50"),
    _region(0.08, 0.40, "Thank you for your business.", w=0.5),
]


def test_a_table_the_model_sees_is_made_from_the_boxes_inside_it():
    assert page_details.page_tables(FOUND, "ocr", "", 1, None) == [], "OCR's boxes alone: no table"
    seen = [{"x": 0.05, "y": 0.18, "w": 0.9, "h": 0.12, "score": 0.9}, {"x": 0.05, "y": 0.38, "w": 0.6, "h": 0.05, "score": 0.6}]
    [table] = page_details.page_tables(FOUND, "ocr", "", 1, None, seen)
    assert table["columns"] == ["Description", "Amount"] and table["found_by"] == "layout"
    assert [[cell["text"] for cell in row] for row in table["rows"]] == [["Steel beams", "1,250.00"], ["Bolts M12", "310.50"]]
    assert table["box"]["y"] < 0.2 < 0.27 < table["box"]["y"] + table["box"]["h"]


def test_a_table_already_read_isnt_added_twice():
    model_text = "| Description | Amount |\n|---|---|\n| Steel beams | 1,250.00 |\n| Bolts M12 | 310.50 |\n"
    seen = [{"x": 0.05, "y": 0.18, "w": 0.9, "h": 0.12, "score": 0.9}]
    [table] = page_details.page_tables(FOUND, "ocr", "", 1, model_text, seen)
    assert "found_by" not in table, "the vision model's table, not a second one from the layout model"
