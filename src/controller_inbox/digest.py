from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from controller_inbox.classify import month_end
from controller_inbox.models import DOCUMENT_LABELS, IMPORTANCE_LABELS, DocumentType, Importance
from controller_inbox.store import Store


def build_digest(store: Store, *, as_of: date, generated_at: datetime) -> dict[str, Any]:
    emails = store.list_emails(limit=500)
    open_actions = store.list_actions(status="open")
    today = as_of.isoformat()
    week_end = (as_of + timedelta(days=7)).isoformat()

    overdue = [(a, e) for a, e in open_actions if a.due_date and a.due_date < today]
    due_today = [(a, e) for a, e in open_actions if a.due_date == today]
    due_week = [
        (a, e)
        for a, e in open_actions
        if a.due_date and today < a.due_date <= week_end
    ]
    undated = [(a, e) for a, e in open_actions if not a.due_date]

    critical_alerts = [
        e
        for e in emails
        if "fraud_risk" in e.flags or e.category == DocumentType.PAYMENT_INSTRUCTION_CHANGE
    ]
    other_critical = [
        e
        for e in emails
        if e.importance == Importance.CRITICAL and e.id not in {x.id for x in critical_alerts}
    ]
    high = [e for e in emails if e.importance in {Importance.CRITICAL, Importance.HIGH}]
    invoices = [e for e in emails if e.category == DocumentType.AP_INVOICE]
    cash = [e for e in emails if e.category == DocumentType.REMITTANCE_ADVICE]
    close_items = [
        e
        for e in emails
        if e.category
        in {
            DocumentType.BANK_STATEMENT,
            DocumentType.BANK_RECONCILIATION,
            DocumentType.WORKPAPER,
        }
        or "month_end" in e.flags
    ]

    close_day = month_end(as_of)
    payload = {
        "date": today,
        "generated_at": generated_at.isoformat(),
        "days_to_close": (close_day - as_of).days,
        "close_date": close_day.isoformat(),
        "kpis": {
            "emails": len(emails),
            "high_importance": len(high),
            "open_actions": len(open_actions),
            "overdue_actions": len(overdue),
            "fraud_alerts": len([e for e in emails if "fraud_risk" in e.flags]),
            "attachments": sum(len(e.attachments) for e in emails),
            "waiting_on_bionic": sum(1 for e in emails if e.model_status == "script_draft"),
        },
        "folder_counts": {
            "important": sum(1 for e in emails if e.folder == "important"),
            "informational": sum(1 for e in emails if e.folder == "informational"),
            "reference": sum(1 for e in emails if e.folder == "reference"),
        },
        "category_counts": dict(Counter(e.category.value for e in emails)),
        "attachment_counts": dict(
            Counter(att.document_type.value for e in emails for att in e.attachments)
        ),
        "critical_alerts": [_email_card(e) for e in critical_alerts],
        "other_critical": [_email_card(e) for e in other_critical],
        "high_importance": [_email_card(e) for e in high],
        "overdue_actions": [_action_card(a, e) for a, e in overdue],
        "due_today": [_action_card(a, e) for a, e in due_today],
        "due_this_week": [_action_card(a, e) for a, e in due_week],
        "undated_actions": [_action_card(a, e) for a, e in undated],
        "invoices_to_enter": [_email_card(e) for e in invoices],
        "cash_to_apply": [_email_card(e) for e in cash],
        "close_items": [_email_card(e) for e in close_items],
    }
    markdown = render_markdown(payload)
    html = render_html(payload)
    store.save_digest(today, generated_at.isoformat(), markdown, html, payload)
    return payload | {"markdown": markdown, "html": html}


def _email_card(email) -> dict[str, Any]:
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


def _action_card(action, email) -> dict[str, Any]:
    return {
        "id": action.id,
        "email_id": email.id,
        "title": action.title,
        "detail": action.detail,
        "due_date": action.due_date,
        "priority": action.priority.value,
        "source": action.source,
        "subject": email.subject,
        "sender": email.sender_name or email.sender_email,
        "category": email.category.value,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    k = payload["kpis"]
    lines = [
        f"# CloseDesk daily digest — {payload['date']}",
        "",
        f"Month-end **{payload['close_date']}** is in **{payload['days_to_close']}** day(s).",
        "",
        "## Snapshot",
        f"- Emails reviewed: {k['emails']}",
        f"- High / critical: {k['high_importance']}",
        f"- Open action items: {k['open_actions']} ({k['overdue_actions']} overdue)",
        f"- Fraud / payment-change alerts: {k['fraud_alerts']}",
        f"- Attachments classified: {k['attachments']}",
        f"- Waiting on Bionic: {k.get('waiting_on_bionic', 0)}",
        "",
        "## Morning folders",
    ]
    folders = payload.get("folder_counts") or {}
    lines.append(
        f"- Important: {folders.get('important', 0)} · Informational: {folders.get('informational', 0)} · Reference: {folders.get('reference', 0)}"
    )
    lines += [
        "",
        "## Do not process — verify by phone",
    ]
    if payload["critical_alerts"]:
        for item in payload["critical_alerts"]:
            lines.append(f"- **{item['subject']}** from {item['sender']} ({', '.join(item['flags']) or 'critical'})")
    else:
        lines.append("- None.")
    lines += ["", "## Other critical mail"]
    if payload.get("other_critical"):
        for item in payload["other_critical"]:
            lines.append(f"- **{item['subject']}** from {item['sender']} ({item['category_label']})")
    else:
        lines.append("- None.")
    lines += ["", "## Overdue"]
    lines += _md_actions(payload["overdue_actions"])
    lines += ["", "## Due today"]
    lines += _md_actions(payload["due_today"])
    lines += ["", "## Due in the next 7 days"]
    lines += _md_actions(payload["due_this_week"])
    lines += ["", "## Invoices to enter"]
    if payload["invoices_to_enter"]:
        for item in payload["invoices_to_enter"]:
            amt = f"${item['amount']:,.2f}" if item["amount"] is not None else "—"
            lines.append(f"- {item['invoice'] or item['subject']} · {amt} · due {item['due'] or 'n/a'}")
    else:
        lines.append("- None.")
    lines += ["", "## Cash to apply"]
    if payload["cash_to_apply"]:
        for item in payload["cash_to_apply"]:
            lines.append(f"- {item['subject']} from {item['sender']}")
    else:
        lines.append("- None.")
    lines += ["", "## Close / reconcile"]
    if payload["close_items"]:
        for item in payload["close_items"]:
            lines.append(f"- {item['subject']}")
    else:
        lines.append("- None.")
    lines += ["", "## Category mix"]
    for key, count in payload["category_counts"].items():
        lines.append(f"- {DOCUMENT_LABELS.get(DocumentType(key), key)}: {count}")
    lines.append("")
    return "\n".join(lines)


def _md_actions(items: list[dict[str, Any]]) -> list[str]:
    if not items:
        return ["- None."]
    return [
        f"- **{item['title']}** — due {item['due_date'] or 'n/a'} ({item['priority']})"
        for item in items
    ]


def render_html(payload: dict[str, Any]) -> str:
    k = payload["kpis"]

    def cards(items: list[dict[str, Any]], empty: str) -> str:
        if not items:
            return f"<p class='empty'>{empty}</p>"
        blocks = []
        for item in items:
            due = item.get("due_date") or item.get("due") or ""
            title = item.get("title") or item.get("subject")
            meta = item.get("sender") or item.get("priority") or ""
            blocks.append(
                f"<li><strong>{_esc(title)}</strong><span>{_esc(meta)}"
                f"{' · due ' + _esc(due) if due else ''}</span></li>"
            )
        return "<ul class='list'>" + "".join(blocks) + "</ul>"

    cats = "".join(
        f"<li>{_esc(DOCUMENT_LABELS.get(DocumentType(k_), k_))} <b>{n}</b></li>"
        for k_, n in payload["category_counts"].items()
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <title>CloseDesk digest {payload['date']}</title>
  <style>
    body {{ font-family: Georgia, 'Times New Roman', serif; background:#f4f1ea; color:#1b241b; margin:0; }}
    main {{ max-width: 760px; margin: 0 auto; padding: 32px 20px 64px; }}
    h1 {{ font-size: 28px; margin-bottom: 6px; }}
    .lede {{ color:#4c584c; margin-bottom: 24px; }}
    .kpis {{ display:grid; grid-template-columns: repeat(3,1fr); gap:10px; margin: 20px 0 28px; }}
    .kpi {{ background:#fff; border:1px solid #d9d1c3; padding:12px 14px; }}
    .kpi b {{ display:block; font-size:22px; }}
    h2 {{ font-size:18px; border-bottom:1px solid #d9d1c3; padding-bottom:6px; margin-top:28px; }}
    ul.list {{ list-style:none; padding:0; }}
    ul.list li {{ background:#fff; border:1px solid #d9d1c3; padding:10px 12px; margin:8px 0; display:flex; flex-direction:column; gap:4px; }}
    ul.list span {{ color:#5b675b; font-size:13px; font-family: system-ui, sans-serif; }}
    .empty {{ color:#6a746a; font-style:italic; }}
    .alert {{ background:#9b2335; color:#fff; padding:12px 14px; }}
    .alert li {{ background:transparent; border:0; color:#fff; padding:6px 0; }}
  </style>
</head>
<body>
<main>
  <h1>CloseDesk daily digest</h1>
  <p class="lede">{payload['date']} · month-end {payload['close_date']} · {payload['days_to_close']} day(s) to close · waiting on Bionic {k.get('waiting_on_bionic', 0)}</p>
  <div class="kpis">
    <div class="kpi"><span>Emails</span><b>{k['emails']}</b></div>
    <div class="kpi"><span>High / critical</span><b>{k['high_importance']}</b></div>
    <div class="kpi"><span>Open actions</span><b>{k['open_actions']}</b></div>
    <div class="kpi"><span>Overdue</span><b>{k['overdue_actions']}</b></div>
    <div class="kpi"><span>Fraud alerts</span><b>{k['fraud_alerts']}</b></div>
    <div class="kpi"><span>Attachments</span><b>{k['attachments']}</b></div>
  </div>
  <h2>Do not process — verify by phone</h2>
  {cards(payload['critical_alerts'], 'No fraud or critical alerts.')}
  <h2>Overdue</h2>
  {cards(payload['overdue_actions'], 'Nothing overdue.')}
  <h2>Due today</h2>
  {cards(payload['due_today'], 'Nothing due today.')}
  <h2>Due this week</h2>
  {cards(payload['due_this_week'], 'Nothing else due this week.')}
  <h2>Invoices to enter</h2>
  {cards(payload['invoices_to_enter'], 'No vendor invoices in the current window.')}
  <h2>Cash to apply</h2>
  {cards(payload['cash_to_apply'], 'No remittances waiting.')}
  <h2>Close / reconcile</h2>
  {cards(payload['close_items'], 'No close items in the current window.')}
  <h2>Category mix</h2>
  <ul class="list">{cats}</ul>
</main>
</body>
</html>
"""


def _esc(value: Any) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


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
