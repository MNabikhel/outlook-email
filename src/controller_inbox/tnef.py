"""The files inside a winmail.dat (TNEF), as Outlook sends them to recipients of mail written in Rich Text.

Only the files are read: each one's name (attTransportFilename, the long name among its MAPI properties, else
attAttachTitle) and its bytes (attAttachData). The message's own Rich Text body and any email attached inside are
left out. A damaged or oversized stream gives the files read up to that point.
"""

from __future__ import annotations

import logging
import struct
from pathlib import PurePosixPath, PureWindowsPath

log = logging.getLogger(__name__)

SIGNATURE = 0x223E9F78
MAX_STREAM = 60_000_000
MAX_FILES = 200

_LEVEL_ATTACHMENT = 2
_ATT_RENDDATA = 0x00069002  # an attachment starts
_ATT_TITLE = 0x00018010
_ATT_DATA = 0x0006800F
_ATT_TRANSPORT_NAME = 0x00019001
_ATT_MAPI = 0x00069005  # the attachment's MAPI properties
_PR_ATTACH_LONG_FILENAME = 0x3707

# MAPI property types with a fixed size (each value padded to 4 bytes).
_FIXED = {0x0002: 2, 0x0003: 4, 0x0004: 4, 0x0005: 8, 0x0006: 8, 0x0007: 8, 0x000A: 4, 0x000B: 2, 0x0014: 8,
          0x0040: 8, 0x0048: 16}
_VARIABLE = {0x000D, 0x001E, 0x001F, 0x0102}


def is_tnef(data: bytes) -> bool:
    return len(data) >= 6 and struct.unpack_from("<I", data)[0] == SIGNATURE


def attachments(data: bytes) -> list[tuple[str, bytes]]:
    """(file name, bytes) for each file in the stream that has both."""
    if not is_tnef(data) or len(data) > MAX_STREAM:
        return []
    files: list[dict] = []
    pos = 6
    try:
        while pos + 9 <= len(data) and len(files) <= MAX_FILES:
            level, attr, length = struct.unpack_from("<BII", data, pos)
            start = pos + 9
            end = start + length
            if end + 2 > len(data):
                break  # cut short: what was read so far is kept
            value = data[start:end]
            pos = end + 2  # the checksum
            if level != _LEVEL_ATTACHMENT:
                continue
            if attr == _ATT_RENDDATA or not files:
                files.append({})
            current = files[-1]
            if attr == _ATT_TITLE:
                current["title"] = _text(value)
            elif attr == _ATT_TRANSPORT_NAME:
                current["transport"] = _text(value)
            elif attr == _ATT_DATA:
                current["data"] = value
            elif attr == _ATT_MAPI:
                long_name = _long_filename(value)
                if long_name:
                    current["long"] = long_name
    except struct.error:
        log.warning("A winmail.dat ended in the middle of an entry")
    out = []
    for item in files[:MAX_FILES]:
        name = _safe_name(item.get("transport") or item.get("long") or item.get("title") or "")
        if name and item.get("data"):
            out.append((name, item["data"]))
    return out


def _text(value: bytes) -> str:
    return value.split(b"\x00", 1)[0].decode("cp1252", errors="replace").strip()


def _safe_name(name: str) -> str:
    """Only the file's own name: a path written into the stream ("..\\..\\x.pdf") is not followed."""
    name = PureWindowsPath(PurePosixPath(name.replace("\x00", "")).name).name.strip()
    return "" if name in {"", ".", ".."} else name[:200]


def _long_filename(blob: bytes) -> str:
    """PR_ATTACH_LONG_FILENAME among an attachment's MAPI properties, or "" when it isn't there or the
    properties can't be read."""
    try:
        (count,) = struct.unpack_from("<I", blob, 0)
        pos = 4
        for _ in range(min(count, 1000)):
            ptype, pid = struct.unpack_from("<HH", blob, pos)
            pos += 4
            if pid >= 0x8000:  # a named property: its GUID, then a number or a name
                (kind,) = struct.unpack_from("<I", blob, pos + 16)
                pos += 20
                if kind == 0:
                    pos += 4
                else:
                    (size,) = struct.unpack_from("<I", blob, pos)
                    pos += 4 + _pad(size)
            multi = bool(ptype & 0x1000)
            base = ptype & ~0x1000
            values = 1
            if multi or base in _VARIABLE:
                (values,) = struct.unpack_from("<I", blob, pos)
                pos += 4
            if base in _VARIABLE:
                for _ in range(min(values, 10_000)):
                    (size,) = struct.unpack_from("<I", blob, pos)
                    item = blob[pos + 4 : pos + 4 + size]
                    pos += 4 + _pad(size)
                    if pid == _PR_ATTACH_LONG_FILENAME and base == 0x001E:
                        return _text(item)
                    if pid == _PR_ATTACH_LONG_FILENAME and base == 0x001F:
                        return item.decode("utf-16-le", errors="replace").split("\x00", 1)[0].strip()
            elif base in _FIXED:
                pos += values * _pad(_FIXED[base])
            else:
                return ""  # a type not known here: the rest can't be found
            if pos > len(blob):
                return ""
    except struct.error:
        return ""
    return ""


def _pad(size: int) -> int:
    return (size + 3) & ~3
