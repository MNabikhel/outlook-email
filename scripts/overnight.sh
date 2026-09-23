#!/bin/bash
# For cron / launchd: runs from the project folder no matter where it starts.
#   30 6 * * 1-5  /path/to/outlook-email/scripts/overnight.sh
cd "$(dirname "$0")/.." || exit 1
mkdir -p data/overnight
.venv/bin/python -m controller_inbox overnight >> data/overnight/scheduler.log 2>&1
