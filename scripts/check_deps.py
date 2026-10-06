"""Exit 1 when a package listed in pyproject.toml is missing or older than it asks for, as after an update
that added or raised one. The launchers run this before starting and reinstall when it fails."""

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


def applies(requirement: str) -> bool:
    """Fallback without ``packaging``: only ``sys_platform == '...'`` markers are used in pyproject.toml."""
    marker = requirement.partition(";")[2]
    platform = re.search(r"sys_platform\s*==\s*['\"]([^'\"]+)['\"]", marker)
    return platform is None or platform.group(1) == sys.platform


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
        name = re.split(r"[<>=!~;\[ ]", requirement, maxsplit=1)[0]
    try:
        installed = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return f"Missing: {requirement}"
    if req is not None and req.specifier and not req.specifier.contains(installed, prereleases=True):
        return f"Outdated: {name} {installed} installed, needs {req.specifier}"
    return ""


def main() -> int:
    project = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8"))
    for requirement in project["project"]["dependencies"]:
        found = problem(requirement)
        if found:
            print(found)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
