"""Scoring: an answer is correct when its text names every expected item."""

from __future__ import annotations

import re


def found(expected: str, answer: str, mode: str) -> bool:
    """Whole-word match that tolerates dotted/path prefixes (`src.pkg.mod`, `tests.test_x`).

    `short` items (classes, functions, packages) may also match by their last dotted segment;
    `qual` items (modules) never match by a bare last segment, which would be ambiguous.
    """
    if mode == "short":
        forms = {expected, expected.rsplit(".", 1)[-1]}
    else:
        forms = {expected, expected.replace(".", "/") + ".py", expected.replace(".", "/")}
    return any(re.search(rf"(?<![\w]){re.escape(f)}(?![\w])", answer) for f in forms)


def score(expected: set[str] | frozenset[str], answer: str, mode: str) -> tuple[bool, float]:
    """(fully correct, recall) for one answer."""
    if not expected:
        return True, 1.0
    hits = sum(found(e, answer, mode) for e in expected)
    recall = hits / len(expected)
    return recall == 1.0, recall
