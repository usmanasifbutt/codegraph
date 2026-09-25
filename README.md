# codegraph

Build a deterministic code graph from a Python repo and ask questions about it with an LLM agent that uses graph tools (callers, impact, tests, owners) alongside grep.

Status: **M0 (indexer) + web UI.**
- `codegraph index` parses a Python repo with the stdlib `ast` module into Neo4j: files, modules, classes and functions, with `CONTAINS`/`IMPORTS`/`INHERITS` edges.
- The web UI (`codegraph ui`) connects and indexes repositories (local folders or public Git URLs) and answers questions typed or spoken in plain language. An LLM writes a read-only Cypher query, the query runs, and the answer is grounded in the returned rows.

Call edges, git history, the tool-using agent and the benchmark come next. See [SPEC.md](SPEC.md) for the design, evaluation plan, milestones and sources.

Stack: Neo4j + LangChain for the graph and querying, Streamlit for the UI, faster-whisper for local speech-to-text, [uv](https://docs.astral.sh/uv/) for Python, `podman compose` to run Neo4j and the app.

## Prerequisites

- [Podman](https://podman.io/) 5.x with a running machine on Windows/macOS (`podman machine init`, `podman machine start`), plus a compose provider (`podman compose version` must work; it may delegate to `docker-compose` or `podman-compose`).
- [uv](https://docs.astral.sh/uv/) to run the CLI, UI and tests on the host (optional if you only use the container).
- `git` on the host, if you want to connect Git URLs from a UI running on the host. The container image includes it.

## Setup

```bash
cp .env.example .env
```

Edit `.env`:

- `NEO4J_PASSWORD`: at least 8 characters. The user must stay `neo4j`.
- `REPO_PATH`: a **parent folder** such as `D:/Projects`, so that every project under it can be connected from the UI. The CLI examples below also work with a single repository.
- The LLM settings (see [Web UI](#web-ui)). Only the web UI needs them.

`.env` is git-ignored; never commit it.

```bash
podman compose up -d
```

This starts Neo4j (browser on http://localhost:7474, Bolt on 7687, data in the `neo4j-data` volume) and the `app` container, which waits for Neo4j to be healthy and then serves the web UI on http://localhost:8501. Port 8501 is published on `127.0.0.1` only, because the UI has no authentication. `REPO_PATH` is mounted read-only at `/repos`.

> **Changing the password later:** `NEO4J_AUTH` only sets the password the first time the `neo4j-data` volume is created. If you change `NEO4J_PASSWORD` in `.env` afterwards, also change it in the database (`ALTER CURRENT USER SET PASSWORD FROM 'old' TO 'new'` in the browser), then run `podman compose up -d` so the container, and its healthcheck, pick up the new value. If you skip that step, the healthcheck keeps logging in with the old password and Neo4j locks the account (`AuthenticationRateLimit`). To start over instead, run `podman compose down -v`, which deletes the graph.

### Windows bind mounts

With `podman machine` on Windows, all of these forms of `REPO_PATH` work (verified with Podman 5.8 and the docker-compose provider):

```
REPO_PATH=D:/Projects
REPO_PATH=D:\Projects
REPO_PATH=/mnt/d/Projects
```

Git Bash rewrites arguments that look like POSIX paths (`/repos` becomes `C:/Program Files/Git/repos`). Run the container commands from PowerShell, or prefix them with `MSYS_NO_PATHCONV=1` in Git Bash.

## Web UI

```bash
uv run codegraph ui
```

That serves the UI on the host at http://localhost:8501 (add `--port N` to change it). With the stack running (`podman compose up -d`), the container serves it at the same address.

1. **Connect repository.**
   - *Local folder:* on the host, enter any path. In the container, pick a folder under `/repos` (your `REPO_PATH`); paths outside it are rejected.
   - *Git URL:* public `https://` repositories only (GitHub, GitLab, ...). Private repos, SSH URLs and URLs with credentials are rejected. The latest commit is shallow-cloned into the workspace, without submodules.

   Name the repo (the default is the folder or URL name), add exclude globs if you need them, and click **Index**. Progress and a summary (counts, parse errors, warnings) are shown. Reusing an existing name asks before replacing that graph.
2. **Pick the repository** in the sidebar. **Re-index** re-reads the folder, or re-clones the latest commit for a Git URL.
3. **Ask a question** in the chat box, or click the mic to speak. Each answer shows:
   - the answer, citing `path:line`
   - the generated Cypher, which you can edit and **Run edited query**
   - the result table
   - the row count, the number of repairs and the time taken

   Questions the graph can't answer yet, such as callers, test coverage or git history, get a clear "can't answer" instead of a guess.

### LLM configuration

| Variable | Meaning |
|---|---|
| `LLM_PROVIDER` | `openai` (default) or `openrouter` |
| `LLM_MODEL` | e.g. `gpt-4o-mini` (OpenAI) or `openai/gpt-4o-mini` (OpenRouter). Pick a model that supports tool calling |
| `OPENAI_API_KEY` / `OPENROUTER_API_KEY` | only the key for the selected provider is needed |
| `NLQ_MAX_ROWS`, `NLQ_TIMEOUT_SECONDS`, `NLQ_MAX_REPAIRS` | limits: rows returned (200), query timeout (15 s), LLM repair attempts (2) |
| `NLQ_AUDIT_LOG` | optional file that receives a copy of the query audit log |

If the key for the selected provider is missing, the UI still connects and indexes repositories, but the question box is disabled and says which variable to set.

**Safety.** Generated and edited Cypher must be a single statement that Neo4j itself classifies as read-only (`EXPLAIN`). It runs in a READ transaction with a timeout and a row cap. `dbms.*`, `apoc.*`, `LOAD CSV`, `CALL ... IN TRANSACTIONS` and non-allow-listed `db.*` procedures are refused. Every query decision (accepted, rejected, error, timeout) is written as one JSON line to the server's stderr (and to `NLQ_AUDIT_LOG`). API keys and passwords are never logged.

**Privacy.** For each question, the question, a description of the graph schema and the result rows (names, docstrings, paths, signatures) are sent to the LLM provider. Source code is not sent. Voice audio is **not** sent anywhere.

### Voice input (local Whisper)

Speech is transcribed on the server by [faster-whisper](https://github.com/SYSTRAN/faster-whisper) (the same setup as `ai-lab/voice-notes`). No API key is needed, and audio stays in memory and is never written to disk. Recordings are limited to 60 s / 10 MB.

| Variable | Default | Notes |
|---|---|---|
| `WHISPER_MODEL` | `small` | `tiny` / `base` are faster, `medium` / `large-v3` are more accurate |
| `WHISPER_DEVICE` | `cpu` | `cuda` if you have an NVIDIA GPU |
| `WHISPER_COMPUTE_TYPE` | `int8` | |

The first voice question downloads the model (about 500 MB for `small`) from Hugging Face and shows "Loading transcription model (first run only)". After that it is cached. If the download fails, the mic is disabled with the reason and typing still works.

### Where things are stored

| What | Host (`codegraph ui`) | Container |
|---|---|---|
| Git clones | `CODEGRAPH_WORKSPACE` (default `.codegraph/clones`, git-ignored) | volume `workspace` at `/workspace` |
| Whisper models | Hugging Face cache (`~/.cache/huggingface`) | volume `model-cache` at `/cache/huggingface` |

Both survive image rebuilds. To free the space, delete the folder on the host, or run `podman compose down` then `podman volume rm codegraph_workspace codegraph_model-cache` for the container.

## Index from the CLI

In the container (`/repos` is your `REPO_PATH`):

```bash
podman compose exec app codegraph index /repos/my-repo --repo-name my-repo
```

On the host (uses `NEO4J_URI=bolt://localhost:7687` from `.env`):

```bash
uv run codegraph index path/to/repo
```

Options:

- `--repo-name NAME`: the name stored on every node (default: the folder name). Several repos can share one database.
- `--exclude GLOB`: skip matching paths, for example `--exclude "migrations/**"`. You can repeat it. `.venv`, `venv`, `build`, `dist`, `node_modules`, `__pycache__`, `site-packages` and similar directories are always skipped.
- `--json`: print the summary as one JSON object on stdout. Progress goes to stderr.

Re-indexing a repo replaces its graph completely. Exit codes are 0 on success (files that fail to parse are reported but don't fail the run), 1 on runtime failure (Neo4j unreachable or lost), and 2 on usage error (bad path, missing `NEO4J_PASSWORD`). The CLI waits up to `NEO4J_CONNECT_TIMEOUT` seconds (default 60) for Neo4j.

`Repo.status` is `complete` only after a successful run. Treat `indexing` or `failed` as an incomplete graph.

## Example queries

Open http://localhost:7474 and log in as `neo4j` with your password.

```cypher
// Classes and their methods
MATCH (c:Class {repo: 'my-repo'})-[:CONTAINS]->(m:Function)
RETURN c.qualified_name, collect(m.name) LIMIT 20;

// Who imports a module (in-repo edges carry file and line)
MATCH (m:Module {repo: 'my-repo'})-[i:IMPORTS]->(t:Module {qualified_name: 'mypkg.models'})
RETURN m.qualified_name, i.source_file, i.line;

// Most-used third-party packages
MATCH (:Module {repo: 'my-repo'})-[:IMPORTS]->(x:Module {is_external: true})
RETURN split(x.qualified_name, '.')[0] AS pkg, count(*) AS uses ORDER BY uses DESC LIMIT 10;

// Subclass tree of a base class
MATCH p = (sub:Class {repo: 'my-repo'})-[:INHERITS*1..]->(:Class {qualified_name: 'mypkg.models.Base'})
RETURN sub.qualified_name, length(p);

// Full-text search over names and docstrings
CALL db.index.fulltext.queryNodes('symbol_text', 'retry') YIELD node, score
WHERE node.repo = 'my-repo' RETURN node.qualified_name, score LIMIT 10;
```

## Tests

```bash
uv run pytest
uv run ruff check
```

Unit tests need no database, and the UI tests use Streamlit's `AppTest` with fakes. Tests marked `neo4j` use `NEO4J_*` from the environment or `.env` and are skipped when Neo4j is unreachable, so start the stack first (`podman compose up -d neo4j`) to run them all. They create repos named `cgtest-*`/`cgcli-*` and delete them afterwards.

Some suites are excluded by default and run on request:

```bash
uv run pytest -m network   # real shallow clone of a public GitHub repo
uv run pytest -m whisper   # real local Whisper model (downloads "tiny")
uv run pytest -m llm       # live text-to-Cypher eval against your LLM key
```

## Layout

```
src/codegraph/
  discover.py   find .py files (default excludes, --exclude globs)
  parse.py      one file -> FileFacts (ast only, never imports the code)
  resolve.py    module names, import and base-class resolution -> GraphBatch
  pipeline.py   discover -> parse -> resolve
  store/        Neo4j schema, batched writer, read-only query gate (readonly.py)
  nlq/          text-to-Cypher: schema prompt, LLM adapter, engine, audit log
  repos.py      connect local folders / public Git URLs, index and re-index
  speech.py     local Whisper transcription
  ui/           Streamlit page (app.py) and its service layer
  cli.py        `codegraph index`, `codegraph ui`
openspec/       specs and change proposals (OpenSpec)
```
