"""Regression tests for the sweep fixes outside the fraud rules: Graph sign-in, hidden HTML text, task due dates."""

from __future__ import annotations

import time
from datetime import date

from controller_inbox.actions import extract_actions
from controller_inbox.extract import extract_fields, html_to_text
from controller_inbox.fraud import TrustContext, assess
from controller_inbox.models import DocumentType, Importance


# 3. MSAL refuses a device sign-in that names one of its own reserved scopes.
def test_graph_device_sign_in_asks_only_for_scopes_msal_accepts(monkeypatch, tmp_path):
    import msal

    from controller_inbox import graph

    assert not set(graph.DELEGATED_SCOPES) & {"openid", "profile", "offline_access"}
    decorate = getattr(msal.ClientApplication, "_decorate_scope", None)
    seen = {}

    class App:
        _exclude_scopes = frozenset()

        def __init__(self, *args, **kwargs):
            pass

        def get_accounts(self):
            return []

        def initiate_device_flow(self, scopes):
            # MSAL's own check, which raised ValueError for "offline_access" before any network call.
            seen["scopes"] = decorate(self, scopes) if decorate else scopes
            return {"user_code": "ABC", "message": "Sign in at https://microsoft.com/devicelogin"}

        def acquire_token_by_device_flow(self, flow):
            return {"access_token": "token"}

    monkeypatch.setattr(graph.msal, "PublicClientApplication", App)
    client = graph.GraphClient(client_id="app-id", cache_path=tmp_path / "token-cache.json")
    assert client.acquire_token(device_code_printer=lambda _message: None) == "token"
    assert "Mail.Read" in seen["scopes"]


# 7. Text the reader is never shown does not split the words the fraud check reads.
def test_hidden_html_text_does_not_hide_a_bank_change():
    for html in (
        '<p>Our bank <span style="display:none">zz</span>details have changed.</p>',
        '<p>Our bank d<span style="font-size:0px">q</span>etails have changed.</p>',
        '<p>Our ba<font style="visibility: hidden"><b>x</b>y</font>nk details have changed.</p>',
        '<p>Our bank <span style="mso-hide:all">zz</span>details have changed.</p>',
    ):
        text = html_to_text(html)
        assert text == "Our bank details have changed.", html
        check = assess(TrustContext(), subject="Invoice", body=text, sender_name="AR", sender_email="ar@vendor.example")
        assert check.level == "high", html


def test_visible_html_text_is_kept():
    shown = html_to_text('<p style="font-size:10px">Invoice 5521</p><p style="font-size:0.9em">is attached.</p>')
    assert " ".join(shown.split()) == "Invoice 5521 is attached."
    # A hidden element never closed hides only its own tag, and a long run of them is read quickly.
    assert html_to_text('<span style="display:none">Hi there') == "Hi there"
    start = time.process_time()
    html_to_text('<span style="display:none">x' * 20_000)
    assert time.process_time() - start < 2


# 9. "Oct." in a date does not end the sentence, so each task keeps its own due date.
def test_abbreviated_month_keeps_each_tasks_own_due_date():
    body = "Please approve the PO by Oct. 9, 2026. Please send the signed W-9 by Oct. 30, 2026."
    items = extract_actions(
        email_id="e", subject="PO 7781", body=body, category=DocumentType.OTHER, importance=Importance.MEDIUM,
        fields=extract_fields(body, as_of=date(2026, 10, 8)), flags=[], as_of=date(2026, 10, 8),
    )
    due = {item.title: item.due_date for item in items}
    assert due == {"Please approve the PO by Oct. 9, 2026.": "2026-10-09", "Please send the signed W-9 by Oct. 30, 2026.": "2026-10-30"}
