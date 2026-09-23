import json
import time
from email.message import EmailMessage
from pathlib import Path

from fastapi.testclient import TestClient

from controller_inbox.cli import main, watch_tick
from controller_inbox.folder_mail import ingest_folder
from controller_inbox.reading import apply_bionic_reading
from controller_inbox.web import create_app


def _eml(path: Path, *, subject: str, body: str, message_id: str | None = None, sender="Dana <dana@vendor.example>"):
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["Date"] = "Tue, 22 Sep 2026 09:00:00 -0400"
    if message_id:
        message["Message-ID"] = message_id
    message.set_content(body)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(message.as_bytes())


def test_same_message_dropped_twice_is_one_record_and_keeps_its_reading(store, settings):
    settings.ensure_data_dir()
    _eml(settings.inbox_incoming / "a.eml", subject="Q3 budget sign-off", body="Please sign off by Friday.", message_id="<abc@corp.example>")
    first = ingest_folder(store, settings)
    assert len(first) == 1
    email_id = first[0].id
    assert email_id.startswith("mail-")

    apply_bionic_reading(
        store,
        email_id,
        {"category": "internal_fyi", "folder": "important", "importance": "high", "summary": "Budget sign-off needed.", "actions": [], "why": "x"},
    )

    # Saved again from Outlook: different file name and bytes, same Message-ID.
    _eml(settings.inbox_incoming / "Q3 budget (1).eml", subject="Q3 budget sign-off", body="Please sign off by Friday.\n", message_id="<ABC@corp.example>")
    report: dict = {}
    again = ingest_folder(store, settings, report=report)
    assert again == []
    assert report["already_read"] == 1
    assert store.counts()["emails"] == 1
    kept = store.get_email(email_id)
    assert kept.model_status == "bionic"
    assert kept.summary == "Budget sign-off needed."
    assert not list(settings.inbox_incoming.glob("*.eml")), "the duplicate file is still cleared from incoming"


def test_unreadable_file_moves_to_failed_with_a_note(store, settings, monkeypatch):
    settings.ensure_data_dir()
    (settings.inbox_incoming / "broken.msg").write_bytes(b"this is not an Outlook file")
    _eml(settings.inbox_incoming / "ok.eml", subject="Fine", body="Hello")
    report: dict = {}
    records = ingest_folder(store, settings, report=report)
    assert [r.subject for r in records] == ["Fine"]
    assert report["failed"] and report["failed"][0]["file"] == "broken.msg"
    assert (settings.inbox_failed / "broken.msg").exists()
    note = (settings.inbox_failed / "broken.msg.why.txt").read_text(encoding="utf-8")
    assert "could not read" in note.lower()
    assert not (settings.inbox_incoming / "broken.msg").exists()
    assert ingest_folder(store, settings) == [], "not retried on the next run"


def test_msg_with_forwarded_email_attached(store, settings, monkeypatch):
    import extract_msg

    class Embedded:
        subject = "Original: vendor dispute"
        sender = "Vendor <v@vendor.example>"
        date = "Mon, 21 Sep 2026 10:00:00 -0400"
        body = "We dispute invoice INV-7788 for $1,250.00."

    class Att:
        def __init__(self, name, data):
            self.longFilename = name
            self.shortFilename = name
            self.data = data

    class FakeMessage:
        def __init__(self, path):
            self.subject = "FW: vendor dispute"
            self.sender = "Pat Lee <pat@corp.example>"
            self.body = "See the forwarded note below. Can you handle this?"
            self.htmlBody = None
            self.date = None
            self.messageId = "<fw-1@corp.example>"
            self.attachments = [Att("Original: vendor dispute.msg", Embedded()), Att("notes.txt", b"call them back")]

        def close(self):
            pass

    monkeypatch.setattr(extract_msg, "Message", FakeMessage)
    settings.ensure_data_dir()
    (settings.inbox_incoming / "FW vendor dispute.msg").write_bytes(b"fake")
    records = ingest_folder(store, settings)
    assert len(records) == 1
    names = {att.filename for att in records[0].attachments}
    assert names == {"Original: vendor dispute.txt", "notes.txt"}
    forwarded = next(att for att in records[0].attachments if att.filename.endswith(".txt") and "Original" in att.filename)
    assert "INV-7788" in forwarded.extracted_text
    unpacked = {path.name for path in (settings.inbox_extracted / records[0].id).iterdir()}
    assert unpacked == {"Original_ vendor dispute.txt", "notes.txt"}, "no characters Windows rejects"


def test_sample_loader_never_erases_real_mail(store, settings):
    settings.ensure_data_dir()
    _eml(settings.inbox_incoming / "real.eml", subject="Board pack", body="Attached is the board pack.")
    ingest_folder(store, settings)
    client = TestClient(create_app(settings, store))
    response = client.post("/demo/reload", follow_redirects=True)
    assert response.status_code == 200
    assert "would erase your own mail" in response.text
    assert [e.subject for e in store.list_emails()] == ["Board pack"]
    settings_page = client.get("/settings")
    assert "Load sample mailbox</button>" not in settings_page.text


def test_cli_demo_refuses_to_replace_real_mail(store, settings, monkeypatch, capsys):
    monkeypatch.setenv("CONTROLLER_INBOX_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("CONTROLLER_INBOX_INBOX_DIR", str(settings.inbox_dir))
    settings.ensure_data_dir()
    _eml(settings.inbox_incoming / "real.eml", subject="Board pack", body="Attached.")
    ingest_folder(store, settings)
    assert main(["demo"]) == 2
    assert "would erase" in capsys.readouterr().out
    assert store.counts()["emails"] == 1


def test_watch_without_outlook_reads_the_drop_folder_not_the_sample(store, settings):
    settings.ensure_data_dir()
    result = watch_tick(settings, store)
    assert result["records"] == []
    assert store.counts()["emails"] == 0, "the sample mailbox is never injected"
    _eml(settings.inbox_incoming / "one.eml", subject="Vendor call", body="Can we talk tomorrow?")
    result = watch_tick(settings, store)
    assert [r.subject for r in result["records"]] == ["Vendor call"]
    assert {e.source for e in store.list_emails()} == {"folder"}


def test_process_button_runs_in_the_background(store, settings):
    settings.ensure_data_dir()
    _eml(settings.inbox_incoming / "one.eml", subject="Invoice INV-9 due October 2, 2026", body="Invoice INV-9 for $300.00 is due October 2, 2026.")
    client = TestClient(create_app(settings, store))
    started = client.post("/process", follow_redirects=False)
    assert started.status_code == 303
    for _ in range(100):
        status = client.get("/process/status").json()
        if status["state"] != "running":
            break
        time.sleep(0.05)
    assert status["state"] == "done", status
    assert status["result"]["ingested"] == 1
    assert status["result"]["headline"]
    home = client.get("/")
    assert "Processed." in home.text
    assert "Invoice INV-9" in home.text
    assert store.get_digest(status["result"]["date"]) is not None


def test_done_redirect_stays_inside_the_dashboard(loaded, settings):
    client = TestClient(create_app(settings, loaded))
    action = loaded.get_email("demo-bec-wire").actions[0]
    response = client.post(f"/actions/{action.id}/complete?next=https://evil.example/", follow_redirects=False)
    assert response.headers["location"] == "/actions"


def test_run_command_end_to_end(settings, monkeypatch, capsys):
    monkeypatch.setenv("CONTROLLER_INBOX_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("CONTROLLER_INBOX_INBOX_DIR", str(settings.inbox_dir))
    settings.ensure_data_dir()
    _eml(
        settings.inbox_incoming / "wire.eml",
        subject="Updated banking instructions",
        body="Our bank details have changed. Please use the new account for all future payments.",
        message_id="<w@x>",
    )
    assert main(["run", "--no-serve"]) == 0
    out = capsys.readouterr().out
    assert "payment-change warning" in out
    assert "Digest:" in out

    assert main(["digest", "--history", "--json"]) == 0
    history = json.loads(capsys.readouterr().out)
    assert len(history) == 1 and history[0]["headline"]

    assert main(["status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["local_model"]["mode"] == "off"
    assert status["counts"]["emails"] == 1

    assert main(["tool", "focus"]) == 0
    focus = json.loads(capsys.readouterr().out)
    assert focus["do_not_process"][0]["subject"] == "Updated banking instructions"
    assert focus["focus"][0]["label"] == "Verify by phone"

    assert main(["llm-check"]) == 1
    assert "turned off" in capsys.readouterr().out
