"""CloseDesk's test environment, for Streamlit Community Cloud.

A Streamlit app over CloseDesk's own modules (no logic of its own beyond showing what they return), loaded with the
built-in sample mailbox. There is no local model here: every answer comes from the no-model ("lookup") paths, and
nothing calls LM Studio. Each visitor gets their own temporary folder, which goes when their session ends.

Run it from the repository root:  streamlit run streamlit/streamlit_app.py
"""

from __future__ import annotations

import hashlib
import html
import io
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# The package is used from the checkout, not installed: Streamlit Cloud runs this file from the repository root.
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
# No model in the cloud, whatever the environment says.
os.environ["CONTROLLER_INBOX_LLM"] = "false"

import pandas as pd  # noqa: E402  (installed with Streamlit)
import streamlit as st  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from controller_inbox import documents, fraud, ocr, page_details, page_view, table_lookup, vision  # noqa: E402
from controller_inbox.actions import local_today  # noqa: E402
from controller_inbox.assistant import answer_stream  # noqa: E402
from controller_inbox.cli import DEMO_NOW  # noqa: E402
from controller_inbox.clock import format_when  # noqa: E402
from controller_inbox.config import Settings  # noqa: E402
from controller_inbox.demo import demo_messages  # noqa: E402
from controller_inbox.digest import build_digest  # noqa: E402
from controller_inbox.folder_mail import ingest_folder, safe_filename  # noqa: E402
from controller_inbox.models import DOCUMENT_LABELS, FOLDER_LABELS, IMPORTANCE_LABELS, EmailRecord  # noqa: E402
from controller_inbox.pipeline import ingest_demo  # noqa: E402
from controller_inbox.profile import is_finance  # noqa: E402
from controller_inbox.store import Store  # noqa: E402

TIMEZONE = "America/New_York"
# Nothing listens here, so even a call that slipped through would fail at once, on this machine, never reaching LM Studio.
OFFLINE_URL = "http://127.0.0.1:9/v1"
MAX_UPLOAD_MB = 10
UPLOAD_TYPES = ["pdf", "png", "jpg", "jpeg", "eml", "msg", "xlsx"]
HEADER_NOTE = "Test environment — sample mailbox, no local model (answers come from looking things up), nothing is saved."
UPLOAD_WARNING = (
    "Files you upload here leave your computer and are processed on Streamlit's servers. "
    "Use sample or made-up files, not real invoices or mail."
)
OCR_MISSING = "OCR isn't available here, so scans and pictures can't be read in this test environment."
PAGES = ["Today", "Mail", "Attachment", "Ask", "Fraud check", "Try your own file"]
FOLDERS = {"Important": "important", "Informational": "informational", "Reference": "reference", "All": ""}
# The page view's colours (static/ui/app.css: --ok, --imp-high, --danger), filled at 22% as the workspace draws them.
TONES = {"ok": (23, 115, 74), "check": (211, 138, 18), "differs": (180, 35, 24)}
TABLE_OUTLINE = (37, 87, 167)
IMPORTANCE_COLOURS = {"critical": "red", "high": "orange", "medium": "blue", "low": "gray"}
FOCUS_COLOURS = {"fraud": "red", "overdue": "red", "due_today": "orange", "due_soon": "orange", "due_week": "blue", "new_task": "blue"}
LEVEL_COLOURS = {"high": "red", "caution": "orange"}


# Each visitor's own mailbox ----------------------------------------------------------------------------------


@dataclass
class Box:
    """A mailbox in a temporary folder: the sample, or the files one visitor uploaded."""

    name: str
    settings: Settings
    store: Store
    folder: tempfile.TemporaryDirectory = field(repr=False)


def make_box(name: str) -> Box:
    folder = tempfile.TemporaryDirectory(prefix=f"closedesk-{name}-")
    root = Path(folder.name)
    settings = Settings(
        data_dir=root / "data",
        inbox_dir=root / "inbox",
        timezone=TIMEZONE,
        llm=False,
        embedding_model="off",
        vision_mode="off",
        llm_base_url=OFFLINE_URL,
        _env_file=None,
    )
    settings.ensure_data_dir()
    return Box(name, settings, Store(settings.db_path), folder)


@st.cache_resource(show_spinner=False)
def sample_files() -> dict[str, bytes]:
    """The sample mailbox's attachments by SHA-256 (the same for every visitor, so shared)."""
    return {hashlib.sha256(att.content).hexdigest(): att.content for message in demo_messages(DEMO_NOW) for att in message.attachments}


def keep_sample_originals(box: Box) -> None:
    """The sample's files where a folder import keeps originals (inbox/extracted/<email id>/), so their pages show."""
    files = sample_files()
    for email in box.store.list_emails(limit=-1):
        for att in email.attachments:
            data = files.get(att.sha256)
            if data is not None:
                path = box.settings.inbox_extracted / email.id / safe_filename(att.filename)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)


def sample_box() -> Box:
    if "sample_box" not in st.session_state:
        with st.spinner("Loading the sample mailbox…"):
            box = make_box("sample")
            ingest_demo(box.store, box.settings, now=DEMO_NOW)
            keep_sample_originals(box)
        st.session_state["sample_box"] = box
    return st.session_state["sample_box"]


def upload_box() -> Box:
    if "upload_box" not in st.session_state:
        st.session_state["upload_box"] = make_box("upload")
    # Kept when the files are removed, so a file still in the upload box isn't read again.
    st.session_state.setdefault("uploaded", set())
    return st.session_state["upload_box"]


def box_named(name: str) -> Box:
    return upload_box() if name == "upload" else sample_box()


@st.cache_resource(show_spinner=False)
def ocr_ready() -> bool:
    """RapidOCR imports (it needs OpenCV, which needs system libraries a server may not have)."""
    if ocr.engine_name() != "RapidOCR":
        return bool(ocr.engine_name())
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except Exception:
        return False
    return True


def settle_ocr() -> None:
    """Without a working RapidOCR, CloseDesk takes the path it takes when none is installed (no OCR)."""
    if not ocr_ready():
        ocr.engine_name = lambda: ""


def board_date():
    """The sample mailbox's day, as the laptop app shows it while only the sample is loaded."""
    return local_today(sample_box().settings.tz, DEMO_NOW)


# Small helpers ------------------------------------------------------------------------------------------------


def money(value: float | None) -> str:
    return "" if value is None else f"${value:,.2f}"


def size_label(size: int) -> str:
    if size >= 1_000_000:
        return f"{size / 1_000_000:.1f} MB"
    return f"{max(size, 0) / 1000:.0f} KB" if size >= 1000 else f"{size} bytes"


def md(text: str) -> str:
    """Mail text shown as itself in Markdown: no HTML, and no "$" read as math, "*" as bold, and so on."""
    escaped = html.escape(str(text or ""), quote=False)
    return re.sub(r"([\\`*_{}\[\]#+!|~$:])", r"\\\1", escaped)


def badge(text: str, colour: str) -> str:
    return f":{colour}-background[{text}]"


def goto(page: str, **state) -> None:
    """Button callback: open another page with what it should show."""
    st.session_state.update(state)
    st.session_state["page"] = page


def open_email(email_id: str, folder: str = "") -> None:
    label = next((label for label, key in FOLDERS.items() if key == folder), None)
    extra = {"mail_folder": label} if label else {}
    goto("Mail", mail_pick=email_id, **extra)


def fraud_view(box: Box, email: EmailRecord) -> dict:
    """The saved fraud check, or a fresh one for mail stored without one (as the laptop app's email page does)."""
    saved = box.store.fraud_check(email.id)
    if saved is None:
        check = fraud.assess_email(box.store, fraud.trust_context(box.store, box.settings), email)
        saved = {"level": check.level, "score": check.score, "signals": check.signal_dicts()}
    return saved


# Showing an email ---------------------------------------------------------------------------------------------


def show_email(box: Box, email: EmailRecord) -> None:
    today = board_date().isoformat()
    importance = email.importance.value
    st.subheader(md(email.subject or "(no subject)"))
    address = f" &lt;{md(email.sender_email)}&gt;" if email.sender_email and email.sender_email != email.sender_name else ""
    st.markdown(
        f"{badge(IMPORTANCE_LABELS.get(email.importance, importance), IMPORTANCE_COLOURS.get(importance, 'gray'))} "
        f"{badge(DOCUMENT_LABELS.get(email.category, email.category.value), 'gray')} "
        f"· **{md(email.sender_name or email.sender_email)}**{address} · {format_when(email.received_at, box.settings.tz)}"
    )
    if email.reply_to and email.reply_to.lower() != (email.sender_email or "").lower():
        st.caption(f"Replies go to {email.reply_to}")
    if email.summary:
        st.markdown(md(email.summary))

    locked = fraud.attachments_locked(email)
    check = fraud_view(box, email)
    if locked:
        st.error("**Possible payment fraud — do not process.** Verify by phone with a number you already have. Its files don't open here.")

    reasons = [reason for reason in email.importance_reasons if reason]
    left, right = st.columns(2)
    with left:
        st.markdown("**Why it's here**")
        st.markdown("\n".join(f"- {md(reason)}" for reason in reasons) if reasons else "_Nothing stood out._")
        fields = email.extracted
        rows = [
            ("Invoices", ", ".join(fields.invoice_numbers)),
            ("POs", ", ".join(fields.po_numbers)),
            ("Amounts", ", ".join(money(amount) for amount in fields.amounts)),
            ("Due dates", ", ".join(fields.due_dates)),
            ("Vendors", ", ".join(fields.vendor_candidates)),
            ("Accounts", ", ".join(f"••{last4}" for last4 in fields.account_last4)),
            ("Bank details", "Mentioned" if fields.mentions_routing_or_account else ""),
        ]
        rows = [(name, value) for name, value in rows if value]
        st.markdown("**What CloseDesk read**")
        if rows:
            st.dataframe(pd.DataFrame(rows, columns=["Field", "Value"]), hide_index=True)
        else:
            st.caption("No invoice numbers, amounts or dates.")
    with right:
        st.markdown("**Tasks**")
        if email.actions:
            for action in email.actions:
                overdue = action.due_date and action.due_date < today and action.status.value == "open"
                due = f" · due {action.due_date}" + (" (overdue)" if overdue else "") if action.due_date else ""
                done = f" · {action.status.value}" if action.status.value != "open" else ""
                st.markdown(f"- **{md(action.title)}**{due}{done}  \n  <small>{md(action.detail)}</small>", unsafe_allow_html=True)
        else:
            st.caption("No tasks.")
        level = check.get("level") or "low"
        st.markdown(
            f"**Fraud check** {badge(level.title() if level in LEVEL_COLOURS else 'No warning', LEVEL_COLOURS.get(level, 'green'))} score {check.get('score', 0)} "
            f"(caution from {fraud.CAUTION_AT}, high from {fraud.HIGH_AT})"
        )
        signals = [
            {"Signal": item.get("label", item.get("key", "")), "Points": item.get("points", 0), "Detail": item.get("detail", "")}
            for item in check.get("signals") or []
        ]
        if signals:
            st.dataframe(pd.DataFrame(signals), hide_index=True)
        else:
            st.caption("No warning signs.")

    st.markdown(f"**Attachments** ({len(email.attachments)})")
    if not email.attachments:
        st.caption("No files.")
    for n, att in enumerate(email.attachments, start=1):
        cols = st.columns([5, 1])
        kind = DOCUMENT_LABELS.get(att.document_type, att.document_type.value)
        found = att.extracted_fields
        extra = " · ".join(part for part in (found.primary_invoice or "", money(found.primary_amount)) if part)
        cols[0].markdown(f"📎 **{md(att.filename)}** · {size_label(att.size_bytes)} · {kind}" + (f" · {md(extra)}" if extra else ""))
        if locked:
            cols[1].caption("Locked")
        else:
            cols[1].button("Open", key=f"open-{box.name}-{email.id}-{n}", on_click=goto, args=("Attachment",), kwargs={"file": (box.name, email.id, n)})
    with st.expander("Message text"):
        st.text(email.body_text or "(empty)")


# Pages --------------------------------------------------------------------------------------------------------


def page_today() -> None:
    box = sample_box()
    payload = build_digest(
        box.store,
        as_of=board_date(),
        generated_at=datetime.now(box.settings.tz),
        tz=box.settings.tz,
        lookback_days=box.settings.digest_lookback_days,
        save=False,
        finance=is_finance(box.settings, box.store),
    )
    st.title("Today")
    st.caption(payload["date_long"] + " · the sample mailbox's day")
    st.markdown(f"#### {payload['headline']}")
    alerts = payload["critical_alerts"]
    if alerts:
        with st.container(border=True):
            st.error("**Do not process — verify by phone.** These ask to pay or to change bank details.")
            for item in alerts:
                cols = st.columns([6, 1])
                cols[0].markdown(f"**{md(item['subject'])}**  \n<small>{md(item['sender'])} · {md(item['summary'])}</small>", unsafe_allow_html=True)
                cols[1].button("Open", key=f"alert-{item['id']}", on_click=open_email, args=(item["id"], item["folder"]))
    kpis = payload["kpis"]
    cols = st.columns(5)
    cols[0].metric("Need you", kpis["need_you"])
    cols[1].metric("Worth knowing", kpis["worth_knowing"])
    cols[2].metric("Filed", kpis["filed"])
    cols[3].metric("Open tasks", kpis["open_actions"])
    cols[4].metric("Overdue", kpis["overdue_actions"])

    st.subheader("Your focus today")
    if not payload["focus"]:
        st.caption("Nothing needs you right now.")
    for row in payload["focus"]:
        with st.container(border=True):
            cols = st.columns([0.5, 8, 1])
            cols[0].markdown(f"### {row['rank']}")
            details = [row["sender"]]
            if row["title"] != row["subject"]:
                details.append(row["subject"])
            if row.get("amount"):
                details.append(money(row["amount"]))
            if row.get("more_tasks"):
                details.append(f"+{row['more_tasks']} more task{'s' if row['more_tasks'] != 1 else ''}")
            cols[1].markdown(
                f"{badge(row['label'], FOCUS_COLOURS.get(row['kind'], 'gray'))} **{md(row['title'])}**  \n"
                f"{md(row['summary'] or row['subject'])}  \n<small>{md(' · '.join(details))}</small>",
                unsafe_allow_html=True,
            )
            cols[2].button("Open", key=f"focus-{row['email_id']}", on_click=open_email, args=(row["email_id"], row["folder"]))

    week = payload["due_this_week"]
    if week:
        st.subheader("Coming up in the next 7 days")
        st.dataframe(
            pd.DataFrame([{"Due": item["due_label"], "Task": item["title"], "Email": item["subject"]} for item in week]),
            hide_index=True,
        )


def _pick_from_table(key: str, ids: list[str]) -> None:
    rows = st.session_state[key]["selection"]["rows"]
    if rows and rows[0] < len(ids):
        st.session_state["mail_pick"] = ids[rows[0]]


def page_mail() -> None:
    box = sample_box()
    st.title("Mail")
    left, right = st.columns([1, 1], gap="large")
    with left:
        label = st.selectbox("Folder", list(FOLDERS), key="mail_folder")
        folder = FOLDERS[label]
        if folder:
            emails = box.store.list_emails(folder=folder, order="score", limit=200)
        else:
            emails = box.store.list_emails(limit=200)
        ids = [email.id for email in emails]
        table = pd.DataFrame(
            [
                {
                    "Importance": IMPORTANCE_LABELS.get(email.importance, email.importance.value),
                    "Subject": email.subject or "(no subject)",
                    "From": email.sender_name or email.sender_email,
                    "Received": format_when(email.received_at, box.settings.tz),
                    "Files": len(email.attachments),
                }
                for email in emails
            ]
        )
        key = f"mail-table-{folder or 'all'}"
        st.dataframe(
            table,
            key=key,
            on_select=lambda: _pick_from_table(key, ids),
            selection_mode="single-row",
            column_config={
                "Importance": st.column_config.TextColumn(width="small"),
                "Subject": st.column_config.TextColumn(width="large"),
                "Files": st.column_config.NumberColumn(width="small"),
            },
            column_order=["Importance", "Subject", "From", "Files"],
            hide_index=True,
            height=min(38 + 35 * max(len(ids), 1), 640),
        )
        st.caption("Click a row to open the email.")
    picked = st.session_state.get("mail_pick")
    if picked is None or box.store.get_email(picked) is None:
        picked = ids[0] if ids else None
    with right:
        if picked is None:
            st.info("This folder is empty.")
        else:
            with st.container(border=True):
                show_email(box, box.store.get_email(picked))


def tone(region: dict, source: str) -> str:
    """Green, amber or red, as the workspace's page view decides (static/ui/reader.js ``tone``)."""
    model = region.get("model")
    if model and model.get("agrees") is False:
        return "differs"
    if model and model.get("agrees") is True:
        return "ok"
    if source == "text":
        return "ok"
    if region.get("confidence") is not None and region["confidence"] < 0.8:
        return "check"
    return "check" if model else "ok"


def draw_boxes(png: bytes, boxes: list[dict], source: str, tables: list[dict]) -> Image.Image:
    """The page with every box filled and outlined in its colour, and each table outlined with dashes."""
    page = Image.open(io.BytesIO(png)).convert("RGBA")
    width, height = page.size
    layer = Image.new("RGBA", page.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    line = max(2, round(width / 800))
    for region in boxes:
        colour = TONES[tone(region, source)]
        x0, y0 = region["x"] * width, region["y"] * height
        x1, y1 = x0 + max(region["w"] * width, 1), y0 + max(region["h"] * height, 1)
        draw.rectangle([x0 - 1, y0 - 1, x1 + 1, y1 + 1], fill=(*colour, 56), outline=(*colour, 220), width=line)
    for table in tables:
        outline = table.get("box")
        if not outline:
            continue
        x0, y0 = outline["x"] * width, outline["y"] * height
        x1, y1 = x0 + outline["w"] * width, y0 + outline["h"] * height
        dash = 10
        for start in range(int(x0), int(x1), dash * 2):
            for y in (y0, y1):
                draw.line([(start, y), (min(start + dash, x1), y)], fill=(*TABLE_OUTLINE, 230), width=line)
        for start in range(int(y0), int(y1), dash * 2):
            for x in (x0, x1):
                draw.line([(x, start), (x, min(start + dash, y1))], fill=(*TABLE_OUTLINE, 230), width=line)
    return Image.alpha_composite(page, layer).convert("RGB")


def page_reading(box: Box, email: EmailRecord, att, data: bytes, page: int) -> dict:
    """What the workspace's regions endpoint gives for a page (web_api ``mail_file_regions``), from the same calls."""
    found = page_view.regions(box.settings, data, att.filename, page)
    png = page_view.page_png(box.settings, data, att.filename, page)
    reading = box.store.page_readings(att.id, att.sha256).get(page)
    boxes = found["regions"]
    model_text = (reading.get("model_text") or "") if reading is not None else None
    if reading is not None:
        boxes = page_view.with_model(boxes, model_text)
    known = [att.extracted_fields, email.extracted]
    extracted = {
        "invoices": [value for fields in known for value in fields.invoice_numbers],
        "pos": [value for fields in known for value in fields.po_numbers],
        "due_dates": [value for fields in known for value in fields.due_dates],
        "vendors": [*(value for fields in known for value in fields.vendor_candidates), email.sender_name or ""],
    }
    try:
        tables = page_details.page_tables(boxes, found["source"], att.extracted_text or "", page, model_text)
        details = page_details.key_details(boxes, found["source"], model_text, extracted)
    except Exception:
        tables, details = [], []
    return {"png": png, "source": found["source"], "reason": found["reason"], "regions": boxes, "reading": reading, "tables": tables, "fields": details}


def show_page_view(box: Box, email: EmailRecord, att, n: int) -> None:
    if not vision.readable_file(att.filename):
        st.info("Only a PDF or a picture has pages to show. The Tables and Text tabs show what CloseDesk read from this file.")
        return
    if not vision.can_render():
        st.info("The page renderer isn't installed here.")
        return
    data = vision.original_bytes(box.settings, email, att)
    if data is None:
        st.info("The original file wasn't kept for this email.")
        return
    try:
        pages = page_view.page_count(data, att.filename)
    except Exception:
        st.warning("This file's pages couldn't be read.")
        return
    page = 1
    if pages > 1:
        page = int(st.number_input(f"Page (of {pages})", min_value=1, max_value=pages, value=1, step=1, key=f"page-{box.name}-{email.id}-{n}"))
    try:
        view = page_reading(box, email, att, data, page)
    except Exception:
        st.warning("This page couldn't be read.")
        return
    boxes, source = view["regions"], view["source"]
    shades = [tone(region, source) for region in boxes]
    counts = {shade: shades.count(shade) for shade in TONES}

    about = []
    if source == "text":
        about.append("Read from the file's own text (exact): each box is exactly what the file says.")
    elif boxes:
        about.append("This page is a scan. OCR read it, and each box says how sure it was.")
    if source == "ocr" and not boxes and not ocr_ready():
        about.append(OCR_MISSING)
    elif view["reason"]:
        about.append(view["reason"])
    if source == "ocr" and view["reading"] is None:
        about.append("No vision model here, so only OCR's reading is shown.")
    if view["tables"]:
        about.append("Dashed blue outlines are tables.")
    st.caption(" ".join(about))

    picture, side = st.columns([3, 2], gap="large")
    with picture:
        st.image(draw_boxes(view["png"], boxes, source, view["tables"]), width="stretch")
    with side:
        if boxes:
            legend = [f"{':green[■]'} " + ("Exact" if source == "text" else "Read clearly (80% sure or more)")]
            if source == "ocr" or counts["check"]:
                legend.append(f"{':orange[■]'} Check it: OCR under 80% sure")
            if counts["differs"]:
                legend.append(f"{':red[■]'} The vision model read a different figure")
            st.markdown("  \n".join(legend))
            st.caption(f"{len(boxes)} box{'es' if len(boxes) != 1 else ''}" + (f" · {counts['check']} to check" if counts["check"] else ""))
        st.markdown("**Key details**")
        if view["fields"]:
            st.dataframe(
                pd.DataFrame([{"Detail": item["label"], "Value": item["value"], "Box": (item["region"] + 1) if item["region"] is not None else None} for item in view["fields"]]),
                hide_index=True,
                )
        else:
            st.caption("No invoice number, date, total or vendor found on this page.")
    if view["tables"]:
        st.markdown("**Tables on this page**")
        for table in view["tables"]:
            st.caption(table.get("label") or f"Table {table.get('id', '')}")
            columns = [str(column) or f"Column {index}" for index, column in enumerate(table.get("columns") or [], start=1)]
            rows = [[cell.get("text", "") for cell in row] for row in table.get("rows") or []]
            width = max([len(columns)] + [len(row) for row in rows]) if (columns or rows) else 0
            columns = _unique(columns + [f"Column {index}" for index in range(len(columns) + 1, width + 1)])
            st.dataframe(pd.DataFrame([row + [""] * (width - len(row)) for row in rows], columns=columns[:width]), hide_index=True)
    if boxes:
        st.markdown("**What was read where**")
        agreement = {True: "agrees", False: "differs", None: "not in its reading"}
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Box": index,
                        "Text": region["text"],
                        "Confidence": "exact" if source == "text" else ("" if region.get("confidence") is None else f"{region['confidence']:.0%}"),
                        "Model agreement": agreement[region["model"].get("agrees")] if region.get("model") else "no model reading",
                        "Colour": {"ok": "green", "check": "amber", "differs": "red"}[shade],
                    }
                    for index, (region, shade) in enumerate(zip(boxes, shades), start=1)
                ]
            ),
            hide_index=True,
        )


def _unique(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for name in names:
        seen[name] = seen.get(name, 0) + 1
        out.append(name if seen[name] == 1 else f"{name} ({seen[name]})")
    return out


def show_tables(att) -> None:
    text = att.extracted_text or ""
    found = table_lookup.tables_in(text) if text.strip() else []
    if not found:
        st.caption("No tables found in this file's text.")
        return
    for number, table in enumerate(found[:40], start=1):
        verdict = table_lookup.verify(table)
        labels = list(table.labels)
        where = f" · {table.where}" if table.where else ""
        st.markdown(f"**Table {number}**{where} · {len(table.rows)} row{'s' if len(table.rows) != 1 else ''}")
        st.dataframe(pd.DataFrame([[row.value(label) for label in labels] for row in table.rows[:1500]], columns=_unique(labels)), hide_index=True)
        if verdict.mismatched:
            st.warning("Totals that don't add up: " + "; ".join(verdict.mismatched))
        elif verdict.matched:
            st.caption(f"Printed totals check out ({verdict.matched} checked).")


def show_text(att) -> None:
    text = att.extracted_text or ""
    if not text.strip():
        st.caption("No text was read from this file." + (" " + OCR_MISSING if vision.readable_file(att.filename) and not ocr_ready() else ""))
        return
    parts = documents.split_parts(text)
    for index, part in enumerate(parts):
        with st.expander(part.label or "Text", expanded=index == 0):
            st.text(part.text)


def page_attachment() -> None:
    st.title("Attachment")
    choice = st.session_state.get("file")
    sample = sample_box()
    options = [
        ("sample", email.id, n)
        for email in sample.store.list_emails(limit=-1)
        if not fraud.attachments_locked(email)
        for n in range(1, len(email.attachments) + 1)
    ]
    if choice and choice not in options:
        options.insert(0, choice)
    if not options:
        st.info("No files to show.")
        return
    names = {}
    for name, email_id, n in options:
        email = box_named(name).store.get_email(email_id)
        if email is not None and 1 <= n <= len(email.attachments):
            prefix = "Your file · " if name == "upload" else ""
            names[(name, email_id, n)] = f"{prefix}{email.attachments[n - 1].filename} — {email.subject}"
    options = [option for option in options if option in names]
    index = options.index(choice) if choice in options else 0
    picked = st.selectbox("File", options, index=index, format_func=names.get)
    if picked != choice:
        st.session_state["file"] = picked
    name, email_id, n = picked
    box = box_named(name)
    email = box.store.get_email(email_id)
    if fraud.attachments_locked(email):
        # Never shown: not its text, not its tables, not its pages.
        st.error("This email is flagged as possible payment fraud, so its files don't open.")
        return
    att = email.attachments[n - 1]
    kind = DOCUMENT_LABELS.get(att.document_type, att.document_type.value)
    st.markdown(f"**{md(att.filename)}** · {size_label(att.size_bytes)} · {kind} {round((att.document_confidence or 0) * 100)}% · from *{md(email.subject)}*")
    view, tables, text = st.tabs(["Page view", "Tables", "Text"])
    with view:
        show_page_view(box, email, att, n)
    with tables:
        show_tables(att)
    with text:
        show_text(att)


SUGGESTED = [
    "What needs me today?",
    "Which invoices are due this week?",
    "Is anything flagged as possible fraud?",
    "What did Northwind Supplies send?",
]


def _suggest(question: str) -> None:
    st.session_state["ask_box"] = question
    st.session_state["ask_now"] = True


def page_ask() -> None:
    box = sample_box()
    st.title("Ask")
    st.caption("Questions are answered by looking things up in the sample mailbox (no model here), with the emails used as sources.")
    cols = st.columns(len(SUGGESTED))
    for col, question in zip(cols, SUGGESTED):
        col.button(question, key=f"suggest-{question}", on_click=_suggest, args=(question,), width="stretch")
    with st.form("ask"):
        question = st.text_input("Your question", key="ask_box", placeholder="e.g. What's the total on invoice INV-10482?")
        about_email = None
        picked = st.session_state.get("mail_pick")
        email = box.store.get_email(picked) if picked else None
        if email is not None:
            if st.checkbox(f"About the email open on the Mail page: {email.subject}", key="ask_about_email"):
                about_email = email.id
        asked = st.form_submit_button("Ask", type="primary")
    if st.session_state.pop("ask_now", False):
        asked = True
    if not (asked and question.strip()):
        return
    as_of = board_date()
    focus = build_digest(
        box.store,
        as_of=as_of,
        generated_at=datetime.now(box.settings.tz),
        tz=box.settings.tz,
        lookback_days=box.settings.digest_lookback_days,
        save=False,
        finance=is_finance(box.settings, box.store),
    )["focus"]
    answer, sources, notes = [], [], []
    for event in answer_stream(box.store, box.settings, question, email_id=about_email, focus=focus, today=as_of.isoformat()):
        kind = event.get("type")
        if kind == "sources":
            sources = event.get("sources") or []
            if event.get("warning"):
                notes.append(("warning", event["warning"]))
        elif kind == "delta":
            answer.append(event.get("text", ""))
        elif kind in {"step", "context"}:
            notes.append(("caption", event.get("text", "")))
        elif kind == "mode" and event.get("note"):
            notes.append(("caption", event["note"]))
    with st.container(border=True):
        for how, text in notes:
            if how == "warning":
                st.warning(text)
            else:
                st.caption(text)
        st.markdown("".join(answer).replace("$", "\\$") or "_No answer._")  # Markdown, but "$" is money, not math
    if sources:
        st.markdown("**Sources**")
        for source in sources:
            cols = st.columns([6, 1])
            flag = f" {badge('possible fraud', 'red')}" if source.get("fraud") else ""
            files = ", ".join(item["name"] for item in source.get("files") or [])
            cols[0].markdown(f"[{source['n']}] **{md(source['subject'])}** · {md(source['sender'])}{flag}" + (f"  \n<small>Files: {md(files)}</small>" if files else ""), unsafe_allow_html=True)
            email = box.store.get_email(source["id"])
            cols[1].button("Open", key=f"source-{source['id']}", on_click=open_email, args=(source["id"], email.folder if email else ""))


def page_fraud() -> None:
    box = sample_box()
    st.title("Fraud check")
    st.caption(
        f"Every email is scored for signs of payment fraud: caution from {fraud.CAUTION_AT} points, high from {fraud.HIGH_AT}. "
        "A high one's files are locked until it is verified by phone."
    )
    flagged = box.store.flagged(limit=100)
    if not flagged:
        st.success("Nothing is flagged.")
    for row in flagged:
        with st.container(border=True):
            cols = st.columns([6, 1])
            signals = [signal for signal in row["signals"] if (signal.get("points") or 0) > 0]
            cleared = " · marked safe" if "fraud_cleared" in row["flags"] else ""
            cols[0].markdown(
                f"{badge(row['level'].title(), LEVEL_COLOURS.get(row['level'], 'gray'))} **{md(row['subject'] or '(no subject)')}** · score {row['score']}{cleared}  \n"
                f"<small>{md(row['sender_name'] or row['sender_email'])} &lt;{md(row['sender_email'])}&gt; · {format_when(row['received_at'], box.settings.tz)}</small>",
                unsafe_allow_html=True,
            )
            if signals:
                cols[0].markdown("\n".join(f"- {md(signal.get('label', signal.get('key', '')))} (+{signal.get('points', 0)})" + (f": {md(signal['detail'])}" if signal.get("detail") else "") for signal in signals))
            email = box.store.get_email(row["email_id"])
            cols[1].button("Open", key=f"fraud-{row['email_id']}", on_click=open_email, args=(row["email_id"], email.folder if email else ""))


def page_upload() -> None:
    st.title("Try your own file")
    st.warning(UPLOAD_WARNING, icon="⚠️")
    box = upload_box()
    st.caption(f"A PDF, picture, email (.eml or .msg) or Excel file, up to {MAX_UPLOAD_MB} MB. It is read the way the laptop app reads a file dropped in its inbox folder, kept only for this session, and shown here as an email.")
    file = st.file_uploader("Choose a file", type=UPLOAD_TYPES, key="upload_file")
    if file is not None:
        data = file.getvalue()
        digest = hashlib.sha256(data).hexdigest()
        if len(data) > MAX_UPLOAD_MB * 1_000_000:
            st.error(f"That file is over {MAX_UPLOAD_MB} MB.")
        elif digest not in st.session_state["uploaded"]:
            name = safe_filename(Path(file.name).name) or "upload"
            (box.settings.inbox_incoming / name).write_bytes(data)
            report: dict = {}
            with st.spinner(f"Reading {name}…"):
                records = ingest_folder(box.store, box.settings, report=report)
            st.session_state["uploaded"].add(digest)
            for failed in report.get("failed") or []:
                st.error(f"{failed['file']} couldn't be read: {failed['error']}")
            if records:
                st.session_state["upload_pick"] = records[-1].id
            elif report.get("already_read"):
                st.info("That file was read already.")
    emails = box.store.list_emails(limit=-1)
    if not emails:
        return
    if not ocr_ready():
        st.caption(OCR_MISSING)
    ids = [email.id for email in emails]
    picked = st.session_state.get("upload_pick")
    picked = picked if picked in ids else ids[0]
    picked = st.selectbox("Your files", ids, index=ids.index(picked), format_func=lambda email_id: box.store.get_email(email_id).subject or "(no subject)")
    st.session_state["upload_pick"] = picked
    with st.container(border=True):
        show_email(box, box.store.get_email(picked))
    if st.button("Remove my files"):
        st.session_state.pop("upload_box", None)
        st.session_state.pop("upload_pick", None)
        if (st.session_state.get("file") or ("",))[0] == "upload":
            st.session_state.pop("file", None)
        st.rerun()


RENDER = {
    "Today": page_today,
    "Mail": page_mail,
    "Attachment": page_attachment,
    "Ask": page_ask,
    "Fraud check": page_fraud,
    "Try your own file": page_upload,
}


def main() -> None:
    st.set_page_config(page_title="CloseDesk test environment", page_icon="📥", layout="wide")
    settle_ocr()
    sample_box()
    with st.sidebar:
        st.markdown("## CloseDesk")
        st.caption("Test environment")
        st.radio("Go to", PAGES, key="page", label_visibility="collapsed")
        counts = sample_box().store.counts()
        st.caption(f"Sample mailbox: {counts['emails']} emails, {counts.get('open_actions', 0)} open tasks.")
        if not ocr_ready():
            st.caption(OCR_MISSING)
    st.info(HEADER_NOTE, icon="🧪")
    RENDER[st.session_state.get("page", "Today")]()


main()
