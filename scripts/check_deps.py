"""Exit 1 when a package listed in pyproject.toml isn't installed, as after an update that added one.
The launchers run this before starting and install what's missing."""

import importlib.metadata
import re
import sys
import tomllib
from pathlib import Path


def applies(requirement: str) -> bool:
    """Only ``sys_platform == '...'`` markers are used in pyproject.toml."""
    marker = requirement.partition(";")[2]
    platform = re.search(r"sys_platform\s*==\s*['\"]([^'\"]+)['\"]", marker)
    return platform is None or platform.group(1) == sys.platform


project = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8"))
for requirement in project["project"]["dependencies"]:
    if not applies(requirement):
        continue
    try:
        importlib.metadata.version(re.split(r"[<>=!~;\[ ]", requirement, maxsplit=1)[0])
    except importlib.metadata.PackageNotFoundError:
        print(f"Missing: {requirement}")
        sys.exit(1)
