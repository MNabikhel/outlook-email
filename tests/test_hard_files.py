"""The hard-file questions (tests/hard_files): is each answer within the model's reach, inside a 16k context?

``baseline.json`` records which questions are answered within reach today. A question that was in reach must stay
so; one that comes into reach fails too, until the baseline is updated, so an improvement is recorded and can't
quietly slip back. ``CLOSEDESK_EVAL_REPORT=path`` writes what each question got, for looking into a miss.

``pytest -m live_model`` asks a real model the same questions instead (set ``CLOSEDESK_EVAL_URL`` to its server,
for example http://127.0.0.1:1234/v1, ``CLOSEDESK_EVAL_MODEL`` to the model's name if the server has several, and
``CLOSEDESK_EVAL_CONTEXT`` to its context length when it doesn't report it; 16384 by default).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from hard_files.harness import FILES, case_id, cases, evidence, found, live, mail_with, resolved

BASELINE = Path(__file__).parent / "hard_files" / "baseline.json"
CASES = cases()


def _baseline() -> dict[str, bool]:
    return json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}


def _needs(case: dict) -> None:
    from controller_inbox import ocr

    if case.get("needs") == "ocr" and not ocr.engine_name():
        pytest.skip("a scanned file: needs the OCR extra (RapidOCR or Tesseract)")


def _report(case: dict, result: dict, reach: bool) -> None:
    path = os.environ.get("CLOSEDESK_EVAL_REPORT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"id": case_id(case), "reach": reach, "held_out": case["held_out"], "answers": case["answers"],
                                 "steps": [e["text"] for e in result["events"] if e["type"] == "step"],
                                 "answer": result["answer"][-1500:], "first": result["first"][-6000:]}) + "\n")


@pytest.mark.parametrize("case", CASES, ids=[case_id(c) for c in CASES])
def test_the_answer_is_within_the_models_reach(case, store, settings, monkeypatch):
    _needs(case)
    email = mail_with(store, settings, case["file"])
    result = evidence(store, settings, email, case["question"], monkeypatch)
    reach = any(found(prompt, case["answers"]) for prompt in result["prompts"]) or found(result["answer"], case["answers"])
    _report(case, result, reach)
    was = _baseline().get(case_id(case), False)
    if was:
        assert reach, f"{case['question']!r}: the answer ({case['answers'][0]}) no longer reaches the model"
    else:
        assert not reach, f"{case['question']!r} is now answered within reach: set it to true in {BASELINE.name}"


SQL_CASES = [c for c in CASES if c["sql"]]


@pytest.mark.parametrize("case", SQL_CASES, ids=[case_id(c) for c in SQL_CASES])
def test_the_tables_hold_the_answer(case):
    # Whatever a model writes, the query it should write gives the answer: the file's tables were read right.
    sql, tables = resolved(case["sql"], case["file"])
    _names, rows, _more = tables.run(sql)
    assert rows and found(" ".join(f"{value:,.2f}" if isinstance(value, float) else str(value) for row in rows for value in row), case["answers"]), rows


@pytest.mark.parametrize("case", SQL_CASES, ids=[case_id(c) for c in SQL_CASES])
def test_the_right_query_s_result_reaches_the_answer(case, store, settings, monkeypatch):
    # The model wrote the right query: its result is what the answer is written from (or is the answer).
    email = mail_with(store, settings, case["file"])
    sql, _tables = resolved(case["sql"], case["file"])
    result = evidence(store, settings, email, case["question"], monkeypatch, sql=sql)
    steps = [e["text"] for e in result["events"] if e["type"] == "step"]
    # With no query asked for (the file fits whole), the file itself is what the answer is written from.
    later = (result["prompts"][1:] if any(step.startswith("Writing a query") for step in steps) else result["prompts"]) + [result["answer"]]
    assert any(found(text, case["answers"]) for text in later), steps


def test_every_file_in_the_cases_is_there():
    assert {c["file"] for c in CASES} <= {p.name for p in FILES.iterdir()}
    assert sum(c["held_out"] for c in CASES) >= len(CASES) // 3, "keep about a third of the questions held out"


@pytest.mark.live_model
@pytest.mark.parametrize("case", CASES, ids=[case_id(c) for c in CASES])
def test_a_real_model_answers(case, store, settings):
    url = os.environ.get("CLOSEDESK_EVAL_URL")
    if not url:
        pytest.skip("set CLOSEDESK_EVAL_URL to an OpenAI-compatible server to ask a real model")
    _needs(case)
    settings.llm = True
    settings.llm_base_url = url
    if model := os.environ.get("CLOSEDESK_EVAL_MODEL"):
        settings.llm_model = model
    settings.llm_timeout = float(os.environ.get("CLOSEDESK_EVAL_TIMEOUT", "900"))
    # The context the server was started with, for a server that doesn't say (llama-cpp-python): else CloseDesk
    # assumes a short one, as it would in use, and reads less of each file than the server could take.
    settings.chat_context_tokens = int(os.environ.get("CLOSEDESK_EVAL_CONTEXT", "16384"))
    email = mail_with(store, settings, case["file"])
    events = live(store, settings, email, case["question"])
    answer = "".join(e["text"] for e in events if e["type"] == "delta")
    path = os.environ.get("CLOSEDESK_EVAL_REPORT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            steps = [e["text"] for e in events if e["type"] == "step"]
            handle.write(json.dumps({"id": case_id(case), "right": found(answer, case["answers"]), "answers": case["answers"], "answer": answer, "steps": steps}) + "\n")
    assert found(answer, case["answers"]), f"{case['question']!r}: wanted {case['answers'][0]}, got: {answer[:400]}"
