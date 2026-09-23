---
name: closedesk-inbox
description: >-
  Read a CloseDesk inbox and answer "what do I need to do today?". Use when
  the user asks to process mail, file messages into Important, Informational,
  or Reference, build the daily digest, clear the waiting queue, look back at
  past digests, or run the CloseDesk inbox skill. Scripts already extracted
  the text; you decide the reading.
---

# CloseDesk inbox

You are the reader. Python scripts already pulled the body, attachment text, amounts, dates, invoice numbers, and a draft folder. Do not open the raw PDF, Excel, or `.msg` files unless a packet says the text was empty.

Work from the project root (the folder that contains `src/controller_inbox`).

## Overnight, unattended

If the user wants this to run while they are away, they should start LM Studio's local server and run:

```bash
python -m controller_inbox overnight
```

That calls the same model you are. Use the steps below when you are the one reading the queue in this chat.

## Read the queue

1. See what is waiting:

```bash
python -m controller_inbox tool queue_status
```

2. Pull a batch. Each packet is the document. `script_draft` is a hint, not your decision.

```bash
python -m controller_inbox tool prepare_queue --limit 20
```

3. For each packet, choose:
   - `category` — one id from the packet's allowed set (the tool rejects an unknown id by keeping the draft category)
   - `folder` — `important` (needs a decision or a task), `informational` (FYI, no task), or `reference` (keep the file, not tonight)
   - `importance` — `critical`, `high`, `medium`, or `low`
   - `summary` — one sentence a person can scan at breakfast
   - `actions` — only real tasks, each with `title`, `due` (`YYYY-MM-DD` or null), and `priority`
   - `why` — one sentence

4. Save one reading at a time. Pass the JSON on stdin:

```bash
python -m controller_inbox tool save_reading --json '{"email_id":"...","category":"ap_invoice","folder":"important","importance":"high","summary":"...","actions":[{"title":"Enter INV-10482","due":"2026-10-05","priority":"high"}],"why":"..."}'
```

5. When the batch is filed, build the morning digest:

```bash
python -m controller_inbox tool build_digest
```

6. If `queue_status` still shows messages waiting, pull another batch. Stop when the queue is empty or the user says to stop.

The field list and folder meanings are in `references/tools.md` in this skill folder. Read that file if you need the exact JSON shape.

## Rules you do not get to break

- A packet with `fraud_risk`, or a payment-instruction change, is **important**. Never file it as informational or reference. The action is to verify by phone using a number already on file. Do not create an action to pay, wire, or update the vendor's account.
- If `saved_corrections_for_sender` is present, follow that category and reason, unless this message itself is a new payment-instruction change. A saved correction does not make a new fraud warning safe.
- Do not invent invoice numbers, amounts, dates, or account numbers. Use only what the packet extracted. Account numbers in the packet are already last-4.
- An attachment note that says there is no extractable text is a scan. Say so in the summary. Do not guess what the scan says.
- An empty `actions` list keeps the script's existing tasks. Send actions only when you want those tasks to replace the draft list.
- Do not overwrite a message the user already corrected. Those are `model_status: corrected` and they are not in the queue.
- If `save_reading` returns `guard_notes`, CloseDesk corrected part of your reading (an amount or date that is not in the packet, or a task due this week filed away from Important). Do not try to save it again the other way.

## "What do I need to do today?"

```bash
python -m controller_inbox tool focus
```

Answer from `do_not_process` first, then `focus` in rank order (each row has a label such as *Overdue 2 days* or *Due today*, a title, and a one-line summary), then `coming_up`. Keep it short: the user wants the list, not the method. For an earlier day, pass `--date YYYY-MM-DD`; `tool digest_history` lists the days that have a digest.

## After you finish

Tell the user how many messages you read, how they split across Important, Informational, and Reference, and whether any fraud warning is still in Important. Point them at the dashboard sections: Daily digest, Important, Action items, Informational, Reference.
