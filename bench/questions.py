"""Structural questions with exact ground truth computed from the indexer's GraphBatch.

Questions are chosen deterministically from the repo (the class with the most subclasses, the
module with the most importers, ...), so the same commit always yields the same questions.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import dataclass

from codegraph.model import GraphBatch


@dataclass(frozen=True)
class Question:
    text: str
    expected: frozenset[str]
    mode: str  # "short": names may match by last segment; "qual": dotted module names

    def to_dict(self) -> dict:
        return {"question": self.text, "expected": sorted(self.expected), "mode": self.mode}

    @classmethod
    def from_dict(cls, d: dict) -> Question:
        return cls(d["question"], frozenset(d["expected"]), d["mode"])


def _defining_module(target: str, in_repo: set[str]) -> str | None:
    """`from M import X` points at X; attribute the import to the module that defines X."""
    mod = target
    while mod and mod not in in_repo:
        mod = mod.rpartition(".")[0]
    return mod or None


def questions(batch: GraphBatch) -> list[Question]:
    rels = batch.rels
    inherits: dict[str, set[str]] = defaultdict(set)
    for r in rels:
        if r.type == "INHERITS":
            inherits[r.tgt].add(r.src)

    def transitive(base: str) -> set[str]:
        out, todo = set(), [base]
        while todo:
            for sub in inherits.get(todo.pop(), ()):
                if sub not in out:
                    out.add(sub)
                    todo.append(sub)
        return out

    in_repo = {m["qualified_name"] for m in batch.nodes["Module"] if not m["is_external"]}
    importers: dict[str, set[str]] = defaultdict(set)
    ext_users: dict[str, set[str]] = defaultdict(set)
    for r in rels:
        if r.type != "IMPORTS":
            continue
        if r.props.get("resolution") == "external":
            ext_users[r.tgt.split(".")[0]].add(r.src)
        mod = _defining_module(r.tgt, in_repo)
        if mod and mod != r.src:
            importers[mod].add(r.src)

    fns = {f["qualified_name"]: f for f in batch.nodes["Function"]}
    methods: dict[str, set[str]] = defaultdict(set)
    for r in rels:
        if r.type == "CONTAINS" and r.src_label == "Class" and r.tgt in fns:
            if fns[r.tgt]["is_method"]:
                methods[r.src].add(fns[r.tgt]["name"])

    qs: list[Question] = []
    if inherits:
        base = max(inherits, key=lambda b: (len(transitive(b)), b))
        name = base.rsplit(".", 1)[-1]
        qs.append(
            Question(
                f"Which classes directly subclass `{name}` ({base})?",
                frozenset(inherits[base]),
                "short",
            )
        )
        qs.append(
            Question(
                f"Which classes inherit from `{name}` directly or indirectly?",
                frozenset(transitive(base)),
                "short",
            )
        )
    if importers:
        mod = max(importers, key=lambda m: (len(importers[m]), m))
        qs.append(
            Question(f"Which modules import the module `{mod}`?", frozenset(importers[mod]), "qual")
        )
    if methods:
        cls = max(methods, key=lambda c: (len(methods[c]), c))
        qs.append(
            Question(
                f"List the methods defined in class `{cls}`.", frozenset(methods[cls]), "short"
            )
        )
    stdlib = set(sys.stdlib_module_names)
    third = {p for p in ext_users if p not in stdlib and not p.startswith("_")}
    qs.append(
        Question(
            "Which third-party (non-standard-library) packages does this repo import?",
            frozenset(third),
            "short",
        )
    )
    if third:
        pkg = max(third, key=lambda p: (len(ext_users[p]), p))
        qs.append(
            Question(
                f"Which modules import the `{pkg}` package?", frozenset(ext_users[pkg]), "qual"
            )
        )
    tests = {fns[q]["name"] for q, f in fns.items() if f["is_test"]}
    if tests:
        qs.append(
            Question(
                "How many test functions are there? List their names.", frozenset(tests), "short"
            )
        )
    async_fns = {fns[q]["name"] for q, f in fns.items() if f["is_async"] and not f["is_test"]}
    if async_fns and len(async_fns) <= 40:
        qs.append(Question("Which non-test functions are async?", frozenset(async_fns), "short"))
    return qs
