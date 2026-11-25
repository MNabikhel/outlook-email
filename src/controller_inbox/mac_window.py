"""The CloseDesk window on macOS: what pywebview 6.2's Cocoa window takes away from WebKit, put back (Windows' WebView2
has its own, app_window.browser_keys).

- **The right-click menu.** pywebview empties WebKit's (its WKWebView's willOpenMenu:withEvent:) unless it runs for
  debugging, which would add Inspect Element too. Kept as WebKit makes it: Copy, Paste, Look Up, spelling suggestions.
- **The menu bar's items, with their keys.** pywebview's has Cut, Copy, Paste, Select All and Enter Full Screen, and
  answers a few Cmd keys itself. Added, as in Safari: Undo and Redo (Cmd+Z, Cmd+Shift+Z); Find, Find Next and Find
  Previous (Cmd+F, Cmd+G, Cmd+Shift+G); Print (Cmd+P); Reload (Cmd+R); Actual Size, Zoom In and Zoom Out (Cmd+0,
  Cmd+Plus, Cmd+Minus); Back and Forward (Cmd+[, Cmd+]). And zooming with two fingers on the trackpad.
- **A find bar.** WebKit finds words on a page for a find bar but makes none, and doesn't answer Find itself: each
  window gets AppKit's (an NSTextFinder), shown above the page.
- **A download saved over a file that is there** (Replace, in the save panel): WebKit saves a download only to a file
  that isn't there, and pywebview says nothing when it doesn't, so yesterday's CSV stayed. The file there goes to the
  Trash first (it can be put back from there), and a download that fails says so.

``prepare`` before webview.start; ``menus_on`` once the window shows. Each part that can't be done is said in the
window's log, and the window works without it. The macOS parts (PyObjC's AppKit, Foundation and objc, pywebview's
Cocoa window) are given to them, so that the tests can give stand-ins."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

# What CI's check of the real window looks for in its log, once it has read the menu bar and the zoom back.
KEYS_ON = "The window's right-click menu, its menu bar's keys, its find bar and zooming are on."

# Safari's steps of zoom.
ZOOMS = (0.5, 0.67, 0.75, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0)

# AppKit's numbers: the modifier keys (NSEventModifierFlag), and what Find asks of the find bar (NSTextFinderAction).
COMMAND = 1 << 20
SHIFT = 1 << 17
SHOW_FIND, FIND_NEXT, FIND_PREVIOUS, HIDE_FIND = 1, 2, 3, 11
# How a view keeps to its window's size (NSAutoresizingMaskOptions): as wide as it, as tall, or kept to its top.
WIDTH_SIZABLE, HEIGHT_SIZABLE, MIN_Y_MARGIN = 2, 16, 8
# NSURLErrorCancelled: the download was cancelled (the save panel's Cancel, or CloseDesk itself after saying why).
CANCELLED = -999

CANT_SAVE = "CloseDesk couldn't save {name}"
NOT_REPLACED = "The file already there couldn't be moved to the Trash ({why}), so nothing was saved. Save it under another name, or move that file away first."
NOT_SAVED = "Nothing was saved ({why}). Try Download again."
A_FOLDER = "it is a folder"

# (menu, [(title, action, key, modifiers, tag, who answers)]): "page" is the page's own view (WebKit's WKWebView),
# "window" the window (its Undo and Redo), "closedesk" CloseDesk's own (``Answers``: Print, the zoom, and Find, which
# it hands to the window's find bar). Added to pywebview's Edit and View menus, and a File menu of its own; at the top
# of each, before what pywebview has there.
MENUS = (
    ("File", [("Print…", "printPage:", "p", COMMAND, 0, "closedesk")]),
    ("Edit", [
        ("Undo", "undo:", "z", COMMAND, 0, "window"),
        ("Redo", "redo:", "z", COMMAND | SHIFT, 0, "window"),
        None,
    ]),
    ("Edit", [
        None,
        ("Find…", "performTextFinderAction:", "f", COMMAND, SHOW_FIND, "closedesk"),
        ("Find Next", "performTextFinderAction:", "g", COMMAND, FIND_NEXT, "closedesk"),
        ("Find Previous", "performTextFinderAction:", "g", COMMAND | SHIFT, FIND_PREVIOUS, "closedesk"),
    ]),
    ("View", [
        ("Reload", "reload:", "r", COMMAND, 0, "page"),
        None,
        ("Actual Size", "actualSize:", "0", COMMAND, 0, "closedesk"),
        ("Zoom In", "zoomIn:", "+", COMMAND, 0, "closedesk"),
        ("Zoom Out", "zoomOut:", "-", COMMAND, 0, "closedesk"),
        None,
        ("Back", "goBack:", "[", COMMAND, 0, "page"),
        ("Forward", "goForward:", "]", COMMAND, 0, "page"),
        None,
    ]),
)
# Cmd+= zooms in too (the same key as Plus, without Shift), as in Safari: an item of its own, out of sight.
ZOOM_IN_TOO = ("Zoom In", "zoomIn:", "=", COMMAND)


def zoom_step(current: float, step: int) -> float:
    """The next of Safari's steps from ``current``: one bigger (``step`` 1) or smaller (-1)."""
    if step > 0:
        return next((zoom for zoom in ZOOMS if zoom > current + 0.001), ZOOMS[-1])
    return next((zoom for zoom in reversed(ZOOMS) if zoom < current - 0.001), ZOOMS[0])


def make_way(path: str, *, trash) -> str:
    """Make way for a download to be saved at ``path``: a file there (the person said Replace) goes to the Trash with
    ``trash(path)``, which returns why it couldn't ("" when it went). "" when the download can be saved there, else
    why not."""
    if not os.path.lexists(path):
        return ""
    if os.path.isdir(path) and not os.path.islink(path):
        return A_FOLDER
    why = trash(path)
    if why:
        return why
    return "it is still there" if os.path.lexists(path) else ""


# Put in place by ``prepare``: what the methods given to pywebview's classes use (PyObjC's, or the tests' stand-ins).
_kit: dict[str, Any] = {}
_made: dict[str, Any] = {}


def _said(error) -> str:
    """An NSError (or anything else) in its own few words."""
    try:
        text = str(error.localizedDescription())
    except Exception:
        text = str(error or "")
    return " ".join(text.split()).rstrip(".")[:200] or "macOS gave no reason"


def _trash(path: str) -> str:
    foundation = _kit["Foundation"]
    url = foundation.NSURL.fileURLWithPath_(path)
    found = foundation.NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(url, None, None)
    ok, error = (found[0], found[-1]) if isinstance(found, tuple) else (found, None)
    return "" if ok else _said(error)


def _alert(title: str, text: str) -> None:
    """A plain alert in the window, from WebKit's own callbacks (on the window's own thread)."""
    try:
        appkit = _kit["AppKit"]
        alert = appkit.NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(text)
        alert.runModal()
    except Exception as exc:  # said in the log at least
        print(f"{title}: {text} (and the alert couldn't be shown: {exc})", flush=True)
        return
    print(f"{title}: {text}", flush=True)


def _keep_menu(self, menu, event) -> None:
    """WebKit's right-click menu, as WebKit makes it (pywebview's empties it)."""


def _decide_destination(self, download, response, suggested, completion_handler) -> None:
    """Where a download is saved: asked in the save panel, starting in Downloads, as pywebview asks. A file there that
    the person said to replace goes to the Trash first: WebKit saves a download only to a file that isn't there."""
    appkit, foundation = _kit["AppKit"], _kit["Foundation"]
    name = str(suggested or "the download")
    self.closedesk_name = name
    panel = appkit.NSSavePanel.savePanel()
    downloads = foundation.NSSearchPathForDirectoriesInDomains(foundation.NSDownloadsDirectory, foundation.NSUserDomainMask, True)[0]
    panel.setDirectoryURL_(foundation.NSURL.fileURLWithPath_(downloads))
    panel.setNameFieldStringValue_(name)
    if panel.runModal() != appkit.NSFileHandlingPanelOKButton:
        completion_handler(None)
        return
    path = str(panel.URL().path())
    why = make_way(path, trash=_trash)
    if why:
        self.closedesk_said = True
        completion_handler(None)
        _alert(CANT_SAVE.format(name=os.path.basename(path)), NOT_REPLACED.format(why=why))
        return
    completion_handler(foundation.NSURL.fileURLWithPath_(path))


def _download_failed(self, download, error, resume_data) -> None:
    """A download that couldn't be saved is said (pywebview says nothing). Not one the person cancelled, nor one
    CloseDesk stopped after saying why."""
    try:
        if int(error.code()) == CANCELLED:
            return
    except Exception:
        pass
    if getattr(self, "closedesk_said", False):
        return
    _alert(CANT_SAVE.format(name=getattr(self, "closedesk_name", "the download")), NOT_SAVED.format(why=_said(error)))


def prepare(*, cocoa=None, objc=None, appkit=None, foundation=None) -> list[str]:
    """Before webview.start: pywebview's Cocoa window given WebKit's right-click menu back, and a download delegate that
    replaces a file the person said to replace. What couldn't be done, in words (the window works without it)."""
    try:
        if cocoa is None:
            from webview.platforms import cocoa
        if objc is None:
            import objc
        if appkit is None:
            import AppKit as appkit
        if foundation is None:
            import Foundation as foundation
    except Exception as exc:
        return [f"the macOS parts didn't load ({exc})"]
    _kit.update(AppKit=appkit, Foundation=foundation, objc=objc)
    missed: list[str] = []
    view = cocoa.BrowserView
    try:
        # Replaced in its class, not by a subclass: pywebview's own methods there call super(BrowserView.WebKitHost).
        objc.classAddMethods(view.WebKitHost, [objc.selector(_keep_menu, selector=b"willOpenMenu:withEvent:", signature=b"v@:@@")])
    except Exception as exc:
        missed.append(f"the right-click menu ({exc})")
    try:
        delegate = _made.get("delegate")
        if delegate is None or not issubclass(delegate, view.DownloadDelegate):

            # pywebview makes one for each download, by this name, when it starts. (An Objective-C class: made once.)
            class CloseDeskDownloadDelegate(view.DownloadDelegate):
                def download_decideDestinationUsingResponse_suggestedFilename_completionHandler_(  # noqa: N802
                    self, download, response, suggested, completion_handler
                ):
                    _decide_destination(self, download, response, suggested, completion_handler)

                def download_didFailWithError_resumeData_(self, download, error, resume_data):  # noqa: N802
                    _download_failed(self, download, error, resume_data)

            delegate = _made["delegate"] = CloseDeskDownloadDelegate
        view.DownloadDelegate = delegate
    except Exception as exc:
        missed.append(f"saving a download over a file already there ({exc})")
    return missed


def _typed(signature: bytes):
    """A method given to Objective-C with its types (``signature``), where PyObjC is here; as it is for the tests."""
    objc = _kit.get("objc")
    typed = getattr(objc, "typedSelector", None)
    return typed(signature) if typed is not None else (lambda method: method)


def show_bar(page, bar, shown: bool, *, appkit) -> bool:
    """The find bar ``bar`` (AppKit's, given by the NSTextFinder) shown above the page, the page below it; or taken
    away, the page as tall as the window again. The first time, the page is put in a view of its own, the window's
    content (pywebview makes the page the window's content itself). False when the page isn't in its window yet."""
    window = page.window()
    if window is None:
        return False
    holder = page.superview()
    if holder is None or window.contentView() == page:
        holder = appkit.NSView.alloc().initWithFrame_(page.frame())
        holder.setAutoresizesSubviews_(True)
        window.setContentView_(holder)
        holder.addSubview_(page)
    size = holder.bounds().size
    tall = float(bar.frame().size.height) if shown and bar is not None else 0.0
    page.setAutoresizingMask_(WIDTH_SIZABLE | HEIGHT_SIZABLE)
    page.setFrame_(appkit.NSMakeRect(0, 0, size.width, size.height - tall))
    if bar is not None:
        if shown:
            bar.setAutoresizingMask_(WIDTH_SIZABLE | MIN_Y_MARGIN)
            bar.setFrame_(appkit.NSMakeRect(0, size.height - tall, size.width, tall))
            if bar.superview() != holder:
                holder.addSubview_(bar)
        elif bar.superview() is not None:
            bar.removeFromSuperview()
    if not shown:
        window.makeFirstResponder_(page)  # typing goes to the page again
    return True


def _find_bar_class(appkit):
    """What holds the window's find bar for AppKit's NSTextFinder (NSTextFinderBarContainer): it shows the bar above
    the page (``show_bar``), and gives the page as what is searched."""
    made = _made.get("find_bar")
    if made is not None:
        return made

    class CloseDeskFindBar(appkit.NSObject):
        @_typed(b"@@:")
        def findBarView(self):  # noqa: N802 (Objective-C's names, from here on)
            return getattr(self, "bar", None)

        @_typed(b"v@:@")
        def setFindBarView_(self, view):  # noqa: N802
            old = getattr(self, "bar", None)
            if old is not None and old != view and old.superview() is not None:
                old.removeFromSuperview()
            self.bar = view
            if getattr(self, "shown", False):
                show_bar(self.page, view, True, appkit=appkit)

        @_typed(b"Z@:")
        def isFindBarVisible(self):  # noqa: N802
            return bool(getattr(self, "shown", False))

        @_typed(b"v@:Z")
        def setFindBarVisible_(self, shown):  # noqa: N802
            # Not said to be shown when it couldn't be (Find asks again then, rather than only going to its box).
            self.shown = show_bar(self.page, getattr(self, "bar", None), bool(shown), appkit=appkit) and bool(shown)

        @_typed(b"@@:")
        def contentView(self):  # noqa: N802
            return self.page

        @_typed(b"v@:")
        def findBarViewDidChangeHeight(self):  # noqa: N802 (its Replace row shown or hidden)
            if getattr(self, "shown", False):
                show_bar(self.page, getattr(self, "bar", None), True, appkit=appkit)

    _made["find_bar"] = CloseDeskFindBar
    return CloseDeskFindBar


def _answers_class(appkit):
    """CloseDesk's own menu items' target: Print, the zoom and Find, for the page's view it is given."""
    made = _made.get("answers")
    if made is not None:
        return made

    class CloseDeskMenuAnswers(appkit.NSObject):
        def printPage_(self, sender):  # noqa: N802 (Objective-C's name: printPage:)
            page = self.page
            operation = page.printOperationWithPrintInfo_(appkit.NSPrintInfo.sharedPrintInfo())
            operation.setShowsPrintPanel_(True)
            operation.setShowsProgressPanel_(True)
            view = operation.view()
            if view is not None:
                view.setFrame_(page.bounds())  # WebKit's own print view has no size until given one
            operation.runOperationModalForWindow_delegate_didRunSelector_contextInfo_(page.window(), None, None, None)

        def zoomIn_(self, sender):  # noqa: N802
            self.page.setPageZoom_(zoom_step(float(self.page.pageZoom()), 1))

        def zoomOut_(self, sender):  # noqa: N802
            self.page.setPageZoom_(zoom_step(float(self.page.pageZoom()), -1))

        def actualSize_(self, sender):  # noqa: N802
            # Pinching on the trackpad magnifies the page apart from its zoom: both put back.
            self.page.setPageZoom_(1.0)
            self.page.setMagnification_(1.0)

        def performTextFinderAction_(self, sender):  # noqa: N802 (Find, Find Next and Find Previous)
            if self.finder is not None:
                self.finder.performAction_(int(sender.tag()))

    _made["answers"] = CloseDeskMenuAnswers
    return CloseDeskMenuAnswers


def _item(appkit, title, action, key, modifiers, tag=0, target=None):
    item = appkit.NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
    item.setKeyEquivalentModifierMask_(modifiers)
    if tag:
        item.setTag_(tag)
    if target is not None:
        item.setTarget_(target)
    return item


def _submenu(appkit, main, title: str, at: int):
    """The menu bar's menu named ``title`` (pywebview's Edit and View), or one made at ``at``."""
    for index in range(main.numberOfItems()):
        found = main.itemAtIndex_(index).submenu()
        if found is not None and str(found.title()) == title:
            return found
    menu = appkit.NSMenu.alloc().initWithTitle_(title)
    holder = appkit.NSMenuItem.alloc().init()
    holder.setTitle_(title)
    holder.setSubmenu_(menu)
    main.insertItem_atIndex_(holder, min(at, main.numberOfItems()))
    return menu


def find_bar(page, *, appkit):
    """AppKit's find bar for ``page`` (the window's WKWebView, which finds the words): its NSTextFinder, which keeps
    neither the page nor what holds the bar (the caller keeps them)."""
    bar = _find_bar_class(appkit).alloc().init()
    bar.page = page
    finder = appkit.NSTextFinder.alloc().init()
    finder.setClient_(page)
    finder.setFindBarContainer_(bar)
    finder.setIncrementalSearchingEnabled_(True)  # found as the word is typed, as in Safari
    return finder, bar


def add_menus(page, *, appkit, application) -> Any:
    """On the window's own thread, once pywebview has made the menu bar: the items in ``MENUS``, for ``page`` (the
    window's WKWebView), the window's find bar, and zooming with two fingers. What answers CloseDesk's own items
    (kept by the caller: the menu bar doesn't keep its items' targets, nor the find bar its parts)."""
    answers = _answers_class(appkit).alloc().init()
    answers.page, answers.missed = page, ""
    try:
        answers.finder, answers.find_bar = find_bar(page, appkit=appkit)
    except Exception as exc:  # the rest of the menu bar still works
        answers.finder = answers.find_bar = None
        answers.missed = f"the find bar ({_said(exc)})"
    whose = {"page": page, "window": None, "closedesk": answers}
    main = application.mainMenu()
    for title, items in MENUS:
        menu = _submenu(appkit, main, title, 1)
        # Edit's Undo and Redo, and View's items, at the top; Find at the end of Edit.
        at = 0 if items[0] is not None else menu.numberOfItems()
        for entry in items:
            if entry is None:
                item = appkit.NSMenuItem.separatorItem()
            else:
                name, action, key, modifiers, tag, who = entry
                item = _item(appkit, name, action, key, modifiers, tag, whose[who])
            menu.insertItem_atIndex_(item, at)
            at += 1
        if title == "View":
            name, action, key, modifiers = ZOOM_IN_TOO
            item = _item(appkit, name, action, key, modifiers, 0, answers)
            item.setHidden_(True)
            if hasattr(item, "setAllowsKeyEquivalentWhenHidden_"):  # macOS 13 and later
                item.setAllowsKeyEquivalentWhenHidden_(True)
            menu.insertItem_atIndex_(item, at)
    page.setAllowsMagnification_(True)
    return answers


# The targets of CloseDesk's own menu items, kept while the window is open.
_kept: list = []


def page_of(window, *, cocoa=None):
    """The WKWebView of a pywebview window (its BrowserView's)."""
    if cocoa is None:
        from webview.platforms import cocoa
    return cocoa.BrowserView.instances[window.uid].webview


def menus_on(window, *, cocoa=None, appkit=None, call_after=None, wait: float = 10.0) -> str:
    """Once the window shows (from a thread of its own): the menu bar's items added, on the window's own thread.
    "" when done, else why not."""
    try:
        if appkit is None:
            import AppKit as appkit
        if call_after is None:
            from PyObjCTools.AppHelper import callAfter as call_after
        page = page_of(window, cocoa=cocoa)
    except Exception as exc:
        return f"the macOS parts didn't load ({exc})"
    done = threading.Event()
    found: dict[str, str] = {}

    def on_the_windows_thread() -> None:
        try:
            answers = add_menus(page, appkit=appkit, application=appkit.NSApplication.sharedApplication())
            _kept.append(answers)
            if answers.missed:
                found["why"] = answers.missed
        except Exception as exc:
            found["why"] = str(exc) or type(exc).__name__
        finally:
            done.set()

    call_after(on_the_windows_thread)
    if not done.wait(wait):
        return "the window's own thread didn't answer"
    return found.get("why", "")


def _action(item) -> str:
    """A menu item's action by its name ("copy:"), "" for none (PyObjC gives a selector's name as text or bytes)."""
    action = item.action()
    if isinstance(action, bytes):
        action = action.decode("ascii", "replace")
    return str(action or "")


def _responds(thing, action: str) -> bool:
    try:
        return bool(thing.respondsToSelector_(action))
    except AttributeError:  # the tests' stand-ins
        return callable(getattr(thing, action.replace(":", "_"), None))


def _answered(target, action: str, page) -> bool:
    """Whether a menu item's action is answered: by its target, or (with none) by the page or what holds it, up to the
    window and the app (AppKit's responder chain, from the page as the window's first responder)."""
    if target is not None:
        return _responds(target, action)
    responder = page
    for _ in range(40):
        if responder is None:
            return False
        if _responds(responder, action):
            return True
        responder = responder.nextResponder()
    return False


def _press(appkit, title: str, action: str):
    """A menu bar item named ``title``, chosen (as a click on it, or its keys, sends its action), and the item."""
    application = appkit.NSApplication.sharedApplication()
    main = application.mainMenu()
    for index in range(main.numberOfItems()):
        menu = main.itemAtIndex_(index).submenu()
        if menu is None:
            continue
        for at in range(menu.numberOfItems()):
            item = menu.itemAtIndex_(at)
            if str(item.title()) == title and _action(item) == action:
                application.sendAction_to_from_(item.action(), item.target(), item)
                return item
    return None


def _bar_shown(item) -> bool:
    """Whether Find showed the find bar in the window: the bar there, and said to be."""
    try:
        holder = item.target().find_bar
        bar = holder.findBarView()
        return bool(holder.isFindBarVisible()) and bar is not None and bar.window() is not None
    except Exception:  # no find bar (CloseDesk's isn't what answers Find)
        return False


def read_back(window, *, cocoa=None, appkit=None, call_after=None, wait: float = 10.0, tries: int = 20) -> dict[str, Any]:
    """CI's check of the real window: what the page's view and the menu bar do now, on the window's own thread.
    {"menu": a right-click menu given to the page's view keeps its items, "keys": (title, key, modifiers) of the menu
    bar's items, "unanswered": titles of ``MENUS``' items whose action nothing answers, "find": choosing Find showed
    the find bar (taken away again after), "zoom": zooming with two fingers on}."""
    if appkit is None:
        import AppKit as appkit
    if call_after is None:
        from PyObjCTools.AppHelper import callAfter as call_after
    if cocoa is None:
        from webview.platforms import cocoa
    page = page_of(window, cocoa=cocoa)
    found: dict[str, Any] = {"menu": False, "keys": [], "unanswered": [], "find": False, "zoom": False}
    pressed: dict[str, Any] = {}

    def on_the_windows_thread(work) -> bool:
        done = threading.Event()

        def run() -> None:
            try:
                work()
            finally:
                done.set()

        call_after(run)
        return done.wait(wait)

    def read() -> None:
        # As WebKit opens it: a menu with one item, which pywebview's empties.
        menu = appkit.NSMenu.alloc().initWithTitle_("CloseDesk's check")
        menu.addItemWithTitle_action_keyEquivalent_("Copy", "copy:", "")
        page.willOpenMenu_withEvent_(menu, None)
        found["menu"] = int(menu.numberOfItems()) == 1
        keys, unanswered = [], []
        wanted = {(entry[0], entry[1]) for _, items in MENUS for entry in items if entry is not None}
        main = appkit.NSApplication.sharedApplication().mainMenu()
        for index in range(main.numberOfItems()):
            menu = main.itemAtIndex_(index).submenu()
            if menu is None:
                continue
            for at in range(menu.numberOfItems()):
                item = menu.itemAtIndex_(at)
                if str(item.keyEquivalent()) and not item.isSeparatorItem():
                    keys.append((str(item.title()), str(item.keyEquivalent()), int(item.keyEquivalentModifierMask())))
                action = "" if item.isSeparatorItem() else _action(item)
                if (str(item.title()), action) in wanted and not _answered(item.target(), action, page):
                    unanswered.append(str(item.title()))
        found["keys"], found["unanswered"] = keys, unanswered
        found["zoom"] = bool(page.allowsMagnification())
        pressed["find"] = _press(appkit, "Find…", "performTextFinderAction:")

    on_the_windows_thread(read)
    item = pressed.get("find")
    if item is not None:
        # The find bar may come in at the window's next turn: looked for a few times.
        for _ in range(tries):
            on_the_windows_thread(lambda: found.update(find=_bar_shown(item)))
            if found["find"]:
                break
            time.sleep(0.1)
        if found["find"]:
            on_the_windows_thread(lambda: item.target().finder.performAction_(HIDE_FIND))
    return found


def all_on(found: dict[str, Any]) -> bool:
    """Whether ``read_back`` found everything on: the right-click menu, every key in ``MENUS`` with its action
    answered, the find bar shown by Find, and the zoom."""
    have = {(title, key, modifiers & (COMMAND | SHIFT)) for title, key, modifiers in found.get("keys", [])}
    wanted = {(entry[0], entry[2], entry[3]) for _, items in MENUS for entry in items if entry is not None}
    return (
        bool(found.get("menu")) and wanted <= have and found.get("unanswered") == [] and bool(found.get("find"))
        and bool(found.get("zoom"))
    )

