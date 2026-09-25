"""Shared plumbing: result folders, question files, one result-row format for every runner."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from bench.questions import Question, questions
from codegraph.pipeline import extract

RESULTS = Path(__file__).parent / "results"  # git-ignored: may name private repos' symbols


@dataclass
class Row:
    """One answered question, in the same shape for every approach."""

    approach: str
    model: str
    question: str
    expected: int
    ok: bool
    recall: float
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    cache_write: int = 0
    cost_usd: float | None = None
    secs: float = 0.0
    steps: int = 1
    answer: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out + self.cache_read + self.cache_write


def results_dir(name: str) -> Path:
    path = RESULTS / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_or_make_questions(repo: Path, name: str) -> list[Question]:
    """Questions are generated once per results folder so every approach answers the same set."""
    path = results_dir(name) / "questions.json"
    if path.exists():
        return [Question.from_dict(d) for d in json.loads(path.read_text(encoding="utf-8"))]
    qs = questions(extract(repo, f"bench-{name}"))
    path.write_text(json.dumps([q.to_dict() for q in qs], indent=1), encoding="utf-8")
    return qs


def save_rows(name: str, approach: str, rows: list[Row]) -> Path:
    path = results_dir(name) / f"{approach}.json"
    path.write_text(json.dumps([asdict(r) for r in rows], indent=1), encoding="utf-8")
    return path


def load_rows(path: Path) -> list[Row]:
    return [Row(**d) for d in json.loads(path.read_text(encoding="utf-8"))]


def usage_tokens(callback) -> tuple[int, int]:
    """(input, output) tokens from langchain's get_usage_metadata_callback()."""
    meta = callback.usage_metadata.values()
    return sum(u.get("input_tokens", 0) for u in meta), sum(u.get("output_tokens", 0) for u in meta)


def progress(row: Row) -> None:
    mark = "OK  " if row.ok else "MISS"
    cost = f"${row.cost_usd:.4f}" if row.cost_usd is not None else "   n/a"
    print(
        f"  {mark} {row.recall:4.0%} {cost} {row.tokens:7d} tok {row.secs:5.1f}s | {row.question}"
    )
    sys.stdout.flush()
