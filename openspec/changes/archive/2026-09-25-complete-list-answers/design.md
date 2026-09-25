## Context

See proposal.md for why this change exists, and the `nl-to-cypher` delta spec for the required behavior. The relevant code as it stands:

- `nlq/engine.py`: `Engine.prepare` runs generate → gate → execute with a repair loop for rejected queries (`NLQ_MAX_REPAIRS`). `Engine.answer_stream` streams the LLM answer and then appends `sources_suffix(...)`, a deterministic `Sources:` line used when the answer cites no `path:line`.
- `store/readonly.py`: `Rows(columns, records, truncated)`. `truncated` is set when more than `max_rows` rows existed.
- `nlq/schema_prompt.py`: the curated schema text, the rules and about 14 few-shot examples. `answer_messages` also caps the rows JSON at about 20k characters (`ROWS_CHAR_BUDGET`).
- The benchmark scripts (`grep_vs_graph.py`, `bench_backend.py`, `bench_claude_code.py`) exist only in a session scratchpad, along with the Claude Code results for `expertise-copilot-backend`.

## Goals / Non-Goals

**Goals:**
- Close both benchmark misses using mechanisms that don't depend on the model behaving well: a structural repair, and a deterministic check after the answer is written.
- No extra LLM calls in the common case where results aren't truncated.
- A benchmark anyone can re-run from the repo.

**Non-Goals:**
- Raising `NLQ_MAX_ROWS` or the answer character budget. Aggregating is the fix; bigger payloads would raise cost.
- Changing the answer model, or adding a second "verify" LLM pass.
- Re-running Claude Code. Its answers don't depend on this change, and the stored results are reused.

## Decisions

### D1. Truncation repair lives in `Engine.prepare`, after a successful execution
```
rows = run(query)                                   # existing loop, with repairs for rejections
if rows.truncated and not truncation_repair_done:
    g = llm.generate(question, repo, feedback + [(query,
          f"results were truncated at {max_rows} rows; rewrite the query to return one row per "
          f"distinct item, aggregating occurrences with count()/collect(), or to narrow it")])
    if g.answerable:
        try:   rows2 = executor.run(g.cypher, origin="repaired")   # same gate + audit
        except (QueryRejected, QueryTimeout): rows2 = None
        if rows2 is not None and not rows2.truncated:
            query, rows = g.cypher, rows2; explanation = g.explanation
    result.repairs += 1
```
- The feedback goes through the existing `feedback` list, so the prompt format is shared with ordinary repairs.
- The attempt happens at most once and is outside `NLQ_MAX_REPAIRS`, as the spec requires.
- If the repaired query returns a different but still truncated result, the original is kept. Its query is the one the user would recognize for their question.
- *Alternative:* raise the row cap automatically. Rejected: it's unbounded, and it moves cost into the answer prompt.

### D2. Completeness suffix, a pure function next to `sources_suffix`
`completeness_suffix(answer, columns, rows) -> str`:
1. **Item column:** the first entry in `columns` that is not one of `file`, `path`, `source_file`, `line` or `start_line`, and whose values include strings. Only distinct string values up to 200 characters count, in row order. Lists, dicts and numbers are ignored.
2. **Mentioned:** `re.search(rf"(?<![\w]){re.escape(v)}(?![\w])", answer)`, or the same test on `v.rsplit('.', 1)[-1]` when `v` contains a dot and that last segment has at least 3 characters. It matches whole words, so `run` won't match inside `rerun`.
3. **Listing vs selecting:** if `mentioned / distinct < 0.6`, return `""`.
4. Otherwise it returns `\n\nAlso in the results: `a`, `b`, ...` with at most 20 values, plus ` (+N more in the results table)`.

It runs after the streamed answer and before `sources_suffix`, so the appended names also count toward which sources are relevant. In the UI it's yielded as a final chunk, the same way `Sources:` already is.
- *Why 60%:* the async miss named 9 of 10 (90%). The deliberate library selection named 3 of 17 (18%). A lost item at the end of a list usually leaves more than 80% mentioned. 60% leaves room either way and is a named constant (`LISTING_THRESHOLD`).
- *Alternative:* ask the LLM to "list every row". That's already in the prompt, and gpt-4o-mini still dropped one. A deterministic check is the only guarantee.

### D3. Prompt: a distinct-list rule and examples
- **New rule:** "For 'which X …' questions return ONE ROW PER DISTINCT X. Aggregate occurrences (`count(*) AS uses`, `collect(DISTINCT i.source_file)[..5] AS files`). Group dependencies by top-level package (`split(x.qualified_name, '.')[0]`)."
- **Replace the per-import library example** with a grouped version. The answer step still sees the imported names, as `collect(DISTINCT n)[..10]` of `i.names`, so questions like "which library connects to the LLM?" keep working.
- **Add an example** "Which third-party packages does this repo import?" that groups by package and has no `LIMIT` that would hide packages.
- Existing unit and DB tests already check that every example passes the gate and runs. A new DB test checks that the list examples return one row per distinct item on the fixture.

### D4. Benchmark moves into `bench/`
```
bench/
  __init__.py
  questions.py      ground truth from GraphBatch (the fixed importer and ground-truth logic from the session)
  scoring.py        correct() and hit rules (dotted/path prefixes; no bare-tail match for modules)
  run_codegraph.py  --repo PATH --name NAME [--model M]  -> results/<name>/codegraph.json
  run_grep_agent.py --repo PATH --name NAME              -> results/<name>/grep_agent.json
  run_claude_code.py --repo PATH --name NAME [--model sonnet] -> results/<name>/claude_code.json
  report.py         results/<name>/*.json -> markdown table (cost, tokens, correct, latency)
  pricing.py        USD per 1M tokens for the models used (explicit, dated)
  results/          git-ignored (may contain private repo names)
```
- The scripts take the repo path as an argument. The only defaults are the public fixture and this repo.
- The stored Claude Code results for the backend are copied into `bench/results/backend/` locally, and are never committed.
- `bench/` is excluded from the package build, and ruff lints it like the rest.
- *Alternative:* keep the scripts in the scratchpad. Rejected: scratchpads are per session, and SPEC M4 needs these scripts anyway.

## Risks / Trade-offs

- [The 60% heuristic misfires: a selection that happens to mention 60% or more gets padded, or a listing below 60% stays incomplete] → Both failures are mild: extra names clearly marked "Also in the results", or the same behavior as today. Unit tests cover both benchmark cases and the boundary.
- [The truncation repair costs one more call (about 3.5k tokens) on truncated questions] → It only happens when results were truncated, which is rare once list queries are distinct, and at most once per question.
- [The last-segment match treats a common word as mentioned (`run`, `get`)] → That errs toward not appending, which is the same as today's behavior. It's acceptable.
- [Benchmark numbers vary with the model's randomness] → Report a single run as before, and optionally `--repeat N` with a median. Claude Code is not re-run.
- [OpenRouter credits are exhausted] → Code and tests can be done without credits (fakes). The benchmark re-run task waits for a top-up.

## Migration Plan

This changes code only. There's no data or schema migration. Roll back with `git revert`. `bench/results/` is added to `.gitignore` in the same change.
