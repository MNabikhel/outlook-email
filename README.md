# CloseDesk

Your Outlook inbox, sorted into one short page every morning, on your own laptop.

Drag emails out of Outlook into a folder and double-click **CloseDesk**. You get a ranked list of what needs you, a one-line summary of everything else, and a chat box for questions about your mail. It works for any inbox; the finance profile adds month-end and close tracking. A local AI model is optional. Without one, everything still works.

## Quick start

1. **Install Python 3.11+** from [python.org](https://www.python.org/downloads/). On Windows, tick **Add python.exe to PATH**.
2. **Get CloseDesk:** green **Code** button → **Download ZIP**, then unzip it somewhere permanent (for example `Documents`). The folder is called `outlook-email-main`; rename it `CloseDesk` if you like.
3. **Double-click `CloseDesk.bat`** (Windows) or **`CloseDesk.command`** (Mac). The first run sets itself up in a minute or two and opens the dashboard. Click **Load sample mailbox** to look around.
4. **Add your mail:** drag emails from Outlook into the `inbox/incoming` folder and click **Process new mail**. ([New Outlook, web, and Mac](docs/SETUP.md#every-morning))
5. **Optional local AI:** in [LM Studio](https://lmstudio.ai/), load a small instruct model (3B–8B) and start the server (Developer tab → **Start server**). CloseDesk finds it on its own.

**Using it:**
- **Open an email:** click it, and it opens right there. **Open in Outlook** opens the original.
- **Ask a question:** use **Ask CloseDesk** (bottom-right), for example *what's urgent today?* On an email, ask about it and its attachments: *summarize the draft*, *is the Q4 total right?*
- **Read an attachment:** on the email page, **Read text** shows every page, sheet, or slide; **Summarize** and **Ask…** send it to the chat.
- **Fraud check:** a flagged email says why, with points per signal. Answer **Not fraud**, **Trust** the sender or their domain, or **This is fraud**; it learns from that, and everything is logged on the **Fraud check** page.
- **Search:** press `/` to search everything.
- **Draft a reply:** open an email and click **Draft a reply**.
- **Choose your inbox type:** **Setup** → *General* or *Finance*.

The full guide and troubleshooting are in [docs/SETUP.md](docs/SETUP.md). Prefer slides? The [quick setup deck](docs/CloseDesk-quick-setup.pptx) walks through the same steps with screenshots.

<details>
<summary>No model, or a small one? What changes</summary>

| | No model | With a local model |
| --- | --- | --- |
| Sorting into Needs you / Worth knowing / Reference | Yes (rules) | Yes (the model decides, with guard rails) |
| One-line summaries | The email's key sentence | Written by the model |
| Tasks, due dates, fraud warnings, digest | Yes | Yes |
| **Ask CloseDesk** | Finds the emails, today's focus list, and the matching passages in their files | Reads the email and its files (and other emails if needed), notes what it finds, checks its answer, and cites the emails |
| **Draft a reply** | A starter template | A written draft; payment-change emails get safety advice instead |

</details>

More detail: [how it works, in slides](docs/CloseDesk-how-it-works.pptx) and the [Bionic / LM Studio guide](docs/BIONIC_GUIDE.md).

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

Supported attachments: PDF, Excel (`.xlsx`, `.xlsm`, `.xls`), Word (`.docx`), PowerPoint (`.pptx`), CSV, TSV, TXT, RTF, HTML, and ZIP. Scanned PDF pages and pictures are read with OCR on the laptop: the launchers install the RapidOCR add-on (`pip install -e ".[ocr]"`, Python 3.12 or older), and Tesseract is used instead if that's what is installed. A scanned page says so in the text, so figures from it can be checked against the file. **Setup** shows which OCR is in use.

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
| Invented dates | An action due date that is not in the email is replaced by the email's own date, or dropped if it has none (kept in the task note). |
| Burying real work | A task due within a week keeps the email in Important. |
| Treating a bank change as routine | Fraud stays in Important as critical; "update the vendor's bank account" tasks are removed and the summary becomes a verify-by-phone warning. |
| Slow or crashed server | The model is resolved once per run; if the server stops answering or times out twice, the run stops asking and the mail stays filed. |

Every correction a guard makes is shown on the message under **Why this was flagged**.

### Attachments and Ask CloseDesk

Attachments become text the model can find its way around: PDF pages (with a note when a page is a scan), every workbook sheet with cell references and formulas (`C4: 1,800 (=SUM(C2:C3))`), Word documents in reading order with tracked changes and comments, slides with speaker notes, and CSV tables. Emails attached to a `.msg` are opened too, with their own files.

PDFs are read from where each character sits on the page, not as a text stream, so letter-spaced or kerned text comes out as words (`Jonathan Alvarez`, not `J o n a t h a n`) and a spreadsheet saved as PDF keeps its columns: a blank cell no longer shifts the values after it into the wrong column. Every table (PDF, Word, PowerPoint, workbook, CSV) with a header row is written so each row names its columns and says which are empty, `Employee: Jonathan Alvarez | Department: not listed | Manager: Priya Raman` (in a sheet, `C5 (Department): Finance`), so a question about one person or account needs only that line. A wide sheet printed across two pages has its second-page columns tied back to the right row; a cell wrapped onto two lines stays in its row; merged Word cells are handled. Files already in the inbox are read again this way once, on the next **Process new mail** or overnight run (scanned PDFs keep their OCR text).

In the chat, a reference such as [1] opens the file it is about: the file named in the same sentence, or the email's only file when the sentence cites a page, sheet or cell. A PDF opens in a new tab at the cited page; other files open in the text view at the cited page, slide or sheet, with the cited cell's row marked. File names in an answer are links too.

When you ask about an email, the chat starts from that email, its file list, and the sections that match your question, sized to the model's context window. If the server supports tools (LM Studio does), the model can also search other mail, open another email, read a page or sheet, read exact cells, trace a total back to its inputs, rank how every row changed between two columns (Q3 actual → Q4 budget), work out sums, percentages and deadlines exactly (small models get these wrong in their heads), and write down what it found. It then checks its answer against those notes before you see it. The notes stay on the email page (**Notes from Ask CloseDesk**) and come back when a later question is on the same subject.

CloseDesk needs a context window of at least 16,384 tokens, enough for most attachments to be read whole (about 16 PDF or Word pages, or 1,400 workbook cells). If LM Studio loaded the model with less, the first question reloads it with 16,384, or goes back to the old size if memory runs short. The **Setup** slider changes this minimum, from Off to 131,072, and shows as you drag how many pages and cells each size reads at once and how much memory it takes (`CONTROLLER_INBOX_MIN_CONTEXT_TOKENS` sets the starting value). With other servers, or if that fails, the chat says which files it only partly read and how to raise it (LM Studio → My Models → Context Length). Asked to summarize a file that doesn't fit, it reads the opening plus the lines that differ from page to page (findings, totals, names), so a 24-page report still gets its page-17 finding.

Every written answer is checked against what was read before it's final. A worked-out figure (a change, total or percentage) is recomputed from the numbers beside it and corrected if the model slipped; a page or cell citation that points to the wrong place is corrected when one other page or cell clearly holds it; a figure cited to the wrong file is pointed out; and a figure that isn't in any email or file read is flagged under the answer. Corrections are listed under the answer, so nothing changes silently.

The overnight run also summarizes the longest attachments (`CONTROLLER_INBOX_OVERNIGHT_FILE_SUMMARIES`, default 20 a night, most important mail first), with every figure and page checked against the file and unsupported lines dropped. "Summarize this file" is then answered at once, and a file too long to read whole gets the summary as its map. A summary is only used while the file's text is unchanged.

Search by meaning: load an embedding model in LM Studio next to the chat model (LM Studio ships **nomic-embed-text**) and the overnight run indexes every email and every section of its files on the laptop. Ask CloseDesk and its search tool then also find mail that says the same thing in other words ("the team trip in Portugal" finds the memo about the Lisbon offsite). A match in meaning has to stand out from the rest of the mail to count; an email with every word of the question still comes first. Without an embedding model, search is keyword-only as before. `CONTROLLER_INBOX_EMBEDDING_MODEL` picks one (or `off`), and `CONTROLLER_INBOX_EMBEDDING_BASE_URL` points to another server.

Files on an email flagged as possible payment fraud are never given to the model and don't download. You can still read their text on the page.

### Fraud check

Each email gets a score. A bank-detail change or gift-card request **in the sender's own words** blocks it (not the quoted thread, not a "we will never change our bank details" footer, not the model's opinion alone). Weaker signals — a reply-to on another domain, a lookalike of a known domain, a borrowed display name, pressure, a first email from an address — add up to a *double-check before paying* note that doesn't block anything. Trusted domains and senders count against the score, but a bank-change request always gets at least a caution. Replies are filed by what the sender wrote, so a colleague's "it wasn't them, ignore it" above a quoted scam is neither flagged nor filed as an invoice.

Tell it when it is wrong: **Not fraud**, **Trust sender**, **Trust everyone at @domain**, **This is fraud**, **Report this sender**. Each answer is logged and changes how much each signal counts. The **Fraud check** page lists trusted and reported domains, what it has learned, the flagged mail, and the full log (CSV export). From a terminal: `python -m controller_inbox trust taz.com` and `python -m controller_inbox fraud-log`.

```env
# Optional overrides (see .env.example)
CONTROLLER_INBOX_LLM=auto                     # auto | true | false
CONTROLLER_INBOX_LLM_BASE_URL=http://127.0.0.1:1234/v1   # Ollama: http://127.0.0.1:11434/v1
CONTROLLER_INBOX_LLM_MODEL=local-model        # "whatever is loaded"; set an id to pin one
```

The Bionic Studio skill in `bionic/closedesk-inbox/` lets the agent do the reading in chat and answer "what do I need to do today?" with `tool focus`.

### AP cost coding

CloseDesk makes `AP cost codes/AP cost codes.xlsx` next to the `inbox` folder (`CONTROLLER_INBOX_COST_CODES_DIR` moves it). Fill in two columns, **Description** and **Cost Code** (JDE codes with their periods, such as `1100.6110.100`); more rows, sheets or workbooks in that folder are all read. Every AP invoice is checked against it:

* a code from the workbook written on the invoice or in the email, even where a scan read a period as a comma, an O for a zero, or dropped the business unit's leading zeros;
* otherwise the code you last confirmed for that sender;
* otherwise a description whose words are on the invoice.

The email page shows the code with **Confirm**, or **Revise** to pick other codes (several for a split invoice). A code-shaped number on the invoice that isn't in the workbook is pointed out. The **AP coding** page lists invoices waiting for review, and *Search all mail* finds an invoice by its code or description.

## Sample mailbox

```bash
python -m controller_inbox demo --serve
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). The sample is a September 2026 mailbox: Northwind invoice INV-10482, a Chase statement, ADP payroll, an IRS CP2000, an auditor PBC, a customer remittance, a close calendar, and a fraudulent wiring-instruction change. There is everyday mail too: a colleague's question, an approval request, a meeting invite, an IT notice, and an FYI. Once your own mail is in the database, `demo` and the **Load sample mailbox** button refuse to run so they cannot erase it; use `CONTROLLER_INBOX_DATA_DIR=./data-sample` to look at the sample separately.

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

For any inbox:

- **Replies, approvals, and meetings.** "Could you send me…?" lands in *Needs you* with a reply task. An approval request gets an "Approve or decline" task. A meeting invite gets "Accept or decline".
- **Noise stays out of the way.** Newsletters, automated notices, and FYIs go to *Worth knowing* or *Reference*.

For invoices, banking, payroll, and close (the finance profile adds the month-end view; the fraud and invoice checks run in every profile):

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
# Time zone follows this computer. Set an IANA name to pin one, or change it in Setup.
# CONTROLLER_INBOX_TIMEZONE=auto
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
- **Invoices & payments** when there are any: invoices to enter and cash to apply (the finance profile adds a month-end countdown and close / bank-rec items)

Each day is stored in SQLite (browse it under **Past digests**, or `digest --history`) and written under `data/digests/` as Markdown, HTML (standalone, printable), and JSON. `CONTROLLER_INBOX_DIGEST_LOOKBACK_DAYS` widens the window.

## Categories it knows

Everyday: reply needed, approval request, meeting / calendar, FYI, newsletter, automated notification, other.

Finance: AP invoice, credit memo, purchase order, packing slip, remittance / cash app, bank statement, bank rec, wire/ACH request, **payment-instruction change**, payroll, expense report, tax document, contract, insurance, audit / PBC, close workpaper, spreadsheet, scanned image, mixed.

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
  documents.py   Attachments to navigable text (pages, sheets, slides), search, exact cells, formula tracing
  pdf_layout.py  PDF characters by position back into words, lines and table rows
  tables.py      Table rows as text: header-named cells, empty cells written out
  fraud.py       Fraud score, trusted domains and senders, verdicts that teach, the fraud log
  agent.py       Ask CloseDesk's read-only tools, notepad, and context budget
  answer_check.py Checks a finished answer's figures and citations against what was read
  file_summaries.py Overnight summaries of long attachments, checked against the file
  semantic.py    Search by meaning with a local embedding model
  ocr.py         Scanned pages and pictures to text (RapidOCR, else Tesseract)
  local_llm.py   LM Studio / Ollama client built for small models
  overnight.py   One pass: drop folder → model → digest (run, overnight, watch, dashboard)
  learn.py       Corrections that teach the classifier
  tools.py       JSON tools for the Bionic agent
  digest.py      Daily focus digest
  assistant.py   Ask CloseDesk chat and reply drafts (local model, or lookups without one)
  profile.py     General / finance inbox profile
  web.py         Local dashboard
  static/app.js  Open-in-place preview, chat box, search shortcut
  cli.py         controller-inbox / closedesk commands
CloseDesk.bat / CloseDesk.command   Double-click launchers (first run sets up .venv)
scripts/overnight.bat / .sh         For Task Scheduler / cron
```

Data lives in `data/closedesk.db` (gitignored). Tokens live in `data/msal_token_cache.bin`.

## Tests

```bash
pytest
```

The suite classifies the demo mailbox end-to-end (including the fraud wire and duplicate invoice), drops real `.eml` files through the folder, exercises the small-model reader against a fake server (structured-output fallback, dead server, chatty replies, invented amounts and dates), checks the focus ranking and digest window, and hits the dashboard routes including background processing. Real `.msg` files (built in `tests/msgfactory.py`) cover Exchange senders, nested forwarded emails, workbooks with formulas, and the fraud lock on their files; the chat's tool loop, verify pass, and context-overflow retry run against a fake model server.

## Privacy notes

- Mail is processed on the machine that runs CloseDesk and stored in local SQLite.
- Routing / account / IBAN values are redacted in stored bodies; only last-4 is kept for matching.
- Outlook write-back is **off** until you set `CONTROLLER_INBOX_WRITEBACK=true`.
- There is no cloud AI in the default path. The model, when used, runs on the laptop, and so do the chat box and reply drafts. The chat's history stays in the browser tab and is cleared when the tab closes. The fraud rules are deterministic so a $48,500 “new account” email cannot be quietly labeled “FYI”, whatever the model says. The chat's tools only read; attachment text is given to the model as data, never as instructions, and files from fraud-flagged mail are never given to it. Dashboard forms refuse posts from other websites.
