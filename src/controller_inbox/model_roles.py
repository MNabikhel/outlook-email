"""Which model does which job, for Setup and the workspace's Settings: the model that answers questions, the one that
reads scanned pages (OvisOCR2 by default, the older method when it can't), the one that finds mail by meaning, and
the OCR engine that reads scans the moment they arrive."""

from __future__ import annotations

import math
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from controller_inbox.local_llm import ModelStatus, check_model

if TYPE_CHECKING:
    from controller_inbox.config import Settings


def _loaded(status: ModelStatus, model: str) -> bool:
    """Whether ``model`` (an id or LM Studio's key for it) is loaded on the server now."""
    if not model:
        return False
    if model in status.instances or model in {key for key, _context in status.instances.values()}:
        return True
    return not status.lm_studio and model == status.model


def chat_row(status: ModelStatus) -> dict:
    row = {"role": "Answers your questions", "model": "", "state": "off", "status": "", "note": ""}
    if status.mode == "off":
        row["status"] = "Turned off in CloseDesk's settings file (.env): answers come from looking things up."
    elif status.active:
        row["model"], row["state"] = status.model, "on"
        context = f" with a {status.context_length:,}-token context" if status.context_length else ""
        row["status"] = f"Loaded{context}."
    elif status.reachable:
        row["status"] = "No model loaded: load one in LM Studio (Qwen3.5 9B)."
    else:
        row["status"] = "LM Studio's server isn't answering: answers come from looking things up."
    return row


def reader_row(settings: Settings, status: ModelStatus) -> dict:
    """The page reader, or the older method scans fall back to, and why."""
    from controller_inbox import vision

    row = {"role": "Reads scanned pages and pictures", "model": "", "state": "fallback", "status": "", "note": ""}
    if settings.vision_mode == "off":
        row["state"] = "off"
        row["status"] = "Turned off in Setup: scans are read with OCR only (the older method)."
        return row
    reader = vision.reading_model(settings)
    if reader and vision.trusted_reader(reader) and vision.can_render():
        row["model"], row["state"] = vision.reader_name(reader), "on"
        row["status"] = "Loaded." if _loaded(status, reader) else "Downloaded: LM Studio loads it when a page is read."
        if settings.vision_mode == "ask":
            row["status"] += " Pages are read only when you ask."
        return row
    row["status"] = f"Scans are read with {vision.older_method(settings, reader)}."
    row["note"] = vision.why_not_reader(settings, reader)
    if reader:
        row["model"] = reader
    return row


def search_row(settings: Settings, status: ModelStatus) -> dict:
    from controller_inbox import semantic

    model = semantic.embedding_model(settings) if status.reachable else ""
    if not model:
        return {"role": "Finds mail by meaning", "model": "", "state": "off", "note": "",
                "status": "Off: search matches words only. Load an embedding model in LM Studio (nomic-embed-text)."}
    # LM Studio lists every downloaded model on /v1/models (just-in-time loading): only its loaded instances count.
    loaded = model in status.embeddings if status.lm_studio else _loaded(status, model) or model in status.models
    return {"role": "Finds mail by meaning", "model": model, "state": "on", "note": "",
            "status": "Loaded." if loaded else "Downloaded: LM Studio loads it when mail is searched."}


def ocr_row() -> dict:
    from controller_inbox import ocr

    engine = ocr.engine_name()
    if engine:
        return {"role": "Reads scans as they arrive (OCR)", "model": engine, "state": "on", "note": "",
                "status": "On this computer: a scan's text is there in seconds, before the page reader runs."}
    return {"role": "Reads scans as they arrive (OCR)", "model": "", "state": "off", "note": "",
            "status": "Not installed: scans wait for the page reader. " + ocr.missing_note(why_only=True)}


def chat_choices(settings: Settings, status: ModelStatus) -> dict | None:
    """What Setup's chat model list offers: LM Studio's downloaded chat models (``models``), the one answering now
    (``current``, its key) and the loaded ones (``loaded``). ``pinned`` when the settings name a model, so loading
    another wouldn't change which one answers. ``too_big``: why each model too big for this computer's memory is
    (see ``too_big``). None when the server can't load models when asked (not LM Studio)."""
    if status.mode == "off" or not status.lm_studio or not status.chat_models:
        return None
    return {
        "models": list(status.chat_models),
        "current": status.instances.get(status.model, (status.model, 0))[0],
        "loaded": {key for key, _context in status.instances.values()},
        "pinned": settings.llm_model not in {"", "local-model"},
        "too_big": too_big(settings, status),
    }


GB = 1024**3
# What a model takes beside its download for each token of context it is loaded with (its key-value cache, at LM
# Studio's usual 16-bit size). It grows with the model: Llama 3.1 8B (about 5 GB at Q4) takes 128 KB a token, Qwen 2.5
# 32B (about 20 GB) 256 KB, Llama 3.1 70B (about 43 GB) 320 KB, and older dense 27B to 32B models up to 500 KB. So it
# is counted as 12 KB a token for each GB of the model, never less than an 8B's 128 KB: about right at 27B to 32B and
# on the safe side for 70B, for Gemma 3 (most of its layers keep only the last 1,024 tokens) and for
# mixture-of-experts models, which take less.
CONTEXT_BYTES_PER_TOKEN = 128 * 1024
CONTEXT_BYTES_PER_TOKEN_PER_GB = 12 * 1024
# The working memory LM Studio sets aside beside the model and its context to work out each answer.
COMPUTE_BYTES = 1 * GB
# The share of a PC's memory a model can count on, and of a Mac's with more than 36 GB: macOS lets the graphics side
# use about three quarters there, and about two thirds on a Mac with 36 GB or less.
USABLE_SHARE = 0.75
SMALL_MAC_SHARE = 2 / 3
SMALL_MAC = 36 * GB
# Kept for the system, Outlook and the browser, whatever the share leaves.
KEPT_FOR_APPS = 4 * GB
# A graphics card with less is too small to hold a model: the model runs in the computer's memory.
SMALLEST_CARD = 4 * GB
# Graphics built into the processor, by the name its driver gives it on Windows: "AMD Radeon(TM) Graphics", "AMD
# Radeon 780M Graphics", "Intel(R) UHD Graphics", "Intel(R) Arc(TM) Graphics". Its memory is a share of the
# computer's set aside for it, so a model there runs no faster than from the computer's memory. A card of its own
# is named by its model: "AMD Radeon RX 7800 XT", "AMD Radeon PRO W7600", "Intel(R) Arc(TM) A770 Graphics". The
# graphics built into Ryzen 2000 and 3000 processors carry RX too ("AMD Radeon(TM) RX Vega 10 Graphics"), so a Vega
# with a number and then "Graphics" is built in, where a Vega card is named "Radeon RX Vega 64".
_BUILT_IN = re.compile(r"graphics", re.IGNORECASE)
_BUILT_IN_VEGA = re.compile(r"\bVega\s*\d+\s+Graphics", re.IGNORECASE)
_A_CARD = re.compile(r"\bRX\b|\bPRO\b|\bArc(\(TM\))?\s+[AB]\d|NVIDIA|GeForce|Quadro", re.IGNORECASE)
DRM_CARDS = Path("/sys/class/drm")
PROBE_SECONDS = 2
_THIS_COMPUTER = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def computer_memory() -> int:
    """This computer's memory in bytes, or 0 when it can't be told."""
    try:
        if os.name == "nt":
            import ctypes

            class _Memory(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), *[(name, ctypes.c_ulonglong) for name in (
                    "total", "free", "total_page", "free_page", "total_virtual", "free_virtual", "free_extended")]]

            memory = _Memory()
            memory.length = ctypes.sizeof(_Memory)
            return int(memory.total) if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)) else 0
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, ValueError):
        return 0


@lru_cache(maxsize=1)
def _apple_silicon() -> bool:
    """A Mac with Apple's own chip, where the model and the computer share one memory."""
    if sys.platform != "darwin":
        return False
    if platform.machine() == "arm64":
        return True
    # An Intel build of Python (under Rosetta) says x86_64 on an Apple chip too: the Mac itself says which it is.
    try:
        done = subprocess.run(
            ["sysctl", "-n", "hw.optional.arm64"], capture_output=True, text=True, timeout=PROBE_SECONDS, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return (done.stdout or "").strip() == "1"


def _nvidia_memory() -> int:
    """The memory of the biggest NVIDIA card, as nvidia-smi gives it, or 0."""
    program = shutil.which("nvidia-smi")
    if not program:
        return 0
    try:
        done = subprocess.run(
            [program, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=PROBE_SECONDS, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),  # on Windows, no console flashes up
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    if done.returncode:
        return 0
    found = [int(line.strip()) for line in (done.stdout or "").splitlines() if line.strip().isdigit()]
    return max(found, default=0) * 1024**2  # nvidia-smi gives MiB


def _windows_card_memory() -> int:
    """The memory of the biggest graphics card, as its driver wrote it in the registry, or 0. Windows' WMI
    (Win32_VideoController.AdapterRAM) stops at 4 GB, so it isn't asked."""
    try:
        import winreg
    except ImportError:
        return 0
    cards = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    best = 0
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, cards) as listed:
            for index in range(32):
                try:
                    name = winreg.EnumKey(listed, index)
                except OSError:
                    break
                if not name.isdigit():  # "Properties" and the like: not a card
                    continue
                try:
                    with winreg.OpenKey(listed, name) as card:
                        value, _kind = winreg.QueryValueEx(card, "HardwareInformation.qwMemorySize")
                        try:
                            said, _kind = winreg.QueryValueEx(card, "DriverDesc")
                        except OSError:
                            said = ""
                except OSError:
                    continue
                if _built_in(str(said or "")):
                    continue
                if isinstance(value, bytes):  # some drivers write it as 8 bytes
                    value = int.from_bytes(value[:8], "little")
                if isinstance(value, int):
                    best = max(best, value)
    except OSError:
        return 0
    return best


def _built_in(name: str) -> bool:
    """The graphics named ``name`` is built into the processor (see _BUILT_IN)."""
    if _BUILT_IN_VEGA.search(name):
        return True
    return bool(_BUILT_IN.search(name)) and not _A_CARD.search(name)


def _linux_card_memory() -> int:
    """The memory of the biggest AMD card on Linux, as its driver gives it, or 0. Graphics built into the processor
    is left out: the driver gives the share of the computer's memory set aside for it as its memory too, but says
    who made the memory (mem_info_vram_vendor) only for a card with memory of its own."""
    best = 0
    for path in DRM_CARDS.glob("card*/device/mem_info_vram_total"):
        if not (path.parent / "mem_info_vram_vendor").exists():
            continue
        try:
            best = max(best, int(path.read_text().strip()))
        except (OSError, ValueError):
            continue
    return best


def _probe_gpu() -> int:
    """The memory of this PC's graphics card in bytes: 0 when it has none a model runs on, or it can't be told."""
    found = _nvidia_memory()
    if not found and os.name == "nt":
        found = _windows_card_memory()
    if not found and sys.platform.startswith("linux"):
        found = _linux_card_memory()
    return found if found >= SMALLEST_CARD else 0


_gpu_found: int | None = None  # what _probe_gpu found this run (None: not looked yet), so it is looked for once
_gpu_lock = threading.Lock()


def gpu_memory() -> int:
    """The memory of this PC's graphics card (0 when there is none to count), looked for once a run."""
    global _gpu_found
    with _gpu_lock:
        if _gpu_found is None:
            try:
                _gpu_found = _probe_gpu()
            except (OSError, ValueError, subprocess.SubprocessError):
                _gpu_found = 0
        return _gpu_found


@dataclass(frozen=True)
class Budget:
    """The memory a model can take on this computer. ``runs_well``: what it can take and still answer at speed (a
    PC's graphics card, when it has one); ``loads``: what it can take at all (the computer's own memory, and on a PC
    with a graphics card the card's beside it, as LM Studio puts what doesn't fit on the card in the computer's
    memory); ``memory``: the computer's memory in all; ``gpu``: the card's (0 without one); ``mac``: a Mac with Apple's
    chip."""

    runs_well: int
    loads: int
    memory: int
    gpu: int = 0
    mac: bool = False

    def said(self) -> str:
        """Which memory a model was compared against, in words for the warnings."""
        if self.gpu:
            return f"this PC's graphics card has {round(self.gpu / GB)} GB"
        where = "this Mac" if self.mac else "this computer"
        # Rounded down, as what a model needs is rounded up: "needs about 5 GB, can give about 4 GB", never 4 and 4.
        return f"{where} can give a model about {int(self.runs_well // GB)} GB of its {round(self.memory / GB)} GB"

    def said_at_all(self) -> str:
        """What a model can take at all, in words for the out-of-memory warning: on a PC with a graphics card, the
        card's memory and the computer's together."""
        if not self.gpu:
            return self.said()
        return (
            f"this PC can give a model about {int(self.loads // GB)} GB: its graphics card's {round(self.gpu / GB)} GB "
            f"and {int((self.loads - self.gpu) // GB)} GB of its {round(self.memory / GB)} GB of memory"
        )


def model_budget() -> Budget | None:
    """What this computer can give a model (see ``Budget``), or None when its memory can't be told. A Mac with
    Apple's chip: the share macOS lets the graphics side use. A PC: three quarters of its memory, and with a graphics
    card, the card's memory for running well, and the card's and those three quarters together for loading at all.
    Either way at least KEPT_FOR_APPS of the computer's memory is left for the rest."""
    memory = computer_memory()
    if not memory:
        return None
    mac = _apple_silicon()
    share = SMALL_MAC_SHARE if mac and memory <= SMALL_MAC else USABLE_SHARE
    usable = max(0, int(min(share * memory, memory - KEPT_FOR_APPS)))
    gpu = 0 if mac else gpu_memory()
    return Budget(runs_well=gpu or usable, loads=gpu + usable, memory=memory, gpu=gpu, mac=mac)


def memory_needed(size_bytes: int, context_tokens: int) -> int:
    """About what loading a model of ``size_bytes`` (its download) with ``context_tokens`` takes: the model, its
    context (more a token for a bigger model) and the working memory beside them."""
    per_token = max(CONTEXT_BYTES_PER_TOKEN, size_bytes * CONTEXT_BYTES_PER_TOKEN_PER_GB // GB)
    return size_bytes + max(0, context_tokens) * per_token + COMPUTE_BYTES


def room_for(size_bytes: int, smallest: int, budget: Budget) -> int:
    """The memory a model of ``size_bytes`` is fitted into: what runs at speed (a PC's graphics card) when it fits
    there with the ``smallest`` context CloseDesk would ask for, else what this computer can give it at all (it runs
    partly from the computer's memory at any context, so a longer one doesn't slow it more)."""
    return budget.runs_well if memory_needed(size_bytes, smallest) <= budget.runs_well else budget.loads


def on_this_computer(settings: Settings, status: ModelStatus) -> bool:
    """LM Studio runs on this computer, so this computer's memory is the one a model loads into."""
    return (urlparse(status.base_url or settings.llm_base_url).hostname or "") in _THIS_COMPUTER


def context_for(settings: Settings, status: ModelStatus, key: str) -> int:
    """The context ``key`` is loaded with: the one Setup asks for, but no more than the model supports."""
    most = status.chat_models.get(key, 0)
    return min(settings.min_context_tokens, most) if settings.min_context_tokens and most else settings.min_context_tokens


def fits(
    settings: Settings, status: ModelStatus, key: str, budget: Budget, *, at_all: bool = False, shorter: bool = False
) -> bool:
    """``key`` runs well in this computer's ``budget`` with the context Setup asks for (False when its size isn't
    known). ``at_all``: it loads at all, though partly from the computer's memory beside the graphics card.
    ``shorter``: with the shortest context CloseDesk would ask for it (``model_fit.auto_context`` steps a model too
    big for Setup's down to that), rather than Setup's."""
    from controller_inbox import model_fit  # it imports this module

    size = status.chat_sizes.get(key, 0)
    room = budget.loads if at_all else budget.runs_well
    tokens = context_for(settings, status, key)
    if shorter and tokens:
        tokens = min(tokens, model_fit.SMALLEST_CONTEXT)
    return bool(size) and memory_needed(size, tokens) <= room


def too_big(settings: Settings, status: ModelStatus) -> dict[str, str]:
    """The downloaded chat models too big to run well on this computer with the context Setup asks for, each with
    why: CloseDesk loads it with a shorter context, it runs slowly from the computer's memory, or it doesn't fit at
    all. A small model (``model_choice.AUTO_LARGEST`` or less) that runs well with the shorter context isn't listed:
    a smaller one wouldn't help. Nothing when LM Studio doesn't give the sizes, the memory can't be read, or LM Studio
    is on another computer."""
    from controller_inbox import model_choice, model_fit  # they import this module

    budget = model_budget()
    if not status.chat_sizes or not budget or not on_this_computer(settings, status):
        return {}
    found = {}
    for key, size in status.chat_sizes.items():
        tokens = context_for(settings, status, key)
        needed = memory_needed(size, tokens)
        if needed <= budget.runs_well:
            continue
        # What CloseDesk would ask LM Studio for: the most of Setup's context that fits.
        asked = model_fit.auto_context(settings, status, key)
        shorter = 0 < asked < tokens
        needed_asked = memory_needed(size, asked) if shorter else needed
        context = f" with a {tokens:,}-token context" if tokens else ""
        instead = f"CloseDesk loads it with a {asked:,}-token context instead, so long attachments are read a section at a time"
        if shorter and needed_asked <= budget.runs_well:
            if size <= model_choice.AUTO_LARGEST:
                continue
            what = instead
        elif needed_asked > budget.loads:
            what = "LM Studio will likely run out of memory with it"
        elif shorter:
            what = f"{instead}, and most of it would run from the computer's memory and answer slowly"
        else:
            what = "most of it would run from the computer's memory and answer slowly"
        # What it was compared against: for running out of memory, the card's and the computer's together.
        compared = budget.said_at_all() if needed_asked > budget.loads else budget.said()
        found[key] = (
            f"{key} needs about {math.ceil(needed / GB)} GB of memory{context}, and {compared}, so {what}. "
            "CloseDesk only needs a small model: a 3B to 8B one is plenty and answers faster."
        )
    return found


def models_in_use(settings: Settings, *, status: ModelStatus | None = None) -> list[dict]:
    """One row per job: ``role``, ``model`` (its name, "" when none), ``state`` ("on", "fallback" for the older
    method, "off"), ``status`` (loaded, downloaded, or what happens instead) and ``note`` (why, for a fallback)."""
    status = status or check_model(settings)
    return [chat_row(status), reader_row(settings, status), search_row(settings, status), ocr_row()]
