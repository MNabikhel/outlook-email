#!/usr/bin/env bash
# One-shot "morning brief": pull the latest mail, (re)build the daily digest,
# and export the open action list to CSV. Good for cron or a login hook.
#
#   */15 * * * *  cd /path/to/closedesk && scripts/daily-brief.sh >> data/daily.log 2>&1
#
# Uses Microsoft Graph when configured (CONTROLLER_INBOX / AZURE_CLIENT_ID set),
# otherwise refreshes the built-in demo mailbox so the flow still works offline.
set -euo pipefail

PY="${PYTHON:-python}"
OUT_DIR="${CONTROLLER_INBOX_DATA_DIR:-./data}"
mkdir -p "$OUT_DIR"

if "$PY" -m controller_inbox status --json | grep -q '"graph_configured": true'; then
  echo "==> Syncing Outlook via Microsoft Graph"
  "$PY" -m controller_inbox sync
else
  echo "==> Graph not configured; refreshing demo mailbox"
  "$PY" -m controller_inbox demo
fi

echo "==> Building daily digest"
"$PY" -m controller_inbox digest

echo "==> Exporting open actions to ${OUT_DIR}/actions.csv"
"$PY" -m controller_inbox export --output "${OUT_DIR}/actions.csv"

echo "==> Triage snapshot"
"$PY" -m controller_inbox triage
