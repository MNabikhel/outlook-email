"""Classic BEC wording that redirects payment to another account is not read as a bank change, so a
first-time sender gets no fraud flag at all (level "none")."""
import pytest

from controller_inbox.fraud import TrustContext, assess


@pytest.mark.parametrize("body", [
    "Due to an ongoing audit our usual account is on hold. Please remit payment for INV-1001 to the alternate account below.",
    "Please make payment for the open invoices to our new beneficiary account.",
    "Our previous account can no longer receive payments, please use the alternative account details below.",
])
def test_redirected_payment_is_flagged(body):
    ctx = TrustContext(domains={"taz.com"})
    check = assess(ctx, subject="Outstanding invoices", body=body, sender_name="Acme Accounts",
                   sender_email="accounts@acme-supply.com")
    assert check.level == "high", [s.key for s in check.signals]
