"""Chat answers are checked against what the model read: wrong arithmetic and citations are corrected, made-up figures flagged."""

from __future__ import annotations

from controller_inbox.answer_check import review

BUDGET = "\n".join(
    [
        'Workbook with 2 sheets: "Summary", "Detail"',
        "",
        '[sheet "Summary" A1:E4]',
        "A1: Department | B1: Q3 actual | C1: Q4 budget | D1: Change | E1: Change %",
        "A2: Marketing | B2: 84,000 (=SUMIFS(Detail!C:C)) | C2: 115,500 | D2: 31,500 (=C2-B2) | E2: 0.375 (=D2/B2)",
        "A3: Sales | B3: 107,600 | C3: 114,600 | D3: 7,000 (=C3-B3) | E3: 0.0651 (=D3/B3)",
        "A4: Total | B4: 191,600 | C4: 230,100 | D4: 38,500",
        "",
        '[sheet "Detail" A1:C3]',
        "A1: Line | B1: Team | C1: Q3",
        "A2: Events | B2: Marketing | C2: 31,000",
    ]
)
AUDIT = "\n\n".join(
    f"[page {n}]\nSection {n}.1: The team reviewed supplier onboarding and noted no exceptions."
    + ("\nFINDING 4 (HIGH): 3 vendor bank-detail changes in July were approved without a call-back." if n == 17 else "")
    for n in range(1, 25)
)
FILES = [("Q4 budget.xlsx", BUDGET), ("FY26 Audit.pdf", AUDIT)]


def _review(answer: str):
    return review(answer, material=["Please review the Q4 budget before Friday."], files=FILES)


def test_worked_out_figures_are_recomputed_from_the_figures_beside_them():
    result = _review("Marketing went up the most, from 84,000 to 115,500, an increase of 31,200 [1].")
    assert result.text == "Marketing went up the most, from 84,000 to 115,500, an increase of 31,500 [1]."
    assert result.checks == ["Corrected 31,200 to 31,500 (115,500 − 84,000)."]
    # A figure that is in the files (31,000 is Detail!C2) is never rewritten, even beside "increase of".
    grounded = _review("Marketing went up the most, from 84,000 to 115,500, an increase of 31,000 [1].")
    assert grounded.text.endswith("an increase of 31,000 [1].")

    percent = _review("Marketing rose 36% (Summary!E2), from 84,000 to 115,500.")
    assert percent.text.startswith("Marketing rose 37.5% ")


def test_right_answers_are_left_alone():
    for answer in (
        "Marketing rose 37.5% (Q4 budget.xlsx, Summary!E2); Sales rose 6.5%.",
        "Q3 was 191,600 and Q4 is 230,100, a total increase of 38,500.",
        "1. Marketing: 115,500\n10. Sales: 114,600",
        "FINDING 4 is on page 17 of FY26 Audit.pdf; it is due 30 November 2026 [1].",
        "The bank rec is due 2026-09-28, a week after the Q4 budget review.",
    ):
        result = _review(answer)
        assert (result.text, result.checks) == (answer, []), answer


def test_figures_that_are_nowhere_in_what_was_read_are_flagged():
    result = _review("The Q4 budget is 230,100. Travel will cost $97,250.")
    assert result.text == "The Q4 budget is 230,100. Travel will cost $97,250."
    assert result.checks == ["$97,250 isn't in the emails or files I read; check it before relying on it."]


def test_a_total_under_a_list_is_the_sum_of_the_amounts_listed():
    material = ["Invoice INV-10482 amount due $12,850.00", "Amount due $1,980.00", "the outstanding $48,500.00 wire"]
    listed = (
        "Due this week:\n\n* **INV-10482:** $12,850.00 due 2026-10-05 [2]\n* **Harbor Packaging:** $1,980.00 [6]\n"
        "* **Apex Vendor (wire):** $48,500.00 [1]\n\n**Total Due:** {total}\n\nVerify the wire by phone first."
    )
    wrong = review(listed.format(total="$73,330.00"), material=material, files=[])
    assert wrong.text == listed.format(total="$63,330.00")
    assert wrong.checks == ["Corrected $73,330.00 to $63,330.00 (the sum of the 3 amounts listed above it)."]
    # The right sum is not flagged for being nowhere in the mail, and neither is the sum of some of the items.
    for total in ("$63,330.00", "$14,830.00"):
        assert review(listed.format(total=total), material=material, files=[]).checks == []
    # Without a list above it, a total stays a figure to check.
    alone = review("Total due: $73,330.00", material=material, files=[])
    assert alone.text == "Total due: $73,330.00" and alone.checks[0].startswith("$73,330.00 isn't in")


def test_a_cited_page_that_is_not_in_the_file_is_corrected():
    roster = "[page 1]\nEmployee: Jonathan Alvarez | Department: not listed | Manager: Priya Raman | Salary: $112,000"
    result = review(
        "Jonathan Alvarez's salary is $112,000 (roster.pdf, page 2).",
        material=[],
        files=[("roster.pdf", roster)],
    )
    assert "page 1" in result.text and "page 2" not in result.text
    assert result.checks == ["Corrected the page for roster.pdf: that is on page 1, not page 2."]


def test_a_wrong_page_is_corrected_when_the_finding_is_on_one_other_page():
    result = _review("FY26 Audit.pdf page 12 says 3 vendor bank-detail changes in July were approved without a call-back.")
    assert "page 17 says" in result.text
    assert result.checks == ["Corrected the page for FY26 Audit.pdf: that is on page 17, not page 12."]


def test_a_wrong_cell_is_corrected_when_one_cell_holds_the_figure():
    result = _review("Marketing's change of 31,500 is in Q4 budget.xlsx cell D3.")
    assert result.text == "Marketing's change of 31,500 is in Q4 budget.xlsx cell D2."
    assert result.checks == ["Corrected the cell for 31,500 in Q4 budget.xlsx: it is in Summary!D2, not D3."]
    ambiguous = _review("Q4 budget.xlsx shows 31,000 in Summary!B2.")
    assert ambiguous.text.endswith("Summary!B2."), "31,000 is only on the Detail sheet, so the Summary cell isn't swapped"


def test_a_figure_cited_to_the_wrong_file_is_pointed_out():
    answer = "[1] · page 2 · Section 2.25: Marketing's Q4 budget is 115,500."
    result = _review(answer)
    assert result.text == answer
    assert result.checks == ["115,500 is in Q4 budget.xlsx, not FY26 Audit.pdf: check where that figure comes from."]
    assert _review("[1] · page 17: 3 vendor bank-detail changes were approved.").checks == []
