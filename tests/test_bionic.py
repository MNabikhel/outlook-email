import json

from controller_inbox.models import DocumentType
from controller_inbox.overnight import run_overnight
from controller_inbox.reading import apply_bionic_reading
from controller_inbox.tools import list_folder, prepare_queue, queue_status, save_reading


def test_script_drafts_file_the_demo_mailbox(loaded):
    rows = {email.id: email for email in loaded.list_emails(limit=50)}
    assert rows["demo-newsletter"].folder == "informational"
    assert rows["demo-newsletter"].model_status == "script_draft"
    assert rows["demo-newsletter"].summary
    assert rows["demo-bec-wire"].folder == "important"
    assert "fraud_risk" in rows["demo-bec-wire"].flags
    assert rows["demo-chase-stmt"].folder == "reference"
    assert rows["demo-inv-10482"].folder == "important"
    assert rows["demo-po"].folder == "reference"
    counts = loaded.counts()
    assert counts["waiting_on_bionic"] == 14
    assert counts["important"] >= 1
    assert counts["informational"] >= 1
    assert counts["reference"] >= 1


def test_prepare_queue_is_extracted_text_not_raw_files(loaded):
    payload = prepare_queue(loaded, limit=5)
    assert payload["ok"] is True
    assert payload["count"] == 5
    packet = payload["packets"][0]
    assert "body" in packet and "extracted" in packet
    assert packet["script_draft"]["folder"] in {"important", "informational", "reference"}
    status = queue_status(loaded, _settings(loaded))
    assert status["waiting_on_bionic"] == 14


def test_fraud_reading_cannot_land_in_informational(loaded):
    result = save_reading(
        loaded,
        {
            "email_id": "demo-bec-wire",
            "category": "newsletter",
            "folder": "informational",
            "importance": "low",
            "summary": "Looks routine.",
            "actions": [{"title": "Pay the new account today", "due": None, "priority": "low"}],
            "why": "The wording is casual.",
        },
    )
    assert result["ok"] is True
    assert result["folder"] == "important"
    email = loaded.get_email("demo-bec-wire")
    assert email is not None
    assert email.category == DocumentType.PAYMENT_INSTRUCTION_CHANGE
    assert email.model_status == "bionic"
    assert "fraud_risk" in email.flags
    titles = " ".join(item.title.lower() for item in email.actions)
    assert "phone" in titles
    assert "new account" not in titles


def test_model_reading_replaces_the_draft(loaded):
    email = apply_bionic_reading(
        loaded,
        "demo-newsletter",
        {
            "category": "internal_fyi",
            "folder": "informational",
            "importance": "low",
            "summary": "Weekly accounting note. No action.",
            "actions": [],
            "why": "Internal roundup, not a vendor bill.",
        },
    )
    assert email.category == DocumentType.INTERNAL_FYI
    assert email.model_status == "bionic"
    assert email.summary.startswith("Weekly accounting")
    listed = list_folder(loaded, "informational")
    assert any(row["email_id"] == "demo-newsletter" for row in listed["emails"])


def test_overnight_without_model_still_files_and_logs(loaded, settings, as_of_now):
    result = run_overnight(loaded, settings, now=as_of_now, sync_graph=False)
    assert result["read_by_bionic"] == 0
    assert result["waiting_on_bionic"] == 14
    assert result["folders"]["important"] >= 1
    log = open(result["log_path"], encoding="utf-8").read()
    assert "Bionic was off" in log
    assert "Important" in log


class AgreeingReader:
    """Stands in for LocalReader: agrees with the script draft."""

    def __init__(self):
        from controller_inbox.local_llm import ReaderStats

        self.model = "test-model"
        self.stats = ReaderStats()
        self.subjects: list[str] = []

    @property
    def stopped(self):
        return bool(self.stats.stopped_reason)

    def read(self, packet):
        self.subjects.append(packet["subject"])
        self.stats.read += 1
        self.stats.seconds += 0.5
        return {
            "category": packet["script_draft"]["category"],
            "folder": packet["script_draft"]["folder"],
            "importance": packet["script_draft"]["importance"],
            "summary": "Bionic read: " + packet["subject"][:60],
            "actions": [],
            "why": "Agreed with the extracted facts.",
        }


def test_overnight_applies_a_local_reading(loaded, settings, as_of_now):
    reader = AgreeingReader()
    result = run_overnight(loaded, settings, now=as_of_now, limit=3, sync_graph=False, reader=reader)
    assert result["read_by_bionic"] == 3
    assert result["waiting_on_bionic"] == 11
    assert result["model"] == "test-model"
    assert result["avg_seconds"] == 0.5
    assert loaded.counts()["read_by_bionic"] == 3
    read = [loaded.get_email(email_id) for email_id in [e.id for e in loaded.list_emails(model_status="bionic")]]
    assert all(email.folder == "important" for email in read), "the model reads Important mail first"


def test_cli_tool_prints_json(loaded, settings, monkeypatch, capsys):
    monkeypatch.setenv("CONTROLLER_INBOX_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("CONTROLLER_INBOX_INBOX_DIR", str(settings.inbox_dir))
    monkeypatch.setenv("CONTROLLER_INBOX_LLM", "false")
    from controller_inbox.cli import main

    assert main(["tool", "list_folder", "--folder", "important"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["count"] >= 1
    assert main(["tool", "save_reading", "--json", "{not json"]) == 1


def _settings(store):
    from controller_inbox.config import Settings

    return Settings(data_dir=store.path.parent, _env_file=None)
