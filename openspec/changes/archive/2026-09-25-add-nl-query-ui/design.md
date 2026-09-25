## Context

`add-infra-and-indexer` has delivered the graph: `File`/`Module`/`Class`/`Function`/`Repo` nodes and `CONTAINS`/`IMPORTS`/`INHERITS` edges, a `symbol_text` full-text index, and everything scoped by `repo`. It also gave us `config.py` (env and `.env` loading, `ConfigError`, a retrying `connect`), a pipeline style where each stage is a pure function, and a compose `app` service that currently runs `sleep infinity`. See proposal.md for the motivation and the specs for the required behavior.

Constraints:
- Neo4j Community 5.26 has no role-based access control, so read-only access has to be enforced in our own code, backed by the server's access mode.
- `CALLS` and `TESTS` edges don't exist yet (they arrive in M1). The prompt has to say this, so the LLM doesn't invent them.
- The provider is OpenAI or OpenRouter (OpenAI-compatible). Speech-to-text runs locally with `faster-whisper`, following the working setup in `ai-lab/voice-notes`.
- Connecting a repo has to work both on the host (any path) and in the container, where only the `/repos` bind mount is visible and Git clones need somewhere writable to go.
- Windows host with Podman, and the same code runs on the host and in the container.

## Goals / Non-Goals

**Goals:**
- An engine you can test without an LLM or a browser (fakes stand in for both), with a thin Streamlit layer on top.
- Safety that doesn't depend on the LLM behaving well: every query, generated or edited, goes through one gate.
- A result object complete enough for M4 to reuse the engine unchanged as the text-to-Cypher baseline.

**Non-Goals:**
- Follow-up questions that depend on earlier ones: each question is answered independently, so no chat memory is sent to the LLM.
- Streaming the generated Cypher, a graph canvas, authentication, or deployment for more than one user.
- Private Git repositories, SSH URLs, deleting repositories from the UI, and indexing in the background across page reloads. Indexing runs inside the user's Streamlit session.
- Tuning prompts for every model. We target one cheap default model and keep the prompt model-agnostic.

## Decisions

### D1. Layout and seams
```
src/codegraph/
  config.py            + LLMSettings / NLQSettings / SpeechSettings / RepoSettings (same ConfigError, same .env rules)
  store/readonly.py    validate(query) -> Verdict ; run_readonly(driver, query, params, limits) -> Rows
  nlq/
    llm.py             QueryLLM protocol + LangChainQueryLLM (ChatOpenAI; base_url for OpenRouter)
    schema_prompt.py   curated schema text + rules + few-shot examples
    engine.py          ask(question, repo) -> QuestionResult   (generate -> validate -> run -> repair -> answer)
    audit.py           JSON-lines audit logger
  speech.py            build_model() ; check_audio(bytes) ; transcribe(model, bytes) -> str   (faster-whisper)
  repos.py             resolve_local(path) ; validate_git_url(url) ; clone(url, branch) ; index_repo(source, name, excludes, progress)
  ui/app.py            Streamlit page; only calls engine / speech / repos / store
  cli.py               + `codegraph ui [--port]`
```
The engine depends only on the `QueryLLM` protocol, which has `generate(...)` and `answer(...)`, and on an injected executor. Unit tests pass fakes for both, and the UI tests pass a fake engine.
- *Alternative:* call LangChain objects directly from the engine. Rejected because structured output is awkward to fake, and it would couple the tests to LangChain internals.

### D2. Our own pipeline instead of `GraphCypherQAChain`
We use `langchain-openai`'s `ChatOpenAI` for both providers (OpenRouter through `base_url="https://openrouter.ai/api/v1"`). The pipeline itself is ours:
- `generate`: `with_structured_output(GeneratedQuery, method="function_calling")`, where `GeneratedQuery` has fields `answerable: bool`, `cypher: str`, `explanation: str` and `reason: str`. Function calling works on OpenAI and on most OpenRouter models. If structured parsing fails, we fall back to taking the first fenced ```cypher block.
- `answer`: a plain chat call with the question, the Cypher and the rows serialized as JSON. It is streamed so the UI can render tokens as they arrive.

Why not `langchain-neo4j`'s `GraphCypherQAChain`: we need hooks it doesn't give us cleanly. Those are our safety gate before execution, the error-feedback repair loop, a `$repo` check, off-topic handling, origin-tagged audit logging, and a structured result. Its schema introspection would also describe the graph less precisely than the curated text in D3. M4's "text-to-Cypher only" baseline will be this engine.

### D3. Prompt: curated schema text, not introspection
`schema_prompt.py` holds a hand-written description that mirrors the `graph-schema` spec:
- each label with its properties, with meanings such as "`is_external` = third-party module stub" and "`IMPORTS.resolution` in exact/external/unresolved"
- relationship directions
- the full-text index name
- a "not available yet" note: there are no CALLS or TESTS edges and no git data

The rules section says:
- every node pattern filters on `repo: $repo`
- return `qualified_name`, `file`, `source_file` and `line` when relevant, so answers can cite locations
- only read queries
- set `answerable=false` for questions the graph can't answer

About 8 few-shot examples cover: importers of a module, subclasses (variable-length `INHERITS*1..`), methods of a class, external packages by use count, files with parse errors, full-text search, the largest modules by function count, and an off-topic question. Repair calls add the failed query and the error to the conversation.
- *Alternative:* `Neo4jGraph.get_schema` introspection. Rejected because it lists property keys without their meanings and says nothing about repo scoping. A unit test keeps the curated text in sync by checking that every label and relationship type in `NODE_KEYS` and in the writer appears in it.

### D4. Safety gate (store/readonly.py), in order
1. **Text checks, which need no database.** A small lexer strips comments and string literals, then:
   - rejects more than one statement (any `;` other than a trailing one)
   - rejects deny-listed tokens with a case-insensitive regex: `dbms.`, `apoc.`, `gds.`, `tx.`, `db.create`, `db.index.fulltext.create`, `LOAD CSV`, `USE`, `IN TRANSACTIONS`
   - lets allow-listed `db.*` read procedures through
   - checks that `$repo` appears when the caller requires it
2. **Server classification.** Run `EXPLAIN <query>` inside `session.execute_read` and require `summary.query_type == "r"`. A write, read-write or schema type, or an explain error, is rejected. Syntax and semantic errors caught here go to the repair loop.
3. **Execution.** `session.execute_read(fn)` runs `tx.run(Query(text, timeout=NLQ_TIMEOUT_SECONDS), params)` in READ access mode, so the server rejects writes as defense in depth. It pulls at most `max_rows + 1` records, then calls `result.consume()` so the server discards the rest. `truncated` is set when a record beyond `max_rows` was seen.
4. **Serialization.** Nodes become `{_labels, ...props}`, relationships become `{_type, ...props}`, paths become lists of those, and temporal values become ISO strings. Every row is JSON-safe for both the table and the answer prompt.

A timeout (`Neo.ClientError.Transaction.TransactionTimedOut*`) is reported as a timeout, not repaired. Every gate decision is written to the audit log (D6).
- *Alternative:* rewrite the query to add `LIMIT`. Rejected because it is fragile with `UNION`, `ORDER BY` and subqueries. Capping on the client side is exact and simple.

### D5. The engine loop
```
generate -> if not answerable: result(answer=reason), no query run
for attempt in 0..NLQ_MAX_REPAIRS:
    verdict = validate(cypher, require_repo=True)      (text checks, then EXPLAIN)
    if verdict.ok: rows = run_readonly(...)  -> break  (on a CypherSyntax/Semantic error: treat as failed)
    if attempt == max: return error result (last cypher, reason)
    cypher = llm.generate(question, repo, feedback=[(cypher, reason)...])   origin = "repaired"
answer = llm.answer(question, cypher, rows[:max_rows], truncated)
```
`QuestionResult` is a dataclass holding every field the `nl-to-cypher` "Question result" requirement lists, plus a `timings` dict for generation, execution and answer. Rows sent to the answer step are capped at about 20k characters of JSON. When the cap cuts rows off, the prompt says so, on top of any row-limit truncation.

### D6. Audit log
`logging.getLogger("codegraph.nlq.audit")` writes one JSON object per gate decision, with the fields `ts`, `repo`, `origin`, `outcome`, `reason`, `rows` and `query`. It goes to stderr by default, and also to a file when `NLQ_AUDIT_LOG` names a path. Settings objects are never logged, so keys and passwords can't leak through this path. A unit test checks that the key values never appear in the captured log output.

### D7. Speech: local faster-whisper (same approach as `ai-lab/voice-notes`)
- **Input (confirmed against Streamlit 1.64 docs).** `st.chat_input(..., accept_audio=True)` gives one chat box with a mic. On submit it returns a `ChatInputValue` whose `.audio` is the recorded WAV (in memory) and whose `.text` holds any typed text. `AppTest`'s `ChatInput.set_value` only accepts text, so the page reads the chat input through the injectable UI hooks (D8). UI tests replace that hook to feed fake audio, and the pure voice handling in `speech.py` is unit-tested directly.
- **Checks.** `check_audio` reads the WAV header with the stdlib `wave` module to get the duration (limit 60 s) and checks the size (limit 10 MB).
- **Model.** `build_model()` returns `faster_whisper.WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE)`, with defaults `small`, `cpu` and `int8` as in voice-notes' `transcribe.py`. The UI wraps it in `@st.cache_resource(show_spinner="Loading transcription model (first run only)...")`, so it loads once per process. Load errors are caught and stored, and the UI then disables the mic and shows the cause.
- **Transcription.** `transcribe(model, data)` passes `io.BytesIO(data)` to `model.transcribe(...)`, since faster-whisper decodes file-like objects through PyAV, and joins the segment texts. Audio is never written to disk. An empty result raises `NoSpeechError`.
- **Model cache.** It is the Hugging Face cache (`HF_HOME`). In the container `HF_HOME=/cache/huggingface` sits on the named volume `model-cache`, so the model downloads once.
- *Alternatives:* the OpenAI transcription API (rejected: you asked for local, it needs a key, and it uploads audio) and the browser Web Speech API (rejected: Chrome/Edge only, and audio goes to the browser vendor). `faster-whisper` needs no `torch`, which keeps the image smaller than `openai-whisper` would.

### D8. Streamlit page
- **Sidebar:** the repo picker (from `MATCH (r:Repo) RETURN r.name, r.status, r.indexed_at, r.source, r.root, r.source_url`, with incomplete repos marked), "Re-index" for the selected repo, the provider and model in use, the voice availability note, and "Clear conversation".
- **"Connect repository" panel:** an expander at the top, open when nothing is indexed. It has two tabs:
  - **Local folder:** a text input on the host, or a selectbox of `CODEGRAPH_REPOS_ROOT` subfolders plus a text input in the container.
  - **Git URL:** a URL field and an optional branch.

  Both share a name field, excludes, and an "Index" button. Progress uses `st.status` with one line per step (clone, parse, write), then a summary table. A replace confirmation (a checkbox plus the button) appears when the name already exists. Indexing sets `session_state.busy`, which disables the other controls.
- **Main area:** the history is rendered with `st.chat_message`. Each assistant entry shows the answer (streamed on first render, then stored) and an expander with the Cypher code, explanation and "Edit & run" (a `text_area` plus a button keyed by entry index). Below that come a `st.dataframe` of the rows, a caption with the row count, truncation, repair count and timings, and error blocks for failures.
- **Input:** `st.chat_input` for text and the audio widget for voice. A new question is processed once per submission. A `pending` entry in `session_state` guards against Streamlit reruns calling the LLM again.
- **Resources:** the Neo4j driver, the LLM clients and the Whisper model are created once with `@st.cache_resource`. If Neo4j is unreachable, the page renders an error and calls `st.stop()`. An LLM settings error only disables the question box, with the message shown, so connecting and indexing still work.
- **Markdown:** answers render as markdown with HTML disabled (Streamlit's default), so text from repo docstrings in the rows can't inject HTML.
- `codegraph ui --port N` runs `python -m streamlit run <ui/app.py> --server.port N --server.address 127.0.0.1 --server.headless true`. The container command uses `--server.address 0.0.0.0` and compose publishes `127.0.0.1:8501:8501`.

### D9. Configuration
`config.py` gains three settings groups. They use the same `.env` loading and `ConfigError`, and number fields are validated.
- `LLMSettings`: provider, model, api_key, base_url
- `NLQSettings`: max_rows, timeout_seconds, max_repairs, audit_log
- `SpeechSettings`: whisper_model, device, compute_type
- `RepoSettings`: repos_root (optional), workspace, clone_timeout

`.env.example` adds:
- LLM: `LLM_PROVIDER`, `LLM_MODEL` (default `gpt-4o-mini`, already there), `OPENROUTER_BASE_URL` (optional override)
- query limits: `NLQ_MAX_ROWS`, `NLQ_TIMEOUT_SECONDS`, `NLQ_MAX_REPAIRS`, `NLQ_AUDIT_LOG`
- speech: `WHISPER_MODEL`, `WHISPER_DEVICE`, `WHISPER_COMPUTE_TYPE`
- repos: `CODEGRAPH_REPOS_ROOT` (unset on the host), `CODEGRAPH_WORKSPACE` (default `.codegraph/clones` on the host, git-ignored), `CODEGRAPH_CLONE_TIMEOUT_SECONDS`

`OPENAI_API_KEY` is now only needed when `LLM_PROVIDER=openai`.

### D11. Repository connection and indexing (repos.py)
- **Local folders.**
  - `resolve_local(path)` calls `Path(path).expanduser().resolve(strict=True)` and requires a directory.
  - When `repos_root` is set, it also requires `resolved.is_relative_to(Path(repos_root).resolve())`. Resolving first defeats `..` and symlink escapes.
  - `list_root_folders()` returns the root's immediate subdirectories that aren't hidden.
- **Git URLs.**
  - `validate_git_url` uses `urllib.parse`. It requires scheme `https`, a hostname, no username or password, and a path. It rejects everything else before running git.
  - The clone target is `workspace/<host>/<owner>/<name>`, sanitized to `[A-Za-z0-9._-]`.
  - `clone` runs `git clone --depth 1 --single-branch [--branch B] --no-recurse-submodules -- <url> <tmpdir>` via `subprocess.run(..., timeout=clone_timeout)`.
    - The environment sets `GIT_TERMINAL_PROMPT=0` and `GIT_ASKPASS=echo`, so git fails instead of prompting.
    - It also passes `-c protocol.allow=never -c protocol.https.allow=always` (https only, including redirects).
    - `-c core.symlinks=false` is set because the indexer never follows symlinks anyway.
    - It clones into a sibling temp dir and only then renames it over the target, replacing any earlier clone. On any failure the temp dir is removed, so no partial folder remains.
  - Git's stderr is shown in errors after credential-looking substrings are removed. An auth or not-found failure maps to "only public repositories are supported".
- **Indexing.**
  - `index_repo(source, name, excludes, progress)` runs `extract` then `write_batch`, exactly the `codegraph index` path.
  - Afterwards it records `source` (`local` or `git`), `root`, and for Git `source_url` and `branch` on the `Repo` node. These are extra properties, so the `graph-schema` identity rules don't change.
  - `progress(step, detail)` feeds `st.status`.
  - Re-indexing reads those properties back from `Repo`. It re-clones Git repos and re-reads local roots. A missing root errors without touching the graph.
  - CLI-indexed repos have no `source`, so they are treated as local from `root`. In the container a host path won't exist, which triggers the "path no longer exists" message.
- **Concurrency.** A process-wide `threading.Lock` allows one indexing job at a time. A second session gets "another indexing job is running". Indexing runs in the Streamlit script thread with progress updates. Closing the tab mid-run leaves `Repo.status=indexing`, which the picker marks as incomplete.
- *Alternatives:* running a background worker or job queue (rejected: too much for a single-user local tool) and `GitPython` (rejected: the subprocess call is simpler and runs the same git binary).

### D10. Tests
- **Unit (no database, no LLM):**
  - the text-level safety rules (every deny-list entry, multiple statements, strings and comments containing those words, the allow-list)
  - the engine with a scripted `FakeQueryLLM` and a fake executor, covering the repair path, an off-topic question, never-valid output, truncation and the timings and fields
  - config parsing and missing keys
  - `check_audio` limits, using generated WAV bytes
  - `transcribe` with a stub model (joining segments, `NoSpeechError`), and the load-failure path
  - `repos.py`:
    - `resolve_local` containment (`..`, a symlink escape, a missing path, a file instead of a folder)
    - `validate_git_url` (every rejected form and accepted https forms)
    - `clone` with a local bare repo served over `file://` behind a test-only override, plus a fake failing git for cleanup and error mapping
    - the timeout path
  - schema prompt coverage (D3)
  - audit logs never containing secrets
- **Integration (`@pytest.mark.neo4j`, fixture repo indexed):**
  - rejection by `EXPLAIN` query type (`DELETE`, and a hidden `SET`)
  - READ mode rejecting a write when the gate is bypassed directly
  - the row cap and truncation flag
  - timeout: `UNWIND range(1, 10^9) AS x RETURN sum(x)` with a 1 s timeout
  - full-text search allowed
  - the whole engine with a fake LLM that returns known Cypher
  - `index_repo` writing `source`, `root`, `source_url` and `branch` on `Repo`, and re-indexing from those properties
- **Network (`@pytest.mark.network`, not run by default):** a real shallow clone of a small public GitHub repo, then indexing it.
- **Whisper (`@pytest.mark.whisper`, not run by default):** load the real `tiny` model and transcribe a bundled short WAV clip.
- **UI (`streamlit.testing.v1.AppTest`):** a fake engine and fake speech are injected through a module-level factory, testing:
  - the repo picker and the "nothing indexed" view
  - the result view
  - the error view
  - edit & run
  - clearing history
  - the voice-disabled view when model loading fails
  - the connect panel: host path, container root choices and rejection, bad Git URL, a name clash needing confirmation, the summary after indexing, re-index, and disabled controls while busy
  - the question box disabled when LLM config is missing
- **Live (`@pytest.mark.llm`, skipped without a key, not run by default):** about 10 questions about the fixture repo with expected answer sets. The generated Cypher runs, and the returned `qualified_name`s are checked against ground truth computed from `GraphBatch`. This doubles as a smoke test of the prompt.

## Risks / Trade-offs

- [The LLM writes valid but wrong Cypher, so answers are confidently wrong] → The Cypher and raw rows are always shown and can be edited. The live eval and M4 measure accuracy. Few-shot examples cover the common question shapes.
- [Prompt injection through repo text (docstrings, names) in rows sent to the answer step] → The answer step has no tools and can't run queries. Its output is shown as markdown without HTML, and it is told to answer only from the rows. Worst case, the answer text is misleading, and the rows shown next to it expose that.
- [Data leaves the machine: questions, the schema text and result rows go to OpenAI/OpenRouter] → The proposal and README document this. Audio stays local.
- [Cost and latency: 2 to 4 LLM calls per question] → The default is a cheap model and repairs are capped (2). Timings are shown for each question so slow steps are visible.
- [Local transcription is slow on CPU, and the first use downloads about 500 MB] → The `small`/`int8` default from voice-notes, a first-run spinner, and the model cached on a volume. `WHISPER_MODEL=base` or `tiny` trades accuracy for speed. The 60 s limit bounds each job.
- [A larger image: `faster-whisper` brings `ctranslate2` and `av`, plus `git`] → No `torch` is needed. It is acceptable for a local dev tool.
- [Cloning untrusted public repos] → The indexer only parses (it never imports or runs code). `git clone` runs no repo hooks. Submodules are off, symlinks are checked out as plain files, and the clone is https only with a timeout. Clones land in a dedicated workspace volume, never under `/repos` (which is read-only).
- [Clones fill the disk] → Clones are shallow, one folder per URL is replaced on re-clone, and the README explains how to clear the workspace. A size quota is out of scope.
- [Arbitrary https URLs make the server fetch from any host] → This is acceptable for a UI that listens on loopback only. The limit is documented, and it is revisited if the UI is ever exposed.
- [Indexing a big repo blocks that browser session for a while] → Progress is shown step by step and other sessions stay responsive. A background job queue is out of scope.
- [`EXPLAIN` adds a round-trip per attempt] → It is cheap (planning only, milliseconds), and correctness matters more here.
- [Streamlit reruns can repeat LLM calls] → A `pending` guard in `session_state`, and results are stored and never recomputed on rerun.
- [OpenRouter models differ in function-calling support] → A fenced-block fallback parser, and the README recommends models known to support tool calls.
- [The UI has no authentication] → It is bound to loopback on the host and in the published container port. Non-loopback binding is left out on purpose.

## Migration Plan

1. Archive `add-infra-and-indexer` first. This change's `local-infra` delta adds to that capability.
2. Add the dependencies and `git`, and rebuild the `app` image. The compose `app` command changes from `sleep infinity` to the UI, and `exec` and `run` for CLI commands keep working. Add the `workspace` and `model-cache` volumes. We recommend setting `REPO_PATH` to a parent folder.
3. To roll back, revert compose to `sleep infinity` (or `git revert` the change) and optionally remove the two volumes. No database migration is needed. The UI only adds optional `Repo` properties (`source`, `source_url`, `branch`), which older code ignores.

## Open Questions

- Which cheap default model gives the best Cypher accuracy for the cost (`gpt-4o-mini` vs newer small models)? It is only a default in `.env.example`, so the live eval in D10 can settle it later without changing the specs or tasks.
