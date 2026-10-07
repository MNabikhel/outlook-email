"""Tables as other PDF writers lay them out (browsers printing HTML, report libraries, word processors):
headings placed differently from a spreadsheet's, tables stacked on one page, cells packed tight."""

from controller_inbox.documents import pdf_text
from pdffactory import Text, build_pdf, width


def _page(*items: Text) -> list[str]:
    text = pdf_text(build_pdf([list(items)]))
    return text.split("\n", 1)[1].splitlines() if text.startswith("[page 1]") else text.splitlines()


def _row(y: float, cells: list[tuple[float, str, str]], *, bold: bool = False, size: float = 10) -> list[Text]:
    """One line of cells: (x, "left"|"right", text); a right-aligned cell ends at x."""
    return [Text(x, y, text, size=size, bold=bold, right=align == "right") for x, align, text in cells if text]


def test_two_stacked_tables_with_different_columns_each_keep_their_own():
    # A reconciliation with four figure columns, and under its own title a list of checks whose dates sit
    # where the reconciliation has the gap between two of its columns.
    rec = [(40, "left"), (250, "right"), (330, "right"), (410, "right"), (490, "right")]
    items = _row(720, list(zip(*zip(*rec), ["", "Operating", "Payroll", "Lockbox", "Total"])), bold=True)
    figures = [
        ["Balance per bank", "1,284,615.42", "48,210.07", "362,904.88", "1,695,730.37"],
        ["Deposits in transit", "86,412.50", "-", "14,775.30", "101,187.80"],
        ["Outstanding checks", "(143,887.19)", "(31,402.66)", "-", "(175,289.85)"],
        ["Adjusted balance", "1,227,140.73", "16,807.41", "377,680.18", "1,621,628.32"],
    ]
    for r, row in enumerate(figures):
        items += _row(705 - 15 * r, [(x, align, cell) for (x, align), cell in zip(rec, row)])
    items.append(Text(40, 630, "Outstanding checks - Operating", size=11, bold=True))
    checks = [(40, "left"), (300, "left"), (395, "left"), (560, "right")]
    items += _row(610, [(40, "left", "Payee"), (300, "left", "Check #"), (395, "left", "Issue Date"), (560, "right", "Amount")], bold=True)
    payees = [["Tidewater Metals", "20418", "9/14/2026", "52,300.00"], ["Summit Ridge Electric", "20425", "9/17/2026", "18,744.65"],
              ["Pinecrest Fleet", "20433", "9/22/2026", "6,215.80"], ["Oakline Office Supply", "20437", "9/24/2026", "66,627.74"]]
    for r, row in enumerate(payees):
        items += _row(595 - 15 * r, [(x, align, cell) for (x, align), cell in zip(checks, row)])
    lines = _page(*items)
    assert "Balance per bank | Operating: 1,284,615.42 | Payroll: 48,210.07 | Lockbox: 362,904.88 | Total: 1,695,730.37" in lines
    assert "Payee: Summit Ridge Electric | Check #: 20425 | Issue Date: 9/17/2026 | Amount: 18,744.65" in lines


def test_a_weekday_and_date_in_one_right_aligned_cell_stay_one_cell():
    # "Mon 09/28" right-aligned under "Date": the dates are all as wide, so the weekdays end level and the
    # dates start level, one space apart, like two cells packed tight.
    rows = [("WD-2", "Mon 09/28", "Cut off AP invoice entry", "Sam Ortiz"), ("WD-1", "Tue 09/29", "Run payroll accrual", "Grace Kim"),
            ("WD1", "Thu 10/01", "Post bank activity", "Li Wei"), ("WD2", "Fri 10/02", "Bank reconciliations", "Li Wei"),
            ("WD3", "Mon 10/05", "Fixed asset depreciation run", "Priya Raman")]
    items = _row(720, [(40, "left", "Workday"), (150, "right", "Date"), (175, "left", "Task"), (400, "left", "Owner")], bold=True)
    for r, (day, date, task, owner) in enumerate(rows):
        items += _row(705 - 15 * r, [(40, "left", day), (150, "right", date), (175, "left", task), (400, "left", owner)])
    lines = _page(*items)
    assert "Workday: WD-2 | Date: Mon 09/28 | Task: Cut off AP invoice entry | Owner: Sam Ortiz" in lines
    assert "Workday: WD3 | Date: Mon 10/05 | Task: Fixed asset depreciation run | Owner: Priya Raman" in lines


def test_a_code_and_a_name_in_one_cell_under_one_heading_stay_together():
    entities = [("US01", "Cobalt Ridge Industries, Inc.", "4,924,247.05"), ("CA02", "Cobalt Ridge Canada ULC", "404,964.37"),
                ("UK03", "Cobalt Ridge UK Ltd.", "486,553.60"), ("DE05", "Cobalt Ridge GmbH", "201,891.65")]
    items = _row(720, [(40, "left", "Entity"), (420, "right", "Receivable")], bold=True)
    for r, (code, name, amount) in enumerate(entities):
        items += _row(705 - 15 * r, [(40, "left", f"{code} {name}"), (420, "right", amount)])
    lines = _page(*items)
    assert "Entity: CA02 Cobalt Ridge Canada ULC | Receivable: 404,964.37" in lines


def test_a_cell_running_past_the_others_in_its_column_is_not_cut():
    # The terms end level ("Net 30" right-aligned), except "2% 10, Net 30", which runs on a word past them.
    terms = ["Net 30", "Net 60", "2% 10, Net 30", "Net 45", "Net 30", "Net 30", "Net 60"]
    items = _row(720, [(40, "left", "Customer"), (190, "left", "Terms"), (320, "right", "Balance")], bold=True)
    for r, term in enumerate(terms):
        y = 705 - 15 * r
        items += _row(y, [(40, "left", f"Customer {r + 1}"), (320, "right", f"{(r + 3) * 1234.5:,.2f}")])
        if term.startswith("2%"):
            items += [Text(221 - width("2% 10, Net", 10), y, "2% 10, Net"), Text(221 + width(" ", 10), y, "30")]
        else:
            items.append(Text(220, y, term, right=True))
    lines = _page(*items)
    assert "Customer: Customer 3 | Terms: 2% 10, Net 30 | Balance: 6,172.50" in lines


def test_a_heading_right_aligned_over_a_wide_figure_column_names_that_column():
    rows = [["Account", "Jan", "Feb", "Mar"], ["GL 6000", "1,200", "1,350", "1,410"], ["GL 6010", "880", "910", "905"], ["GL 6020", "450", "475", "460"]]
    items = []
    for r, row in enumerate(rows):
        items += _row(720 - 15 * r, [(40, "left", row[0]), (230, "right", row[1]), (300, "right", row[2]), (370, "right", row[3])], bold=r == 0)
    lines = _page(*items)
    assert "Account | Jan | Feb | Mar" in lines
    assert "Account: GL 6010 | Jan: 880 | Feb: 910 | Mar: 905" in lines


def test_a_small_print_header_above_a_table_is_not_a_row_of_it():
    # A browser prints the file name and the time in small type at the top of the page.
    items = [Text(36, 760, "AR Aging 93026", size=5), Text(500, 760, "Run 10/07/2026 0814 AM", size=5)]
    items += _row(720, [(40, "left", "Customer"), (260, "right", "Balance"), (340, "right", "Current"), (420, "right", "Over 90")], bold=True)
    data = [("Alderbrook Hospitality", "24,652.10", "18,442.10", "0.00"), ("Brightline Transit", "68,260.73", "42,785.33", "9,875.40"),
            ("Cascadia Food Co-op", "3,912.75", "3,912.75", "0.00"), ("Dunmore Precision", "18,683.67", "0.00", "11,304.62")]
    for r, row in enumerate(data):
        items += _row(705 - 15 * r, [(40, "left", row[0]), (260, "right", row[1]), (340, "right", row[2]), (420, "right", row[3])])
    lines = _page(*items)
    assert "Customer: Brightline Transit | Balance: 68,260.73 | Current: 42,785.33 | Over 90: 9,875.40" in lines
