## 1. Setup and configuration

- [x] 1.1 Check the prerequisite: `add-infra-and-indexer` is archived, or archive it now. Verify that `openspec/specs/local-infra/spec.md` exists and `openspec validate add-nl-query-ui --strict` passes.
- [x] 1.2 Add dependencies `streamlit`, `langchain-core`, `langchain-openai` and `faster-whisper` with `uv add`, and load the version-matched Streamlit docs (developing-with-streamlit skill) to confirm which audio-input API the installed version provides. Verify that `uv sync` succeeds and `uv run python -c "import streamlit, langchain_openai, faster_whisper"` exits 0. Record the chosen audio API in design.md D7.
- [x] 1.3 Extend `config.py` with `LLMSettings`, `NLQSettings`, `SpeechSettings` (`WHISPER_MODEL`, `WHISPER_DEVICE`, `WHISPER_COMPUTE_TYPE`) and `RepoSettings` (`CODEGRAPH_REPOS_ROOT`, `CODEGRAPH_WORKSPACE`, `CODEGRAPH_CLONE_TIMEOUT_SECONDS`), reusing `.env` loading and `ConfigError`. Verify with unit tests for defaults, "OpenRouter selected", "Missing key" (the error names `OPENAI_API_KEY`), an unknown provider, invalid numbers, and that speech settings need no API key.
- [x] 1.4 Update `.env.example` with the new variables and comments, and add the host clone workspace (`.codegraph/`) to `.gitignore`. Verify that every variable read in `config.py` appears in `.env.example`, using a unit test that scans both, and that `git check-ignore .codegraph/clones/x` matches.

## 2. Cypher safety gate (store/readonly.py)

- [x] 2.1 Implement the text checks: a lexer that strips comments and strings, the single-statement rule, the deny-list and allow-list, and the `$repo` requirement. Verify with unit tests for "Multiple statements rejected", "Admin procedure rejected", "Full-text search allowed", deny-list words inside string literals and comments, and every deny-list entry.
- [x] 2.2 Implement server classification with `EXPLAIN` in `execute_read`, requiring `query_type == "r"`, and map syntax and semantic errors to a repairable verdict. Verify with `@pytest.mark.neo4j` tests for "Write rejected", "Hidden write rejected" and "Read query accepted".
- [x] 2.3 Implement `run_readonly`: a READ access-mode transaction, a `Query(timeout=...)`, pulling at most `max_rows + 1` records then calling `consume()`, the truncation flag, JSON-safe serialization of nodes, relationships, paths and temporal values, and a distinct timeout error. Verify with neo4j tests for "Unbounded match", "Slow query" and "Defense in depth" (calling the executor directly with a write and checking the database is unchanged), plus a unit test for serialization.
- [x] 2.4 Implement the audit logger (JSON lines to stderr, and to a file when `NLQ_AUDIT_LOG` is set) and call it at every gate decision with its origin and outcome. Verify with unit tests for "Rejected query logged", the log file, and a check that API keys and passwords from the settings never appear in captured log output.

## 3. Text-to-Cypher engine (nlq/)

- [x] 3.1 Write `schema_prompt.py`: the curated schema text (labels, properties, meanings, directions, full-text index, "no CALLS/TESTS/git yet"), the rules, and about 8 few-shot examples. Verify with a unit test that every label in `NODE_KEYS`, `Repo`, and every relationship type the writer emits appears in the prompt, and that every example query passes the text checks from 2.1.
- [x] 3.2 Implement the `QueryLLM` protocol and `LangChainQueryLLM`: `ChatOpenAI` with the OpenRouter `base_url`, structured `generate` (answerable/cypher/explanation/reason) with the fenced-block fallback, and a streaming `answer`. Verify with unit tests that build the client for both providers without network access (checking model and base URL) and that test the fallback parser on sample replies.
- [x] 3.3 Implement `engine.ask`: generate, check answerable, validate and run with a bounded repair loop, answer, and return a `QuestionResult` with its timings. Also add `run_cypher` for user-edited queries (origin `user-edited`). Verify with unit tests using `FakeQueryLLM` and a fake executor for "Syntax error repaired", "Write query never executed", "Unrelated question", "Empty result" (the answer step receives zero rows), "Truncated rows", and "Result fields".
- [x] 3.4 Write the answer prompt: grounded in the rows only, `path:line` citations, and wording for empty and truncated results, with the rows JSON capped at about 20k characters. Verify with unit tests on the rendered prompt (rows included, truncation note present when capped) and on the fake-LLM contract for "Citations".
- [x] 3.5 Add an engine integration test (`@pytest.mark.neo4j`): index the fixture repo, then run `ask` with a fake LLM that returns a known query for "which classes inherit from Base?". Verify that the rows contain `mypkg.user.User`, `mypkg.user.Admin` and `mypkg.core.engine.Service` ("Structural question", "Repo passed as parameter").

## 4. Repository connection (repos.py)

- [x] 4.1 Implement `resolve_local` (resolve strictly, require a directory, contain paths under `repos_root` when it is set) and `list_root_folders`. Verify with unit tests for "Host folder", "Container root" (including the `..` and symlink escapes and `/etc` rejected), "Missing folder", and a file instead of a folder.
- [x] 4.2 Implement `validate_git_url` (https only, a host and path, no credentials) and the sanitized workspace target path. Verify with unit tests for "Disallowed URL" (ssh, `git@`, `file://`, `http://`, embedded `user:token@`) and for accepted GitHub and GitLab https forms mapping to the expected folders.
- [x] 4.3 Implement `clone`: a shallow single-branch clone with no submodules, `GIT_TERMINAL_PROMPT=0`, the https-only protocol config, a temp dir then rename, cleanup on any failure, a timeout, stderr with secrets removed, and auth or not-found mapped to "only public repositories are supported". Verify with unit tests using a local bare repo (through a test-only protocol override) for success and re-clone replacement, and a fake failing or hanging git for "Private or missing repository", the timeout, and "no folder left behind".
- [x] 4.4 Implement `index_repo` and `reindex_repo`: `extract` then `write_batch`, a progress callback, recording `source`/`root`/`source_url`/`branch` on `Repo`, re-indexing from those properties with a missing-path check, and the process-wide indexing lock. Verify with neo4j tests for "Index a new folder", "Re-index after changes", "Git repository updated" (the local bare repo gets a new commit and is re-indexed), a missing recorded path leaving the graph unchanged, and a second concurrent `index_repo` raising "another indexing job is running".
- [x] 4.5 Add a network smoke test (`@pytest.mark.network`, excluded by default) that clones `https://github.com/pallets/itsdangerous` and indexes it ("Public GitHub repository"). Verify with `uv run pytest -m network` that it passes and the `Repo` node has `source=git`.

## 5. Voice input (speech.py, local Whisper)

- [x] 5.1 Implement `build_model()` from `SpeechSettings` (`faster_whisper.WhisperModel`, defaults `small`/`cpu`/`int8` as in `ai-lab/voice-notes/transcribe.py`), `check_audio` (WAV duration of 60 s or less, size of 10 MB or less) and `transcribe(model, data)` (in-memory `BytesIO`, joined segments, `NoSpeechError` when empty). Verify with unit tests on generated WAV bytes ("Too long", 11 MB rejected, a short clip passing), a stub model for "Silent recording" and "Transcription error", and a check that no files are created, using a watched temp dir.
- [x] 5.2 Add a real-model test (`@pytest.mark.whisper`, excluded by default): load `WHISPER_MODEL=tiny` and transcribe a bundled 2-second WAV of a spoken phrase. Verify with `uv run pytest -m whisper` that the transcript contains the expected words, and that a second `build_model()` call through the cached resource does not reload ("Second use").

## 6. Streamlit UI (ui/app.py)

- [x] 6.1 Build the page skeleton: the Neo4j startup check with an error page, LLM config errors disabling only the question box, `@st.cache_resource` resources (driver, LLM, Whisper model with the first-run spinner), and factory hooks so tests can inject fakes. Verify with `AppTest` tests for "Neo4j down", "LLM key missing" and "Start on host" (the question box and repository panel are present).
- [x] 6.2 Build the "Connect repository" panel: Local folder and Git URL tabs, the name defaulting from the folder or URL, excludes, the replace confirmation when the name exists, `st.status` progress, the summary table, auto-selecting the repo on success, disabling controls while busy, and error display. Verify with `AppTest` tests (fake `repos` service) for "Host folder", "Container root", "Disallowed URL", "Name already used", "Parse errors shown", a failed clone, and disabled controls during indexing.
- [x] 6.3 Build the sidebar repo picker (status, index time and source, incomplete marker), "Re-index", and the "Nothing indexed" prompt. Verify with `AppTest` tests for "Pick a repository", "Nothing indexed" and re-index calling the service with the recorded source.
- [x] 6.4 Implement asking and the result view: `st.chat_input`, a spinner, the `pending` rerun guard, history in `session_state`, the streamed answer, the Cypher expander with its explanation, the dataframe, the row count, truncation, repairs and timings caption, and the error view. Verify with `AppTest` tests for "Typed question", "Successful question" and "Failed question", and check that a rerun does not call the fake engine again.
- [x] 6.5 Implement "Edit & run" for each entry, which calls `run_cypher` and appends a new entry, and "Clear conversation". Verify with `AppTest` tests for "Edited query", "Edited write query" and "Clear history".
- [x] 6.6 Add voice to the page: the audio widget (the API chosen in 1.2), `check_audio`, then transcription with the cached model, the transcript shown with a "spoken" marker and then submitted, and the mic disabled with the cause when model loading failed. Verify with `AppTest` tests using a fake model for "Spoken question", "Silent recording", "Transcription error", "Too long" and "Model download fails".
- [x] 6.7 Add `codegraph ui [--port]` to the CLI, launching Streamlit on 127.0.0.1. Verify with a CLI unit test that checks the constructed command line (mocking the subprocess), then run it by hand: `uv run codegraph ui` serves the page at http://localhost:8501 ("Start on host").

## 7. Container and docs

- [x] 7.1 Update the `Containerfile` (install `git`, `EXPOSE 8501`, dependencies via `uv.lock`, `HF_HOME=/cache/huggingface`, `CODEGRAPH_WORKSPACE=/workspace`) and `compose.yaml`:
  - the `app` command runs Streamlit on 0.0.0.0:8501, with ports `127.0.0.1:8501:8501`
  - `CODEGRAPH_REPOS_ROOT=/repos`
  - named volumes `workspace:/workspace` and `model-cache:/cache/huggingface`

  Verify after `podman compose up -d --build`:
  - http://localhost:8501 loads ("UI after compose up")
  - `podman port codegraph-app-1` shows only `127.0.0.1` ("Not exposed on the network")
  - `podman compose exec app codegraph --help` and `podman compose exec app git --version` exit 0 ("CLI still available", "Git available")
  - with `REPO_PATH` set to a parent folder, its subfolders are offered in the UI ("Parent folder mounted")
- [x] 7.2 Check persistence in the container: record one voice question (the model downloads), clone one public repo from the UI, then run `podman compose up -d --build --force-recreate app`. Verify that the next voice question shows no loading message and doesn't download again ("Model survives rebuild"), and that the cloned repo can be re-indexed ("Clones survive restart").
- [x] 7.3 Update the README:
  - the UI section: starting it on the host and in compose, the connect panel, pointing `REPO_PATH` at a parent folder, and public Git URLs only
  - voice: local Whisper, the first-run download of about 500 MB, and `WHISPER_MODEL` choices
  - the LLM configuration table
  - a data-privacy note: what goes to the LLM provider, and that audio stays local
  - where the workspace and model cache live, and how to clear them
  - the audit log location

  Verify by following the README from a clean copy with a real key: the UI starts, one folder is connected and indexed, and one typed question is answered.

## 8. End-to-end verification

- [x] 8.1 Add the live eval (`@pytest.mark.llm`, skipped without a key and excluded by default): about 10 fixture-repo questions whose expected `qualified_name` sets are computed from `GraphBatch`. Verify with `uv run pytest -m llm` using a real key; record the pass rate and average latency and cost in the change notes. A pass rate below 70% is reported as a finding, not treated as a failure.
- [x] 8.2 Check by hand in the browser, both on the host and in the container:
  - connect and index `langchain_basics` from the UI
  - connect and index a public GitHub URL
  - re-index one of them
  - ask one typed question and one spoken question
  - a failing-then-repaired question, if one occurs naturally
  - run one edited query
  - attempt one write through Edit & run

  Verify that each behaves as the specs say and that the audit log shows the matching query entries.
- [x] 8.3 Run the full default suite and lint: `uv run pytest` with the stack up (no neo4j skips; the `llm`/`network`/`whisper` markers are excluded by default), `uv run ruff check`, and `openspec validate add-nl-query-ui --strict`. Verify that all pass.
