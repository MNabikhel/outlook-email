# CloseDesk — easy setup

Follow this page once. When the three checks at the bottom pass, it is ready to run overnight.

You need Python 3.11 or newer. You do not need an Outlook login to start. The longer notes are in [BIONIC_GUIDE.md](BIONIC_GUIDE.md). The picture version is [CloseDesk-how-it-works.pptx](CloseDesk-how-it-works.pptx).

## 1. Install

Open a terminal in this project folder.

Mac or Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Windows (Command Prompt):

```bat
py -3 -m venv .venv
.venv\Scripts\activate
pip install -e ".[dev]"
```

Leave that terminal open. The next commands assume the virtualenv is active (`(.venv)` shows in the prompt).

## 2. See it work before you add a model

```bash
python -m controller_inbox demo --serve
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765).

You should see Morning, with mail already filed into Important, Informational, and Reference. The wiring-instruction sample sits in Important and says to verify by phone. Stop the dashboard with Ctrl+C when you have looked.

## 3. Turn on the local model

1. Install [LM Studio](https://lmstudio.ai/) and download a model that can follow instructions.
2. Load the model.
3. Start the local server. Leave it running. CloseDesk calls `http://127.0.0.1:1234/v1`.
4. In this project folder, copy `.env.example` to `.env` and set:

```env
CONTROLLER_INBOX_LLM=true
CONTROLLER_INBOX_LLM_BASE_URL=http://127.0.0.1:1234/v1
CONTROLLER_INBOX_LLM_MODEL=local-model
```

`local-model` means “use whatever LM Studio currently has loaded.”

Optional: in Bionic, open **Settings → Skills** and add `bionic/closedesk-inbox/SKILL.md`. Open this project as Bionic’s folder. You can skip the skill if you only want the overnight command.

## 4. Give it your mail

Drag Outlook messages into `inbox/incoming/` as `.msg` or `.eml`.

Attachments inside the message are unpacked on their own. If you saved the files separately, put them in `inbox/attachments/` inside a folder with the same name as the message.

## 5. Run the night

LM Studio’s server stays on. In the project terminal:

```bash
python -m controller_inbox overnight
```

In the morning:

```bash
python -m controller_inbox serve
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765) again. Start with Daily digest, then Important, then Action items. Informational is skippable. Reference is “keep this, not tonight.”

A row that says **Waiting on Bionic** was filed by the scripts only. **Read by Bionic** means the model read it. If the server was off, run `overnight` again after you start it.

The night also writes `data/overnight/` plus today’s date, as a markdown file.

## You are ready when

1. `python -m controller_inbox demo --serve` opens the morning board.
2. A `.msg` dropped in `inbox/incoming/` shows up after `python -m controller_inbox ingest` or after `overnight`.
3. With LM Studio’s server on, `overnight` prints a log path and the morning rows can say **Read by Bionic**.

## If something is off

| What you see | What to do |
| --- | --- |
| `python` is not recognized | Use `python3` on Mac, or `py -3` on Windows. |
| The page does not open | The serve command must still be running. Use `http://127.0.0.1:8765`. |
| Everything says Waiting on Bionic | LM Studio’s local server is not running, or `CONTROLLER_INBOX_LLM` is not `true`. Start the server and run `overnight` again. |
| A payment-change email landed in Informational | It should not stay there. Open it. If the warning is gone, say so in the project — the fraud rule is supposed to force Important and “verify by phone.” |

When a category is wrong, open the message, use **Wrong category?**, and write one sentence on why. The next night will not undo that message.
