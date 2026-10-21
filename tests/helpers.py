"""Small helpers shared by the test modules (fixtures live in conftest.py)."""

from __future__ import annotations

import json

# The header CloseDesk's own pages send with every request; the JSON API refuses requests without it.
PAGE = {"X-CloseDesk": "1"}


def ndjson_events(response) -> list[dict]:
    """The events of a streamed answer (one JSON object per line), in order."""
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]
