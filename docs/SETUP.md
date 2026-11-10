# CloseDesk — easy setup

CloseDesk turns a pile of Outlook mail into one short page each morning: what you must act on, what you should know, and what can wait. It runs on your laptop. No Outlook add-in, no IT approval, no cloud. It works for any inbox, with or without a local AI model.

The longer notes are in [BIONIC_GUIDE.md](BIONIC_GUIDE.md). The picture versions are [CloseDesk-quick-setup.pptx](CloseDesk-quick-setup.pptx) (these steps, with screenshots) and [CloseDesk-how-it-works.pptx](CloseDesk-how-it-works.pptx).

## One-time setup

No admin rights are needed at any step, and you don't install Python: where the computer has none that fits, CloseDesk downloads its own copy into your user folder.

1. **Get CloseDesk.**
   - **Mac:** open **Terminal** (⌘ Space, type *Terminal*, Return), paste this line and press Return:

     ```bash
     curl -fsSL https://raw.githubusercontent.com/MNabikhel/outlook-email/main/scripts/install.sh | bash
     ```

     It puts CloseDesk in a `CloseDesk` folder in your home folder, sets it up, and opens the dashboard. From then on, double-click `CloseDesk.command` in that folder.
   - **Windows:** on GitHub click **Code → Download ZIP**, unzip it somewhere permanent (for example `Documents`), and double-click `CloseDesk.bat`. If Windows shows "Windows protected your PC", click **More info → Run anyway**.
2. Optional: install [LM Studio](https://lmstudio.ai/), download a small *instruct* model (3B–8B is plenty), load it, and start the local server (Developer tab → **Start server**). CloseDesk finds it on its own. To also search by meaning, load LM Studio's **nomic-embed-text** embedding model next to it. Ollama works too (see the README). If your laptop can't run a model, skip this step: sorting, summaries, tasks, fraud warnings, search, and the digest all work without one.
3. After the first launch, open **Setup** and choose **What kind of inbox is this?** *General* suits anyone. *Finance* adds the month-end countdown and close sections.

### On a Mac, from a downloaded ZIP

The Terminal line above is the smooth way. If you downloaded the ZIP in a browser, macOS marks everything in it as "from the internet" and won't open `CloseDesk.command` on a double-click; on current macOS, letting it through takes an administrator's password. You don't need one. Open it once from Terminal:

1. Open **Terminal**, type `bash ` (with the space), drag `CloseDesk.command` from Finder into the Terminal window, and press Return.
2. CloseDesk takes that mark off its own folder as it starts. From then on a double-click works.

Keep the folder in your home folder (not in Downloads, Documents or Desktop), and macOS won't ask to let Terminal into those either.

## Updating

Either way works, and neither needs admin rights:

- **Double-click `CloseDesk-Update.command`** (Mac) or **`CloseDesk-Update.bat`** (Windows). It fetches the newest CloseDesk and replaces CloseDesk's own files. Your mail (`data`, `inbox`), your settings (`.env`) and the `AP cost codes` folder are not touched, and the files it replaced are kept in `.previous`.
- **Or by hand:** download the ZIP again and swap the folders, keeping `data`, `inbox`, `.env` and `AP cost codes` from the old one. (On a Mac the Terminal line from the setup does this for you: run again, it updates.)

Then open CloseDesk as usual. Every start checks the set-up and puts right what isn't: packages an update added, a folder that was moved or renamed, a Python that was removed or upgraded. When everything is in place it just starts (the check takes about half a second).

To see the check without starting, run `CloseDesk.command --check` (Mac) or `CloseDesk.bat --check` (Windows). It lists what is in order, what is optional and missing, and what to do about anything marked FIX.

## Every morning

1. **Classic Outlook on Windows: let CloseDesk copy it.** Open **Settings → Outlook (classic)**, click **Check Outlook**, tick the folders to copy (the Inbox is ticked), and either turn on **Sync automatically** or click **Sync now** when you want. It copies mail and attachments and never changes anything in Outlook; mail you delete in Outlook disappears from CloseDesk on the next sync. If you use this, skip the dragging below.

   **Otherwise, get yesterday's mail out of Outlook** into the project's `inbox/incoming` folder:
   - **Classic Outlook (Windows):** click the first message, Shift+click the last, and drag the selection onto `inbox\incoming` in File Explorer. Each becomes a `.msg` with its attachments inside.
   - **New Outlook / Outlook on the web:** open a message → **… → Save as** (or **Download**) → save the `.eml` into `inbox/incoming`.
   - **Outlook for Mac:** drag messages into `inbox/incoming` in Finder.

   Tip: pin `inbox\incoming` to Quick Access or make a desktop shortcut. Dropping an email you already dropped is fine; it is recognized.

2. **Double-click `CloseDesk.bat`** (Windows) or **`CloseDesk.command`** (Mac).

   It reads the folder, lets the local model read the most important mail first, writes today's digest, and opens the dashboard in your browser. Leave the black window open while you use the dashboard; close it when you're done.

3. **Read the Today page top to bottom.**
   - A red **Do not process — verify by phone** box means someone asked to change payment details. Call a number you already have before doing anything.
   - **Your focus today** is the short ranked list. Click **Done** as you finish things; they drop off.
   - **What came in since yesterday** splits the rest into *Needs you*, *Worth knowing*, and *Filed for reference*, each with a one-line summary.

Every day's digest is kept under **Past digests**, so you can look back at what came in last Tuesday.

## Getting around

- **Open an email:** click it anywhere (the focus list, a folder, search results), and it opens in a panel on the right. **Esc** closes it.
- **Open in Outlook:** opens the original `.msg` or `.eml` with your mail app. Mail loaded some other way (the sample, or Outlook sync) downloads as an `.eml` copy instead.
- **Draft a reply:** writes a draft to copy, or opens it in your mail app. With no model you get a starter template. A payment-change email gets "verify by phone" advice instead of a reply.
- **Ask CloseDesk:** the button at the bottom-right. Try *what's urgent today?*, *what needs a reply?*, *anything from Maya?*, or *invoice 10482*. Click a [number] in the answer to open that email. With no model, it lists the matching emails and your focus list.
- **Ask about an email and its files:** open the email and ask, for example *summarize the draft*, *what does the contract say about renewal?*, or *does the total on the Summary sheet add up?* It reads the attachments (PDF pages, every sheet with its formulas, Word drafts with tracked changes, slides), and can open other emails if the answer is there. On the email page each file has **Read text**, **Summarize**, and **Ask…**.
- **Fraud check:** a flagged email shows why, signal by signal. If it is wrong, click **Not fraud**, **Trust** the sender, or **Trust everyone at @yourco.com**; if it is right, **This is fraud**. It learns from each answer, and the **Fraud check** page (left menu) keeps the log. Add your own company's domain there first.
- **Search:** press `/` anywhere and type a name, company, invoice number, or a phrase from an attachment.

## Try it first with sample mail

```bash
python -m controller_inbox demo --serve
```

Or click **Load sample mailbox** on the Get started page. Once your own mail is loaded, the sample button is switched off so it can never erase your mail.

## Run it before you wake up (optional)

Windows Task Scheduler, weekdays at 6:30 (change the path):

```bat
schtasks /Create /SC WEEKLY /D MON,TUE,WED,THU,FRI /ST 06:30 /TN "CloseDesk morning" /TR "\"C:\Users\you\Documents\CloseDesk\scripts\overnight.bat\""
```

Mac / Linux (`crontab -e`):

```cron
30 6 * * 1-5  /Users/you/Documents/CloseDesk/scripts/overnight.sh
```

Leave LM Studio's server running overnight. In the morning, double-click CloseDesk as usual — the night's reading is already done, so it only picks up anything new and opens the digest.

## You are ready when

1. Double-clicking CloseDesk opens the dashboard.
2. A message dragged into `inbox/incoming` shows up on the Today page after **Process new mail**.
3. The rail at the bottom-left says **Local model** with a green dot and the model's name (or you are happy with script-only filing).

## If something is off

| What you see | What to do |
| --- | --- |
| The Mac asks for an administrator's name and password | CloseDesk never needs one. It is macOS asking, for one of three things: opening a file downloaded in a browser (use the Terminal line in the setup, or the steps under "On a Mac, from a downloaded ZIP"), Python's own installer from python.org (not needed: CloseDesk fetches its own Python), or "install the command line developer tools" (click **Cancel**; an older CloseDesk tripped that, this one doesn't). |
| "CloseDesk couldn't get Python" | The download of CloseDesk's own Python didn't work. Check the internet connection and open CloseDesk again. If the computer blocks such downloads, ask IT for Python 3.12; CloseDesk uses it when it finds it. |
| Something is off after an update or after moving the folder | Open CloseDesk again: every start checks the set-up and repairs it. `CloseDesk.command --check` (or `CloseDesk.bat --check`) lists what is and isn't in order. Deleting the `.venv` folder makes the next start set everything up afresh; your mail isn't in it. |
| "Another program is using port 8765" | CloseDesk says so and uses the next free port that time. If the other program is LM Studio's server, set that back to its own port (1234). |
| The page does not open | The CloseDesk window must still be open. Go to [http://127.0.0.1:8765](http://127.0.0.1:8765). |
| Local model: **not running** | Open LM Studio, load a model, and start the server. Then click **Process new mail** again. `python -m controller_inbox llm-check` says exactly what is wrong. |
| "The local model read 0" or "three messages in a row failed" | Update CloseDesk (double-click `CloseDesk-Update`). Reasoning models such as Qwen3.5 used to come back empty; they now work, with thinking turned off for filing. The message ends with what the model actually sent, and `python -m controller_inbox llm-check` shows the same for one sample. |
| Which LM Studio address? | CloseDesk uses the OpenAI-compatible one, `http://localhost:1234/v1`, and adds `/chat/completions` itself. Pasting `…/v1/chat/completions` or `…/api/v1/chat` into `CONTROLLER_INBOX_LLM_BASE_URL` works too. |
| Summaries look generic | Those came from the fast scripts. Start the model and process again; the model reads everything that is still waiting. |
| **Outlook (classic)** says it isn't available | It works on Windows with classic Outlook only. On New Outlook, use the drop folder. |
| Outlook asks "A program is trying to access…" | Choose **Allow**. If IT blocks programs from reading Outlook, the sync says so; use the drop folder instead. |
| A file landed in `inbox/failed` | Open the `.why.txt` next to it. Usually re-saving the message from Outlook fixes it. |
| A category is wrong | Open the message, use **Wrong category?**, and write one sentence on why. That sender is learned. |
| **Ask CloseDesk** says the model isn't running | That's fine: it still finds emails. Start LM Studio's server for written answers. |
| **Ask CloseDesk** says the model didn't answer | The model is running but sent no answer; the reason is in brackets. Try again, or load a non-reasoning *instruct* model. |
| Mac says CloseDesk.command "can't be opened" | It was downloaded in a browser. Open it once from Terminal (see "On a Mac, from a downloaded ZIP"); after that, double-click works. |
| **Open in Outlook** downloads a file instead | That email has no original file (sample mail, or Outlook sync). Open the downloaded `.eml`. |
| A payment-change email is not in the red box | It should be. Please report it — the fraud rule forces Important and "verify by phone". |
| Colleagues' emails get fraud warnings | Open **Fraud check** in the left menu and trust your company's domain (for example `taz.com`), or click **Trust everyone at @…** on one of their emails. The flagged mail is checked again straight away. |
| An attachment's files are "locked" | The email is flagged as possible payment fraud, so its files don't download and the model doesn't read them. Verify by phone, then click **Not fraud** on the email. You can read the text either way. |
| **Ask CloseDesk** says it only read part of a file | The model's context window is small. CloseDesk reloads LM Studio models with 16,384 tokens on its own (the **Minimum context** slider in Setup changes this and shows what each size can read); if the chat says that failed, the computer is short of memory: close other apps or pick a smaller model, then in LM Studio set My Models → the model's settings → **Context Length** 16,384 and reload. **Setup** shows the current size. |
| **Ask CloseDesk** gets a spreadsheet comparison wrong | Name the columns: "which line went up most from Q3 actual to Q4 budget on the Detail sheet?" Small models (3B) still slip on row-by-row arithmetic; a 7B+ model is steadier. |
| **Ask CloseDesk** gets an attachment wrong | Ask it to name the page or sheet; its notes are on the email page under **Notes from Ask CloseDesk** (clear them there to start fresh). A picture or scanned PDF is read with OCR; if **Setup** says OCR isn't available, run `CloseDesk.command --check` (or `CloseDesk.bat --check`): it says why and what to do. |
