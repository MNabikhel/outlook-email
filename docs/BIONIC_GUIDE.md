# CloseDesk with Bionic — how it runs overnight

The short path is [SETUP.md](SETUP.md). This page is the detail behind it. The slides are [CloseDesk-how-it-works.pptx](CloseDesk-how-it-works.pptx).

CloseDesk reads a heavy Outlook inbox on your laptop. Fast scripts pull the text, the amounts, the dates, and the file type. A local model in LM Studio's Bionic does the actual reading: what the message is, which folder it belongs in, a one-line summary, and the tasks.

You can run that reading two ways.

- **Overnight, unattended.** Leave LM Studio's local server on and run one command. The same model Bionic uses files the queue while you sleep.
- **In Bionic Studio.** Install the skill in this repo. Bionic calls the same tools, one batch at a time, and you can watch it work.

The dashboard is the morning view either way: Daily digest, Important, Action items, Informational, and Reference.

## What each part does

| Piece | Job |
| --- | --- |
| Drop folder | Where you put `.msg` and `.eml` files, plus any loose attachments |
| Scripts | Extract text, invoice numbers, amounts, due dates, and file types. Flag a payment-instruction change. Draft a folder so the board is never empty. |
| Bionic / the local model | Decides category, folder, summary, and action items from that packet. It does not re-open the PDF. |
| Dashboard | Morning board on `http://127.0.0.1:8765` |
| Corrections | When a category is wrong, you say why. That sender is learned. The overnight run will not overwrite a message you corrected. |

Scripts are the tools. The model is the reader. A fraud warning is the one decision the model is not allowed to soften.

## 1. Put the project on the laptop

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

On Windows, activate with `.venv\Scripts\activate`.

Copy `.env.example` to `.env` if you want to change paths or the timezone. The defaults already point at a local database and the drop folder.

## 2. Start the local model

1. Install [LM Studio](https://lmstudio.ai/) and download a model you are willing to leave running overnight. A model that follows JSON instructions matters more than a huge one.
2. Load the model.
3. Start the local server. The default address CloseDesk expects is `http://127.0.0.1:1234/v1`.
4. In `.env`:

```env
CONTROLLER_INBOX_LLM=true
CONTROLLER_INBOX_LLM_BASE_URL=http://127.0.0.1:1234/v1
CONTROLLER_INBOX_LLM_MODEL=local-model
```

`local-model` means "use whatever LM Studio has loaded." Set `CONTROLLER_INBOX_LLM_MODEL` to a specific id only when you want to pin one.

Leave the server running. Overnight has nothing to call if the server is asleep.

## 3. Install the skill in Bionic Studio

The skill is the folder `bionic/closedesk-inbox/`. It contains `SKILL.md` plus `references/tools.md`.

Either:

- In Bionic, open **Settings → Skills** and add that `SKILL.md`, or
- In a Bionic chat, use `@Install Skill` and point it at the folder.

The skill is used when you ask Bionic to process the inbox, or when you type `@` and pick `closedesk-inbox`.

Bionic needs this project open as its working folder, because the skill runs `python -m controller_inbox tool ...` from that folder.

## 4. Give it mail

1. Drag Outlook messages into `inbox/incoming/` as `.msg` or `.eml`.
2. Files stored inside the message are unpacked on their own.
3. If you saved the attachments separately, put them in `inbox/attachments/<same name as the message>/`.
4. A loose PDF or workbook dropped in `inbox/incoming/` is read as its own item.

Excel (`.xlsx`, `.xlsm`, `.xls`), PDF, Word, PowerPoint, CSV, TSV, TXT, RTF, HTML, and ZIP are parsed. Images are marked as scans. If Tesseract is installed, image text is read too.

Account and routing numbers are stored as the last four digits only.

## 5. Run the night

From the project folder, with the virtualenv active and LM Studio's server up:

```bash
python -m controller_inbox overnight
```

That command:

1. Reads anything new in the drop folder.
2. Pulls recent Outlook mail too, if you already connected Microsoft 365.
3. Sends each draft packet to the local model, up to 40 a night (`CONTROLLER_INBOX_OVERNIGHT_BATCH`).
4. Files the reading into Important, Informational, or Reference.
5. Rebuilds the daily digest.
6. Writes `data/overnight/YYYY-MM-DD.md` so you can see what happened.

If the model is off, the command still files script drafts and says so in the log. The dashboard shows those rows as **Waiting on Bionic**. Run the command again after the server is up, or let Bionic finish the queue with the skill.

A prompt you can paste into Bionic when you want the agent, not the unattended command, to do the reading:

```text
Use the closedesk-inbox skill. Process the waiting queue in batches,
save each reading, then build the daily digest. Stop when the queue is empty.
Do not file any payment-instruction warning outside Important.
```

## 6. Read it in the morning

```bash
python -m controller_inbox serve
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765).

| Section | What you do there |
| --- | --- |
| Morning | Counts for the three folders, what's due, and how many messages are still waiting on Bionic |
| Daily digest | One page: fraud warnings, overdue, due today, invoices, cash to apply |
| Important | Mail that needs a decision, a payment check, or a close task |
| Action items | The task list. Mark items done. Export CSV if you want it in a sheet. |
| Informational | Newsletters and FYI. Nothing is waiting on you. |
| Reference | Statements, purchase orders, contracts, and other files to keep. Not tonight's work. |
| All mail | The full list, when you need to search |
| Attachments | Every file, by type |

Each row has a one-line summary. **Waiting on Bionic** means the folder is still the script draft. **Read by Bionic** means the model filed it.

## 7. Teach it when it is wrong

Open the message and use **Wrong category?**. Say what it actually is, and why, in a sentence.

- The next message from that sender follows the correction.
- The example is appended to `data/training/corrections.jsonl`.
- The overnight run skips that message, so the model does not undo you.
- The correction is included in the packet when the model reads a *later* message from the same sender.
- A saved correction cannot clear a new payment-instruction warning. It can clear the warning on the message you corrected, because you looked at that one.

## Fraud stays a hard stop

"Our bank details changed, please wire today" is critical. The action is **verify by phone**, using a number you already have, not "process the payment."

If the model tries to file that packet as informational, or as a newsletter, CloseDesk puts it back in Important, keeps the fraud flag, and drops any action that says to pay the new account. You will see it at the top of the digest under **Do not process**.

A real wire that says "not a change of account" is a wire request, not a fraud flag.

## Sample mailbox

Before your own files are in the folder:

```bash
python -m controller_inbox demo --serve
```

The sample is a September 2026 inbox: a Northwind invoice, a duplicate of that invoice, a bank statement, payroll, an IRS notice, an audit PBC, a remittance, a close calendar, a purchase order, and a fraudulent wiring-instruction change. With the model off, those already land in Important, Informational, and Reference so you can see the morning board.

## Commands

| Command | What it does |
| --- | --- |
| `python -m controller_inbox overnight` | Night run: drop folder, model, digest, log |
| `python -m controller_inbox tool queue_status` | JSON count of what Bionic still has to read |
| `python -m controller_inbox tool prepare_queue --limit 20` | JSON packets (text already extracted) |
| `python -m controller_inbox tool save_reading --json '...'` | File one reading |
| `python -m controller_inbox tool list_folder --folder important` | JSON list for one folder |
| `python -m controller_inbox tool build_digest` | Rebuild the digest |
| `python -m controller_inbox ingest` | Read the drop folder only |
| `python -m controller_inbox serve` | Dashboard |
| `python -m controller_inbox demo` | Load the sample mailbox |

## If you also connect Outlook

That part is optional. The drop folder is enough. When you want Graph as well, register an Entra app as described in the README, then `python -m controller_inbox auth` and leave `AZURE_CLIENT_ID` in `.env`. Overnight will pull recent mail before it asks the model to read.

## What was added so a heavy inbox is usable

These are the pieces beyond "classify and summarize," because a long inbox fails when everything looks the same.

- **Three folders plus the digest and the action list.** Important is the pile you work. Informational is the pile you can skip. Reference is the nearby folder for "keep this, don't do it tonight," so statements and purchase orders do not sit on top of invoices.
- **A visible queue.** Rows say Waiting on Bionic until the model has read them. You can see in the morning whether the night finished.
- **One-line summaries** on the list, so you are not opening every message to find out what it is.
- **An overnight log** at `data/overnight/YYYY-MM-DD.md`.
- **A batch cap** so one huge night does not run until noon. Raise `CONTROLLER_INBOX_OVERNIGHT_BATCH` if the laptop and the model can take more.
- **Your corrections in the model's packet,** and a lock so the night run does not overwrite a message you already fixed.
- **The fraud guardrail** stays in the script, not in the model's judgment.
- **Duplicate invoices and missing attachments** are still caught by the scripts, then shown in the summary.
- **All mail and Attachments** stay one click away when the folders are not the question you have.

The deck `docs/CloseDesk-how-it-works.pptx` is the same story in slides.
