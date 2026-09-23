# CloseDesk tools

Run these from the project root. Every tool prints one JSON object to stdout.

```bash
python -m controller_inbox tool queue_status
python -m controller_inbox tool prepare_queue --limit 20
python -m controller_inbox tool list_folder --folder important
python -m controller_inbox tool build_digest
python -m controller_inbox tool save_reading --json '<object>'
```

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

## Unattended

```bash
python -m controller_inbox overnight
```

Requires `CONTROLLER_INBOX_LLM=true` and LM Studio's local server (default `http://127.0.0.1:1234/v1`). With the model off, the command still files script drafts and writes `data/overnight/YYYY-MM-DD.md`.
