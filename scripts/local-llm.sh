#!/usr/bin/env bash
# Run CloseDesk against a local LLM (LM Studio / Ollama / Bionic).
#
# 1. Start your local server and load a model:
#      - LM Studio: enable the local server (default http://localhost:1234/v1)
#      - Ollama:    `ollama serve` then set CONTROLLER_INBOX_LLM_BASE_URL=http://localhost:11434/v1
# 2. Run this script. It checks connectivity, loads the demo mailbox with
#    AI enrichment, and opens the dashboard.
set -euo pipefail

export CONTROLLER_INBOX_LLM="${CONTROLLER_INBOX_LLM:-true}"
export CONTROLLER_INBOX_LLM_BASE_URL="${CONTROLLER_INBOX_LLM_BASE_URL:-http://localhost:1234/v1}"
export CONTROLLER_INBOX_LLM_MODEL="${CONTROLLER_INBOX_LLM_MODEL:-local-model}"

PY="${PYTHON:-python}"

echo "==> Checking local LLM at ${CONTROLLER_INBOX_LLM_BASE_URL}"
"$PY" -m controller_inbox llm-check || {
  echo "Local model server not reachable — continuing with rules-only fallback." >&2
}

echo "==> Loading demo mailbox with enrichment and starting the dashboard"
exec "$PY" -m controller_inbox demo --serve
