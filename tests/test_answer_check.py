"""Chat answers are checked against what the model read: wrong arithmetic and citations are corrected, made-up figures flagged."""

from __future__ import annotations

from controller_inbox.answer_check import Grounding, check_citations, check_numbers, numbers_in, review

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


def test_a_right_total_under_a_list_is_not_rewritten_to_another_sum():
    # Two figures a line: the total adds up the second column (Q4), not the first (Q3).
    workbook = '[sheet "Budget" A1:D4]\nA2 (Line): Ads | B2 (Q3): 1,000 | C2 (Q4): 1,500\nA3 (Line): Travel | B3 (Q3): 250 | C3 (Q4): 300'
    by_line = "Q3 to Q4 by line [1]:\n- Ads: $1,000 → $1,500\n- Travel: $250 → $300\nTotal: $1,800"
    right = review(by_line, material=[], files=[("budget.xlsx", workbook)])
    assert (right.text, right.checks) == (by_line, [])
    # It isn't clear which column a wrong total meant to add up: it is flagged, not rewritten.
    slip = review(by_line.replace("$1,800", "$1,900"), material=[], files=[("budget.xlsx", workbook)])
    assert slip.text.endswith("Total: $1,900") and slip.checks == ["$1,900 isn't in the emails or files I read; check it before relying on it."]
    # A total of more than the lines listed (the top three of four vendors) is flagged, not rewritten to their sum.
    ap = "Vendor: Harbor Steel | Balance: $22,150.00\nVendor: Ajax | Balance: $15,000.00\nVendor: Brio | Balance: $9,000.00\nVendor: Cole | Balance: $4,000.00"
    top = "Largest balances:\n- Harbor Steel: $22,150.00\n- Ajax: $15,000.00\n- Brio: $9,000.00\nTotal owed to all four vendors: $50,150.00"
    result = review(top, material=[], files=[("ap.xlsx", ap)])
    assert result.text == top and not any(check.startswith("Corrected") for check in result.checks)


def test_a_cited_page_that_is_not_in_the_file_is_corrected():
    roster = "[page 1]\nEmployee: Jonathan Alvarez | Department: not listed | Manager: Priya Raman | Salary: $112,000"
    result = review(
        "Jonathan Alvarez's salary is $112,000 (roster.pdf, page 2).",
        material=[],
        files=[("roster.pdf", roster)],
    )
    assert "page 1" in result.text and "page 2" not in result.text
    assert result.checks == ["Corrected the page for roster.pdf: that is on page 1, not page 2."]


def test_a_page_of_a_file_without_pages_is_not_corrected_to_a_page_of_another_file():
    contract = "# Master services agreement\nSection 4. Either party may cancel with 90 days notice before renewal. Fees are $12,500 a year."
    quote = "[page 1]\nAcme quote Q-881\n[page 2]\nSupport plan $9,600.00\n[page 3]\nTerms: payment due in 90 days. Annual fee $12,500 for the renewal and cancel notice."
    answer = "You can cancel with 90 days notice before renewal, and the fee is $12,500 a year (contract.docx, page 2) [1]."
    result = review(answer, material=[], files=[("contract.docx", contract), ("quote.pdf", quote)])
    assert (result.text, result.checks) == (answer, [])
    # A sentence that names no file is still about the only file with pages.
    unnamed = review("The annual fee of $12,500 for the renewal is on page 2.", material=[], files=[("contract.docx", contract), ("quote.pdf", quote)])
    assert unnamed.text == "The annual fee of $12,500 for the renewal is on page 3."


def test_a_wrong_page_is_corrected_when_the_finding_is_on_one_other_page():
    result = _review("FY26 Audit.pdf page 12 says 3 vendor bank-detail changes in July were approved without a call-back.")
    assert "page 17 says" in result.text
    assert result.checks == ["Corrected the page for FY26 Audit.pdf: that is on page 17, not page 12."]


def test_a_number_in_the_files_name_doesnt_move_a_right_page():
    # The invoice number is in the file's name and printed on page 1; the row cited is on page 2.
    invoice = (
        "[page 1]\nInvoice No. 58213 | Due date: October 28, 2026\n#: 36 | Description: Lens paper | Qty: 6 | Amount: 719.16\n"
        "[page 2]\n#: 37 | Description: Kimwipes, small (box 280) | Qty: 10 | Amount: 2,046.80"
    )
    answer = "Ten boxes of Kimwipes were ordered, for $2,046.80. That is row 37 on page 2 of Ridgeview Invoice 58213.pdf."
    assert review(answer, material=[], files=[("Ridgeview Invoice 58213.pdf", invoice)]).checks == []
    wrong = review("The lens paper line comes to $719.16 (Ridgeview Invoice 58213.pdf, page 2).", material=[], files=[("Ridgeview Invoice 58213.pdf", invoice)])
    assert wrong.checks == ["Corrected the page for Ridgeview Invoice 58213.pdf: that is on page 1, not page 2."]


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


def test_right_answers_from_the_review_are_left_alone():
    def checked(answer, material):
        return review(answer, material=material, files=[])

    # A fall written as a positive percentage is the change over the earlier figure.
    fall = checked("Services revenue fell 10% from $200,000 to $180,000.", ["Services revenue: Q2 $200,000; Q3 $180,000"])
    assert (fall.text, fall.checks) == ("Services revenue fell 10% from $200,000 to $180,000.", [])
    # A total of three figures is not "corrected" to the sum of two of them.
    three = checked("The three open invoices total $5,800 ($500 + $2,500 + $2,800).", ["INV-1 $500.00", "INV-2 $2,500.00", "INV-3 $2,800.00"])
    assert (three.text, three.checks) == ("The three open invoices total $5,800 ($500 + $2,500 + $2,800).", [])
    # A credit in a list takes away from its total.
    credit = "Open items:\n- INV-1001: $1,000.00\n- INV-1002: $2,000.00\n- CM-2291 (credit): -$300.00\n\nTotal: $2,700.00"
    assert checked(credit, ["INV-1001 1,000.00", "INV-1002 2,000.00", "CM-2291 (300.00)"]).text == credit
    # Only the first figure on a "Total" line is the list's total.
    listed = "Due:\n* INV-10482: $12,850.00\n* Harbor: $1,980.00\n* Apex: $48,500.00\n\nTotal: $63,330.00, leaving $36,670.00 of the $100,000.00 budget."
    left = checked(listed, ["$12,850.00", "$1,980.00", "$48,500.00", "budget $100,000.00"])
    assert left.text == listed and not any(check.startswith("Corrected") for check in left.checks)


def test_a_page_range_is_not_corrected_to_one_page():
    loan = (
        "[page 1]\nPmt #: 23 | Ending Balance: 1,612,004.10\n\n[page 2]\nPmt #: 24 | Ending Balance: 1,571,781.00\n\n"
        "[page 3]\nPmt #: 37 | Ending Balance: 1,051,370.72\n"
    )
    answer = "The schedule runs from payment 23 to payment 24 on pages 1-2 of Loan.pdf, ending at 1,571,781.00."
    assert review(answer, material=[], files=[("Loan.pdf", loan)]).text == answer


def test_a_dash_between_a_name_and_its_amount_is_not_a_minus_sign():
    answer = "Open invoices:\n- Harbor Steel LLC - $48,500.00\n- Acme Industrial Supply - $24,310.50\n- Orion Software - $18,600.00\n\nTotal: $91,410.50"
    material = ["Harbor Steel LLC $48,500.00", "Acme Industrial Supply $24,310.50", "Orion Software $18,600.00"]
    result = review(answer, material=material, files=[])
    assert (result.text, result.checks) == (answer, [])


def test_a_files_name_is_left_out_only_where_it_stands_alone():
    from controller_inbox.answer_check import _without_names

    assert _without_names("The total in 100.pdf is $4,100.00 on page 2.", ["100.pdf"]) == "The total in         is $4,100.00 on page 2."
    assert "4,100.50" in _without_names("Statement 100 shows 4,100.50 and 100.25", ["Statement 100.pdf"])


# A percentage change is measured from the earlier figure: a fall from the larger one, a rise from the smaller one.
def test_a_fall_is_measured_from_the_figure_it_fell_from():
    material = ["Costs: 10,000 last year, 8,500 this year"]
    # 20% was "corrected" to 17.6%, the change measured against 8,500. The fall is 15%: too far off to rewrite, so flagged.
    answer = "Costs fell 20% from $10,000 to $8,500 [1]."
    result = review(answer, material=material, files=[])
    assert result.text == answer and result.checks
    close = "Costs fell 16% from $10,000 to $8,500 [1]."
    assert review(close, material=material, files=[]).text == close.replace("16%", "15%")
    # 18% (the change over 8,500, rounded) passed as right; the fall is 15%.
    wrong = "Costs fell 18% from $10,000 to $8,500 [1]."
    result = review(wrong, material=material, files=[])
    assert result.checks or result.changed(wrong)


def test_a_rise_is_measured_from_the_figure_it_rose_from():
    material = ["Costs: 8,500 last year, 10,000 this year"]
    answer = "Costs rose 20% from $8,500 to $10,000 [1]."
    assert review(answer, material=material, files=[]).text == answer.replace("20%", "17.6%")


def test_a_change_with_no_direction_is_flagged_not_rewritten():
    answer = "Costs saw a change of 20% from $10,000 to $8,500 [1]."
    result = review(answer, material=["10,000 and 8,500"], files=[])
    assert result.text == answer and result.checks


# European-style amounts ("€1.234,56") were read as 1.234.
def test_a_european_formatted_amount_matches_the_same_amount_written_us_style():
    result = review("The invoice total is €1,234.56 [1].", material=[], files=[("Rechnung.pdf", "[page 1]\nGesamtbetrag: €1.234,56\n")])
    assert result.checks == []


def test_a_right_total_under_european_formatted_lines_is_not_rewritten():
    body = "[page 1]\nPosition A: €1.234,56\nPosition B: €2.000,00\nGesamt: €3.234,56\n"
    answer = "The two lines are:\n- A: €1.234,56\n- B: €2.000,00\nTotal: €3,234.56"
    result = review(answer, material=[], files=[("Rechnung.pdf", body)])
    assert result.text == answer, result.checks


def test_us_format_numbers_and_short_lists_read_as_before():
    from controller_inbox.answer_check import numbers_in

    assert [n.value for n in numbers_in("$1,234.56 and 1.234 and items 1,2 and 1,234,567 and 1.5,2")] == [1234.56, 1.234, 1, 1234567, 1.5]


# The day and month of a numeric date are not figures.
def test_numeric_dates_are_not_flagged():
    for answer, said in [("Invoice 58213 is due 11/14/2026 [1].", "due November 14, 2026"), ("Invoice 58213 is due 14.11.2026 [1].", "due 14 November 2026")]:
        assert review(answer, material=[f"Invoice 58213, {said}"], files=[]).checks == []


# "increased by", like "rose by", says the figure was worked out from the ones beside it.
def test_a_figure_after_a_past_tense_verb_is_recomputed():
    for verb in ["increased by", "dropped by", "decreased by"]:
        result = review(f"Costs {verb} $1,600, between $8,500 and $10,000 [1].", material=["8,500 and 10,000"], files=[])
        assert "$1,500" in result.text, (verb, result.text, result.checks)


def test_a_page_correction_does_not_match_a_figure_inside_a_bigger_one():
    pdf = (
        "[page 1]\nDeposit received $500.00\n"
        "[page 2]\nRemaining balance $2,500.00 after the deposit received toward it\n"
        "[page 3]\nTerms and conditions\n"
    )
    answer = "The deposit received toward the balance was $500.00 (page 3) [1]."
    # $500.00 is only on page 1; page 2's $2,500.00 merely contains the characters "500.00".
    assert "(page 2)" not in review(answer, material=[], files=[("statement.pdf", pdf)]).text


def test_a_page_is_corrected_when_the_answer_drops_zero_cents():
    # The standalone-figure match read "1,300" as not in "1,300.00", so the wrong page stood.
    text = (
        "[page 1]\nHarbor Packaging invoice INV-2231. Bill to Taz Corp. Terms net 30.\n"
        "[page 2]\nLine items\nFreight charge 1,300.00\nPallets 2,880.00\nTotal 4,180.00\n"
    )
    result = check_citations("The freight charge was $1,300 (invoice.pdf, page 1).", [("invoice.pdf", text)])
    assert "page 2" in result.text, result


def test_a_page_is_corrected_when_the_answer_drops_a_trailing_zero():
    text = "[page 1]\nCover letter and general terms of the agreement.\n[page 2]\nInterest rate 2.50 per annum fixed.\n"
    result = check_citations("The interest rate is 2.5 per annum (loan.pdf, page 1).", [("loan.pdf", text)])
    assert "page 2" in result.text, result


def test_a_list_of_pages_is_not_corrected_to_one_page():
    pdf = "[page 1]\nInvoice 58213\nSubtotal $4,000.00\n[page 2]\nTotal due $4,100.00\n"
    answer = "The total due is $4,100.00 on pages 1, 2 [1]."
    assert review(answer, material=[], files=[("a.pdf", pdf)]).text == answer


# Found by fuzzing: a European amount under a thousand ("€447,15") lost its cents.

def test_european_amount_under_one_thousand_keeps_its_cents():
    # "€447,15" is 447.15 in European style, just like "€1.447,15" is 1447.15.
    assert [n.value for n in numbers_in("€447,15")] == [447.15]


def test_correct_european_list_total_is_not_rewritten():
    answer = (
        "Open invoices:\n"
        "- Vendor A: €3.452,00\n"
        "- Vendor B: €447,15\n"
        "- Vendor C: €83.098,00\n"
        "Total: €86.997,15"
    )
    material = "Vendor A €3.452,00. Vendor B €447,15. Vendor C €83.098,00."
    review = check_numbers(answer, Grounding([material]))
    assert review.text == answer, review.checks


def test_wrong_european_sum_is_corrected_to_the_right_figure_not_spliced():
    answer = "The invoices were €444,23 and €149,40, a total of €637,25."
    review = check_numbers(answer, Grounding(["Invoice A €444,23. Invoice B €149,40."]))
    # Either flagged or corrected to €593,63; never "€593,25" (the euros of the sum glued to the old cents).
    assert "€593,25" not in review.text, (review.text, review.checks)


def test_a_payment_listed_without_a_minus_is_not_added_to_the_total():
    # "Less payment received" takes away from the total: the right $1,200.00 was "corrected" to $1,800.00.
    answer = (
        "Here's what Ridgeview still owes:\n- Invoice 1042: $1,000.00\n- Invoice 1043: $500.00\n"
        "- Less payment received 10/2: $300.00\nTotal due: $1,200.00"
    )
    material = ["Invoice 1042 for $1,000.00 dated 9/12. Invoice 1043 for $500.00 dated 9/19. We received your payment of $300.00 on 10/2."]
    assert review(answer, material=material, files=[]).text == answer


def test_a_cell_is_corrected_on_the_sheet_the_sentence_names_or_given_its_sheet():
    book = (
        '[sheet "Summary" rows 1-3]\nA1 (Item): Revenue | B1 (Amount): 12,400.00\nA2 (Item): Expenses | B2 (Amount): 7,400.00\n'
        'A3 (Item): Net | B3 (Amount): 5,000.00\n[sheet "Detail" rows 1-3]\nA1 (Line): Rent | B1 (Amount): 3,200.00\n'
        "A2 (Line): Payroll | B2 (Amount): 4,200.00\nA3 (Line): Marketing | B3 (Amount): 900.00"
    )
    files = [("Q3 P&L.xlsx", book)]
    # B2 of the Summary sheet is Expenses: the corrected cell says which sheet it is on.
    named = review("Payroll was $4,200.00 (Summary sheet, cell B3).", material=[], files=files)
    assert named.text == "Payroll was $4,200.00 (Summary sheet, cell Detail!B2).", named.checks
    bare = review("Payroll was $4,200.00 (cell B3).", material=[], files=files)
    assert bare.text == "Payroll was $4,200.00 (cell Detail!B2).", bare.checks
