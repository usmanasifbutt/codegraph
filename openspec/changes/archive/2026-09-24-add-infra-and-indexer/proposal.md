## Why

codegraph is spec-only today. Every later milestone (call edges, the git layer, the agent, the benchmark that decides whether the project goes on) needs a running Neo4j instance and a structural graph of a Python repo to query. This change delivers milestone M0 from `SPEC.md`: repeatable local infrastructure and a deterministic indexer. It also checks the riskiest setup step early, which is bind-mounting a repo from a Windows host through `podman machine`.

## What Changes

- Add `compose.yaml` (run with `podman compose up`) with two services:
  - `neo4j`: fully qualified official image, ports 7474/7687, a named `/data` volume, auth from env vars, and a healthcheck.
  - `app`: built from a `Containerfile` on a `uv` base. It waits for a healthy `neo4j`, mounts the target repo read-only at `/repos`, and reads config from `.env`.
- Add `.env.example` covering the Neo4j connection settings and placeholder model and API-key variables. `.env` stays git-ignored. SPEC §6.2 asks for a separate read-only Neo4j user, but Neo4j Community Edition has no role-based access control, so such a user would still be able to write. That item moves to M3, where read-only access will be enforced with READ-mode transactions plus a query-type check (see design.md).
- Add a `uv`-managed Python 3.11+ package `codegraph` with a CLI entry point.
- Add a Neo4j schema bootstrap that is safe to run more than once:
  - uniqueness constraints on `File.path` and on `qualified_name` for `Module`, `Class` and `Function`, all scoped by `repo`
  - a full-text index over `name` and `docstring`
- Add a Python indexer that uses the stdlib `ast` module:
  - walks a repo and creates `File`, `Module`, `Class` and `Function` nodes with the attributes listed in SPEC §5
  - creates `CONTAINS`, `IMPORTS` and `INHERITS` edges
  - gives every edge `source_file` and `line`, and gives every node `repo`
  - writes to Neo4j in batched `UNWIND` statements
- Add the CLI command `codegraph index <path>`. It indexes the repo, replacing any earlier data for that repo, and prints a summary of node and edge counts.
- Out of scope, handled by later changes: `CALLS`/`TESTS` edges (M1), the git layer (M2), the agent and `ask` (M3), the benchmark (M4), incremental indexing and the UI (M5).

## Capabilities

### New Capabilities
- `local-infra`: running Neo4j and the app with `podman compose`, including env-based config, the read-only repo mount and healthchecks.
- `graph-schema`: the Neo4j labels, relationship types, required properties, constraints and indexes the indexer writes and later tools query, including the repo scoping rules. This adds one label to SPEC §5: a `Repo` node that records index status and time.
- `python-indexer`: parsing a Python repo with `ast` into File/Module/Class/Function nodes and CONTAINS/IMPORTS/INHERITS edges, with evidence (file, line) on every edge, how import and base-class resolution works, and how syntax errors are tolerated.
- `index-cli`: the `codegraph index <path>` command, including its inputs, re-index behavior, exit codes and summary output.

### Modified Capabilities
<!-- None: openspec/specs/ is empty. -->

## Impact

- **New files:** `compose.yaml`, `Containerfile`, `.env.example`, `pyproject.toml`, `uv.lock`, `src/codegraph/…`, `tests/…`, and a README update.
- **Dependencies:** `neo4j` (official Python driver), a CLI library, and `pytest`/`testcontainers` or a compose-based Neo4j for integration tests. LangChain is not added yet; it arrives in M3.
- **Systems:** local Podman (`podman machine` on Windows) and a Neo4j Community container. No hosted services and no secrets in git.
- **Later milestones:** the node and edge property names fixed here (`qualified_name`, `repo`, `source_file`, `line`) become the contract that M1 to M3 queries depend on.
