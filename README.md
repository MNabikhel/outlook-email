# CloseDesk

A local inbox assistant for Outlook mail.

Drop `.msg` or `.eml` files into a folder on your laptop, or connect Microsoft 365. CloseDesk reads the message and the attachments (Excel, PDF, Word, PowerPoint, CSV, and ZIP), decides what each file is, and builds a daily list of what you need to do.

## Laptop drop folder

This is the path that does not need an Azure app.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
python -m controller_inbox serve
```

Then:

1. Put Outlook messages in `inbox/incoming/` (`.msg` or `.eml`).
2. Attachments stored inside the message are unpacked automatically.
3. If the files were saved separately, put them in `inbox/attachments/<same name as the message>/`.
4. A loose PDF or workbook dropped straight into `inbox/incoming/` is classified on its own.
5. On the Setup page, click **Read the drop folder**, or run `python -m controller_inbox ingest`.

Originals move to `inbox/processed/`. Unpacked copies are written to `inbox/extracted/` so you can open them.

Supported attachments: PDF, Excel (`.xlsx`, `.xlsm`, `.xls`), Word (`.docx`), PowerPoint (`.pptx`), CSV, TSV, TXT, RTF, HTML, and ZIP. Images are marked as scans. If Tesseract and Pillow are installed on the laptop, image text is read too.

## Rules and a local model, together

Use both, with a clear split:

- **Scripting** reads the file and pulls invoice numbers, amounts, due dates, and the payment-instruction / fraud check. That part should not depend on a model. A “please wire this to the new account” message stays critical even if a model would call it routine.
- **A local model** (optional) suggests a category only when the rules are unsure. Point it at [Ollama](https://ollama.com) or any local agent that speaks the OpenAI chat API, including a Bionic-style agent:

```env
CONTROLLER_INBOX_LLM=true
CONTROLLER_INBOX_LLM_BASE_URL=http://127.0.0.1:11434/v1
CONTROLLER_INBOX_LLM_MODEL=llama3.2
```

- **Learning** is the correction box on each message. If a category is wrong, say what it should be and why. That sender is classified that way next time, and the example is appended to `data/training/corrections.jsonl` so you can fine-tune the local model later. A saved correction does not silence a new payment-instruction warning.

## Sample mailbox

```bash
python -m controller_inbox demo --serve
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). The sample is a September 2026 mailbox: Northwind invoice INV-10482, a Chase statement, ADP payroll, an IRS CP2000, an auditor PBC, a customer remittance, a close calendar, and a fraudulent wiring-instruction change.

| Command | What it does |
| --- | --- |
| `python -m controller_inbox ingest` | Read the drop folder |
| `python -m controller_inbox serve` | Dashboard |
| `python -m controller_inbox demo` | Load the sample mailbox and print a digest |
| `python -m controller_inbox digest` | Rebuild today’s action list |
| `python -m controller_inbox export` | CSV of action items |
| `python -m controller_inbox watch` | Poll Outlook and the drop folder; write the morning digest |
| `python -m controller_inbox status` | Local counts |

## What happens to each message

When a message arrives, CloseDesk:

1. Reads the body and attachments from the drop folder or from Microsoft Graph.
2. Extracts text from PDF, Excel, Word, PowerPoint, CSV, and HTML. Account and routing numbers are stored as last-4 only.
3. Classifies each attachment and the message. Scripted rules run first. A local model can break a tie when you turn it on.
4. Scores importance from due dates, dollar amount, sender, month-end proximity, and Outlook’s own importance flag.
5. Turns the message into action items you can complete in the dashboard or export to Excel.
6. Optionally writes Outlook categories and a follow-up flag back onto the message.
7. Builds a daily digest you can open locally, email to yourself, or run from `watch` every morning.

## What it catches

These are the extras that matter when the inbox is invoices, banking, payroll, and close — not a generic mail client:

- **Payment-instruction / BEC trap.** “Our bank details have changed, please wire today” is treated as critical. The action is *verify by phone*, not *process the payment*.
- **Duplicate invoice detection** on invoice number (the classic double-entry from a resent PDF).
- **Missing attachment** when the body says “please see attached” and nothing arrived.
- **Month-end countdown** and extra weight on recs, payroll, and workpapers in the last week of the month.
- **Cash application** vs **AP invoice** vs **outgoing wire**, so remittances and bills are not one pile.
- **CSV export** of the action list for the spreadsheet workflow you already have.
- **Local-first.** SQLite on disk. The sample mailbox needs no Azure tenant.

## Connect your real Outlook

CloseDesk talks to Outlook through **Microsoft Graph**. You need an Entra ID app registration; CloseDesk never asks for your password.

1. In [Microsoft Entra admin center](https://entra.microsoft.com) go to **Identity → Applications → App registrations → New registration**.
2. Name it `CloseDesk`. For a single company mailbox choose the single-tenant option; for a mix of work and personal accounts choose “Accounts in any organizational directory and personal Microsoft accounts”.
3. Under **Authentication**:
   - Add a platform → **Mobile and desktop applications**
   - Use the redirect `https://login.microsoftonline.com/common/oauth2/nativeclient`
   - Enable **Allow public client flows**
4. Under **API permissions** add Microsoft Graph *delegated* permissions:
   - `User.Read`
   - `Mail.Read` (required)
   - `Mail.ReadWrite` (optional — flag and categorize mail in Outlook)
   - `Mail.Send` (optional — email yourself the daily digest)
   - Grant admin consent if your tenant requires it
5. Copy the **Application (client) ID**. Copy `.env.example` to `.env` and set:

```env
AZURE_CLIENT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
AZURE_TENANT_ID=common
CONTROLLER_INBOX_TIMEZONE=America/New_York
CONTROLLER_INBOX_WRITEBACK=false
# CONTROLLER_INBOX_DIGEST_TO=you@yourco.com
```

6. Sign in and pull mail:

```bash
python -m controller_inbox auth
python -m controller_inbox sync --hours 72
python -m controller_inbox serve
```

Leave it running in the background:

```bash
python -m controller_inbox watch
```

That command polls Graph on `CONTROLLER_INBOX_POLL_SECONDS` (default 2 minutes) and writes the daily digest at `CONTROLLER_INBOX_DIGEST_HOUR` (default 7:00 local). A cron equivalent if you prefer not to keep a process up:

```cron
*/15 * * * * cd /path/to/outlook-email && .venv/bin/python -m controller_inbox sync
0 7 * * *   cd /path/to/outlook-email && .venv/bin/python -m controller_inbox digest --send
```

### Daemon / shared mailbox (optional)

If IT would rather do app-only access against a shared mailbox, create a *confidential* client (client secret), grant **application** Graph permissions `Mail.Read` (and `Mail.ReadWrite` / `Mail.Send` if you want write-back or digest mail), then set:

```env
AZURE_CLIENT_ID=...
AZURE_TENANT_ID=your-tenant-id
AZURE_CLIENT_SECRET=...
CONTROLLER_INBOX_MAILBOX=controller@yourco.com
```

## Daily digest

The digest is the thing to read with coffee:

- Do-not-process / payment-instruction alerts
- Overdue actions
- Due today and due this week
- Invoices to enter (number, amount, due date)
- Cash to apply
- Close / bank-rec items
- Category mix of what landed

It is stored in SQLite and written under `data/digests/` as Markdown, HTML, and JSON.

## Categories it knows

AP invoice, credit memo, purchase order, packing slip, remittance / cash app, bank statement, bank rec, wire/ACH request, **payment-instruction change**, payroll, expense report, tax document, contract, insurance, audit / PBC, close workpaper, spreadsheet, scanned image, newsletter, mixed, other.

Importance is a 0–100 score, not a single keyword: a $400 newsletter stays low; a $12,850 invoice due in two days does not.

## Project layout

```
src/controller_inbox/
  graph.py       Microsoft Graph client (device code or client secret)
  demo.py        Sample mailbox
  extract.py     Attachment text + invoice/amount/due-date parsing
  classify.py    Finance document rules + importance
  actions.py     Action-item extraction
  pipeline.py    Ingest → classify → store
  digest.py      Daily brief
  web.py         Local dashboard
  cli.py         controller-inbox / closedesk commands
```

Data lives in `data/closedesk.db` (gitignored). Tokens live in `data/msal_token_cache.bin`.

## Tests

```bash
pytest
```

The suite classifies the demo mailbox end-to-end (including the fraud wire and duplicate invoice) and hits the dashboard routes.

## Privacy notes

- Mail is processed on the machine that runs CloseDesk and stored in local SQLite.
- Routing / account / IBAN values are redacted in stored bodies; only last-4 is kept for matching.
- Outlook write-back is **off** until you set `CONTROLLER_INBOX_WRITEBACK=true`.
- There is no vendor cloud AI in the default path. Classification is deterministic so a $48,500 “new account” email cannot be quietly labeled “FYI”.
