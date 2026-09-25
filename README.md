# codegraph

Build a deterministic code graph from a Python repo and ask questions about it with an LLM agent that uses graph tools (callers, impact, tests, owners) alongside grep.

Status: **M0 (infra and indexer)**. `codegraph index` parses a Python repo with the stdlib `ast` module into Neo4j (files, modules, classes, functions, `CONTAINS`/`IMPORTS`/`INHERITS`). Call edges, git history, the agent and the benchmark come next. See [SPEC.md](SPEC.md) for the design, evaluation plan, milestones and sources.

Stack: Neo4j + LangChain for the graph and querying, [uv](https://docs.astral.sh/uv/) for Python, `podman compose` to run Neo4j and the app.

## Prerequisites

- [Podman](https://podman.io/) 5.x with a running machine on Windows/macOS (`podman machine init`, `podman machine start`), plus a compose provider (`podman compose version` must work; it may delegate to `docker-compose` or `podman-compose`).
- [uv](https://docs.astral.sh/uv/) to run the CLI and tests on the host (optional if you only use the container).

## Setup

```bash
cp .env.example .env
```

Edit `.env`: set `NEO4J_PASSWORD` (at least 8 characters; the user must stay `neo4j`) and `REPO_PATH` to the repository you want to index. `.env` is git-ignored; never commit it.

```bash
podman compose up -d
```

> **Changing the password later:** `NEO4J_AUTH` only sets the password the first time the `neo4j-data` volume is created. If you change `NEO4J_PASSWORD` in `.env` afterwards, also change it in the database (`ALTER CURRENT USER SET PASSWORD FROM 'old' TO 'new'` in the browser), then run `podman compose up -d` so the container, and its healthcheck, pick up the new value. If you skip that step, the healthcheck keeps logging in with the old password and Neo4j locks the account (`AuthenticationRateLimit`). To start over instead, run `podman compose down -v`, which deletes the graph.

This starts Neo4j (browser on http://localhost:7474, Bolt on 7687, data in the `neo4j-data` volume) and the `app` container, which waits for Neo4j to be healthy. `REPO_PATH` is mounted read-only at `/repos`.

### Windows bind mounts

With `podman machine` on Windows, all of these forms of `REPO_PATH` work (verified with Podman 5.8 and the docker-compose provider):

```
REPO_PATH=D:/Projects/my-repo
REPO_PATH=D:\Projects\my-repo
REPO_PATH=/mnt/d/Projects/my-repo
```

Git Bash rewrites arguments that look like POSIX paths (`/repos` becomes `C:/Program Files/Git/repos`). Run the container commands from PowerShell, or prefix them with `MSYS_NO_PATHCONV=1` in Git Bash.

## Index a repository

In the container (indexes whatever `REPO_PATH` points at):

```bash
podman compose run --rm app codegraph index /repos --repo-name my-repo
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

Unit tests need no database. Tests marked `neo4j` use `NEO4J_*` from the environment or `.env` and are skipped when Neo4j is unreachable, so start the stack first (`podman compose up -d neo4j`) to run them all. They create repos named `cgtest-*`/`cgcli-*` and delete them afterwards.

## Layout

```
src/codegraph/
  discover.py   find .py files (default excludes, --exclude globs)
  parse.py      one file -> FileFacts (ast only, never imports the code)
  resolve.py    module names, import and base-class resolution -> GraphBatch
  pipeline.py   discover -> parse -> resolve
  store/        Neo4j schema (constraints, indexes) and batched writer
  cli.py        `codegraph index`
openspec/       specs and change proposals (OpenSpec)
```
