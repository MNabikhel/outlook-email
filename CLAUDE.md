# CloseDesk

This codebase is yours: what it's for and how it's built are your call, and you'll keep building on it.

## What it is

CloseDesk sorts an Outlook inbox into one short page each morning, on the user's own laptop: what needs them,
what is worth knowing, what is filed. It reads attachments, flags payment fraud, suggests AP cost codes, writes a
daily digest, and answers questions about the mail. A local model (LM Studio) is optional; nothing leaves the
computer. The people using it are accountants, not developers: they double-click a launcher and never see a
terminal, so set-up, updates and error messages have to work without one.

## Layout

- `src/controller_inbox/`: the app (FastAPI, SQLite, plain-JS pages with no build step). `README.md` has a line
  on every module under "Project layout".
- `CloseDesk.command` / `CloseDesk.bat`: the double-click launchers. They find the Python on the computer
  (a prerequisite: none is ever downloaded unasked, and with none they say so) and run `scripts/launch.py`,
  which checks the set-up on every start, installs what is missing, and starts the app. `--check` runs the
  self-check (`doctor.py`), `--update` fetches the newest version, and `--get-python` downloads a Python for
  CloseDesk alone into the user's folder, for someone without the rights to install one.
- `tests/`: pytest. `tests/launcher_smoke.py` runs the real launchers end to end (CI does, on macOS, Windows
  and Linux).
- `docs/SETUP.md`: the guide users read.

## Working on it

```bash
pip install -e ".[dev,ocr,tables]"     # or: uv pip install ...
pytest -m "not browser"                 # the main suite (about 7 minutes)
pytest -m browser                       # the pages in a real Chromium (needs the browser extra)
ruff check src tests scripts
python tests/launcher_smoke.py          # after touching the launchers or scripts/launch.py
```

- A bug gets a test that fails first; behaviour changes are stated in the PR.
- Python 3.11 is the oldest supported (CI runs 3.11 to 3.14); keep syntax 3.11-compatible.
- Classic Outlook is read only: `outlook_desktop.py` never saves, moves, deletes or sends, and a test fails if
  such a call is added.
- Mail held as possible payment fraud never reaches the model, the previews or the downloads.
- Comments and user-facing text are plain words about what happens, in the style already there.
- Changes go to `main` through pull requests, which the owner merges.
