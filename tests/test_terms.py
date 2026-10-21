"""One reading of a question's words: picking emails (assistant.keywords), searching passages (documents.search_parts,
agent) and matching past answers (chats) agree on what a word is."""

from __future__ import annotations

import pytest

from controller_inbox import agent, chats, documents
from controller_inbox.assistant import keywords
from controller_inbox.documents import STOP_WORDS, search_parts, singular, terms


@pytest.mark.parametrize(
    "text, words",
    [
        ("Invoices from Northwind", ["invoice", "northwind"]),
        ("the business address", ["business", "address"]),
        ("Q4s and 1099s", ["q4s", "1099s"]),
        # A name that ends "s" folds too, the same way on every side: "Reyes's" and "Reyes" are one word.
        ("Reyes's invoice from Reyes", ["reye", "invoice"]),
        ("What's due? Don't pay", ["due", "don", "pay"]),
        ("O'Brien Supply", ["o'brien", "supply"]),
        ("ap@vendor.com sent INV-2024-0457", ["ap@vendor.com", "sent", "inv-2024-0457"]),
        ("F-150 at 12.5% for AT&T", ["f-150", "12.5%", "at&t"]),
        ("$4,210.00 owed", ["4", "210.00", "owed"]),
        ("Is it 7 or 42?", ["7", "42"]),
        ("invoice invoices INVOICE", ["invoice"]),
        ("", []),
    ],
)
def test_terms(text, words):
    assert terms(text, stop=STOP_WORDS) == words


def test_terms_without_folding_plurals():
    assert terms("Invoices and payments", stop=STOP_WORDS, fold_plurals=False) == ["invoices", "payments"]


@pytest.mark.parametrize("word, folded", [("invoices", "invoice"), ("class", "class"), ("bus", "bus"), ("q4s", "q4s"), ("costs", "cost")])
def test_singular(word, folded):
    assert singular(word) == folded


def test_mail_search_passages_and_past_answers_read_a_question_alike():
    question = "Which of Reyes's invoices list the business address?"
    picked = set(keywords(question))
    assert {"reye", "invoice", "business", "address"} <= picked
    assert {"reye", "invoice", "business", "address"} <= chats._words(question)
    assert {"reye", "invoice", "business", "address"} <= set(documents.terms(question, stop=agent._STOP))


def test_a_plural_in_the_question_finds_the_passage_with_the_word():
    # "invoices" asked finds the section that says "invoice", as the mail search does.
    text = "[page 1]\nPayroll for October.\n\n[page 2]\nInvoice INV-77 from Globex.\n"
    assert [part.label for part in search_parts(text, "Globex invoices")] == ["page 2"]
    assert [part.label for part in search_parts(text, "invoices")] == ["page 2"]


def test_a_word_ending_ss_is_not_folded_for_past_answers():
    # "business" and "busines" used to be one word for past answers but not for the mail search.
    assert chats._words("the business address") == {"business", "address"}
