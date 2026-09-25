## Why

The code graph from `add-infra-and-indexer` can only be built from a terminal (`codegraph index`) and explored by writing Cypher by hand in the Neo4j browser. Most people who want to ask "who imports `models`?" or "which classes inherit from `Base`?" don't know Cypher, don't know the graph schema, and would rather not use a CLI. This change adds a web UI that covers the whole loop:
- **Connect a repository:** a local folder, or a public Git URL.
- **Index it:** build its graph without leaving the browser.
- **Ask about it:** type or speak a question in plain language. An LLM turns the question into a read-only Cypher query, the query runs, and the rows come back with a plain-language answer grounded in them.

The text-to-Cypher engine is also the baseline that SPEC §6 and §9 need for the M4 benchmark, so building it now gives M4 something to measure.

## What Changes

- Add a **text-to-Cypher engine**: a pipeline that works without the UI.
  - It prompts the configured LLM with a curated description of the graph schema and the selected repo, and gets back a single Cypher query plus a short explanation.
  - It checks the query is read-only and runs it with a row limit and a timeout.
  - When validation or execution fails, it feeds the error back to the LLM and retries a bounded number of times.
  - It then asks the LLM for a short answer grounded only in the returned rows, citing file and line where the rows include them.
- Add **read-only enforcement** for generated Cypher. This covers the part of SPEC §6.2 that `add-infra-and-indexer` deferred, since Neo4j Community has no role-based access control. Queries run in READ access-mode transactions and must pass a query-type check (`EXPLAIN` reports it as read-only) and a deny-list of procedures. Generated Cypher is logged.
- Add **repository connection and indexing from the UI**:
  - **Local folder:** any path on the host. In the container, any folder under the mounted `/repos` root.
  - **Public Git URL:** `https` only. The repo is shallow-cloned into a workspace on disk.
  - **Indexing:** uses the existing indexer (the same code as `codegraph index`), shows progress and a summary, and selects the repo when it finishes. You can re-index a repo later, which re-reads the folder or re-clones the latest commit.
- Add **local voice input**. The browser records audio and the server transcribes it locally with `faster-whisper`, the same approach as `ai-lab/voice-notes`: Whisper `small`, CPU, `int8`, loaded once per process. The transcript is shown and used as the question. Audio never leaves the machine and no API key is needed. The model (about 500 MB) downloads on first use and is cached.
- Add a **Streamlit web UI** (`codegraph ui`, and the `app` container's default command):
  - a "Connect repository" panel
  - a repo picker
  - a question box with a microphone button
  - a conversation history
  - for each question: the answer, the generated Cypher (with a "run edited Cypher" option), a results table, and a timing summary
- Add **LLM configuration** from environment variables. The provider is `openai` or `openrouter` (OpenRouter through its OpenAI-compatible API), with a model name, API keys, and limits for row count, query timeout and retries. Also add Whisper settings (model size, device, compute type) and repo-connection settings (repos root, clone workspace, clone timeout). `.env.example` is updated.
- **Compose:**
  - The `app` service runs the UI on port 8501, published on host loopback only.
  - The image gains `git`.
  - Named volumes hold the clone workspace and the Whisper model cache.
  - The CLI still works through `podman compose run`/`exec`.
- **Dependencies:** `streamlit`, `langchain-core`, `langchain-openai`, `faster-whisper`.
- **Out of scope:**
  - the curated-tools agent and `codegraph ask` (M3)
  - private Git repositories and credentials, SSH URLs
  - deleting a repository from the UI
  - authentication or multi-user deployment
  - graph visualisation
  - conversational follow-ups that depend on earlier questions (each question is answered on its own)

## Capabilities

### New Capabilities
- `nl-to-cypher`: turning a natural-language question into validated read-only Cypher for a chosen repo, running it with limits, repairing failed queries, and producing a grounded answer. Also covers the LLM configuration it needs.
- `cypher-safety`: the rules that guarantee generated or user-edited Cypher cannot modify the database or run unbounded: READ transactions, the query-type check, the procedure deny-list, the row cap, the timeout and query logging.
- `voice-input`: recording a spoken question in the browser, transcribing it locally with Whisper, and the limits and fallbacks involved.
- `query-ui`: the Streamlit interface:
  - connecting local folders and public Git URLs, indexing and re-indexing them
  - the repo picker
  - text and voice question entry
  - the per-question result view, including editing and re-running Cypher
  - error display and session history

### Modified Capabilities
- `local-infra`: adds three requirements:
  - the `app` service serves the web UI on port 8501, reachable from the host on loopback only
  - the image includes `git`, and named volumes persist the clone workspace and Whisper model cache
  - `/repos` is exposed as the root folder for connecting local repos

  These are ADDED deltas on a capability introduced by `add-infra-and-indexer`, so that change must be archived first.

## Impact

- **New code:**
  - `src/codegraph/nlq/` (LLM client factory, schema prompt, generation and repair loop, answer synthesis)
  - `src/codegraph/store/readonly.py` (safety checks and execution)
  - `src/codegraph/repos.py` (path validation, Git clone, index service)
  - `src/codegraph/speech.py` (faster-whisper)
  - `src/codegraph/ui/app.py`
  - a `codegraph ui` command, and tests
- **Changed:** `compose.yaml` (app port and command, volumes), `Containerfile` (`git`, dependencies, exposed port), `.env.example`, `pyproject.toml`/`uv.lock`, README.
- **External services:** calls to the OpenAI or OpenRouter API for each question (usually two LLM calls, up to about four with repairs). This costs money and sends questions, the schema description and result rows to the provider. Source code is not sent, beyond the names, docstrings, paths and signatures that appear in result rows. Audio is transcribed locally and never uploaded.
- **Network and disk:**
  - Git clones of public repos from arbitrary `https` hosts go into the workspace.
  - The one-time Whisper model download (about 500 MB) comes from Hugging Face.
  - The container image grows (`git`, `ctranslate2`, `av`).
- **Depends on:** `add-infra-and-indexer` (the graph schema and property names are the contract the prompt describes, and the UI's indexing reuses its pipeline and writer).
