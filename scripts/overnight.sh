#!/bin/bash
# For cron / launchd: runs from the project folder no matter where it starts.
#   30 6 * * 1-5  /path/to/outlook-email/scripts/overnight.sh
# It goes through the launcher, so a run after an update first installs what the update needs.
cd "$(dirname "$0")/.." || exit 1
mkdir -p data/overnight
bash ./CloseDesk.command --command overnight >> data/overnight/scheduler.log 2>&1 < /dev/null
