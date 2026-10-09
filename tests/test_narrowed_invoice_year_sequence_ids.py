"""Fix 4796099 drops any invoice "number" shaped \\d{4}-\\d{2}(-\\d{2})? as an ISO date, but year-sequence and
account-suffix invoice IDs ("2025-47", "1001-15", "4512-03") share that shape and are not dates."""
from datetime import date

import pytest

from controller_inbox.extract import extract_fields


@pytest.mark.parametrize("text, wanted", [
    ("Please find attached Invoice No. 2025-47 for September services.", "2025-47"),
    ("Invoice #1001-15 is now past due.", "1001-15"),
    ("Attached: invoice 4512-03, amount $2,400.00", "4512-03"),
])
def test_year_sequence_invoice_id_is_kept(text, wanted):
    fields = extract_fields(text, as_of=date(2026, 10, 1))
    assert wanted in fields.invoice_numbers, fields.invoice_numbers
