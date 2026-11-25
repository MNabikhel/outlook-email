"""CloseDesk's icon: the files committed in static/ are the ones scripts/build_icon.py draws, in the sizes and formats
Windows and macOS need (the shortcut, the window, the taskbar and Dock, CloseDesk.app), and the pages' own icon is the
same mark."""

from __future__ import annotations

import importlib.util
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "controller_inbox" / "static"


def _build_icon():
    spec = importlib.util.spec_from_file_location("build_icon_under_test", ROOT / "scripts" / "build_icon.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _picture(path: Path, size=None) -> Image.Image:
    """The picture of ``size`` pixels in an icon file (an .ico's, or a .icns's at twice the size it is named for, as a
    Retina screen shows it); the largest when not given."""
    with Image.open(path) as icon:
        if size is not None and icon.format == "ICNS":
            icon.size = (size // 2, size // 2)
            icon.load(scale=2)
        elif size is not None:
            icon.size = (size, size)
            icon.load()
        else:
            icon.load()
        return icon.convert("RGBA")


def _ico_entries(path: Path) -> list[tuple[int, int, str]]:
    """(size, bits per pixel, "png" or "bmp") of each picture in an .ico."""
    data = path.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    assert (reserved, kind) == (0, 1), "not an icon file"
    entries = []
    for i in range(count):
        width, _height, _colours, _, _planes, bits, _size, offset = struct.unpack_from("<BBBBHHII", data, 6 + 16 * i)
        entries.append((width or 256, bits, "png" if data[offset : offset + 8] == b"\x89PNG\r\n\x1a\n" else "bmp"))
    return entries


def test_the_windows_icon_has_every_size_windows_asks_for_each_a_32_bit_bitmap():
    entries = _ico_entries(STATIC / "closedesk.ico")
    assert sorted(size for size, _, _ in entries) == [16, 24, 32, 48, 64, 256]
    # The window's icon is read by .NET (pywebview's System.Drawing.Icon), which has had trouble with PNG pictures.
    assert all(bits == 32 and kind == "bmp" for _, bits, kind in entries), entries
    with Image.open(STATIC / "closedesk.ico") as icon:
        assert sorted(icon.info["sizes"]) == [(s, s) for s in (16, 24, 32, 48, 64, 256)]


def test_the_mac_icons_open_at_1024_and_the_macos_26_one_is_filled_to_its_edges():
    for name in ("closedesk.icns", "closedesk-full.icns"):
        with Image.open(STATIC / name) as icon:
            assert icon.format == "ICNS"
        picture = _picture(STATIC / name)  # 1024 pixels: the Retina picture the Dock shows
        assert picture.size == (1024, 1024), name
        corner, middle = picture.getpixel((2, 2))[3], picture.getpixel((512, 512))[3]
        assert middle == 255, name
        if name == "closedesk-full.icns":
            # macOS 26 rounds an app's icon itself; one with clear edges it shrinks onto a grey tile.
            assert picture.getchannel("A").getextrema() == (255, 255)
        else:
            assert corner == 0, "a Mac icon (macOS 15 and earlier) has a clear margin round its rounded square"
        for size in (32, 64):  # drawn for themselves (sharp), not shrunk from the big one
            assert _picture(STATIC / name, size).size == (size, size)


def test_the_committed_icon_files_are_what_the_script_draws(tmp_path):
    # Changed by hand, or the drawing changed without the files being made again: run scripts/build_icon.py.
    made = _build_icon().build(tmp_path)
    assert {path.name for path in made} == {"closedesk.ico", "closedesk.icns", "closedesk-full.icns", "closedesk.png", "icon.svg"}
    assert (tmp_path / "icon.svg").read_text(encoding="utf-8") == (STATIC / "icon.svg").read_text(encoding="utf-8")
    for name, sizes in (("closedesk.ico", (16, 24, 32, 48, 64, 256)), ("closedesk.png", (None,)),
                        ("closedesk.icns", (32, 64, 1024)), ("closedesk-full.icns", (32, 64, 1024))):
        for size in sizes:
            difference = ImageChops.difference(_picture(tmp_path / name, size), _picture(STATIC / name, size)).getextrema()
            # A pixel or two may come out a shade apart on another Pillow; nothing more.
            assert max(high for _, high in difference) <= 3, (name, size, difference)


def test_the_icon_is_cream_letters_on_navy_and_sharp_at_16_pixels():
    build_icon = _build_icon()
    small = _picture(STATIC / "closedesk.ico", 16)
    # Drawn pixel by pixel: the C's upright and the D's are cream; inside the letters, the navy.
    for x, y in ((2, 7), (3, 7), (8, 7), (9, 7), (12, 7), (13, 7)):
        assert small.getpixel((x, y))[:3] == build_icon.CREAM, (x, y)
    for x, y in ((5, 7), (10, 7), (7, 7)):
        r, g, b, _ = small.getpixel((x, y))
        assert b > r and b > 70 and r < 60, ((x, y), small.getpixel((x, y)))


def test_the_pages_icon_is_the_same_mark_drawn_as_shapes():
    # The browser tab shows static/icon.svg: the letters as shapes, so it looks the same without the Arial font.
    root = ET.fromstring((STATIC / "icon.svg").read_text(encoding="utf-8"))
    names = {element.tag.rsplit("}", 1)[-1] for element in root.iter()}
    assert "text" not in names and "path" in names and root.get("viewBox") == "0 0 32 32"


@pytest.mark.parametrize("name", ["closedesk.ico", "closedesk.icns", "closedesk-full.icns", "closedesk.png", "icon.svg"])
def test_the_icon_files_ship_with_closedesk(name):
    # pyproject's package data takes static/*, and an update replaces them with the rest of the folder.
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]["package-data"]
    assert "static/*" in data["controller_inbox"]
    assert (STATIC / name).is_file()
