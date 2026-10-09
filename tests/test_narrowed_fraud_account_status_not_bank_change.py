"""Fix 4796099 added a PAYMENT_CHANGE_RE branch "(our|the) (usual|previous|old|current|existing) [bank] account is
on hold / closed / ..." with no payment or bank word required. A supplier's ordinary credit-hold notice, or a
customer-account consolidation note, is then a "bank_change" and a first-time sender's mail is fraud level high."""
import pytest

from controller_inbox.fraud import TrustContext, assess


@pytest.mark.parametrize("body", [
    "Hi, a reminder that the current account is on hold until the past-due balance on INV-2201 is paid. "
    "Orders will ship once payment is received.",
    "Your order is on credit hold: the existing account is on hold pending the August payment of $4,210.00.",
    "We merged your two ship-to locations. The old account is closed and future invoices will bill under "
    "customer account 55012.",
])
def test_customer_account_status_is_not_a_bank_change(body):
    ctx = TrustContext(domains={"taz.com"})
    check = assess(ctx, subject="Account status", body=body, sender_name="Acme AR", sender_email="ar@acme-supply.com")
    assert "bank_change" not in [s.key for s in check.signals]
    assert check.level != "high"


def test_known_vendor_credit_hold_is_not_a_bank_change():
    ctx = TrustContext(domains={"taz.com"}, known={"ar@acme-supply.com": 12}, senders={"ar@acme-supply.com": "Acme AR"})
    check = assess(ctx, subject="Credit hold", sender_name="Acme AR", sender_email="ar@acme-supply.com",
                   body="The current account is on hold until the past-due balance on INV-2201 is paid.")
    assert "bank_change" not in [s.key for s in check.signals]
