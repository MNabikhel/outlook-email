from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from controller_inbox.actions import local_today
from controller_inbox.config import Settings, load_settings
from controller_inbox.demo import DemoMailbox
from controller_inbox.digest import build_digest, write_digest_files
from controller_inbox.models import (
    DOCUMENT_LABELS,
    IMPORTANCE_LABELS,
    TRIAGE_BIN_LABELS,
    TRIAGE_BIN_ORDER,
    TriageBin,
)
from controller_inbox.pipeline import ingest_demo, ingest_mailbox
from controller_inbox.store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="controller-inbox",
        description="CloseDesk: classify Outlook mail and attachments, then produce a daily AP/close action list.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    demo = sub.add_parser("demo", help="Load a realistic assistant-controller mailbox (no Outlook login).")
    demo.add_argument("--serve", action="store_true", help="Start the dashboard after loading demo mail.")
    demo.add_argument("--host", default=None)
    demo.add_argument("--port", type=int, default=None)
    demo.add_argument("--reset", action="store_true", default=True, help="Replace the local database (default).")

    sub.add_parser("auth", help="Sign in to Microsoft 365 with a device code.")

    triage = sub.add_parser("triage", help="Group the current inbox into triage bins (agent-friendly).")
    triage.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of a table.")
    triage.add_argument("--bin", default=None, help="Only show one bin (e.g. action_required, fraud_review).")

    sub.add_parser("llm-check", help="Check that the local LLM server (LM Studio / Ollama / Bionic) is reachable.")

    sync = sub.add_parser("sync", help="Pull recent Outlook mail via Microsoft Graph and classify it.")
    sync.add_argument("--hours", type=int, default=None, help="Lookback window (defaults to CONTROLLER_INBOX_LOOKBACK_HOURS).")

    watch = sub.add_parser("watch", help="Poll Outlook and write a daily digest at the configured hour.")
    watch.add_argument("--once", action="store_true")

    digest = sub.add_parser("digest", help="Rebuild today's action-item digest from the local database.")
    digest.add_argument("--date", default=None, help="YYYY-MM-DD (defaults to today in the configured timezone).")
    digest.add_argument("--send", action="store_true", help="Email the digest via Graph if CONTROLLER_INBOX_DIGEST_TO is set.")
    digest.add_argument("--json", action="store_true", help="Print the digest payload as JSON instead of Markdown.")
    digest.add_argument("--history", action="store_true", help="List stored past digests and exit.")

    serve = sub.add_parser("serve", help="Open the local CloseDesk dashboard.")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)

    export = sub.add_parser("export", help="Write open action items to CSV (stdout).")
    export.add_argument("--output", default=None)

    status = sub.add_parser("status", help="Show local database counts and connection state.")
    status.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    args = parser.parse_args(argv)
    settings = load_settings()
    store = Store(settings.db_path)

    if args.cmd == "demo":
        store.reset()
        now = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)
        records = ingest_demo(store, settings, now=now)
        as_of = local_today(settings.tz, now)
        payload = build_digest(store, as_of=as_of, generated_at=now)
        write_digest_files(payload, settings.digest_dir, as_of.isoformat())
        _print_run_summary(records, payload)
        if args.serve:
            return _serve(settings, store, host=args.host, port=args.port)
        print("\nNext: python -m controller_inbox serve")
        return 0

    if args.cmd == "auth":
        mailbox = _graph_mailbox(settings)
        user = mailbox.client.signed_in_user()
        print(f"Signed in as {user.get('displayName')} <{user.get('mail') or user.get('userPrincipalName')}>")
        return 0

    if args.cmd == "sync":
        mailbox = _graph_mailbox(settings)
        hours = args.hours if args.hours is not None else settings.lookback_hours
        after = datetime.now(timezone.utc) - timedelta(hours=hours)
        records = ingest_mailbox(mailbox, store, settings, received_after=after)
        print(f"Classified {len(records)} message(s).")
        _print_run_summary(records, None)
        return 0

    if args.cmd == "watch":
        return _watch(settings, store, once=args.once)

    if args.cmd == "triage":
        return _triage(settings, store, only_bin=args.bin, as_json=args.json)

    if args.cmd == "llm-check":
        return _llm_check(settings)

    if args.cmd == "digest":
        if args.history:
            return _digest_history(store, as_json=args.json)
        now = datetime.now(settings.tz)
        as_of = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else local_today(settings.tz, now)
        payload = build_digest(store, as_of=as_of, generated_at=now)
        md_path, html_path = write_digest_files(payload, settings.digest_dir, as_of.isoformat())
        if args.json:
            print(json.dumps({k: v for k, v in payload.items() if k not in {"markdown", "html"}}, indent=2, default=str))
            return 0
        print(payload["markdown"])
        print(f"\nWrote {md_path}\nWrote {html_path}")
        if args.send:
            _send_digest(settings, payload, as_of.isoformat())
        return 0

    if args.cmd == "serve":
        return _serve(settings, store, host=args.host, port=args.port)

    if args.cmd == "export":
        text = export_actions_csv(store)
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
            print(f"Wrote {args.output}")
        else:
            sys.stdout.write(text)
        return 0

    if args.cmd == "status":
        counts = store.counts()
        bins = store.bin_counts()
        if args.json:
            print(
                json.dumps(
                    {
                        "database": str(settings.db_path),
                        "graph_configured": settings.graph_configured,
                        "llm_configured": settings.llm_configured,
                        "llm_endpoint": settings.llm_endpoint if settings.llm_configured else None,
                        "last_sync": store.get_state("last_sync_at"),
                        "counts": counts,
                        "bins": bins,
                    },
                    indent=2,
                )
            )
            return 0
        print(f"Database: {settings.db_path}")
        print(f"Graph configured: {settings.graph_configured}")
        print(f"Local LLM: {'on · ' + settings.llm_endpoint if settings.llm_configured else 'off (rules only)'}")
        print(f"Last sync: {store.get_state('last_sync_at') or 'never'}")
        for key, value in counts.items():
            print(f"{key}: {value}")
        if bins:
            print("bins: " + ", ".join(f"{_bin_label(k)}={v}" for k, v in bins.items()))
        return 0

    return 1


def _bin_label(key: str) -> str:
    try:
        return TRIAGE_BIN_LABELS.get(TriageBin(key), key)
    except ValueError:
        return key


def _triage(settings: Settings, store: Store, *, only_bin: str | None, as_json: bool) -> int:
    emails = store.list_emails(limit=500)
    groups: dict[str, list] = {b.value: [] for b in TRIAGE_BIN_ORDER}
    for email in emails:
        groups.setdefault(email.triage_bin.value, []).append(email)
    if only_bin:
        wanted = only_bin.strip().lower()
        groups = {k: v for k, v in groups.items() if k == wanted}
        if not groups:
            print(f"Unknown bin '{only_bin}'. Choose from: {', '.join(b.value for b in TRIAGE_BIN_ORDER)}", file=sys.stderr)
            return 2

    if as_json:
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "bins": [
                {
                    "bin": bin_value,
                    "label": _bin_label(bin_value),
                    "count": len(items),
                    "emails": [
                        {
                            "id": e.id,
                            "subject": e.subject,
                            "sender": e.sender_name or e.sender_email,
                            "importance": e.importance.value,
                            "category": e.category.value,
                            "summary": e.summary,
                            "ai_source": e.ai_source,
                            "amount": e.extracted.primary_amount,
                            "due": e.extracted.primary_due,
                            "actions": [a.title for a in e.actions],
                        }
                        for e in items
                    ],
                }
                for bin_value, items in groups.items()
            ],
        }
        print(json.dumps(payload, indent=2, default=str))
        return 0

    if not emails:
        print("No mail yet. Run: python -m controller_inbox demo   (or sync)")
        return 0
    for bin_value, items in groups.items():
        if not items:
            continue
        print(f"\n=== {_bin_label(bin_value)} ({len(items)}) ===")
        for e in sorted(items, key=lambda r: r.importance_score, reverse=True):
            tag = "*" if e.ai_source.startswith("llm") else " "
            print(f" {tag}[{e.importance.value:8}] {e.subject[:70]}")
            if e.summary:
                print(f"      {e.summary[:100]}")
    return 0


def _digest_history(store: Store, *, as_json: bool) -> int:
    history = store.list_digests()
    if as_json:
        print(json.dumps(history, indent=2, default=str))
        return 0
    if not history:
        print("No digests stored yet. Run: python -m controller_inbox digest")
        return 0
    print("Stored daily digests (newest first):")
    for row in history:
        k = row.get("kpis", {})
        print(
            f"  {row['period_date']}  ·  {k.get('emails', 0)} emails, "
            f"{k.get('open_actions', 0)} open actions, {k.get('fraud_alerts', 0)} fraud alert(s)"
        )
    return 0


def _llm_check(settings: Settings) -> int:
    from controller_inbox.llm import LocalLLMClient

    endpoint = settings.llm_endpoint
    print(f"Local LLM endpoint: {endpoint}")
    print(f"Model: {settings.llm_model}")
    print(f"Enabled (CONTROLLER_INBOX_LLM): {settings.llm}")
    if not settings.llm:
        print(
            "\nLLM enrichment is OFF, so CloseDesk uses deterministic rules only.\n"
            "To turn it on: set CONTROLLER_INBOX_LLM=true and start LM Studio / Ollama / Bionic,\n"
            "then set CONTROLLER_INBOX_LLM_BASE_URL if it is not the LM Studio default."
        )
    client = LocalLLMClient(settings)
    try:
        models = client.list_models()
    except Exception as exc:  # noqa: BLE001 - connectivity probe
        print(f"\nCould not reach the local model server: {exc}")
        print("Is LM Studio / Ollama / Bionic running and serving an OpenAI-compatible API?")
        return 1
    print(f"\nConnected. {len(models)} model(s) available:")
    for name in models[:20]:
        marker = "  <- selected" if name == settings.llm_model else ""
        print(f"  - {name}{marker}")
    return 0


def _print_run_summary(records, payload) -> None:
    print(f"Processed {len(records)} email(s).")
    for rec in sorted(records, key=lambda r: r.importance_score, reverse=True)[:8]:
        flags = f" [{', '.join(rec.flags)}]" if rec.flags else ""
        print(
            f"  {IMPORTANCE_LABELS[rec.importance]:<8} {DOCUMENT_LABELS[rec.category]:<28} "
            f"{rec.subject[:70]}{flags}"
        )
    if payload:
        k = payload["kpis"]
        print(
            f"\nDigest {payload['date']}: {k['open_actions']} open actions, "
            f"{k['overdue_actions']} overdue, {k['fraud_alerts']} fraud alert(s)."
        )


def _graph_mailbox(settings: Settings):
    if not settings.graph_configured:
        raise SystemExit(
            "Azure is not configured. Set AZURE_CLIENT_ID (and optionally AZURE_TENANT_ID) "
            "in .env — see README — or run: python -m controller_inbox demo --serve"
        )
    from controller_inbox.graph import GraphClient, GraphMailbox

    client = GraphClient(
        client_id=settings.azure_client_id,
        tenant_id=settings.azure_tenant_id,
        client_secret=settings.azure_client_secret,
        mailbox=settings.mailbox,
        cache_path=settings.token_cache_path,
    )
    return GraphMailbox(client)


def _serve(settings: Settings, store: Store, host: str | None, port: int | None) -> int:
    import uvicorn

    from controller_inbox.web import create_app

    app = create_app(settings, store)
    uvicorn.run(app, host=host or settings.host, port=port or settings.port, log_level="info")
    return 0


def _watch(settings: Settings, store: Store, *, once: bool) -> int:
    import time

    mailbox: DemoMailbox | object
    if settings.graph_configured:
        mailbox = _graph_mailbox(settings)
    else:
        print("Graph is not configured; watch will refresh the demo mailbox.")
        mailbox = DemoMailbox()

    def tick() -> None:
        after = None
        last = store.get_state("last_sync_at")
        if last:
            after = datetime.fromisoformat(last)
        else:
            after = datetime.now(timezone.utc) - timedelta(hours=settings.lookback_hours)
        if isinstance(mailbox, DemoMailbox):
            after = None
        records = ingest_mailbox(mailbox, store, settings, received_after=after)
        print(f"{datetime.now().isoformat(timespec='seconds')} classified {len(records)} message(s)")
        now = datetime.now(settings.tz)
        as_of = local_today(settings.tz, now)
        if now.hour >= settings.digest_hour:
            existing = store.get_digest(as_of.isoformat())
            if not existing or once:
                payload = build_digest(store, as_of=as_of, generated_at=now)
                write_digest_files(payload, settings.digest_dir, as_of.isoformat())
                print(f"Digest {as_of.isoformat()} written ({payload['kpis']['open_actions']} open actions).")
                if settings.digest_to and settings.graph_configured:
                    _send_digest(settings, payload, as_of.isoformat())

    tick()
    if once:
        return 0
    while True:
        time.sleep(max(30, settings.poll_seconds))
        tick()


def _send_digest(settings: Settings, payload: dict, period: str) -> None:
    if not settings.digest_to:
        raise SystemExit("CONTROLLER_INBOX_DIGEST_TO is not set.")
    mailbox = _graph_mailbox(settings)
    subject = f"CloseDesk daily digest — {period} ({payload['kpis']['open_actions']} open actions)"
    mailbox.send_mail(settings.digest_to, subject, payload["html"])
    print(f"Sent digest to {settings.digest_to}")


def export_actions_csv(store: Store) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["priority", "due_date", "status", "title", "subject", "sender", "category", "source", "email_id"]
    )
    for action, email in store.list_actions(status=None):
        writer.writerow(
            [
                action.priority.value,
                action.due_date or "",
                action.status.value,
                action.title,
                email.subject,
                email.sender_email,
                email.category.value,
                action.source,
                email.id,
            ]
        )
    return buf.getvalue()


if __name__ == "__main__":
    raise SystemExit(main())
