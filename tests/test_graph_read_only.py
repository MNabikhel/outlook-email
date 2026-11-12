"""The Microsoft 365 connection (graph.py) only reads: it asks to sign in and read mail, and sends GET requests only."""

from __future__ import annotations

import ast
from pathlib import Path

from controller_inbox import graph

SOURCE = Path(graph.__file__)


def test_it_asks_microsoft_only_to_read_mail():
    assert graph.DELEGATED_SCOPES == ["User.Read", "Mail.Read"]


def test_every_request_it_makes_is_a_get():
    # The one way it reaches Microsoft Graph is GraphClient.get, which sends a GET: no other HTTP call is in the file.
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    calls = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert not calls & {"request", "post", "patch", "put", "delete", "send", "stream"}, calls & {"request", "post", "patch", "put", "delete", "send", "stream"}
    assert not hasattr(graph.GraphMailbox, "apply_categories") and not hasattr(graph.GraphMailbox, "send_mail")
