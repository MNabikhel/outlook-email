"""Amounts of money: reading one as printed, and writing one for the page and messages.

An amount is read the way an invoice or a statement prints it: US style (1,234.56) or European (1.234,56), with
its currency mark ($, €, £, or a code such as USD), and a credit written any of the usual ways: a minus in front
or behind ("450.00-"), brackets ("(450.00)"), or "CR" after it. "DR" after an amount says it's a debit, which is
what an amount is anyway.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_CURRENCY = r"(?:US\$|[$€£]|USD|EUR|GBP|CAD)"
# A minus: the hyphen, or the minus sign a spreadsheet or a PDF prints (U+2212).
_MINUS = "-\N{MINUS SIGN}"
_SIGNS = r"\-\u2212"
# What may stand round the figure: brackets, signs, a currency mark; then "CR" or "DR".
_SHAPE = re.compile(
    r"(?i)^\s*(?P<lead>(?:[(+" + _SIGNS + r"\s]|" + _CURRENCY + r")*?)"
    r"(?P<figure>[.,]?\d(?:[\d.,]*\d)?)"
    r"(?P<tail>(?:[)" + _SIGNS + r"\s]|" + _CURRENCY + r")*?)"
    r"(?:(?P<side>CR|DR)\.?)?\s*$"
)
# A figure with its thousands mark (group 1) and, after it, the other mark for the decimals: 1,234.56 or 1.234,56.
# A figure with one mark and three figures after it ("1,234", "1.234") is in thousands.
_GROUPED = re.compile(r"\d{1,3}(?:([.,])\d{3})+(?:(?!\1)[.,](\d+))?")
_PLAIN = re.compile(r"\d*(?:[.,](\d+))?")
# Currencies printed with a decimal point: there "3.459" is three and a bit, not three thousand.
_POINT_CURRENCY = re.compile(r"(?i)\$|USD|CAD|£|GBP")


def parse_amount(text: str | None, *, decimal: str | None = None) -> Decimal | None:
    """The amount ``text`` prints, signed (a credit is negative), or None when it isn't one amount.

    ``decimal`` is the decimal mark when the caller knows it ("." for figures read as US style): the other mark
    then only groups the figures, wherever it stands ("1,23,456" is 123456), and "1.234" is one and a bit."""
    shape = _SHAPE.match(text or "")
    if not shape:
        return None
    lead, figure, tail = shape.group("lead"), shape.group("figure"), shape.group("tail")
    if decimal is not None:
        grouping = "," if decimal == "." else "."
        figure = figure.replace(grouping, "")
        if figure.count(decimal) > 1:
            return None
        figure = figure.replace(decimal, ".")
    elif (grouped := _GROUPED.fullmatch(figure)) and not _decimals_only(figure, grouped, lead + tail):
        figure = figure.replace(grouped.group(1), "").replace(",", ".")
    elif _PLAIN.fullmatch(figure):
        figure = figure.replace(",", ".")
    else:
        return None
    try:
        amount = Decimal(figure)
    except InvalidOperation:
        return None
    credit = any(mark in lead + tail for mark in _MINUS)
    credit = credit or ("(" in lead and ")" in tail) or (shape.group("side") or "").upper() == "CR"
    return -amount if credit else amount


def _decimals_only(figure: str, grouped: re.Match, marks: str) -> bool:
    """Whether a figure that could be in thousands ("1.234", "0,125") is a decimal fraction: one that starts with 0
    (no figure is grouped after a lone 0), or one point and three figures in a currency printed with a point."""
    if grouped.group(2) is not None or figure.count(grouped.group(1)) > 1:
        return False
    return figure.startswith("0") or (grouped.group(1) == "." and bool(_POINT_CURRENCY.search(marks)))


def fmt_money(value: float | Decimal | int | None, *, symbol: str = "$") -> str:
    """An amount as the page and messages show it: "$4,210.00", a credit "-$450.00"; nothing for no amount."""
    if value is None:
        return ""
    return f"-{symbol}{-value:,.2f}" if value < 0 else f"{symbol}{value:,.2f}"
