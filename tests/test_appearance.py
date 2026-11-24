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
