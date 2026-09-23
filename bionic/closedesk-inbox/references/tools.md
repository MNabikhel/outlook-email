# CloseDesk tools

Run these from the project root. Every tool prints one JSON object to stdout.

```bash
python -m controller_inbox tool queue_status
python -m controller_inbox tool prepare_queue --limit 20
python -m controller_inbox tool list_folder --folder important
python -m controller_inbox tool build_digest
python -m controller_inbox tool save_reading --json '<object>'
python -m controller_inbox tool focus
python -m controller_inbox tool digest_history --limit 14
```

`prepare_queue` returns the most important waiting mail first. `focus` returns today's ranked list (what to do first, with one-line summaries) plus any do-not-process warnings — use it to answer "what do I need to do today?". `digest_history` lists past digests with their one-sentence headline.

`list_folder` accepts `important`, `informational`, or `reference`.

## save_reading

```json
{
  "email_id": "the id from the packet",
  "category": "ap_invoice",
  "folder": "important",
  "importance": "high",
  "summary": "One sentence.",
  "actions": [
    {"title": "Enter the invoice", "due": "2026-10-05", "priority": "high"}
  ],
  "why": "Why you chose this."
}
```

Categories you can use:

`ap_invoice`, `ar_invoice`, `credit_memo`, `purchase_order`, `packing_slip`, `remittance_advice`, `bank_statement`, `bank_reconciliation`, `wire_ach_request`, `payment_instruction_change`, `payroll`, `expense_report`, `tax_document`, `contract`, `insurance`, `audit_request`, `workpaper`, `spreadsheet`, `image_scan`, `newsletter`, `internal_fyi`, `mixed`, `other`.

Folders:

| Folder | File it here when |
| --- | --- |
| important | Someone must decide, pay, post, approve, or call |
| informational | Newsletter or FYI. No task. |
| reference | Statement, PO, contract, or file to keep. Not tonight's work. |

A payment-instruction change is always forced back to `important`, even if you send another folder.

CloseDesk also checks every reading. `save_reading` returns `guard_notes` when it changed something:

- a summary that quotes a dollar amount not in the packet is replaced by the script summary;
- an action `due` date that is not one of the packet's `due_dates` is replaced by the message's date when it has exactly one, and dropped otherwise (your date is kept in the action detail);
- a message with a task due within 7 days stays in `important`;
- on a payment-instruction change, any task that would update bank details or pay is removed, and a summary that does not warn is replaced by a verify-by-phone warning.

## Unattended

```bash
python -m controller_inbox overnight
```

Uses LM Studio's local server (default `http://127.0.0.1:1234/v1`) whenever it has a model loaded (`CONTROLLER_INBOX_LLM=auto`, the default). With no model answering, the command still files script drafts and writes `data/overnight/YYYY-MM-DD.md`. If the server stops answering mid-run, the run stops asking and says so in the log.
