"""An answer's checks on the chat pages, in a real Chromium. The workspace showed "$12,480 isn't in the emails or files
I read; check it before relying on it." in a green box with a tick, as if it had been checked and found right. A
figure to check now shows with the warning sign, a correction with the tick, and the box is amber when any check is a
warning: in a new answer, and in a conversation saved before checks were marked. Skipped without Playwright's
Chromium."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("playwright")

from controller_inbox import answer_check, chats, web  # noqa: E402
from liveserver import serving  # noqa: E402
from test_chat_time_browser import WIDE, _ask, _scripted  # noqa: E402
from test_workspace import CONTRAST  # noqa: E402

NOT_FOUND = "$12,480 isn't in the emails or files I read; check it before relying on it."
CORRECTED = "Corrected 31,200 to 31,500 (115,500 − 84,000)."
# The icons' drawings, as dom.js has them: the warning sign, and the tick inside the "ok" ring.
ALERT = re.search(r'\balert: "([^"]+)"', (Path(web.__file__).parent / "static" / "ui" / "dom.js").read_text())[1]
TICK = "M8 12l3 3 5-6"

# The box's background and each check's icon and its colour, with the colours of the tokens they should be.
SHOWN = """() => {
  const probe = (value) => {
    const el = document.createElement('div');
    el.style.color = value;
    document.body.append(el);
    const out = getComputedStyle(el).color;
    el.remove();
    return out;
  };
  const box = document.querySelector('.msg.bot .msg-check');
  return {
    warned: box.classList.contains('warn'),
    background: getComputedStyle(box).backgroundColor,
    items: [...box.querySelectorAll('li')].map((li) => ({
      text: li.innerText.trim(),
      icon: li.querySelector('svg path').getAttribute('d'),
      color: getComputedStyle(li.querySelector('svg')).color,
    })),
    tokens: { warn: probe('var(--warn)'), ok: probe('var(--ok)'), warnSoft: probe('var(--warn-soft)'), okSoft: probe('var(--ok-soft)') },
  };
}"""


def _answer(*checks: str):
    return [
        {"type": "sources", "sources": [], "mode": "model"},
        {"type": "delta", "text": "Marketing went up by 31,500. Travel is $12,480."},
        {"type": "check", "items": answer_check.check_items(list(checks))},
    ]


def _shown(page) -> dict:
    page.wait_for_selector(".msg.bot:not(.pending) .msg-check li")
    return page.evaluate(SHOWN)


def test_a_figure_to_check_shows_as_a_warning_and_a_correction_with_a_tick(settings, store, page, monkeypatch):
    _scripted(monkeypatch, _answer(CORRECTED, NOT_FOUND))
    with serving(web.create_app(settings, store)) as base:
        _ask(page, base, "How did Marketing change?")
        shown = _shown(page)
    tokens = shown["tokens"]
    assert shown["warned"] and shown["background"] == tokens["warnSoft"], "a list with a warning in it is amber"
    corrected, warning = shown["items"]
    assert corrected["text"] == CORRECTED and TICK in corrected["icon"] and corrected["color"] == tokens["ok"]
    assert warning["text"] == NOT_FOUND and warning["icon"] == ALERT and warning["color"] == tokens["warn"]


def test_corrections_alone_keep_the_green_box_and_the_tick(settings, store, page, monkeypatch):
    _scripted(monkeypatch, _answer(CORRECTED))
    with serving(web.create_app(settings, store)) as base:
        _ask(page, base, "How did Marketing change?")
        shown = _shown(page)
    assert not shown["warned"] and shown["background"] == shown["tokens"]["okSoft"]
    assert [(item["text"], TICK in item["icon"]) for item in shown["items"]] == [(CORRECTED, True)]


def _saved(store) -> str:
    """A conversation saved before checks were marked: its checks are plain words."""
    chat_id = chats.new_id()
    store.create_chat(chat_id, "Travel")
    store.add_chat_turn(chat_id, "user", "What is travel?")
    store.add_chat_turn(chat_id, "assistant", "Travel is $12,480.", {"mode": "model", "sources": [], "checks": [CORRECTED, NOT_FOUND]})
    return chat_id


def _open_saved(page, base: str, chat_id: str) -> None:
    page.set_viewport_size(WIDE)
    page.goto(f"{base}/app/all")
    page.evaluate("id => localStorage.setItem('closedesk-chat-id', id)", chat_id)
    page.reload()
    page.click("#chat-btn")


@pytest.mark.parametrize("dark", [False, True])
def test_a_conversation_saved_before_shows_its_warning_as_one_readably_in_both_themes(settings, store, page, dark):
    page.emulate_media(color_scheme="dark" if dark else "light")
    chat_id = _saved(store)
    with serving(web.create_app(settings, store)) as base:
        _open_saved(page, base, chat_id)
        shown = _shown(page)
        ratios = {
            selector: page.evaluate(CONTRAST, f".msg.bot .msg-check {selector}")
            for selector in ("li.warn span", "li.warn .ic", "li.ok span", "li.ok .ic")
        }
    assert shown["warned"] and shown["background"] == shown["tokens"]["warnSoft"]
    assert [(item["text"], item["icon"] == ALERT) for item in shown["items"]] == [(CORRECTED, False), (NOT_FOUND, True)]
    # Words at 4.5:1 and the icons at 3:1 against the box, at least.
    assert ratios["li.warn span"] >= 4.5 and ratios["li.ok span"] >= 4.5, ratios
    assert ratios["li.warn .ic"] >= 3 and ratios["li.ok .ic"] >= 3, ratios


def test_the_classic_chat_shows_a_saved_conversations_checks_in_words(settings, store, page):
    chat_id = _saved(store)
    with serving(web.create_app(settings, store)) as base:
        page.goto(f"{base}/chat/window")
        page.evaluate("id => localStorage.setItem('closedesk-chat-id', id)", chat_id)
        page.reload()
        page.wait_for_selector("#chat .msg-check li")
        items = page.eval_on_selector_all("#chat .msg-check li", "els => els.map((el) => el.innerText.trim())")
    assert items == [CORRECTED, NOT_FOUND]
