# CloseDesk — easy setup

CloseDesk turns a pile of Outlook mail into one short page each morning: what you must act on, what you should know, and what can wait. It runs on your laptop. No Outlook add-in, no IT approval, no cloud. It works for any inbox, with or without a local AI model.

The longer notes are in [BIONIC_GUIDE.md](BIONIC_GUIDE.md). The picture versions are [CloseDesk-quick-setup.pptx](CloseDesk-quick-setup.pptx) (these steps, with screenshots) and [CloseDesk-how-it-works.pptx](CloseDesk-how-it-works.pptx).

## One-time setup

1. **Python 3.11 or newer** must be on the computer (3.12 is the best choice). If it isn't, install it from [python.org](https://www.python.org/downloads/). On Windows, tick **Add python.exe to PATH**; *Install for me only* needs no admin rights. CloseDesk uses the Python that is there and never installs one on its own. Where there is none, it stops and says so. (No admin rights on a Mac? See below.)
2. **Get CloseDesk.**
   - **Mac:** open **Terminal** (⌘ Space, type *Terminal*, Return), paste this line and press Return:

     ```bash
     curl -fsSL https://raw.githubusercontent.com/MNabikhel/outlook-email/main/scripts/install.sh | bash
     ```

     It puts CloseDesk in a `CloseDesk` folder in your home folder, sets it up, and opens the dashboard. It also adds **CloseDesk** to Applications in your user folder: from then on, open it from there (Spotlight finds it) or keep it in the Dock. Double-clicking `CloseDesk.command` in that folder still works too.
   - **Windows:** on GitHub click **Code → Download ZIP**, unzip it somewhere permanent (for example `Documents`), and double-click `CloseDesk.bat`. If Windows shows "Windows protected your PC", click **More info → Run anyway**. The first start adds a **CloseDesk** shortcut to your Desktop and Start menu: from then on, open CloseDesk with it. `CloseDesk.bat` still works too.
3. Optional: install [LM Studio](https://lmstudio.ai/), download a chat model (a 3B–8B *instruct* model is plenty; a bigger one works if your computer has the memory), and start the local server (Developer tab → **Start server**). CloseDesk finds it on its own. You don't have to load the model: when none is loaded, CloseDesk loads one as it starts (the one you used last, or the biggest one you downloaded of up to about 8B that fits this computer; a Mac counts the share macOS gives the graphics side, a PC with a graphics card counts the card's memory). To also search by meaning, load LM Studio's **nomic-embed-text** embedding model next to it. Ollama works too (see the README). If your laptop can't run a model, skip this step: sorting, summaries, tasks, fraud warnings, search, and the digest all work without one.
4. After the first launch, open **Setup** and choose **What kind of inbox is this?** *General* suits anyone. *Finance* adds the month-end countdown and close sections.

### Without admin rights on a Mac

CloseDesk itself never needs an administrator. Python's installer for the Mac does. If you can't get one to install Python, ask CloseDesk to download a copy of Python for itself. It goes into your own user folder, nothing is installed on the computer, and no rights are asked. CloseDesk only does this when you ask:

- installing for the first time, use this line instead of the one above:

  ```bash
  curl -fsSL https://raw.githubusercontent.com/MNabikhel/outlook-email/main/scripts/install.sh | bash -s -- --get-python
  ```

- or, with CloseDesk already in place: `bash ~/CloseDesk/CloseDesk.command --get-python`

(On Windows the same is `CloseDesk.bat --get-python`, for a computer where you can't install Python at all.)

### On a Mac, from a downloaded ZIP

The Terminal line above is the smooth way. If you downloaded the ZIP in a browser, macOS marks everything in it as "from the internet" and won't open `CloseDesk.command` on a double-click; on current macOS, letting it through takes an administrator's password. You don't need one. Open it once from Terminal:

1. Open **Terminal**, type `bash ` (with the space), drag `CloseDesk.command` from Finder into the Terminal window, and press Return.
2. CloseDesk takes that mark off its own folder as it starts. From then on a double-click works.

Keep the folder in your home folder (not in Downloads, Documents or Desktop), and macOS won't ask to let Terminal into those either.

## The CloseDesk shortcut and window

The first time `CloseDesk.bat` or `CloseDesk.command` opens the dashboard, CloseDesk adds a shortcut that opens it in a
window of its own, with its own icon, instead of a browser tab:

- **Windows:** **CloseDesk** on your Desktop and in your Start menu. To keep it on the taskbar, right-click CloseDesk's
  button there while it is open and choose **Pin to taskbar**.
- **Mac:** **CloseDesk** in Applications in your user folder. Spotlight finds it (⌘ Space, type *CloseDesk*); to keep
  it in the Dock, right-click its Dock icon while it is open and choose **Options → Keep in Dock**.

No admin rights are needed, and since the shortcut is made on your computer (it is never downloaded), neither Windows
nor macOS asks about it. On Linux there is no shortcut: `CloseDesk.command` opens CloseDesk in the browser.

The window is CloseDesk: it reads the new mail and opens the dashboard as the double-click files do, and **closing the
window closes CloseDesk**. While mail is being processed, or CloseDesk is updating, it asks first (*CloseDesk is still
processing mail. Close it anyway?*): **Close** closes it and **Cancel** keeps it open (on Windows the buttons are
**OK** and **Cancel**). An update carries on by itself after the window is closed; open CloseDesk again once it is
done (opened meanwhile, CloseDesk says it is being updated). In the window:

- **Open in mail app** opens your mail app; another website, the printable digest and a file's original open in your
  browser.
- **Download** (a file, the `.eml` copy, a CSV) asks where to save it, starting in your Downloads folder.
- When a newer version is out, the window asks as the browser does. **Update now** keeps the window open: it says
  **Updating CloseDesk** while the update runs, then opens the new version by itself. If the update can't finish,
  CloseDesk opens as it was and says so.
- If CloseDesk is already open (from `CloseDesk.bat` / `CloseDesk.command`, or another window), the shortcut shows
  that one, and closing the window leaves it open. If that one is closed while this window is still open, this window
  starts CloseDesk again by itself (after waiting a moment, in case it was only updating).

The double-click files work exactly as before: CloseDesk in your browser, with the black window (Terminal on a Mac)
that you leave open while you use it. Use whichever you like.

**Where the window can't open, CloseDesk opens in your browser instead,** and says why the first time:

- **Windows without Microsoft Edge WebView2 Runtime.** Windows 11 has it; some company Windows 10 laptops don't. Your
  IT team can install it (it is free from Microsoft); after that CloseDesk opens in its window.
- **The window's part couldn't be installed** (no internet at the first start, say). Each start from the shortcut
  tries again.

**Deleted the shortcut?** It isn't put back. To add it again: **Settings → This copy and its check → Add the CloseDesk
shortcut again** (on a Mac, **Add CloseDesk to Applications again**; in the classic pages, **Setup → This copy**), or
run `CloseDesk.bat --shortcut` (Mac: `bash ~/CloseDesk/CloseDesk.command --shortcut`).

**Moved or renamed CloseDesk's folder?** The shortcut can't find it until you double-click `CloseDesk.bat` (or
`CloseDesk.command`) once in its new place; that start points the shortcut at the new place. Until then, Windows says
*Problem with Shortcut*, and the Mac says *CloseDesk can't find its folder*. If Windows offered to delete the shortcut
and you clicked **Yes**, that start puts it back.

**The same Desktop on two computers** (OneDrive keeps it in step): the shortcut on it opens CloseDesk on the computer
that made it first. The other computer leaves it as it is and says so; there, open CloseDesk from the Start menu.

**On a Mac, keep the folder in your home folder** (the install line puts it there). From Desktop, Documents, Downloads,
iCloud Drive, another cloud drive or another disk, macOS would ask before letting CloseDesk.app in, so from there
CloseDesk.app opens `CloseDesk.command` in Terminal and CloseDesk in your browser instead. Move the folder into your
home folder and double-click `CloseDesk.command` once, and the window opens from then on. The same goes for the
folders `.env` can put elsewhere (`CONTROLLER_INBOX_DATA_DIR`, `CONTROLLER_INBOX_INBOX_DIR`,
`CONTROLLER_INBOX_COST_CODES_DIR`): one of them in Desktop, Documents, Downloads or a cloud drive has CloseDesk.app open
Terminal too, and the first start says which.

There is no black window with the shortcut, so what CloseDesk says as it starts is written to its log:
`%LOCALAPPDATA%\CloseDesk\CloseDesk-app.log` on Windows, `~/Library/Logs/CloseDesk/CloseDesk-app.log` on a Mac. When
something goes wrong as it opens, a message says so and names the log.

## Updating

Each copy has a version number, its date year first, shown with the first characters of its commit: **CloseDesk
2026.11.16 (e8926db)**. You see it at the bottom of the side bar, in **Settings → This copy and its check**, and on the
first line of the self-check. The date is the commit's day in UTC, as GitHub gives it.

When CloseDesk opens, and once a day while it is open, it asks GitHub for the newest version's number; nothing about
your mail is sent. When a newer one is out, the first page CloseDesk shows asks: **A new version of CloseDesk is
ready**, with the new and the current version, what changed last, and two buttons. **Update now** updates as below;
**Not now** closes the question and CloseDesk opens as normal, and it asks again the next time you open CloseDesk (not
on every page). Opening CloseDesk with no internet, or when GitHub is slow, nothing is asked and nothing waits on it:
the asking gives up after a few seconds. A newer version found later while CloseDesk is open (it asks again a minute
later, and once a day) is shown in Settings and the top bar, not asked about in the middle of your work. If an update
didn't finish, the next start says **CloseDesk wasn't updated** and opens as it was.

The top bar also shows a small **Update ready** link, and Settings says **A newer CloseDesk is ready (2026.11.16; you
have 2026.11.13)** with an **Update** button. To stop the asking, put `CONTROLLER_INBOX_UPDATE_CHECK=false` in the
`.env` file in CloseDesk's folder: then no question comes up either. When GitHub can't be reached (no internet, or a
company network that blocks it), Settings says so under the version, and CloseDesk-Update still works.

Any of these works, and none needs admin rights:

- **Click Update now when CloseDesk asks as it opens.** The same as Update in Settings, next.
- **Click Update in Settings.** CloseDesk closes, fetches the newest version, and opens again by itself in a minute or two: in its window, which says **Updating CloseDesk** meanwhile, or in the same browser tab. Your mail and settings are kept. It waits while mail is being processed, and offers the update again once that is done. (This needs CloseDesk opened with its shortcut or by double-clicking `CloseDesk.bat` / `CloseDesk.command`; started some other way, it doesn't ask as it opens, and Settings says to use CloseDesk-Update instead.)
- **Double-click `CloseDesk-Update.command`** (Mac) or **`CloseDesk-Update.bat`** (Windows). It fetches the newest CloseDesk and replaces CloseDesk's own files. Your mail (`data`, `inbox`), your settings (`.env`) and the `AP cost codes` folder are not touched, and the files it replaced are kept in `.previous`.
- **Or by hand:** download the ZIP again and swap the folders, keeping `data`, `inbox`, `.env` and `AP cost codes` from the old one. (On a Mac the Terminal line from the setup does this for you: run again, it updates.)

After CloseDesk-Update or an update by hand, open CloseDesk as usual. Every start checks the set-up and puts right what isn't: packages an update added, a folder that was moved or renamed, a Python that was upgraded or replaced. When everything is in place it just starts (the check takes about half a second). Two of these need a double-click on `CloseDesk.bat` (or `CloseDesk.command`) once, since the shortcut can't start CloseDesk until they are put right: a folder that was moved or renamed, and on Windows a Python that was removed (the Python CloseDesk was set up with, uninstalled after a newer one came).

To see the check without starting, run `CloseDesk.command --check` (Mac) or `CloseDesk.bat --check` (Windows). It lists what is in order, what is optional and missing, and what to do about anything marked FIX.

## Every morning

1. **Classic Outlook on Windows: connect it once.** Open **Settings → Sync inbox** and click **Connect to Outlook**. CloseDesk says what it will do and asks first: it copies your mail and attachments, and your calendar if you leave **Read my calendar too** ticked, and keeps them up to date while it's open. It only reads: nothing in Outlook is changed, moved, marked read, flagged, deleted or sent. Mail you delete in Outlook disappears from CloseDesk on the next sync. Which folders, how often and how far back are under **Advanced** (the Inbox, every 15 minutes, the last 30 days, until you change them). **Disconnect** stops it; what was copied stays. If you use this, skip the dragging below.

   **Otherwise, get yesterday's mail out of Outlook** into the project's `inbox/incoming` folder:
   - **Classic Outlook (Windows):** click the first message, Shift+click the last, and drag the selection onto `inbox\incoming` in File Explorer. Each becomes a `.msg` with its attachments inside.
   - **New Outlook / Outlook on the web:** open a message → **… → Save as** (or **Download**) → save the `.eml` into `inbox/incoming`.
   - **Outlook for Mac:** drag messages into `inbox/incoming` in Finder.

   Tip: pin `inbox\incoming` to Quick Access or make a desktop shortcut. Dropping an email you already dropped is fine; it is recognized.

2. **Open CloseDesk:** the **CloseDesk** shortcut on your Desktop or in the Start menu (Windows), or **CloseDesk** in the Dock or Applications (Mac).

   It reads the folder, lets the local model read the most important mail first, writes today's digest, and opens the dashboard in CloseDesk's window. Close the window when you're done; that closes CloseDesk. (Double-clicking `CloseDesk.bat` or `CloseDesk.command` does the same in your browser: leave the black window open while you use the dashboard, and close it when you're done.)

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
- **Related mail:** each email shows its conversation, oldest first, and the other emails that share an invoice number, a PO, a file, or another version of a file with it, each with why. **Whole matter** shows everything linked to it on one page, with what changed between versions of its files. With a local model, **Look for more** has it suggest emails about the same matter that nothing else ties together, with its reason. Mail held as possible fraud is marked and its files link to nothing.
- **Calendar:** the left menu (or `g` then `m`). Day, week and month; `t` today, `d`/`w`/`m` to switch, ←/→ to move. Click a meeting to see where, who, and the mail about it. Meetings come from your Outlook calendar when it is connected with the calendar ticked, and from meeting invitations in your mail either way. Ask CloseDesk *what's on tomorrow?* or *am I free Friday?*

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

Leave LM Studio's server running overnight. In the morning, double-click CloseDesk as usual — the night's reading is already done, so it only picks up anything new and opens the digest. With classic Outlook connected, the morning run copies your mail only if Outlook is open; it never starts Outlook itself, and CloseDesk syncs when you start it.

## You are ready when

1. The CloseDesk shortcut (or double-clicking `CloseDesk.bat` / `CloseDesk.command`) opens the dashboard.
2. A message dragged into `inbox/incoming` shows up on the Today page after **Process new mail**.
3. The rail at the bottom-left says **Local model** with a green dot and the model's name (or you are happy with script-only filing).

## If something is off

| What you see | What to do |
| --- | --- |
| The Mac asks for an administrator's name and password | CloseDesk never needs one. It is macOS asking, for one of three things. Opening a file downloaded in a browser: use the Terminal line in the setup, or the steps under "On a Mac, from a downloaded ZIP". Python's own installer: see "Without admin rights on a Mac". "Install the command line developer tools": click **Cancel**; an older CloseDesk set that off, this one doesn't. |
| Windows: "No runtime installed that matches 3.12" | An older CloseDesk took the Python install manager's `py` for a Python it could use. Update CloseDesk (`CloseDesk-Update.bat`); this one moves on to the Python you have. |
| "CloseDesk needs Python 3.11 or newer" | The computer has no Python CloseDesk can use. Install it from python.org and open CloseDesk again; the message also gives the line to run if you can't install it. |
| Something is off after an update or after moving the folder | Open CloseDesk again: every start checks the set-up and repairs it. `CloseDesk.command --check` (or `CloseDesk.bat --check`) lists what is and isn't in order. Deleting the `.venv` folder makes the next start set everything up afresh; your mail isn't in it. |
| "Another program is using port 8765" | CloseDesk says so and uses the next free port that time. If the other program is LM Studio's server, set that back to its own port (1234). |
| The page does not open | The CloseDesk window (or the black window, with the double-click files) must still be open. Go to [http://127.0.0.1:8765](http://127.0.0.1:8765). |
| The CloseDesk shortcut opens CloseDesk in the browser, not a window | The window can't open on this computer, and the first time CloseDesk said why: usually Windows without Microsoft Edge WebView2 Runtime, which IT can install (free from Microsoft), or the window's part not installed yet (each start tries again). See "The CloseDesk shortcut and window". |
| Windows: *Problem with Shortcut*, or the Mac: *CloseDesk can't find its folder* | CloseDesk's folder was moved or renamed. Double-click `CloseDesk.bat` (or `CloseDesk.command`) once in its new place: the shortcut follows it (and is put back if you let Windows delete it). |
| Windows: *Python venv launcher is sorry to say ... did not find executable* | The Python CloseDesk was set up with is gone (uninstalled, often after a newer one was installed). Double-click `CloseDesk.bat` once: it sets CloseDesk up again with the Python that is there, and the shortcut works again. |
| The CloseDesk shortcut says *CloseDesk couldn't open*, or does nothing | What went wrong is in its log: `%LOCALAPPDATA%\CloseDesk\CloseDesk-app.log` (Windows) or `~/Library/Logs/CloseDesk/CloseDesk-app.log` (Mac). Double-clicking `CloseDesk.bat` / `CloseDesk.command` shows the same start in its window, and `--check` says what isn't in order. |
| The CloseDesk shortcut or the Mac's CloseDesk is gone | It isn't put back once deleted. **Settings → This copy and its check → Add the CloseDesk shortcut again** (Mac: **Add CloseDesk to Applications again**), or `CloseDesk.bat --shortcut` / `CloseDesk.command --shortcut`. |
| Mac: CloseDesk.app opens Terminal and the browser, not a window | CloseDesk's folder, or a folder `.env` puts elsewhere (the data, the mail drop, the AP cost codes), is in Desktop, Documents, Downloads, iCloud Drive, another cloud drive or on another disk, where macOS would ask first. Move it into your home folder (and change its line in `.env`), then double-click `CloseDesk.command` once. |
| Local model: **not running** | Open LM Studio and start the server. CloseDesk loads a model the next time it starts; or choose one in Settings → **Model that answers your questions**. Then click **Process new mail** again. `python -m controller_inbox llm-check` says exactly what is wrong. |
| "CloseDesk couldn't load … in LM Studio" when it starts | LM Studio couldn't load the model CloseDesk chose (usually not enough memory). The mail is filed without a model meanwhile. Close other apps, or choose a smaller model in Settings → **Model that answers your questions**. |
| CloseDesk loaded a model I didn't want | It loads one only when none is loaded. Choose another in Settings → **Model that answers your questions** (it is used from then on), or load yours in LM Studio before you start CloseDesk. |
| The overnight note says *Stopped reading: … takes over 816 s an email on this computer* | The model sent nothing for twice the wait CloseDesk gives it between words (Settings → **Local model** shows that wait), so it is too big to read mail at a useful speed here. Choose the smaller model the note names (or any 3B–8B one) in Settings → **Local model**. A model that writes slowly but steadily is not stopped. |
| The note says *LM Studio was still loading …* | LM Studio took more than 10 minutes to load the model. Check LM Studio shows it loaded (or choose a smaller one), then **Process new mail** again. |
| Settings or Today suggests another model | The one answering is slow on this computer (more than about 45 seconds an email) or too big for its memory, and a smaller one you downloaded fits. **Load** switches to it; **Keep** leaves yours, and you aren't asked about that model again. |
| "The local model read 0" or "three messages in a row failed" | Update CloseDesk (double-click `CloseDesk-Update`). Reasoning models such as Qwen3.5 used to come back empty; they now work, with thinking turned off for filing. The message ends with what the model actually sent, and `python -m controller_inbox llm-check` shows the same for one sample. |
| Which LM Studio address? | CloseDesk uses the OpenAI-compatible one, `http://localhost:1234/v1`, and adds `/chat/completions` itself. Pasting `…/v1/chat/completions` or `…/api/v1/chat` into `CONTROLLER_INBOX_LLM_BASE_URL` works too. |
| The note says *Stopped summarizing files: … took longer than 6 minutes on …* (or *Stopped looking for related mail*), or LM Studio's log shows *client disconnected, stopping generation* | The model was still reading or writing a long file's summary when CloseDesk's wait for it ran out, so the rest of that run's summaries, and its related-mail questions, were left for the next run. The next run summarizes the other files first, then tries that one again with twice the wait (the note says how long), up to 30 minutes, or the wait you set when that is longer. If it already had that long, it is left until a longer wait is set (Settings names it: *Left until a longer wait is set: …*): type more minutes in Settings → **Local model** → **Wait up to … minutes** and click **Save**. That wait is for the chat's answers and reading new mail; file summaries wait at least as long. While a summary is written, the bar at the top says how long it has taken, and **Stop** there ends it at once. A smaller model (3B to 8B) writes a summary in a fraction of the time. |
| Summaries look generic | Those came from the fast scripts. Start the model and process again; the model reads everything that is still waiting. |
| **Sync inbox** says classic Outlook isn't on this computer | It works on Windows with classic Outlook only. On New Outlook or a Mac, use the drop folder; meeting invitations in that mail still show in the calendar. |
| The calendar shows only some meetings | Without Outlook connected, it shows the meetings from invitations in your mail. Connect Outlook in **Settings → Sync inbox** with **Read my calendar too** on for the whole calendar; it is read once an hour. |
| **Sync inbox** says "Last sync didn't work" | The reason follows it (Outlook wasn't open, or couldn't be read). Open classic Outlook and click **Sync now**; the line goes once a sync works. Mail already copied stays. |
| Outlook asks "A program is trying to access…" | Choose **Allow**. If IT blocks programs from reading Outlook, the sync says so; use the drop folder instead. |
| A file landed in `inbox/failed` | Open the `.why.txt` next to it. Usually re-saving the message from Outlook fixes it. |
| A category is wrong | Open the message, use **Wrong category?**, and write one sentence on why. That sender is learned. |
| **Ask CloseDesk** says the model isn't running | That's fine: it still finds emails. Start LM Studio's server for written answers. |
| **Ask CloseDesk** says the model didn't answer | The model is running but sent no answer; the reason is in brackets, in LM Studio's own words when it gave one. Try again, or load a non-reasoning *instruct* model. |
| The chat or the reading says a model "thinks before every answer and can't be told not to" | Some reasoning models (DeepSeek-R1 distills, QwQ) always think, and LM Studio can't turn that off. CloseDesk gives them twice the usual room and waits longer for them; when even that runs out, it stops and says so. Choose a model that doesn't have to think first (like Gemma 3, or a Qwen3 that isn't a "Thinking" build), or a smaller one, in Settings → **Model that answers your questions**. |
| Mac says CloseDesk.command "can't be opened" | It was downloaded in a browser. Open it once from Terminal (see "On a Mac, from a downloaded ZIP"); after that, double-click works. |
| **Open in Outlook** downloads a file instead | That email has no original file (sample mail, or Outlook sync). Open the downloaded `.eml`. |
| A payment-change email is not in the red box | It should be. Please report it — the fraud rule forces Important and "verify by phone". |
| Colleagues' emails get fraud warnings | Open **Fraud check** in the left menu and trust your company's domain (for example `taz.com`), or click **Trust everyone at @…** on one of their emails. The flagged mail is checked again straight away. |
| An attachment's files are "locked" | The email is flagged as possible payment fraud, so its files don't download, it doesn't open in your mail app, and the model doesn't read it or its files. Verify by phone, then click **Not fraud** on the email. You can read the email's text either way; its files' text shows once it is marked safe. |
| **Ask CloseDesk** says the model ran out of memory (LM Studio's "Compute error", "llama_decode returned -3", "Insufficient Memory", or a crash with a very long exit code) | The model is too big for this computer. CloseDesk only needs a small one: a 3B to 8B model is plenty and answers faster. Pick one under **Model that answers your questions** in Settings (a model too big for this computer is marked there), or do what the chat's note says about the context: for a model CloseDesk loaded, move **Minimum context** down (to 8k, say) and Save, and it is reloaded with it straight away (the note leaves this out when the model already has the shortest context CloseDesk gives it here); for one you loaded yourself, reload it in LM Studio with the **Context Length** the note names, the most that fits this computer. When CloseDesk itself gave the model its context, the chat asks before it changes anything: **Reload and ask again** loads it with the smaller size offered and asks your question again, **Keep as is** leaves it as it is. Settings → **Local model** offers the same button. If the overnight reading or **Process new mail** ran out of memory, it says so after the run and the mail is filed without the model; reload it smaller in Settings → **Local model**. |
| **Ask CloseDesk** says it only read part of a file | The model's context window is small. CloseDesk reloads LM Studio models with 16,384 tokens on its own (the **Minimum context** slider in Setup changes this and shows what each size can read), or for a model too big for that on your computer, the most that fits: Settings → **Local model** shows what it chose, and a smaller model reads whole files; if the chat says that failed, the computer is short of memory: close other apps or pick a smaller model, then in LM Studio set My Models → the model's settings → **Context Length** 16,384 and reload. **Setup** shows the current size. |
| **Ask CloseDesk** says the model needed more time | A big model on a laptop reads a long question and its files slowly. Under the answer, the minutes box is filled in with about how long it needs: click **Save and ask again**. The wait is kept for that model; change it any time in Settings → **Local model** (or click **Automatic** there to let CloseDesk work it out again). A smaller model (3B to 8B) answers in a fraction of the time. |
| **Ask CloseDesk** gets a spreadsheet comparison wrong | Name the columns: "which line went up most from Q3 actual to Q4 budget on the Detail sheet?" Small models (3B) still slip on row-by-row arithmetic; a 7B+ model is steadier. |
| **Ask CloseDesk** gets an attachment wrong | Ask it to name the page or sheet; its notes are on the email page under **Notes from Ask CloseDesk** (clear them there to start fresh). A picture or scanned PDF is read with OCR; if **Setup** says OCR isn't available, run `CloseDesk.command --check` (or `CloseDesk.bat --check`): it says why and what to do. |
