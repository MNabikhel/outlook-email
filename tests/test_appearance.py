"""The colors for color blindness and the importance on each email (Settings → Appearance, Setup → Colors): saved for
CloseDesk on this computer, and written onto <html> by the server, so the workspace and the classic pages both paint
in them from the start, with no script and no flash."""

from __future__ import annotations

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


def test_a_processed_run_that_needs_a_look_says_so_in_words(client):
    """The classic job box told a run that needs a look from one that went well by its red or green edge alone."""
    import re
    import threading

    said = "Stopped reading: the local model ran out of memory on three messages in a row. Choose one in Settings."
    for about, result in (
        (None, {"ingested": 1, "model": "gemma", "read_by_bionic": 0, "model_note": said}),
        ({"kind": "outlook"}, {"kind": "outlook", "message": "Outlook synced: 2 new.", "model": "gemma", "model_note": said}),
    ):
        job = client.app.state.job
        assert job.start(lambda progress, result=result: result, about=about)
        for _ in range(250):
            if job.snapshot()["state"] != "running":
                break
            threading.Event().wait(0.02)
        section = re.search(r'<section class="job error">(.*?)</section>', client.get("/").text, re.S)[1]
        words = " ".join(re.sub(r"<[^>]+>", " ", section).split())
        assert "Needs a look:" in words and said in words, words
        assert '<b class="job-flag">Needs a look:</b>' in section
    job.start(lambda progress: {"ingested": 1, "model": "", "read_by_bionic": 0})
    for _ in range(250):
        if job.snapshot()["state"] != "running":
            break
        threading.Event().wait(0.02)
    assert "Needs a look" not in client.get("/").text, "a run that went well says nothing of the kind"


def test_the_help_says_what_the_colors_for_color_blindness_change(client):
    """The help said the box "swaps the red, orange and green" and that "High and warnings turn yellow". There is no
    orange in either palette, and High and warnings are the same yellow amber in both, so someone ticking the box to
    see High change saw nothing happen. What changes is a deeper red and a blue-green done."""
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
        assert "deeper red" in text and "blue-green" in text, where
        assert "orange" not in text and "turn yellow" not in text, where
    # And so they are: in the light theme, Critical and fraud are darker and done turns from green to blue-green.
    for standard, blind, ok in ((pm.workspace("light"), pm.workspace("light", "color-blind"), "--ok"), (pm.classic(), pm.classic("color-blind"), "--green")):
        for name in ("--imp-critical", "--danger"):
            assert pm.oklch(blind[name])[0] < pm.oklch(standard[name])[0] - 5, name
        assert pm.oklch(standard[ok])[2] < 170 < pm.oklch(blind[ok])[2] < 230


def test_the_importance_words_say_only_the_workspace_changed(client, demo):
    """Ticking only "Show Critical, High, Medium or Low on each email" said "The workspace and the classic pages now
    look this way", but only the workspace's lists change: the classic pages show the importance in a column anyway."""
    words = web.NOTICES["importance-words"]
    assert "workspace" in words and "classic" not in words
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
    """The printable digest's own stylesheet, as a file palette_math can read."""
    css = tmp_path / "digest.css"
    css.write_text(html[html.index("<style>") + len("<style>") : html.index("</style>")], encoding="utf-8")
    return css


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
