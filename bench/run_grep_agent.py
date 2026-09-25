"""Answer the benchmark questions with a grep/read_file tool-calling agent (the usual baseline).

  uv run python -m bench.run_grep_agent --repo PATH --name NAME

This sends the repository's SOURCE CODE to the configured LLM provider; don't point it at a
private repo unless that is acceptable.
"""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path

from bench.common import Row, load_or_make_questions, progress, save_rows, usage_tokens
from bench.pricing import cost_usd
from bench.scoring import score

MAX_STEPS = 12
TOOL_CHARS = 6000
SYSTEM = (
    "You answer questions about a Python repository using the tools grep, read_file and "
    "list_files. Investigate until you are confident, then give a concise final answer that "
    "lists every matching item by name (use dotted module/qualified names where relevant, e.g. "
    "`pkg.mod` for a module in pkg/mod.py). Do not guess."
)


def make_tools(root: Path, files: list[str]):
    from langchain_core.tools import tool

    cache = {
        f: (root / f).read_text(encoding="utf-8", errors="replace").splitlines() for f in files
    }

    @tool
    def grep(pattern: str) -> str:
        """Search all Python files with a regular expression. Returns `path:line: text` lines."""
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return f"invalid regex: {exc}"
        out = [f"{f}:{i}: {ln.strip()}" for f, lines in cache.items()
               for i, ln in enumerate(lines, 1) if rx.search(ln)]  # fmt: skip
        text = "\n".join(out) or "(no matches)"
        if len(text) > TOOL_CHARS:
            return text[:TOOL_CHARS] + f"\n... truncated ({len(out)} matches)"
        return text

    @tool
    def read_file(path: str, start: int = 1, end: int = 200) -> str:
        """Read lines start..end (1-based, inclusive) of a Python file in the repo."""
        lines = cache.get(path.replace("\\", "/").removeprefix("./"))
        if lines is None:
            return f"no such file: {path}"
        end = min(end, start + 299, len(lines))
        return "\n".join(f"{i}: {lines[i - 1]}" for i in range(max(start, 1), end + 1))[:TOOL_CHARS]

    @tool
    def list_files() -> str:
        """List all Python files in the repo (relative paths)."""
        return "\n".join(files)[:TOOL_CHARS]

    return [grep, read_file, list_files]


def run_agent(chat, tools, question: str) -> tuple[str, int]:
    from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

    bound = chat.bind_tools(tools)
    by_name = {t.name: t for t in tools}
    msgs = [SystemMessage(SYSTEM), HumanMessage(question)]
    for step in range(1, MAX_STEPS + 1):
        ai = bound.invoke(msgs)
        msgs.append(ai)
        if not ai.tool_calls:
            return str(ai.content), step
        for call in ai.tool_calls:
            result = by_name[call["name"]].invoke(call["args"])
            msgs.append(ToolMessage(content=str(result), tool_call_id=call["id"]))
    msgs.append(HumanMessage("Stop using tools. Give your best final answer now."))
    return str(chat.invoke(msgs).content), MAX_STEPS + 1


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--name", required=True)
    args = ap.parse_args(argv)

    from langchain_core.callbacks import get_usage_metadata_callback

    from codegraph.config import load_llm_settings
    from codegraph.discover import discover
    from codegraph.nlq.llm import build_chat

    settings = load_llm_settings()
    chat = build_chat(settings)
    tools = make_tools(args.repo, discover(args.repo))
    rows: list[Row] = []
    for q in load_or_make_questions(args.repo, args.name):
        with get_usage_metadata_callback() as cb:
            t = time.perf_counter()
            answer, steps = run_agent(chat, tools, q.text)
            secs = time.perf_counter() - t
        tin, tout = usage_tokens(cb)
        ok, recall = score(q.expected, answer, q.mode)
        row = Row(
            approach="grep_agent", model=settings.model, question=q.text,
            expected=len(q.expected), ok=ok, recall=recall, tokens_in=tin, tokens_out=tout,
            cost_usd=cost_usd(settings.model, tin, tout), secs=secs, steps=steps, answer=answer,
        )  # fmt: skip
        rows.append(row)
        progress(row)
    print(f"saved {save_rows(args.name, 'grep_agent', rows)}")


if __name__ == "__main__":
    main()
