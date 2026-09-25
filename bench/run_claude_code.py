"""Answer the benchmark questions with Claude Code headless, restricted to read-only tools.

  uv run python -m bench.run_claude_code --repo PATH --name NAME [--model sonnet]

Needs a logged-in `claude` CLI. Cost is Claude Code's own `total_cost_usd` (API-equivalent;
on a Pro/Max plan it counts against usage limits instead of billing).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

from bench.common import Row, load_or_make_questions, progress, save_rows
from bench.scoring import score

READ_ONLY = ["Read", "Grep", "Glob"]
DENY = ["Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "Task", "Agent"]
SUFFIX = (
    "\n\nAnswer from this repository's Python code only. List every matching item by name "
    "(dotted module names for modules, e.g. app.api.routes)."
)


def ask(repo: Path, question: str, model: str) -> dict:
    cmd = ["claude", "-p", question + SUFFIX, "--output-format", "json", "--model", model,
           "--allowedTools", *READ_ONLY, "--disallowedTools", *DENY]  # fmt: skip
    t = time.perf_counter()
    proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, encoding="utf-8",
                          timeout=900, check=False)  # fmt: skip
    data = json.loads(proc.stdout or "{}")
    data["_secs"] = time.perf_counter() - t
    return data


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--name", required=True)
    ap.add_argument("--model", default="sonnet")
    args = ap.parse_args(argv)
    rows: list[Row] = []
    for q in load_or_make_questions(args.repo, args.name):
        data = ask(args.repo, q.text, args.model)
        if data.get("is_error") or "result" not in data:
            raise SystemExit(f"claude failed: {data.get('result') or 'no output'}")
        usage = data.get("usage", {})
        answer = data["result"]
        ok, recall = score(q.expected, answer, q.mode)
        row = Row(
            approach="claude_code", model=",".join(data.get("modelUsage", {})) or args.model,
            question=q.text, expected=len(q.expected), ok=ok, recall=recall,
            tokens_in=usage.get("input_tokens", 0), tokens_out=usage.get("output_tokens", 0),
            cache_read=usage.get("cache_read_input_tokens", 0),
            cache_write=usage.get("cache_creation_input_tokens", 0),
            cost_usd=data.get("total_cost_usd"), secs=data["_secs"],
            steps=data.get("num_turns") or 1, answer=answer,
        )  # fmt: skip
        rows.append(row)
        progress(row)
    print(f"saved {save_rows(args.name, 'claude_code', rows)}")


if __name__ == "__main__":
    main()
