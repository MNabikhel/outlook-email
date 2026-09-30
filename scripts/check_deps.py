"""Exit 1 when a package listed in pyproject.toml isn't installed, as after an update that added one.
The launchers run this before starting and install what's missing."""

import importlib.metadata
import re
import sys
import tomllib
from pathlib import Path

project = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(encoding="utf-8"))
for requirement in project["project"]["dependencies"]:
    try:
        importlib.metadata.version(re.split(r"[<>=!~;\[ ]", requirement, maxsplit=1)[0])
    except importlib.metadata.PackageNotFoundError:
        print(f"Missing: {requirement}")
        sys.exit(1)
