"""Find the Python files to index under a repository root."""

from __future__ import annotations

import os
from collections.abc import Iterable
from fnmatch import fnmatchcase
from pathlib import Path

DEFAULT_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        "node_modules",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        "build",
        "dist",
        "site-packages",
    }
)


def _matches(path: str, patterns: Iterable[str]) -> bool:
    """Glob match on a relative posix path.

    `*` matches across `/` (fnmatch semantics), so `migrations/**` covers everything below
    `migrations/`. A leading `**/` also matches at the repo root. A pattern that matches a
    parent directory excludes everything inside it.
    """
    parts = path.split("/")
    candidates = ["/".join(parts[: i + 1]) for i in range(len(parts))]
    for pattern in patterns:
        variants = [pattern]
        if pattern.startswith("**/"):
            variants.append(pattern[3:])
        for variant in variants:
            if any(fnmatchcase(c, variant) for c in candidates):
                return True
    return False


def discover(root: Path, excludes: Iterable[str] = ()) -> list[str]:
    """Return sorted, forward-slash paths (relative to `root`) of the `.py` files to index."""
    excludes = list(excludes)
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        dirnames[:] = sorted(
            d for d in dirnames if d not in DEFAULT_EXCLUDED_DIRS and not (base / d).is_symlink()
        )
        for name in filenames:
            if not name.endswith(".py"):
                continue
            full = base / name
            if full.is_symlink():
                continue
            rel = full.relative_to(root).as_posix()
            if not _matches(rel, excludes):
                found.append(rel)
    return sorted(found)
