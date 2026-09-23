# CloseDesk

A local focus digest for a busy Outlook inbox.

Export yesterday's mail from Outlook into a folder on your laptop (drag and drop — no add-in, no IT approval), double-click **CloseDesk**, and get one page: what you must act on, ranked; what came in and what it was about; and what can wait. Scripts pull the text, amounts, dates, and file types from every email and attachment. A small local model in LM Studio (Bionic) reads the short packets the scripts built and writes the one-line summaries, with guard rails that keep a small model honest. Every day's digest is kept so you can look back.

Start here: [docs/SETUP.md](docs/SETUP.md). How it works, in slides: [docs/CloseDesk-how-it-works.pptx](docs/CloseDesk-how-it-works.pptx). The longer notes are in [docs/BIONIC_GUIDE.md](docs/BIONIC_GUIDE.md).

## The everyday loop

1. Drag yesterday's mail from Outlook into `inbox/incoming/` (`.msg` from classic Outlook, `.eml` from new Outlook, the web, or Mac).
2. Double-click `CloseDesk.bat` (Windows) or `CloseDesk.command` (Mac/Linux). The first run creates `.venv` and installs everything.
3. The dashboard opens on **Today**:
   - **Do not process — verify by phone** when someone asks to change payment details;
   - **Your focus today** — a ranked list (fraud, overdue, due today, due soon, new decisions), one row per email, with **Done** buttons;
   - **What came in since yesterday** — *Needs you*, *Worth knowing*, *Filed for reference*, each with a one-line summary;
   - **Coming up** in the next 7 days.
4. **Past digests** keeps every day (also written to `data/digests/YYYY-MM-DD.{md,html,json}`).

The same thing from a terminal:

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: py -3 -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev]"
python -m controller_inbox run
```

The digest covers mail since the start of the previous working day (Friday, on a Monday). Open tasks and unverified payment-change warnings carry over until you mark them done.

## Laptop drop folder

1. Put Outlook messages in `inbox/incoming/` (`.msg` or `.eml`). Attachments stored inside the message are unpacked automatically, including forwarded emails attached as items.
2. If the files were saved separately, put them in `inbox/attachments/<same name as the message>/`.
3. A loose PDF or workbook dropped straight into `inbox/incoming/` is classified on its own.
4. Click **Process new mail** on the dashboard, or run `python -m controller_inbox run` (or `ingest` for the scripts only).

Originals move to `inbox/processed/`. Unpacked copies are written to `inbox/extracted/`. A file that cannot be read moves to `inbox/failed/` with a `.why.txt` note instead of being retried forever.

Mail is identified by its `Message-ID`, so the same email saved twice — or once as `.msg` and once as `.eml` — is one record. A message the model already read, or that you corrected, is never reset by dropping it again.

Supported attachments: PDF, Excel (`.xlsx`, `.xlsm`, `.xls`), Word (`.docx`), PowerPoint (`.pptx`), CSV, TSV, TXT, RTF, HTML, and ZIP. Images are marked as scans. If Tesseract and Pillow are installed on the laptop, image text is read too.

## Scripts extract. The local model decides.

- **Scripts** read every file and pull invoice numbers, amounts, due dates, and the payment-instruction check. They also draft a folder and a summary, so the board works before — or without — a model. A "please wire this to the new account" message stays critical even if a model would call it routine.
- **The local model** (LM Studio / Bionic, or Ollama) reads a short plain-text packet — never the raw PDF — and chooses the category, the folder, a one-line summary, and the action items. The most important mail is read first.
- **Learning** is the correction box on each message. Say what it should be and why. That sender is classified that way next time, the model never overwrites the message you fixed, and the example is appended to `data/training/corrections.jsonl`. A saved correction does not silence a new payment-instruction warning.

### Built for a small model on a laptop

No configuration is needed: `CONTROLLER_INBOX_LLM=auto` (the default) uses a model whenever LM Studio's server has one loaded, and files from the script draft when it does not. `python -m controller_inbox llm-check` shows the loaded model, times a sample reading, and estimates how long the waiting queue will take.

| Problem with small models | What CloseDesk does |
| --- | --- |
| Short context windows | Each packet is plain text under a hard budget (`CONTROLLER_INBOX_LLM_MAX_PROMPT_CHARS`, default 6000). |
| Chatty or fenced JSON | Structured output is requested when the server supports it; fenced, chatty, or trailing-comma replies are still parsed. |
| Invented numbers | A summary quoting a dollar amount that is not in the email is replaced by the script summary. |
| Invented dates | An action due date that is not in the email is dropped (kept in the task note). |
| Burying real work | A task due within a week keeps the email in Important. Fraud stays in Important with "verify by phone". |
| Slow or crashed server | The model is resolved once per run; if the server stops answering or times out twice, the run stops asking and the mail stays filed. |

Every correction a guard makes is shown on the message under **Why this was flagged**.

```env
# Optional overrides (see .env.example)
CONTROLLER_INBOX_LLM=auto                     # auto | true | false
CONTROLLER_INBOX_LLM_BASE_URL=http://127.0.0.1:1234/v1   # Ollama: http://127.0.0.1:11434/v1
CONTROLLER_INBOX_LLM_MODEL=local-model        # "whatever is loaded"; set an id to pin one
```

The Bionic Studio skill in `bionic/closedesk-inbox/` lets the agent do the reading in chat and answer "what do I need to do today?" with `tool focus`.

## Sample mailbox

```bash
python -m controller_inbox demo --serve
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). The sample is a September 2026 mailbox: Northwind invoice INV-10482, a Chase statement, ADP payroll, an IRS CP2000, an auditor PBC, a customer remittance, a close calendar, and a fraudulent wiring-instruction change. Once your own mail is in the database, `demo` and the **Load sample mailbox** button refuse to run so they cannot erase it; use `CONTROLLER_INBOX_DATA_DIR=./data-sample` to look at the sample separately.

| Command | What it does |
| --- | --- |
| `python -m controller_inbox run` | The everyday command: drop folder, local model, today's digest, open the dashboard |
| `python -m controller_inbox overnight` | Same, unattended, plus a log in `data/overnight/` (schedule `scripts/overnight.bat` / `.sh`) |
| `python -m controller_inbox llm-check` | Is the local model answering, and how fast |
| `python -m controller_inbox ingest` | Read the drop folder with the scripts only |
| `python -m controller_inbox serve` | Dashboard |
| `python -m controller_inbox digest` | Rebuild today's digest (`--date`, `--json`, `--history`) |
| `python -m controller_inbox demo` | Load the sample mailbox and print a digest |
| `python -m controller_inbox export` | CSV of action items |
| `python -m controller_inbox watch` | Keep running: drop folder (and Outlook if connected), digest each morning |
| `python -m controller_inbox status` | Counts, model status (`--json`) |
| `python -m controller_inbox tool …` | JSON tools for the Bionic agent: `queue_status`, `prepare_queue`, `save_reading`, `list_folder`, `build_digest`, `focus`, `digest_history` |

## What happens to each message

When a message arrives, CloseDesk:

1. Reads the body and attachments from the drop folder or from Microsoft Graph.
2. Extracts text from PDF, Excel, Word, PowerPoint, CSV, and HTML. Account and routing numbers are stored as last-4 only.
3. Drafts a category, a folder (important, informational, or reference), and a one-line summary from those facts. This step is fast and never waits on a model.
4. When a local model is answering, it reads the waiting messages — Important first — and replaces the draft: category, folder, importance, summary, and action items. The guard rails above apply to every reading.
5. Scores importance from due dates, dollar amount, sender, month-end proximity, and Outlook’s own importance flag when the model has not read the message yet.
6. Turns the message into action items you can complete in the dashboard or export to Excel.
7. Optionally writes Outlook categories and a follow-up flag back onto the message.
8. Builds the daily focus digest you can open locally, print, email to yourself, or leave for the morning from `overnight`.

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

The digest is the thing to read with coffee. It opens with one sentence — for example *"4 of 13 emails since Mon Sep 21 need you. 1 payment-change warning."* — and then:

- **Do not process — verify by phone** for payment-instruction changes (carried over until the verify task is done)
- **Your focus today**: up to seven items, ranked — fraud, overdue, due today, due in the next few days, new decisions — one row per email with its one-line summary
- **What came in** since the previous working day, grouped into *Needs you*, *Worth knowing*, and *Filed for reference*
- **Coming up** in the next 7 days
- **Month-end** lists when there is something in them: invoices to enter, cash to apply, close / bank-rec items

Each day is stored in SQLite (browse it under **Past digests**, or `digest --history`) and written under `data/digests/` as Markdown, HTML (standalone, printable), and JSON. `CONTROLLER_INBOX_DIGEST_LOOKBACK_DAYS` widens the window.

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
  pipeline.py    Ingest → classify → store (scripts only, fast)
  folder_mail.py Drop folder: .msg / .eml / loose files, Message-ID dedupe, inbox/failed
  reading.py     Script drafts, model packets, and the guard rails on a model reading
  local_llm.py   LM Studio / Ollama client built for small models
  overnight.py   One pass: drop folder → model → digest (run, overnight, watch, dashboard)
  learn.py       Corrections that teach the classifier
  tools.py       JSON tools for the Bionic agent
  digest.py      Daily focus digest
  web.py         Local dashboard
  cli.py         controller-inbox / closedesk commands
CloseDesk.bat / CloseDesk.command   Double-click launchers (first run sets up .venv)
scripts/overnight.bat / .sh         For Task Scheduler / cron
```

Data lives in `data/closedesk.db` (gitignored). Tokens live in `data/msal_token_cache.bin`.

## Tests

```bash
pytest
```

The suite classifies the demo mailbox end-to-end (including the fraud wire and duplicate invoice), drops real `.eml` files through the folder, exercises the small-model reader against a fake server (structured-output fallback, dead server, chatty replies, invented amounts and dates), checks the focus ranking and digest window, and hits the dashboard routes including background processing.

## Privacy notes

- Mail is processed on the machine that runs CloseDesk and stored in local SQLite.
- Routing / account / IBAN values are redacted in stored bodies; only last-4 is kept for matching.
- Outlook write-back is **off** until you set `CONTROLLER_INBOX_WRITEBACK=true`.
- There is no cloud AI in the default path. The model, when used, runs on the laptop. The fraud rules are deterministic so a $48,500 “new account” email cannot be quietly labeled “FYI”, whatever the model says.
