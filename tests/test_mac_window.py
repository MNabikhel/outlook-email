"""The CloseDesk window on macOS (mac_window.py): what pywebview 6.2's Cocoa window takes away from WebKit, put back.
WebKit's right-click menu; the menu bar's Undo, Redo, Find, Print, Reload, zoom, Back and Forward with their keys;
and a download saved over a file already there. Run here with stand-ins for PyObjC and pywebview's Cocoa window (the
real one is read back in CI's window check on macOS, tests/launcher_smoke.py)."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from controller_inbox import mac_window

# PyObjC, standing in ----------------------------------------------------------------------------------------------


class FakeObjc:
    def selector(self, function, *, selector: bytes, signature: bytes):
        return SimpleNamespace(callable=function, selector=selector, signature=signature)

    def classAddMethods(self, cls, methods) -> None:  # noqa: N802 (PyObjC's name)
        for method in methods:
            setattr(cls, method.selector.decode().replace(":", "_"), method.callable)


class Item:
    def __init__(self, title: str = "", action=None, key: str = "", *, separator: bool = False) -> None:
        self._title, self.action, self.key, self.separator = title, action, key, separator
        self.mask, self.tag, self.target, self.sub, self.hidden, self.when_hidden = 0, 0, None, None, False, False

    # NSMenuItem's
    def title(self):
        return self._title

    def setTitle_(self, title):  # noqa: N802
        self._title = title

    def keyEquivalent(self):  # noqa: N802
        return self.key

    def keyEquivalentModifierMask(self):  # noqa: N802
        return self.mask

    def setKeyEquivalentModifierMask_(self, mask):  # noqa: N802
        self.mask = mask

    def setTag_(self, tag):  # noqa: N802
        self.tag = tag

    def setTarget_(self, target):  # noqa: N802
        self.target = target

    def setSubmenu_(self, menu):  # noqa: N802
        self.sub = menu

    def submenu(self):
        return self.sub

    def setHidden_(self, hidden):  # noqa: N802
        self.hidden = hidden

    def setAllowsKeyEquivalentWhenHidden_(self, allowed):  # noqa: N802
        self.when_hidden = allowed

    def isSeparatorItem(self):  # noqa: N802
        return self.separator


class Menu:
    def __init__(self, title: str = "") -> None:
        self._title, self.items = title, []

    def title(self):
        return self._title

    def numberOfItems(self):  # noqa: N802
        return len(self.items)

    def itemAtIndex_(self, index):  # noqa: N802
        return self.items[index]

    def insertItem_atIndex_(self, item, index):  # noqa: N802
        self.items.insert(index, item)

    def addItem_(self, item):  # noqa: N802
        self.items.append(item)

    def addItemWithTitle_action_keyEquivalent_(self, title, action, key):  # noqa: N802
        item = Item(title, action, key)
        self.items.append(item)
        return item

    def removeAllItems(self):  # noqa: N802
        self.items.clear()

    def find(self, title: str) -> Item | None:
        return next((item for item in self.items if item.title() == title), None)


class _Alloc:
    def __init__(self, make) -> None:
        self.make = make

    def init(self):
        return self.make()

    def initWithTitle_(self, title):  # noqa: N802
        return self.make(title)

    def initWithTitle_action_keyEquivalent_(self, title, action, key):  # noqa: N802
        return self.make(title, action, key)


class Page:
    """The window's WKWebView."""

    def __init__(self) -> None:
        self.zoom, self.magnifies, self.printed = 1.0, False, []

    def pageZoom(self):  # noqa: N802
        return self.zoom

    def setPageZoom_(self, zoom):  # noqa: N802
        self.zoom = zoom

    def setAllowsMagnification_(self, on):  # noqa: N802
        self.magnifies = on

    def allowsMagnification(self):  # noqa: N802
        return self.magnifies

    def bounds(self):
        return "the page's bounds"

    def window(self):
        return "the window"

    def printOperationWithPrintInfo_(self, info):  # noqa: N802
        page = self

        class Operation:
            def __init__(self) -> None:
                self.frame = None

            def setShowsPrintPanel_(self, on):  # noqa: N802
                self.panel = on

            def setShowsProgressPanel_(self, on):  # noqa: N802
                pass

            def view(self):
                operation = self
                return SimpleNamespace(setFrame_=lambda frame: setattr(operation, "frame", frame))

            def runOperationModalForWindow_delegate_didRunSelector_contextInfo_(self, window, *rest):  # noqa: N802
                page.printed.append((window, self.panel, self.frame))

        return Operation()

    def willOpenMenu_withEvent_(self, menu, event):  # noqa: N802 (as the class has it, replaced or not)
        type(self).host.willOpenMenu_withEvent_(self, menu, event)


class AppKit:
    NSFileHandlingPanelOKButton = 1

    def __init__(self, tmp_path: Path) -> None:
        self.choose: str | None = None  # the save panel's answer: a path, or None for Cancel
        self.alerts: list[tuple[str, str]] = []
        self.main = Menu()
        kit = self

        class NSObject:
            @classmethod
            def alloc(cls):
                return _Alloc(cls)

        class NSSavePanel:
            @staticmethod
            def savePanel():  # noqa: N802
                chosen = kit.choose
                return SimpleNamespace(
                    setDirectoryURL_=lambda url: None, setNameFieldStringValue_=lambda name: None,
                    runModal=lambda: 1 if chosen else 0, URL=lambda: SimpleNamespace(path=lambda: chosen),
                )

        class NSAlert:
            @staticmethod
            def alloc():
                said: dict = {}
                return _Alloc(lambda: SimpleNamespace(
                    setMessageText_=lambda text: said.update(title=text), setInformativeText_=lambda text: said.update(text=text),
                    runModal=lambda: kit.alerts.append((said["title"], said["text"])),
                ))

        self.NSObject, self.NSSavePanel, self.NSAlert = NSObject, NSSavePanel, NSAlert
        self.NSMenu = SimpleNamespace(alloc=lambda: _Alloc(Menu))
        self.NSMenuItem = SimpleNamespace(alloc=lambda: _Alloc(Item), separatorItem=lambda: Item(separator=True))
        self.NSPrintInfo = SimpleNamespace(sharedPrintInfo=lambda: "print info")
        self.NSApplication = SimpleNamespace(sharedApplication=lambda: SimpleNamespace(mainMenu=lambda: kit.main))


class Foundation:
    NSDownloadsDirectory, NSUserDomainMask = 15, 1

    def __init__(self, tmp_path: Path) -> None:
        self.downloads = tmp_path / "Downloads"
        self.downloads.mkdir()
        self.trash = tmp_path / ".Trash"
        self.trash.mkdir()
        self.trash_refuses = ""
        found = self

        def trash(url, resulting, error):
            if found.trash_refuses:
                return (False, None, SimpleNamespace(localizedDescription=lambda: found.trash_refuses))
            moved = found.trash / Path(url.path).name
            shutil.move(url.path, moved)
            return (True, SimpleNamespace(path=str(moved)), None)

        self.NSURL = SimpleNamespace(fileURLWithPath_=lambda path: SimpleNamespace(path=str(path)))
        self.NSFileManager = SimpleNamespace(defaultManager=lambda: SimpleNamespace(trashItemAtURL_resultingItemURL_error_=trash))
        self.NSSearchPathForDirectoriesInDomains = lambda *where: [str(found.downloads)]


def pywebviews_cocoa():
    """pywebview 6.2.1's Cocoa window, as far as CloseDesk changes it (cocoa.py)."""

    class WebKitHost:
        def willOpenMenu_withEvent_(self, menu, event):  # noqa: N802
            menu.removeAllItems()  # pywebview's, unless it runs for debugging

    class DownloadDelegate:
        def download_decideDestinationUsingResponse_suggestedFilename_completionHandler_(self, *args):  # noqa: N802
            raise AssertionError("pywebview's own: hands WebKit a file that is there")

        @classmethod
        def alloc(cls):
            return _Alloc(cls)

    class BrowserView:
        instances: dict = {}

    BrowserView.WebKitHost, BrowserView.DownloadDelegate = WebKitHost, DownloadDelegate
    return SimpleNamespace(BrowserView=BrowserView)


@pytest.fixture
def mac(tmp_path, monkeypatch):
    monkeypatch.setattr(mac_window, "_made", {})
    monkeypatch.setattr(mac_window, "_kit", {})
    monkeypatch.setattr(mac_window, "_kept", [])
    kit, foundation, cocoa = AppKit(tmp_path), Foundation(tmp_path), pywebviews_cocoa()
    missed = mac_window.prepare(cocoa=cocoa, objc=FakeObjc(), appkit=kit, foundation=foundation)
    page = Page()
    Page.host = cocoa.BrowserView.WebKitHost
    cocoa.BrowserView.instances["main"] = SimpleNamespace(webview=page)
    # pywebview's menu bar: its app menu, Edit and View.
    app_menu, edit, view = Item("CloseDesk"), Item("Edit"), Item("View")
    app_menu.setSubmenu_(Menu("CloseDesk"))
    edit.setSubmenu_(Menu("Edit"))
    view.setSubmenu_(Menu("View"))
    for name, key in (("Cut", "x"), ("Copy", "c"), ("Paste", "v"), ("Select All", "a")):
        edit.sub.addItemWithTitle_action_keyEquivalent_(name, name.lower(), key)
    view.sub.addItemWithTitle_action_keyEquivalent_("Enter Full Screen", "toggleFullScreen:", "f")
    kit.main.items = [app_menu, edit, view]
    return SimpleNamespace(kit=kit, foundation=foundation, cocoa=cocoa, page=page, missed=missed,
                           window=SimpleNamespace(uid="main"), tmp=tmp_path)


def _download(mac, name: str, chosen: str | None):
    """A link with ``download`` clicked: pywebview's delegate for it (CloseDesk's now) asked where to save it."""
    mac.kit.choose = chosen
    delegate = mac.cocoa.BrowserView.DownloadDelegate.alloc().init()
    handed: list = []
    delegate.download_decideDestinationUsingResponse_suggestedFilename_completionHandler_(None, None, name, handed.append)
    return delegate, handed


# A download saved over a file that is there -----------------------------------------------------------------------


def test_a_download_saved_over_yesterdays_file_replaces_it(mac):
    # Monday's CSV in Downloads, Tuesday's saved over it (Replace): WebKit saves only to a file that isn't there, and
    # nothing was said, so Monday's stayed. It goes to the Trash first, and WebKit is given the place.
    assert mac.missed == []
    monday = mac.foundation.downloads / "closedesk-actions.csv"
    monday.write_text("Monday's actions", encoding="utf-8")
    _, handed = _download(mac, "closedesk-actions.csv", str(monday))
    (url,) = handed
    assert url.path == str(monday) and not monday.exists(), "the file there is out of the way before WebKit saves"
    assert (mac.foundation.trash / "closedesk-actions.csv").read_text(encoding="utf-8") == "Monday's actions", "kept in the Trash"
    assert mac.kit.alerts == []


def test_a_download_to_a_new_name_is_saved_as_before(mac):
    _, handed = _download(mac, "Invoice 2211.eml", str(mac.foundation.downloads / "Invoice 2211.eml"))
    assert handed[0].path == str(mac.foundation.downloads / "Invoice 2211.eml")
    assert list(mac.foundation.trash.iterdir()) == []


def test_cancel_in_the_save_panel_saves_nothing_and_says_nothing(mac):
    delegate, handed = _download(mac, "closedesk-fraud-log.csv", None)
    assert handed == [None]
    delegate.download_didFailWithError_resumeData_(None, SimpleNamespace(code=lambda: mac_window.CANCELLED), None)
    assert mac.kit.alerts == []


def test_a_file_that_cant_be_moved_to_the_trash_is_said_and_nothing_is_saved(mac):
    there = mac.foundation.downloads / "closedesk-actions.csv"
    there.write_text("Monday's", encoding="utf-8")
    mac.foundation.trash_refuses = "You don't have permission to move it to the Trash."
    delegate, handed = _download(mac, "closedesk-actions.csv", str(there))
    assert handed == [None] and there.read_text(encoding="utf-8") == "Monday's"
    ((title, text),) = mac.kit.alerts
    assert title == "CloseDesk couldn't save closedesk-actions.csv"
    assert "couldn't be moved to the Trash (You don't have permission to move it to the Trash)" in text and "nothing was saved" in text
    # WebKit then says the download was cancelled: not said twice.
    delegate.download_didFailWithError_resumeData_(None, SimpleNamespace(code=lambda: mac_window.CANCELLED), None)
    assert len(mac.kit.alerts) == 1


def test_a_download_that_fails_is_said(mac):
    delegate, _ = _download(mac, "Scan 14.pdf", str(mac.foundation.downloads / "Scan 14.pdf"))
    error = SimpleNamespace(code=lambda: -1009, localizedDescription=lambda: "The disk is full.")
    delegate.download_didFailWithError_resumeData_(None, error, None)
    assert mac.kit.alerts == [("CloseDesk couldn't save Scan 14.pdf", "Nothing was saved (The disk is full). Try Download again.")]


def test_making_way_for_a_download(tmp_path):
    gone: list[str] = []
    assert mac_window.make_way(str(tmp_path / "new.csv"), trash=gone.append) == "" and gone == []
    folder = tmp_path / "a folder"
    folder.mkdir()
    assert mac_window.make_way(str(folder), trash=gone.append) == mac_window.A_FOLDER and gone == []
    there = tmp_path / "there.csv"
    there.write_text("x", encoding="utf-8")
    assert mac_window.make_way(str(there), trash=lambda path: "locked") == "locked"
    assert mac_window.make_way(str(there), trash=lambda path: "") == "it is still there"
    assert mac_window.make_way(str(there), trash=lambda path: there.unlink() or "") == ""


# WebKit's right-click menu, and the menu bar's keys --------------------------------------------------------------


def test_the_right_click_menu_is_webkits_own(mac):
    # pywebview emptied it unless it ran for debugging: right-click on a misspelt word, a selected IBAN or a pasted
    # note showed nothing at all (no Copy, Paste, Look Up or spelling suggestions).
    menu = Menu()
    for name in ("Look Up “IBAN”", "Copy", "Paste"):
        menu.addItemWithTitle_action_keyEquivalent_(name, None, "")
    mac.page.willOpenMenu_withEvent_(menu, None)
    assert [item.title() for item in menu.items] == ["Look Up “IBAN”", "Copy", "Paste"]


def test_the_menu_bar_has_the_browsers_keys(mac):
    # Only Cut, Copy, Paste, Select All, Enter Full Screen and Quit: Cmd+F, Cmd+P, Cmd+Plus/Minus/0, Cmd+[ and
    # Cmd+Shift+Z did nothing.
    before = mac_window.read_back(mac.window, cocoa=mac.cocoa, appkit=mac.kit, call_after=lambda work: work())
    assert not mac_window.all_on(before)
    assert mac_window.menus_on(mac.window, cocoa=mac.cocoa, appkit=mac.kit, call_after=lambda work: work()) == ""
    found = mac_window.read_back(mac.window, cocoa=mac.cocoa, appkit=mac.kit, call_after=lambda work: work())
    assert mac_window.all_on(found), found
    keys = {(title, key, mask) for title, key, mask in found["keys"]}
    command, shift = mac_window.COMMAND, mac_window.SHIFT
    for wanted in [("Undo", "z", command), ("Redo", "z", command | shift), ("Find…", "f", command), ("Print…", "p", command),
                   ("Zoom In", "+", command), ("Zoom Out", "-", command), ("Actual Size", "0", command),
                   ("Back", "[", command), ("Forward", "]", command), ("Reload", "r", command),
                   ("Cut", "x", 0), ("Enter Full Screen", "f", 0)]:
        assert wanted in keys, wanted
    titles = [item.title() for item in mac.kit.main.items]
    assert titles == ["CloseDesk", "File", "Edit", "View"], titles
    edit = mac.kit.main.items[2].submenu()
    assert [item.title() for item in edit.items][:2] == ["Undo", "Redo"] and edit.find("Cut") is not None
    find = edit.find("Find…")
    assert find.action == "performTextFinderAction:" and find.tag == 1 and find.target is mac.page
    # Cmd+= zooms in too, as in Safari, from an item out of sight.
    view = mac.kit.main.items[3].submenu()
    hidden = [item for item in view.items if item.hidden]
    assert [(item.key, item.when_hidden) for item in hidden] == [("=", True)]
    assert mac.page.magnifies is True, "pinching to zoom"


def test_zoom_print_and_actual_size_act_on_the_page(mac):
    mac_window.menus_on(mac.window, cocoa=mac.cocoa, appkit=mac.kit, call_after=lambda work: work())
    view = mac.kit.main.items[3].submenu()
    press = lambda title: (lambda item: getattr(item.target, item.action.replace(":", "_"))(item))(view.find(title))  # noqa: E731
    press("Zoom In")
    press("Zoom In")
    assert mac.page.zoom == 1.25
    press("Actual Size")
    press("Zoom Out")
    assert mac.page.zoom == 0.9
    file_menu = mac.kit.main.items[1].submenu()
    item = file_menu.find("Print…")
    getattr(item.target, item.action.replace(":", "_"))(item)
    assert mac.page.printed == [("the window", True, "the page's bounds")]


def test_zoom_goes_in_safaris_steps_and_stops_at_the_ends():
    assert mac_window.zoom_step(1.0, 1) == 1.1 and mac_window.zoom_step(1.0, -1) == 0.9
    assert mac_window.zoom_step(1.05, 1) == 1.1 and mac_window.zoom_step(1.05, -1) == 1.0
    assert mac_window.zoom_step(3.0, 1) == 3.0 and mac_window.zoom_step(0.5, -1) == 0.5


@pytest.mark.skipif(sys.platform == "darwin", reason="on a Mac the parts load, and would change pywebview's window here")
def test_without_the_macos_parts_what_couldnt_be_done_is_said():
    (said,) = mac_window.prepare()
    assert said.startswith("the macOS parts didn't load")
