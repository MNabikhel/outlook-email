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
import unicodedata
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from typing import TYPE_CHECKING

from controller_inbox.classify import (
    AUTOMATED_SENDERS,
    PAYMENT_CHANGE_RE,
    QUOTE_START_RE,
    normalize_text,
    normalize_with_spans,
    own_words,
)

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

# Providers that hand out mailboxes under their name in many countries: yahoo.co.uk, hotmail.fr, outlook.com.br, gmx.de.
_FREEMAIL_PROVIDERS = {
    "gmail", "googlemail", "yahoo", "ymail", "rocketmail", "outlook", "hotmail", "live", "msn", "windowslive", "aol",
    "gmx", "yandex",
}
# What follows the provider's name: a country code ("fr"), or "com", "net", "org" or "co", maybe with one ("co.uk").
_FREEMAIL_TAIL_RE = re.compile(r"(?:(?:com|net|org|co)(?:\.[a-z]{2})?|[a-z]{2})")


class _FreeMail(frozenset):
    """The free-mail and ISP domains, where anyone can get an address. ``domain in FREEMAIL`` also covers a known
    provider under any country ending (``yahoo.co.uk``, ``hotmail.fr``), not only the domains listed."""

    def __contains__(self, domain: object) -> bool:
        if not isinstance(domain, str):
            return False
        domain = domain.strip().lower().strip(".")
        if frozenset.__contains__(self, domain):
            return True
        label, _, tail = domain.partition(".")
        return label in _FREEMAIL_PROVIDERS and bool(_FREEMAIL_TAIL_RE.fullmatch(tail))


FREEMAIL = _FreeMail({
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com", "hotmail.com", "live.com", "msn.com",
    "aol.com", "icloud.com", "me.com", "mac.com", "proton.me", "protonmail.com", "pm.me", "gmx.com", "gmx.net",
    "mail.com", "yandex.com", "zoho.com", "zohomail.com", "fastmail.com", "tutanota.com", "tuta.io", "hey.com",
    # Country mail services.
    "web.de", "t-online.de", "freenet.de", "orange.fr", "wanadoo.fr", "free.fr", "laposte.net", "sfr.fr", "libero.it",
    "virgilio.it", "mail.ru", "inbox.ru", "rambler.ru", "qq.com", "163.com", "126.com", "naver.com", "rediffmail.com",
    "seznam.cz", "wp.pl", "o2.pl", "interia.pl",
    # Internet providers' mailboxes.
    "btinternet.com", "sky.com", "virginmedia.com", "talktalk.net", "ntlworld.com", "comcast.net", "att.net",
    "verizon.net", "sbcglobal.net", "bellsouth.net", "cox.net", "charter.net", "earthlink.net", "optonline.net",
    "frontier.com", "rogers.com", "shaw.ca", "sympatico.ca", "bigpond.com", "optusnet.com.au",
})

# A warning that frames the sentence as hypothetical ("we will never…", "if you receive…"), including
# the usual footer "we will never notify you of a change to our bank details by email".
_NEVER_DO = r"(?:change|ask|request|send|update|contact|email|notify|inform|tell|advise|alert)"
STRONG_NOTICE_RE = re.compile(
    r"(\bnever\s+" + _NEVER_DO + r"\b|\b(?:will\s+not|won'?t)\s+" + _NEVER_DO + r"\b|"
    # Not "if you get a chance, buy gift cards": that is a request, said politely.
    r"\bif\s+you\s+(?:receive|get(?!\s+(?:a\s+)?(?:chance|moment|minute|second|sec)\b)|are\s+contacted)\b|"
    r"\bif\s+(?:anyone|anybody|someone|somebody)\s+(?:tells?|says?|claims?|emails?|calls?|contacts?)\b|"
    r"\b(?:scam|phishing|fraudulent|spoofed)\s+(?:e-?mails?|messages?|requests?|calls?)\b)",
    re.I,
)
# A warning word that can just as well introduce a real request ("please be aware our bank details have changed").
WEAK_NOTICE_RE = re.compile(r"(\bbeware\b|\bbe\s+(?:aware|alert|vigilant)\b|\balways\s+(?:call|verify|confirm)\b)", re.I)
# Where one part of a sentence ends and the next begins ("…, but our bank details have changed"). A "please"
# starts a new part even without a comma: "if you receive our next invoice please pay it to our new account"
# asks for the change itself. Not "asked to please update", which still reports the hypothetical message.
CLAUSE_RE = re.compile(
    r"[,;:]|\s[-–—]+\s|\s(?=(?:but|however|although|though|yet|whereas)\b)|(?<!\bto)\s(?=(?:please|kindly)\b)", re.I
)
# Words that report what a hypothetical message says ("…, claiming to be us, saying our bank details have changed").
REPORTING_RE = re.compile(
    r"\b(?:say(?:s|ing)?|said|claim(?:s|ed|ing)?|stat(?:es|ed|ing)|advis(?:es|ed|ing)|notif(?:y|ies|ied|ying)|"
    r"inform(?:s|ed|ing)?|tell(?:s|ing)?|told|ask(?:s|ed|ing)?|request(?:s|ed|ing)?|purport(?:s|ed|ing)?|"
    r"suggest(?:s|ed|ing)?|indicat(?:es|ed|ing)|alert(?:s|ed|ing)?)\b",
    re.I,
)
# A part of the sentence that turns away from the warning: what to do about it ("…, please call us") or a
# contrast ("…, but our bank details have changed"). A warning's reach ends there.
TURN_RE = re.compile(
    r"\s*(?:(?:and|so|then)\s+)?(?:but|however|although|though|yet|whereas|(?:please\s+|kindly\s+)?(?:call|phone|telephone|"
    r"ring|contact|verify|confirm|check|ignore|delete|report|forward|speak|talk|do\s+not|don'?t|let\s+us\s+know))\b",
    re.I,
)
# What someone else's message claims, told as a warning: "emails claiming", "fraudsters may … claim",
# "criminals are sending emails purporting to come from us stating". It must lead straight to the change, and
# not be the sender's own message ("as our letter said our bank details have changed" is the sender's claim).
HEARSAY_RE = re.compile(
    r"(?<!\bour )(?<!\bmy )\b(?:e-?mails?|messages?|letters?|calls?|texts?|requests?|notifications?|anyone|anybody|someone|somebody|"
    r"fraudsters?|criminals?|scammers?|imp[oe]stors?)\b[^.,;:!?\n]{0,60}?"
    r"\b(?:claim(?:s|ed|ing)?|say(?:s|ing)?|said|stat(?:es|ed|ing)|purport(?:s|ed|ing)?|tell(?:s|ing)?|told|"
    r"pretend(?:s|ed|ing)?|suggest(?:s|ed|ing)?|advis(?:es|ed|ing)|notif(?:y|ies|ied|ying)|inform(?:s|ed|ing)?)"
    r"(?:\s+(?:you|us|to\s+you|to\s+us|that))*(?:\s+(?:our|the|their|its|my))?\s*$",
    re.I,
)
# Words that make a sentence a warning about fraud, so reported claims in it are not the sender's own.
FRAUD_WORD_RE = re.compile(r"\b(?:fraud\w*|criminals?|scam\w*|phish\w*|imperson\w*|imp[oe]stors?|spoof\w*)\b", re.I)
# A change spoken of in general: "any notification of a change of bank details", "all requests to update".
ANY_CHANGE_RE = re.compile(
    r"\b(?:any|all|every)\s+(?:notifications?|notices?|requests?|e-?mails?|messages?|letters?|calls?|instructions?)\s+"
    r"(?:of|about|regarding|for|to|asking\s+(?:you\s+)?to)\s+(?:a\s+)?$",
    re.I,
)
# A colleague saying the quoted request was fake (not "this is not phishing", which vouches for it).
DISAVOW_RE = re.compile(
    r"\b(?:(?:was|is|it'?s)\s+not\s+(?:them|legit\w*|genuine|real)|(?:wasn'?t|isn'?t)\s+(?:them|legit\w*|genuine|real)|"
    r"(?:is|was|looks\s+like|seems\s+like)\s+(?:a\s+)?(?:scam|phish\w*|fake|spoof\w*)|"
    r"(?<!not )(?<!n't )(?<!not a )(?<!no )phishing|"
    r"blocked\s+(?:the|this)\s+sender|reported\s+(?:it|this|the\s+sender))\b",
    re.I,
)
# "eGift cards" and "e-gift cards" are gift cards too.
GIFT_RE = re.compile(
    r"(?:\b(?:(?:itunes|apple|google\s+play|steam|amazon|visa|target|walmart|ebay|best\s*buy)\s+)?(?:e-?)?gift\s*-?\s*cards?\b|"
    r"\b(?:itunes|google\s+play|steam)\s+cards?\b)",
    re.I,
)
# "Our gift card program", "the gift card policy", "your gift card balance": not an ask by themselves.
GIFT_PROGRAM_RE = re.compile(r"[^.\n]{0,40}\b(?:program|policy|balance)\b", re.I)
# ...unless it is asked to buy those cards: the buying word comes just before the mention, in the same part of
# the sentence, and is not "do not purchase" or a "purchase order" ("buy 10 Amazon gift cards for our program")...
GIFT_BUY_BEFORE_RE = re.compile(
    r"(?<!not )(?<!n't )(?<!never )\b(?:buy|purchase(?!\s+orders?\b)|pick\s+up|grab)\b[^.?!;:\n]{0,40}$", re.I
)
# ...or the sentence asks for their codes.
GIFT_SEND_CODES_RE = re.compile(r"(?<!not )(?<!n't )(?<!never )\bsend\b[^.\n]{0,40}\b(?:codes?|card\s+numbers|pins?)\b", re.I)
# Gift cards only count when someone is asked to get them or hand over their codes. Not "employees may not
# purchase gift cards" or "never buy gift cards for anyone who emails you".
GIFT_ASK_RE = re.compile(
    r"(?<!not )(?<!n't )(?<!never )"
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

    def __post_init__(self) -> None:
        self.names = name_index(self.names)


def name_key(name: str) -> str:
    """A display name as the fraud check reads it: "José García" and "Jose Garcia", or "O’Brien" and
    "O'Brien", are the same name."""
    return " ".join(normalize_text(name or "").lower().split())


def name_index(names: dict[str, str]) -> dict[str, str]:
    """Display names seen from trusted domains, keyed the way the fraud check reads a sender's name."""
    return {name_key(name): address for name, address in names.items() if name_key(name)}


def domain_of(address: str) -> str:
    address = (address or "").strip().lower()
    return address.rsplit("@", 1)[1].strip(">. ") if "@" in address else ""


@lru_cache(maxsize=4096)
def canonical_domain(domain: str) -> str:
    """One spelling of a domain, so "müller.de" and its punycode form "xn--mller-kva.de" are the same domain:
    lower case, with each punycode label read as the letters it stands for."""
    labels = []
    for label in (domain or "").strip().lower().split("."):
        if label.startswith("xn--"):
            try:
                label = label.encode("ascii").decode("idna")
            except UnicodeError:
                pass
        labels.append(unicodedata.normalize("NFC", label))
    return ".".join(labels)


def same_or_under(domain: str, other: str) -> bool:
    """``domain`` is ``other`` or one of its subdomains, however either is spelled (Unicode or punycode)."""
    domain, other = canonical_domain(domain), canonical_domain(other)
    return domain == other or domain.endswith("." + other)


def domain_matches(domain: str, trusted: set[str] | list[str]) -> str:
    """The trusted entry covering ``domain`` (itself or a parent domain), or ""."""
    for item in trusted:
        if same_or_under(domain, item):
            return item
    return ""


def trust_context(store: "Store", settings: "Settings") -> TrustContext:
    # Not a free-mail domain, though: a mailbox at gmail.com does not make every gmail.com sender a colleague.
    ctx = TrustContext(domains={domain for domain in settings.trusted_domain_list if domain not in FREEMAIL})
    for row in store.trust_entries():
        if row["kind"] == "domain":
            (ctx.domains if row["verdict"] == "safe" else ctx.fraud_domains).add(row["value"])
        elif row["kind"] == "sender":
            ctx.senders[row["value"]] = row["verdict"]
    ctx.fraud_domains -= ctx.domains
    ctx.known = dict(store.sender_domains(limit=200))
    ctx.names = name_index(store.names_at_domains(ctx.domains))
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


def _requests(text: str) -> list[int]:
    """Where in the text a bank change or gift cards are asked for."""
    starts = [match.start() for match in PAYMENT_CHANGE_RE.finditer(text)]
    return starts + [match.start() for match in _gift_asks(text)]


def _is_notice(sentence: str) -> bool:
    if STRONG_NOTICE_RE.search(sentence):
        # A warning covers the words after it, to the end of that part of the sentence:
        # "if you receive an email saying our bank details have changed, call us" is a warning.
        # A change asked for anywhere else is a request, whatever the sentence says after it:
        # "our bank details have changed, pay the new account, and if you receive other instructions call us",
        # "we will never ask for gift cards, but our bank details have changed".
        requests = _requests(sentence)
        if not requests:
            return True
        # Where each part of the sentence ends, found once for every warning in it.
        cuts = list(CLAUSE_RE.finditer(sentence))
        cut_starts = [cut.start() for cut in cuts]
        markers = list(STRONG_NOTICE_RE.finditer(sentence))
        scopes = []
        for marker in markers:
            after = bisect_left(cut_starts, marker.end())
            scopes.append((marker.start(), cut_starts[after] if after < len(cuts) else len(sentence)))
        inside = _spans_test(scopes)
        # A later part of the sentence that reports what the hypothetical message says is still the warning:
        # "if you receive an email from us, or anyone claiming to be us, saying our bank details have
        # changed, please call us". Its reach ends where the sentence turns to what to do, or to a contrast.
        turns = [cut.end() for cut in cuts if TURN_RE.match(sentence, cut.end())]
        reports = list(REPORTING_RE.finditer(sentence))
        report_starts = [report.start() for report in reports]
        marker_ends = [marker.end() for marker in markers]

        def reported(at: int) -> bool:
            before = bisect_left(cut_starts, at)
            part = cuts[before - 1].end() if before else 0
            last = bisect_left(report_starts, at) - 1
            if last < 0 or reports[last].start() < part or reports[last].end() > at:
                return False
            warning = bisect_right(marker_ends, part) - 1
            if warning < 0:
                return False
            turn = bisect_left(turns, marker_ends[warning])
            return not (turn < len(turns) and turns[turn] <= part)

        return all(inside(at) or reported(at) or _spoken_of(sentence, at) for at in requests)
    # "Beware of scams" is a notice; "please be aware our bank details have changed" is not.
    requests = _requests(sentence)
    if not requests:
        return bool(WEAK_NOTICE_RE.search(sentence))
    # "Beware of emails claiming our bank details have changed" and "any notification of a change of bank
    # details must be verified by phone" speak of a change; they do not ask for one.
    return all(_spoken_of(sentence, at) for at in requests)


def _spoken_of(sentence: str, at: int) -> bool:
    """The change asked for at ``at`` is one the sentence speaks of rather than asks for.

    In general terms ("any notification of a change of bank details", "any change of bank details"), or, in a
    sentence that warns of fraud ("beware", "fraudsters"), as what someone else's message claims ("fraudsters may
    send emails claiming our bank details have changed"). Only that part of the sentence counts, so "beware, our
    bank details have changed" still asks.
    """
    starts = [cut.end() for cut in CLAUSE_RE.finditer(sentence, 0, at)]
    part = sentence[starts[-1] if starts else 0 : at]
    if ANY_CHANGE_RE.search(part):
        return True
    if sentence[at : at + 6].lower() == "change" and re.search(r"\b(?:any|all|every)\s+$", part, re.I):
        return True
    warned = WEAK_NOTICE_RE.search(sentence) or FRAUD_WORD_RE.search(sentence)
    return bool(warned and HEARSAY_RE.search(part))


def _spans_test(spans: list[tuple[int, int]]):
    """A test for whether a point falls inside one of the spans (start included, end not)."""
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    starts = [start for start, _end in merged]

    def inside(at: int) -> bool:
        index = bisect_right(starts, at) - 1
        return index >= 0 and at < merged[index][1]

    return inside


def strip_notices(text: str) -> str:
    """Drop anti-fraud notices ("we will never change our bank details by email") before looking for a request.

    Only warnings are dropped: a sentence that asserts a change ("please be aware our banking
    details have changed") stays, whatever warning words it starts or ends with.

    A line that goes on in lower case is the same sentence, wrapped by the sender's mail program
    ("if you receive an email from anyone\\nsaying our bank details have changed, call us"), so it is
    read with the line before it. A new paragraph, or a line that starts afresh, is a part of its own.
    """
    parts = re.split(r"(?<=[.!?])\s+|\n[ \t]*\n\s*|\n(?![ \t]*[a-z])", text or "")
    parts = [re.sub(r"[ \t]*\n[ \t]*", " ", part) for part in parts]
    return " ".join(part for part in parts if not _is_notice(part))


def _gift_ask(text: str) -> re.Match[str] | None:
    """A gift-card mention in a sentence that asks someone to buy them or send their codes."""
    found = _gift_asks(text)
    return found[0] if found else None


def _gift_asks(text: str) -> list[re.Match[str]]:
    """Every gift-card mention in a sentence that asks someone to buy them or send their codes.

    Each sentence is read once, however many mentions it has, so a long run of them stays quick.
    """
    mentions = list(GIFT_RE.finditer(text or ""))
    if not mentions:
        return []
    stops = [stop.start() for stop in re.finditer(r"[.\n]", text)]
    read: dict[tuple[int, int], tuple[bool, bool]] = {}
    found = []
    for match in mentions:
        before = bisect_left(stops, match.start())
        after = bisect_left(stops, match.end())
        span = (stops[before - 1] + 1 if before else 0, stops[after] if after < len(stops) else len(text))
        if span not in read:
            sentence = text[span[0] : span[1]]
            read[span] = (bool(GIFT_ASK_RE.search(sentence)), bool(GIFT_SEND_CODES_RE.search(sentence)))
        asks, sends = read[span]
        # "Buy 10 gift cards for our staff rewards program and send me the codes" is still an ask; "thank you for
        # your gift card program purchase" and "our gift card policy: do not purchase them" are not.
        if GIFT_PROGRAM_RE.match(text, match.end()) and not sends:
            lead = text[max(span[0], match.start() - 60) : match.start()]
            if not GIFT_BUY_BEFORE_RE.search(lead):
                continue
        if asks:
            found.append(match)
    return found


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
    domain_history: int = 0,
) -> FraudCheck:
    """``history`` counts earlier mail from this address, ``domain_history`` earlier mail from its domain."""
    sender = (sender_email or "").strip().lower()
    domain = domain_of(sender)
    signals: list[Signal] = []
    # A zero-width space, an empty <span> or a Cyrillic "a" inside "bank" still reads as "bank". What the
    # user is shown as evidence is quoted from the email as written.
    evidence = _Evidence(subject, body)
    shown_name = sender_name
    subject, body, sender_name = normalize_text(subject), normalize_text(body), normalize_text(sender_name)

    def add(key: str, detail: str = "") -> None:
        points = POINTS[key]
        if points > 0:
            points = round(points * ctx.weights.get(key, 1.0))
        signals.append(Signal(key, points, detail))

    # A reply's subject repeats the thread it answers, so it is left out of the sender's own words, but
    # only when the body quotes that thread. "RE:" over a body with no quote is just a subject line.
    is_reply = bool(REPLY_PREFIX_RE.match(subject or "")) and bool(QUOTE_START_RE.search(body or ""))
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
        add("bank_change", evidence.quote(text, match))
    else:
        quoted = PAYMENT_CHANGE_RE.search(everything)
        if quoted:
            add("bank_change_quoted", evidence.quote(everything, quoted))
        for filename, text in attachments or []:
            hit = PAYMENT_CHANGE_RE.search(strip_notices(normalize_text((text or "")[:60_000])))
            if hit:
                add("bank_change_attachment", filename)
                break
    gift = _gift_ask(own) or _gift_ask(own_full)
    if gift:
        add("gift_cards", evidence.quote(gift.string, gift))
    account = ACCOUNT_RE.search(own)
    if account:
        add("account_numbers", evidence.quote(own, account))
    ask = PAYMENT_ASK_RE.search(own)
    if ask:
        add("payment_request", evidence.quote(own, ask))
    pressure = PRESSURE_RE.search(own)
    if pressure:
        add("pressure", evidence.words(pressure.group(0)))
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
        and not same_or_under(reply_domain, domain)
        and not same_or_under(domain, reply_domain)
        and not domain_matches(reply_domain, ctx.domains)
    ):
        add("reply_to_mismatch", reply_to.lower())

    trusted_domain = domain_matches(domain, ctx.domains) if domain else ""
    if domain and not trusted_domain:
        look = _lookalike(domain, ctx, established=domain_history > 0)
        if look:
            add("lookalike_domain", f"{domain} looks like {look}")
    spoof = _display_name_spoof(sender_name, sender, domain, trusted_domain, ctx, shown=shown_name)
    if spoof:
        add("display_name_spoof", spoof)

    # Machine mail ("no-reply@") rarely asks for anything, but anyone can pick that address on a domain of
    # their own. So it counts only from a sender you have mail from or a domain you trust, and never
    # against bank-change wording.
    keys = {item.key for item in signals}
    familiar = history > 0 or bool(trusted_domain)
    if any(needle in sender for needle in AUTOMATED_SENDERS) and familiar and not keys & ({"bank_change"} | SOFT_BANK):
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
    elif "bank_change_quoted" in keys and not (trust and DISAVOW_RE.search(mine)):
        # Bank-change wording below a quote marker ("From:", ">") can be a forged thread, and a
        # colleague forwarding a vendor's change still needs a phone call before anyone pays.
        # Only someone you trust saying it was fake ("it was not them") keeps it quiet: anyone
        # can write "this is not phishing" above a forged thread.
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
        domain_history=store.domain_history(domain_of(email.sender_email), exclude=email.id),
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


def _lookalike(domain: str, ctx: TrustContext, *, established: bool = False) -> str:
    """The trusted or frequent domain this one passes for, or "".

    ``established``: you already have mail from this domain. Then being one letter off another name is not
    a sign by itself: with "pnc.com" trusted, your auditor at "pwc.com", or "usps.com" next to "ups.com", is
    a real company of its own. Domains that read the same ("tаz.com" with a Cyrillic "а") still count.
    """
    mine = ctx.known.get(domain, 0)
    candidates = [(item, True) for item in ctx.domains] + [
        (item, False) for item, count in ctx.known.items() if count > max(mine, 1)
    ]
    shown = _skeleton(domain)
    tokens = re.split(r"[.-]", shown.rsplit(".", 1)[0])
    for other, trusted in candidates:
        if same_or_under(domain, other) or same_or_under(other, domain):
            continue
        theirs = _skeleton(other)
        if shown == theirs:
            return other
        # One letter off a name you trust ("tax.com", "tazz.com", "t4z.com" for "taz.com"). A domain you
        # merely hear from a lot needs a longer name, so "pwc.com" is not taken for "pnc.com".
        label = other.split(".")[0]
        limit = 2 if len(other) >= 10 else 1
        if not established and len(label) >= (3 if trusted else 4) and _distance(shown, theirs, limit) <= limit:
            return other
        # A trusted name inside another domain ("taz-payments.net"), both read the same way.
        if trusted and len(label) >= 3 and theirs.split(".")[0] in tokens:
            return other
    return ""


def _display_name_spoof(name: str, sender: str, domain: str, trusted_domain: str, ctx: TrustContext, *,
                        shown: str = "") -> str:
    """``name`` is the display name as the rules read it, ``shown`` as written (for the message)."""
    name = (name or "").strip()
    shown = (shown or name).strip()
    embedded = EMBEDDED_ADDRESS_RE.search(name)
    if embedded and embedded.group(0).lower() != sender:
        written = EMBEDDED_ADDRESS_RE.search(shown)
        return f"name shows {(written or embedded).group(0).lower()}"
    if trusted_domain or not domain:
        return ""
    seen = ctx.names.get(name_key(name))
    if seen and not same_or_under(domain_of(seen), domain) and len(name) >= 5:
        return f"“{shown}” usually writes from {seen}"
    return ""


def _skeleton(domain: str) -> str:
    """How a domain reads on screen: punycode labels ("xn--tz-7kc") decoded, Cyrillic, Greek and full-width
    look-alike letters read as Latin, and pairs that pass for one another ("rn" and "m", "0" and "o") made the same."""
    text = normalize_text(canonical_domain(domain)).lower()
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
    return _snippet(text, match.start(), match.end())


def _snippet(text: str, start: int, end: int) -> str:
    begin = max(0, start - 40)
    snippet = " ".join(text[begin : end + 40].split())
    return ("…" if begin else "") + snippet[:160]


class _Evidence:
    """Quotes for the fraud panel, taken from the subject and body as written.

    The rules read a normalized copy (accents dropped, look-alike letters read as Latin, "¹" as "1"), which
    must not change the figures, names or disguised letters the user is shown. A phrase found in that copy
    is looked up again in the normalized whole and mapped back to the characters it came from.
    """

    def __init__(self, subject: str, body: str):
        # Subject and body read as one text, joined the way the rules join them ("Subject. Body").
        self.text = f"{subject}. {body or ''}" if subject else body or ""
        self._read: tuple[str, list[int], list[int]] | None = None

    def _span(self, found: str) -> tuple[int, int] | None:
        # The quote shows at most 160 characters, so the start of a long match is enough to place it.
        words = found[:200].split()
        if len(found) > 200 and len(words) > 1:
            words.pop()
        if not words:
            return None
        if self._read is None:
            self._read = normalize_with_spans(self.text)
        normalized, starts, ends = self._read
        hit = re.search(r"\s+".join(map(re.escape, words)), normalized)
        if hit is None:
            return None
        return starts[hit.start()], ends[hit.end() - 1]

    def quote(self, text: str, match: re.Match[str]) -> str:
        span = self._span(match.group(0))
        return _snippet(self.text, *span) if span else _quote(text, match)

    def words(self, found: str) -> str:
        span = self._span(found)
        return self.text[span[0] : span[1]] if span else found


def _stamp(now: datetime | None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(microsecond=0).isoformat()
