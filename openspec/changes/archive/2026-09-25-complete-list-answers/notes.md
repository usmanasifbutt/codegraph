# Implementation notes: complete-list-answers

## Backend benchmark (task 5.1), 2026-09-25

This covers `expertise-copilot-backend` (70 Python files, 6.9k lines) and 8 structural questions whose ground truth comes from the indexer. codegraph ran on OpenRouter `openai/gpt-4o-mini`, with `uv run python -m bench.run_codegraph --repo … --name backend`. The Claude Code results are the stored run from 2026-09-25 (Sonnet 5, read-only tools), not re-run.

| | codegraph before | codegraph after | Claude Code |
|---|---|---|---|
| Correct | 6/8 (97% of items) | **8/8 (100%)** | 8/8 (100%) |
| Tokens per question | 5.3k | **5.0k** | 91.9k |
| Cost per question | $0.00105 | **$0.00099** | $0.12268 |
| Total for 8 questions | $0.0084 | **$0.0079** | $0.9815 |
| Latency per question | 8.9 s | **7.1 s** | 19.9 s |

**What fixed each miss:**
- **"Which third-party packages does this repo import?"** It now returns one row per top-level package (the distinct-list rule and the grouped example). It never reached the row cap, so the truncation repair wasn't needed on this run.
- **"Which non-test functions are async?"** gpt-4o-mini's answer still left an item out, and the deterministic completeness check appended it (`Also in the results: …`). This is exactly the case it was designed for.

The truncation repair didn't trigger on this benchmark. It's covered by unit tests and a Neo4j integration test with a row cap of 3.

## Live eval (task 3.2)

The run was `uv run pytest -m llm -s tests/test_live_eval.py` with OpenRouter gpt-4o-mini. It scored **14/14**, including the two new list questions, with an average latency of 3.6 s and an estimated $0.0085 per run.

## Deviations from the plan

- **Prompt rule 5 changed.** It used to say "add `LIMIT 50`". A `LIMIT` hides items silently, so neither the truncation repair nor the "truncated" note would fire. Rule 5 now allows `LIMIT` only for "top N / most" questions, and the test-functions example no longer has a `LIMIT`.
- **"Item first" convention.** The completeness check treats the first non-location column as the item. Two examples (full-text search, and "what does X import") returned `kind` first, so they were reordered, and the distinct-list rule now says "make the item the first column".
- **One row per importer.** The "who imports M / package P" examples return one row per importer module (`min(line)`, `count(*)`), not one row per import statement, which satisfies the new distinct-rows DB test.
- **pytest path.** pytest gets `pythonpath = ["."]` so `tests/test_bench.py` can import `bench`, which is deliberately not part of the installed package.
