from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from controller_inbox.actions import local_today
from controller_inbox.config import Settings, load_settings
from controller_inbox.digest import build_digest, write_digest_files
from controller_inbox.models import DOCUMENT_LABELS, IMPORTANCE_LABELS
from controller_inbox.pipeline import ingest_demo, ingest_mailbox
from controller_inbox.profile import STATE_KEY as PROFILE_KEY, is_finance
from controller_inbox.store import Store

DEMO_NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="controller-inbox",
        description="CloseDesk: read exported Outlook mail, let a local model file it, and get a daily focus digest.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser(
        "run",
        help="The everyday command: read the drop folder, let the local model read, write today's digest, open the dashboard.",
    )
    run.add_argument("--no-serve", action="store_true", help="Process and print the digest, then exit.")
    run.add_argument("--no-browser", action="store_true", help="Do not open a browser window.")
    run.add_argument("--limit", type=int, default=None, help="How many messages the model reads this run.")
    run.add_argument("--host", default=None)
    run.add_argument("--port", type=int, default=None)

    demo = sub.add_parser("demo", help="Load a sample mailbox (no Outlook login).")
    demo.add_argument("--serve", action="store_true", help="Start the dashboard after loading demo mail.")
    demo.add_argument("--host", default=None)
    demo.add_argument("--port", type=int, default=None)
    demo.add_argument("--force", action="store_true", help="Replace the database even if it holds your own mail.")

    sub.add_parser("auth", help="Sign in to Microsoft 365 with a device code.")

    sync = sub.add_parser("sync", help="Pull recent Outlook mail via Microsoft Graph and classify it.")
    sync.add_argument("--hours", type=int, default=None, help="Lookback window (defaults to CONTROLLER_INBOX_LOOKBACK_HOURS).")

    watch = sub.add_parser(
        "watch", help="Keep running: check the drop folder (and Outlook, if connected) and write the digest each morning."
    )
    watch.add_argument("--once", action="store_true")

    digest = sub.add_parser("digest", help="Rebuild today's focus digest from the local database.")
    digest.add_argument("--date", default=None, help="YYYY-MM-DD (defaults to today in the configured timezone).")
    digest.add_argument("--send", action="store_true", help="Email the digest via Graph if CONTROLLER_INBOX_DIGEST_TO is set.")
    digest.add_argument("--history", action="store_true", help="List saved digests instead of building one.")
    digest.add_argument("--json", action="store_true", help="Print JSON instead of markdown.")

    serve = sub.add_parser("serve", help="Open the local CloseDesk dashboard.")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)

    ingest = sub.add_parser("ingest", help="Read .msg/.eml files and attachments dropped in the inbox folder.")
    ingest.add_argument("--serve", action="store_true", help="Open the dashboard after reading the folder.")

    export = sub.add_parser("export", help="Write open action items to CSV (stdout).")
    export.add_argument("--output", default=None)

    status = sub.add_parser("status", help="Show local database counts and connection state.")
    status.add_argument("--json", action="store_true")

    llm_check = sub.add_parser("llm-check", help="Check that the local model answers, and time one sample reading.")
    llm_check.add_argument("--no-sample", action="store_true", help="Only list models; skip the timed sample read.")

    overnight = sub.add_parser(
        "overnight",
        help="Unattended: read the drop folder, let Bionic file the queue, and write the morning digest.",
    )
    overnight.add_argument("--limit", type=int, default=None, help="How many drafts Bionic reads this run.")
    overnight.add_argument("--no-graph", action="store_true", help="Skip Outlook even if it is configured.")

    from controller_inbox.tools import TOOL_NAMES

    tool = sub.add_parser("tool", help="JSON tool for the local Bionic agent.")
    tool.add_argument("name", choices=TOOL_NAMES)
    tool.add_argument("--limit", type=int, default=20)
    tool.add_argument("--folder", default="")
    tool.add_argument("--json", default="", help="Reading JSON for save_reading. Reads stdin when omitted.")
    tool.add_argument("--date", default=None)

    args = parser.parse_args(argv)
    settings = load_settings()
    store = Store(settings.db_path)

    if args.cmd == "run":
        return _run(settings, store, args)

    if args.cmd == "demo":
        real = store.real_mail_count()
        if real and not args.force:
            print(
                f"Your database has {real} message(s) of your own mail. Loading the sample would erase them.\n"
                "Use a separate data folder for the sample, for example:\n"
                "  CONTROLLER_INBOX_DATA_DIR=./data-sample python -m controller_inbox demo --serve\n"
                "or pass --force to replace it anyway."
            )
            return 2
        records, payload = load_sample(store, settings)
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
        if args.history:
            rows = store.list_digests()
            if args.json:
                print(json.dumps(rows, indent=2, default=str))
                return 0
            if not rows:
                print("No digests saved yet. Run: python -m controller_inbox run")
            for row in rows:
                print(f"{row['period_date']}  {row['headline'] or ''}")
            print(f"\nFiles: {settings.digest_dir}")
            return 0
        now = datetime.now(settings.tz)
        as_of = datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else local_today(settings.tz, now)
        payload = make_digest(store, settings, as_of=as_of, now=now)
        md_path, html_path = write_digest_files(payload, settings.digest_dir, as_of.isoformat())
        if args.json:
            print(json.dumps({k: v for k, v in payload.items() if k not in {"markdown", "html"}}, indent=2, default=str))
        else:
            print(payload["markdown"])
            print(f"\nWrote {md_path}\nWrote {html_path}")
        if args.send:
            _send_digest(settings, payload, as_of.isoformat())
        return 0

    if args.cmd == "serve":
        return _serve(settings, store, host=args.host, port=args.port)

    if args.cmd == "ingest":
        from controller_inbox.folder_mail import ingest_folder

        report: dict = {}
        records = ingest_folder(store, settings, report=report)
        print(f"Read {len(records)} file(s) from {settings.inbox_incoming}")
        if report.get("already_read"):
            print(f"{report['already_read']} were already filed earlier and were left as they were.")
        for row in report.get("failed", []):
            print(f"Could not read {row['file']}: {row['error']} (moved to {settings.inbox_failed})")
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
        from controller_inbox.local_llm import check_model

        counts = store.counts()
        model = check_model(settings, use_cache=False)
        info = {
            "database": str(settings.db_path),
            "drop_folder": str(settings.inbox_incoming),
            "graph_configured": settings.graph_configured,
            "local_model": model.to_dict(),
            "last_sync": store.get_state("last_sync_at"),
            "last_run": store.get_state("last_overnight_at"),
            "counts": counts,
        }
        if args.json:
            print(json.dumps(info, indent=2, default=str))
            return 0
        print(f"Database: {info['database']}")
        print(f"Drop folder: {info['drop_folder']}")
        print(f"Outlook connected: {settings.graph_configured}")
        print(model.describe())
        print(f"Last run: {info['last_run'] or 'never'}")
        for key, value in counts.items():
            print(f"{key}: {value}")
        return 0

    if args.cmd == "llm-check":
        return _llm_check(settings, store, sample=not args.no_sample)

    if args.cmd == "overnight":
        from controller_inbox.overnight import run_overnight

        result = run_overnight(store, settings, limit=args.limit, sync_graph=not args.no_graph)
        _print_overnight(result)
        return 0

    if args.cmd == "tool":
        from controller_inbox.tools import dispatch

        if args.name == "save_reading" and not args.json and not sys.stdin.isatty():
            args.json = sys.stdin.read()
        result = dispatch(store, settings, args.name, args)
        json.dump(result, sys.stdout, ensure_ascii=False, default=str)
        sys.stdout.write("\n")
        return 0 if result.get("ok") else 1

    return 1


def make_digest(store: Store, settings: Settings, *, as_of, now: datetime) -> dict:
    return build_digest(
        store,
        as_of=as_of,
        generated_at=now,
        tz=settings.tz,
        lookback_days=settings.digest_lookback_days,
        finance=is_finance(settings, store),
    )


def load_sample(store: Store, settings: Settings):
    chosen = store.get_state(PROFILE_KEY)
    store.reset()
    if chosen:
        store.set_state(PROFILE_KEY, chosen)
    records = ingest_demo(store, settings, now=DEMO_NOW)
    as_of = local_today(settings.tz, DEMO_NOW)
    payload = make_digest(store, settings, as_of=as_of, now=DEMO_NOW.astimezone(settings.tz))
    write_digest_files(payload, settings.digest_dir, as_of.isoformat())
    return records, payload


def _run(settings: Settings, store: Store, args) -> int:
    from controller_inbox.overnight import run_overnight

    print(f"CloseDesk — reading {settings.inbox_incoming}")

    def progress(stage: str, done: int, total: int, note: str) -> None:
        label = {"importing": "Reading file", "reading": "Model reading", "digest": "Writing digest"}.get(stage, stage)
        suffix = f" {done}/{total}" if total > 1 else ""
        print(f"  {label}{suffix}: {note[:70]}", flush=True)

    result = run_overnight(store, settings, limit=args.limit, on_progress=progress)
    print()
    _print_overnight(result)
    if args.no_serve:
        return 0
    host = args.host or settings.host
    port = args.port or settings.port
    url = f"http://{host}:{port}/"
    print(f"\nOpening {url}  (leave this window open; press Ctrl+C to stop)")
    if not args.no_browser:
        _open_browser_soon(url)
    return _serve(settings, store, host=host, port=port)


def _print_overnight(result: dict) -> None:
    print(result["headline"])
    print(
        f"Read {result['ingested']} new file(s)"
        + (f", {result['already_read']} already filed" if result.get("already_read") else "")
        + ". The local model"
        + (f" ({result['model']})" if result.get("model") else "")
        + f" read {result['read_by_bionic']}"
        + (f" (about {result['avg_seconds']}s each)" if result.get("avg_seconds") else "")
        + "."
    )
    print(
        "Folders — important {important}, informational {informational}, reference {reference}.".format(
            **result["folders"]
        )
    )
    for row in result.get("failed_files", []):
        print(f"Could not read {row['file']}: {row['error']} (moved to inbox/failed)")
    if result.get("model_note"):
        print(result["model_note"])
    if result["waiting_on_bionic"]:
        print(f"Still waiting on the model: {result['waiting_on_bionic']} (they are filed from the script draft).")
    print(f"Digest: {result['digest_path']}")
    print(f"Log: {result['log_path']}")


def _open_browser_soon(url: str) -> None:
    import threading
    import webbrowser

    threading.Timer(1.5, lambda: webbrowser.open(url)).start()


def _llm_check(settings: Settings, store: Store, *, sample: bool) -> int:
    from controller_inbox.local_llm import LocalReader, check_model

    status = check_model(settings, timeout=4, use_cache=False)
    print(status.describe())
    if status.models:
        print("Loaded models: " + ", ".join(status.models))
    if not status.active:
        print(
            "\nTo fix: open LM Studio, load a model, and start the local server (Developer tab → Start server).\n"
            f"CloseDesk looks at {settings.llm_base_url}. Change it with CONTROLLER_INBOX_LLM_BASE_URL."
        )
        return 1
    if not sample:
        return 0
    packet = {
        "email_id": "sample",
        "subject": "Invoice INV-2201 from Harbor Packaging — due October 1",
        "sender_name": "Harbor AP",
        "sender_email": "ap@harbor.example",
        "received_at": datetime.now(timezone.utc).isoformat(),
        "body": "Hello, please find invoice INV-2201 for $2,200.00 attached. Payment is due October 1, 2026. Thanks!",
        "attachments": [],
        "filenames": ["INV-2201.pdf"],
        "extracted": {"invoice_numbers": ["INV-2201"], "amounts": [2200.0], "due_dates": ["2026-10-01"]},
        "script_draft": {"category": "ap_invoice", "folder": "important", "importance": "medium", "flags": []},
    }
    reader = LocalReader(settings, model=status.model)
    started = time.monotonic()
    parsed = reader.read(packet)
    elapsed = time.monotonic() - started
    if not parsed:
        print(f"The model answered, but not with usable JSON ({reader.stats.stopped_reason or 'unparseable reply'}).")
        print("Try a stronger instruction-following model, e.g. a 7B/8B instruct model.")
        return 1
    print(f"Sample reading in {elapsed:.1f}s:")
    print(json.dumps(parsed, indent=2, ensure_ascii=False))
    waiting = store.counts()["waiting_on_bionic"]
    if waiting:
        print(f"\n{waiting} message(s) are waiting. At this speed that is about {elapsed * waiting / 60:.0f} minute(s).")
    return 0


def _print_run_summary(records, payload) -> None:
    print(f"Processed {len(records)} email(s).")
    for rec in sorted(records, key=lambda r: r.importance_score, reverse=True)[:8]:
        flags = f" [{', '.join(rec.flags)}]" if rec.flags else ""
        folder = rec.folder or "unfiled"
        print(
            f"  {folder:<14} {IMPORTANCE_LABELS[rec.importance]:<8} {DOCUMENT_LABELS[rec.category]:<28} "
            f"{rec.subject[:60]}{flags}"
        )
    if payload:
        print(f"\nDigest {payload['date']}: {payload['headline']}")


def _graph_mailbox(settings: Settings):
    if not settings.graph_configured:
        raise SystemExit(
            "Azure is not configured. Set AZURE_CLIENT_ID (and optionally AZURE_TENANT_ID) "
            "in .env — see README — or use the drop folder: python -m controller_inbox run"
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
    uvicorn.run(app, host=host or settings.host, port=port or settings.port, log_level="warning")
    return 0


def watch_tick(settings: Settings, store: Store, *, force_digest: bool = False, now: datetime | None = None) -> dict:
    """One watch cycle. Never touches the sample mailbox."""
    from controller_inbox.folder_mail import ingest_folder
    from controller_inbox.overnight import read_queue

    records = []
    if settings.graph_configured:
        last = store.get_state("last_sync_at")
        after = datetime.fromisoformat(last) if last else datetime.now(timezone.utc) - timedelta(hours=settings.lookback_hours)
        records = ingest_mailbox(_graph_mailbox(settings), store, settings, received_after=after)
    records.extend(ingest_folder(store, settings))
    reading = read_queue(store, settings) if records or store.counts()["waiting_on_bionic"] else {"read_ids": []}
    now = now or datetime.now(settings.tz)
    as_of = local_today(settings.tz, now)
    wrote = None
    if now.hour >= settings.digest_hour and (force_digest or not store.get_digest(as_of.isoformat())):
        payload = make_digest(store, settings, as_of=as_of, now=now)
        write_digest_files(payload, settings.digest_dir, as_of.isoformat())
        wrote = payload
    return {"records": records, "read": len(reading["read_ids"]), "digest": wrote}


def _watch(settings: Settings, store: Store, *, once: bool) -> int:
    source = "Outlook and the drop folder" if settings.graph_configured else f"the drop folder {settings.inbox_incoming}"
    print(f"Watching {source}. Digest at {settings.digest_hour}:00 each day. Ctrl+C to stop.")

    def tick() -> None:
        result = watch_tick(settings, store, force_digest=once)
        stamp = datetime.now().isoformat(timespec="seconds")
        print(f"{stamp} read {len(result['records'])} message(s); model read {result['read']}")
        payload = result["digest"]
        if payload:
            print(f"Digest {payload['date']} written: {payload['headline']}")
            if settings.digest_to and settings.graph_configured:
                _send_digest(settings, payload, payload["date"])

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
    subject = f"CloseDesk daily digest — {period}: {payload['headline']}"
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
