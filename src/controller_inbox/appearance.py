"""How the pages look: the colors for color blindness, and the importance written on each email in the workspace's lists
(Settings → Appearance in the workspace, Setup → Colors on the classic pages).

Both are saved for CloseDesk on this computer (in the database, with the other Setup choices), not in one browser, so
the workspace and the classic pages use them alike. The server writes them onto each page's <html> as it sends it, so
a page never paints in the other colors first; the stylesheets do the rest."""

from __future__ import annotations

from markupsafe import Markup

from controller_inbox.store import Store

COLORS = ("standard", "color-blind")
# Their keys in sync_state (store.SETTING_KEYS keeps them when the sample mailbox is loaded).
COLORS_KEY = "colors"
WORDS_KEY = "importance_words"


def current(store: Store) -> dict:
    """{"colors": "standard" or "color-blind", "importance_words": True or False}. A value saved by something else (or
    by a later version) is the standard colors, and no words."""
    colors = store.get_state(COLORS_KEY)
    return {"colors": colors if colors in COLORS else "standard", "importance_words": store.get_state(WORDS_KEY) == "1"}


def save(store: Store, *, colors: str | None = None, importance_words: bool | None = None) -> dict:
    """Save what is given (None leaves that one as it is) and say what is in use now. ValueError for colors that
    aren't one of COLORS."""
    if colors is not None and colors not in COLORS:
        raise ValueError(f"Unknown colors: {colors!r}")
    if colors is not None:
        store.set_state(COLORS_KEY, colors)
    if importance_words is not None:
        store.set_state(WORDS_KEY, "1" if importance_words else "0")
    return current(store)


def html_attrs(look: dict | None) -> Markup:
    """What goes on <html> for this look, after lang="en": ' data-colors="color-blind"' and ' data-importance-words="on"'
    when chosen; nothing otherwise, and nothing for a page rendered without a look."""
    if not look:
        return Markup("")
    attrs = ""
    if look.get("colors") == "color-blind":
        attrs += ' data-colors="color-blind"'
    if look.get("importance_words"):
        attrs += ' data-importance-words="on"'
    return Markup(attrs)
