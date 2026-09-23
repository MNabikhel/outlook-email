#!/bin/bash
# Double-click (macOS) or run ./CloseDesk.command (Linux): read the drop folder, let the local
# model read, write today's digest, and open the dashboard. The first run sets everything up.
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
  echo "First run: setting up CloseDesk. This takes a minute or two."
  PY="$(command -v python3 || command -v python || true)"
  if [ -z "$PY" ]; then
    echo "Python 3.11 or newer is needed: https://www.python.org/downloads/"
    read -r -p "Press Enter to close. " _
    exit 1
  fi
  if ! { "$PY" -m venv .venv && .venv/bin/python -m pip install --quiet --upgrade pip && .venv/bin/python -m pip install --quiet -e .; }; then
    echo "Setup failed. The message above says why."
    read -r -p "Press Enter to close. " _
    exit 1
  fi
  echo "Setup finished."
fi

exec .venv/bin/python -m controller_inbox run "$@"
