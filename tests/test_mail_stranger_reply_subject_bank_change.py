"""A first-time sender from a domain never seen, untrusted, writes "RE: Updated bank details" over a forged
"-----Original Message-----" block (whose own Subject is something else), asks to "update your vendor records",
and attaches the bank-change letter. Because the subject starts with RE: and the body has a quote marker, the
subject is dropped from the sender's own words, even on the stranger path. Result: only "caution", so the email
is not blocked and its letter is opened and coded like any other."""
from controller_inbox.fraud import TrustContext, assess

BODY = (
    "Hi Maya,\n\nPlease see the attached letter on our letterhead and update your vendor records accordingly "
    "before the next payment run.\n\nRegards,\nTom\n\n"
    "-----Original Message-----\nFrom: Maya Chen <maya@taz.com>\nSent: Monday, October 5, 2026 9:12 AM\n"
    "Subject: Invoice INV-1001\n\nHi Tom, payment is scheduled for Friday."
)
LETTER = "Northwind Supplies. Effective immediately our bank details have changed. New account 12345678 sort code 20-00-00."


def test_stranger_reply_with_bank_change_subject_is_blocked():
    ctx = TrustContext(domains={"taz.com"})
    check = assess(
        ctx,
        subject="RE: Updated bank details for Northwind",
        body=BODY,
        sender_name="Tom Reed",
        sender_email="tom@northwind-supplies.com",
        attachments=[("letter.pdf", LETTER)],
    )
    assert check.level == "high", (check.score, [(s.key, s.points) for s in check.signals])
