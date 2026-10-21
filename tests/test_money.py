from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from controller_inbox import agent, teach
from controller_inbox.extract import extract_fields
from controller_inbox.extract import parse_amount as extract_amount
from controller_inbox.money import fmt_money, parse_amount


@pytest.mark.parametrize(
    "text, amount",
    [
        # A point then three figures is a decimal point where the currency prints it so (dollars, pounds), and a
        # figure that starts with 0 is never in thousands; a bare one, or in euros, is in thousands.
        ("$3.459", "3.459"),
        ("12.500 USD", "12.500"),
        ("£1.250", "1.250"),
        ("$0.125", "0.125"),
        ("0.125", "0.125"),
        ("0,125", "0.125"),
        ("€1.250", "1250"),
        ("1.250 EUR", "1250"),
        ("$1.234.567", "1234567"),
    ],
)
def test_a_point_and_three_figures_is_read_as_the_currency_prints_it(text, amount):
    assert parse_amount(text) == Decimal(amount)


def test_a_dollar_total_with_three_decimals_isnt_read_in_thousands():
    assert extract_fields("Total due: $3.459", as_of=date(2026, 10, 1)).primary_amount == pytest.approx(3.46)
    assert extract_fields("Total: 12.500 USD", as_of=date(2026, 10, 1)).primary_amount == pytest.approx(12.5)
    assert extract_fields("Total due: €48.000", as_of=date(2026, 10, 1)).primary_amount == 48000


@pytest.mark.parametrize(
    "text, amount",
    [
        # US style, and the figures extract's patterns give (no currency mark, no sign).
        ("1,234.56", "1234.56"),
        ("12,850.00", "12850.00"),
        ("48,000", "48000"),
        ("1,234,567.89", "1234567.89"),
        ("250.00", "250.00"),
        ("7", "7"),
        (".50", "0.50"),
        # European style, as teach reads it.
        ("1.234,56", "1234.56"),
        ("1.234.567,89", "1234567.89"),
        ("12,50", "12.50"),
        ("4.210", "4210"),
        # One mark with three figures after it is in thousands, either way round.
        ("1,234", "1234"),
        ("1.234", "1234"),
        # Currency marks and codes, either side.
        ("$1,234.56", "1234.56"),
        ("$ 48,000", "48000"),
        ("US$48,000", "48000"),
        ("€ 1.234,56", "1234.56"),
        ("£12.50", "12.50"),
        ("USD 1,234.00", "1234.00"),
        ("1.234,00 EUR", "1234.00"),
        ("450 CAD", "450"),
        # Credits.
        ("-450.00", "-450.00"),
        ("\N{MINUS SIGN}14,789.53", "-14789.53"),
        ("-$5", "-5"),
        ("$-5", "-5"),
        ("(450.00)", "-450.00"),
        ("($78,096.09)", "-78096.09"),
        ("(€1.234,56)", "-1234.56"),
        ("450.00-", "-450.00"),
        ("450.00 CR", "-450.00"),
        ("450.00CR", "-450.00"),
        ("5 cr.", "-5"),
        ("450.00 DR", "450.00"),
        ("+3", "3"),
        # A bracket that isn't closed isn't a credit (teach's rule).
        ("(450.00", "450.00"),
    ],
)
def test_parse_amount_reads_every_format(text, amount):
    assert parse_amount(text) == Decimal(amount)


@pytest.mark.parametrize("text", ["", None, "abc", "1,2,3", "12,345,67", "5 - 3", "5x", "x5", "20 kg", "1e5"])
def test_parse_amount_is_none_for_what_isnt_one_amount(text):
    assert parse_amount(text) is None


@pytest.mark.parametrize(
    "text, decimal, amount",
    [
        ("1.234", ".", "1.234"),
        ("9,000.00", ".", "9000.00"),
        ("1,23,456", ".", "123456"),
        ("1,234", ",", "1.234"),
        ("1.234,5", ",", "1234.5"),
    ],
)
def test_parse_amount_with_a_known_decimal_mark(text, decimal, amount):
    assert parse_amount(text, decimal=decimal) == Decimal(amount)


def test_extract_parse_amount_keeps_a_float_to_the_cent():
    assert extract_amount("12,850.00") == 12850.0
    assert extract_amount("1.234,56") == 1234.56
    assert extract_amount("") is None
    assert isinstance(extract_amount("48,000"), float)


@pytest.mark.parametrize(
    "value, shown",
    [(4210, "$4,210.00"), (4210.5, "$4,210.50"), (Decimal("1234.567"), "$1,234.57"), (-450, "-$450.00"), (0, "$0.00"), (None, "")],
)
def test_fmt_money(value, shown):
    assert fmt_money(value) == shown


def test_fmt_money_without_a_symbol():
    assert fmt_money(4210.5, symbol="") == "4,210.50"
    assert fmt_money(-4210.5, symbol="") == "-4,210.50"


def test_teach_shows_a_credit_with_its_minus_first():
    assert teach.shown("total", "-450.0") == "-$450.00"
    assert teach.shown("total", "4210.5") == "$4,210.50"


@pytest.mark.parametrize(
    "text, amount",
    [
        ("Total due: €4.210,50", 4210.5),
        ("Amount due 1.234,56", 1234.56),
        ("Invoice total EUR 2.500,00", 2500.0),
        ("Balance due €12,50", 12.5),
        ("Please pay €48.000 by Friday", 48000.0),
        ("Qty: Total | Price: not listed | Ext: 1.234,56", 1234.56),
    ],
)
def test_extract_fields_reads_european_amounts_as_teach_does(text, amount):
    # teach reads a European amount; the email's own fields used to miss it (or read 4.21 of 4.210,50).
    assert teach.value_of("total", text, as_of=date(2026, 10, 1)) == str(amount)
    assert extract_fields(text, as_of=date(2026, 10, 1)).amounts == [amount]


@pytest.mark.parametrize(
    "text, amounts",
    [
        ("Amount due $12,850.00.", [12850.0]),
        ("Total: 3 items", []),
        ("$1,2345", []),
        ("Total due $4,210.00 incl. 20% VAT of $701.67", [4210.0, 701.67]),
        ("Total 1,234.56 and 2.50 shipping", [1234.56]),
    ],
)
def test_extract_fields_us_amounts_unchanged(text, amounts):
    assert extract_fields(text, as_of=date(2026, 10, 1)).amounts == amounts


@pytest.mark.parametrize(
    "figure, amount",
    [("$9,000", "9000"), ("9,000.00", "9000"), ("(78,096.09)", "78096.09"), ("\N{MINUS SIGN}14,789.53", "14789.53"), ("32.0%", "32"), ("1.234", "1.234")],
)
def test_agent_amount_reads_a_figure_us_style_without_its_sign(figure, amount):
    assert agent._amount(figure) == Decimal(amount)


def test_a_credit_note_is_summed_up_with_its_minus_first():
    # A credit taught as the total (-450.00) was written "$-450.00" in the summary line, but "-$450.00" on the page.
    from controller_inbox.models import DocumentType, EmailRecord, ExtractedFields, Importance
    from controller_inbox.reading import script_summary

    email = EmailRecord(
        id="m1", subject="Credit memo CM-7", sender_name="Acme", sender_email="ar@acme.example", received_at="2026-10-01T09:00:00+00:00",
        body_text="", body_preview="", has_attachments=False, outlook_importance="normal", is_read=False,
        category=DocumentType.AP_INVOICE, category_confidence=0.9, importance=Importance.MEDIUM, importance_score=48,
        extracted=ExtractedFields(invoice_numbers=["CM-7"], amounts=[-450.0]),
    )
    assert "-$450.00" in script_summary(email)
