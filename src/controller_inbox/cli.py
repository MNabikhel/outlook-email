from __future__ import annotations

import argparse
import csv
import io
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from controller_inbox.actions import local_today
from controller_inbox.config import Settings, load_settings
from controller_inbox.demo import DemoMailbox
from controller_inbox.digest import build_digest, write_digest_files
from controller_inbox.models import DOCUMENT_LABELS, IMPORTANCE_LABELS
from controller_inbox.pipeline import ingest_demo, ingest_mailbox
from controller_inbox.store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="controller-inbox",
        description="CloseDesk: read Outlook mail or a drop folder, classify attachments, and keep a daily action list.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    demo = sub.add_parser("demo", help="Load a sample mailbox (no Outlook login).")
    demo.add_argument("--serve", action="store_true", help="Start the dashboard after loading demo mail.")
    demo.add_argument("--host", default=None)
    demo.add_argument("--port", type=int, default=None)
    demo.add_argument("--reset", action="store_true", default=True, help="Replace the local database (default).")

    sub.add_parser("auth", help="Sign in to Microsoft 365 with a device code.")

    sync = sub.add_parser("sync", help="Pull recent Outlook mail via Microsoft Graph and classify it.")
    sync.add_argument("--hours", type=int, default=None, help="Lookback window (defaults to CONTROLLER_INBOX_LOOKBACK_HOURS).")

    watch = sub.add_parser("watch", help="Poll Outlook and write a daily digest at the configured hour.")
    watch.add_argument("--once", action="store_true")

    digest = sub.add_parser("digest", help="Rebuild today's action-item digest from the local database.")
    digest.add_argument("--date", default=None, help="YYYY-MM-DD (defaults to today in the configured timezone).")
    digest.add_argument("--send", action="store_true", help="Email the digest via Graph if CONTROLLER_INBOX_DIGEST_TO is set.")

    serve = sub.add_parser("serve", help="Open the local CloseDesk dashboard.")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)

    ingest = sub.add_parser("ingest", help="Read .msg/.eml files and attachments dropped in the inbox folder.")
    ingest.add_argument("--serve", action="store_true", help="Open the dashboard after reading the folder.")

    export = sub.add_parser("export", help="Write open action items to CSV (stdout).")
    export.add_argument("--output", default=None)

    sub.add_parser("status", help="Show local database counts and connection state.")

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

    if args.cmd == "digest":
        now = datetime.now(settings.tz)
        as_of = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else local_today(settings.tz, now)
        payload = build_digest(store, as_of=as_of, generated_at=now)
        md_path, html_path = write_digest_files(payload, settings.digest_dir, as_of.isoformat())
        print(payload["markdown"])
        print(f"\nWrote {md_path}\nWrote {html_path}")
        if args.send:
            _send_digest(settings, payload, as_of.isoformat())
        return 0

    if args.cmd == "serve":
        return _serve(settings, store, host=args.host, port=args.port)

    if args.cmd == "ingest":
        from controller_inbox.folder_mail import ingest_folder

        records = ingest_folder(store, settings)
        print(f"Read {len(records)} file(s) from {settings.inbox_incoming}")
        if not records:
            print(f"Nothing new. Drop .msg or .eml files in {settings.inbox_incoming}")
            print(f"Put related attachments in {settings.inbox_attachments}/<message name>/")
        else:
            _print_run_summary(records, None)
        if args.serve:
            return _serve(settings, store, host=None, port=None)
        return 0

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
        print(f"Database: {settings.db_path}")
        print(f"Graph configured: {settings.graph_configured}")
        print(f"Last sync: {store.get_state('last_sync_at') or 'never'}")
        for key, value in counts.items():
            print(f"{key}: {value}")
        return 0

    return 1


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
        from controller_inbox.folder_mail import ingest_folder

        records.extend(ingest_folder(store, settings))
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
