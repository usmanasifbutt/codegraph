## Context

The repo contains only `SPEC.md` and a README; there is no code yet. See proposal.md for the motivation. The specs (`local-infra`, `graph-schema`, `python-indexer`, `index-cli`) define the required behavior. This document covers how to build it.

Constraints that shape the design:
- Windows host, Podman (rootless, `podman machine`), no Docker.
- Neo4j **Community** edition. It has no role-based access control, and property-existence and node-key constraints are Enterprise-only; property uniqueness constraints are available.
- Later milestones (M1 call edges, M4 benchmark ground truth) will reuse the parse results, so the extraction must be usable without a database.

## Goals / Non-Goals

**Goals:**
- A pipeline where parsing and resolution are pure functions, testable without Neo4j, and the Neo4j writer is a thin layer on top.
- Indexing fast enough for repos of a few thousand files (tens of seconds, dominated by batched writes).
- A layout where M1 (CALLS/TESTS) plugs into the resolution stage without restructuring.

**Non-Goals:**
- Incremental indexing. `sha256` is stored now, but only M5 uses it.
- Atomic re-index. The `Repo.status` flag is used instead; see Decisions.
- Parsing with a Python version other than the one running the indexer.

## Decisions

### D1. Pipeline: discover → parse → resolve → write
```
discover(root, excludes) -> [relative paths]
parse(path)              -> FileFacts      # per file, pure: defs, imports, bases, parse_error
resolve(all FileFacts)   -> GraphBatch     # repo-wide: module names, symbol table, edges
write(GraphBatch, repo)  -> Summary        # Neo4j only here
```
`FileFacts` and `GraphBatch` are plain dataclasses, and `GraphBatch` holds node dicts and relationship dicts keyed by `(label, identity)`. The parse and resolve stages never touch the database, so unit tests assert on `GraphBatch` directly, and M4 can generate ground truth from the same objects.
- *Alternative:* write while walking the AST. Rejected because imports and bases cannot be resolved until every module in the repo is known.

Package layout: `src/codegraph/{cli.py, config.py, discover.py, model.py, parse.py, resolve.py, store/schema.py, store/writer.py}`.

### D2. Parser: stdlib `ast`, run under Python 3.12
- Read files with `tokenize.open` so PEP 263 encoding cookies are honored, then call `ast.parse`. If either step raises `SyntaxError`, `UnicodeDecodeError` or `ValueError`, set `parse_error` with the line number when there is one.
- `signature`, `decorators` and `bases` come from `ast.unparse` of the relevant nodes, which is deterministic and needs no source slicing.
- The container uses Python 3.12 so repos that use 3.12 syntax (type parameters, PEP 695) parse. The package requires Python 3.11 or later. On an older interpreter, newer syntax is reported as a parse error, which the specs allow.
- *Alternative:* tree-sitter. Rejected for M0 per SPEC §6. It is the upgrade path for multi-language support.

### D3. Module naming and resolution tables
- Module names follow the `__init__.py` walk-up described in the spec. After naming every file, look for colliding names; for each collision, switch the files involved to path-derived names and record a warning.
- Build a `modules: dict[qualified_name, FileFacts]` table and a per-module `bindings: dict[local_name, target]`. Bindings come from top-level classes and functions and from import statements, including aliases. For `import a.b`, bind `a` to module `a`. For `import a.b as x`, bind `x` to module `a.b`.
- Import targets follow the precedence in the spec: submodule, then top-level definition, then the module itself. Re-exports are not followed in M0, so a class re-exported through `pkg/__init__.py` resolves to `Module pkg`. M1 can add bounded re-export following in the same resolver.
- Base-class resolution: for a base written as `Name`, look up `bindings[Name]`. For `a.b.C`, resolve `a` through the bindings, then walk the remaining segments through the module table and each module's top-level definitions. Anything that does not resolve produces no edge.
- A block counts as `TYPE_CHECKING` when its test is `TYPE_CHECKING` or `<anything>.TYPE_CHECKING`.

### D4. Neo4j 5 LTS (`docker.io/library/neo4j:5.26-community`)
- 5.26 is the current 5.x long-term-support release. Pin the tag and never use `latest`.
- Schema setup uses `CREATE ... IF NOT EXISTS` for each statement:
  - composite uniqueness constraints `(n.repo, n.path)` on `File` and `(n.repo, n.qualified_name)` on `Module`, `Class` and `Function`, plus `Repo.name` unique
  - a range index on `repo` for each label, used for fast per-repo deletes
  - `CREATE FULLTEXT INDEX symbol_text IF NOT EXISTS FOR (n:Module|Class|Function) ON EACH [n.name, n.docstring]`
- APOC is not installed because no M0 query needs it.

### D5. Writer: delete, then batched `UNWIND ... CREATE`
1. `MERGE (r:Repo {name:$repo})` and set `status='indexing'` and `root`.
2. Delete the old graph for each label in batches, with `MATCH (n:Label {repo:$repo}) CALL { WITH n DETACH DELETE n } IN TRANSACTIONS OF 5000 ROWS`, run as an auto-commit query.
3. Create nodes per label with `UNWIND $rows AS row CREATE (n:Label) SET n = row`, in batches of 1000.
4. Create relationships, grouped by `(type, source label, target label)`, with `UNWIND $rows AS row MATCH (a:SrcLabel {repo:$repo, <key>:row.src}) MATCH (b:TgtLabel {repo:$repo, <key>:row.tgt}) CREATE (a)-[r:TYPE]->(b) SET r = row.props`. Both lookups hit the composite unique indexes.
5. Set `status='complete'`, `indexed_at` and `file_count`. On an exception, try to set `status='failed'`.

`CREATE` rather than `MERGE` is safe because of step 2, and it is faster. The uniqueness constraints turn any duplicate the resolver produces into a loud error instead of silent corruption.
- *Alternative:* one transaction for the whole re-index, which would be atomic. Rejected because transaction memory grows with repo size and the Community heap defaults are small. `Repo.status` gives consumers the signal they need; see the `index-cli` spec.
- *Alternative:* write to a staging repo name and then rename. Rejected because renaming means rewriting the `repo` property on every node, which costs as much as rewriting the graph.

### D6. CLI and config
- Use `typer` for the CLI, since M3 adds `ask` and more commands. Exit codes are set explicitly: 2 for usage errors, 1 for runtime errors.
- Use `python-dotenv` with `override=False`, so real environment variables win over `.env`.
- Connection retry calls `driver.verify_connectivity()` with exponential backoff, capped at `NEO4J_CONNECT_TIMEOUT` (default 60 s). This also covers `podman compose` providers that ignore `depends_on: condition: service_healthy`.
- Output for humans is plain aligned text. `--json` writes `json.dumps(summary)`, and logs go to stderr so stdout remains valid JSON.

### D7. Containers
- `Containerfile`: base image `ghcr.io/astral-sh/uv:python3.12-bookworm-slim`. Copy `pyproject.toml` and `uv.lock` first and run `uv sync --frozen --no-dev --no-install-project` so the dependency layer is cached, then copy `src/` and run `uv sync --frozen --no-dev`. Put `.venv/bin` on `PATH`.
- `compose.yaml` services:
  - `neo4j`:
    - `NEO4J_AUTH=neo4j/${NEO4J_PASSWORD}`. The initial username must be `neo4j`, and `.env.example` explains this.
    - healthcheck: `cypher-shell -u neo4j -p "$$NEO4J_PASSWORD" "RETURN 1"`, with `interval` 5 s, `retries` 30 and `start_period` 20 s
    - a named volume `neo4j-data:/data`, plus heap settings through env vars
  - `app`:
    - `depends_on: neo4j: condition: service_healthy`
    - `env_file: .env`, with `NEO4J_URI` overridden to `bolt://neo4j:7687`
    - a read-only volume `${REPO_PATH:-./}:/repos:ro`
    - `command: sleep infinity`, so both `compose run --rm app codegraph …` and `compose exec app codegraph …` work. M5 replaces this command with Streamlit.
- Read-only Neo4j user: dropped from M0 (see proposal). M3 will run generated Cypher in `session.execute_read` (the server rejects writes in READ access mode) and will reject any query whose `EXPLAIN` query type is not `r`.

### D8. Tests
- `pytest` unit tests run against a fixture repo in `tests/fixtures/sample_repo/`, built to trigger every scenario in the specs: src layout, script-name collision, relative imports, `TYPE_CHECKING`, aliased base, property setter, syntax error, a Latin-1 encoding cookie, and a `.venv` directory. These tests assert on `GraphBatch` and need no database.
- Integration tests are marked `@pytest.mark.neo4j`. They use `NEO4J_URI` from the environment, which normally points at the compose stack, and are skipped when it is unreachable. They cover schema idempotence, constraint rejection, full-text search, re-index replacement, repo isolation and determinism.
- *Alternative:* testcontainers-python. Rejected because it needs a Docker-compatible socket, which is fragile with rootless Podman on Windows.

## Risks / Trade-offs

- [Windows bind mounts: `podman machine` sees host drives as `/mnt/<drive>/…`, and path translation differs between compose providers (docker-compose vs podman-compose)] → Verify early (task group 2). The README documents the form that works, which is either `REPO_PATH=D:/Projects/x` or `/mnt/d/Projects/x`.
- [`podman compose` may delegate to a provider that ignores health conditions] → The CLI retries the connection (D6), so the stack works either way.
- [Re-indexing is not atomic, and readers can see a half-written graph] → `Repo.status` is `indexing` or `failed` until the run completes, and consumers are told to check it.
- [Module-name heuristics break on namespace packages (PEP 420, no `__init__.py`) and on `sys.path` tricks] → Those files fall back to shorter names or path names, and collisions are reported. This is acceptable for M0; revisit if the benchmark corpus is affected.
- [Parsing under Python 3.12 rejects Python 2 files and syntax newer than 3.12] → They are recorded as `parse_error` and counted in the summary, never silently dropped.
- [Composite uniqueness constraints must be supported in Community 5.26] → The first integration test checks this. If they are not supported, fall back to a single `uid` property (`repo + '::' + qualified_name`) with a plain uniqueness constraint. The specs do not change.
- [Re-exports resolve to the package module instead of the defining class] → Documented. M1's resolver extends this.

## Migration Plan

This is a greenfield project with no existing data. To roll back, run `podman compose down -v`, which drops the Neo4j volume. Nothing outside the repo and the Podman volume is touched.
