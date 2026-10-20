"""A first run reads each file once: the re-read for a new reader version (``reread_attachments``) comes before the
drop folder is read, so files just read with the current reader aren't read again."""

from __future__ import annotations

from pathlib import Path

from controller_inbox.overnight import run_overnight
from msgfactory import PDF, write_msg

FIXTURE = Path(__file__).parent / "fixtures" / "excel_schedules" / "AP Aging 9-30-26.pdf"


def test_a_first_run_reads_each_file_once(store, settings, monkeypatch):
    from controller_inbox import folder_mail

    reread = []
    real = folder_mail.attachment_text
    monkeypatch.setattr(folder_mail, "attachment_text", lambda name, ctype, data: reread.append(name) or real(name, ctype, data))
    settings.ensure_data_dir()
    write_msg(settings.inbox_incoming / "a.msg", "AP aging", "Attached.", sender_email="ap@vendor.com", attachments=[("AP Aging.pdf", FIXTURE.read_bytes(), PDF)])
    result = run_overnight(store, settings, sync_graph=False)
    assert result["ingested"] == 1
    assert reread == [], "read once, with the reader of this version, not again straight after"
    assert run_overnight(store, settings, sync_graph=False)["files_reread"] == 0
