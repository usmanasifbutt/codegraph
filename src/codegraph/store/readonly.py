"""The only way generated or user-edited Cypher reaches Neo4j (design D4).

Order: text checks (no DB) -> EXPLAIN query-type check -> READ-mode execution with a
timeout and a client-side row cap. Every decision is audited.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from neo4j import Driver, ManagedTransaction, unit_of_work
from neo4j.exceptions import ClientError, Neo4jError
from neo4j.graph import Node, Path, Relationship

from codegraph.nlq.audit import audit

# Procedures under `db.` that stay allowed; any other `db.` call is rejected.
ALLOWED_DB_PROCEDURES = frozenset(
    {
        "db.index.fulltext.querynodes",
        "db.labels",
        "db.relationshiptypes",
        "db.propertykeys",
        "db.schema.visualization",
    }
)

# (pattern on comment/string-stripped text, human reason)
DENY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bdbms\s*\.", re.I), "dbms.* procedures are not allowed"),
    (re.compile(r"\bapoc\s*\.", re.I), "apoc.* procedures are not allowed"),
    (re.compile(r"\bgds\s*\.", re.I), "gds.* procedures are not allowed"),
    # `tx` is also a plausible variable name, so only procedure-call forms count.
    (re.compile(r"\bcall\s+tx\s*\.|\btx\s*\.\s*\w+\s*\(", re.I), "tx.* procedures are not allowed"),
    (re.compile(r"\bdb\s*\.\s*create", re.I), "db.create* procedures are not allowed"),
    (
        re.compile(r"\bdb\s*\.\s*index\s*\.\s*fulltext\s*\.\s*create", re.I),
        "db.index.fulltext.create* is not allowed",
    ),
    (re.compile(r"\bload\s+csv\b", re.I), "LOAD CSV is not allowed"),
    (re.compile(r"(^|\{)\s*use\b", re.I), "USE clauses are not allowed"),
    (re.compile(r"\bin\s+transactions\b", re.I), "CALL ... IN TRANSACTIONS is not allowed"),
]
# `db.x(...)` anywhere, or `CALL db.x` (a standalone CALL may omit the parentheses).
DB_CALL = re.compile(r"\bdb\s*((?:\.\s*\w+\s*)+)\(|\bcall\s+db\s*((?:\.\s*\w+)+)", re.I)
REPO_PARAM = re.compile(r"\$(repo\b|`repo`)")

TIMEOUT_CODES = ("Neo.ClientError.Transaction.TransactionTimedOut",)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str | None = None
    repairable: bool = False  # True when the LLM could fix it (syntax/semantic errors)


@dataclass
class Rows:
    columns: list[str]
    records: list[dict[str, Any]]
    truncated: bool


class QueryRejected(Exception):
    def __init__(self, reason: str, repairable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.repairable = repairable


class QueryTimeout(Exception):
    pass


# -- 1. text checks ---------------------------------------------------------------
def strip_comments_and_strings(query: str) -> str:
    """Replace string literals with '' and drop comments; backtick names keep their text."""
    out: list[str] = []
    i, n = 0, len(query)
    while i < n:
        c = query[i]
        if c in ("'", '"'):
            quote, i = c, i + 1
            while i < n and query[i] != quote:
                i += 2 if query[i] == "\\" else 1
            out.append("''")
            i += 1
        elif c == "`":  # quoted identifier: keep its text so `apoc.x` is still seen
            j = query.find("`", i + 1)
            j = n if j == -1 else j
            out.append(query[i + 1 : j])
            i = j + 1
        elif query.startswith("//", i):
            j = query.find("\n", i)
            i = n if j == -1 else j
        elif query.startswith("/*", i):
            j = query.find("*/", i + 2)
            i = n if j == -1 else j + 2
            out.append(" ")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def check_text(query: str, require_repo: bool = True) -> Verdict:
    if not query or not query.strip():
        return Verdict(False, "empty query", repairable=True)
    code = strip_comments_and_strings(query).strip()
    code = code.rstrip(";").rstrip()
    if ";" in code:
        return Verdict(False, "only a single statement is allowed")
    for pattern, reason in DENY_PATTERNS:
        if pattern.search(code):
            return Verdict(False, reason)
    for m in DB_CALL.finditer(code):
        name = "db" + re.sub(r"\s+", "", m.group(1) or m.group(2)).lower()
        if name not in ALLOWED_DB_PROCEDURES:
            return Verdict(False, f"procedure {name} is not allowed")
    if require_repo and not REPO_PARAM.search(code):
        return Verdict(False, "query must filter by the $repo parameter", repairable=True)
    return Verdict(True)


# -- 2. server classification --------------------------------------------------------
def _explain(tx: ManagedTransaction, query: str, params: dict[str, Any]) -> str:
    return tx.run(f"EXPLAIN {query}", params).consume().query_type


def classify(driver: Driver, query: str, params: dict[str, Any]) -> Verdict:
    """Ask Neo4j to plan the query and require the read-only query type 'r'."""
    try:
        with driver.session() as session:
            query_type = session.execute_read(_explain, query, params)
    except ClientError as exc:
        return Verdict(False, f"{exc.code}: {exc.message}", repairable=True)
    if query_type != "r":
        kind = {"rw": "read-write", "w": "write", "s": "schema"}.get(query_type, query_type)
        return Verdict(False, f"not read-only (Neo4j classifies it as {kind})")
    return Verdict(True)


# -- 3. execution --------------------------------------------------------------------
def serialize(value: Any) -> Any:
    """Make driver values JSON-safe for tables and prompts."""
    if isinstance(value, Node):
        return {"_labels": sorted(value.labels), **{k: serialize(v) for k, v in value.items()}}
    if isinstance(value, Relationship):
        return {"_type": value.type, **{k: serialize(v) for k, v in value.items()}}
    if isinstance(value, Path):
        return {
            "nodes": [serialize(n) for n in value.nodes],
            "relationships": [serialize(r) for r in value.relationships],
        }
    if isinstance(value, dict):
        return {k: serialize(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [serialize(v) for v in value]
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if hasattr(value, "iso_format"):  # neo4j.time types
        return value.iso_format()
    if isinstance(value, bytes):
        return value.hex()
    return str(value)


def _fetch(max_rows: int, timeout: float):
    @unit_of_work(timeout=timeout)
    def work(tx: ManagedTransaction, query: str, params: dict[str, Any]) -> Rows:
        result = tx.run(query, params)
        columns = list(result.keys())
        records: list[dict[str, Any]] = []
        truncated = False
        for record in result:
            if len(records) >= max_rows:
                truncated = True
                break
            records.append({k: serialize(record[k]) for k in columns})
        result.consume()  # discard anything beyond the cap server-side
        return Rows(columns, records, truncated)

    return work


def run_readonly(
    driver: Driver, query: str, params: dict[str, Any], *, max_rows: int, timeout: float
) -> Rows:
    """Execute in a READ access-mode transaction; the server rejects any write."""
    try:
        with driver.session() as session:
            return session.execute_read(_fetch(max_rows, timeout), query, params)
    except Neo4jError as exc:
        code = exc.code or ""
        if code.startswith(TIMEOUT_CODES):
            raise QueryTimeout(f"query exceeded {timeout:g}s and was stopped") from exc
        if code == "Neo.ClientError.Statement.AccessMode":
            raise QueryRejected("not read-only (rejected by Neo4j in READ mode)") from exc
        raise QueryRejected(
            f"{code}: {exc.message}", repairable=code.startswith("Neo.ClientError.Statement.")
        ) from exc


# -- the gate ------------------------------------------------------------------------
def guarded_run(
    driver: Driver,
    query: str,
    params: dict[str, Any],
    *,
    max_rows: int,
    timeout: float,
    origin: str,
    repo: str | None,
    require_repo: bool = True,
) -> Rows:
    """Validate, execute and audit one query. Raises QueryRejected / QueryTimeout."""
    verdict = check_text(query, require_repo=require_repo)
    if verdict.ok:
        verdict = classify(driver, query, params)
    if not verdict.ok:
        audit(repo=repo, origin=origin, outcome="rejected", query=query, reason=verdict.reason)
        raise QueryRejected(verdict.reason or "rejected", repairable=verdict.repairable)
    try:
        rows = run_readonly(driver, query, params, max_rows=max_rows, timeout=timeout)
    except QueryTimeout as exc:
        audit(repo=repo, origin=origin, outcome="timeout", query=query, reason=str(exc))
        raise
    except QueryRejected as exc:
        audit(repo=repo, origin=origin, outcome="error", query=query, reason=exc.reason)
        raise
    audit(repo=repo, origin=origin, outcome="accepted", query=query, rows=len(rows.records))
    return rows
