"""The CloseDesk window on macOS: what pywebview 6.2's Cocoa window takes away from WebKit, put back (Windows' WebView2
has its own, app_window.browser_keys).

- **The right-click menu.** pywebview empties WebKit's (its WKWebView's willOpenMenu:withEvent:) unless it runs for
  debugging, which would add Inspect Element too. Kept as WebKit makes it: Copy, Paste, Look Up, spelling suggestions.
- **The menu bar's items, with their keys.** pywebview's has Cut, Copy, Paste, Select All and Enter Full Screen, and
  answers a few Cmd keys itself. Added, as in Safari: Undo and Redo (Cmd+Z, Cmd+Shift+Z); Find, Find Next and Find
  Previous (Cmd+F, Cmd+G, Cmd+Shift+G); Print (Cmd+P); Reload (Cmd+R); Actual Size, Zoom In and Zoom Out (Cmd+0,
  Cmd+Plus, Cmd+Minus); Back and Forward (Cmd+[, Cmd+]). And zooming with two fingers on the trackpad.
- **A download saved over a file that is there** (Replace, in the save panel): WebKit saves a download only to a file
  that isn't there, and pywebview says nothing when it doesn't, so yesterday's CSV stayed. The file there goes to the
  Trash first (it can be put back from there), and a download that fails says so.

``prepare`` before webview.start; ``menus_on`` once the window shows. Each part that can't be done is said in the
window's log, and the window works without it. The macOS parts (PyObjC's AppKit, Foundation and objc, pywebview's
Cocoa window) are given to them, so that the tests can give stand-ins."""

from __future__ import annotations

import os
import threading
from typing import Any

# What CI's check of the real window looks for in its log, once it has read the menu bar and the zoom back.
KEYS_ON = "The window's right-click menu, its menu bar's keys and zooming are on."

# Safari's steps of zoom.
ZOOMS = (0.5, 0.67, 0.75, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0)

# AppKit's numbers: the modifier keys (NSEventModifierFlag), and what Find asks of the page (NSTextFinderAction).
COMMAND = 1 << 20
SHIFT = 1 << 17
SHOW_FIND, FIND_NEXT, FIND_PREVIOUS = 1, 2, 3
# NSURLErrorCancelled: the download was cancelled (the save panel's Cancel, or CloseDesk itself after saying why).
CANCELLED = -999

CANT_SAVE = "CloseDesk couldn't save {name}"
NOT_REPLACED = "The file already there couldn't be moved to the Trash ({why}), so nothing was saved. Save it under another name, or move that file away first."
NOT_SAVED = "Nothing was saved ({why}). Try Download again."
A_FOLDER = "it is a folder"

# (menu, [(title, action, key, modifiers, tag, who answers)]): "page" is the page's own view (WebKit's WKWebView),
# "window" the window (its Undo and Redo), "closedesk" CloseDesk's own (``Answers``). Added to pywebview's Edit and
# View menus, and a File menu of its own; at the top of each, before what pywebview has there.
MENUS = (
    ("File", [("Print…", "printPage:", "p", COMMAND, 0, "closedesk")]),
    ("Edit", [
        ("Undo", "undo:", "z", COMMAND, 0, "window"),
        ("Redo", "redo:", "z", COMMAND | SHIFT, 0, "window"),
        None,
    ]),
    ("Edit", [
        None,
        ("Find…", "performTextFinderAction:", "f", COMMAND, SHOW_FIND, "page"),
        ("Find Next", "performTextFinderAction:", "g", COMMAND, FIND_NEXT, "page"),
        ("Find Previous", "performTextFinderAction:", "g", COMMAND | SHIFT, FIND_PREVIOUS, "page"),
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


def _answers_class(appkit):
    """CloseDesk's own menu items' target: Print and the zoom, for the page's view it is given."""
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
            self.page.setPageZoom_(1.0)

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


def add_menus(page, *, appkit, application) -> Any:
    """On the window's own thread, once pywebview has made the menu bar: the items in ``MENUS``, for ``page`` (the
    window's WKWebView), and zooming with two fingers. What answers CloseDesk's own items (kept by the caller: the
    menu bar doesn't keep its items' targets)."""
    answers = _answers_class(appkit).alloc().init()
    answers.page = page
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
            _kept.append(add_menus(page, appkit=appkit, application=appkit.NSApplication.sharedApplication()))
        except Exception as exc:
            found["why"] = str(exc) or type(exc).__name__
        finally:
            done.set()

    call_after(on_the_windows_thread)
    if not done.wait(wait):
        return "the window's own thread didn't answer"
    return found.get("why", "")


def read_back(window, *, cocoa=None, appkit=None, call_after=None, wait: float = 10.0) -> dict[str, Any]:
    """CI's check of the real window: what the page's view and the menu bar do now, on the window's own thread.
    {"menu": a right-click menu given to the page's view keeps its items, "keys": (title, key, modifiers) of the menu
    bar's items, "zoom": zooming with two fingers on}."""
    if appkit is None:
        import AppKit as appkit
    if call_after is None:
        from PyObjCTools.AppHelper import callAfter as call_after
    if cocoa is None:
        from webview.platforms import cocoa
    page = page_of(window, cocoa=cocoa)
    done = threading.Event()
    found: dict[str, Any] = {"menu": False, "keys": [], "zoom": False}

    def on_the_windows_thread() -> None:
        try:
            # As WebKit opens it: a menu with one item, which pywebview's empties.
            menu = appkit.NSMenu.alloc().initWithTitle_("CloseDesk's check")
            menu.addItemWithTitle_action_keyEquivalent_("Copy", "copy:", "")
            page.willOpenMenu_withEvent_(menu, None)
            found["menu"] = int(menu.numberOfItems()) == 1
            keys = []
            main = appkit.NSApplication.sharedApplication().mainMenu()
            for index in range(main.numberOfItems()):
                menu = main.itemAtIndex_(index).submenu()
                if menu is None:
                    continue
                for at in range(menu.numberOfItems()):
                    item = menu.itemAtIndex_(at)
                    if str(item.keyEquivalent()) and not item.isSeparatorItem():
                        keys.append((str(item.title()), str(item.keyEquivalent()), int(item.keyEquivalentModifierMask())))
            found["keys"] = keys
            found["zoom"] = bool(page.allowsMagnification())
        finally:
            done.set()

    call_after(on_the_windows_thread)
    done.wait(wait)
    return found


def all_on(found: dict[str, Any]) -> bool:
    """Whether ``read_back`` found everything on: the right-click menu, every key in ``MENUS``, and the zoom."""
    have = {(title, key, modifiers & (COMMAND | SHIFT)) for title, key, modifiers in found.get("keys", [])}
    wanted = {(entry[0], entry[2], entry[3]) for _, items in MENUS for entry in items if entry is not None}
    return bool(found.get("menu")) and wanted <= have and bool(found.get("zoom"))

