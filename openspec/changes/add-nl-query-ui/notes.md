# Implementation notes: add-nl-query-ui

## Live eval (task 8.1), 2026-09-24

The run was `uv run pytest -m llm -s tests/test_live_eval.py` against the fixture repo, using OpenRouter `openai/gpt-4o-mini` with `LLM_PROVIDER`/`LLM_MODEL` set by environment override. It asked 11 questions, whose expected answers were computed from the indexer's `GraphBatch`.

| Metric | Result |
|---|---|
| Pass rate | **10/11 (91%)**, on each of three runs |
| Average latency | 4.0 to 4.2 s per question (generation, execution and answer) |
| Tokens | about 27k in and 1.3k out for all 11 questions |
| Estimated cost | about **$0.005** per full run (gpt-4o-mini at $0.15/$0.60 per 1M tokens) |
| Repairs needed | 0 |
| Off-topic handling | "Who calls retry?" correctly answered as not available (no CALLS edges) |

**Finding: the one failing question.** The question is "Which in-repo functions or classes does mypkg.core.engine import?". The model sometimes filters with `t.is_external = false`. That property exists only on `Module` nodes, so `Class`/`Function` import targets are silently dropped. The schema prompt now states that `i.resolution = 'exact'` is the right filter, and a few-shot example covers "what does module X import". With those, the model writes the correct query in about half of the samples, even at temperature 0. Options if this matters for M4:
- (a) Write `is_external: false` on every in-repo node. This is a small `graph-schema` change and would make the naive filter correct.
- (b) Use a stronger default model.
- (c) Treat it as expected text-to-Cypher error, and let the benchmark quantify it.

**Follow-up, 2026-09-25: library questions were refused as off-topic.** On `langchain_basics`, "which library we are using to connect to LLM" got "not about the indexed code", even though the graph holds the external imports. There were two causes: no rule or example covered dependency questions, and the only off-topic example ("What's the weather in Lahore?") matches that repo's domain. The fixes:
- A generation rule that says library/dependency questions are answered from external imports.
- A few-shot example for exactly this question.
- An answer-prompt permission to interpret well-known package names with general knowledge, while claims about the repo still come from the rows. It is told to separate chat-model clients (`init_chat_model`, `ChatOpenAI`) from embeddings.
- Paths must be copied exactly from the file columns.
- The `Sources:` fallback now cites only the rows the answer mentions.

The live eval gained "Which library do we use to make HTTP requests?". It now scores **12/12** at an average of 3.4 s and an estimated $0.006 per run.

**Follow-up, 2026-09-25: "how many ENVs are required?" was refused with the wrong reason.** Refusing is correct. The graph stores no string literals, `os.getenv` names or `.env` files, and in `langchain_basics` the variables are read implicitly by libraries after `load_dotenv()`, so even CALLS edges would not find them. The reason given, "not about the indexed code", was wrong. The prompt now:
- lists configuration, environment variables and non-Python files as not available yet
- requires `reason` to name the missing data and suggest an answerable follow-up
- has an example for this exact question

It now replies that string literals and `.env` files aren't indexed and suggests "which modules import dotenv or os?", which the graph does answer. Answering this properly needs the M3 grep/read_file tools (SPEC §8, question type 5).

## Deviations from the plan

- **Safety deny-list.** Any `db.*` procedure that is not on the allow-list is rejected, not only `db.create*` / `db.index.fulltext.create*`. This is stricter than the spec's list, and every spec-allowed procedure still works.
- **Citations.** The model does not always write `path:line`. To guarantee the requirement, the engine appends a deterministic `Sources:` line built from the rows when the answer cites none of the returned locations.
- **Voice input.** It uses `st.chat_input(accept_audio=True)` (Streamlit 1.64). The page reads it through the `Services.chat_input` hook so `AppTest` can feed audio, which recorded in D7.
- **Excludes.** The exclude globs are recorded on `Repo.excludes` so that re-indexing from the UI reuses them. `codegraph index` also records `source=local` and clears any Git metadata left on that repo name.

## Manual verification (task 8.2)

- **Host UI** (`codegraph ui`, OpenRouter):
  - Typed questions answered with `path:line` citations.
  - An edited query ran as `user-edited`.
  - An edited `DETACH DELETE` was rejected with "not read-only (Neo4j classifies it as write)", the graph was unchanged, and the audit log shows both entries.
- **Container UI:**
  - `/repos` subfolders are offered.
  - `langchain_basics` was indexed from the UI, with the replace confirmation.
  - `https://github.com/pallets/itsdangerous` was cloned and indexed from the UI.
  - After `--build --force-recreate`, the clone was still present and Re-index worked, and the Whisper model loaded with `HF_HUB_OFFLINE=1`, which proves it was cached.
- **Not done by hand:** a spoken question in the browser. The in-app browser pane has no microphone. Voice is covered instead by:
  - `AppTest` (fake audio through the chat-input hook)
  - `-m whisper` (the real `tiny` model transcribes a generated WAV of "Which modules import requests?")
  - an in-container load of the real `small` model

  One spoken question through a real microphone is still worth doing.
- **Configuration note:** the user's `.env` sets `LLM_PROVIDER=openai` but only `OPENROUTER_API_KEY` is filled in. The container UI therefore shows the question box disabled with "OPENAI_API_KEY is not set". To use OpenRouter, set `LLM_PROVIDER=openrouter` and `LLM_MODEL=openai/gpt-4o-mini`.
