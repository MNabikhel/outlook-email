"""Exit 1 when a package listed in pyproject.toml is missing or older than it asks for, as after an update
that added or raised one. The launchers run this before starting and reinstall when it fails.

Besides the base dependencies, the optional extras the launchers install (``LAUNCHER_EXTRAS``) are checked too,
so raising one of their versions also brings a reinstall. An extra counts only where it is installed: its first
package is (the OCR reader, Camelot). Where it never installed, nothing about it is reported, so a computer that
can't install it isn't sent to reinstall on every start.
"""

import importlib.metadata
import re
import sys
import tomllib
from pathlib import Path

try:  # packaging comes with pip; pip also carries its own copy
    from packaging.requirements import Requirement
except ImportError:  # pragma: no cover - depends on what is installed
    try:
        from pip._vendor.packaging.requirements import Requirement
    except ImportError:
        Requirement = None

# The extras CloseDesk.bat and CloseDesk.command install when they can.
LAUNCHER_EXTRAS = ("ocr", "tables")


def applies(requirement: str) -> bool:
    """Fallback without ``packaging``: only ``sys_platform == '...'`` and ``python_version < '...'`` markers are
    used in pyproject.toml."""
    marker = requirement.partition(";")[2]
    platform = re.search(r"sys_platform\s*==\s*['\"]([^'\"]+)['\"]", marker)
    if platform is not None and platform.group(1) != sys.platform:
        return False
    below = re.search(r"python_version\s*<\s*['\"](\d+)\.(\d+)['\"]", marker)
    return below is None or sys.version_info[:2] < (int(below.group(1)), int(below.group(2)))


def _name(requirement: str) -> str:
    return re.split(r"[<>=!~;\[ ]", requirement, maxsplit=1)[0]


def problem(requirement: str) -> str:
    """Why ``requirement`` is not met here, or "" when it is."""
    if Requirement is not None:
        req = Requirement(requirement)
        if req.marker is not None and not req.marker.evaluate():
            return ""
        name = req.name
    else:
        if not applies(requirement):
            return ""
        req = None
        name = _name(requirement)
    try:
        installed = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return f"Missing: {requirement}"
    if req is not None and req.specifier and not req.specifier.contains(installed, prereleases=True):
        return f"Outdated: {name} {installed} installed, needs {req.specifier}"
    return ""


def extra_installed(requirements: list[str]) -> bool:
    """Whether an extra was installed here: its first package is (and applies to this Python)."""
    if not requirements:
        return False
    first = requirements[0]
    if Requirement is not None:
        req = Requirement(first)
        if req.marker is not None and not req.marker.evaluate():
            return False
        name = req.name
    else:
        if not applies(first):
            return False
        name = _name(first)
    try:
        importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return False
    return True


def requirements_to_check(project: dict) -> list[str]:
    """The base dependencies, then those of each launcher extra that is installed here."""
    wanted = list(project["project"]["dependencies"])
    extras = project["project"].get("optional-dependencies", {})
    for extra in LAUNCHER_EXTRAS:
        listed = extras.get(extra, [])
        if extra_installed(listed):
            wanted.extend(listed)
    return wanted


def main() -> int:
    project = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8"))
    for requirement in requirements_to_check(project):
        found = problem(requirement)
        if found:
            print(found)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
