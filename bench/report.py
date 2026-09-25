"""Summarise bench/results/NAME/*.json as markdown.

uv run python -m bench.report --name NAME [--per-question]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from bench.common import RESULTS, Row, load_rows

ORDER = ["codegraph", "grep_agent", "claude_code"]
LABELS = {"codegraph": "codegraph", "grep_agent": "grep agent", "claude_code": "Claude Code"}


@dataclass
class Summary:
    approach: str
    model: str
    n: int
    correct: int
    recall: float
    tokens_per_q: float
    cost_per_q: float | None
    total_cost: float | None
    secs_per_q: float


def summarise(rows: list[Row]) -> Summary:
    n = len(rows)
    costs = [r.cost_usd for r in rows]
    total = None if any(c is None for c in costs) else sum(costs)
    return Summary(
        approach=rows[0].approach, model=rows[0].model, n=n,
        correct=sum(r.ok for r in rows), recall=sum(r.recall for r in rows) / n,
        tokens_per_q=sum(r.tokens for r in rows) / n,
        cost_per_q=None if total is None else total / n, total_cost=total,
        secs_per_q=sum(r.secs for r in rows) / n,
    )  # fmt: skip


def load(folder: Path) -> dict[str, list[Row]]:
    found = {p.stem: load_rows(p) for p in folder.glob("*.json") if p.stem in ORDER}
    return {k: found[k] for k in ORDER if k in found and found[k]}


def _money(v: float | None, digits: int = 4) -> str:
    return "n/a" if v is None else f"${v:.{digits}f}"


def markdown(folder: Path, per_question: bool = False) -> str:
    data = load(folder)
    if not data:
        raise SystemExit(f"no results in {folder}")
    lines = [
        "| Approach | Model | Correct | Items found | Tokens / q | Cost / q | Total cost "
        "| Latency / q |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for rows in data.values():
        s = summarise(rows)
        lines.append(
            f"| {LABELS[s.approach]} | {s.model} | {s.correct}/{s.n} | {s.recall:.0%} | "
            f"{s.tokens_per_q / 1000:.1f}k | {_money(s.cost_per_q, 5)} | {_money(s.total_cost)} | "
            f"{s.secs_per_q:.1f}s |"
        )
    if per_question:
        approaches = list(data)
        lines += ["", "| Question | " + " | ".join(LABELS[a] for a in approaches) + " |",
                  "|---|" + "---|" * len(approaches)]  # fmt: skip
        for i, row in enumerate(next(iter(data.values()))):
            cells = []
            for a in approaches:
                r = data[a][i] if i < len(data[a]) else None
                cells.append("—" if r is None else f"{'✓' if r.ok else '✗'} {r.recall:.0%}")
            lines.append(f"| {row.question} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--name", required=True)
    ap.add_argument("--per-question", action="store_true")
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):  # ✓/✗ on Windows consoles (cp1252)
        sys.stdout.reconfigure(encoding="utf-8")
    print(markdown(RESULTS / args.name, args.per_question))


if __name__ == "__main__":
    main()
