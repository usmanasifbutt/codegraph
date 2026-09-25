"""Parse one Python file into FileFacts using the stdlib `ast` module (never executes code)."""

from __future__ import annotations

import ast
import hashlib
import io
import tokenize
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from codegraph.model import BaseRef, Definition, FileFacts, ImportFact

TEST_DIRS = frozenset({"tests", "test"})


def is_test_file(path: str) -> bool:
    p = PurePosixPath(path)
    name = p.name
    return (
        (name.startswith("test_") and name.endswith(".py"))
        or name.endswith("_test.py")
        or any(part in TEST_DIRS for part in p.parts[:-1])
    )


def is_test_function(path: str, name: str, parent: Definition | None) -> bool:
    """pytest-style discovery: `test*` at module level or in a `Test*` class, in a test file."""
    if not name.startswith("test") or not is_test_file(path):
        return False
    if parent is None:
        return True
    return parent.kind == "class" and parent.parent is None and parent.name.startswith("Test")


def _dotted(expr: ast.expr) -> str | None:
    """`a.b.C` -> "a.b.C"; `a.B[T]` -> "a.B"; anything else -> None."""
    if isinstance(expr, ast.Subscript):
        expr = expr.value
    parts: list[str] = []
    while isinstance(expr, ast.Attribute):
        parts.append(expr.attr)
        expr = expr.value
    if isinstance(expr, ast.Name):
        parts.append(expr.id)
        return ".".join(reversed(parts))
    return None


def _is_type_checking_test(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    type_params = getattr(node, "type_params", None)
    tp = f"[{', '.join(ast.unparse(t) for t in type_params)}]" if type_params else ""
    sig = f"{tp}({ast.unparse(node.args)})"
    if node.returns is not None:
        sig += f" -> {ast.unparse(node.returns)}"
    return sig


class _Collector(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self.path = path
        self.scope: list[Definition] = []  # enclosing class/function definitions
        self.type_checking = 0
        self.definitions: dict[str, Definition] = {}  # local_name -> first definition
        self.imports: list[ImportFact] = []

    # -- definitions -------------------------------------------------------------
    def _define(self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        parent = self.scope[-1] if self.scope else None
        local_name = f"{parent.local_name}.{node.name}" if parent else node.name
        common = dict(
            local_name=local_name,
            name=node.name,
            parent=parent.local_name if parent else None,
            start_line=node.lineno,
            end_line=node.end_lineno or node.lineno,
            decorators=tuple(ast.unparse(d) for d in node.decorator_list),
            docstring=ast.get_docstring(node),
        )
        if isinstance(node, ast.ClassDef):
            definition = Definition(
                kind="class",
                bases=tuple(
                    BaseRef(text=ast.unparse(b), dotted=_dotted(b), line=b.lineno)
                    for b in node.bases
                ),
                **common,
            )
        else:
            definition = Definition(
                kind="function",
                signature=_signature(node),
                is_method=parent is not None and parent.kind == "class",
                is_async=isinstance(node, ast.AsyncFunctionDef),
                is_test=is_test_function(self.path, node.name, parent),
                **common,
            )
        # Redefinitions (property setters, conditional defs) keep the first definition.
        existing = self.definitions.setdefault(local_name, definition)
        self.scope.append(existing)
        self.generic_visit(node)
        self.scope.pop()

    visit_ClassDef = _define
    visit_FunctionDef = _define
    visit_AsyncFunctionDef = _define

    # -- imports -----------------------------------------------------------------
    def visit_If(self, node: ast.If) -> None:
        if not _is_type_checking_test(node.test):
            self.generic_visit(node)
            return
        self.type_checking += 1
        for stmt in node.body:
            self.visit(stmt)
        self.type_checking -= 1
        for stmt in node.orelse:
            self.visit(stmt)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(
                ImportFact(
                    kind="import",
                    module=alias.name,
                    level=0,
                    names=((alias.name, alias.asname),),
                    line=node.lineno,
                    is_type_checking=self.type_checking > 0,
                )
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.imports.append(
            ImportFact(
                kind="from",
                module=node.module,
                level=node.level,
                names=tuple((a.name, a.asname) for a in node.names),
                line=node.lineno,
                is_type_checking=self.type_checking > 0,
            )
        )


def _error_message(exc: Exception) -> str:
    if isinstance(exc, SyntaxError):
        where = f"line {exc.lineno}" if exc.lineno else "unknown line"
        return f"SyntaxError at {where}: {exc.msg}"
    return f"{type(exc).__name__}: {exc}"


def parse_file(root: Path, rel_path: str) -> FileFacts:
    full = root / rel_path
    data = full.read_bytes()
    mtime = datetime.fromtimestamp(full.stat().st_mtime, tz=UTC)
    facts = FileFacts(
        path=rel_path,
        loc=len(data.splitlines()),
        sha256=hashlib.sha256(data).hexdigest(),
        last_modified=mtime.isoformat().replace("+00:00", "Z"),
    )
    try:
        # Same decoding as tokenize.open(): honors PEP 263 cookies and BOMs.
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        source = data.decode(encoding)
        tree = ast.parse(source, filename=rel_path)
    except (SyntaxError, UnicodeDecodeError, ValueError, LookupError) as exc:
        facts.parse_error = _error_message(exc)
        return facts

    collector = _Collector(rel_path)
    collector.visit(tree)
    facts.docstring = ast.get_docstring(tree)
    facts.definitions = list(collector.definitions.values())
    facts.imports = collector.imports
    return facts
