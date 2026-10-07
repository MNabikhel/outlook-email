"""Payment-fraud check: scored signals, trusted senders, and verdicts the user teaches.

An email is blocked ("high") when the sender, in their own words, asks to change
bank details or buy gift cards and nothing vouches for them. Weaker signals add
up to a "caution" note that does not stop work. Quoted threads, legal footers and
"we will never change our bank details by email" notices do not count as a
request. Every check and verdict goes to the fraud log, and verdicts adjust how
much each signal counts next time.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from controller_inbox.classify import AUTOMATED_SENDERS, PAYMENT_CHANGE_RE, own_words

if TYPE_CHECKING:
    from controller_inbox.config import Settings
    from controller_inbox.models import EmailRecord
    from controller_inbox.store import Store

POINTS = {
    "bank_change": 50,
    "bank_change_quoted": 10,
    "bank_change_attachment": 20,
    "gift_cards": 35,
    "account_numbers": 12,
    "payment_request": 5,
    "reply_to_mismatch": 25,
    "lookalike_domain": 40,
    "display_name_spoof": 25,
    "freemail_request": 10,
    "pressure": 10,
    "first_contact": 10,
    "model_said_bank_change": 12,
    "reported": 60,
    "automated_sender": -20,
    "trusted_domain": -30,
    "trusted_sender": -60,
}

LABELS = {
    "bank_change": "Asks to change bank or payment details",
    "bank_change_quoted": "Bank-change wording in the quoted thread, not the sender's own words",
    "bank_change_attachment": "Attachment mentions changed bank details",
    "gift_cards": "Asks for gift cards",
    "account_numbers": "Gives account or routing numbers",
    "payment_request": "Asks for a payment or wire",
    "reply_to_mismatch": "Replies go to a different domain",
    "lookalike_domain": "Sender domain looks like a known one",
    "display_name_spoof": "Display name borrowed from someone else",
    "freemail_request": "Money request from a free email account",
    "pressure": "Urgency or secrecy",
    "first_contact": "First email from this address",
    "model_said_bank_change": "The local model read a bank-detail change",
    "reported": "You reported this sender as fraud",
    "automated_sender": "Automated sender",
    "trusted_domain": "Trusted domain",
    "trusted_sender": "Sender you trusted",
}

# A bank-change request in the sender's own words, or gift cards, is what can block an email.
CORE = {"bank_change", "gift_cards"}
SOFT_BANK = {"bank_change_quoted", "bank_change_attachment", "model_said_bank_change"}
# Without one of these there is nothing to steal, so other oddities stay quiet.
CONTEXT = CORE | SOFT_BANK | {"account_numbers", "payment_request", "lookalike_domain", "display_name_spoof", "reported"}
LEARNABLE = {key for key, points in POINTS.items() if points > 0 and key != "reported"}

HIGH_AT = 50
CAUTION_AT = 20

FREEMAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
    "aol.com", "icloud.com", "me.com", "proton.me", "protonmail.com", "gmx.com", "gmx.net", "mail.com",
    "yandex.com", "zoho.com", "zohomail.com", "fastmail.com", "tutanota.com",
}

# A warning that frames the sentence as hypothetical ("we will never…", "if you receive…").
STRONG_NOTICE_RE = re.compile(
    r"(\bnever\s+(?:change|ask|request|send|update|contact|email)\b|\b(?:will\s+not|won'?t)\s+(?:change|ask|request)\b|"
    r"\bif\s+you\s+(?:receive|get|are\s+contacted)\b|"
    r"\b(?:scam|phishing|fraudulent|spoofed)\s+(?:e-?mails?|messages?|requests?|calls?)\b)",
    re.I,
)
# A warning word that can just as well introduce a real request ("please be aware our bank details have changed").
WEAK_NOTICE_RE = re.compile(r"(\bbeware\b|\bbe\s+(?:aware|alert|vigilant)\b|\balways\s+(?:call|verify|confirm)\b)", re.I)
# What a real warning tells the reader to do.
PROTECTIVE_RE = re.compile(
    r"\b(?:call|phone|telephone|verify|verbally|contact\s+(?:us|your)|known\s+number|on\s+file|report|ignore|delete)\b", re.I
)
# A colleague saying the quoted request was fake.
DISAVOW_RE = re.compile(
    r"\b(?:(?:was|is|it'?s)\s+not\s+(?:them|legit\w*|genuine|real)|(?:wasn'?t|isn'?t)\s+(?:them|legit\w*|genuine|real)|"
    r"(?:is|was|looks\s+like|seems\s+like)\s+(?:a\s+)?(?:scam|phish\w*|fake|spoof\w*)|phishing|"
    r"blocked\s+(?:the|this)\s+sender|reported\s+(?:it|this|the\s+sender))\b",
    re.I,
)
GIFT_RE = re.compile(
    r"(?:\b(?:(?:itunes|apple|google\s+play|steam|amazon|visa|target|walmart|ebay|best\s*buy)\s+)?gift\s*-?\s*cards?\b|"
    r"\b(?:itunes|google\s+play|steam)\s+cards?\b)(?![^.\n]{0,40}\b(?:program|policy|balance)\b)",
    re.I,
)
# Gift cards only count when someone is asked to get them or hand over their codes.
GIFT_ASK_RE = re.compile(
    r"\b(?:buy|purchase|pick\s+up|get\s+(?:me|us|some|them|a\s+few)|grab|send\s+(?:me|us)|need|scratch|codes?|card\s+numbers|pins?)\b",
    re.I,
)
ACCOUNT_RE = re.compile(
    r"(\b(?:routing|aba|account|acct|iban|swift|bic|sort\s+code)\b[\s#:.no]{0,12}(?:\*{2,}|[x•]{2,})?[\dA-Z][\d\- ]{5,}|"
    r"\b(?:routing|account|iban)\s+\*{4}\d{4}\b|\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b)",
    re.I,
)
PAYMENT_ASK_RE = re.compile(
    r"\b(?:process|send|release|make|initiate|pay|wire|transfer|remit|expedite)\b[^.\n]{0,60}"
    r"\b(?:wire|payment|invoice|funds|transfer|ach|balance|\$\s?\d)",
    re.I,
)
PRESSURE_RE = re.compile(
    r"\b(?:urgent(?:ly)?|asap|immediately|right\s+away|today|within\s+the\s+hour|before\s+(?:noon|eod|cob)|"
    r"confidential|keep\s+this\s+(?:between\s+us|quiet)|don'?t\s+(?:tell|mention|call)|"
    r"in\s+(?:a\s+)?meetings?|can'?t\s+(?:talk|take\s+calls?)|quick\s+favou?r)\b",
    re.I,
)
REPLY_PREFIX_RE = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|wg|tr)\s*:\s*)+", re.I)
EMBEDDED_ADDRESS_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_HOMOGLYPHS = (("rn", "m"), ("vv", "w"), ("cl", "d"), ("0", "o"), ("1", "l"), ("5", "s"))


@dataclass
class Signal:
    key: str
    points: int
    detail: str = ""

    @property
    def label(self) -> str:
        return LABELS.get(self.key, self.key.replace("_", " "))


@dataclass
class FraudCheck:
    score: int
    level: str
    signals: list[Signal] = field(default_factory=list)
    trust: str = ""
    verdict: str = ""

    @property
    def flags(self) -> list[str]:
        if self.level == "high":
            return ["fraud_risk", "do_not_process"]
        if self.level == "caution":
            return ["payment_caution"]
        return []

    def signal_dicts(self) -> list[dict]:
        return [{**asdict(item), "label": item.label} for item in self.signals]


@dataclass
class TrustContext:
    domains: set[str] = field(default_factory=set)
    senders: dict[str, str] = field(default_factory=dict)
    fraud_domains: set[str] = field(default_factory=set)
    known: dict[str, int] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)


def domain_of(address: str) -> str:
    address = (address or "").strip().lower()
    return address.rsplit("@", 1)[1].strip(">. ") if "@" in address else ""


def domain_matches(domain: str, trusted: set[str] | list[str]) -> str:
    """The trusted entry covering ``domain`` (itself or a parent domain), or ""."""
    for item in trusted:
        if domain == item or domain.endswith("." + item):
            return item
    return ""


def trust_context(store: "Store", settings: "Settings") -> TrustContext:
    ctx = TrustContext(domains=set(settings.trusted_domain_list))
    for row in store.trust_entries():
        if row["kind"] == "domain":
            (ctx.domains if row["verdict"] == "safe" else ctx.fraud_domains).add(row["value"])
        elif row["kind"] == "sender":
            ctx.senders[row["value"]] = row["verdict"]
    ctx.fraud_domains -= ctx.domains
    ctx.known = dict(store.sender_domains(limit=200))
    ctx.names = store.names_at_domains(ctx.domains)
    ctx.weights = learned_weights(store)
    return ctx


def learned_weights(store: "Store") -> dict[str, float]:
    """How much each signal counts, nudged by every "fraud" / "not fraud" verdict the user gave."""
    tally: dict[str, list[int]] = {}
    for row in store.fraud_log(limit=2000, events=("marked_fraud", "marked_safe")):
        for item in row["signals"]:
            key = item.get("key") if isinstance(item, dict) else None
            if key in LEARNABLE:
                counts = tally.setdefault(key, [0, 0])
                counts[0 if row["event"] == "marked_fraud" else 1] += 1
    weights = {}
    for key, (fraud_n, safe_n) in tally.items():
        # A bank-change or gift-card request always blocks; "not fraud" answers never weaken it.
        floor = 1.0 if key in CORE else 0.5
        weights[key] = round(max(floor, min(1.6, 1 + 0.15 * (fraud_n - safe_n))), 2)
    return weights


def _asks(text: str) -> bool:
    """The sentence itself asks for a bank change or gift cards."""
    return bool(PAYMENT_CHANGE_RE.search(text) or _gift_ask(text))


def _is_notice(sentence: str) -> bool:
    strong = STRONG_NOTICE_RE.search(sentence)
    if strong:
        # "If you receive an email saying our bank details have changed, call us" is a warning;
        # "we will never ask for gift cards, but our bank details have changed" is a request.
        return not _asks(sentence) or bool(PROTECTIVE_RE.search(STRONG_NOTICE_RE.sub(" ", sentence)))
    # "Beware of scams" is a notice; "please be aware our bank details have changed" is not.
    return bool(WEAK_NOTICE_RE.search(sentence)) and not _asks(sentence)


def strip_notices(text: str) -> str:
    """Drop anti-fraud notices ("we will never change our bank details by email") before looking for a request.

    Only warnings are dropped: a sentence that asserts a change ("please be aware our banking
    details have changed") stays, whatever warning word it starts with.
    """
    parts = re.split(r"(?<=[.!?])\s+|\n+", text or "")
    return " ".join(part for part in parts if not _is_notice(part))


def _gift_ask(text: str) -> re.Match[str] | None:
    """A gift-card mention in a sentence that asks someone to buy them or send their codes."""
    for match in GIFT_RE.finditer(text or ""):
        start = max(text.rfind(".", 0, match.start()), text.rfind("\n", 0, match.start())) + 1
        ends = [i for i in (text.find(".", match.end()), text.find("\n", match.end())) if i >= 0]
        if GIFT_ASK_RE.search(text[start : min(ends) if ends else len(text)]):
            return match
    return None


def assess(
    ctx: TrustContext,
    *,
    subject: str,
    body: str,
    sender_name: str,
    sender_email: str,
    reply_to: str = "",
    attachments: list[tuple[str, str]] | None = None,
    history: int = 0,
    flags: list[str] | tuple[str, ...] = (),
) -> FraudCheck:
    sender = (sender_email or "").strip().lower()
    domain = domain_of(sender)
    signals: list[Signal] = []

    def add(key: str, detail: str = "") -> None:
        points = POINTS[key]
        if points > 0:
            points = round(points * ctx.weights.get(key, 1.0))
        signals.append(Signal(key, points, detail))

    is_reply = bool(REPLY_PREFIX_RE.match(subject or ""))
    mine = strip_notices(own_words(body))
    own = mine if is_reply else f"{subject or ''}. {mine}"
    everything = strip_notices(f"{subject or ''}\n{body or ''}")
    # The sender's words with legal-footer paragraphs kept: a request tucked into a
    # "this email is confidential" paragraph is still the sender's request.
    mine_full = strip_notices(own_words(body, keep_disclaimers=True))
    own_full = mine_full if is_reply else f"{subject or ''}. {mine_full}"

    text = own
    match = PAYMENT_CHANGE_RE.search(own)
    if not match:
        text = own_full
        match = PAYMENT_CHANGE_RE.search(own_full)
    if match:
        add("bank_change", _quote(text, match))
    else:
        quoted = PAYMENT_CHANGE_RE.search(everything)
        if quoted:
            add("bank_change_quoted", _quote(everything, quoted))
        for filename, text in attachments or []:
            hit = PAYMENT_CHANGE_RE.search(strip_notices((text or "")[:60_000]))
            if hit:
                add("bank_change_attachment", filename)
                break
    gift = _gift_ask(own) or _gift_ask(own_full)
    if gift:
        add("gift_cards", _quote(gift.string, gift))
    account = ACCOUNT_RE.search(own)
    if account:
        add("account_numbers", _quote(own, account))
    ask = PAYMENT_ASK_RE.search(own)
    if ask:
        add("payment_request", _quote(own, ask))
    pressure = PRESSURE_RE.search(own)
    if pressure:
        add("pressure", pressure.group(0))
    if "model_said_bank_change" in flags:
        add("model_said_bank_change")

    keys = {item.key for item in signals}
    if domain in FREEMAIL and keys & (CORE | SOFT_BANK | {"payment_request", "account_numbers"}):
        add("freemail_request", domain)
    if history == 0 and keys & (CORE | {"bank_change_attachment"}) and sender:
        add("first_contact", sender)

    reply_domain = domain_of(reply_to)
    if (
        reply_domain
        and domain
        and reply_domain != domain
        and not reply_domain.endswith("." + domain)
        and not domain.endswith("." + reply_domain)
        and not domain_matches(reply_domain, ctx.domains)
    ):
        add("reply_to_mismatch", reply_to.lower())

    trusted_domain = domain_matches(domain, ctx.domains) if domain else ""
    if domain and not trusted_domain:
        look = _lookalike(domain, ctx)
        if look:
            add("lookalike_domain", f"{domain} looks like {look}")
    spoof = _display_name_spoof(sender_name, sender, domain, trusted_domain, ctx)
    if spoof:
        add("display_name_spoof", spoof)

    if any(needle in sender for needle in AUTOMATED_SENDERS):
        add("automated_sender")
    verdict_for_sender = ctx.senders.get(sender, "")
    reported = verdict_for_sender == "fraud" or (domain and domain_matches(domain, ctx.fraud_domains))
    trust = ""
    if reported:
        add("reported", sender if verdict_for_sender == "fraud" else domain)
    elif verdict_for_sender == "safe":
        add("trusted_sender", sender)
        trust = "sender"
    elif trusted_domain:
        add("trusted_domain", trusted_domain)
        trust = "domain"

    score = max(0, sum(item.points for item in signals))
    keys = {item.key for item in signals}
    verdict = "fraud" if "fraud_confirmed" in flags else "safe" if "fraud_cleared" in flags else ""
    if verdict == "fraud" or "reported" in keys:
        level = "high"
    elif verdict == "safe":
        level = "none"
    elif keys & CORE:
        # The sender asks, in their own words, to change bank details or buy gift cards: that blocks
        # unless you trust them. From someone trusted it is still worth a phone call.
        level = "high" if not trust or score >= HIGH_AT else "caution"
    elif keys & SOFT_BANK and score >= HIGH_AT + 10:
        level = "high"
    elif score >= CAUTION_AT and keys & CONTEXT:
        level = "caution"
    elif "bank_change_quoted" in keys and not trust and not DISAVOW_RE.search(mine):
        # Bank-change wording below a quote marker ("From:", ">") can be a forged thread.
        # Only a colleague saying it was fake ("it was not them") keeps it quiet.
        level = "caution"
    else:
        level = "none"
    return FraudCheck(score=score, level=level, signals=signals, trust=trust, verdict=verdict)


def assess_email(store: "Store", ctx: TrustContext, email: "EmailRecord") -> FraudCheck:
    return assess(
        ctx,
        subject=email.subject,
        body=email.body_text,
        sender_name=email.sender_name,
        sender_email=email.sender_email,
        reply_to=email.reply_to,
        attachments=[(att.filename, att.extracted_text) for att in email.attachments],
        history=store.sender_history(email.sender_email, exclude=email.id),
        flags=email.flags,
    )


def attachments_locked(email: "EmailRecord") -> bool:
    """Files on a suspected-fraud email are not opened or given to the model until the user clears it."""
    from controller_inbox.models import DocumentType

    if "fraud_cleared" in email.flags:
        return False
    return "fraud_risk" in email.flags or email.category == DocumentType.PAYMENT_INSTRUCTION_CHANGE


def save_check(store: "Store", email: "EmailRecord", check: FraudCheck, *, now: datetime | None = None, event: str = "") -> None:
    """Remember the check; log it when it flags something new or changes level."""
    at = _stamp(now)
    previous = store.fraud_check(email.id)
    store.save_fraud_check(email.id, check.score, check.level, check.signal_dicts(), at)
    changed = (previous is None and check.level != "none") or (previous is not None and previous["level"] != check.level)
    if event or changed:
        store.log_fraud(
            {
                "at": at,
                "email_id": email.id,
                "event": event or ("flagged" if previous is None else "level_changed"),
                "level": check.level,
                "score": check.score,
                "sender_email": email.sender_email,
                "subject": email.subject,
                "signals": check.signal_dicts(),
                "note": f"was {previous['level']}" if previous and previous["level"] != check.level else "",
            }
        )


def reassess_email(store: "Store", settings: "Settings", email: "EmailRecord", *, ctx: TrustContext | None = None,
                   now: datetime | None = None, event: str = "") -> tuple["EmailRecord", FraudCheck]:
    """Run the check again on a stored email and refile it when its fraud level changed."""
    from controller_inbox.pipeline import rescore_stored

    ctx = ctx or trust_context(store, settings)
    check = assess_email(store, ctx, email)
    was_high = "fraud_risk" in email.flags
    was_caution = "payment_caution" in email.flags
    if (check.level == "high") != was_high:
        email = rescore_stored(store, settings, email, check)
    elif (check.level == "caution") != was_caution:
        email.flags = [flag for flag in email.flags if flag != "payment_caution"] + check.flags
        store.upsert_email(email)
    save_check(store, email, check, now=now, event=event)
    return email, check


def record_fraud_verdict(
    store: "Store",
    settings: "Settings",
    email_id: str,
    *,
    verdict: str,
    scope: str = "email",
    note: str = "",
    now: datetime | None = None,
) -> dict:
    """The user says an email is (or is not) fraud, for this email, its sender, or the sender's domain."""
    if verdict not in {"safe", "fraud"}:
        raise ValueError("Verdict must be 'safe' or 'fraud'.")
    if scope not in {"email", "sender", "domain"}:
        raise ValueError("Scope must be 'email', 'sender', or 'domain'.")
    email = store.get_email(email_id)
    if email is None:
        raise KeyError(email_id)
    sender = (email.sender_email or "").lower()
    domain = domain_of(sender)
    if scope == "sender" and not sender:
        raise ValueError("This email has no sender address to remember.")
    if scope == "domain":
        if not domain:
            raise ValueError("This email has no sender domain to remember.")
        if domain in FREEMAIL:
            raise ValueError(f"{domain} is a free email service anyone can use. Trust the sender's address instead.")
    at = _stamp(now)
    before = store.fraud_check(email.id)
    ctx = trust_context(store, settings)
    signals = before["signals"] if before else assess_email(store, ctx, email).signal_dicts()

    keep, drop = ("fraud_cleared", "fraud_confirmed") if verdict == "safe" else ("fraud_confirmed", "fraud_cleared")
    email.flags = [flag for flag in email.flags if flag != drop]
    if keep not in email.flags:
        email.flags.append(keep)
    store.upsert_email(email)
    if scope != "email":
        value = sender if scope == "sender" else domain
        store.set_trust(scope, value, verdict, source="you", note=note[:300], at=at)
    store.log_fraud(
        {
            "at": at,
            "email_id": email.id,
            "event": "marked_safe" if verdict == "safe" else "marked_fraud",
            "level": before["level"] if before else "",
            "score": before["score"] if before else 0,
            "sender_email": sender,
            "subject": email.subject,
            "signals": signals,
            "note": f"scope: {scope}" + (f" — {note.strip()[:300]}" if note.strip() else ""),
        }
    )

    ctx = trust_context(store, settings)
    targets = [email.id]
    if scope == "sender":
        targets += store.email_ids_from(sender=sender)
    elif scope == "domain":
        targets += store.email_ids_from(domain=domain)
    changed = 0
    for target in dict.fromkeys(targets):
        record = store.get_email(target)
        if record is None:
            continue
        level_before = "high" if "fraud_risk" in record.flags else "caution" if "payment_caution" in record.flags else "none"
        _, check = reassess_email(store, settings, record, ctx=ctx, now=now)
        changed += check.level != level_before
    email = store.get_email(email.id) or email
    return {"email": email, "check": store.fraud_check(email.id), "changed": changed, "scope": scope, "verdict": verdict}


def set_domain_trust(store: "Store", settings: "Settings", domain: str, *, verdict: str = "safe", note: str = "",
                     remove: bool = False, now: datetime | None = None) -> int:
    """Add or remove a trusted (or reported) domain from the fraud page, then recheck its mail."""
    domain = domain.strip().lower().lstrip("@").strip(".")
    if "." not in domain or not re.fullmatch(r"[a-z0-9.-]+", domain):
        raise ValueError("Enter a domain such as taz.com.")
    if not remove and verdict == "safe" and domain in FREEMAIL:
        raise ValueError(f"{domain} is a free email service anyone can use. Trust individual senders instead.")
    at = _stamp(now)
    if remove:
        store.remove_trust("domain", domain)
    else:
        store.set_trust("domain", domain, verdict, source="you", note=note[:300], at=at)
    store.log_fraud(
        {
            "at": at,
            "event": "untrusted" if remove else ("trusted" if verdict == "safe" else "reported"),
            "sender_email": "@" + domain,
            "note": note.strip()[:300],
        }
    )
    ctx = trust_context(store, settings)
    for email_id in store.email_ids_from(domain=domain):
        record = store.get_email(email_id)
        if record is not None:
            reassess_email(store, settings, record, ctx=ctx, now=now)
    return len(store.email_ids_from(domain=domain))


def remove_sender_trust(store: "Store", settings: "Settings", sender: str, *, now: datetime | None = None) -> None:
    sender = sender.strip().lower()
    store.remove_trust("sender", sender)
    store.log_fraud({"at": _stamp(now), "event": "untrusted", "sender_email": sender})
    ctx = trust_context(store, settings)
    for email_id in store.email_ids_from(sender=sender):
        record = store.get_email(email_id)
        if record is not None:
            reassess_email(store, settings, record, ctx=ctx, now=now)


def suggested_domains(store: "Store", settings: "Settings", *, limit: int = 8) -> list[tuple[str, int]]:
    """Frequent sender domains nobody has trusted or reported yet (free-mail and flagged senders left out)."""
    ctx = trust_context(store, settings)
    flagged = {domain_of(row["sender_email"]) for row in store.flagged(limit=1000)}
    out = []
    for domain, count in store.sender_domains(limit=60):
        if domain in FREEMAIL or domain in flagged or domain_matches(domain, ctx.domains) or domain_matches(domain, ctx.fraud_domains):
            continue
        out.append((domain, count))
    return out[:limit]


def log_csv(store: "Store", *, limit: int = 5000) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["at", "event", "level", "score", "sender", "subject", "signals", "note", "email_id"])
    for row in store.fraud_log(limit=limit):
        signals = "; ".join(
            f"{item['key']} {int(item.get('points') or 0):+}" for item in row["signals"] if isinstance(item, dict) and item.get("key")
        )
        writer.writerow(
            [_cell(row.get(name)) for name in ("at", "event", "level")]
            + [row.get("score") if row.get("level") else ""]
            + [_cell(row.get(name)) for name in ("sender_email", "subject")]
            + [signals, _cell(row.get("note")), row.get("email_id") or ""]
        )
    return buf.getvalue()


def _cell(value) -> str:
    """Senders write the subject; keep a spreadsheet from running it as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in {"=", "+", "-", "@", "\t", "\r"} else text


def _lookalike(domain: str, ctx: TrustContext) -> str:
    mine = ctx.known.get(domain, 0)
    candidates = [(item, True) for item in ctx.domains] + [
        (item, False) for item, count in ctx.known.items() if count > max(mine, 1)
    ]
    for other, trusted in candidates:
        if other == domain or domain.endswith("." + other) or other.endswith("." + domain):
            continue
        if _skeleton(domain) == _skeleton(other):
            return other
        limit = 2 if len(other) >= 10 else 1
        if len(other.split(".")[0]) >= 4 and _distance(domain, other, limit) <= limit:
            return other
        label = other.split(".")[0]
        tokens = re.split(r"[.-]", domain.rsplit(".", 1)[0])
        if trusted and len(label) >= 3 and label in tokens:
            return other
    return ""


def _display_name_spoof(name: str, sender: str, domain: str, trusted_domain: str, ctx: TrustContext) -> str:
    name = (name or "").strip()
    embedded = EMBEDDED_ADDRESS_RE.search(name)
    if embedded and embedded.group(0).lower() != sender:
        return f"name shows {embedded.group(0).lower()}"
    if trusted_domain or not domain:
        return ""
    seen = ctx.names.get(name.lower())
    if seen and domain_of(seen) != domain and len(name) >= 5:
        return f"“{name}” usually writes from {seen}"
    return ""


def _skeleton(domain: str) -> str:
    text = domain.lower()
    for old, new in _HOMOGLYPHS:
        text = text.replace(old, new)
    return text


def _distance(a: str, b: str, limit: int) -> int:
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def _quote(text: str, match: re.Match[str]) -> str:
    start = max(0, match.start() - 40)
    snippet = " ".join(text[start : match.end() + 40].split())
    return ("…" if start else "") + snippet[:160]


def _stamp(now: datetime | None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0).isoformat()
