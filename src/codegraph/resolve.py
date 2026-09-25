"""Repo-wide resolution: module names, import targets, base classes -> GraphBatch.

Pure: takes the FileFacts of every discovered file, returns nodes and relationships.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from codegraph.model import NODE_KEYS, Definition, FileFacts, GraphBatch, ImportFact, Rel


# -- module naming ------------------------------------------------------------------
def _walk_up_name(path: str, all_paths: set[str]) -> str:
    parts = PurePosixPath(path).parts
    dirs, stem = list(parts[:-1]), PurePosixPath(path).stem
    i = len(dirs)
    while i > 0 and "/".join(dirs[:i]) + "/__init__.py" in all_paths:
        i -= 1
    names = dirs[i:] if stem == "__init__" else [*dirs[i:], stem]
    return ".".join(names) or "__init__"


def _path_name(path: str) -> str:
    p = path[: -len(".py")] if path.endswith(".py") else path
    if p.endswith("/__init__"):
        p = p[: -len("/__init__")]
    return p.replace("/", ".")


def module_names(paths: Iterable[str], all_paths: set[str]) -> tuple[dict[str, str], list[str]]:
    """Map file path -> dotted module name, falling back to path names on collisions."""
    paths = sorted(paths)
    names = {p: _walk_up_name(p, all_paths) for p in paths}
    by_name: dict[str, list[str]] = defaultdict(list)
    for p, n in names.items():
        by_name[n].append(p)
    warnings = []
    for name, colliding in sorted(by_name.items()):
        if len(colliding) > 1:
            for p in colliding:
                names[p] = _path_name(p)
            warnings.append(
                f"module name collision '{name}' for {', '.join(colliding)}; using path-based names"
            )
    return names, warnings


# -- resolution ---------------------------------------------------------------------
@dataclass
class _Module:
    qn: str
    facts: FileFacts
    is_package: bool
    top_defs: dict[str, Definition] = field(default_factory=dict)  # name -> top-level def
    bindings: dict[str, str] = field(default_factory=dict)  # local name -> dotted target


class _Resolver:
    def __init__(self, repo: str, facts: list[FileFacts]) -> None:
        self.repo = repo
        self.batch = GraphBatch(repo=repo)
        self.parsed = [f for f in facts if f.parse_error is None]
        names, warnings = module_names(
            (f.path for f in self.parsed), all_paths={f.path for f in facts}
        )
        self.batch.warnings.extend(warnings)
        self.modules: dict[str, _Module] = {}
        for f in self.parsed:
            qn = names[f.path]
            mod = _Module(qn=qn, facts=f, is_package=f.path.endswith("__init__.py"))
            mod.top_defs = {d.name: d for d in f.definitions if d.parent is None}
            self.modules[qn] = mod
        self.top_level = {qn.split(".")[0] for qn in self.modules}
        self.class_qns = {
            f"{m.qn}.{d.local_name}"
            for m in self.modules.values()
            for d in m.facts.definitions
            if d.kind == "class"
        }
        self.stub_modules: dict[str, bool] = {}  # external/unresolved module qn -> is_external
        self.imports: dict[tuple, dict[str, Any]] = {}  # merged IMPORTS edges
        self.rels: list[Rel] = []

    # helpers ---------------------------------------------------------------------
    def _rel(
        self,
        type_: str,
        src: tuple[str, str],
        tgt: tuple[str, str],
        path: str,
        line: int,
        **props: Any,
    ) -> None:
        props = {"source_file": path, "line": line, **props}
        self.rels.append(Rel(type_, src[0], src[1], tgt[0], tgt[1], props))

    def _relative_base(self, mod: _Module, level: int) -> str | None:
        parts = mod.qn.split(".")
        pkg = parts if mod.is_package else parts[:-1]
        drop = level - 1
        if drop >= len(pkg):
            return None
        return ".".join(pkg[: len(pkg) - drop])

    def _stub(self, qn: str, *, external: bool) -> tuple[str, str]:
        self.stub_modules.setdefault(qn, external)
        return ("Module", qn)

    def _absolute_module_target(self, name: str) -> tuple[tuple[str, str], str]:
        if name in self.modules:
            return ("Module", name), "exact"
        if name.split(".")[0] in self.top_level:
            return self._stub(name, external=False), "unresolved"
        return self._stub(name, external=True), "external"

    def _add_import(
        self,
        mod: _Module,
        imp: ImportFact,
        target: tuple[str, str],
        resolution: str,
        name: str,
        alias: str | None,
    ) -> None:
        key = (mod.qn, target, imp.line)
        edge = self.imports.get(key)
        if edge is None:
            self.imports[key] = {
                "names": [name],
                "aliases": [alias],
                "resolution": resolution,
                "is_type_checking": imp.is_type_checking,
                "path": mod.facts.path,
            }
        else:
            edge["names"].append(name)
            edge["aliases"].append(alias)

    # imports -----------------------------------------------------------------------
    def resolve_imports(self, mod: _Module) -> None:
        for imp in mod.facts.imports:
            if imp.kind == "import":
                name, asname = imp.names[0]
                target, resolution = self._absolute_module_target(name)
                self._add_import(mod, imp, target, resolution, name, asname)
                head = name.split(".")[0]
                if asname:
                    mod.bindings[asname] = name
                else:
                    mod.bindings[head] = head
                continue

            # from ... import ...
            if imp.level:
                base = self._relative_base(mod, imp.level)
                dots = "." * imp.level
                if base is None:
                    source = f"{dots}{imp.module or ''}"
                    for n, a in imp.names:
                        target = self._stub(source, external=False)
                        self._add_import(mod, imp, target, "unresolved", n, a)
                    continue
                source = f"{base}.{imp.module}" if imp.module else base
                in_repo_prefix = True
            else:
                source = imp.module or ""
                in_repo_prefix = source.split(".")[0] in self.top_level

            for n, a in imp.names:
                if n != "*":
                    mod.bindings[a or n] = f"{source}.{n}"
                if source in self.modules:
                    sub = f"{source}.{n}"
                    top = self.modules[source].top_defs.get(n)
                    if n != "*" and sub in self.modules:
                        target = ("Module", sub)
                    elif n != "*" and top is not None:
                        label = "Class" if top.kind == "class" else "Function"
                        target = (label, f"{source}.{n}")
                    else:
                        target = ("Module", source)
                    self._add_import(mod, imp, target, "exact", n, a)
                elif in_repo_prefix:
                    self._add_import(
                        mod, imp, self._stub(source, external=False), "unresolved", n, a
                    )
                else:
                    self._add_import(mod, imp, self._stub(source, external=True), "external", n, a)

    # inheritance -------------------------------------------------------------------
    def resolve_bases(self, mod: _Module) -> None:
        for d in mod.facts.definitions:
            if d.kind != "class":
                continue
            src = ("Class", f"{mod.qn}.{d.local_name}")
            for base in d.bases:
                if base.dotted is None:
                    continue
                target = self._resolve_class(mod, base.dotted)
                if target is not None:
                    self._rel("INHERITS", src, ("Class", target), mod.facts.path, base.line)

    def _resolve_class(self, mod: _Module, dotted: str) -> str | None:
        head, _, rest = dotted.partition(".")
        if head in mod.top_defs and f"{mod.qn}.{dotted}" in self.class_qns:
            return f"{mod.qn}.{dotted}"
        if head in mod.bindings:
            candidate = mod.bindings[head] + (f".{rest}" if rest else "")
            if candidate in self.class_qns:
                return candidate
        return None

    # nodes -------------------------------------------------------------------------
    def emit(self) -> GraphBatch:
        repo = self.repo
        batch = self.batch
        rows: dict[str, dict[str, dict[str, Any]]] = {label: {} for label in NODE_KEYS}

        def add(label: str, row: dict[str, Any]) -> bool:
            key = row[NODE_KEYS[label]]
            if key in rows[label]:
                batch.warnings.append(f"duplicate {label} '{key}' in {row.get('file')}; kept first")
                return False
            rows[label][key] = {k: v for k, v in row.items() if v is not None} | {"repo": repo}
            return True

        for f in self._all_facts:
            add(
                "File",
                {
                    "path": f.path,
                    "language": "python",
                    "loc": f.loc,
                    "last_modified": f.last_modified,
                    "sha256": f.sha256,
                    "parse_error": f.parse_error,
                },
            )
            if f.parse_error:
                batch.parse_errors.append({"path": f.path, "error": f.parse_error})

        for mod in self.modules.values():
            f = mod.facts
            add(
                "Module",
                {
                    "qualified_name": mod.qn,
                    "name": mod.qn.split(".")[-1],
                    "file": f.path,
                    "is_external": False,
                    "is_package": mod.is_package,
                    "docstring": f.docstring,
                },
            )
            self._rel("CONTAINS", ("File", f.path), ("Module", mod.qn), f.path, 1)
            for d in f.definitions:
                qn = f"{mod.qn}.{d.local_name}"
                label = "Class" if d.kind == "class" else "Function"
                common = {
                    "qualified_name": qn,
                    "name": d.name,
                    "file": f.path,
                    "start_line": d.start_line,
                    "end_line": d.end_line,
                    "decorators": list(d.decorators),
                    "docstring": d.docstring,
                }
                if d.kind == "class":
                    row = common | {"bases": [b.text for b in d.bases]}
                else:
                    row = common | {
                        "signature": d.signature,
                        "is_method": d.is_method,
                        "is_async": d.is_async,
                        "is_test": d.is_test,
                    }
                if not add(label, row):
                    continue
                if d.parent is None:
                    parent = ("Module", mod.qn)
                else:
                    parent_def = next(x for x in f.definitions if x.local_name == d.parent)
                    parent_label = "Class" if parent_def.kind == "class" else "Function"
                    parent = (parent_label, f"{mod.qn}.{d.parent}")
                self._rel("CONTAINS", parent, (label, qn), f.path, d.start_line)

        for qn, external in self.stub_modules.items():
            add(
                "Module",
                {
                    "qualified_name": qn,
                    "name": qn.split(".")[-1],
                    "is_external": external,
                    "is_package": False,
                },
            )

        for (src_qn, target, line), e in self.imports.items():
            aliases = [a for a in e["aliases"] if a]
            props: dict[str, Any] = {
                "names": e["names"],
                "resolution": e["resolution"],
                "is_type_checking": e["is_type_checking"],
            }
            if len(e["names"]) == 1 and aliases:
                props["alias"] = aliases[0]
            self._rel("IMPORTS", ("Module", src_qn), target, e["path"], line, **props)

        # Drop edges whose endpoints were deduplicated away; sort everything.
        batch.nodes = {label: [by_key[k] for k in sorted(by_key)] for label, by_key in rows.items()}
        keys = {label: set(by_key) for label, by_key in rows.items()}
        batch.rels = sorted(
            (r for r in self.rels if r.src in keys[r.src_label] and r.tgt in keys[r.tgt_label]),
            key=Rel.sort_key,
        )
        batch.parse_errors.sort(key=lambda e: e["path"])
        return batch

    def run(self, all_facts: list[FileFacts]) -> GraphBatch:
        self._all_facts = sorted(all_facts, key=lambda f: f.path)
        for qn in sorted(self.modules):
            self.resolve_imports(self.modules[qn])
        for qn in sorted(self.modules):
            self.resolve_bases(self.modules[qn])
        return self.emit()


def build_graph(repo: str, facts: list[FileFacts]) -> GraphBatch:
    """Resolve the FileFacts of a whole repository into a GraphBatch."""
    return _Resolver(repo, facts).run(facts)
