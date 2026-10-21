"""Which row of a table is a total: the one rule a file's reader (``pdf_layout``) and the table questions
(``table_lookup``) share, so a row one of them counts as a total the other doesn't count as a record.

A total row's label starts "Total", "Grand Total" or "Subtotal" ("Total Vehicles"), or ends with "Total" or
"Subtotal" ("Finance Subtotal"); or it is only the words for what is owed ("Amount Due", "Balance owing"), as an
invoice prints under its lines. A row whose label starts "%" ("% of budget") is a ratio, not a record: the table
questions leave it out of what they add up (``percent=True``), while the file's reader keeps it in its category.

Other "total" rules serve other purposes and stay where they are: a column heading that names a total
(``page_details._TOTAL_HEADING``), a summary table that counts tax and shipping among its totals
(``page_details._TOTALS``), the "Total" line of an answer (``answer_check._TOTAL_LINE``), and the label written
beside a figure under the columns (``pdf_layout._SUMMARY``, which takes a bare "Amount" too).
"""

from __future__ import annotations

import re

# "Total Revenue", "Grand total", "Subtotal", "Sub-total", "Sub total", "Totals".
PREFIX = re.compile(r"^(?:grand\s+|sub-?\s?)?totals?\b", re.I)
# A label can name its total last ("Finance Subtotal", "Company Total"); a description can't, so only the end counts.
_LAST = re.compile(r"\b(?:sub-?)?totals?$", re.I)
_OWED = r"(?:due|owing|payable)"
# A label that is only the word for a total: such a row is a total whatever its figures.
_WORD = re.compile(r"^(?:(?:grand\s+|sub-?\s?)?totals?(?:\s+" + _OWED + r")?):?$", re.I)
_DUE = re.compile(r"^(?:amount|balance)\s+" + _OWED + r":?$", re.I)


def is_total_word(label: str, *, due: bool = True, percent: bool = False) -> bool:
    """Whether ``label`` is only the word for a total ("Total", "Grand Total:", "Subtotal"), or, with ``due``,
    for what is owed ("Amount Due", "Balance owing"); with ``percent``, a label that starts "%" counts too."""
    label = (label or "").strip()
    if percent and label.startswith("%"):
        return True
    return bool(_WORD.match(label) or (due and _DUE.match(label)))


def is_total_label(label: str, *, due: bool = True, percent: bool = False) -> bool:
    """Whether a row labelled ``label`` is a total or subtotal row (see the module's note)."""
    label = (label or "").strip()
    return bool(PREFIX.match(label) or _LAST.search(label)) or is_total_word(label, due=due, percent=percent)
