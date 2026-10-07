"""Prompt sizes as a model's tokenizer counts them: a schedule's digits cost far more than their length."""

from __future__ import annotations

from controller_inbox import agent


def test_figures_cost_more_than_words_of_the_same_length():
    words = "The offsite moves to Lisbon in November and the venue deposit is confirmed. " * 10
    figures = "Pmt #: 24 | Payment: 48,623.15 | Interest: 8,502.49 | Principal: 40,120.66 |" * 10
    assert len(figures) >= len(words)
    assert agent.prompt_size(figures) > 1.8 * agent.prompt_size(words)
    # About three budget characters a token: plain English runs about four characters a token.
    assert 0.6 * len(words) < agent.prompt_size(words) < 0.95 * len(words)


def test_a_cut_always_fits_its_room():
    text = "a short line of words\n" * 5 + "1,234,567.89 | 9,876,543.21 | 5,555,555.55\n" * 50
    for room in (40, 200, 900, 2500):
        cut = agent.clip(text, room)
        assert agent.prompt_size(cut) <= room and cut.endswith("…")
    assert agent.clip("short", 500) == "short"


def test_no_room_leaves_only_the_mark():
    text = "1,234,567.89 | 9,876,543.21\n" * 400
    assert agent.clip(text, 0) == "…"
    assert agent.clip(text, 3) == "…"


def test_the_tool_definitions_are_counted_in_the_budget():
    assert 1000 < agent.TOOL_SCHEMA_TOKENS < 2000
    assert agent.prompt_budget(16384, 500, tools=True) < agent.prompt_budget(16384, 500, tools=False)
