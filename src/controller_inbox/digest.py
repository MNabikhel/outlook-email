"""The morning focus digest.

One page that answers, in order: is anything dangerous, what must I do today,
what came in since yesterday and what was it about, and what is coming up.
The window is "since the start of the previous working day", so Monday's
digest covers the weekend. Open tasks carry over until they are marked done,
whenever the email arrived.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any

from controller_inbox.classify import month_end
from controller_inbox.models import (
    DOCUMENT_LABELS,
    FOLDER_LABELS,
    IMPORTANCE_LABELS,
    DocumentType,
    EmailRecord,
    Importance,
)
from controller_inbox.store import Store

FOCUS_LIMIT = 7

FOLDER_HEADINGS = {
    "important": "Needs you",
    "informational": "Worth knowing",
    "reference": "Filed for reference",
}

_PRIORITY_WEIGHT = {"critical": 60, "high": 40, "medium": 10, "low": 0}


def digest_window(as_of: date, tz: tzinfo, lookback_days: int = 1) -> tuple[datetime, datetime]:
    """Start of the previous working day (Friday for a Monday) through the end of ``as_of``."""
    start_day = as_of - timedelta(days=max(1, lookback_days))
    while start_day.weekday() >= 5:
        start_day -= timedelta(days=1)
    start = datetime.combine(start_day, time.min, tzinfo=tz)
    end = datetime.combine(as_of + timedelta(days=1), time.min, tzinfo=tz)
    return start, end


def build_digest(
    store: Store,
    *,
    as_of: date,
    generated_at: datetime,
    tz: tzinfo | None = None,
    lookback_days: int = 1,
    save: bool = True,
) -> dict[str, Any]:
    tz = tz or generated_at.tzinfo or timezone.utc
    start, end = digest_window(as_of, tz, lookback_days)
    window_emails = store.list_emails(
        received_from=_utc(start),
        received_before=_utc(end),
        order="score",
        limit=1000,
    )
    total_emails = store.counts()["emails"]
    open_actions = store.list_actions(status="open")
    # Phone-verification tasks are reported as fraud alerts, not as overdue work.
    dated = [(a, e) for a, e in open_actions if a.source != "fraud_rule"]
    today = as_of.isoformat()
    week_end = (as_of + timedelta(days=7)).isoformat()

    overdue = [(a, e) for a, e in dated if a.due_date and a.due_date < today]
    due_today = [(a, e) for a, e in dated if a.due_date == today]
    due_week = [(a, e) for a, e in dated if a.due_date and today < a.due_date <= week_end]
    undated = [(a, e) for a, e in open_actions if not a.due_date]

    emails_by_id: dict[str, EmailRecord] = {e.id: e for e in window_emails}

    def full(email_id: str) -> EmailRecord | None:
        if email_id not in emails_by_id:
            found = store.get_email(email_id)
            if found is None:
                return None
            emails_by_id[email_id] = found
        return emails_by_id[email_id]

    # A payment-change warning stays at the top until its verify task is done.
    critical_alerts = [e for e in window_emails if _is_fraud(e)]
    alert_ids = {e.id for e in critical_alerts}
    for _action, stub in open_actions:
        email = full(stub.id)
        if email is not None and email.id not in alert_ids and _is_fraud(email):
            critical_alerts.append(email)
            alert_ids.add(email.id)
    other_critical = [e for e in window_emails if e.importance == Importance.CRITICAL and e.id not in alert_ids]
    high = [e for e in window_emails if e.importance in {Importance.CRITICAL, Importance.HIGH}]
    invoices = [e for e in window_emails if e.category == DocumentType.AP_INVOICE]
    cash = [e for e in window_emails if e.category == DocumentType.REMITTANCE_ADVICE]
    close_items = [
        e
        for e in window_emails
        if e.category in {DocumentType.BANK_STATEMENT, DocumentType.BANK_RECONCILIATION, DocumentType.WORKPAPER}
        or "month_end" in e.flags
    ]

    focus = _focus(as_of, window_emails, open_actions, full)
    by_folder = {name: [e for e in window_emails if (e.folder or "informational") == name] for name in FOLDER_LABELS}
    need_you = len(by_folder["important"])
    newsletters = sum(1 for e in by_folder["informational"] if e.category == DocumentType.NEWSLETTER)

    close_day = month_end(as_of)
    kpis = {
        "emails": len(window_emails),
        "total_emails": total_emails,
        "need_you": need_you,
        "worth_knowing": len(by_folder["informational"]),
        "filed": len(by_folder["reference"]),
        "high_importance": len(high),
        "open_actions": len(open_actions),
        "overdue_actions": len(overdue),
        "due_today": len(due_today),
        "fraud_alerts": len(critical_alerts),
        "attachments": sum(len(e.attachments) for e in window_emails),
        "waiting_on_bionic": sum(1 for e in window_emails if e.model_status == "script_draft"),
        "read_by_bionic": sum(1 for e in window_emails if e.model_status == "bionic"),
        "skippable": newsletters,
    }
    payload = {
        "date": today,
        "date_long": _long_date(as_of),
        "generated_at": generated_at.isoformat(),
        "window": {
            "start": _utc(start),
            "end": _utc(end),
            "since_label": _long_date(start.date(), short=True),
        },
        "headline": _headline(kpis, _long_date(start.date(), short=True)),
        "days_to_close": (close_day - as_of).days,
        "close_date": close_day.isoformat(),
        "kpis": kpis,
        "focus": focus,
        "new_mail": {name: [_email_card(e) for e in rows] for name, rows in by_folder.items()},
        "folder_counts": {name: len(rows) for name, rows in by_folder.items()},
        "category_counts": dict(Counter(e.category.value for e in window_emails)),
        "attachment_counts": dict(Counter(att.document_type.value for e in window_emails for att in e.attachments)),
        "critical_alerts": [_email_card(e) for e in critical_alerts],
        "other_critical": [_email_card(e) for e in other_critical],
        "high_importance": [_email_card(e) for e in high],
        "overdue_actions": [_action_card(a, e, as_of) for a, e in overdue],
        "due_today": [_action_card(a, e, as_of) for a, e in due_today],
        "due_this_week": [_action_card(a, e, as_of) for a, e in due_week],
        "undated_actions": [_action_card(a, e, as_of) for a, e in undated],
        "invoices_to_enter": [_email_card(e) for e in invoices],
        "cash_to_apply": [_email_card(e) for e in cash],
        "close_items": [_email_card(e) for e in close_items],
    }
    if not save:
        return payload
    markdown = render_markdown(payload)
    html = render_html(payload)
    store.save_digest(today, generated_at.isoformat(), markdown, html, payload)
    return payload | {"markdown": markdown, "html": html}


def _focus(as_of: date, window_emails, open_actions, full) -> list[dict[str, Any]]:
    """Rank what the manager should do first. One row per email, best reason wins."""
    today = as_of.isoformat()
    window_ids = {e.id for e in window_emails}
    best: dict[str, dict[str, Any]] = {}
    task_counts: Counter[str] = Counter()

    for action, stub in open_actions:
        email = full(stub.id)
        if email is None:
            continue
        task_counts[email.id] += 1
        due = action.due_date
        fraud = _is_fraud(email)
        if fraud:
            score, kind, label = 1000, "fraud", "Verify by phone"
        elif due and due < today:
            days = (as_of - date.fromisoformat(due)).days
            score, kind, label = 500 + min(days, 30), "overdue", f"Overdue {days} day{'s' if days != 1 else ''}"
        elif due == today:
            score, kind, label = 400, "due_today", "Due today"
        elif due and due <= (as_of + timedelta(days=3)).isoformat():
            score, kind, label = 300, "due_soon", f"Due {_short_day(due)}"
        elif due and due <= (as_of + timedelta(days=7)).isoformat():
            score, kind, label = 200, "due_week", f"Due {_short_day(due)}"
        elif email.id in window_ids and email.folder == "important":
            score, kind, label = 100, "new_task", "New task"
        else:
            continue
        score += _PRIORITY_WEIGHT.get(action.priority.value, 0) + email.importance_score // 10
        row = _focus_row(email, kind, label, score, action=action)
        if email.id not in best or best[email.id]["score"] < score:
            best[email.id] = row

    for email in window_emails:
        if email.id in best or email.folder != "important":
            continue
        kind, label = ("fraud", "Verify by phone") if _is_fraud(email) else ("decide", "New · needs a look")
        score = (1000 if kind == "fraud" else 50) + email.importance_score // 2
        best[email.id] = _focus_row(email, kind, label, score)

    ranked = sorted(best.values(), key=lambda row: (-row["score"], row.get("due") or "9999", row["subject"]))
    for index, row in enumerate(ranked[:FOCUS_LIMIT], start=1):
        row["rank"] = index
        row["more_tasks"] = max(0, task_counts.get(row["email_id"], 0) - (1 if row.get("action_id") else 0))
    return ranked[:FOCUS_LIMIT]


def _focus_row(email: EmailRecord, kind: str, label: str, score: int, *, action=None) -> dict[str, Any]:
    return {
        "kind": kind,
        "label": label,
        "score": score,
        "title": action.title if action else email.subject,
        "summary": email.summary,
        "subject": email.subject,
        "sender": email.sender_name or email.sender_email,
        "email_id": email.id,
        "action_id": action.id if action else "",
        "due": action.due_date if action else email.extracted.primary_due,
        "amount": email.extracted.primary_amount,
        "folder": email.folder,
        "model_status": email.model_status,
        "why": next((r for r in email.importance_reasons if r), ""),
    }


def _headline(kpis: dict[str, int], since: str) -> str:
    emails = kpis["emails"]
    if not emails and not kpis["open_actions"]:
        return f"Nothing new since {since}, and no open tasks."
    parts = [f"{kpis['need_you']} of {emails} email{'s' if emails != 1 else ''} since {since} need you"]
    if kpis["overdue_actions"]:
        parts.append(f"{kpis['overdue_actions']} task{'s' if kpis['overdue_actions'] != 1 else ''} overdue")
    if kpis["fraud_alerts"]:
        parts.append(
            f"{kpis['fraud_alerts']} payment-change warning{'s' if kpis['fraud_alerts'] != 1 else ''}"
        )
    return ". ".join(parts) + "."


def _is_fraud(email: EmailRecord) -> bool:
    return "fraud_risk" in email.flags or email.category == DocumentType.PAYMENT_INSTRUCTION_CHANGE


def _email_card(email: EmailRecord) -> dict[str, Any]:
    return {
        "id": email.id,
        "subject": email.subject,
        "sender": email.sender_name or email.sender_email,
        "sender_email": email.sender_email,
        "received_at": email.received_at,
        "category": email.category.value,
        "category_label": DOCUMENT_LABELS.get(email.category, email.category.value),
        "importance": email.importance.value,
        "importance_label": IMPORTANCE_LABELS.get(email.importance, email.importance.value),
        "score": email.importance_score,
        "flags": email.flags,
        "folder": email.folder,
        "summary": email.summary,
        "model_status": email.model_status,
        "invoice": email.extracted.primary_invoice,
        "amount": email.extracted.primary_amount,
        "due": email.extracted.primary_due,
        "attachment_count": len(email.attachments),
        "reasons": email.importance_reasons,
    }


def _action_card(action, email, as_of: date | None = None) -> dict[str, Any]:
    return {
        "id": action.id,
        "email_id": email.id,
        "title": action.title,
        "detail": action.detail,
        "due_date": action.due_date,
        "due_label": _short_day(action.due_date) if action.due_date else "",
        "priority": action.priority.value,
        "source": action.source,
        "subject": email.subject,
        "sender": email.sender_name or email.sender_email,
        "category": email.category.value,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    k = payload["kpis"]
    since = payload["window"]["since_label"]
    lines = [
        f"# CloseDesk daily digest — {payload['date_long']}",
        "",
        f"**{payload['headline']}**",
        "",
    ]
    if payload["critical_alerts"]:
        lines += ["## Do not process — verify by phone", ""]
        for item in payload["critical_alerts"]:
            lines.append(f"- **{item['subject']}** from {item['sender']}. {item['summary']}")
        lines.append("")
    lines += ["## Your focus today", ""]
    if payload["focus"]:
        for row in payload["focus"]:
            extra = f" (+{row['more_tasks']} more)" if row.get("more_tasks") else ""
            lines.append(f"{row['rank']}. **{row['title']}** — {row['label']}{extra}")
            detail = row["summary"] or row["subject"]
            lines.append(f"   {detail} · from {row['sender']}")
    else:
        lines.append("Nothing needs you right now.")
    lines.append("")
    lines += [f"## What came in since {since}", ""]
    for folder, heading in FOLDER_HEADINGS.items():
        rows = payload["new_mail"].get(folder, [])
        lines.append(f"### {heading} ({len(rows)})")
        if not rows:
            lines.append("- None.")
        for item in rows[:25]:
            summary = f" — {item['summary']}" if item["summary"] and folder != "reference" else ""
            lines.append(f"- **{item['subject']}** · {item['sender']}{summary}")
        if len(rows) > 25:
            lines.append(f"- …and {len(rows) - 25} more in the dashboard.")
        lines.append("")
    lines += ["## Coming up in the next 7 days", ""]
    lines += _md_actions(payload["due_this_week"], "Nothing else is dated this week.")
    lines.append("")
    finance = [
        ("Invoices to enter", payload["invoices_to_enter"]),
        ("Cash to apply", payload["cash_to_apply"]),
        ("Close / reconcile", payload["close_items"]),
    ]
    if any(rows for _, rows in finance):
        lines += [f"## Month-end ({payload['days_to_close']} day(s) to {payload['close_date']})", ""]
        for heading, rows in finance:
            if not rows:
                continue
            lines.append(f"### {heading}")
            for item in rows:
                amount = f" · ${item['amount']:,.2f}" if item["amount"] is not None else ""
                due = f" · due {item['due']}" if item["due"] else ""
                lines.append(f"- {item['invoice'] or item['subject']}{amount}{due}")
            lines.append("")
    lines += [
        "## Snapshot",
        f"- New emails: {k['emails']} (needs you {k['need_you']}, worth knowing {k['worth_knowing']}, filed {k['filed']})",
        f"- Open tasks: {k['open_actions']} ({k['overdue_actions']} overdue, {k['due_today']} due today)",
        f"- Read by the local model: {k['read_by_bionic']} · waiting: {k['waiting_on_bionic']}",
        f"- Attachments read: {k['attachments']}",
        "",
    ]
    return "\n".join(lines)


def _md_actions(items: list[dict[str, Any]], empty: str) -> list[str]:
    if not items:
        return [f"- {empty}"]
    return [f"- **{item['title']}** — due {item['due_label'] or item['due_date']} · {item['subject']}" for item in items]


def render_html(payload: dict[str, Any]) -> str:
    """Self-contained page: open it in a browser, print it, or paste it into an email."""
    k = payload["kpis"]
    since = payload["window"]["since_label"]

    def rows(items: list[dict[str, Any]], empty: str, *, summary: bool = True) -> str:
        if not items:
            return f"<p class='empty'>{_esc(empty)}</p>"
        out = []
        for item in items:
            title = item.get("title") or item.get("subject")
            meta = [item.get("sender") or ""]
            if item.get("due_label") or item.get("due"):
                meta.append("due " + str(item.get("due_label") or item.get("due")))
            if item.get("amount") is not None:
                meta.append(f"${item['amount']:,.2f}")
            text = item.get("summary") if summary else ""
            out.append(
                f"<li><strong>{_esc(title)}</strong><span>{_esc(' · '.join(m for m in meta if m))}</span>"
                + (f"<p>{_esc(text)}</p>" if text else "")
                + "</li>"
            )
        return "<ul class='list'>" + "".join(out) + "</ul>"

    focus = "".join(
        f"<li><b class='rank'>{row['rank']}</b><div><strong>{_esc(row['title'])}</strong>"
        f"<em class='tag {row['kind']}'>{_esc(row['label'])}</em>"
        f"<p>{_esc(row['summary'] or row['subject'])}</p><span>{_esc(row['sender'])}</span></div></li>"
        for row in payload["focus"]
    ) or "<li class='empty'>Nothing needs you right now.</li>"
    alerts = ""
    if payload["critical_alerts"]:
        alerts = (
            "<section class='alert'><h2>Do not process — verify by phone</h2>"
            + rows(payload["critical_alerts"], "")
            + "</section>"
        )
    new_mail = "".join(
        f"<h3>{heading} <small>{len(payload['new_mail'].get(folder, []))}</small></h3>"
        + rows(payload["new_mail"].get(folder, []), "None.", summary=folder != "reference")
        for folder, heading in FOLDER_HEADINGS.items()
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>CloseDesk digest {payload['date']}</title>
  <style>
    body {{ font-family: Georgia, 'Times New Roman', serif; background:#f4f1ea; color:#1b241b; margin:0; }}
    main {{ max-width: 760px; margin: 0 auto; padding: 32px 20px 64px; }}
    h1 {{ font-size: 28px; margin: 0 0 4px; }}
    .lede {{ color:#4c584c; margin: 0 0 20px; font-size: 18px; }}
    h2 {{ font-size:19px; border-bottom:1px solid #d9d1c3; padding-bottom:6px; margin-top:30px; }}
    h3 {{ font-size:15px; margin: 18px 0 4px; font-family: system-ui, sans-serif; }}
    h3 small {{ color:#6a746a; font-weight: normal; }}
    ul.list, ol.focus {{ list-style:none; padding:0; margin: 0; }}
    ul.list li {{ background:#fff; border:1px solid #d9d1c3; padding:10px 12px; margin:8px 0; }}
    ul.list li span, ol.focus span {{ display:block; color:#5b675b; font-size:13px; font-family: system-ui, sans-serif; }}
    ul.list li p, ol.focus p {{ margin: 4px 0 0; font-size: 14px; }}
    ol.focus li {{ display:flex; gap:12px; background:#fff; border:1px solid #d9d1c3; padding:12px; margin:8px 0; }}
    .rank {{ font-size: 20px; min-width: 26px; color:#1f5130; }}
    .tag {{ display:inline-block; margin-left:8px; font-style:normal; font-size:12px; font-family: system-ui, sans-serif;
            padding:1px 8px; border-radius: 10px; background:#e7efe5; color:#1f5130; }}
    .tag.fraud, .tag.overdue {{ background:#9b2335; color:#fff; }}
    .tag.due_today {{ background:#b8651b; color:#fff; }}
    .empty {{ color:#6a746a; font-style:italic; }}
    .alert {{ background:#9b2335; color:#fff; padding:4px 14px 10px; margin-top: 16px; }}
    .alert h2 {{ color:#fff; border-color: rgba(255,255,255,.4); margin-top: 12px; }}
    .alert li {{ color:#1b241b; }}
    .stats {{ color:#4c584c; font-family: system-ui, sans-serif; font-size: 13px; }}
  </style>
</head>
<body>
<main>
  <h1>Your day, {_esc(payload['date_long'])}</h1>
  <p class="lede">{_esc(payload['headline'])}</p>
  {alerts}
  <h2>Your focus today</h2>
  <ol class="focus">{focus}</ol>
  <h2>What came in since {_esc(since)}</h2>
  {new_mail}
  <h2>Coming up in the next 7 days</h2>
  {rows(payload['due_this_week'], 'Nothing else is dated this week.', summary=False)}
  <h2>Snapshot</h2>
  <p class="stats">{k['emails']} new emails · {k['open_actions']} open tasks ({k['overdue_actions']} overdue) ·
  {k['read_by_bionic']} read by the local model · {k['waiting_on_bionic']} waiting · month-end {payload['close_date']}
  ({payload['days_to_close']} days)</p>
</main>
</body>
</html>
"""


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _long_date(value: date, *, short: bool = False) -> str:
    if short:
        return f"{value.strftime('%a')} {value.strftime('%b')} {value.day}"
    return f"{value.strftime('%A')}, {value.strftime('%B')} {value.day}, {value.year}"


def _short_day(iso: str) -> str:
    try:
        return _long_date(date.fromisoformat(iso), short=True)
    except (TypeError, ValueError):
        return iso or ""


def _esc(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def write_digest_files(payload: dict[str, Any], digest_dir, period: str) -> tuple[str, str]:
    digest_dir.mkdir(parents=True, exist_ok=True)
    md_path = digest_dir / f"{period}.md"
    html_path = digest_dir / f"{period}.html"
    md_path.write_text(payload["markdown"], encoding="utf-8")
    html_path.write_text(payload["html"], encoding="utf-8")
    (digest_dir / f"{period}.json").write_text(
        json.dumps({k: v for k, v in payload.items() if k not in {"markdown", "html"}}, indent=2, default=str),
        encoding="utf-8",
    )
    return str(md_path), str(html_path)
