"""Answer the benchmark questions with codegraph (index, then text-to-Cypher).

  uv run python -m bench.run_codegraph --repo PATH --name NAME [--dry-run] [--keep-graph]

Uses LLM_PROVIDER / LLM_MODEL / keys from the environment or .env, like the UI.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from bench.common import Row, load_or_make_questions, progress, save_rows, usage_tokens
from bench.pricing import cost_usd
from bench.scoring import score


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True, type=Path)
    ap.add_argument("--name", required=True, help="results folder name, e.g. backend")
    ap.add_argument("--dry-run", action="store_true", help="print the questions; no DB, no LLM")
    ap.add_argument("--keep-graph", action="store_true", help="leave the bench-NAME graph indexed")
    args = ap.parse_args(argv)

    qs = load_or_make_questions(args.repo, args.name)
    if args.dry_run:
        for q in qs:
            print(f"{len(q.expected):3d} expected | {q.text}")
        return

    from langchain_core.callbacks import get_usage_metadata_callback

    from codegraph.config import NLQSettings, connect, load_llm_settings, load_settings
    from codegraph.nlq.engine import DriverExecutor, Engine
    from codegraph.nlq.llm import LangChainQueryLLM
    from codegraph.pipeline import extract
    from codegraph.store.writer import write_batch

    settings = load_llm_settings()
    driver = connect(load_settings())
    nlq = NLQSettings()
    repo = f"bench-{args.name}"
    t0 = time.perf_counter()
    batch = extract(args.repo, repo)
    write_batch(driver, batch, root=str(args.repo))
    print(f"indexed {len(batch.nodes['File'])} files in {time.perf_counter() - t0:.1f}s (no LLM)")
    engine = Engine(LangChainQueryLLM(settings), DriverExecutor(driver, nlq), nlq)
    rows: list[Row] = []
    try:
        for q in qs:
            with get_usage_metadata_callback() as cb:
                t = time.perf_counter()
                result = engine.ask(q.text, repo)
                secs = time.perf_counter() - t
            tin, tout = usage_tokens(cb)
            ok, recall = score(q.expected, result.answer, q.mode)
            row = Row(
                approach="codegraph", model=settings.model, question=q.text,
                expected=len(q.expected), ok=ok, recall=recall, tokens_in=tin, tokens_out=tout,
                cost_usd=cost_usd(settings.model, tin, tout), secs=secs,
                steps=1 + result.repairs, answer=result.answer,
                extra={"cypher": result.cypher, "truncated": result.truncated,
                       "rows": len(result.rows), "error": result.error},
            )  # fmt: skip
            rows.append(row)
            progress(row)
    finally:
        if not args.keep_graph:
            with driver.session() as s:
                s.run("MATCH (n {repo: $r}) DETACH DELETE n", r=repo).consume()
        driver.close()
    print(f"saved {save_rows(args.name, 'codegraph', rows)}")


if __name__ == "__main__":
    main()
