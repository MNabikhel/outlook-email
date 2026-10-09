"""Large tables with names merged down the rows, and questions about rows deep inside them that ask for several
figures at once: each figure is read under the right name, picked out for the question, and asked for."""

from __future__ import annotations

from pdffactory import Text, build_pdf

from controller_inbox import agent, assistant, camelot_tables, chats, table_lookup
from controller_inbox.documents import pdf_text
from controller_inbox.local_llm import ToolReply

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


AGING = "\n".join(
    [
        "[page 1]",
        "[table]",
        "Salesperson | Customer | Cust # | Current | Past due 1-30 | Past due 31-60 | Past due 61-90 | Past due 90+ | Total | Credit limit",
        "Salesperson: Chen, M. | Customer: Granite Labs | Cust # : 40112 | Current: 9,706.75 | Past due 1-30: 1,704.20 | Past due 31-60: - | "
        "Past due 61-90: - | Past due 90+: 70,000.00 | Total: 81,410.95 | Credit limit: 75,000",
        "Salesperson: Chen, M. | Customer: Apex Supply | Cust # : 40147 | Current: 20,753.97 | Past due 1-30: - | Past due 31-60: 17,242.35 | "
        "Past due 61-90: - | Past due 90+: 2,000.00 | Total: 39,996.32 | Credit limit: 100,000",
        "Salesperson: Holt, K. | Customer: Delta Motors | Cust # : 42895 | Current: 3,000.00 | Past due 1-30: 492.86 | Past due 31-60: - | "
        "Past due 61-90: - | Past due 90+: 9,000.00 | Total: 12,492.86 | Credit limit: 10,000",
    ]
).replace("Cust # :", "Cust #:")


def test_an_aging_bucket_is_named_by_its_days():
    found = table_lookup.answer(AGING, "What does Apex Supply owe in the 90+ bucket?")
    assert found is not None and ("Chen, M. · Apex Supply: Past due 90+", "2,000.00") in found.points
    most = table_lookup.answer(AGING, "Which customer has the largest balance over 90 days, and how much is it?")
    assert most is not None and most.points == [("Largest Past due 90+: Granite Labs", "70,000.00")]


def test_over_their_credit_limit_holds_the_total_against_it():
    out = table_lookup.lookup(AGING, "Which of Chen's customers are over their credit limit?")
    assert "Total over Credit limit" in out and "Granite Labs" in out
    assert "Apex Supply" not in out and "Delta Motors" not in out


def test_a_heading_over_aging_buckets_names_only_them():
    # "Current" and "Total" have nothing under them; "Past due" sits over the four day ranges.
    header = [Text(40, 720, "Customer", bold=True), Text(170, 720, "Current", bold=True, right=True),
              Text(320, 720, "Past due", bold=True), Text(560, 720, "Total", bold=True, right=True)]
    subs = [Text(x, 708, label, bold=True, right=True) for x, label in [(240, "1-30"), (310, "31-60"), (380, "61-90"), (450, "90+")]]
    rows = []
    for n, name in enumerate(["Apex", "Birch", "Cedar"]):
        y = 694 - 14 * n
        rows += [Text(40, y, name)] + [
            Text(x, y, f"{(n + 1) * k},000.00", right=True) for k, x in enumerate([170, 240, 310, 380, 450, 560], start=1)
        ]
    text = pdf_text(build_pdf([header + subs + rows]))
    assert "Customer | Current | Past due 1-30 | Past due 31-60 | Past due 61-90 | Past due 90+ | Total" in text, text


def test_a_row_picked_by_its_number_is_named_too():
    found = table_lookup.answer(BUDGET, "What is account code 160-5190, and how much is encumbered and still available on it?")
    assert found is not None
    values = [value for _point, value in found.points]
    assert values[:2] == ["Library", "Training"]


def test_a_worked_out_figure_the_answer_misses_is_added():
    points = [("Largest Earnings Overtime: Johnson, Priya", "954.45")]
    answer = "Johnson, Thomas earned the most overtime pay this period with $269.88."
    assert agent.missing_points(points, answer) == []
    assert agent.missing_points(points, answer, exact=True) == points


STATEMENT = "\n".join([
    "[page 1]", "[table]", "Line | 2025 | 2024",
    "Line: Revenue | 2025: 5,200,000 | 2024: 4,800,000",
    "Line: Cost of sales | 2025: 3,100,000 | 2024: 2,900,000",
])


def test_a_change_between_years_goes_by_the_question_not_the_column_order():
    # The current year is printed first: "from 2024 to 2025" and "2025 over 2024" are both 2025 less 2024.
    for question in ("How did revenue change from 2024 to 2025?", "How much did revenue grow in 2025 over 2024?"):
        out = table_lookup.lookup(STATEMENT, question)
        assert "2025 5,200,000 − 2024 4,800,000 = 400,000 (+8.3%)" in out, (question, out)


WORKBOOK = "\n".join([
    "[page 1]", "[table]", "Line | Q3 | Q4 | Change",
    "Line: Ads | Q3: 1,000 | Q4: 1,500 | Change: 500",
    "Line: Travel | Q3: 250 | Q4: 300 | Change: 50",
    "Line: Total | Q3: 1,250 | Q4: 1,800 | Change: 550",
])


def test_a_change_word_naming_a_column_or_a_row_is_not_a_change_worked_out():
    out = table_lookup.lookup(WORKBOOK, "What was the Q4 change for Ads?")
    assert "−" not in out and "Change: 500" in out, out
    prices = "\n".join([
        "[page 1]", "[table]", "Item | Old Price | New Price | Increase %",
        "Item: Widget | Old Price: 10.00 | New Price: 12.00 | Increase %: 20%",
        "Item: Gadget | Old Price: 20.00 | New Price: 21.00 | Increase %: 5%",
    ])
    out = table_lookup.lookup(prices, "What is the new price and increase % for Widget?")
    assert "−" not in out and "Increase %: 20%" in out, out
    changes = "\n".join([
        "[page 1]", "[table]", "Vendor | Change Type | Effective | Amount",
        "Vendor: Acme | Change Type: Change of address | Effective: 10/01/2026 | Amount: 1,200.00",
        "Vendor: Birch | Change Type: Bank account change | Effective: 10/03/2026 | Amount: 5,400.00",
        "Vendor: Cedar | Change Type: New vendor | Effective: 10/04/2026 | Amount: 900.00",
    ])
    out = table_lookup.lookup(changes, "What is the total amount for vendors with a bank account change?")
    assert "−" not in out and "5,400.00" in out, out


def test_the_total_change_is_a_total():
    out = table_lookup.lookup(WORKBOOK, "What is the total change?")
    assert 'Total of "Change"' in out and "550" in out, out


def test_a_row_named_for_a_change_is_listed():
    cash = "\n".join([
        "[page 1]", "[table]", "Line | 2025 | 2024",
        "Line: Net cash from operations | 2025: 410,000 | 2024: 350,000",
        "Line: Net change in cash | 2025: 290,000 | 2024: 260,000",
        "Line: Cash, end of year | 2025: 1,290,000 | 2024: 1,000,000",
    ])
    out = table_lookup.lookup(cash, "What was the net change in cash in 2025 and 2024?")
    assert "Worked out" not in out and "Net change in cash → 2025: 290,000 | 2024: 260,000" in out, out


def test_a_figure_in_a_limit_is_not_several_things_and_not_on_a_customer_number():
    out = table_lookup.lookup(AGING, "Which customers have a balance of more than 30,000?")
    assert "Cust # more than 30,000" not in out and "Delta Motors" not in out, out
    assert "Granite Labs" in out and "Apex Supply" in out, out


def _centred(groups: list[tuple[str, list[str]]], *, top: float = 700, pitch: float = 14) -> list[Text]:
    """Names centred down their rows, a two-row one between them (the page reader folds it onto the first)."""
    items = [Text(40, top, "Department", bold=True), Text(140, top, "Account", bold=True), Text(360, top, "Amount", bold=True, right=True)]
    y, amount = top - pitch, 1000
    for name, accounts in groups:
        first = y
        for account in accounts:
            amount += 137
            items += [Text(140, y, account), Text(360, y, f"{amount:,}.00", right=True)]
            y -= pitch
        items.append(Text(40, (first + y + pitch) / 2, name))
    return items


def test_a_two_row_name_centred_between_its_rows_is_not_read_as_on_the_first():
    for groups, account, name in [
        ([("Fire", ["Salaries", "Fuel"]), ("Parks", ["Mowing", "Irrigation", "Trees"])], "Mowing", "Parks"),
        ([("Fire", ["Salaries"]), ("Parks", ["Mowing", "Trees"]), ("Library", ["Books", "Rent", "Wifi"])], "Books", "Library"),
    ]:
        text = pdf_text(build_pdf([_centred(groups)]))
        assert f"Department: {name} | Account: {account} |" in text, text


def test_a_figure_or_date_written_another_way_is_given():
    found = table_lookup.answer(BUDGET, "How much is encumbered and still available on Police fuel?")
    assert found is not None
    assert agent.missing_points(found.points, "Police fuel has $9,000 encumbered and $75,400.62 still available.") == []
    ar = "\n".join([
        "[page 1]", "[table]", "Customer | Invoice # | Invoice Date | Due Date | Amount | Balance",
        "Customer: Acme | Invoice #: 5001 | Invoice Date: 09/01/2026 | Due Date: 10/01/2026 | Amount: 1,200.00 | Balance: 1,200.00",
        "Customer: Cedar | Invoice #: 5003 | Invoice Date: 09/09/2026 | Due Date: 10/09/2026 | Amount: 900.00 | Balance: 400.00",
    ])
    found = table_lookup.answer(ar, "When is Cedar's invoice due and what is the balance?")
    assert found is not None
    # A row is named by its words, not its dates.
    assert all(point.startswith("Cedar: ") for point, _value in found.points), found.points
    answer = "Cedar's invoice 5003 is due October 9, 2026, with a balance of $400.00."
    assert agent.missing_points(found.points, answer) == []


INVOICE = "\n".join([
    "[page 1]", "[table]", "Item | Description | Qty | Unit Price | Amount",
    "Item: 1001 | Description: Audit fieldwork | Qty: 10 | Unit Price: 150.00 | Amount: 1,500.00",
    "Item: 1002 | Description: Tax return prep | Qty: 4 | Unit Price: 200.00 | Amount: 800.00",
    "Item: 1003 | Description: Bookkeeping | Qty: 5 | Unit Price: 90.00 | Amount: 450.00",
    "Item: Subtotal | Amount: 2,750.00",
    "Item: Sales tax | Amount: 220.00",
    "Item: Total due | Amount: 2,970.00",
])


def test_a_total_worked_out_is_not_held_against_the_answer(store, settings, mail, monkeypatch):
    # The line items add up to the subtotal; the amount due has the tax on it. A sum is not exact, a largest is.
    total = table_lookup.answer(INVOICE, "What is the total amount due on this invoice?")
    assert total is not None and not total.exact
    largest = table_lookup.answer(INVOICE, "Which line has the largest amount?")
    assert largest is not None and largest.exact
    budget = mail["Q4 budget draft"]
    budget.attachments[0].extracted_text = INVOICE
    store.upsert_email(budget)
    reply = "The total amount due on this invoice is $2,970.00, including $220.00 of sales tax [1]."
    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "complete_text", lambda *_a, **_k: "Plan: none.\nSQL: NONE")
    monkeypatch.setattr(assistant, "chat_with_tools", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("tools")))
    monkeypatch.setattr(assistant, "stream_text", lambda *_a, **_k: iter([reply]))
    events = list(assistant.answer_stream(store, settings, "What is the total amount due on this invoice?", email_id=budget.id))
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "Worked out exactly from the table" not in text and "Also from the table" not in text, text


def test_figures_worked_out_from_the_table_are_not_flagged_as_unread(store, settings, monkeypatch):
    # The change isn't in the file: it was worked out and shown to the model, then added to an answer that
    # left it out, or copied by one that didn't.
    for reply in ("Fern hose sales went up in the Gulf Coast (sales.txt, page 3) [1].",
                  "Fern hose sales rose by 320,001, or 139.1% (sales.txt, page 3) [1]."):
        chat = chats.new_id()
        store.create_chat(chat)
        chats.add_file(store, settings, chat, "sales.txt", "text/plain", SALES.encode())
        monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
        monkeypatch.setattr(assistant, "needs_more_context", lambda _s: False)
        monkeypatch.setattr(assistant, "context_length", lambda _s: 16384)
        monkeypatch.setattr(assistant, "complete_text", lambda *_a, **_k: "SQL: NONE")
        monkeypatch.setattr(assistant, "stream_text", lambda *_a, reply=reply, **_k: iter([reply]))
        monkeypatch.setattr(assistant, "chat_with_tools", lambda *_a, reply=reply, **_k: ToolReply(reply))
        question = "How did Fern hose sales in the Gulf Coast change from FY2024 to FY2025 in total?"
        events = list(assistant.answer_stream(store, settings, question, uploads=chats.chat_mail(store, chat)))
        text = "".join(e["text"] for e in events if e["type"] == "delta")
        assert "320,001" in text, text
        checks = [item for e in events if e["type"] == "check" for item in e["items"]]
        assert not [check for check in checks if "320,001" in check], checks


def test_the_room_for_many_figures_fits_a_small_context(store, settings, monkeypatch):
    rows = ["[page 1]", "[table]", "Department | Account | Budget | Actual YTD"]
    rows += [f"Department: Dept{i} | Account: Acct{i} | Budget: {1000 + i * 10:,}.00 | Actual YTD: {1100 + i * 13:,}.00" for i in range(30)]
    chat = chats.new_id()
    store.create_chat(chat)
    chats.add_file(store, settings, chat, "budget vs actual.txt", "text/plain", "\n".join(rows).encode())
    seen = []

    def tools(_s, messages, _tools, *, max_tokens):
        seen.append((assistant.prompt_chars(messages), max_tokens))
        return ToolReply("Acct0 is over by $100.00 [1].")

    monkeypatch.setattr(assistant, "llm_active", lambda _s: True)
    monkeypatch.setattr(assistant, "needs_more_context", lambda _s: False)
    monkeypatch.setattr(assistant, "context_length", lambda _s: 4096)
    monkeypatch.setattr(assistant, "reply_budget", lambda _s, n: n)
    monkeypatch.setattr(assistant, "complete_text", lambda *_a, **_k: "SQL: NONE")
    monkeypatch.setattr(assistant, "chat_with_tools", tools)
    monkeypatch.setattr(assistant, "stream_text", lambda *_a, **_k: iter(["Acct0 is over by $100.00 [1]."]))
    question = "Which accounts are over budget, and what is the difference for each?"
    list(assistant.answer_stream(store, settings, question, uploads=chats.chat_mail(store, chat)))
    assert seen
    chars, reply = seen[0]
    assert reply > 500 and chars / agent.CHARS_PER_TOKEN + agent.TOOL_SCHEMA_TOKENS + reply <= 4096, seen[0]

