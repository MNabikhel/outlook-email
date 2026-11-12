"""Quarter, half-year and period codes ("Q3", "Q4", "H1", "P1") are searched for like any word: in the chat, by the
model's tools and in a file. A query with no word to search for says so, rather than that no file mentions it."""

from __future__ import annotations

from controller_inbox import agent, assistant, documents


def test_a_short_code_with_a_figure_is_a_search_word():
    assert documents.terms("Anything about Q4?", stop=assistant._STOP) == ["q4"]
    assert documents.terms("H1 and P1 numbers") == ["h1", "and", "p1", "number"]
    assert documents.terms("AP is up") == [], "two letters without a figure are still no word"


def test_q3_and_q4_mail_is_found(loaded, settings):
    events = list(assistant.answer_stream(loaded, settings, "Which email mentions Q3?", today="2026-09-22"))
    assert "demo-pbc" in [card["id"] for e in events if e["type"] == "sources" for card in e["sources"]]
    ws = agent.Workspace(loaded, settings, [loaded.get_email("demo-question"), loaded.get_email("demo-meeting")], question="q")
    assert "Quick question on the Q4 headcount numbers" in agent.run_tool(ws, "search_mail", {"query": "Q4"}, limit=2000)
    assert "None of the files mention" not in agent.run_tool(ws, "find_in_file", {"email": 2, "query": "Q4"}, limit=2000)


def test_a_query_with_no_word_to_search_for_says_so(loaded, settings):
    ws = agent.Workspace(loaded, settings, [loaded.get_email("demo-meeting")], question="q")
    said = agent.run_tool(ws, "find_in_file", {"email": 1, "query": "AP"}, limit=2000)
    assert "None of the files mention" not in said and "too short" in said, said
