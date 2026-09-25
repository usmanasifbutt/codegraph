"""Question -> validated read-only Cypher -> rows -> grounded answer (design D5).

The engine only sees the `QueryLLM` protocol and an `Executor`, so tests use fakes for both.
The UI calls `prepare*` and then streams `answer_stream`; other callers use `ask`/`run_cypher`.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from codegraph.config import NLQSettings
from codegraph.nlq.llm import QueryLLM
from codegraph.store.readonly import QueryRejected, QueryTimeout, Rows, guarded_run


class Executor(Protocol):
    def run(self, query: str, params: dict[str, Any], *, origin: str, repo: str) -> Rows: ...


class DriverExecutor:
    """Runs queries through the cypher-safety gate against a Neo4j driver."""

    def __init__(self, driver: Any, nlq: NLQSettings) -> None:
        self.driver, self.nlq = driver, nlq

    def run(self, query: str, params: dict[str, Any], *, origin: str, repo: str) -> Rows:
        return guarded_run(
            self.driver,
            query,
            params,
            max_rows=self.nlq.max_rows,
            timeout=self.nlq.timeout_seconds,
            origin=origin,
            repo=repo,
        )


@dataclass
class QuestionResult:
    question: str
    repo: str
    origin: str = "generated"  # or "user-edited"
    cypher: str = ""
    explanation: str = ""
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    truncated: bool = False
    answer: str = ""
    error: str | None = None
    repairs: int = 0
    answerable: bool = True
    executed: bool = False
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def elapsed(self) -> float:
        return round(sum(self.timings.values()), 3)


FILE_KEYS = ("source_file", "file", "path")
LINE_KEYS = ("line", "start_line")
MAX_SOURCES = 10


def citations(rows: list[dict[str, Any]]) -> list[str]:
    """`path:line` locations found in result rows (first file/line pair of each row)."""
    seen: list[str] = []
    for row in rows:
        path = next((row[k] for k in FILE_KEYS if isinstance(row.get(k), str) and row[k]), None)
        line = next((row[k] for k in LINE_KEYS if isinstance(row.get(k), int)), None)
        if path and line:
            cite = f"{path}:{line}"
            if cite not in seen:
                seen.append(cite)
    return seen


def _mentioned(row: dict[str, Any], answer: str) -> bool:
    """True when the answer mentions one of the row's non-location values (a name, package)."""
    skip = set(FILE_KEYS) | set(LINE_KEYS)
    for key, value in row.items():
        for item in value if isinstance(value, list) else [value]:
            if key not in skip and isinstance(item, str) and len(item) >= 3 and item in answer:
                return True
    return False


def sources_suffix(answer: str, rows: list[dict[str, Any]]) -> str:
    """Guarantee `path:line` citations: returns a Sources line if the answer cites none.

    Prefers the rows the answer talks about, so aggregate answers don't cite unrelated rows.
    """
    cites = citations(rows)
    if not cites or any(c in answer for c in cites):
        return ""
    relevant = citations([r for r in rows if _mentioned(r, answer)])
    cites = relevant or cites
    shown = ", ".join(f"`{c}`" for c in cites[:MAX_SOURCES])
    more = f" (+{len(cites) - MAX_SOURCES} more)" if len(cites) > MAX_SOURCES else ""
    return f"\n\nSources: {shown}{more}"


LOCATION_KEYS = frozenset({*FILE_KEYS, *LINE_KEYS})
LISTING_THRESHOLD = 0.6  # answers mentioning >= 60% of the items are listing, not selecting
MAX_APPENDED = 20
MAX_ITEM_CHARS = 200


def item_values(columns: list[str], rows: list[dict[str, Any]]) -> list[str]:
    """Distinct text values of the item column: the first non-location column holding text."""
    for column in columns:
        if column in LOCATION_KEYS:
            continue
        values: list[str] = []
        for row in rows:
            value = row.get(column)
            if isinstance(value, str) and value and len(value) <= MAX_ITEM_CHARS:
                if value not in values:
                    values.append(value)
        if values:
            return values
    return []


def _word(text: str, answer: str) -> bool:
    return re.search(rf"(?<![\w]){re.escape(text)}(?![\w])", answer) is not None


def is_mentioned(value: str, answer: str) -> bool:
    """The value itself, or its last dotted segment (3+ chars), appears as a whole word."""
    if _word(value, answer):
        return True
    tail = value.rsplit(".", 1)[-1]
    return "." in value and len(tail) >= 3 and _word(tail, answer)


def completeness_suffix(answer: str, columns: list[str], rows: list[dict[str, Any]]) -> str:
    """Append item values a *listing* answer left out (spec: Grounded answer, completeness)."""
    values = item_values(columns, rows)
    if not values:
        return ""
    missing = [v for v in values if not is_mentioned(v, answer)]
    if not missing or (len(values) - len(missing)) / len(values) < LISTING_THRESHOLD:
        return ""
    shown = ", ".join(f"`{v}`" for v in missing[:MAX_APPENDED])
    more = len(missing) - MAX_APPENDED
    tail = f" (+{more} more in the results table)" if more > 0 else ""
    return f"\n\nAlso in the results: {shown}{tail}"


def off_topic_answer(repo: str, reason: str) -> str:
    base = f"I can't answer that from the code graph of '{repo}'."
    return f"{base} {reason}".strip() if reason else base


class Engine:
    def __init__(self, llm: QueryLLM, executor: Executor, nlq: NLQSettings) -> None:
        self.llm, self.executor, self.nlq = llm, executor, nlq

    # -- phase 1: produce rows ------------------------------------------------------------
    def prepare(self, question: str, repo: str) -> QuestionResult:
        result = QuestionResult(question=question, repo=repo)
        feedback: list[tuple[str, str]] = []
        gen_time = exec_time = 0.0
        try:
            for attempt in range(self.nlq.max_repairs + 1):
                t0 = time.perf_counter()
                try:
                    generated = self.llm.generate(question, repo, feedback)
                finally:
                    gen_time += time.perf_counter() - t0
                if not generated.answerable:
                    result.answerable = False
                    result.answer = off_topic_answer(repo, generated.reason)
                    return result
                result.cypher = generated.cypher.strip()
                result.explanation = generated.explanation.strip()
                result.repairs = attempt
                origin = "generated" if attempt == 0 else "repaired"
                t0 = time.perf_counter()
                try:
                    rows = self.executor.run(
                        result.cypher, {"repo": repo}, origin=origin, repo=repo
                    )
                except QueryTimeout as exc:
                    result.error = f"Timed out: {exc}"
                    return result
                except QueryRejected as exc:
                    feedback.append((result.cypher, exc.reason))
                    if attempt == self.nlq.max_repairs:
                        result.error = (
                            f"No valid query after {attempt + 1} attempt(s): {exc.reason}"
                        )
                        return result
                    continue
                finally:
                    exec_time += time.perf_counter() - t0
                if rows.truncated:
                    t0 = time.perf_counter()
                    rows = self._repair_truncation(result, rows, question, repo, feedback)
                    gen_time += time.perf_counter() - t0
                self._fill(result, rows)
                return result
        except Exception as exc:  # LLM/API failures: report, never crash the caller
            result.error = f"LLM request failed: {type(exc).__name__}: {exc}"
            return result
        finally:
            result.timings.update(generate=round(gen_time, 3), execute=round(exec_time, 3))
        return result  # pragma: no cover  (loop always returns)

    def _repair_truncation(
        self,
        result: QuestionResult,
        rows: Rows,
        question: str,
        repo: str,
        feedback: list[tuple[str, str]],
    ) -> Rows:
        """One extra attempt to aggregate or narrow a truncated result (outside max_repairs).

        Keeps the original rows unless the new query runs and is not truncated.
        """
        note = (
            f"The query ran but its results were truncated at {self.nlq.max_rows} rows. Rewrite "
            "it to return ONE ROW PER DISTINCT ITEM, aggregating occurrences with count() and "
            "collect(...)[..5], or narrow it, so that the complete answer fits."
        )
        result.repairs += 1
        generated = self.llm.generate(question, repo, [*feedback, (result.cypher, note)])
        if not generated.answerable or not generated.cypher.strip():
            return rows
        cypher = generated.cypher.strip()
        try:
            repaired = self.executor.run(cypher, {"repo": repo}, origin="repaired", repo=repo)
        except (QueryRejected, QueryTimeout):
            return rows  # the gate already audited the rejection/timeout
        if repaired.truncated:
            return rows
        result.cypher = cypher
        result.explanation = generated.explanation.strip() or result.explanation
        return repaired

    def prepare_cypher(self, cypher: str, repo: str, question: str = "") -> QuestionResult:
        """A user-edited query: same gate, logged with origin `user-edited`."""
        result = QuestionResult(
            question=question or "(edited query)",
            repo=repo,
            origin="user-edited",
            cypher=cypher.strip(),
        )
        t0 = time.perf_counter()
        try:
            rows = self.executor.run(result.cypher, {"repo": repo}, origin="user-edited", repo=repo)
        except QueryTimeout as exc:
            result.error = f"Timed out: {exc}"
        except QueryRejected as exc:
            result.error = f"Query rejected: {exc.reason}"
        else:
            self._fill(result, rows)
        result.timings["execute"] = round(time.perf_counter() - t0, 3)
        return result

    @staticmethod
    def _fill(result: QuestionResult, rows: Rows) -> None:
        result.columns, result.rows, result.truncated = rows.columns, rows.records, rows.truncated
        result.executed = True

    # -- phase 2: the answer -------------------------------------------------------------
    def answer_stream(self, result: QuestionResult) -> Iterator[str]:
        """Yield answer tokens and store the full answer on `result` when done."""
        if not result.ok or not result.executed:
            if result.answer:
                yield result.answer
            return
        t0 = time.perf_counter()
        parts: list[str] = []
        try:
            for piece in self.llm.answer(
                result.question, result.cypher, result.rows, result.truncated
            ):
                parts.append(piece)
                yield piece
            # Deterministic guarantees, in order: missing list items, then citations.
            for make_suffix in (
                lambda text: completeness_suffix(text, result.columns, result.rows),
                lambda text: sources_suffix(text, result.rows),
            ):
                suffix = make_suffix("".join(parts))
                if suffix:
                    parts.append(suffix)
                    yield suffix
        except Exception as exc:
            result.error = f"Answer generation failed: {type(exc).__name__}: {exc}"
        finally:
            result.answer = "".join(parts).strip()
            result.timings["answer"] = round(time.perf_counter() - t0, 3)

    def _complete(self, result: QuestionResult) -> QuestionResult:
        for _ in self.answer_stream(result):
            pass
        return result

    def ask(self, question: str, repo: str) -> QuestionResult:
        return self._complete(self.prepare(question, repo))

    def run_cypher(self, cypher: str, repo: str, question: str = "") -> QuestionResult:
        return self._complete(self.prepare_cypher(cypher, repo, question))
