"""How much memory a model takes, how much this computer can give it, and which model CloseDesk loads by itself.

Every model was counted at 128 KB of context a token (an 8B's figure; a 31B takes about twice that) against three
quarters of the computer's memory, whatever the computer. So a 19 GB 31B model on a 32 GB Mac looked like it fitted
at 16k (21 GB of 24 GB) though macOS lets a model use only about 21 GB there: no warning, and the automatic start-up
choice (the largest that fits) loaded it itself. The same on a Windows PC with 32 GB and an 8 GB graphics card,
where the model runs at speed only in the card's 8 GB.
"""

from __future__ import annotations

import subprocess
import sys
import types

import pytest
from test_lm_studio_models import CHAT, CODER, LMStudio, _serve

from controller_inbox import model_choice, model_roles
from controller_inbox.local_llm import check_model, use_chat_model

GB = 1024**3
BIG, MID, GEMMA27 = "google/gemma-4-31b", "mistral-small-24b", "google/gemma-3-27b"
# Download sizes at about Q4_K_M.
SIZES = {CHAT: int(4.9 * GB), CODER: int(4.7 * GB), BIG: 19 * GB, MID: int(14.3 * GB)}


@pytest.fixture
def lm(settings, monkeypatch):
    settings.llm = None
    settings.ensure_data_dir()
    server = LMStudio(loaded=[])
    server.extra = [BIG, MID]
    server.sizes = dict(SIZES)
    _serve(monkeypatch, server)
    return server


def _computer(monkeypatch, memory_gb: float, *, mac: bool = False, card_gb: float = 0) -> None:
    monkeypatch.setattr(model_roles, "computer_memory", lambda: int(memory_gb * GB))
    monkeypatch.setattr(model_roles, "_apple_silicon", lambda: mac, raising=False)
    monkeypatch.setattr(model_roles, "_gpu_found", int(card_gb * GB), raising=False)


def _answering(lm, settings, key):
    from controller_inbox import local_llm

    lm.loaded = [key]
    lm.contexts = {key: 16384}
    local_llm._status_cache.clear()
    return check_model(settings)


# What a model takes ------------------------------------------------------------------------------------------


def test_a_bigger_model_takes_more_memory_for_each_token_of_context():
    per_token = lambda size: (model_roles.memory_needed(size, 16384) - model_roles.memory_needed(size, 0)) / 16384  # noqa: E731
    assert per_token(int(4.9 * GB)) == pytest.approx(128 * 1024, rel=0.05), "Llama 3.1 8B: 128 KB a token"
    assert per_token(int(19.9 * GB)) >= 230 * 1024, "Qwen 2.5 32B: 256 KB a token"
    assert per_token(int(42.5 * GB)) >= 320 * 1024, "Llama 3.1 70B: 320 KB a token"


def test_the_working_memory_beside_the_model_is_counted():
    assert model_roles.memory_needed(5 * GB, 0) >= 5.9 * GB


# What this computer can give -----------------------------------------------------------------------------------


def test_a_31b_model_on_a_32_gb_mac_is_flagged_and_not_loaded_by_itself(settings, store, lm, monkeypatch):
    _computer(monkeypatch, 32, mac=True)
    status = check_model(settings, use_cache=False)
    warning = model_roles.too_big(settings, status)[BIG]
    assert "about 24 GB" in warning and "this Mac can give a model about 21 GB" in warning and "3B to 8B" in warning
    # CloseDesk loads it with the context that fits (model_fit), so it doesn't say LM Studio runs out of memory.
    assert "it asks for a 4,096-token context" in warning and "run out of memory" not in warning
    assert CHAT not in model_roles.too_big(settings, status)
    assert model_choice.auto_choice(settings, status) == CHAT
    lm.sizes = {BIG: 19 * GB}
    assert model_choice.auto_choice(settings, check_model(settings, use_cache=False)) == "", "not even when it is the only one"
    lm.sizes = dict(SIZES)
    # Loaded anyway (the user chose it in Settings): the suggestion says which memory it was compared against.
    tip = model_choice.recommendation(settings, store, _answering(lm, settings, BIG))
    assert tip["reason"] == "too_big" and tip["suggested"] == CHAT
    assert "this Mac can give a model about 21 GB" in tip["message"]


def test_a_pc_with_an_8_gb_graphics_card_flags_a_19_gb_model(settings, store, lm, monkeypatch):
    _computer(monkeypatch, 32, card_gb=8)
    status = check_model(settings, use_cache=False)
    flagged = model_roles.too_big(settings, status)
    assert "this PC's graphics card has 8 GB" in flagged[BIG] and "3B to 8B" in flagged[BIG]
    assert "this PC's graphics card has 8 GB" in flagged[MID]
    assert CHAT not in flagged, "an 8B at 16k (about 8 GB) runs on the card"
    assert model_choice.auto_choice(settings, status) == CHAT
    tip = model_choice.recommendation(settings, store, _answering(lm, settings, BIG))
    assert tip["reason"] == "too_big" and "this PC's graphics card has 8 GB" in tip["message"]
    assert tip["message"].endswith(f"{CHAT} fits on its graphics card.")


def test_a_model_too_big_for_the_card_but_not_the_pc_is_said_to_run_slowly(settings, lm, monkeypatch):
    _computer(monkeypatch, 64, card_gb=8)
    warning = model_roles.too_big(settings, check_model(settings, use_cache=False))[MID]
    assert "run from the computer's memory" in warning and "run out of memory" not in warning


def test_with_no_model_running_well_on_the_card_one_that_loads_is_still_chosen(settings, lm, monkeypatch):
    _computer(monkeypatch, 32, card_gb=4)
    lm.sizes = {CHAT: int(6.6 * GB), BIG: 19 * GB}
    assert model_choice.auto_choice(settings, check_model(settings, use_cache=False)) == CHAT


def test_a_smaller_model_just_as_much_too_big_for_the_card_isn_t_suggested(settings, store, lm, monkeypatch):
    """A 14B on a PC with an 8 GB card: the only smaller one, a 12B, doesn't fit the card either. It was suggested as
    "fits in its memory", while the same page said it would run from the computer's memory and answer slowly."""
    _computer(monkeypatch, 32, card_gb=8)
    fourteen, twelve = "qwen/qwen3-14b", "google/gemma-3-12b"
    lm.extra = [fourteen, twelve]
    lm.sizes = {fourteen: 9 * GB, twelve: int(7.5 * GB)}
    status = check_model(settings, use_cache=False)
    assert twelve in model_roles.too_big(settings, status)
    assert model_choice.recommendation(settings, store, _answering(lm, settings, fourteen)) is None


def test_a_64_gb_mac_still_takes_a_27b_when_the_user_picks_it(settings, store, lm, monkeypatch):
    _computer(monkeypatch, 64, mac=True)
    lm.extra = [GEMMA27]
    lm.sizes = {CHAT: int(4.9 * GB), GEMMA27: 17 * GB}
    status = check_model(settings, use_cache=False)
    assert model_roles.too_big(settings, status) == {}
    assert use_chat_model(settings, GEMMA27) == "" and GEMMA27 in lm.loaded
    assert model_choice.recommendation(settings, store, _answering(lm, settings, GEMMA27)) is None


@pytest.mark.parametrize("mac", [True, False], ids=["8 GB Mac", "8 GB PC"])
def test_an_8_gb_computer_still_chooses_a_small_model_with_a_shorter_context(settings, lm, monkeypatch, mac):
    """An 8 GB computer can give a model about 4 GB: a 3B or a 1.5B doesn't fit with Setup's 16,384 tokens, but does
    with 8,192, which is what CloseDesk asks for. Before, no model was chosen at the start and every one was marked
    too big, the 1.5B with "needs about 4 GB ... can give about 4 GB, so LM Studio will likely run out of memory"."""
    _computer(monkeypatch, 8, mac=mac)
    small, tiny = "llama-3.2-3b-instruct", "qwen2.5-1.5b-instruct"
    lm.extra = [small, tiny]
    lm.sizes = {small: 2 * GB, tiny: int(1.1 * GB)}
    status = check_model(settings, use_cache=False)
    assert model_roles.too_big(settings, status) == {}
    assert model_choice.auto_choice(settings, status) == small
    assert model_choice.load_at_start(settings)["model"] == small
    assert lm.calls[-1] == ("load", small, 8192)


def test_a_model_that_doesnt_fit_even_with_the_shortest_context_still_says_it_runs_out_of_memory(settings, lm, monkeypatch):
    _computer(monkeypatch, 8, mac=True)
    warning = model_roles.too_big(settings, check_model(settings, use_cache=False))[CHAT]
    assert "run out of memory" in warning and "needs about 8 GB" in warning, warning


def test_a_small_mac_keeps_memory_for_macos_and_outlook(monkeypatch):
    _computer(monkeypatch, 8, mac=True)
    assert model_roles.model_budget().runs_well == 4 * GB, "8 GB less 4 GB, under two thirds"
    _computer(monkeypatch, 36, mac=True)
    assert model_roles.model_budget().runs_well == 24 * GB
    _computer(monkeypatch, 64, mac=True)
    assert model_roles.model_budget().runs_well == 48 * GB


def test_a_pc_without_a_graphics_card_counts_its_memory(monkeypatch):
    _computer(monkeypatch, 32)
    budget = model_roles.model_budget()
    assert budget.runs_well == budget.loads == 24 * GB
    _computer(monkeypatch, 8)
    assert model_roles.model_budget().runs_well == 4 * GB
    _computer(monkeypatch, 0)
    assert model_roles.model_budget() is None


# The automatic choice ------------------------------------------------------------------------------------------


def test_the_automatic_choice_prefers_an_8b_to_a_31b_on_a_64_gb_computer(settings, lm, monkeypatch):
    _computer(monkeypatch, 64)
    status = check_model(settings, use_cache=False)
    assert model_roles.too_big(settings, status) == {}, "all of them fit"
    assert model_choice.auto_choice(settings, status) == CHAT
    assert model_choice.load_at_start(settings)["model"] == CHAT and lm.loaded == [CHAT]


def test_with_only_big_models_the_smallest_that_fits_is_chosen(settings, lm, monkeypatch):
    _computer(monkeypatch, 64)
    lm.sizes = {BIG: 19 * GB, MID: int(14.3 * GB)}
    assert model_choice.auto_choice(settings, check_model(settings, use_cache=False)) == MID


# Finding the graphics card -------------------------------------------------------------------------------------


def _nvidia(monkeypatch, answer):
    """nvidia-smi on the PATH, answering with ``answer`` (text, or an exception to raise). Returns the calls."""
    calls = []

    def run(command, **kw):
        calls.append((command, kw))
        if isinstance(answer, BaseException):
            raise answer
        return subprocess.CompletedProcess(command, 0, stdout=answer, stderr="")

    monkeypatch.setattr(model_roles, "_gpu_found", None, raising=False)
    monkeypatch.setattr(model_roles.shutil, "which", lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None)
    monkeypatch.setattr(model_roles.subprocess, "run", run)
    monkeypatch.setattr(model_roles, "_linux_card_memory", lambda: 0)
    return calls


def test_an_nvidia_card_is_asked_once_a_run(settings, store, lm, monkeypatch):
    monkeypatch.setattr(model_roles, "computer_memory", lambda: 32 * GB)
    calls = _nvidia(monkeypatch, "8192\n")
    status = check_model(settings, use_cache=False)
    for _ in range(3):
        assert "this PC's graphics card has 8 GB" in model_roles.too_big(settings, status)[BIG]
        model_choice.auto_choice(settings, status)
        model_roles.chat_choices(settings, status)
        model_choice.recommendation(settings, store, status)
    assert len(calls) == 1
    command, kw = calls[0]
    assert command[1:] == ["--query-gpu=memory.total", "--format=csv,noheader,nounits"] and kw["timeout"] <= 2


@pytest.mark.parametrize("answer", [
    subprocess.TimeoutExpired("nvidia-smi", 2), OSError("not allowed"), "[N/A]\n", "", "NVIDIA-SMI has failed\n",
])
def test_a_graphics_card_that_cant_be_read_falls_back_to_the_computers_memory(settings, lm, monkeypatch, answer):
    monkeypatch.setattr(model_roles, "computer_memory", lambda: 24 * GB)
    calls = _nvidia(monkeypatch, answer)
    assert model_roles.gpu_memory() == 0 and model_roles.gpu_memory() == 0 and len(calls) == 1
    budget = model_roles.model_budget()
    assert budget.runs_well == budget.loads == 18 * GB and budget.gpu == 0
    assert "this computer can give a model about 18 GB of its 24 GB" in model_roles.too_big(settings, check_model(settings, use_cache=False))[BIG]


def test_a_card_built_into_the_processor_doesnt_count(monkeypatch):
    _nvidia(monkeypatch, "2048\n")
    assert model_roles.gpu_memory() == 0
    _nvidia(monkeypatch, "4096\n24576\n")  # two cards: the bigger one
    assert model_roles.gpu_memory() == 24 * GB


def test_windows_cards_memory_is_read_from_the_registry_not_wmi(monkeypatch):
    """WMI's AdapterRAM stops at 4 GB; the driver's qwMemorySize in the registry is the card's own figure."""
    values = {"0000": 512 * 1024**2, "0001": 12 * GB, "Properties": None}

    class Key:
        def __init__(self, name):
            self.name = name

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def open_key(parent, name):
        if isinstance(parent, Key) and values.get(name) is None:
            raise OSError("no such key")
        return Key(name)

    def enum_key(_key, index):
        names = list(values)
        if index >= len(names):
            raise OSError("no more")
        return names[index]

    def query(key, value):
        if value == "DriverDesc":
            raise OSError("not written")
        assert value == "HardwareInformation.qwMemorySize"
        return values[key.name], 11

    fake = types.SimpleNamespace(HKEY_LOCAL_MACHINE="HKLM", OpenKey=open_key, EnumKey=enum_key, QueryValueEx=query)
    monkeypatch.setitem(sys.modules, "winreg", fake)
    assert model_roles._windows_card_memory() == 12 * GB
    values["0001"] = (12 * GB).to_bytes(8, "little")  # some drivers write it as binary
    assert model_roles._windows_card_memory() == 12 * GB


def _windows_cards(monkeypatch, cards: dict[str, tuple[str, int]]) -> None:
    """The registry as Windows has it: each card's name (DriverDesc) and memory (qwMemorySize)."""

    class Key:
        def __init__(self, name):
            self.name = name

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def enum_key(_key, index):
        names = list(cards)
        if index >= len(names):
            raise OSError("no more")
        return names[index]

    def query(key, value):
        name, memory = cards[key.name]
        return (name, 1) if value == "DriverDesc" else (memory, 11)

    fake = types.SimpleNamespace(HKEY_LOCAL_MACHINE="HKLM", OpenKey=lambda _parent, name: Key(name), EnumKey=enum_key, QueryValueEx=query)
    monkeypatch.setitem(sys.modules, "winreg", fake)


@pytest.mark.parametrize("name", [
    "AMD Radeon(TM) Graphics", "AMD Radeon 780M Graphics", "AMD Radeon(TM) Vega 8 Graphics",
    "AMD Radeon(TM) RX Vega 10 Graphics", "AMD Radeon RX Vega 11 Graphics",
    "Intel(R) UHD Graphics", "Intel(R) Iris(R) Xe Graphics", "Intel(R) Arc(TM) Graphics",
])
def test_windows_doesn_t_count_graphics_built_into_the_processor(monkeypatch, name):
    """A Ryzen laptop's built-in graphics with 4 or 8 GB of the computer's memory set aside: the driver writes that
    share as its memory, and it was taken for a card, so an 8B was said to run slowly from the computer's memory."""
    _windows_cards(monkeypatch, {"0000": (name, 8 * GB)})
    assert model_roles._windows_card_memory() == 0


@pytest.mark.parametrize("name", [
    "AMD Radeon RX 7800 XT", "AMD Radeon PRO W7600", "Intel(R) Arc(TM) A770 Graphics", "Intel(R) Arc(TM) B580 Graphics",
    "NVIDIA GeForce RTX 4060", "Radeon RX Vega", "AMD Radeon RX Vega 64",
])
def test_windows_counts_a_card_with_memory_of_its_own(monkeypatch, name):
    _windows_cards(monkeypatch, {"0000": ("AMD Radeon(TM) Graphics", 4 * GB), "0001": (name, 16 * GB)})
    assert model_roles._windows_card_memory() == 16 * GB


def test_linux_doesn_t_count_graphics_built_into_the_processor(monkeypatch, tmp_path):
    """The AMD driver gives the share set aside for built-in graphics as mem_info_vram_total too, but writes who made
    the memory (mem_info_vram_vendor) only for a card with memory of its own."""
    built_in = tmp_path / "card0" / "device"
    built_in.mkdir(parents=True)
    (built_in / "mem_info_vram_total").write_text(f"{8 * GB}\n")
    monkeypatch.setattr(model_roles, "DRM_CARDS", tmp_path)
    assert model_roles._linux_card_memory() == 0
    card = tmp_path / "card1" / "device"
    card.mkdir(parents=True)
    (card / "mem_info_vram_total").write_text(f"{16 * GB}\n")
    (card / "mem_info_vram_vendor").write_text("samsung\n")
    assert model_roles._linux_card_memory() == 16 * GB


def test_a_model_bigger_than_the_card_loads_into_the_card_and_the_computer_s_memory_together(settings, lm, monkeypatch):
    """On a PC with 16 GB of memory and a 24 GB graphics card, a 26 GB model was judged against the 12 GB the
    computer gives alone: cut to 4,096 tokens, never chosen by itself, and said to run out of memory naming the
    card. LM Studio puts what fits on the card and the rest in the computer's memory, about 36 GB here."""
    from controller_inbox import model_fit

    _computer(monkeypatch, 16, card_gb=24)
    lm.sizes = {BIG: 26 * GB}
    status = check_model(settings, use_cache=False)
    budget = model_roles.model_budget()
    assert budget.runs_well == 24 * GB and budget.loads == 36 * GB
    assert model_fit.auto_context(settings, status, BIG) == 16384
    assert model_roles.fits(settings, status, BIG, budget, at_all=True, shorter=True)
    warning = model_roles.too_big(settings, status)[BIG]
    assert "run from the computer's memory" in warning and "run out of memory" not in warning

    # One too big even for both says what both give.
    lm.sizes = {BIG: 40 * GB}
    warning = model_roles.too_big(settings, check_model(settings, use_cache=False))[BIG]
    assert "run out of memory" in warning
    assert "this PC can give a model about 36 GB: its graphics card's 24 GB and 12 GB of its 16 GB of memory" in warning


def test_the_suggestion_never_says_a_model_needs_what_the_mac_can_give(settings, store, lm, monkeypatch):
    """A 19 GB model loaded with 6,144 tokens on a 32 GB Mac needs 21.3 GB, a little over the 21.3 GB macOS lets it
    use: the line said it "needs about 21 GB" and the Mac "can give a model about 21 GB", and still warned."""
    from controller_inbox import local_llm

    _computer(monkeypatch, 32, mac=True)
    _answering(lm, settings, BIG)
    lm.contexts = {BIG: 6144}
    local_llm._status_cache.clear()
    tip = model_choice.recommendation(settings, store, check_model(settings))
    assert tip and tip["reason"] == "too_big"
    assert "needs about 22 GB of memory with a 6,144-token context" in tip["message"]
    assert "this Mac can give a model about 21 GB" in tip["message"]


def test_the_automatic_choice_takes_an_8b_over_a_14b_under_10_gb(settings, lm, monkeypatch):
    """A 16 GB PC with no graphics card and a 1.5B, an 8B, a 14B and a 31B downloaded: CloseDesk loaded the 14B
    (9.0 GB at Q4, under the 10 GB it took for "an 8B"), though Setup's guide says it loads one of up to about 8B and
    a 14B answers about half as fast from the processor alone. How many parameters a model has goes by LM Studio's
    own figure, or by its name, and by its size only when neither says."""
    _computer(monkeypatch, 16)
    tiny, eight, fourteen = "qwen2.5-1.5b-instruct", "meta-llama-3.1-8b-instruct", "qwen2.5-14b-instruct"
    lm.extra = [tiny, eight, fourteen, BIG]
    lm.sizes = {tiny: int(1.1e9), eight: int(4.9e9), fourteen: int(9.0e9), BIG: int(19e9)}
    status = check_model(settings, use_cache=False)
    assert model_choice.auto_choice(settings, status) == eight
    assert model_choice.billions(status, fourteen) == 14 and model_choice.billions(status, tiny) == 1.5
    assert not model_choice.small(status, fourteen) and model_choice.small(status, eight)

    # A name that doesn't say: LM Studio's own figure, else the size.
    plain = "acme/house-chat"
    lm.extra = [plain, eight]
    lm.sizes = {plain: int(8.5e9), eight: int(4.9e9)}
    status = check_model(settings, use_cache=False)
    assert model_choice.small(status, plain), "no figure: under 10 GB counts as small"
    assert model_choice.auto_choice(settings, status) == plain
    status.chat_params[plain] = "14B"
    assert not model_choice.small(status, plain) and model_choice.auto_choice(settings, status) == eight


def test_lm_studio_s_parameter_count_is_read(settings, lm, monkeypatch):
    from controller_inbox import local_llm

    listing = {"models": [{"type": "llm", "key": "acme/house-chat", "params_string": "14B", "size_bytes": 9 * GB, "loaded_instances": []}]}
    monkeypatch.setattr(local_llm.httpx, "get", lambda url, **kw: httpx_response(url, listing))
    local_llm._status_cache.clear()
    assert check_model(settings, use_cache=False).chat_params == {"acme/house-chat": "14B"}


def httpx_response(url, listing):
    import httpx

    request = httpx.Request("GET", url)
    if url.endswith("/api/v1/models"):
        return httpx.Response(200, json=listing, request=request)
    return httpx.Response(200, json={"data": [{"id": "acme/house-chat"}]}, request=request)


def test_an_8b_closedesk_loads_on_a_6_gb_card_isnt_called_too_big(settings, lm, monkeypatch):
    """A 16 GB PC with a 6 GB graphics card and an 8B downloaded: CloseDesk loaded the 8B itself, then Settings
    marked it "likely too big for this computer", said most of it would run from the computer's memory (about 6 of
    its 8 GB are on the card) and told the user a 3B to 8B model is plenty."""
    _computer(monkeypatch, 16, card_gb=6)
    eight = "meta-llama-3.1-8b-instruct"
    lm.extra = [eight]
    lm.sizes = {eight: int(4.9 * GB)}
    status = check_model(settings, use_cache=False)
    assert model_choice.auto_choice(settings, status) == eight
    assert model_roles.too_big(settings, status) == {}


def test_a_model_mostly_on_the_card_is_said_to_run_partly_from_the_computer_s_memory(settings, lm, monkeypatch):
    _computer(monkeypatch, 32, card_gb=8)
    twelve = "google/gemma-3-12b"
    lm.extra = [twelve]
    lm.sizes = {twelve: int(7.5 * GB)}
    warning = model_roles.too_big(settings, check_model(settings, use_cache=False))[twelve]
    assert "part of it would run from the computer's memory" in warning and "most of it" not in warning, warning


def test_a_small_model_that_doesnt_fit_at_all_isnt_told_a_3b_to_8b_is_plenty(settings, lm, monkeypatch):
    _computer(monkeypatch, 8, mac=True)
    warning = model_roles.too_big(settings, check_model(settings, use_cache=False))[CHAT]
    assert "run out of memory" in warning and "3B to 8B" not in warning and "A smaller one" in warning, warning


def test_a_6_gb_card_keeps_an_8b_rather_than_go_down_to_a_1_5b(settings, store, lm, monkeypatch):
    """A 16 GB PC with a 6 GB card, a 1.5B and an 8B downloaded: the 1.5B was loaded at the start, and once the user
    loaded the 8B, Settings and Today suggested going back to the 1.5B, against the "3B to 8B is plenty" on the same
    page. The 8B needs a little over the card at 4,096 tokens (by the 1 GB set aside to work in), so a little of it
    sits in the computer's memory."""
    _computer(monkeypatch, 16, card_gb=6)
    tiny, eight = "qwen2.5-1.5b-instruct", "meta-llama-3.1-8b-instruct"
    lm.extra = [tiny, eight]
    lm.sizes = {tiny: int(1.1e9), eight: 4_920_734_016}
    status = check_model(settings, use_cache=False)
    assert model_choice.auto_choice(settings, status) == eight
    assert model_roles.too_big(settings, status) == {}
    _answering(lm, settings, eight)
    lm.contexts = {eight: 4096}
    from controller_inbox import local_llm

    local_llm._status_cache.clear()
    assert model_choice.recommendation(settings, store, check_model(settings)) is None


def test_a_7b_that_runs_on_the_card_with_a_shorter_context_is_chosen_over_a_1_5b(settings, lm, monkeypatch):
    _computer(monkeypatch, 16, card_gb=6)
    tiny, seven = "qwen2.5-1.5b-instruct", "qwen2.5-7b-instruct"
    lm.extra = [tiny, seven]
    lm.sizes = {tiny: int(1.1e9), seven: int(4.36 * GB)}
    assert model_choice.auto_choice(settings, check_model(settings, use_cache=False)) == seven


def test_settings_for_a_remembered_model_not_loaded_says_what_closedesk_will_ask_for(settings, store, lm, monkeypatch):
    """Nothing loaded in LM Studio, and the model used last remembered: Settings said the context was "not
    reported", told the user to reload the model at 16,384 tokens (a size the same page said doesn't fit a 31B on
    a 32 GB Mac) and to set a settings variable by name, and listed the model as "Loaded"."""
    import html

    from fastapi.testclient import TestClient

    from controller_inbox import local_llm, model_fit, web

    _computer(monkeypatch, 32, mac=True)
    lm.sizes = {BIG: 19 * GB}
    settings.llm = True
    local_llm.remember_model(settings, BIG)
    status = check_model(settings, use_cache=False)
    assert status.model == BIG and not status.instances
    line = model_fit.context_line(settings, status)
    assert "CONTROLLER_INBOX" not in line["advice"] and "16,384" not in line["advice"], line["advice"]
    assert f"{BIG} isn't loaded in LM Studio right now" in line["advice"]
    assert "CloseDesk asks for 4,096 tokens (the most that fits this computer)" in line["advice"], line["advice"]
    assert model_roles.chat_row(status)["status"] == "Downloaded: LM Studio loads it when you ask something."

    page = html.unescape(TestClient(web.create_app(settings, store)).get("/settings").text)
    assert "CONTROLLER_INBOX" not in page and f"{BIG} isn't loaded in LM Studio right now" in page


def test_a_remembered_model_not_loaded_isnt_said_to_be_ready(settings, lm):
    """With nothing loaded in LM Studio, Settings said "Local model ready: <model>": nothing said it wasn't loaded,
    or that the first question would wait for LM Studio to load it."""
    from controller_inbox import local_llm

    settings.llm = True
    local_llm.remember_model(settings, BIG)
    said = check_model(settings, use_cache=False).describe()
    assert "ready" not in said and f"{BIG}" in said and "isn't loaded now: LM Studio loads it when you ask something" in said, said
    lm.loaded.append(BIG)
    assert check_model(settings, use_cache=False).describe().startswith(f"Local model ready: {BIG} at ")


def test_a_server_that_doesnt_say_its_context_isnt_answered_with_a_settings_variable(settings):
    from controller_inbox import model_fit
    from controller_inbox.local_llm import ModelStatus

    settings.chat_context_tokens = 0
    status = ModelStatus(mode="auto", reachable=True, model="llama3", base_url="http://127.0.0.1:11434/v1")
    advice = model_fit.context_line(settings, status)["advice"]
    assert "CONTROLLER_INBOX" not in advice and "doesn't say" in advice, advice


def test_a_3b_on_an_8_gb_laptop_isnt_called_too_big_by_the_context_line(settings, lm, monkeypatch):
    """An 8 GB laptop with no graphics card and a 3B loaded at 4,096 tokens: the context line said the 3B "is too big
    for this computer's memory with a longer context" and that "A smaller model reads whole workbooks and PDFs",
    though a 3B is the smallest size the guide recommends and the model list beside it doesn't mark it."""
    from controller_inbox import local_llm, model_fit

    _computer(monkeypatch, 8)
    three = "llama-3.2-3b-instruct"
    lm.extra = [three]
    lm.sizes = {three: int(2.05 * GB)}
    _answering(lm, settings, three)
    lm.contexts = {three: 4096}
    local_llm._status_cache.clear()
    status = check_model(settings)
    assert local_llm.context_target(settings, status) == 4096 and three not in model_roles.too_big(settings, status)
    advice = model_fit.context_line(settings, status)["advice"]
    assert "too big" not in advice and "smaller model" not in advice, advice
    assert "so CloseDesk asks LM Studio for 4,096 tokens" in advice

    # A big model is still pointed to a smaller one.
    lm.extra = [three, BIG]
    lm.sizes = {three: int(2.05 * GB), BIG: 19 * GB}
    _computer(monkeypatch, 32, mac=True)
    _answering(lm, settings, BIG)
    lm.contexts = {BIG: 4096}
    local_llm._status_cache.clear()
    advice = model_fit.context_line(settings, check_model(settings))["advice"]
    assert "too big for this computer's memory with a longer context" in advice and "A smaller model" in advice


def test_the_minimum_context_help_doesnt_promise_a_reload_a_big_model_wont_get(settings, store):
    from pathlib import Path

    from fastapi.testclient import TestClient

    from controller_inbox import web

    page = TestClient(web.create_app(settings, store)).get("/settings").text
    script = (Path(web.__file__).parent / "static" / "ui" / "settings.js").read_text(encoding="utf-8")
    for text in (page, script):
        assert "LM Studio models loaded with less are reloaded with this on the next question" not in text
        # Said plainly, the same on both pages.
        assert "for one too big for this much here" not in text
        assert "A model too big for that is loaded with the most this computer fits." in text
