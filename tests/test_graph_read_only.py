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


def test_a_sign_in_saved_by_a_version_that_could_write_is_dropped(tmp_path, monkeypatch):
    from controller_inbox import graph
    from controller_inbox.graph import GraphClient

    monkeypatch.setattr(graph.msal, "PublicClientApplication", lambda *_a, **_k: object())  # no network

    cache = tmp_path / "msal_cache.json"
    cache.write_text('{"AccessToken": {"k": {"target": "Mail.ReadWrite Mail.Send User.Read", "secret": "x"}}}', encoding="utf-8")
    GraphClient(client_id="00000000-0000-0000-0000-000000000000", cache_path=cache)
    assert not cache.exists()
    cache.write_text('{"AccessToken": {"k": {"target": "Mail.Read User.Read", "secret": "x"}}}', encoding="utf-8")
    GraphClient(client_id="00000000-0000-0000-0000-000000000000", cache_path=cache)
    assert cache.exists()
