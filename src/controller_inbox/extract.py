from __future__ import annotations

import calendar
import hashlib
import io
import re
from datetime import date, datetime
from html import unescape
from typing import Iterable

from dateutil import parser as date_parser

from controller_inbox.classify import strip_html_comments
from controller_inbox.documents import decode_text, extract_document
from controller_inbox.models import ExtractedFields
from controller_inbox.ocr import image_text


# A further "-0457" segment belongs to the number ("INV-2024-0457"); a segment needs a digit, so
# "12345-due" stays "12345".
_ID_TAIL = r"(?:[-_](?=[A-Z0-9]*\d)[A-Z0-9]{1,12})*"
# "Invoice Number: 12345", "PO No. 4500123": the label word is read whole, or the separators would take its "No".
_NUMBER_WORD = r"(?:\s*(?:number|num|no)\b\.?)?"
INVOICE_RE = re.compile(
    r"\b(?:invoice|inv(?![-_]?\d)\.?|bill)" + _NUMBER_WORD + r"[\s#:No.-]*((?:[A-Z]{1,6}[-_]?\d{2,12}|\d{3,12})" + _ID_TAIL + r")",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _is_date(value: str) -> bool:
    """A real calendar date written year first ("2026-10-01"); "2025-47" and "4512-03" are invoice numbers."""
    if not _ISO_DATE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True
INVOICE_BARE_RE = re.compile(r"\b(INV[-_]?\d{3,8}(?:[-_]\d{1,8})*|IN[-_]?\d{4,8}(?:[-_]\d{1,8})*)\b", re.IGNORECASE)
PO_RE = re.compile(
    r"\b(?:purchase\s+order|p\.?o\.?)" + _NUMBER_WORD + r"[\s#:No.-]*([A-Z]{0,4}-?\d{3,10})\b",
    re.IGNORECASE,
)
# After a currency mark, cents are optional ("$48,000"). Not followed by a further digit group,
# so "$1,2345" is not read as $1.
AMOUNT_RE = re.compile(
    r"(?<!\w)(?:USD|US\$|\$)\s*([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{2})?|[0-9]+(?:\.[0-9]{2})?)(?![\w]|[.,]\d)"
)
# Without a currency mark a bare whole number ("total 3 items") is not an amount: it needs
# thousands separators or cents.
AMOUNT_WORDS_RE = re.compile(
    r"\b(?:amount(?:\s+due)?|total(?:\s+due)?|balance(?:\s+due)?|grand\s+total)\s*[:\-]?\s*\$?\s*"
    r"([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]{2})?|[0-9]+\.[0-9]{2})(?![\w]|[.,]\d)",
    re.IGNORECASE,
)
_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
# "October 15" with no year: read against the date the mail was sent (see parse_due_date).
_MONTH_DAY = _MONTH + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?(?!\d)"
# The year after "Oct 15,": four digits, or two not followed by a time or a word, so "Oct 15, 12pm",
# "Oct 15, 10:00 AM" and "Oct 30, 30 days net" are not read as 2012, 2010 and 2030.
YEAR_AFTER_DAY = r"(?:\d{4}|\d{2}(?!\s*(?:[:.]\d|[a-z])))(?!\d)"
# A weekday before a date names the date's day ("Friday, October 16"): the date is read, not the next Friday.
_WEEKDAY_BEFORE = r"(?:(?:mon|tues?|wed(?:nes)?|thu(?:rs?)?|fri|sat(?:ur)?|sun)(?:day)?\.?,?\s+)?"
# "Due on October 12" is read as "due October 12".
DUE_RE = re.compile(
    r"\b(?:due(?:\s+date)?(?:\s+on)?|payment\s+due|remit\s+by|pay\s+by|respond\s+by|needed\s+by|"
    r"please\s+(?:complete|provide|respond|approve)\s+by|deadline|by)\s*[:\-]?\s*" + _WEEKDAY_BEFORE +
    r"([A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+" + YEAR_AFTER_DAY + "|" + _MONTH_DAY + r"|"
    # Day first, as UK and EU suppliers write it: "15 October 2026", "15-Oct-26", "15 October". A two-digit year
    # only follows directly ("15 Oct 26"): in "due 15 October, 10% late fee" and "by 2 June, 12 cases" the
    # figure is not a year, nor in "by 2 June 12 cases short".
    r"\d{1,2}(?:st|nd|rd|th)?[\s-]+" + _MONTH + r"\b\.?(?:,?[\s-]+\d{4}|[\s-]\d{2}(?!\s*[a-z]))?(?![\d%])|"
    r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}-\d{2}-\d{2}|"
    r"EOD|COB|today|tomorrow|Monday|Tuesday|Wednesday|Thursday|Friday)",
    re.IGNORECASE,
)
# An account number has digits in it (at least four); "account statement" and "account manager" are words.
_ACCOUNT_NUMBER = r"(?=(?:[A-Z]*\d){4})[A-Z0-9]{6,34}\b"
ACCOUNT_RE = re.compile(
    r"\b(?:account(?:\s+(?:number|no\.?))?|acct\.?(?:\s+(?:number|no\.?))?|a/c|iban)[\s#:]*(" + _ACCOUNT_NUMBER + ")",
    re.IGNORECASE,
)
ROUTING_RE = re.compile(
    r"\b(?:routing(?:\s+number)?|aba|sort\s+code)[\s#:]*(\d{6,9})\b",
    re.IGNORECASE,
)
VENDOR_RE = re.compile(
    r"\b(?:from|vendor|supplier|bill\s+from|sold\s+by|remit\s+to)\s*[:\-]\s*([A-Z][A-Za-z0-9&.,' \-]{2,60})",
)
ATTACHMENT_MENTION_RE = re.compile(
    r"\b(attached|attachment|enclosed|please\s+see\s+attached|see\s+the\s+attached)\b",
    re.IGNORECASE,
)


def _grouped(least: int) -> str:
    """A number written in groups of digits ("1234 5678 9012", "1234-5678-9012"), with at least ``least`` digits
    in all; a date (2026-09-30, 10-15-2026) is not one."""
    not_a_date = r"(?!\d{4}-\d{2}-\d{2}\b|\d{1,2}[ -]\d{1,2}[ -](?:19|20)\d{2}(?![\d-]))"
    return not_a_date + r"(?=(?:[ -]?\d){" + str(least) + r"})\d{2,}(?:[ -]\d{2,}){1,7}(?![\d-])"


# After its label a number may follow "is", a colon, a dash or "#": "Account Number - 12345678", "A/C No: 12345678".
# A short note in brackets may come between: "Account Number (IBAN): DE89 …".
_SECRET_LABEL_END = r"(?:\s*\([^()\n]{1,20}\))?(?:\s+is\b)?[\s#:\-\u2013]*"
# Masked to the last four digits wherever text is stored or shown: bank account, routing and sort-code numbers,
# IBANs, and card numbers, written whole or in groups as statements and remittance letters print them
# ("IBAN GB29 NWBK 6016 1331 9268 19"). Only a number beside its label is masked, so invoice and PO numbers,
# phone numbers, dates and amounts stay as written.
BANK_SECRET_RE = re.compile(
    r"\b(?P<routing>(?:routing|aba)(?:\s+(?:number|no\.?|#))?|sort\s+code)" + _SECRET_LABEL_END
    + r"(?:\d{6,9}\b|\d{3}[ -]\d{3}[ -]\d{3}(?![\d-])|\d{2}[ -]\d{2}[ -]\d{2}(?![\d-]))|"
    r"\b(?P<account>(?:account|acct\.?|a/c)(?:\s+(?:number|no\.?|#))?)" + _SECRET_LABEL_END
    + r"(?:" + _ACCOUNT_NUMBER + "|" + _grouped(6) + r"|[A-Z]{2}\d{2}(?:[ -][A-Z0-9]{4}){2,7}(?:[ -][A-Z0-9]{1,3})?\b)|"
    r"\b(?P<iban>iban)" + _SECRET_LABEL_END
    + r"[A-Z]{2}\d{2}(?:[A-Z0-9]{10,30}\b|(?:[ -][A-Z0-9]{4}){2,7}(?:[ -][A-Z0-9]{1,3})?\b)|"
    r"\b(?P<card>(?:(?:credit|debit)\s+)?card|visa|mastercard|amex)(?:\s+(?:number|no\.?|#))?" + _SECRET_LABEL_END
    + r"(?:\d{12,19}\b|" + _grouped(12) + r")",
    re.IGNORECASE,
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# Tags inside a line of text. A browser shows no space for them, so "b<span></span>ank" reads "bank".
_INLINE_TAG_RE = re.compile(
    r"(?i)</?(?:a|abbr|b|bdi|bdo|big|cite|code|del|dfn|em|font|i|ins|kbd|mark|nobr|o:p|q|s|samp|small|"
    r"span|strike|strong|sub|sup|time|tt|u|var|wbr)(?:\s[^<>]*)?/?>"
)


def _drop_scripts(html: str) -> str:
    """Replace each <script>…</script> and <style>…</style> block with a space, in one pass over the text.

    Once no closing tag of a kind follows, no later block of that kind can close either, so the search
    stops instead of reading to the end of the text again from every "<script".
    """
    low = html.lower()
    out: list[str] = []
    pos = 0
    kinds = ["script", "style"]
    while kinds:
        opener = re.compile("<(" + "|".join(kinds) + ")").search(low, pos)
        if opener is None:
            break
        end_of_tag = low.find(">", opener.end())
        if end_of_tag < 0:
            break
        close = low.find(f"</{opener.group(1)}>", end_of_tag + 1)
        if close < 0:
            kinds.remove(opener.group(1))
            continue
        out.append(html[pos : opener.start()] + " ")
        pos = close + len(opener.group(1)) + 3
    out.append(html[pos:])
    return "".join(out)


def html_to_text(html: str) -> str:
    text = strip_html_comments(_drop_scripts(html))
    # What the reader is never shown (display:none, visibility:hidden, font-size:0, Outlook's mso-hide:all) is
    # dropped with all it holds: "Our bank <span style="display:none">zz</span>details" reads "Our bank details".
    # Each hidden element ends at its own closing tag; one that is never closed hides only its tag.
    # The style value runs to its own closing quote: Word writes style='font-family:"Calibri";display:none'.
    hides = r"(?:display\s*:\s*none|visibility\s*:\s*hidden|mso-hide\s*:\s*all|font-size\s*:\s*0(?![.\d]*[1-9]))"
    hidden = re.compile(
        r"<([a-z][a-z0-9]*)\b[^<>]*?\bstyle\s*=\s*(?:\"[^\"<>]*?" + hides + r"|'[^'<>]*?" + hides + r")[^<>]*>",
        re.I,
    )
    if hidden.search(text):
        pieces: list[str] = []
        pos = 0
        unclosed: set[str] = set()
        while (opener := hidden.search(text, pos)) is not None:
            name, end = opener.group(1).lower(), opener.end()
            void = name in {"img", "br", "hr", "input", "meta", "link", "wbr"} or opener.group(0).endswith("/>")
            if not void and name not in unclosed:
                depth = 1
                for tag in re.compile(rf"<(/?){name}\b[^<>]*>", re.I).finditer(text, end):
                    if not tag.group(0).endswith("/>"):
                        depth += -1 if tag.group(1) else 1
                    if depth == 0:
                        end = tag.end()
                        break
                else:
                    # Not closed by the end of the text: later ones of that name hide only their tag, so a long
                    # run of unclosed tags is read once, not once each.
                    unclosed.add(name)
            pieces.append(text[pos : opener.start()])
            pos = end
        text = "".join(pieces) + text[pos:]
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"(?i)</div>", "\n", text)
    text = _INLINE_TAG_RE.sub("", strip_html_comments(text))
    # "[^<>]": a stray "<" ends the tag it is in, so a long run of them is read once, not once per "<".
    text = re.sub(r"<[^<>]+>", " ", text)
    # Every entity, named or numbered: "&#8203;" is a zero-width space, not five characters of text.
    text = unescape(text)
    return collapse_ws(text)


def collapse_ws(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def redact_financial_secrets(text: str) -> str:
    def _mask(match: re.Match[str]) -> str:
        raw = match.group(0)
        digits = re.sub(r"\D", "", raw)
        last4 = digits[-4:] if len(digits) >= 4 else "****"
        if match.group("routing"):
            return f"routing ****{last4}"
        if match.group("iban"):
            return f"IBAN ****{last4}"
        if match.group("card"):
            return f"card ****{last4}"
        return f"account ****{last4}"

    return BANK_SECRET_RE.sub(_mask, text)


def extract_text_from_bytes(filename: str, content_type: str, data: bytes) -> str:
    name = (filename or "").lower()
    ctype = (content_type or "").lower()
    if not data:
        return ""
    try:
        structured = extract_document(filename, content_type, data)
        if structured:
            return structured
        if name.endswith((".txt", ".md")) or ctype.startswith("text/plain"):
            return decode_text(data)[:400_000]
        if name.endswith(".rtf") or "rtf" in ctype:
            return _rtf_text(data)
        if "html" in ctype or name.endswith((".html", ".htm")):
            return html_to_text(decode_text(data))
        if ctype.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp")):
            return image_text(data)
        if name.endswith(".zip") or "zip" in ctype:
            return zip_text(data)
    except Exception as exc:  # extraction should never fail the pipeline
        return f"[extraction error: {exc}]"
    # Last resort: if it looks like text, keep a sample. Cut on a character boundary, so a
    # UTF-8 character split at byte 2000 does not make a whole text file look binary.
    cut = min(len(data), 2000)
    back = 0
    while cut < len(data) and back < 3 and (data[cut - back] & 0xC0) == 0x80:
        back += 1
    sample = data[: cut - back]
    if b"\x00" not in sample:
        try:
            decoded = sample.decode("utf-8")
            if decoded.isprintable() or "\n" in decoded:
                return decoded
        except UnicodeDecodeError:
            return ""
    return ""


_RTF_TOKEN = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z'])|([{}])|([^\\{}\r\n]+)|[\r\n]+")
# Groups that hold no document text: tables of fonts, colours and styles, document properties, pictures,
# embedded objects and the instructions of a field (its result is the text shown).
_RTF_HIDDEN = {
    "fonttbl", "colortbl", "stylesheet", "info", "pict", "object", "fldinst", "themedata", "colorschememapping",
    "datastore", "latentstyles", "listtable", "listoverridetable", "rsidtbl", "generator", "xmlnstbl", "filetbl",
}
_RTF_BREAKS = {"par": "\n", "line": "\n", "sect": "\n", "page": "\n", "row": "\n", "tab": "\t", "cell": " | "}
_RTF_MARKS = {
    "emdash": "—", "endash": "–", "bullet": "•", "lquote": "‘", "rquote": "’", "ldblquote": "“", "rdblquote": "”",
    "emspace": " ", "enspace": " ", "qmspace": " ",
}


def _rtf_text(data: bytes) -> str:
    """The text of an RTF document. Characters written as codes (\\'a3 for £, \\u8364 for €) are decoded in
    the document's code page; font and colour tables, pictures and other hidden groups are left out."""
    raw = data.decode("latin-1")
    page = re.search(r"\\ansicpg(\d+)", raw[:4096])
    encoding = f"cp{page.group(1)}" if page else "cp1252"
    try:
        b"".decode(encoding)
    except LookupError:
        encoding = "cp1252"
    out: list[str] = []
    coded = bytearray()  # \'hh bytes, decoded together so a two-byte character stays whole
    groups: list[tuple[bool, int]] = []
    hidden, fallback, drop = False, 1, 0  # after \uN, the next ``fallback`` characters repeat it for old readers
    for match in _RTF_TOKEN.finditer(raw):
        word, number, code, symbol, brace, text = match.groups()
        if coded and code is None:
            out.append(coded.decode(encoding, errors="replace"))
            coded.clear()
        if brace == "{":
            groups.append((hidden, fallback))
        elif brace == "}":
            hidden, fallback = groups.pop() if groups else (hidden, fallback)
            drop = 0
        elif symbol == "*" or word in _RTF_HIDDEN:
            hidden = True
        elif hidden:
            continue
        elif code:
            if drop:
                drop -= 1
            else:
                coded.append(int(code, 16))
        elif word == "uc":
            fallback = int(number or 1)
        elif word == "u" and number:
            out.append(chr(int(number) % 65536))
            drop = fallback
        elif word:
            out.append(_RTF_BREAKS.get(word) or _RTF_MARKS.get(word, ""))
        elif symbol:
            out.append({"~": " ", "_": "-", "\r": "\n", "\n": "\n"}.get(symbol, symbol if symbol in "\\{}" else ""))
        elif text:
            if drop:
                text, drop = text[drop:], max(0, drop - len(text))
            out.append(text)
    if coded:
        out.append(coded.decode(encoding, errors="replace"))
    text = "".join(out).encode("utf-16", "surrogatepass").decode("utf-16", errors="replace")
    return collapse_ws(re.sub(r"(?: \| )+(?=\n|$)", "", text))


def explode_archives(items: list, *, limit: int = 40) -> list:
    """Unpack zip attachments into the files inside, so a zipped workbook is still classified. A zip with files
    that weren't unpacked (too many, too large, password-protected) is kept too: its text says which they are."""
    from controller_inbox.models import RawAttachment

    exploded: list[RawAttachment] = []
    for item in items:
        name = (item.filename or "").lower()
        if not name.endswith(".zip") and "zip" not in (item.content_type or "").lower():
            exploded.append(item)
            continue
        skipped: list[tuple[str, str]] = []
        inner = _unzip(item, limit=limit, skipped=skipped)
        exploded.extend(_read_mail_files(item, inner) + ([item] if skipped else []) if inner else [item])
    return exploded


def _read_mail_files(item, parts: list) -> list:
    """Emails saved as files in a zip (.msg, .eml) are read as forwarded emails, as when attached to a message:
    their text, then the files they carried."""
    # Imported here: folder_mail reads mail and imports this module.
    from controller_inbox.folder_mail import mail_file_attachments

    out = []
    for part in parts:
        is_mail = part.filename.lower().endswith((".msg", ".eml"))
        mail = mail_file_attachments(part.filename, "", part.content or b"") if is_mail else None
        for att in mail or []:
            att.id = f"{item.id}:{att.filename}"
        out.extend(mail if mail is not None else [part])
    return out


# A zip bomb is a small file that unpacks to gigabytes: zipped zeros shrink a thousandfold, documents
# a few times. Unpack at most this much in all, and skip a large entry packed tighter than this.
MAX_UNZIPPED = 100_000_000
MAX_ZIP_RATIO = 100


def _zip_plan(infos: list, limit: int) -> tuple[list, list[tuple[str, str]]]:
    """The entries of a zip to unpack, and the files left packed with the reason why."""
    chosen, skipped = [], []
    total = 0
    for info in infos:
        filename = _basename(_entry_name(info))
        if info.is_dir() or info.filename.startswith("__MACOSX") or not filename or filename.startswith("."):
            continue
        if len(chosen) >= limit:
            skipped.append((filename, f"past the first {limit} files"))
        elif info.file_size > 30_000_000:
            skipped.append((filename, "over 30 MB"))
        # zipfile stops reading an entry at its stated size, so the stated sizes bound what is unpacked.
        elif total + info.file_size > MAX_UNZIPPED or (
            info.file_size > 1_000_000 and info.file_size > MAX_ZIP_RATIO * info.compress_size
        ):
            skipped.append((filename, "unpacks to too much"))
        elif info.flag_bits & 0x1:
            skipped.append((filename, "password-protected"))
        else:
            chosen.append(info)
            total += info.file_size
    return chosen, skipped


def _unzip(item, *, limit: int, skipped: list | None = None) -> list:
    import zipfile

    from controller_inbox.models import RawAttachment

    if not item.content:
        return []
    try:
        archive = zipfile.ZipFile(io.BytesIO(item.content))
    except Exception:  # damaged in ways zipfile reports differently (bad name encoding, unknown version): kept as is
        return []
    chosen, left = _zip_plan(archive.infolist(), limit)
    out = []
    for info in chosen:
        filename = _basename(_entry_name(info))
        try:
            payload = archive.read(info)
        except Exception:
            left.append((filename, "damaged"))
            continue
        out.append(
            RawAttachment(
                id=f"{item.id}:{filename}",
                filename=filename,
                content_type="application/octet-stream",
                size_bytes=len(payload),
                content=payload,
            )
        )
    if skipped is not None:
        skipped.extend(left)
    return out


def zip_text(data: bytes) -> str:
    """A zip's own text: which of its files CloseDesk didn't unpack, and why. The files it did unpack are read
    as attachments of their own."""
    import zipfile

    try:
        infos = zipfile.ZipFile(io.BytesIO(data)).infolist()
    except Exception:
        return ""
    chosen, skipped = _zip_plan(infos, 40)
    if not skipped:
        return ""
    shown = [f"- {name} ({why})" for name, why in skipped[:60]]
    if len(skipped) > 60:
        shown.append(f"- … and {len(skipped) - 60} more")
    return (
        f"[Zip file with {len(chosen) + len(skipped)} files. CloseDesk read {len(chosen)} of them separately and did not "
        f"unpack these; open the zip to see them:]\n" + "\n".join(shown)
    )


def _entry_name(info) -> str:
    """A zip entry's name. Without the UTF-8 flag zipfile reads the name as cp437, but many zippers (macOS Archive
    Utility among them) write UTF-8 there anyway: a name that is valid UTF-8 is read as UTF-8."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("utf-8")
    except UnicodeError:
        return info.filename


def _basename(filename: str) -> str:
    return filename.replace("\\", "/").split("/")[-1]


def parse_amount(raw: str) -> float | None:
    try:
        return round(float(raw.replace(",", "")), 2)
    except ValueError:
        return None


def parse_due_date(raw: str, *, as_of: date) -> str | None:
    token = raw.strip()
    low = token.lower()
    if low in {"eod", "cob", "today"}:
        return as_of.isoformat()
    if low == "tomorrow":
        return date.fromordinal(as_of.toordinal() + 1).isoformat()
    weekdays = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
    }
    if low in weekdays:
        delta = (weekdays[low] - as_of.weekday()) % 7
        if delta == 0:
            delta = 7
        return date.fromordinal(as_of.toordinal() + delta).isoformat()
    has_year = len(re.findall(r"\d+", token)) >= 2
    year = as_of.year
    while True:
        try:
            parsed = date_parser.parse(token, default=datetime(year, as_of.month, as_of.day), fuzzy=False).date()
            break
        except (ValueError, OverflowError, TypeError):
            # "February 29" with no year, written in a common year, is the next leap year's.
            if has_year or calendar.isleap(year):
                return None
            year += 1
            while not calendar.isleap(year):
                year += 1
    shift = 0
    if not has_year and (as_of - parsed).days > 90:
        # "January 5" written in late December is next January, not eleven months ago.
        shift = 1
    elif not has_year and (parsed - as_of).days > 270:
        # "December 28" written on January 3 is last December, not eleven months ahead.
        shift = -1
    if shift:
        try:
            parsed = parsed.replace(year=parsed.year + shift)
        except ValueError:
            return None
    return parsed.isoformat()


def extract_fields(text: str, *, as_of: date, extra_vendor: str | None = None) -> ExtractedFields:
    # "Your Adobe invoice - 2026-10-01": a date after the word is the billing date, not the invoice's number.
    invoices = _unique(_normalize_id(m.group(1)) for m in INVOICE_RE.finditer(text) if not _is_date(m.group(1)))
    invoices += [v for v in _unique(_normalize_id(m.group(1)) for m in INVOICE_BARE_RE.finditer(text)) if v not in invoices]
    pos = _unique(_normalize_id(m.group(1)) for m in PO_RE.finditer(text))
    amounts: list[float] = []
    for match in list(AMOUNT_RE.finditer(text)) + list(AMOUNT_WORDS_RE.finditer(text)):
        amount = parse_amount(match.group(1))
        if amount is not None and 0.01 <= amount <= 10_000_000:
            amounts.append(amount)
    amounts = _unique(amounts)
    due_dates: list[str] = []
    for match in DUE_RE.finditer(text):
        parsed = parse_due_date(match.group(1), as_of=as_of)
        if parsed:
            due_dates.append(parsed)
    due_dates = _unique(due_dates)
    vendors = _unique(m.group(1).strip(" \t-,.") for m in VENDOR_RE.finditer(text))
    if extra_vendor:
        vendors = _unique([extra_vendor, *vendors])
    last4: list[str] = []
    mentions_account = False
    numbers = [m.group(1) for m in list(ACCOUNT_RE.finditer(text)) + list(ROUTING_RE.finditer(text))]
    # The bank numbers that are masked are bank numbers here too: an IBAN or account number written in groups
    # ("DE89 3704 0044 0532 0130 00"), or after a dash. Its label holds no digits, so the match's digits are the number's.
    numbers += [m.group(0) for m in BANK_SECRET_RE.finditer(text) if not m.group("card")]
    for number in numbers:
        mentions_account = True
        digits = re.sub(r"\D", "", number)
        if len(digits) >= 4:
            last4.append(digits[-4:])
    return ExtractedFields(
        invoice_numbers=invoices[:8],
        po_numbers=pos[:8],
        amounts=amounts[:8],
        due_dates=due_dates[:6],
        vendor_candidates=vendors[:6],
        account_last4=_unique(last4)[:6],
        mentions_routing_or_account=mentions_account,
        mentions_attachment=bool(ATTACHMENT_MENTION_RE.search(text)),
    )


def _normalize_id(value: str) -> str:
    return re.sub(r"\s+", "", value).strip(".,;:").upper()


def _unique(items: Iterable) -> list:
    seen: set[str] = set()
    out = []
    for item in items:
        if item is None or item == "":
            continue
        key = str(item)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
