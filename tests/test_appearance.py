"""The colors for color blindness and the importance on each email (Settings → Appearance, Setup → Colors): saved for
CloseDesk on this computer, and written onto <html> by the server, so the workspace and the classic pages both paint
in them from the start, with no script and no flash."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from controller_inbox import appearance, web
from controller_inbox.pipeline import ingest_demo
from helpers import PAGE

PAGES = ("/app", "/app/settings", "/", "/settings", "/inbox", "/chat/window")
SAME = {"origin": "http://testserver"}


@pytest.fixture
def demo(store, settings, as_of_now):
    ingest_demo(store, settings, now=as_of_now)
    return store


@pytest.fixture
def client(demo, settings):
    return TestClient(web.create_app(settings, demo))


def colors(client, body: dict):
    return client.post("/api/settings/colors", headers=PAGE, json=body)


def html_tag(client, path: str) -> str:
    text = client.get(path).text
    return text[text.index("<html") : text.index(">", text.index("<html")) + 1]


def test_both_are_off_until_chosen(client):
    assert client.get("/api/settings", headers=PAGE).json()["appearance"] == {"colors": "standard", "importance_words": False}
    assert client.get("/api/state", headers=PAGE).json()["appearance"] == {"colors": "standard", "importance_words": False}
    assert {path: html_tag(client, path) for path in PAGES} == {path: '<html lang="en">' for path in PAGES}


def test_choosing_them_writes_them_on_every_page(client, demo):
    reply = colors(client, {"colors": "color-blind"})
    assert reply.status_code == 200, reply.text
    assert reply.json()["message"] == web.NOTICES["colors"]
    assert reply.json()["appearance"] == {"colors": "color-blind", "importance_words": False}
    assert {path: html_tag(client, path) for path in PAGES} == {path: '<html lang="en" data-colors="color-blind">' for path in PAGES}

    # Each is saved on its own: one leaves the other as it was.
    assert colors(client, {"importance_words": True}).json()["appearance"] == {"colors": "color-blind", "importance_words": True}
    both = '<html lang="en" data-colors="color-blind" data-importance-words="on">'
    assert {path: html_tag(client, path) for path in PAGES} == {path: both for path in PAGES}
    assert colors(client, {"colors": "standard"}).json()["appearance"] == {"colors": "standard", "importance_words": True}
    assert html_tag(client, "/app") == '<html lang="en" data-importance-words="on">'
    assert colors(client, {"importance_words": False}).json()["appearance"] == {"colors": "standard", "importance_words": False}
    assert html_tag(client, "/") == '<html lang="en">'
    assert client.get("/api/state", headers=PAGE).json()["appearance"] == appearance.current(demo)


def test_they_are_kept_for_the_next_start(client, demo, settings):
    colors(client, {"colors": "color-blind", "importance_words": "true"})
    again = TestClient(web.create_app(settings, demo))
    assert html_tag(again, "/app") == '<html lang="en" data-colors="color-blind" data-importance-words="on">'
    assert again.get("/api/settings", headers=PAGE).json()["appearance"] == {"colors": "color-blind", "importance_words": True}


@pytest.mark.parametrize("body", [{"colors": "purple"}, {}, {"colors": ""}, {"colors": None, "importance_words": None}])
def test_anything_else_is_refused_and_changes_nothing(client, demo, body):
    colors(client, {"importance_words": True})
    reply = colors(client, body)
    assert reply.status_code == 400
    assert reply.json()["detail"] == "Choose the standard colors or the colors for color blindness."
    assert appearance.current(demo) == {"colors": "standard", "importance_words": True}


def test_other_websites_cannot_change_them(client, demo):
    for headers in ({**PAGE, "Origin": "https://evil.example"}, {}):
        assert client.post("/api/settings/colors", json={"colors": "color-blind"}, headers=headers).status_code == 403
    assert client.post("/settings/colors", data={"colors": "color-blind"}, headers={"origin": "https://evil.example"}).status_code == 403
    assert appearance.current(demo) == {"colors": "standard", "importance_words": False}


def test_an_unknown_saved_value_is_the_standard_colors(store):
    store.set_state("colors", "sepia")
    store.set_state("importance_words", "maybe")
    assert appearance.current(store) == {"colors": "standard", "importance_words": False}
    with pytest.raises(ValueError):
        appearance.save(store, colors="sepia")
    assert appearance.save(store, colors="color-blind") == {"colors": "color-blind", "importance_words": False}
    assert appearance.save(store, importance_words=True) == {"colors": "color-blind", "importance_words": True}


def test_html_attrs_is_empty_without_a_look():
    assert appearance.html_attrs(None) == ""
    assert appearance.html_attrs({"colors": "standard", "importance_words": False}) == ""
    both = appearance.html_attrs({"colors": "color-blind", "importance_words": True})
    assert both == ' data-colors="color-blind" data-importance-words="on"'


def test_todays_rows_carry_the_importance_in_words(client, demo):
    """The workspace writes the importance on each row of Today as well as the mail lists, so /api/focus says it."""
    from controller_inbox.models import IMPORTANCE_LABELS

    rows = client.get("/api/focus", headers=PAGE).json()["focus"]
    assert rows
    for row in rows:
        email = demo.get_email(row["email_id"])
        assert row["importance"] == email.importance.value
        assert row["importance_label"] == IMPORTANCE_LABELS[email.importance], row
    assert {row["importance_label"] for row in rows} >= {"Critical", "High"}


def test_the_classic_setup_page_saves_both(client, demo):
    """Setup → Colors on the classic pages: a form that works without JavaScript. A box left unticked sends nothing,
    which is off."""
    page = client.get("/settings").text
    assert 'id="colors"' in page and "Colors for color blindness" in page and "Show Critical, High, Medium or Low on each email" in page
    assert 'name="colors" value="color-blind" checked' not in page

    saved = client.post("/settings/colors", data={"colors": "color-blind", "importance_words": "1"}, headers=SAME, follow_redirects=False)
    assert saved.status_code == 303 and saved.headers["location"] == "/settings?notice=colors#colors"
    assert appearance.current(demo) == {"colors": "color-blind", "importance_words": True}
    page = client.get("/settings?notice=colors").text
    assert web.NOTICES["colors"] in page
    assert 'name="colors" value="color-blind" checked' in page and 'name="importance_words" value="1" checked' in page
    assert page.startswith('<!DOCTYPE html>\n<html lang="en" data-colors="color-blind" data-importance-words="on">')

    client.post("/settings/colors", data={}, headers=SAME, follow_redirects=False)
    assert appearance.current(demo) == {"colors": "standard", "importance_words": False}
    page = client.get("/settings").text
    assert 'value="color-blind" checked' not in page and 'value="1" checked' not in page


def test_a_setup_page_left_open_saves_only_the_box_changed_on_it(client, demo):
    """Setup → Colors sends both boxes, and an unticked box sends nothing, which is off. A Setup page drawn before the
    colors were changed in the workspace still showed the old boxes, so ticking the other box and saving quietly
    undid the workspace's change, with "Saved" said. The form says what each box showed when it was drawn, and only
    a box changed on it is saved."""
    import re

    page = client.get("/settings").text
    drawn = dict(re.findall(r'<input type="hidden" name="(\w+_was)" value="([^"]*)"', page))
    assert drawn == {"colors_was": "standard", "importance_words_was": "0"}

    def save(**fields):
        reply = client.post("/settings/colors", data={**drawn, **fields}, headers=SAME, follow_redirects=False)
        assert reply.status_code == 303
        return reply.headers["location"]

    # The workspace turns on the colors for color blindness; the page left open ticks only the words box.
    appearance.save(demo, colors="color-blind")
    assert save(importance_words="1") == "/settings?notice=importance-words#colors"
    assert appearance.current(demo) == {"colors": "color-blind", "importance_words": True}
    # The other way round: the workspace turns the words off again; the page ticks the colors box (already on).
    appearance.save(demo, importance_words=False)
    assert save(colors="color-blind") == "/settings?notice=colors#colors"
    assert appearance.current(demo) == {"colors": "color-blind", "importance_words": False}
    # A box changed on the page is saved whatever was saved since: here unticking what it drew unticked.
    drawn = {"colors_was": "color-blind", "importance_words_was": "1"}
    assert save(colors="", importance_words="1") == "/settings?notice=colors#colors"
    assert appearance.current(demo) == {"colors": "standard", "importance_words": False}
    # A form from before this (no _was fields) saves both, as it always did.
    drawn = {}
    save(colors="color-blind", importance_words="1")
    assert appearance.current(demo) == {"colors": "color-blind", "importance_words": True}
    page = client.get("/settings").text
    assert dict(re.findall(r'<input type="hidden" name="(\w+_was)" value="([^"]*)"', page)) == {"colors_was": "color-blind", "importance_words_was": "1"}


def _finished(client, result, about=None) -> str:
    """The classic Today page's job box once a run that gave ``result`` has finished, as its words."""
    import re
    import threading

    job = client.app.state.job
    assert job.start(lambda progress: result, about=about)
    for _ in range(250):
        if job.snapshot()["state"] != "running":
            break
        threading.Event().wait(0.02)
    section = re.search(r'<section class="job \w+">(.*?)</section>', client.get("/").text, re.S)[1]
    return section


def test_a_processed_run_that_needs_a_look_says_so_in_words(client):
    """The classic job box told a run that needs a look from one that went well by its red or green edge alone. The
    words go just before what needs a look: they were first, in front of "Read 4 new files", which was fine."""
    import html
    import re

    said = "Stopped reading: the local model ran out of memory on three messages in a row. Choose one in Settings."
    loaded = {"message": "Loaded gemma in LM Studio.", "failed": False}
    failed = {"message": "LM Studio couldn't load gemma: not enough memory.", "failed": True}
    for about, result, before, problem in (
        (None, {"ingested": 4, "model": "gemma", "read_by_bionic": 0, "model_note": said, "model_load": loaded},
         "Processed. Read 4 new files. The local model read 0. Loaded gemma in LM Studio.", said),
        (None, {"ingested": 4, "model": "", "read_by_bionic": 0, "model_load": failed}, "Processed. Read 4 new files.", failed["message"]),
        ({"kind": "outlook"}, {"kind": "outlook", "message": "Outlook synced: 2 new.", "model": "gemma", "model_note": said, "model_load": loaded},
         "Outlook synced: 2 new. Loaded gemma in LM Studio.", said),
        ({"kind": "outlook"}, {"kind": "outlook", "message": "Outlook couldn't be read this time.", "outlook": {"note": "Outlook couldn't be read this time."}},
         "", "Outlook couldn't be read this time."),
    ):
        section = _finished(client, result, about)
        words = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", section)).split())
        assert words == f"{before} Needs a look: {problem}".strip(), words
        assert section.count('<b class="job-flag">Needs a look:</b>') == 1
    section = _finished(client, {"ingested": 1, "model": "gemma", "read_by_bionic": 1, "model_load": loaded})
    assert "Needs a look" not in section, "a run that went well says nothing of the kind"
    assert "Needs a look" not in client.get("/").text


def test_the_help_says_what_the_colors_for_color_blindness_change(client):
    """The help said the box "swaps the red, orange and green" and that "High and warnings turn yellow": there is no
    orange in either palette. Then it said Critical and fraud "turn a deeper red" and High and warnings "stay yellow":
    in the dark theme the red turns pinker, not deeper, and High is an amber everywhere else. What changes in every
    theme is a red that leans to crimson and a blue-green done; High and warnings stay amber."""
    from pathlib import Path

    import palette_math as pm

    source = Path(web.__file__).parent
    page = client.get("/settings").text
    helps = {
        "classic": page[page.index('id="colors"') : page.index("</section>", page.index('id="colors"'))],
        "workspace": (source / "static" / "ui" / "settings.js").read_text(encoding="utf-8"),
        "README": (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8"),
    }
    for where, text in helps.items():
        text = text[text.index("Colors for color blindness") :][:400]
        text = text[: text.index("Critical, High, Medium or Low")]
        assert "crimson" in text and "blue-green" in text and "amber" in text, where
        assert not [word for word in ("orange", "yellow", "deeper") if word in text], where

    def hue(value):
        found = pm.oklch(value)[2]
        return found - 360 if found > 180 else found

    # And so they are, in each theme: a true red turns crimson (towards blue), green turns blue-green, and High and
    # warnings keep an amber's hue.
    for ui, theme in (("workspace", "light"), ("workspace", "dark"), ("classic", "light")):
        standard, blind = pm.tokens_of(ui, "standard", theme), pm.tokens_of(ui, "color-blind", theme)
        meanings = pm.MEANINGS[ui]
        for name in ("critical", "danger"):
            token = meanings[name]
            assert hue(blind[token]) <= 15 and hue(standard[token]) - hue(blind[token]) >= 8, (ui, theme, name)
        assert pm.oklch(standard[meanings["ok"]])[2] < 170 < pm.oklch(blind[meanings["ok"]])[2] < 230, (ui, theme)
        for name in ("high", "high-fill", "warn"):
            assert 70 <= hue(blind[meanings[name]]) <= 100, (ui, theme, name)


def test_the_words_help_says_which_emails_have_a_colored_edge():
    """The words box said it writes the importance "as well as the colored edge", but only Critical and High have
    one: a Medium or Low email has no edge at all."""
    from pathlib import Path

    import palette_math as pm

    source = (Path(web.__file__).parent / "static" / "ui" / "settings.js").read_text(encoding="utf-8")
    text = source[source.index('b("Show Critical, High, Medium or Low on each email")') :][:300]
    assert "as well as the colored edge" not in text
    assert "only Critical and High" in text and "colored edge" in text, text
    for level in ("critical", "high"):
        assert pm.rule_value(pm.WORKSPACE_CSS, f".item.imp-{level}::before", "background") == f"var(--imp-{level})"
    css = pm.WORKSPACE_CSS.read_text(encoding="utf-8")
    assert ".item.imp-medium" not in css and ".item.imp-low" not in css


def test_the_importance_words_say_which_lists_changed(client, demo):
    """Ticking only "Show Critical, High, Medium or Low on each email" said "The workspace and the classic pages now
    look this way", but the classic Inbox and folder lists show the importance in a column either way: what changes
    is the workspace's lists, and Today and the digest on the classic pages."""
    words = web.NOTICES["importance-words"]
    assert "workspace's lists" in words and "Today and the digest" in words
    reply = colors(client, {"importance_words": True})
    assert reply.json()["message"] == words and reply.json()["notice"] == "importance-words"
    assert colors(client, {"importance_words": False}).json()["message"] == words
    assert colors(client, {"colors": "color-blind"}).json()["message"] == web.NOTICES["colors"]
    # Both at once (Ctrl K and the classic form can): the colors' message, which covers both.
    assert colors(client, {"colors": "standard", "importance_words": True}).json()["message"] == web.NOTICES["colors"]

    # The classic form sends both boxes every time: its message is for what changed.
    def classic(**fields):
        saved = client.post("/settings/colors", data=fields, headers=SAME, follow_redirects=False)
        assert saved.status_code == 303
        return saved.headers["location"]

    assert classic(importance_words="1") == "/settings?notice=colors#colors"  # nothing changed: as it was
    assert classic() == "/settings?notice=importance-words#colors"
    assert classic(importance_words="1") == "/settings?notice=importance-words#colors"
    assert classic(colors="color-blind", importance_words="1") == "/settings?notice=colors#colors"
    assert classic(colors="color-blind") == "/settings?notice=importance-words#colors"
    assert classic(importance_words="1") == "/settings?notice=colors#colors"
    from markupsafe import escape

    assert str(escape(words)) in client.get("/settings?notice=importance-words").text


def test_both_setup_pages_say_where_the_importance_words_go(client):
    """Classic Setup said the box was for "the workspace's lists. These pages already show it in the Importance
    column", but the classic home page, Today, and the digest have no such column; and the workspace said the classic
    pages use the box too, which they didn't. Now Today and the digest do, and both pages say so."""
    from pathlib import Path

    page = client.get("/settings").text
    section = page[page.index('id="colors"') : page.index("</section>", page.index('id="colors"'))]
    words = section[section.index("Show Critical, High, Medium or Low on each email") :]
    assert "already" not in words and "Today and the digest" in words and "Inbox and folder lists" in words, words
    source = (Path(web.__file__).parent / "static" / "ui" / "settings.js").read_text(encoding="utf-8")
    assert "The two boxes under Colors are saved for CloseDesk on this computer, and the classic pages follow them too." in source


def _focus_items(html: str) -> list[str]:
    """Each focus item on a classic page, as written."""
    items = html.split('<li class="focus-item ')[1:]
    return [item[: item.index("</li>")] for item in items]


def test_classic_today_and_the_digest_write_the_importance_on_each_email(client, demo):
    """Today, the classic home page, and the digest had no importance on their emails, ticked or not: an accountant
    who ticked the box and works from Today never saw it. Each email there now carries it as the Inbox's Importance
    column does (the pill), shown when the box is ticked."""
    from controller_inbox.models import IMPORTANCE_LABELS

    def levels(html: str) -> list[str]:
        return [item[item.index('<span class="pill ') :].split(">", 1)[1].split("<", 1)[0] for item in _focus_items(html) if '<span class="pill ' in item]

    today = client.get("/").text
    rows = client.get("/api/focus", headers=PAGE).json()["focus"]
    assert len(_focus_items(today)) == len(levels(today)) >= 3, "every email in the focus list"
    want = ["Held for fraud" if row["locked"] else IMPORTANCE_LABELS[demo.get_email(row["email_id"]).importance] for row in rows]
    assert levels(today)[: len(want)] == want
    assert 'class="pill critical item-level"' in today and 'class="pill high item-level"' in today
    # What came in since: each email in the three folder cards.
    cards = today[today.index('class="panel folder-card') : today.index("</section>", today.index('class="panel folder-card'))]
    assert cards.count("item-level") == cards.count('<a href="/inbox/'), "each email in the cards"
    assert client.post("/digest/rebuild", headers=SAME, follow_redirects=False).status_code == 303
    digest = client.get("/digest").text
    assert len(_focus_items(digest)) == len(levels(digest)) >= 3
    assert digest.count("item-level") > len(levels(digest)), "and its other lists of emails"


HELD = "demo-bec-wire"  # the sample's wire change, held as possible payment fraud


def _words_on(html: str, email_id: str) -> list[tuple[str, str]]:
    """(class, word) of the importance written on each row of a classic page that links to this email."""
    out = []
    for item in re.split(r"<li[\s>]", html)[1:]:
        item = item[: item.index("</li>")]
        if f'href="/inbox/{email_id}"' in item and "item-level" in item:
            start = item.rindex('class="', 0, item.index("item-level"))
            out.append((item[start + 7 : item.index('"', start + 7)], item[item.index("item-level") :].split(">", 1)[1].split("<", 1)[0]))
    return out


def test_a_held_email_says_held_for_fraud_wherever_its_importance_is_written(client, demo):
    """With the importance on each email, the email held as possible payment fraud said "Held for fraud" in the
    workspace's lists, but "Critical" in the workspace's digest and on classic Today and the digest: one email, two
    words, by page. The digest's rows now say it is held, and every page writes the held word first."""
    from controller_inbox import fraud

    assert fraud.attachments_locked(demo.get_email(HELD))
    assert client.post("/digest/rebuild", headers=SAME, follow_redirects=False).status_code == 303
    payload = client.get("/api/digest", headers=PAGE).json()["payload"]
    rows = [*payload["focus"], *payload["critical_alerts"], *(row for rows in payload["new_mail"].values() for row in rows)]
    held = [row for row in rows if (row.get("email_id") or row.get("id")) == HELD]
    assert len(held) == 3 and all(row["locked"] is True for row in held), held
    assert not [row for row in rows if row["locked"] and row not in held]
    # Today's box of emails to verify by phone, in the workspace, from /api/focus.
    assert [alert["locked"] for alert in client.get("/api/focus", headers=PAGE).json()["alerts"]] == [True]
    for path in ("/", "/digest"):
        html = client.get(path).text
        # Its box to verify by phone, its row in the focus list, and in Needs you: each says held, not its importance.
        assert _words_on(html, HELD) == [("pill held item-level", "Held for fraud")] * 3, path
        assert html.count(">Held for fraud<") == 3, path


@pytest.mark.parametrize(("notice", "section"), [("colors", "colors"), ("importance-words", "colors"), ("timezone", "timezone")])
def test_setup_says_saved_in_the_section_it_goes_back_to(client, notice, section):
    """Saving Colors (or the time zone) goes back to that section of Setup, at the top of the window, but "Saved." was
    written at the top of the page, out of sight above it (468px above at 1280 by 800, 892px on a phone)."""
    from markupsafe import escape

    page = client.get(f"/settings?notice={notice}").text
    said = f'<p class="notice">{escape(web.NOTICES[notice])}</p>'
    assert page.count(said) == 1, "said once"
    start = page.index(f'<section class="panel" id="{section}">')
    assert start < page.index(said) < page.index("</section>", start), "in the section"
    # Anything else is still said at the top.
    other = client.get("/settings?notice=profile").text
    assert other.index(f'<p class="notice">{escape(web.NOTICES["profile"])}</p>') < other.index('<section class="panel profile-card">')


def test_classic_pages_are_never_kept_and_say_the_colors_in_use(client, demo):
    """Back after Setup → Colors showed the page before in the old colors, from the copy the browser kept: the
    classic pages now say not to keep them, as the workspace's does. A page kept anyway (the back/forward cache, a
    chat window left open) asks for the colors in use when it is shown again."""
    for path in PAGES:
        assert client.get(path).headers.get("cache-control") == "no-store", path
    assert client.get("/api/settings/colors", headers=PAGE).json() == {"appearance": {"colors": "standard", "importance_words": False}}
    appearance.save(demo, colors="color-blind", importance_words=True)
    assert client.get("/api/settings/colors", headers=PAGE).json() == {"appearance": {"colors": "color-blind", "importance_words": True}}
    assert client.get("/api/settings/colors").status_code == 403, "for CloseDesk's own pages"


def _digest_css(html: str, tmp_path):
    """The printable digest's stylesheets, in their order, as one file palette_math can read (the last rule wins)."""
    import re

    css = tmp_path / "digest.css"
    css.write_text("\n".join(re.findall(r"<style>(.*?)</style>", html, re.S)), encoding="utf-8")
    return css


# The colours a digest saved before this version wrote into its page.
EARLIER_COLORS = """
    body { font-family: Georgia, serif; background:#f4f1ea; color:#1b241b; margin:0; }
    .tag { display:inline-block; padding:1px 8px; border-radius: 10px; background:#e7efe5; color:#1f5130; }
    .tag.fraud, .tag.overdue { background:#9b2335; color:#fff; }
    .tag.due_today { background:#b8651b; color:#fff; }
    .alert { background:#9b2335; color:#fff; padding:4px 14px 10px; margin-top: 16px; }
"""


@pytest.mark.parametrize("payload", ["as saved", "in a shape this version can't draw"])
def test_a_digest_saved_before_this_version_shows_in_the_colors_now(client, demo, settings, as_of_now, tmp_path, payload):
    """A digest saved by an earlier version kept the old crimson, and white words on the orange "Due today" at
    4.27:1, in its own stylesheet, whatever colors were chosen: only digests written since followed them. Shown
    again, a saved digest is drawn again from what it saved; one this version can't draw again keeps its page as
    saved, with the colors in use now after its own."""
    import json
    import re
    from datetime import date

    import palette_math as pm
    from controller_inbox.digest import build_digest

    period = "2026-09-22"
    build_digest(demo, as_of=date(2026, 9, 22), generated_at=as_of_now, tz=settings.tz)
    row = demo.get_digest(period)
    earlier = re.sub(r"<style>.*?</style>", f"<style>{EARLIER_COLORS}</style>", row["html"], count=1, flags=re.S)
    earlier = earlier.replace("<h1>", '<h1 data-saved="earlier">', 1)
    kept = json.loads(row["payload"]) if payload == "as saved" else {"date": period}
    demo.save_digest(period, row["generated_at"], row["markdown"], earlier, kept)

    standard, blind = pm.classic(), pm.classic("color-blind")
    for colors in ("standard", "color-blind"):
        appearance.save(demo, colors=colors)
        shown = client.get(f"/digest/{period}.html").text
        assert shown.startswith('<!DOCTYPE html>\n<html lang="en"' + (' data-colors="color-blind">' if colors == "color-blind" else ">"))
        css = _digest_css(shown, tmp_path)
        for selector in (".tag.overdue", ".tag.fraud", ".alert"):
            assert pm.rule_colour(css, selector, "background", {}) == standard["--danger"], (colors, selector)
            assert pm.rule_colour(css, f'html[data-colors="color-blind"] {selector}', "background", {}) == blind["--danger"]
        assert pm.rule_colour(css, ".tag.due_today", "background", {}) == standard["--imp-high-fill"]
        assert pm.rule_colour(css, ".tag.due_today", "color", {}) == standard["--on-imp-high"]
        if payload == "as saved":
            assert "#b8651b" not in shown and "#9b2335" not in shown, "drawn again"
            assert 'data-saved="earlier"' not in shown and "Do not process" in shown
        else:
            assert 'data-saved="earlier"' in shown, "the page as it was saved"
    assert '<html lang="en">' in demo.get_digest(period)["html"], "what was saved is left as it was"


def test_the_printable_digest_uses_the_true_red_and_follows_the_colors(client, demo, settings, as_of_now, tmp_path):
    """The printable digest kept the old crimson and orange, with white words on the orange "Due today" at 4.27:1, and
    ignored the colors for color blindness, so it and the Today page disagreed. It uses the classic pages' colors
    now, and the page CloseDesk shows follows the choice; the file saved for printing and email keeps the standard
    ones (it has words on every tag)."""
    from datetime import date

    import palette_math as pm
    from controller_inbox.digest import build_digest, write_digest_files

    period = "2026-09-22"
    payload = build_digest(demo, as_of=date(2026, 9, 22), generated_at=as_of_now, tz=settings.tz)
    write_digest_files(payload, settings.digest_dir, period)
    standard, blind = pm.classic(), pm.classic("color-blind")
    shown = client.get(f"/digest/{period}.html")
    assert shown.headers["cache-control"] == "no-store"
    assert '<html lang="en">' in shown.text
    css = _digest_css(shown.text, tmp_path)
    red = pm.rule_colour(css, ".tag.overdue", "background", {})
    assert red == pm.rule_colour(css, ".tag.fraud", "background", {}) == pm.rule_colour(css, ".alert", "background", {}) == standard["--danger"]
    assert pm.rule_colour(css, ".tag.due_today", "background", {}) == standard["--imp-high-fill"]
    assert pm.rule_colour(css, ".tag.due_today", "color", {}) == standard["--on-imp-high"]
    assert pm.contrast(standard["--on-imp-high"], standard["--imp-high-fill"]) >= 4.5
    for selector in ('html[data-colors="color-blind"] .tag.overdue', 'html[data-colors="color-blind"] .tag.fraud', 'html[data-colors="color-blind"] .alert'):
        assert pm.rule_colour(css, selector, "background", {}) == blind["--danger"], selector
    assert "#9b2335" not in shown.text and "#b8651b" not in shown.text

    appearance.save(demo, colors="color-blind")
    assert '<html lang="en" data-colors="color-blind">' in client.get(f"/digest/{period}.html").text
    saved = (settings.digest_dir / f"{period}.html").read_text(encoding="utf-8")
    assert '<html lang="en">' in saved, "the file for printing and email is as it was"
