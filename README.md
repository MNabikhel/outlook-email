# CloseDesk

An Outlook inbox assistant for **assistant controllers**.

It watches Microsoft 365 / Outlook, reads each new message **and every attachment**, figures out what the file actually is (vendor invoice, bank statement, payroll register, remittance, tax notice, auditor PBC, and so on), flags the mail that should not wait, and builds a **daily action list**: enter this invoice, apply this cash, reconcile this account, call the vendor because the wiring instructions changed.

A realistic demo mailbox is included so you can use it before connecting Outlook.

## What it does

When a message arrives, CloseDesk:

1. Pulls the body and downloads attachments through Microsoft Graph (or the built-in demo mailbox).
2. Extracts text from PDF, Excel, Word, CSV, and HTML. Account and routing numbers are stored as last-4 only.
3. Classifies each attachment and the email as a whole, using finance-controller rules rather than a generic “spam vs not spam” model.
4. Scores importance (critical / high / medium / low) from due dates, dollar amount, sender, month-end proximity, and Outlook’s own importance flag.
5. Turns the message into action items you can complete in the dashboard or export to Excel.
6. Optionally writes Outlook categories and a follow-up flag back onto the message.
7. Builds a daily digest you can open locally, email to yourself, or run from `watch` every morning.

## Improvements aimed at an assistant controller

These are the extras that matter when the inbox is AP, banking, payroll, and close — not a generic mail client:

- **Payment-instruction / BEC trap.** “Our bank details have changed, please wire today” is treated as critical. The action is *verify by phone*, not *process the payment*.
- **Duplicate invoice detection** on invoice number (the classic double-entry from a resent PDF).
- **Missing attachment** when the body says “please see attached” and nothing arrived.
- **Month-end countdown** and extra weight on recs, payroll, and workpapers in the last week of the month.
- **Cash application** vs **AP invoice** vs **outgoing wire**, so remittances and bills are not one pile.
- **CSV export** of the action list for the spreadsheet workflow you already have.
- **Local-first.** SQLite on disk. Demo mode needs no Azure tenant.

## Quick start (demo, no Outlook login)

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m controller_inbox demo --serve
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). You should see a September 2026 mailbox: Northwind invoice INV-10482, a Chase statement, ADP payroll, an IRS CP2000, an auditor PBC, a customer remittance, a close calendar, and a **fraudulent wiring-instruction change**.

Useful commands:

| Command | What it does |
| --- | --- |
| `python -m controller_inbox demo` | Load the sample mailbox and print a digest |
| `python -m controller_inbox serve` | Dashboard (inbox, actions, attachments, digest) |
| `python -m controller_inbox digest` | Rebuild today’s action-item brief |
| `python -m controller_inbox export` | CSV of action items to stdout |
| `python -m controller_inbox status` | Local counts |

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
  demo.py        Sample assistant-controller mailbox
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
