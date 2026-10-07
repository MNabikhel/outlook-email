from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from controller_inbox.models import DocumentType, ExtractedFields, Importance


@dataclass
class Classification:
    document_type: DocumentType
    confidence: float
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    importance: Importance = Importance.MEDIUM
    importance_score: int = 40
    importance_reasons: list[str] = field(default_factory=list)


@dataclass
class Rule:
    document_type: DocumentType
    weight: int
    keywords: tuple[str, ...] = ()
    regexes: tuple[re.Pattern[str], ...] = ()
    filename_keywords: tuple[str, ...] = ()
    sender_keywords: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    reason: str = ""
    subject_regexes: tuple[re.Pattern[str], ...] = ()
    # Only look at what the sender wrote: no quoted thread, disclaimer or attachment text.
    own_words_only: bool = False


QUOTE_START_RE = re.compile(
    r"^\s*(?:-{2,}\s*(?:original|forwarded)\s+message|_{5,}\s*$|from:\s|sent:\s|on .{6,120} wrote:|>|begin forwarded message)",
    re.I | re.M,
)

DISCLAIMER_RE = re.compile(
    r"(intended\s+(?:only\s+)?(?:for\s+the\s+)?(?:named\s+)?recipient|received\s+this\s+(?:e-?mail|message|communication)\s+in\s+error|"
    r"confidentiality\s+notice|privileged\s+and\s+confidential|may\s+contain\s+(?:confidential|privileged)|"
    r"this\s+(?:e-?mail|message)\s+(?:and\s+any\s+attachments\s+)?(?:is|are|may\s+be)\s+(?:strictly\s+)?confidential|"
    r"originated\s+from\s+outside\s+(?:of\s+)?(?:the|your|our)\s+organi[sz]ation|"
    r"do\s+not\s+click\s+links\s+or\s+open\s+attachments)",
    re.I,
)


# Characters that show nothing but split a word for a pattern ("ba\u200bnk"): the soft hyphen, zero-width
# spaces and joiners, direction marks, the word joiner and the byte-order mark. Then the accents and other
# marks that sit on a letter once it is taken apart ("\u00e9" is "e" and a mark).
_HIDDEN_RE = re.compile(
    "[\u00ad\u034f\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff"
    "\u0300-\u036f\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\ufe20-\ufe2f]"
)
# Formatting tags left in plain text split a word without showing a space ("b<span></span>ank").
_INLINE_TAG_RE = re.compile(
    r"</?(?:a|abbr|b|big|code|del|em|font|i|ins|mark|o:p|s|small|span|strike|strong|sub|sup|u|wbr)(?:\s[^<>]{0,300})?/?>",
    re.I,
)


def strip_html_comments(text: str) -> str:
    """Drop every "<!-- … -->" in one pass over the text. An "<!--" that is never closed stays as text.

    A pattern such as ``<!--.*?-->`` reads to the end of the text from every unclosed "<!--", so a long
    run of them took minutes.
    """
    if "<!--" not in text:
        return text
    out: list[str] = []
    pos = 0
    for start, end in _comment_spans(text):
        out.append(text[pos:start])
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _comment_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    pos = 0
    while True:
        start = text.find("<!--", pos)
        if start < 0:
            break
        end = text.find("-->", start + 4)
        if end < 0:
            break
        spans.append((start, end + 3))
        pos = end + 3
    return spans
# An HTML entity left in text: numbered ("&#8203;", "&#x200b;") or named with its closing ";" ("&amp;").
# A bare "&not" or "&copy" in plain text ("Smith&notary", "Print&copy") is written that way, not an entity.
_ENTITY_RE = re.compile(r"&(?:#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[A-Za-z][A-Za-z0-9]{1,31};)")
# Cyrillic and Greek letters drawn like Latin ones ("b\u0430nk" with a Cyrillic a), curly quotes and dashes.
_LOOKALIKES = str.maketrans(
    {
        **dict(zip("\u0430\u0435\u043e\u0440\u0441\u0443\u0445\u043a\u043c\u043d\u0442\u0455\u0456\u0458\u0501\u051b\u051d\u04bb\u04cf", "aeopcyxkmhtsijdqwhl")),  # Cyrillic
        **dict(zip("\u0410\u0412\u0415\u041a\u041c\u041d\u041e\u0420\u0421\u0422\u0425\u0423\u0405\u0406\u0408\u051a\u051c\u04ba\u04c0", "ABEKMHOPCTXYSIJQWHI")),  # Cyrillic capitals
        **dict(zip("\u03b1\u03b5\u03b9\u03ba\u03bd\u03bf\u03c1\u03c4\u03c5\u03c7\u03c9\u03f2\u03f3", "aeikvoptuxwcj")),  # Greek
        **dict(zip("\u0391\u0392\u0395\u0396\u0397\u0399\u039a\u039c\u039d\u039f\u03a1\u03a4\u03a5\u03a7\u03f9", "ABEZHIKMNOPTYXC")),  # Greek capitals
        **dict(zip("\u0131\u0237\u0251\u0261\u0585\u057d", "ijagou")),  # dotless i and j, Latin alpha and script g, Armenian o and u
        **dict.fromkeys("\u2018\u2019\u201a\u201b\u2032", "'"),  # curly quotes
        **dict.fromkeys("\u201c\u201d\u201e\u201f\u2033", '"'),  # curly double quotes
        **dict.fromkeys("\u2010\u2012\u2013\u2014\u2015\u2212", "-"),  # hyphens, dashes and the minus sign
    }
)


def normalize_text(text: str) -> str:
    """The text as the rules read it, so a disguised word still matches.

    HTML entities and inline tags are resolved, full-width and other compatibility
    forms become plain letters, invisible characters and accents are dropped, and
    Cyrillic or Greek look-alike letters are read as Latin ones. Only for matching:
    what is stored and shown is left as it was.
    """
    if not text:
        return ""
    if "&" in text:
        text = _ENTITY_RE.sub(lambda entity: html.unescape(entity.group(0)), text)
    if "<" in text:
        text = _INLINE_TAG_RE.sub("", strip_html_comments(text))
    if text.isascii():
        return text
    return _HIDDEN_RE.sub("", unicodedata.normalize("NFKD", text)).translate(_LOOKALIKES)


def normalize_with_spans(text: str) -> tuple[str, list[int], list[int]]:
    """``normalize_text``, and for each character it returns, where in ``text`` it came from (start, end).

    So a phrase the rules found in the normalized text can be shown as it was written.
    """
    if not text:
        return "", [], []
    starts = list(range(len(text)))
    ends = list(range(1, len(text) + 1))
    if "&" in text:
        pieces: list[str] = []
        new_starts: list[int] = []
        new_ends: list[int] = []
        pos = 0
        for entity in _ENTITY_RE.finditer(text):
            pieces.append(text[pos : entity.start()])
            new_starts += starts[pos : entity.start()]
            new_ends += ends[pos : entity.start()]
            decoded = html.unescape(entity.group(0))
            pieces.append(decoded)
            new_starts += [starts[entity.start()]] * len(decoded)
            new_ends += [ends[entity.end() - 1]] * len(decoded)
            pos = entity.end()
        pieces.append(text[pos:])
        text, starts, ends = "".join(pieces), new_starts + starts[pos:], new_ends + ends[pos:]
    if "<" in text:
        text, starts, ends = _drop_spans(text, starts, ends, _comment_spans(text))
        text, starts, ends = _drop_spans(text, starts, ends, [tag.span() for tag in _INLINE_TAG_RE.finditer(text)])
    if text.isascii():
        return text, starts, ends
    pieces, new_starts, new_ends = [], [], []
    for run in re.finditer(r"[\x00-\x7f]+|[^\x00-\x7f]", text):
        piece = run.group(0)
        if not piece.isascii():
            piece = _HIDDEN_RE.sub("", unicodedata.normalize("NFKD", piece)).translate(_LOOKALIKES)
            new_starts += [starts[run.start()]] * len(piece)
            new_ends += [ends[run.start()]] * len(piece)
        else:
            new_starts += starts[run.start() : run.end()]
            new_ends += ends[run.start() : run.end()]
        pieces.append(piece)
    return "".join(pieces), new_starts, new_ends


def _drop_spans(text: str, starts: list[int], ends: list[int], spans: list[tuple[int, int]]):
    if not spans:
        return text, starts, ends
    pieces: list[str] = []
    new_starts: list[int] = []
    new_ends: list[int] = []
    pos = 0
    for start, end in spans:
        pieces.append(text[pos:start])
        new_starts += starts[pos:start]
        new_ends += ends[pos:start]
        pos = end
    pieces.append(text[pos:])
    return "".join(pieces), new_starts + starts[pos:], new_ends + ends[pos:]


def own_words(body: str, *, keep_disclaimers: bool = False) -> str:
    """The part of a message the sender actually wrote: no quoted thread, no legal footer or banner.

    ``keep_disclaimers=True`` keeps paragraphs that look like a legal footer, so the fraud
    check can see a request someone tucked into one.
    """
    text = (body or "").replace("\r\n", "\n")
    quote = QUOTE_START_RE.search(text)
    # A bare forward has nothing of its own, so the forwarded message is what was sent.
    if quote and text[: quote.start()].strip():
        text = text[: quote.start()]
    paragraphs = re.split(r"\n\s*\n", text)
    kept = [p for p in paragraphs if keep_disclaimers or not DISCLAIMER_RE.search(p)]
    return "\n\n".join(kept).strip()


# Words that name where a payment goes. "Account" and "payment" on their own also name logins, account
# managers, card details and payment terms, so they count only next to one of these.
_BANK = r"(?:bank(?:ing|s)?|remit(?:tance)?|remit[- ]to|wire|wiring|ach|iban|swift|bic|sort\s+code)"
# The details themselves: "bank details", "bank account number", "wire instructions", "our payment details".
_BANK_DETAILS = (
    r"(?:" + _BANK + r"\s+(?:account\s+)?(?:details|information|info|instructions|account|numbers?|data|coordinates)"
    r"|routing\s+(?:and\s+account\s+)?numbers?|account\s+and\s+routing\s+numbers?"
    r"|(?:payment|remittance)\s+instructions|our\s+payment\s+(?:details|information|info))"
)
_CHANGED = r"(?:changed|changing|updated|amended|modified|replaced|moved|switched)"
# Up to 25 characters inside one sentence, but not across a "no", "not", "nicht", "pas"...: "datos bancarios no han
# cambiado" says the details have NOT changed.
_GAP = r"(?:(?!\b(?:no|not|nunca|jamas|pas|jamais|nicht|nie|kein\w*|nao|sin|sem)\b)[^.\n]){0,25}?"

# Every phrasing starts a word, so the leading "\b(?=[a-z])" lets the many alternatives be tried only there:
# three times quicker on a long email than trying each of them at every character.
PAYMENT_CHANGE_RE = re.compile(
    r"\b(?=[a-z])(\bnew\s+(?:bank(?:ing)?|routing|account|wire)\s+instruct|"
    r"\bupdated\s+(?:bank(?:ing)?|wire|account|payment)\s+instruct|"
    # "Change of bank details", "changes to our banking information", "notice of change of bank."
    # Not "changes to your bank account fees": a change to the reader's own account is not a payee's change.
    r"(?<!not a )(?<!not )\bchange[ds]?\s+(?:in|of|to)\s+(?:(?:our|the|my|its|their)\s+)?"
    r"(?:" + _BANK_DETAILS + r"|bank(?:ing|s)?(?=\s*(?:[.,;:!?)]|\n|$)))|"
    # "Our bank account has changed", "bank details were updated", "our bank is changing".
    r"\b(?:" + _BANK_DETAILS + r"|our\s+bank(?:ing|s)?)\s+(?:has|have|had|was|were|is|are)\s+"
    r"(?:(?:now|just|recently|also|all|since)\s+)?(?:been\s+)?" + _CHANGED + r"\b|"
    # "Our bank account change", "their bank details changed" (not "have your bank details changed?" or an
    # auditor's "the log of bank account changes": the details must be the writer's or the payee's own).
    r"\b(?:our|my|its|their)\s+(?:new\s+)?(?:" + _BANK_DETAILS + r"|(?:payment|remittance)\s+(?:details|information|info))"
    r"\s+change[ds]?\b|"
    # "New bank details", "updated ACH information", "new routing number".
    r"\b(?:new|updated|revised|changed|different|amended)\s+(?:" + _BANK + r"\s+(?:account\s+)?"
    r"(?:details|information|info|instructions|numbers?|data)|routing\s+numbers?|(?:payment|remittance)\s+instructions)|"
    # "We have a new bank account", "our new account details".
    r"\b(?:a|our)\s+new\s+bank(?:ing)?\s+account\b|\bour\s+new\s+(?:bank\s+)?account\s+(?:details|information|info|number)|"
    # "Update the bank account on file", "update your records with our payment information".
    r"\bupdate\s+(?:(?:your|the)\s+(?:records?|files?|vendor\s+(?:file|master|records?))\s+(?:with|to)\s+)?"
    r"(?:(?:our|the|its|their)\s+)?(?:new\s+)?" + _BANK_DETAILS + r"|"
    # "Please pay invoice 5521 to the new account", "send this payment to a different account" (not "send the W-9 to
    # the new account manager").
    r"\b(?:pay|paid|paying|payments?|remit\w*|wire[ds]?|wiring|transfer\w*|deposit\w*|funds)\b[^.\n]{0,60}?"
    r"\b(?:to|into)\s+(?:the|our|this|a)\s+(?:new|different|updated)\s+(?:bank\s+)?account\b|"
    # "Please use the new account for all future payments", "use account ****9981 for all payments going forward".
    r"\b(?:use|pay|remit|wire|transfer|deposit)\b[^.\n]{0,30}?\b(?:new|different|other|following|bank)\s+account\s+"
    r"for\s+(?:all|any|future|upcoming|further)\b[^.\n]{0,25}?\bpayments?\b|"
    # The number must look like a bank account, masked ("****9981") or seven digits or more: "use account 6150
    # for all software payments going forward" is a ledger account.
    r"\b(?:use|pay|remit|wire|transfer|deposit)\b[^.\n]{0,30}?\b(?:account|acct\.?)\s*(?:number|no\.?|#)?\s*[:#]?\s*"
    r"(?:[*xX•]{2,12}[\s-]?\d{2,6}|\d{7,17})\b[^.\n]{0,50}?\b(?:for\s+(?:all|any|future|upcoming|further)\b[^.\n]{0,25}?\bpayments?\b|"
    r"going\s+forward|from\s+now\s+on|effective\s+immediately)|"
    # "Please use the following account for the next payment" (but not "the following account for coding").
    r"\bplease\s+use\s+(?:the\s+)?following\s+(?:bank(?:ing)?\b|routing\b|(?:account|details)\b"
    r"(?=[^.\n]{0,60}\b(?:pay\w*|remit\w*|wir(?:e|ing)|ach|routing|bank\w*|transfer\w*|deposit\w*|iban|swift)\b))|"
    r"\bdo\s+not\s+use\s+(?:the\s+)?previous\s+account|"
    r"(?<!not )\b(?:changed|switched|moved)\s+(?:our\s+bank(?:s|ing\s+partner)?|banks|to\s+a\s+new\s+bank)\b|"
    # "Kindly remit to the account below".
    r"\b(?:remit|send|pay|wire|transfer|make)\w*\s+(?:all\s+|any\s+|future\s+|the\s+)*(?:payments?\s+|funds\s+)?"
    r"(?:to|into)\s+(?:the|our)\s+(?:bank\s+)?account\s+(?:below|listed\s+below|shown\s+below|details\s+below|as\s+follows)\b|"
    # The same in Spanish, French, German and Portuguese (read without accents, see normalize_text).
    r"\b(?:datos|cuenta|informacion|coordenadas)\s+bancari[oa]s?\b" + _GAP + r"\b(?:ha|han)\s+(?:sido\s+)?"
    r"(?:cambiad|actualizad|modificad)[oa]s?\b|"
    r"\bnuev[oa]s?\s+(?:datos|cuenta|informacion)\s+bancari[oa]s?\b|\bcambio\s+de\s+(?:(?:cuenta|datos)\s+bancari[oa]s?|banco)\b|"
    r"\b(?:coordonnees|informations|donnees|references)\s+bancaires\b" + _GAP + r"\b(?:ont|a)\s+(?:ete\s+)?"
    r"(?:change|modifie|mis\s+a\s+jour|mise\s+a\s+jour)e?s?\b|"
    r"\bnouve(?:au|l|lle|lles|aux)\s+(?:rib|iban|compte\s+bancaire|coordonnees\s+bancaires)\b|"
    r"\bchangement\s+de\s+(?:coordonnees\s+bancaires|rib|compte\s+bancaire|banque)\b|"
    r"\b(?:rib|compte\s+bancaire|banque)\s+a\s+change\b|"
    r"\b(?:bankverbindung|bankdaten|kontodaten|kontoverbindung|bankkonto)\b" + _GAP +
    r"\b(?:(?:hat|haben)\s+sich\s+ge(?:a|ae)ndert|(?:wurde|wurden|ist|sind)\s+(?:ge(?:a|ae)ndert|aktualisiert))\b|"
    r"\bneue[nrs]?\s+(?:bankverbindung|bankdaten|kontodaten|kontoverbindung|bankkonto|iban)\b|"
    r"\b(?:a|ae)nderung\s+(?:der|unserer|ihrer)\s+(?:bankverbindung|bankdaten|kontodaten|kontoverbindung)\b|"
    r"\b(?:uberweisen|uberweisung|zahlen|zahlung\w*)\b[^.\n]{0,40}\b(?:auf|an)\s+(?:das|unser)\s+neue[sn]?\s+konto\b|"
    r"\b(?:dados|conta|informacoes)\s+bancari[oa]s?\s+(?:(?:foram|foi)\s+)?"
    r"(?:alterad[oa]s?|atualizad[oa]s?|modificad[oa]s?|mudaram|mudou)\b|"
    r"\bnov[oa]s?\s+(?:dados|conta|informacoes)\s+bancari[oa]s?\b|"
    r"\b(?:alteracao|mudanca|troca)\s+(?:de|dos|nos|da)\s+(?:dados|conta)\s+bancari)",
    re.IGNORECASE,
)

# A change the sentence says is not happening: "there is no change to our bank details", "we have not made any
# changes to our bank details", "keine Änderung unserer Bankverbindung", "aucun changement de RIB", "no hay cambio
# de cuenta bancaria", "sem alteração de dados bancários". Read up to two filler words back, within the clause.
_NEGATION_BEFORE_RE = re.compile(
    r"(?:^|[^\w'])(?:no|not|never|without|nor|\w+n't|keine?[nrms]?|nicht|ohne|aucune?|sans|pas\s+de|ni|sin|"
    r"ningun[oa]?|nunca|nao|sem|nenhum[oa]?)"
    r"(?:\s+(?:any|recent|further|other|such|planned|the|a|an|been|made|have|had|has|be|is|are|was|were|there|"
    r"hay|ha|habido|houve|es|gibt)){0,2}\s+$",
    re.IGNORECASE,
)


def _negated(text: str, start: int) -> bool:
    head = text[max(0, start - 60) : start]
    stop = max(head.rfind(mark) for mark in ".,;:!?()\n")
    return bool(_NEGATION_BEFORE_RE.search(head[stop + 1 :]))


class _BankChangePattern:
    """``PAYMENT_CHANGE_RE``: the bank-change wording, less a change the sentence says is not happening.

    It is used like a compiled pattern (``search``, ``finditer``, ``pattern``).
    """

    def __init__(self, compiled: re.Pattern[str]):
        self.compiled = compiled
        self.pattern = compiled.pattern
        self.flags = compiled.flags

    def finditer(self, text: str):
        for match in self.compiled.finditer(text):
            if not _negated(text, match.start()):
                yield match

    def search(self, text: str) -> re.Match[str] | None:
        return next(self.finditer(text), None)


PAYMENT_CHANGE_RE = _BankChangePattern(PAYMENT_CHANGE_RE)

NEWSLETTER_RE = re.compile(
    r"(unsubscribe|view\s+in\s+browser|you\s+are\s+receiving\s+this|"
    r"weekly\s+roundup|this\s+week\s+in|newsletter)",
    re.IGNORECASE,
)

REPLY_RE = re.compile(
    r"(\b(?:can|could|would)\s+you\b[^.?!\n]{0,120}\?|"
    r"\bplease\s+(?:confirm|advise|reply|respond|get\s+back\s+to\s+me)\b|"
    r"\blet\s+me\s+know\s+(?:by|before|whether|when\s+you|what\s+you|if\s+you\s+(?:can|could|are|agree))\b|"
    r"\bwhat\s+do\s+you\s+think\b|\byour\s+thoughts\?|\bany\s+update\s+on\b|"
    r"\bwaiting\s+(?:on|for)\s+your\b|\bquick\s+question\b|\bare\s+you\s+(?:available|free|able)\b)",
    re.IGNORECASE,
)

APPROVAL_RE = re.compile(
    r"(\bplease\s+sign\b(?!\s*(?:in|up|out|into|on\s+to|onto)\b)|"
    r"\bsign[- ]?off\s+on\b|\byour\s+sign[- ]?off\b|\bneed\s+(?:your\s+)?sign[- ]?off\b|"
    r"\b(?:sign|approve)\s+(?:the|this|attached)\s+(?:po|invoice|contract|agreement|request|document|form|quote)\b)",
    re.IGNORECASE,
)

MEETING_SUBJECT_RE = re.compile(
    r"^\s*(?:(?:re|fw|fwd):\s*)?(?:(?:updated\s+)?invitation|accepted|declined|tentative(?:ly\s+accepted)?|canceled|cancelled)\s*:",
    re.IGNORECASE,
)

MEETING_RE = re.compile(
    r"\breschedul\w*\s+(?:the\s+|our\s+|this\s+|my\s+|a\s+)?(?:\w+\s+)?(?:meeting|call|sync|1:1|one-on-one|interview|catch[- ]?up|review)\b",
    re.IGNORECASE,
)

AUTOMATED_SENDERS = (
    "no-reply",
    "noreply",
    "donotreply",
    "do-not-reply",
    "notifications@",
    "notification@",
    "alerts@",
    "notify@",
    "mailer-daemon",
)

SYSTEM_ALERTS = (
    "password reset",
    "verification code",
    "your order has shipped",
    "sign-in attempt",
    "security alert",
    "unusual sign-in activity",
    "new sign-in",
)

RULES: tuple[Rule, ...] = (
    Rule(
        DocumentType.PAYMENT_INSTRUCTION_CHANGE,
        120,
        regexes=(PAYMENT_CHANGE_RE,),
        flags=("fraud_risk", "do_not_process"),
        reason="Banking / payment instructions appear to have changed",
    ),
    Rule(
        DocumentType.TAX_DOCUMENT,
        95,
        keywords=("cp2000", "irs", "form 941", "form 1099", "w-2", "w-9", "franchise tax", "notice of deficiency", "penalty notice"),
        filename_keywords=("w-9", "w9", "1099", "w-2", "w2", "941", "tax-notice"),
        sender_keywords=("irs.gov", "treasury.gov", "ftb.ca.gov"),
        flags=("tax", "deadline"),
        reason="Tax form or notice language",
    ),
    Rule(
        DocumentType.AUDIT_REQUEST,
        90,
        keywords=("pbc", "prepared by client", "audit request", "sample selection", "provided by client", "external audit", "testing request", "workpaper request"),
        filename_keywords=("pbc", "pbc-list", "audit"),
        sender_keywords=("deloitte", "pwc", "ey.com", "kpmg", "rsmus", "bdo.com"),
        flags=("audit",),
        reason="Audit / PBC request",
    ),
    Rule(
        DocumentType.WIRE_ACH_REQUEST,
        80,
        keywords=("wire transfer", "wire request", "please wire", "ach payment", "initiate ach", "outgoing wire", "beneficiary bank"),
        filename_keywords=("wire", "ach-request"),
        flags=("payment_approval",),
        reason="Wire or ACH payment request",
    ),
    Rule(
        DocumentType.AP_INVOICE,
        78,
        keywords=("invoice", "amount due", "remit to", "bill to", "invoice number", "payment terms", "net 30", "net 15"),
        filename_keywords=("invoice", "inv-", "inv_", "bill"),
        reason="Vendor invoice language",
    ),
    Rule(
        DocumentType.CREDIT_MEMO,
        76,
        keywords=("credit memo", "credit note", "cm #", "amount credited"),
        filename_keywords=("credit-memo", "credit_memo", "cm-"),
        reason="Credit memo language",
    ),
    Rule(
        DocumentType.PURCHASE_ORDER,
        74,
        keywords=("purchase order", "po number", "ship to", "please fulfill"),
        filename_keywords=("purchase-order", "po-", "po_"),
        reason="Purchase order language",
    ),
    Rule(
        DocumentType.PACKING_SLIP,
        62,
        keywords=("packing slip", "packing list", "shipped qty", "delivery note"),
        filename_keywords=("packing", "delivery-note", "bol"),
        reason="Packing / receiving document",
    ),
    Rule(
        DocumentType.REMITTANCE_ADVICE,
        86,
        keywords=("remittance advice", "payment remittance", "cash application", "we have paid", "payment notification", "ach credit"),
        filename_keywords=("remittance", "remit", "payment-advice"),
        reason="Customer remittance / cash application",
    ),
    Rule(
        DocumentType.BANK_STATEMENT,
        82,
        keywords=("account statement", "ending balance", "beginning balance", "statement period", "checking statement", "business checking"),
        filename_keywords=("statement", "stmt", "bank-stmt"),
        sender_keywords=("chase.com", "bankofamerica.com", "wellsfargo.com", "pnc.com", "usbank.com", "bofa.com"),
        flags=("reconcile",),
        reason="Bank statement",
    ),
    Rule(
        DocumentType.BANK_RECONCILIATION,
        80,
        keywords=("bank rec", "bank reconciliation", "outstanding checks", "deposits in transit", "reconciled balance"),
        filename_keywords=("bank-rec", "reconciliation", "bkrec"),
        flags=("month_end",),
        reason="Bank reconciliation workpaper",
    ),
    Rule(
        DocumentType.PAYROLL,
        84,
        keywords=("payroll register", "pay date", "gross pay", "net pay", "employer taxes", "direct deposit register"),
        filename_keywords=("payroll", "register", "pay-register"),
        sender_keywords=("adp.com", "paychex.com", "gusto.com", "ukg.com", "paylocity.com"),
        flags=("payroll",),
        reason="Payroll register / payroll provider",
    ),
    Rule(
        DocumentType.EXPENSE_REPORT,
        72,
        keywords=("expense report", "expense reimbursement", "out of pocket", "mileage"),
        filename_keywords=("expense", "er-", "t&e", "tande"),
        flags=("approval",),
        reason="Expense report",
    ),
    Rule(
        DocumentType.CONTRACT,
        60,
        keywords=("master services agreement", "statement of work", "msa", "please countersign", "terms and conditions"),
        filename_keywords=("msa", "sow", "agreement", "contract", "nda"),
        reason="Contract / agreement",
    ),
    Rule(
        DocumentType.INSURANCE,
        58,
        keywords=("certificate of insurance", "evidence of coverage", "additional insured", "coi"),
        filename_keywords=("coi", "insurance", "acord"),
        reason="Insurance / COI",
    ),
    Rule(
        DocumentType.WORKPAPER,
        55,
        keywords=("rollforward", "flux analysis", "tie-out", "accrual support", "prepaid schedule", "close checklist"),
        filename_keywords=("rollforward", "flux", "accrual", "prepaid", "tieout", "workpaper"),
        flags=("month_end",),
        reason="Close / accounting workpaper",
    ),
    Rule(
        DocumentType.APPROVAL_REQUEST,
        70,
        keywords=(
            "please approve",
            "approval needed",
            "needs your approval",
            "awaiting your approval",
            "for your approval",
            "approve or reject",
            "approve or decline",
            "signature requested",
            "docusign",
        ),
        regexes=(APPROVAL_RE,),
        flags=("approval",),
        reason="Someone is waiting on your approval",
        own_words_only=True,
    ),
    Rule(
        DocumentType.MEETING,
        50,
        keywords=(
            "meeting invitation",
            "calendar invite",
            "meeting request",
            "join zoom meeting",
            "microsoft teams meeting",
            "join the meeting",
            "google meet",
        ),
        regexes=(MEETING_RE,),
        subject_regexes=(MEETING_SUBJECT_RE,),
        filename_keywords=(".ics",),
        reason="Meeting or calendar invite",
        own_words_only=True,
    ),
    Rule(
        DocumentType.REPLY_NEEDED,
        45,
        regexes=(REPLY_RE,),
        reason="Someone asked you a question or is waiting on your reply",
        own_words_only=True,
    ),
    Rule(
        DocumentType.NOTIFICATION,
        36,
        keywords=(
            "this is an automated message",
            "do not reply to this email",
            "automated notification",
            "password reset",
            "verification code",
            "your order has shipped",
            "sign-in attempt",
            "security alert",
            "unusual sign-in activity",
            "new sign-in",
        ),
        sender_keywords=AUTOMATED_SENDERS,
        reason="Automated notification",
    ),
    Rule(
        DocumentType.INTERNAL_FYI,
        34,
        keywords=("fyi", "for your information", "heads up", "heads-up", "no action needed", "no action required", "for your awareness"),
        reason="FYI, nothing asked of you",
        own_words_only=True,
    ),
    Rule(
        DocumentType.NEWSLETTER,
        40,
        regexes=(NEWSLETTER_RE,),
        keywords=("roundup", "industry news"),
        reason="Looks like a newsletter",
    ),
)


VIP_DEFAULT = (
    "cfo",
    "controller",
    "ceo@",
    "irs.gov",
    "treasury.gov",
    "deloitte",
    "pwc.com",
    "ey.com",
    "kpmg",
)


def _keyword_hit(blob: str, keyword: str) -> bool:
    """Whole-word match so 'irs' does not fire inside 'first'."""
    if not keyword:
        return False
    pattern = r"(?<![\w.])" + re.escape(keyword.lower()) + r"s?(?![\w.])"
    return re.search(pattern, blob) is not None


def _haystack(subject: str, body: str, filename: str, sender: str, extracted_text: str) -> str:
    return "\n".join([subject or "", body or "", filename or "", sender or "", extracted_text or ""]).lower()


FILENAME_BONUS = 25


def _name_hit(filename: str, keyword: str) -> bool:
    """``keyword`` starts a word of the file name: "nda" is in "NDA_signed.pdf" but not in "close_calendar.xlsx"."""
    return re.search(rf"(?<![a-z]){re.escape(keyword)}", filename) is not None


def score_rules(
    *,
    subject: str,
    body: str,
    filename: str = "",
    sender: str = "",
    extracted_text: str = "",
    payment_rule: bool = True,
) -> dict[DocumentType, tuple[int, list[str], list[str]]]:
    subject, body = normalize_text(subject), normalize_text(body)
    filename, extracted_text = normalize_text(filename), normalize_text(extracted_text)
    own_blob = f"{subject or ''}\n{own_words(body)}".lower()
    # A reply with words of its own is about those words; the thread it quotes doesn't pick its category.
    replied = bool(QUOTE_START_RE.search(body or "")) and own_blob.strip() != (subject or "").strip().lower()
    if replied and not (filename or extracted_text):
        full_blob = f"{own_blob}\n{(sender or '').lower()}"
    else:
        full_blob = _haystack(subject, body, filename, sender, extracted_text)
    file_low = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", filename or "").lower()
    sender_low = (sender or "").lower()
    scores: dict[DocumentType, tuple[int, list[str], list[str]]] = {}
    for rule in RULES:
        if not payment_rule and rule.document_type == DocumentType.PAYMENT_INSTRUCTION_CHANGE:
            continue
        blob = own_blob if rule.own_words_only else full_blob
        hit = False
        reasons: list[str] = []
        matched_kw = next((k for k in rule.keywords if _keyword_hit(blob, k)), None)
        if matched_kw:
            hit = True
            reasons.append(f"keyword:{matched_kw}")
        for rx in rule.regexes:
            if rx.search(blob):
                hit = True
                reasons.append(rule.reason or rx.pattern[:40])
        if any(rx.search(subject or "") for rx in rule.subject_regexes):
            hit = True
            reasons.append("subject")
        if rule.filename_keywords and any(_name_hit(file_low, k) for k in rule.filename_keywords):
            hit = True
            reasons.append("filename")
        if rule.sender_keywords and any(k in sender_low for k in rule.sender_keywords):
            hit = True
            reasons.append("sender")
        if not hit:
            continue
        prev = scores.get(rule.document_type, (0, [], []))
        flags = list(dict.fromkeys(list(prev[2]) + list(rule.flags)))
        # What a file is called says more about it than one word somewhere in its pages.
        named = FILENAME_BONUS if "filename" in reasons else 0
        scores[rule.document_type] = (
            prev[0] + rule.weight + named,
            list(dict.fromkeys(prev[1] + reasons + ([rule.reason] if rule.reason else []))),
            flags,
        )
    # Bulk and machine mail asks questions nobody is waiting on.
    if DocumentType.NEWSLETTER in scores or DocumentType.NOTIFICATION in scores:
        scores.pop(DocumentType.REPLY_NEEDED, None)
    if DocumentType.NEWSLETTER in scores:
        scores.pop(DocumentType.MEETING, None)
    # Approval tools (Concur, DocuSign) send from no-reply addresses, so only
    # account and delivery alerts override an approval.
    if any(_keyword_hit(full_blob, k) for k in SYSTEM_ALERTS):
        scores.pop(DocumentType.APPROVAL_REQUEST, None)
    return scores


def classify_document(
    *,
    subject: str = "",
    body: str = "",
    filename: str = "",
    sender: str = "",
    extracted_text: str = "",
    content_type: str = "",
    has_text: bool | None = None,
    payment_rule: bool = True,
) -> Classification:
    """``payment_rule=False`` leaves bank-change wording to the fraud check in ``fraud.py``."""
    scores = score_rules(
        subject=subject,
        body=body,
        filename=filename,
        sender=sender,
        extracted_text=extracted_text,
        payment_rule=payment_rule,
    )
    if not scores:
        ctype = (content_type or "").lower()
        name = (filename or "").lower()
        if ctype.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".tif", ".tiff")):
            return Classification(DocumentType.IMAGE_SCAN, 0.45, ["image attachment"], importance=Importance.MEDIUM, importance_score=35)
        if name.endswith((".xlsx", ".xlsm", ".csv")) or "spreadsheet" in ctype:
            return Classification(DocumentType.SPREADSHEET, 0.4, ["spreadsheet without a stronger document type"], importance=Importance.MEDIUM, importance_score=40)
        if has_text is False:
            return Classification(DocumentType.IMAGE_SCAN, 0.3, ["no extractable text"], importance=Importance.LOW, importance_score=25)
        return Classification(DocumentType.OTHER, 0.2, ["no stronger pattern matched"], importance=Importance.LOW, importance_score=20)

    best_type, (weight, reasons, flags) = max(scores.items(), key=lambda item: item[1][0])
    confidence = min(0.98, 0.35 + weight / 180)
    return Classification(
        document_type=best_type,
        confidence=round(confidence, 2),
        reasons=reasons,
        flags=flags,
    )


def classify_email(
    *,
    subject: str,
    body: str,
    sender: str,
    outlook_importance: str,
    attachments: list[Classification],
    fields: ExtractedFields,
    as_of: date,
    high_amount: float = 10_000,
    vip_senders: list[str] | None = None,
    has_attachments: bool = False,
    duplicate_invoice: bool = False,
    finance: bool = True,
    fraud: str | None = None,
) -> Classification:
    """Pick the email's category and importance.

    ``fraud`` is the level from ``fraud.assess``. When it is given, only a
    ``high`` check makes the email a payment-instruction change; without it the
    bank-change wording rule decides on its own.
    """
    if fraud is not None:
        attachments = [item for item in attachments if item.document_type != DocumentType.PAYMENT_INSTRUCTION_CHANGE]
    attachment_types = [item.document_type for item in attachments]
    combined_flags: list[str] = []
    for item in attachments:
        combined_flags.extend(item.flags)
    if fraud is not None:
        combined_flags = [flag for flag in combined_flags if flag not in {"fraud_risk", "do_not_process"}]
    email_scores = score_rules(subject=subject, body=body, sender=sender, extracted_text="", payment_rule=fraud is None)
    if email_scores:
        email_type, (weight, reasons, flags) = max(email_scores.items(), key=lambda item: item[1][0])
        combined_flags.extend(flags)
    else:
        email_type, weight, reasons = DocumentType.OTHER, 0, []

    if attachment_types:
        ranked = _rank_types(attachment_types)
        primary = ranked[0]
        if len(set(attachment_types)) > 1 and ranked[0] != DocumentType.PAYMENT_INSTRUCTION_CHANGE:
            category = DocumentType.MIXED if len(set(t for t in attachment_types if t not in {DocumentType.OTHER, DocumentType.SPREADSHEET, DocumentType.IMAGE_SCAN})) > 1 else primary
            if category == DocumentType.MIXED:
                # Keep a useful primary if one attachment is clearly the point of the email.
                if _type_rank(primary) <= _type_rank(DocumentType.AP_INVOICE):
                    category = primary
        else:
            category = primary
        confidence = max((item.confidence for item in attachments), default=0.5)
        reasons = [f"attachment:{item.document_type.value}" for item in attachments] + reasons
    else:
        category = email_type
        confidence = min(0.9, 0.3 + weight / 180) if weight else 0.25

    if fields.mentions_attachment and not has_attachments:
        combined_flags.append("missing_attachment")
        reasons.append("Email says something is attached, but no attachment was found")

    if duplicate_invoice and category in _INVOICE_TYPES:
        combined_flags.append("duplicate_invoice")
        reasons.append("Invoice number already seen on another email")

    if fraud == "high" or category == DocumentType.PAYMENT_INSTRUCTION_CHANGE or "fraud_risk" in combined_flags:
        combined_flags.extend(["fraud_risk", "do_not_process"])
        category = DocumentType.PAYMENT_INSTRUCTION_CHANGE
    elif fraud == "caution":
        combined_flags.append("payment_caution")

    importance, score, imp_reasons = _importance(
        category=category,
        flags=combined_flags,
        fields=fields,
        outlook_importance=outlook_importance,
        sender=sender,
        as_of=as_of,
        high_amount=high_amount,
        vip_senders=vip_senders or [],
        subject=subject,
        body=body,
        finance=finance,
    )
    return Classification(
        document_type=category,
        confidence=round(confidence, 2),
        reasons=list(dict.fromkeys(reasons)),
        flags=list(dict.fromkeys(combined_flags)),
        importance=importance,
        importance_score=score,
        importance_reasons=imp_reasons,
    )


_INVOICE_TYPES = {
    DocumentType.AP_INVOICE,
    DocumentType.AR_INVOICE,
    DocumentType.CREDIT_MEMO,
    DocumentType.MIXED,
}


_TYPE_PRIORITY = [
    DocumentType.PAYMENT_INSTRUCTION_CHANGE,
    DocumentType.TAX_DOCUMENT,
    DocumentType.AUDIT_REQUEST,
    DocumentType.WIRE_ACH_REQUEST,
    DocumentType.AP_INVOICE,
    DocumentType.BANK_STATEMENT,
    DocumentType.BANK_RECONCILIATION,
    DocumentType.PAYROLL,
    DocumentType.REMITTANCE_ADVICE,
    DocumentType.CREDIT_MEMO,
    DocumentType.EXPENSE_REPORT,
    DocumentType.PURCHASE_ORDER,
    DocumentType.CONTRACT,
    DocumentType.WORKPAPER,
    DocumentType.AR_INVOICE,
    DocumentType.INSURANCE,
    DocumentType.PACKING_SLIP,
    DocumentType.SPREADSHEET,
    DocumentType.IMAGE_SCAN,
    DocumentType.APPROVAL_REQUEST,
    DocumentType.REPLY_NEEDED,
    DocumentType.MEETING,
    DocumentType.NEWSLETTER,
    DocumentType.NOTIFICATION,
    DocumentType.INTERNAL_FYI,
    DocumentType.OTHER,
]


def _type_rank(doc_type: DocumentType) -> int:
    try:
        return _TYPE_PRIORITY.index(doc_type)
    except ValueError:
        return len(_TYPE_PRIORITY)


def _rank_types(types: list[DocumentType]) -> list[DocumentType]:
    return sorted(types, key=_type_rank)


def _importance(
    *,
    category: DocumentType,
    flags: list[str],
    fields: ExtractedFields,
    outlook_importance: str,
    sender: str,
    as_of: date,
    high_amount: float,
    vip_senders: list[str],
    subject: str,
    body: str,
    finance: bool = True,
) -> tuple[Importance, int, list[str]]:
    score = 30
    reasons: list[str] = []
    blob = f"{subject}\n{own_words(body)}".lower()
    sender_low = (sender or "").lower()

    if "fraud_risk" in flags or category == DocumentType.PAYMENT_INSTRUCTION_CHANGE:
        return Importance.CRITICAL, 100, ["Possible payment-instruction / BEC fraud — verify by a known phone number"]

    if category == DocumentType.TAX_DOCUMENT:
        score += 40
        reasons.append("Tax notice / filing document")
    if category == DocumentType.AUDIT_REQUEST:
        score += 35
        reasons.append("Auditor request")
    if category == DocumentType.WIRE_ACH_REQUEST:
        score += 32
        reasons.append("Outgoing payment request")
    if category == DocumentType.AP_INVOICE:
        score += 18
        reasons.append("Vendor invoice to enter / schedule")
    if category == DocumentType.PAYROLL:
        score += 22
        reasons.append("Payroll")
    if category == DocumentType.BANK_STATEMENT:
        score += 12
        reasons.append("Bank statement to reconcile")
    if category == DocumentType.REMITTANCE_ADVICE:
        score += 14
        reasons.append("Cash to apply")
    if category == DocumentType.APPROVAL_REQUEST:
        score += 24
        reasons.append("Waiting on your approval")
    if category == DocumentType.REPLY_NEEDED:
        score += 16
        reasons.append("Someone is waiting on your reply")
    if category == DocumentType.MEETING:
        score += 6
        reasons.append("Meeting or calendar change")
    if category == DocumentType.NOTIFICATION:
        score -= 18
        reasons.append("Automated notification")
    if category in {DocumentType.NEWSLETTER, DocumentType.INTERNAL_FYI}:
        score -= 20
        reasons.append("Informational / newsletter")
    if "missing_attachment" in flags:
        score += 15
        reasons.append("Claimed attachment is missing")
    if "duplicate_invoice" in flags:
        score += 20
        reasons.append("Possible duplicate invoice")

    amount = fields.primary_amount
    if amount is not None and amount >= high_amount:
        score += 18
        reasons.append(f"Amount ${amount:,.2f} is at or above the review threshold")
    if amount is not None and amount >= high_amount * 5:
        score += 10
        reasons.append("Very large dollar amount")

    if fields.primary_due:
        try:
            due = date.fromisoformat(fields.primary_due)
            days = (due - as_of).days
            if days < 0:
                score += 25
                reasons.append(f"Past due ({due.isoformat()})")
                flags.append("overdue")
            elif days == 0:
                score += 22
                reasons.append("Due today")
            elif days <= 3:
                score += 16
                reasons.append(f"Due in {days} day(s)")
            elif days <= 7:
                score += 10
                reasons.append("Due within a week")
        except ValueError:
            pass

    last_day = _month_end(as_of)
    days_to_close = (last_day - as_of).days
    if finance and days_to_close <= 7 and (
        category
        in {
            DocumentType.BANK_STATEMENT,
            DocumentType.BANK_RECONCILIATION,
            DocumentType.WORKPAPER,
            DocumentType.AP_INVOICE,
            DocumentType.PAYROLL,
        }
        or "month_end" in flags
        or re.search(r"\bclose\b|month[- ]end", blob)
    ):
        score += 12
        reasons.append(f"Month-end is in {days_to_close} day(s)")
        flags.append("month_end")

    if (outlook_importance or "").lower() == "high":
        score += 10
        reasons.append("Sender marked the message as high importance in Outlook")

    vip_needles = list(vip_senders) + list(VIP_DEFAULT)
    if any(needle in sender_low for needle in vip_needles):
        score += 12
        reasons.append("VIP / elevated sender")

    if re.search(r"\b(urgent|asap|immediately|eod|cob|today)\b", blob):
        score += 8
        reasons.append("Urgent timing language")

    score = max(0, min(99, score))
    if score >= 85:
        importance = Importance.CRITICAL
    elif score >= 62:
        importance = Importance.HIGH
    elif score >= 38:
        importance = Importance.MEDIUM
    else:
        importance = Importance.LOW
    return importance, score, list(dict.fromkeys(reasons))


def _month_end(as_of: date) -> date:
    if as_of.month == 12:
        nxt = date(as_of.year + 1, 1, 1)
    else:
        nxt = date(as_of.year, as_of.month + 1, 1)
    return date.fromordinal(nxt.toordinal() - 1)


def month_end(as_of: date) -> date:
    return _month_end(as_of)


def score_importance(**kwargs) -> tuple[Importance, int, list[str]]:
    """How important an email of a given category is, scored the way ``classify_email`` scores it."""
    return _importance(**kwargs)


def outlook_categories(classification: Classification) -> list[str]:
    labels = ["CloseDesk", DOCUMENT_OUTLOOK.get(classification.document_type, classification.document_type.value)]
    if classification.importance in {Importance.CRITICAL, Importance.HIGH}:
        labels.append("CloseDesk-Important")
    if "fraud_risk" in classification.flags:
        labels.append("CloseDesk-FraudReview")
    return labels


DOCUMENT_OUTLOOK = {
    DocumentType.AP_INVOICE: "AP-Invoice",
    DocumentType.PAYMENT_INSTRUCTION_CHANGE: "Fraud-Review",
    DocumentType.BANK_STATEMENT: "Bank-Statement",
    DocumentType.PAYROLL: "Payroll",
    DocumentType.TAX_DOCUMENT: "Tax",
    DocumentType.AUDIT_REQUEST: "Audit",
    DocumentType.REMITTANCE_ADVICE: "Cash-App",
    DocumentType.EXPENSE_REPORT: "T&E",
    DocumentType.WIRE_ACH_REQUEST: "Payment-Approval",
    DocumentType.WORKPAPER: "Close",
}
