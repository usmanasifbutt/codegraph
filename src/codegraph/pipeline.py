"""discover -> parse -> resolve, with no database access."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from codegraph.discover import discover
from codegraph.model import GraphBatch
from codegraph.parse import parse_file
from codegraph.resolve import build_graph


def extract(root: Path, repo: str, excludes: Iterable[str] = ()) -> GraphBatch:
    facts = [parse_file(root, rel) for rel in discover(root, excludes)]
    return build_graph(repo, facts)
