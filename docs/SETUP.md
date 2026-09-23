# CloseDesk — easy setup

CloseDesk turns a pile of Outlook mail into one short page each morning: what you must act on, what you should know, and what can wait. It runs on your laptop. No Outlook add-in, no IT approval, no cloud. It works for any inbox, with or without a local AI model.

The longer notes are in [BIONIC_GUIDE.md](BIONIC_GUIDE.md). The picture versions are [CloseDesk-quick-setup.pptx](CloseDesk-quick-setup.pptx) (these steps, with screenshots) and [CloseDesk-how-it-works.pptx](CloseDesk-how-it-works.pptx).

## One-time setup

1. Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/). On Windows, tick **Add python.exe to PATH** in the installer.
2. Put this project folder somewhere permanent, for example `Documents`. From GitHub's **Download ZIP** the folder is called `outlook-email-main`; rename it `CloseDesk` if you like.
3. Optional: install [LM Studio](https://lmstudio.ai/), download a small *instruct* model (3B–8B is plenty), load it, and start the local server (Developer tab → **Start server**). CloseDesk finds it on its own. Ollama works too (see the README). If your laptop can't run a model, skip this step: sorting, summaries, tasks, fraud warnings, search, and the digest all work without one.
4. After the first launch, open **Setup** and choose **What kind of inbox is this?** *General* suits anyone. *Finance* adds the month-end countdown and close sections.

That's it. The first double-click below finishes the setup by itself.

## Every morning

1. **Get yesterday's mail out of Outlook** into the project's `inbox/incoming` folder:
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
| "Python 3.11 or newer is needed" | Install Python from python.org, tick **Add python.exe to PATH**, double-click again. |
| The page does not open | The CloseDesk window must still be open. Go to [http://127.0.0.1:8765](http://127.0.0.1:8765). |
| Local model: **not running** | Open LM Studio, load a model, and start the server. Then click **Process new mail** again. `python -m controller_inbox llm-check` says exactly what is wrong. |
| Summaries look generic | Those came from the fast scripts. Start the model and process again; the model reads everything that is still waiting. |
| A file landed in `inbox/failed` | Open the `.why.txt` next to it. Usually re-saving the message from Outlook fixes it. |
| A category is wrong | Open the message, use **Wrong category?**, and write one sentence on why. That sender is learned. |
| **Ask CloseDesk** says the model isn't running | That's fine: it still finds emails. Start LM Studio's server for written answers. |
| Mac says CloseDesk.command "can't be opened" | Right-click it → **Open** → **Open** once. After that, double-click works. |
| **Open in Outlook** downloads a file instead | That email has no original file (sample mail, or Outlook sync). Open the downloaded `.eml`. |
| A payment-change email is not in the red box | It should be. Please report it — the fraud rule forces Important and "verify by phone". |
