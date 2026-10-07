"""Regression tests for the second fraud-rules review: footers, negations, gift cards, look-alikes, names,
evidence, the digest count and long crafted emails."""

from __future__ import annotations

import time

from controller_inbox.classify import classify_document, normalize_text
from controller_inbox.extract import html_to_text
from controller_inbox.fraud import TrustContext, assess


def _check(body, *, subject="Invoice 5521", sender="ar@vendor.example", history=5, ctx=None, **extra):
    return assess(ctx or TrustContext(), subject=subject, body=body, sender_name="Vendor AR", sender_email=sender,
                  history=history, **extra)


def _keys(check):
    return {signal.key for signal in check.signals}


def _cpu_seconds(work) -> float:
    start = time.process_time()
    work()
    return time.process_time() - start


# Long crafted emails are read in one pass: each of these took from several seconds to minutes.
CRAFTED_200KB = {
    "unclosed comments": "<!--" * 50_000,
    "repeated warnings": "if you receive " * 13_333,
    "gift card mentions": "gift card " * 20_000,
    "gift card program mentions": "gift card program " * 11_111,
}


def test_long_crafted_bodies_are_read_quickly():
    for name, body in CRAFTED_200KB.items():
        spent = _cpu_seconds(lambda: _check(body, attachments=[("a.txt", body)]))
        assert spent < 2.5, f"{name}: {spent:.1f}s"
        assert _cpu_seconds(lambda: classify_document(subject="Hello", body=body, extracted_text=body)) < 2.5, name


def test_long_crafted_html_is_read_quickly():
    for body in ("<script" * 30_000, "<style>" * 28_000, "<!--" * 50_000, "<" * 200_000):
        assert _cpu_seconds(lambda: html_to_text(body)) < 1.0, body[:8]


def test_comments_are_still_dropped():
    assert normalize_text("Our ba<!-- x -->nk details") == "Our bank details"
    assert normalize_text("a <!-- never closed") == "a <!-- never closed"
    assert html_to_text("<p>x<script>var a = '<b>';</script>y<style>p {}</style>z</p>") == "x y z"
