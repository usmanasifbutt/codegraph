"""Plain data passed between the pipeline stages (see design D1).

parse -> FileFacts (one per file), resolve -> GraphBatch (whole repo), write -> Summary.
None of these types touch the database.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

# Node label -> identity property (unique together with `repo`).
NODE_KEYS: dict[str, str] = {
    "File": "path",
    "Module": "qualified_name",
    "Class": "qualified_name",
    "Function": "qualified_name",
}


@dataclass(frozen=True)
class BaseRef:
    """One base-class expression of a class definition."""

    text: str  # ast.unparse of the expression, e.g. "m.Base" or "Generic[T]"
    dotted: str | None  # "m.Base" when the expression is a (subscripted) dotted name
    line: int


@dataclass(frozen=True)
class Definition:
    """A class or function definition, named relative to its module."""

    kind: str  # "class" | "function"
    local_name: str  # dotted path inside the module, e.g. "Service.run.helper"
    name: str
    parent: str | None  # local_name of the enclosing class/function, None at module level
    start_line: int
    end_line: int
    decorators: tuple[str, ...] = ()
    docstring: str | None = None
    # class only
    bases: tuple[BaseRef, ...] = ()
    # function only
    signature: str | None = None
    is_method: bool = False
    is_async: bool = False
    is_test: bool = False


@dataclass(frozen=True)
class ImportFact:
    """One `import` or `from ... import` statement."""

    kind: str  # "import" | "from"
    module: str | None  # "a.b" for `import a.b` / `from a.b import x`; None for `from . import x`
    level: int  # number of leading dots for relative imports
    names: tuple[tuple[str, str | None], ...]  # (name, asname)
    line: int
    is_type_checking: bool = False


@dataclass
class FileFacts:
    """Everything the indexer learns from one file, before repo-wide resolution."""

    path: str  # relative to repo root, forward slashes
    loc: int
    sha256: str
    last_modified: str  # ISO-8601 UTC
    parse_error: str | None = None
    docstring: str | None = None
    definitions: list[Definition] = field(default_factory=list)
    imports: list[ImportFact] = field(default_factory=list)


@dataclass(frozen=True)
class Rel:
    """A relationship between two nodes identified by (label, key)."""

    type: str
    src_label: str
    src: str
    tgt_label: str
    tgt: str
    props: dict[str, Any]

    def sort_key(self) -> tuple:
        return (self.type, self.src_label, self.src, self.tgt_label, self.tgt, self.props["line"])


@dataclass
class GraphBatch:
    """The whole graph for one repo, ready to write."""

    repo: str
    nodes: dict[str, list[dict[str, Any]]] = field(default_factory=dict)  # label -> rows
    rels: list[Rel] = field(default_factory=list)
    parse_errors: list[dict[str, str]] = field(default_factory=list)  # {"path", "error"}
    warnings: list[str] = field(default_factory=list)

    def node_counts(self) -> dict[str, int]:
        return {label: len(self.nodes.get(label, [])) for label in NODE_KEYS}

    def rel_counts(self) -> dict[str, int]:
        counts = Counter(r.type for r in self.rels)
        return {t: counts.get(t, 0) for t in ("CONTAINS", "IMPORTS", "INHERITS")}


@dataclass
class Summary:
    repo: str
    nodes: dict[str, int]
    relationships: dict[str, int]
    parse_errors: list[dict[str, str]]
    warnings: list[str]
    elapsed_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "nodes": self.nodes,
            "relationships": self.relationships,
            "parse_errors": self.parse_errors,
            "warnings": self.warnings,
            "elapsed_seconds": self.elapsed_seconds,
        }
