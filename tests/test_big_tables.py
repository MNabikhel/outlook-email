"""Large tables with names merged down the rows, and questions about rows deep inside them that ask for several
figures at once: each figure is read under the right name, picked out for the question, and asked for."""

from __future__ import annotations

from pdffactory import Text, build_pdf

from controller_inbox import agent, camelot_tables, table_lookup
from controller_inbox.documents import pdf_text

ACCOUNTS = ["Salaries", "Overtime", "Fuel", "Utilities"]


def _merged(groups: list[tuple[str, list[str]]], *, top: float = 700, pitch: float = 14, heading: str = "", named_left: float = 40) -> list[Text]:
    """A budget table whose department is drawn once, halfway down its rows (between the second and third of
    four), as a report writer centres a cell merged down them. ``heading``: a fund's name over the departments."""
    items = [Text(named_left, top, "Department", bold=True), Text(140, top, "Account", bold=True), Text(360, top, "Amount", bold=True, right=True)]
    y = top - pitch
    if heading:
        items.append(Text(40, y, heading, bold=True))
        y -= pitch
    amount = 1000
    for name, accounts in groups:
        first = y
        for account in accounts:
            amount += 137
            items += [Text(140, y, account), Text(360, y, f"{amount:,}.00", right=True)]
            y -= pitch
        if name:
            middle = (first + y + pitch) / 2
            items.append(Text(named_left, middle, name))
    return items


def test_a_name_merged_halfway_down_its_rows_names_each_of_them():
    text = pdf_text(build_pdf([_merged([("Fire", ACCOUNTS), ("Parks", ACCOUNTS), ("Library", ACCOUNTS)])]))
    for name in ("Fire", "Parks", "Library"):
        for account in ACCOUNTS:
            assert f"Department: {name} | Account: {account} |" in text, text


def test_a_heading_over_the_departments_names_none_of_their_rows():
    text = pdf_text(build_pdf([_merged([("Fire", ACCOUNTS), ("Parks", ACCOUNTS)], heading="GENERAL FUND")]))
    assert "Department: GENERAL FUND" not in text
    assert "Department: Fire | Account: Salaries |" in text and "Department: Parks | Account: Utilities |" in text


def test_a_total_under_a_merged_name_does_not_name_the_rows_above_it():
    items = _merged([("Northeast", ACCOUNTS)])
    items += [Text(40, 700 - 14 * 5, "Northeast total"), Text(360, 700 - 14 * 5, "9,999.00", right=True)]
    text = pdf_text(build_pdf([items]))
    assert "Northeast total | Account: Utilities" not in text
    assert "Department: Northeast | Account: Utilities |" in text


def test_camelot_rows_without_the_merged_name_are_not_new():
    read = [["Northeast", "Iris sensor", "187,714", "199,664"], ["Northeast", "Harbor meter", "104,755", "173,700"]]
    camelot = [["", "Iris sensor", "187,714", "199,664"], ["", "Harbor meter", "104,755", "173,700"], ["", "", "", ""]]
    assert not camelot_tables._adds_named_rows(camelot, read)


SALES = "\n".join(
    [
        "[page 3]",
        "[table]",
        "Region | Product | FY2024 Q1 | FY2024 Q2 | FY2024 Total | FY2025 Q1 | FY2025 Q2 | FY2025 Total",
        *(
            f"Region: {region} | Product: {product} | FY2024 Q1: {a:,} | FY2024 Q2: {b:,} | FY2024 Total: {a + b:,} | "
            f"FY2025 Q1: {c:,} | FY2025 Q2: {d:,} | FY2025 Total: {c + d:,}"
            for region, product, a, b, c, d in [
                ("Gulf Coast", "Aster valve", 158_749, 177_048, 151_169, 151_194),
                ("Gulf Coast", "Fern hose", 120_000, 110_000, 300_001, 250_000),
                ("Mountain", "Iris sensor", 116_729, 132_645, 118_075, 118_023),
                ("Mountain", "Cobalt pump", 90_210, 80_400, 91_000, 86_118),
            ]
        ),
    ]
)

BUDGET = "\n".join(
    [
        "[page 2]",
        "[table]",
        "Department | Account | Code | Budget Amended | Actual YTD | Actual Encumbered | Available",
        "Department: Library | Account: Training | Code: 160-5190 | Budget Amended: 831,458.28 | Actual YTD: 894,789.45 | "
        "Actual Encumbered: 14,764.92 | Available: -78,096.09",
        "Department: Library | Account: Fuel | Code: 160-5150 | Budget Amended: 74,445.68 | Actual YTD: 74,285.46 | "
        "Actual Encumbered: 1,700.28 | Available: -1,540.06",
        "Department: Police | Account: Utilities | Code: 130-5170 | Budget Amended: 371,260.61 | Actual YTD: 384,050.14 | "
        "Actual Encumbered: 2,000.00 | Available: -14,789.53",
        "Department: Police | Account: Fuel | Code: 130-5150 | Budget Amended: 384,400.62 | Actual YTD: 300,000.00 | "
        "Actual Encumbered: 9,000.00 | Available: 75,400.62",
    ]
)


def test_several_figures_from_one_deep_row_are_each_a_point():
    found = table_lookup.answer(SALES, "What were the Q1 and Q2 FY2025 sales for the Iris sensor in the Mountain region, and its FY2025 total?")
    assert found is not None
    assert found.points == [
        ("Mountain · Iris sensor: FY2025 Q1", "118,075"),
        ("Mountain · Iris sensor: FY2025 Q2", "118,023"),
        ("Mountain · Iris sensor: FY2025 Total", "236,098"),
    ]


def test_each_column_under_a_merged_heading_is_asked_for():
    found = table_lookup.answer(SALES, "For the Cobalt pump in Mountain, give me each quarter of FY2024.")
    assert found is not None
    labels = [point.split(": ")[1] for point, _value in found.points]
    assert labels == ["FY2024 Q1", "FY2024 Q2", "FY2024 Total"]


def test_a_change_is_the_later_figure_less_the_earlier():
    out = table_lookup.lookup(SALES, "How did Fern hose sales in the Gulf Coast change from FY2024 to FY2025 in total?")
    assert "FY2025 Total 550,001 − FY2024 Total 230,000 = 320,001 (+139.1%)" in out


def test_each_thing_a_question_asks_of_one_row_is_shown():
    found = table_lookup.answer(BUDGET, "What is account code 160-5190, and how much is encumbered and still available on it?")
    assert found is not None
    values = [value for _point, value in found.points]
    assert "14,764.92" in values and "-78,096.09" in values and "Training" in values
    assert "Worked out" not in table_lookup.render(found)


def test_over_an_amended_budget_compares_the_actual_with_it():
    out = table_lookup.lookup(BUDGET, "Which Police accounts are over their amended budget so far?")
    assert "Utilities" in out and "Actual YTD over Budget Amended" in out
    assert "Account: Fuel" not in out.split("row:")[0]


def test_the_plan_lists_each_point_and_the_answer_is_checked_for_them():
    points = [("Mountain · Iris sensor: FY2025 Q1", "118,075"), ("Mountain · Iris sensor: FY2025 Q2", "118,023")]
    plan = agent.answer_plan(points)
    assert "asks for 2 things" in plan and "1. Mountain · Iris sensor: FY2025 Q1" in plan
    assert agent.missing_points(points, "Q1 was 118,075 (page 3).") == [points[1]]
    assert agent.missing_points(points, "Q1 was 118075 and Q2 $118,023.") == []
    # An answer that gives none of them is about something else: nothing is added to it.
    assert agent.missing_points(points, "The file has no such product.") == []
    assert agent.missing_points([("Available", "-78,096.09")] * 1 + [("Encumbered", "14,764.92")], "Available is (78,096.09).") == [("Encumbered", "14,764.92")]


def test_a_question_asking_for_many_figures_gets_room_for_them():
    assert agent.reply_tokens([("a", "1")], 500) == 500
    assert agent.reply_tokens([(str(n), "1") for n in range(20)], 500) == 1150
    assert agent.reply_tokens([(str(n), "1") for n in range(100)], 500) == agent.MAX_REPLY_TOKENS


def test_a_long_merge_named_left_of_its_heading_stays_in_the_table():
    # The page starts with a department's twelve rows; its name is drawn halfway down them, a little left of where
    # its column's heading ("Department", centred) starts.
    twelve = [f"Account {n}" for n in range(1, 13)]
    items = _merged([("Fire", twelve), ("Parks", twelve)], named_left=70)
    items = [item if item.text not in {"Fire", "Parks"} else Text(40, item.y, item.text) for item in items]
    text = pdf_text(build_pdf([items]))
    assert "Department: Fire | Account: Account 1 |" in text and "Department: Fire | Account: Account 12 |" in text
    assert "Department: Parks | Account: Account 1 |" in text and "Department: Parks | Account: Account 12 |" in text


def test_a_figure_inside_another_is_not_given():
    points = [("Hours OT", "0.00"), ("Gross", "2,766.40")]
    assert agent.missing_points(points, "Gross pay was 2,766.40 on 3,000.00 hours.") == [("Hours OT", "0.00")]


PAYROLL = "\n".join(
    [
        "[page 1]",
        "[table]",
        "Employee | Hours Reg | Hours OT | Earnings Overtime | Net pay",
        "Employee: Moreau, Grace | Hours Reg: 76.50 | Hours OT: 14.00 | Earnings Overtime: 422.10 | Net pay: 1,500.00",
        "Employee: Johnson, Priya | Hours Reg: 76.50 | Hours OT: 13.50 | Earnings Overtime: 954.45 | Net pay: 3,139.64",
        "Employee: Reyes, Hannah | Hours Reg: 80.00 | Hours OT: 0.00 | Earnings Overtime: 0.00 | Net pay: 2,125.98",
    ]
)


def test_overtime_hours_is_the_ot_column_and_earned_is_earnings():
    found = table_lookup.answer(PAYROLL, "For Hannah Reyes, what were the overtime hours and net pay?")
    assert found is not None
    assert [point.split(": ")[-1] for point, _value in found.points] == ["Hours OT", "Net pay"]
    most = table_lookup.answer(PAYROLL, "Who earned the most overtime pay this period, and how much?")
    assert most is not None and most.points == [("Largest Earnings Overtime: Johnson, Priya", "954.45")]
    # The winner's whole row is shown: it may be in a part of the file the model isn't shown.
    assert "row: Employee: Johnson, Priya |" in table_lookup.render(most)
