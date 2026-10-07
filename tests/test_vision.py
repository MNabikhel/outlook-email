"""Pages read two ways: OCR (or the PDF's own text) and a local model that looks at the page itself.

The model's reading is kept beside the first one, compared figure by figure, and the page is shown as the reading
that holds up best, with the differences written under it. These tests run the whole path with a stand-in for
LM Studio: noticing the model can see, the offer and its estimate, the read, what the chat and the table lookup then
read, the answer check, the routes of both dashboards, and the overnight run's time budget.
"""

from __future__ import annotations

import base64
import io
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from controller_inbox import agent, answer_check, assistant, local_llm, ocr, table_lookup, vision, web
from controller_inbox.folder_mail import ingest_folder
from controller_inbox.local_llm import check_model
from controller_inbox.store import shown_text
from msgfactory import PDF, write_msg

PAGE = {"X-CloseDesk": "1"}
MODEL = "qwen3.5-9b"

# What a model that can see writes for the scanned balance sheet: the table whole, every figure as printed.
BALANCE = """BALANCE SHEET

| | 2026 | 2025 |
|---|---|---|
| Current Assets | | |
| Cash | 12,400 | 9,800 |
| Accounts Receivable | 30,250 | 28,000 |
| Less: Allowance for Doubtful Accounts | (1,250) | (1,000) |
| Total Current Assets | 41,400 | 36,800 |
"""
# What OCR reads from it: the words and figures, the table's shape lost, and one figure misread.
OCR_TEXT = """BALANCE SHEET 2026 2025 Current Assets
Cash 12,400 9,800
Accounts Receivable 30,256 28,000
Less: Allowance for Doubtful Accounts (1,250) (1,000)
Total Current Assets 41,400 36,800"""


def _scan_pdf(pages: int = 1) -> bytes:
    """A PDF whose pages are pictures only, as a scanner makes them: no text layer."""
    images = []
    for _ in range(pages):
        image = Image.new("RGB", (850, 1100), "white")
        draw = ImageDraw.Draw(image)
        for row, line in enumerate(OCR_TEXT.splitlines()):
            draw.text((60, 80 + 40 * row), line, fill="black")
        images.append(image)
    out = io.BytesIO()
    images[0].save(out, "PDF", resolution=100, save_all=True, append_images=images[1:])
    return out.getvalue()


class FakeVisionServer:
    """LM Studio with a model loaded that can look at pictures. A request with a picture gets ``page``; a chat
    request gets ``answer``."""

    def __init__(self, page: str = BALANCE, *, vision: bool = True, answer: str = "The receivable is 30,250 [1]."):
        self.page = page
        self.vision = vision
        self.answer = answer
        self.calls: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": MODEL}]})
        if path == "/api/v1/models":
            caps = {"vision": self.vision, "reasoning": {"allowed_options": ["off", "on"], "default": "on"}}
            model = {"type": "llm", "key": MODEL, "loaded_instances": [{"id": MODEL, "config": {"context_length": 16384}}], "capabilities": caps}
            return httpx.Response(200, json={"models": [model]})
        if path in {"/api/v0/models", "/props"}:
            return httpx.Response(404)
        payload = json.loads(request.content)
        self.calls.append(payload)
        content = payload["messages"][-1]["content"]
        sees = isinstance(content, list) and any(part.get("type") == "image_url" for part in content)
        text = self.page if sees else self.answer
        events = [{"choices": [{"delta": {"content": text[i : i + 40]}}]} for i in range(0, len(text), 40)]
        events.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)


def _serve(monkeypatch, server) -> None:
    client = httpx.Client(transport=httpx.MockTransport(server))
    monkeypatch.setattr(local_llm.httpx, "get", lambda url, **kw: client.get(url, headers=kw.get("headers")))
    monkeypatch.setattr(local_llm.httpx, "post", lambda url, **kw: client.post(url, json=kw.get("json"), headers=kw.get("headers")))
    monkeypatch.setattr(
        local_llm.httpx, "stream", lambda method, url, **kw: client.stream(method, url, json=kw.get("json"), headers=kw.get("headers"))
    )


@pytest.fixture
def seeing(settings, monkeypatch) -> FakeVisionServer:
    server = FakeVisionServer()
    settings.llm = None
    _serve(monkeypatch, server)
    return server


def _ingest_scan(store, settings, monkeypatch, *, ocr_text: str = OCR_TEXT, pages: int = 1, subject: str = "Scanned balance sheet"):
    monkeypatch.setattr(ocr, "engine_name", lambda: "RapidOCR" if ocr_text else "")
    monkeypatch.setattr(ocr, "image_text", lambda _data: ocr_text)
    settings.trusted_domains = "taz.com"
    settings.ensure_data_dir()
    name = subject.lower().replace(" ", "-")
    write_msg(
        settings.inbox_incoming / f"{name}.msg",
        subject,
        "The balance sheet scan is attached.",
        sender_name="Maya Chen",
        sender_email="maya@taz.com",
        attachments=[("balance sheet.pdf", _scan_pdf(pages), PDF)],
    )
    [email] = [record for record in ingest_folder(store, settings) if record.subject == subject]
    return email


@pytest.fixture
def scan(store, settings, monkeypatch):
    return _ingest_scan(store, settings, monkeypatch)


def _read(store, settings, email_id: str, pages=(1,)):
    email = store.get_email(email_id)
    att = email.attachments[0]
    data = agent.original_file(settings, email, att).read_bytes()
    return vision.read_pages(store, settings, email, att, data, list(pages))


def _stored_text(store, att_id: str) -> str:
    with store.connect() as conn:
        return conn.execute("SELECT extracted_text FROM attachments WHERE id = ?", (att_id,)).fetchone()[0]


# Noticing a model that can see -------------------------------------------------------------------------------


def test_lm_studio_says_which_loaded_model_can_see(settings, monkeypatch):
    settings.llm = None
    _serve(monkeypatch, FakeVisionServer())
    status = check_model(settings)
    assert status.model == MODEL and status.vision and status.to_dict()["vision"] is True
    local_llm._status_cache.clear()
    _serve(monkeypatch, FakeVisionServer(vision=False))
    assert not check_model(settings).vision, "a text-only model is never sent a picture"


def test_older_lm_studio_and_llama_cpp_say_so_too(settings, monkeypatch):
    settings.llm = None

    def older(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "gemma-3-12b"}]})
        if request.url.path == "/api/v0/models":
            return httpx.Response(200, json={"data": [{"id": "gemma-3-12b", "type": "vlm", "state": "loaded"}]})
        return httpx.Response(404)

    _serve(monkeypatch, older)
    assert check_model(settings).vision

    def llama_cpp(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "qwen3.5-vision", "meta": {"n_ctx": 16384}}]})
        if request.url.path == "/props":
            return httpx.Response(200, json={"modalities": {"vision": True, "audio": False}})
        return httpx.Response(404)

    local_llm._status_cache.clear()
    _serve(monkeypatch, llama_cpp)
    status = check_model(settings)
    assert status.model == "qwen3.5-vision" and status.vision and status.context_length == 16384


# The model's page as CloseDesk reads it ------------------------------------------------------------------------


def test_the_models_table_is_written_row_by_row_with_every_cell_named():
    page = vision.page_text(BALANCE)
    assert page.startswith("[notes]\nBALANCE SHEET")
    assert "Line | 2026 | 2025" in page, "the blank heading over the row names is named, so no row splits"
    assert "Group: Current Assets" in page
    assert "Line: Less: Allowance for Doubtful Accounts | 2026: (1,250) | 2025: (1,000)" in page
    [table] = table_lookup.tables_in("[page 1]\n" + page)
    verdict = table_lookup.verify(table)
    assert verdict.matched == 2 and not verdict.mismatched, "the total is checked against the rows above it"


def test_bold_headings_repeated_headers_and_thinking_are_dropped():
    markdown = "<think>Let me look.</think>\n## **Aging**\n\n| Vendor | Amount |\n|:--|--:|\n| **Acme** | 1,200.00 |\n| Vendor | Amount |\n| Beta | 300.00 |\n"
    page = vision.page_text(markdown)
    assert "Let me look" not in page and "**" not in page
    assert "[heading]\nAging" in page
    assert page.count("Vendor: ") == 2 and "Vendor: Vendor" not in page


def test_figures_are_signed_and_leave_out_dates_years_and_row_numbers():
    found = vision.figures("Paid 1,250.00 on 03/14/2026 for FY 2026, row 12; credit (2,750); fee -45.50; rate 15%")
    assert found == {
        vision.Decimal("1250.00"): ["1,250.00"],
        vision.Decimal("-2750"): ["(2,750)"],
        vision.Decimal("-45.50"): ["-45.50"],
        vision.Decimal("15"): ["15%"],
    }


def test_the_reading_that_holds_up_is_shown_and_the_difference_written_under_it():
    comparison = vision.compare(OCR_TEXT, vision.page_text(BALANCE))
    assert (comparison.figures, comparison.confirmed) == (8, 7)
    assert comparison.differ == [("30,250", "30,256")]
    assert comparison.model_totals == (2, 0) and comparison.choice == "model"
    page = vision.merged_page(OCR_TEXT, BALANCE, comparison, first_name="OCR", model=MODEL)
    assert page.startswith(
        f"[Read two ways, by OCR and by the vision model ({MODEL}): 7 of 8 figures read the same. "
        "Shown: the vision model's reading; its printed totals add up.]"
    )
    assert page.endswith("[Where the two readings differ (the vision model / OCR): 30,250 / 30,256]")
    assert vision.unconfirmed(page) == {vision.Decimal("30250"): "OCR read 30,256"}


def test_a_reading_of_something_else_is_not_shown():
    """A model that read another page, or made one up, shares few figures with the first reading."""
    other = "| Item | Amount |\n|---|---|\n| Travel | 7,310 |\n| Meals | 2,115 |\n| Total | 9,425 |\n"
    comparison = vision.compare(OCR_TEXT, vision.page_text(other))
    assert comparison.confirmed == 0 and comparison.choice == "first"
    page = vision.merged_page(OCR_TEXT, other, comparison, first_name="OCR", model=MODEL)
    assert "Shown: OCR's reading" in page and "30,256" in page and "7,310" not in page.split("\n", 1)[1].split("[Where")[0]
    assert vision.unconfirmed(page) == {}, "figures shown as OCR read them aren't flagged as the model's"


def test_a_reading_whose_totals_dont_add_up_loses_to_one_whose_do():
    wrong = BALANCE.replace("| Cash | 12,400 |", "| Cash | 12,900 |")
    first = vision.page_text(BALANCE)  # the PDF's own text, already a table that adds up
    comparison = vision.compare(first, vision.page_text(wrong))
    assert comparison.model_totals == (1, 1) and comparison.first_totals == (2, 0)
    assert comparison.choice == "first"


def test_without_ocr_the_models_reading_is_the_page_and_every_figure_is_unconfirmed():
    comparison = vision.compare("(no text on this page)", vision.page_text(BALANCE))
    assert comparison.choice == "model"
    page = vision.merged_page("(no text on this page)", BALANCE, comparison, first_name="OCR", model=MODEL)
    assert "OCR found no text on this page" in page and "none of its figures is confirmed" in page
    assert "Line: Cash | 2026: 12,400" in page
    flagged = vision.unconfirmed(f"[page 1]\n{page}")
    assert vision.Decimal("41400") in flagged and vision.Decimal("-1250") in flagged and len(flagged) == 8


# A scanned PDF read two ways, end to end -------------------------------------------------------------------------


def test_a_scanned_page_is_offered_read_and_shown_both_ways(scan, store, settings, seeing):
    email = store.get_email(scan.id)
    att = email.attachments[0]
    assert "30,256" in att.extracted_text and "read with OCR" in att.extracted_text

    told = vision.offer(store, settings, email, att)
    assert told["available"] and told["pages"] == [1] and told["read"] == []
    assert told["estimate"] is None, "the speed of this computer isn't known before its first page"
    assert "The first page shows how long this computer takes" in told["text"]

    result = _read(store, settings, scan.id)
    assert (result.pages, result.shown_model, result.failed) == (1, 1, [])
    [call] = seeing.calls
    picture, prompt = call["messages"][0]["content"]
    assert picture["image_url"]["url"].startswith("data:image/png;base64,")
    png = Image.open(io.BytesIO(base64.b64decode(picture["image_url"]["url"].split(",", 1)[1])))
    assert max(png.size) <= vision.MAX_SIDE and png.size[0] >= 800, "the page at about 100 DPI"
    assert "Copy every number exactly" in prompt["text"]
    assert call["temperature"] == 0.0 and call["reasoning_effort"] == "none" and call["max_tokens"] >= vision.MAX_TOKENS

    shown = store.get_email(scan.id).attachments[0].extracted_text
    assert shown.startswith("[Scanned page 1 read with OCR"), "the note before the page stays"
    assert f"[Read two ways, by OCR and by the vision model ({MODEL}): 7 of 8 figures read the same." in shown
    assert "Line: Accounts Receivable | 2026: 30,250" in shown
    assert "[Where the two readings differ (the vision model / OCR): 30,250 / 30,256]" in shown
    assert next(e for e in store.list_emails() if e.id == scan.id).attachments[0].extracted_text == shown
    assert "[Read two ways" not in _stored_text(store, att.id), "the stored text stays the first reading"
    assert shown_text(shown, store.page_readings(att.id, att.sha256), att.sha256) == shown, "showing it twice changes nothing"
    assert any(not table_lookup.verify(t).mismatched and table_lookup.verify(t).matched == 2 for t in table_lookup.tables_in(shown))

    after = vision.offer(store, settings, store.get_email(scan.id), att)
    assert after["pages"] == [] and after["read"] == [1] and after["text"] == ""
    assert vision.seconds_per_page(store, settings) is not None
    [row] = vision.side_by_side(store.page_readings(att.id, att.sha256))
    assert row["first_name"] == "OCR" and row["model"] == MODEL
    assert "<mark>30,256</mark>" in str(row["first_html"]) and "<mark>30,250</mark>" in str(row["model_html"])
    [data] = vision.readings_json(store.page_readings(att.id, att.sha256))
    assert data["marks_first"] == ["30,256"] and data["marks_model"] == ["30,250"]
    assert data["model_blocks"][1]["header"] == ["", "2026", "2025"] and data["comparison"]["choice"] == "model"


def test_a_scan_without_ocr_is_read_by_the_model_alone(store, settings, monkeypatch, seeing):
    email = _ingest_scan(store, settings, monkeypatch, ocr_text="")
    assert "(no text on this page)" in email.attachments[0].extracted_text
    assert _read(store, settings, email.id).pages == 1
    shown = store.get_email(email.id).attachments[0].extracted_text
    assert "OCR found no text on this page" in shown and "Line: Cash | 2026: 12,400" in shown


def test_a_reading_of_an_earlier_version_of_the_file_is_not_used(scan, store, settings, seeing):
    _read(store, settings, scan.id)
    att = store.get_email(scan.id).attachments[0]
    readings = store.page_readings(att.id)
    assert shown_text(att.extracted_text, readings, "another file's sha256") == att.extracted_text
    assert store.page_readings(att.id, "another file's sha256") == {}


def test_a_first_reading_that_changed_since_is_compared_again(scan, store, settings, seeing):
    _read(store, settings, scan.id)
    att = store.get_email(scan.id).attachments[0]
    rows = store.page_readings(att.id, att.sha256)
    fixed = _stored_text(store, att.id).replace("30,256", "30,250")  # a better OCR, after an update
    shown = shown_text(fixed, rows, att.sha256)
    assert "8 of 8 figures read the same" in shown and "differ" not in shown


def test_reading_again_replaces_the_pages_and_clearing_mail_forgets_them(scan, store, settings, seeing):
    _read(store, settings, scan.id)
    att = store.get_email(scan.id).attachments[0]
    seeing.page = BALANCE.replace("41,400", "41,900")
    _read(store, settings, scan.id)
    [row] = store.page_readings(att.id, att.sha256).values()
    assert "41,900" in row["model_text"] and row["first"] == _stored_text(store, att.id).split("[page 1]\n", 1)[1].strip()
    assert "Shown: OCR's reading" in store.get_email(scan.id).attachments[0].extracted_text, "a total that doesn't add up loses"
    store.clear_mail()
    assert store.page_readings(att.id) == {}


def test_a_page_that_fails_doesnt_lose_the_others(store, settings, monkeypatch, seeing):
    email = _ingest_scan(store, settings, monkeypatch, pages=2)
    calls = []

    def flaky(_settings, png, **_kw):
        calls.append(png)
        if len(calls) == 1:
            raise httpx.ReadTimeout("the model took too long")
        return BALANCE

    monkeypatch.setattr(vision, "transcribe", flaky)
    result = _read(store, settings, email.id, pages=(1, 2))
    assert result.pages == 1 and result.failed == ["page 1: the model took too long"]
    att = store.get_email(email.id).attachments[0]
    assert sorted(store.page_readings(att.id, att.sha256)) == [2]
    assert "No page could be read" not in vision.result_text(result) and "1 couldn't be read" in vision.result_text(result)


def test_files_of_a_suspected_fraud_email_are_never_sent_to_the_model(mail, store, settings, seeing):
    scam = store.get_email(mail["Updated remittance details"].id)
    assert vision.files_to_read(store, settings, scam) == []
    told = vision.offer(store, settings, scam, scam.attachments[0])
    assert not told["available"] and "payment fraud" in told["reason"]
    client = TestClient(web.create_app(settings, store))
    for path in (f"/inbox/{scam.id}/files/1/vision", f"/api/mail/{scam.id}/files/1/vision"):
        refused = client.post(path, headers=PAGE)
        assert refused.status_code == 409 and "payment fraud" in refused.json()["message"]
    assert client.get(f"/api/mail/{scam.id}/files/1", headers=PAGE).status_code == 403
    assert seeing.calls == []


def test_the_offer_says_why_it_cant_read(scan, store, settings, monkeypatch):
    email = store.get_email(scan.id)
    att = email.attachments[0]
    settings.llm = None
    _serve(monkeypatch, FakeVisionServer(vision=False))
    assert "can't look at pictures" in vision.offer(store, settings, email, att)["reason"]
    settings.vision_mode = "off"
    assert "turned off in Setup" in vision.offer(store, settings, email, att)["reason"]


# How long it takes, and asking first ---------------------------------------------------------------------------


def test_time_is_said_in_words():
    assert vision.duration(25) == "about 30 seconds"
    assert vision.duration(600) == "about 10 minutes"
    assert vision.duration(4500) == "about 1 hour 15 minutes"
    assert vision.offer_text(3, 1800) == "Read 3 pages with the vision model as well: about 30 minutes on this computer."


def _seconds_per_page(store, seconds: float) -> None:
    """As if this computer had read a page of another file in ``seconds``."""
    store.save_page_reading("earlier-file", 1, first="x", model_text="x", model="local-model", seconds=seconds, comparison="{}", sha256="s")


def _chat_model(monkeypatch, answer: str = "The receivable is 30,250 [1]."):
    asked = []

    def stream(_settings, messages, *, max_tokens):
        asked.append(messages[-1]["content"])
        yield answer

    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "context_length", lambda _s: 16384)
    monkeypatch.setattr(assistant, "chat_with_tools", lambda *_a, **_k: (_ for _ in ()).throw(assistant.ToolsUnsupported("no tools")))
    monkeypatch.setattr(assistant, "stream_text", stream)
    monkeypatch.setattr(assistant, "complete_text", lambda *_a, **_k: "Plan: none.\nSQL: NONE")
    monkeypatch.setattr(vision, "available", lambda _s: True)
    return asked


def test_a_long_read_is_offered_with_the_time_it_would_take(scan, store, settings, monkeypatch):
    asked = _chat_model(monkeypatch)
    monkeypatch.setattr(vision, "transcribe", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("read without asking")))
    _seconds_per_page(store, 600)
    events = list(assistant.answer_stream(store, settings, "What is the accounts receivable for 2026?", email_id=scan.id))
    [offer] = [event for event in events if event["type"] == "vision"]
    assert [event["type"] for event in events][-2:] == ["vision", "done"]
    assert offer["email_id"] == scan.id and offer["n"] == 1 and offer["pages"] == 1 and offer["estimate"] == 600
    assert offer["question"] == "What is the accounts receivable for 2026?"
    assert "balance sheet.pdf has 1 page the vision model could read too, beside OCR." in offer["text"]
    assert "about 10 minutes on this computer" in offer["text"]
    assert "30,256" in asked[-1], "meanwhile the answer comes from OCR's reading"


def test_a_quick_read_happens_while_the_question_waits(scan, store, settings, monkeypatch):
    asked = _chat_model(monkeypatch)
    monkeypatch.setattr(vision, "transcribe", lambda *_a, **_k: BALANCE)
    _seconds_per_page(store, 8)
    events = list(assistant.answer_stream(store, settings, "What is the accounts receivable for 2026?", email_id=scan.id))
    assert not [event for event in events if event["type"] == "vision"]
    assert "Reading balance sheet.pdf with the vision model (about 10 seconds)" in [e["text"] for e in events if e["type"] == "step"]
    assert "[Read two ways, by OCR and by the vision model" in asked[-1] and "Line: Accounts Receivable | 2026: 30,250" in asked[-1]
    checks = [item for event in events if event["type"] == "check" for item in event["items"]]
    assert "30,250 was read from the page by the vision model only (OCR read 30,256): check it against the file." in checks


def test_in_ask_mode_even_a_quick_read_waits_for_a_click(scan, store, settings, monkeypatch):
    _chat_model(monkeypatch)
    monkeypatch.setattr(vision, "transcribe", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("read without asking")))
    settings.vision_mode = "ask"
    _seconds_per_page(store, 8)
    events = list(assistant.answer_stream(store, settings, "What is the accounts receivable for 2026?", email_id=scan.id))
    assert [event for event in events if event["type"] == "vision"]


def test_the_answer_check_flags_a_figure_only_the_model_read():
    page = vision.merged_page(OCR_TEXT, BALANCE, vision.compare(OCR_TEXT, vision.page_text(BALANCE)), first_name="OCR", model=MODEL)
    review = answer_check.review("Receivables were $30,250 and cash $12,400 [1].", material=[], files=[("balance sheet.pdf", page)])
    assert "$30,250 was read from the page by the vision model only (OCR read 30,256): check it against the file." in review.checks
    assert not [check for check in review.checks if "12,400" in check], "a figure both readings have is confirmed"


# The overnight run -----------------------------------------------------------------------------------------------


def test_the_overnight_run_reads_waiting_scans_within_its_time(scan, store, settings, monkeypatch):
    monkeypatch.setattr(vision, "available", lambda _s: True)
    read = []
    monkeypatch.setattr(vision, "transcribe", lambda *_a, **_k: read.append(1) or BALANCE)
    assert vision.read_waiting(store, settings, minutes=0).pages == 0, "starting CloseDesk reads no pages"
    assert vision.read_waiting(store, settings, minutes=1).pages == 0, "a page of unknown length isn't started in a minute"
    _seconds_per_page(store, 900)
    assert vision.read_waiting(store, settings, minutes=10).pages == 0, "a 15-minute page doesn't fit in 10"
    result = vision.read_waiting(store, settings, minutes=30)
    assert result.pages == 1 and read == [1]
    assert vision.read_waiting(store, settings, minutes=30).pages == 0, "a page read once isn't read again"
    settings.vision_mode = "ask"
    other = _ingest_scan(store, settings, monkeypatch, subject="Second scan")
    assert vision.read_waiting(store, settings, minutes=30).pages == 0, "in ask mode nothing is read unasked"
    assert other.id


def test_process_new_mail_reads_only_pages_this_computer_reads_quickly(store, settings, monkeypatch):
    from controller_inbox import overnight

    given = []
    monkeypatch.setattr(overnight, "run_overnight", lambda *_a, vision_minutes=None, **_k: given.append(vision_minutes) or {})
    monkeypatch.setattr(web.ProcessJob, "start", lambda self, target: target(lambda *_a: None) is not None)
    client = TestClient(web.create_app(settings, store))
    client.post("/process", follow_redirects=False)
    client.post("/api/process", headers=PAGE)
    assert given == [1.0, 1.0], "a minute: only a computer that reads a page in seconds reads any"


# The two dashboards ----------------------------------------------------------------------------------------------


def _wait_for_job(client: TestClient, headers=PAGE) -> dict:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        state = client.get("/process/status", headers=headers).json()
        if state["state"] != "running":
            return state
        time.sleep(0.05)
    raise AssertionError("the read didn't finish")


def test_the_file_page_offers_the_read_and_shows_both_readings(scan, store, settings, seeing):
    client = TestClient(web.create_app(settings, store))
    page = client.get(f"/inbox/{scan.id}/files/1")
    assert "Read with the vision model too" in page.text and 'data-action="vision-read"' in page.text
    assert client.get(f"/inbox/{scan.id}/files/1/vision").json()["pages"] == [1]
    assert client.post(f"/inbox/{scan.id}/files/1/vision").status_code == 403, "only CloseDesk's own page starts a read"

    started = client.post(f"/inbox/{scan.id}/files/1/vision", headers=PAGE).json()
    assert started["started"] and started["pages"] == [1]
    done = _wait_for_job(client)
    assert done["state"] == "done" and done["result"]["kind"] == "vision" and done["result"]["pages"] == 1
    assert done["result"]["href"] == f"/inbox/{scan.id}/files/1" and "Ask again" in done["result"]["message"]

    page = client.get(f"/inbox/{scan.id}/files/1")
    assert "Pages read two ways" in page.text and "<mark>30,256</mark>" in page.text and "<mark>30,250</mark>" in page.text
    assert "Every page that needed it has been read both ways" in page.text
    again = client.post(f"/inbox/{scan.id}/files/1/vision?again=1", headers=PAGE).json()
    assert again["started"] and again["pages"] == [1]
    assert _wait_for_job(client)["state"] == "done" and len(seeing.calls) == 2


def test_the_workspace_file_view_gets_the_offer_and_the_readings_as_data(scan, store, settings, seeing):
    client = TestClient(web.create_app(settings, store))
    data = client.get(f"/api/mail/{scan.id}/files/1", headers=PAGE).json()
    assert data["vision"]["offer"]["pages"] == [1] and data["vision"]["readings"] == []
    started = client.post(f"/api/mail/{scan.id}/files/1/vision", headers=PAGE).json()
    assert started["started"]
    assert client.post(f"/api/mail/{scan.id}/files/1/vision", headers=PAGE).status_code in {200, 409}
    _wait_for_job(client)
    data = client.get(f"/api/mail/{scan.id}/files/1", headers=PAGE).json()
    [reading] = data["vision"]["readings"]
    assert reading["page"] == 1 and reading["marks_model"] == ["30,250"] and reading["first_text"].startswith("BALANCE SHEET")
    assert "Line: Accounts Receivable | 2026: 30,250" in "\n".join(part["text"] for part in data["parts"])
    assert client.post(f"/api/mail/{scan.id}/files/9/vision", headers=PAGE).status_code == 404


def test_a_read_isnt_started_while_another_job_runs(scan, store, settings, seeing, monkeypatch):
    client = TestClient(web.create_app(settings, store))
    monkeypatch.setattr(web.ProcessJob, "start", lambda self, target: False)
    for path in (f"/inbox/{scan.id}/files/1/vision", f"/api/mail/{scan.id}/files/1/vision"):
        busy = client.post(path, headers=PAGE)
        assert busy.status_code == 409 and "busy" in busy.json()["message"]


def test_setup_saves_the_choice_across_restarts(store, settings):
    client = TestClient(web.create_app(settings, store))
    page = client.get("/settings")
    assert 'name="mode" value="ask"' in page.text or "vision" in page.text
    response = client.post("/settings/vision", data={"mode": "ask"}, headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert response.status_code in {303, 302}
    assert settings.vision_mode == "ask" and store.get_state("vision_mode") == "ask"
    fresh = type(settings)(data_dir=settings.data_dir, inbox_dir=settings.inbox_dir, _env_file=None)
    vision.apply_saved_mode(fresh, store)
    assert fresh.vision_mode == "ask"
    with pytest.raises(ValueError):
        vision.save_mode(settings, store, "always")
