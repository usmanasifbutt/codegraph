## Why

In the Claude Code benchmark (`expertise-copilot-backend`, 8 structural questions), codegraph scored 6/8 while Claude Code scored 8/8. Both misses were incomplete **lists**, even though every missing item was in the graph:

1. **"Which third-party packages does this repo import?"** The generated query returned one row per import statement, not one row per package. That hit the 200-row cap, and the answer step's character budget then cut it further, so `typer` and `weaviate` never reached the answer model.
2. **"Which non-test functions are async?"** The query returned all 10 functions, but gpt-4o-mini listed only 9 and dropped `_get_health_db`.

Both are failures of the LLM steps, not of the graph. Closing them should bring codegraph level with Claude Code on this benchmark while keeping its cost at about 1/100th.

## What Changes

- **Distinct list queries.** Prompt rules and few-shot examples so that "which X…" questions return one row per distinct item: `DISTINCT` on the item, aggregating occurrences with `count()`/`collect()`, and grouping by top-level package for dependency questions.
- **Truncated-result repair.** When a query's results are cut off at `NLQ_MAX_ROWS`, the engine asks the LLM once to rewrite the query to aggregate or narrow it before writing the answer. If the rewritten query is still truncated, or fails, the engine answers from the original partial rows and says so, as today.
- **Deterministic completeness check.** After the answer is written, the engine takes the rows' item column and appends any values the answer text doesn't mention. The same approach already guarantees the `Sources:` line.
- **Benchmark in the repo.** The grep/Claude Code benchmark scripts move from a session scratchpad into `bench/`, so they can be re-run. The backend benchmark is re-run to measure the effect. Results for private repositories are git-ignored and never committed.
- No schema, indexer, UI-layout or safety changes.

## Capabilities

### New Capabilities
<!-- none: the benchmark scripts are developer tooling with no product-facing behavior -->

### Modified Capabilities
- `nl-to-cypher`:
  - "Grounded answer" gains the completeness guarantee for list answers.
  - A new requirement covers repairing truncated results.
  - A new requirement covers distinct-item list queries.

## Impact

- **Code:** `src/codegraph/nlq/schema_prompt.py` (rules and examples), `src/codegraph/nlq/engine.py` (truncation repair, completeness suffix), unit and live-eval tests, and the new `bench/` scripts.
- **Cost:** at most one extra generation call, and only for questions whose results were truncated. The completeness check uses no LLM.
- **Prerequisite:** re-running the benchmark needs OpenRouter credits (the key is out of credits), and the Claude Code side needs the logged-in `claude` CLI. The earlier Claude Code results can be reused, because Claude Code itself doesn't change.
- **Privacy:** `bench/` defaults to public or local repos. Paths to private repos come from command-line arguments, and their question and result files go to a git-ignored folder.
