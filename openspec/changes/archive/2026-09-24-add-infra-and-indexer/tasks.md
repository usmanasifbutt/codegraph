## 1. Project scaffold

- [x] 1.1 Create the `uv` project (`pyproject.toml`, requires-python >=3.11, `src/codegraph/` package with empty `cli.py`, `config.py`, `discover.py`, `model.py`, `parse.py`, `resolve.py`, `store/schema.py`, `store/writer.py`), declaring a `codegraph` console script. Verify with `uv run codegraph --help`, which should exit 0.
- [x] 1.2 Add dependencies `neo4j`, `typer` and `python-dotenv`, plus dev dependencies `pytest` and `ruff`, and commit `uv.lock`. Verify that `uv sync` succeeds and `uv run pytest` runs (0 tests is fine).
- [x] 1.3 Add `.env.example` with `NEO4J_PASSWORD`, `NEO4J_URI`, `NEO4J_USER=neo4j`, `NEO4J_CONNECT_TIMEOUT`, `REPO_PATH`, heap settings and placeholder model/API-key variables, with comments (the user must be `neo4j`; the password needs at least 8 characters). Verify `.env` is still git-ignored with `git check-ignore .env`.

## 2. Local infrastructure (Podman)

- [x] 2.1 Write `compose.yaml` with the `neo4j` service (pinned `docker.io/library/neo4j:5.26-community`, ports 7474/7687, `neo4j-data` volume, `NEO4J_AUTH` from env, `cypher-shell` healthcheck). Verify that after `podman compose up -d neo4j`, `podman inspect` reports it `healthy` and the browser loads at http://localhost:7474.
- [x] 2.2 Write the `Containerfile` (uv Python 3.12 base, cached dependency layer, `.venv/bin` on `PATH`) and add the `app` service (`depends_on` healthy, `env_file`, `NEO4J_URI=bolt://neo4j:7687`, `${REPO_PATH}:/repos:ro`, `sleep infinity`). Verify that `podman compose run --rm app codegraph --help` exits 0.
- [x] 2.3 Check the Windows bind mount: set `REPO_PATH` to a Windows path on the D: drive and run `podman compose run --rm app ls /repos` to confirm the files are listed, then `podman compose run --rm app touch /repos/x` to confirm it fails as read-only. If the `D:/…` form doesn't work, try `/mnt/d/…`. Record the working form in the README.
- [x] 2.4 Check persistence: create a node, run `podman compose down` and `podman compose up -d`, and confirm the node is still there with `cypher-shell`.

## 3. Discovery and parsing (pure, no Neo4j)

- [x] 3.1 Build the fixture repo `tests/fixtures/sample_repo/`, which must include:
  - a src layout package
  - `scripts/run.py` and `tools/run.py` (colliding script names)
  - relative imports, a `TYPE_CHECKING` import and an external import
  - an aliased imported base class and an external base class
  - a property getter/setter and a nested function
  - tests under `tests/` and a `test_`-named function outside them
  - a file with a syntax error on line 3 and a file with a Latin-1 encoding cookie
  - a `.venv/` directory containing `.py` files

  Verify that the files are in place and that `broken.py` fails `python -m py_compile`.
- [x] 3.2 Implement `model.py` dataclasses (`FileFacts`, `Definition`, `ImportFact`, `GraphBatch`, `Summary`). Verify with `ruff check`, which should report nothing.
- [x] 3.3 Implement `discover.py` (default excluded directories, `--exclude` globs, no symlink following, sorted relative forward-slash paths). Verify with unit tests for the "Virtualenv skipped" and "User exclude" scenarios.
- [x] 3.4 Implement `parse.py`:
  - read with `tokenize.open`, then `ast.parse`
  - collect definitions with qualified name, lines, `signature`/`decorators`/`bases` via `ast.unparse`, docstring and `is_async`
  - collect imports with line and `TYPE_CHECKING` detection
  - compute `loc`, `sha256` and `last_modified`, and record `parse_error` on failure

  Verify with unit tests for "Method and nested function", "Property getter and setter", "Syntax error in one file" and the encoding-cookie file.
- [x] 3.5 Implement test detection (`is_test` rules). Verify with unit tests for "Pytest function" and "Test-named function outside tests".

## 4. Resolution (pure, no Neo4j)

- [x] 4.1 Implement module naming (the `__init__.py` walk-up, `is_package`, collision fallback plus a warning). Verify with unit tests for "src layout" and "Colliding script names".
- [x] 4.2 Build the module table and per-module bindings, then emit `File`/`Module`/`Class`/`Function` nodes and `CONTAINS` edges, all with `repo`, `source_file` and `line`. Verify with a unit test that every node has `repo` and every relationship has `source_file` and a positive `line`.
- [x] 4.3 Implement `IMPORTS` resolution (relative imports; submodule, then definition, then module precedence; external and unresolved targets; `names`, `alias`, `is_type_checking`). Verify with unit tests for "Relative import to sibling module", "Third-party import", "Type-checking import" and an unresolvable relative import.
- [x] 4.4 Implement `INHERITS` resolution (`Name` and dotted `Attribute` bases through the bindings). Verify with unit tests for "Imported base class" and "External base class".
- [x] 4.5 Make the output deterministic (sorted nodes and relationships, no set-ordering leaks). Verify with a unit test that runs discover, parse and resolve twice on the fixture and compares the two `GraphBatch` results for equality.

## 5. Neo4j schema and writer

- [x] 5.1 Implement `config.py`: load `.env` without overriding real environment variables, validate required variables (a missing one is a usage error), and create the driver with bounded exponential-backoff connectivity retry. Verify with unit tests for env precedence and the missing-password error, using a fake driver for the retry.
- [x] 5.2 Implement `store/schema.py` (the composite uniqueness constraints, per-label `repo` range indexes, the `Repo.name` constraint and the `symbol_text` full-text index, all with `IF NOT EXISTS`). Verify with `@pytest.mark.neo4j` integration tests for "Setup twice" and "Duplicate rejected". If Community rejects composite constraints, switch to the `uid` fallback from the design.
- [x] 5.3 Implement `store/writer.py`:
  - `Repo` status set to `indexing`
  - batched per-label `DETACH DELETE` in transactions
  - `UNWIND` node creates in batches of 1000
  - relationship creates grouped by (type, source label, target label)
  - on completion, status `complete` with `indexed_at` and `file_count`; on an exception, a best-effort `failed`

  Verify with integration tests for "Deleted function disappears", "Other repos untouched", "Same module name in two repos", "No cross-repo edges" and "Search by docstring word".
- [x] 5.4 Add an integration test for determinism: index the fixture twice and compare sorted exports of nodes and relationships, ignoring `Repo.indexed_at`. Verify that it passes.

## 6. CLI

- [x] 6.1 Implement `codegraph index <path> [--repo-name] [--exclude ...] [--json]`:
  - validate the path before connecting (exit code 2)
  - ensure the schema exists, then run the pipeline and the writer
  - map errors to exit codes 0/1/2

  Verify with `typer.testing.CliRunner` tests for "Nonexistent path", "Missing password" and "Custom repository name".
- [x] 6.2 Implement the human-readable and `--json` summaries (counts per label and relationship type, parse errors with paths, warnings, elapsed time; logs to stderr). Verify with CLI tests for "Human summary", "JSON summary" and "Parse errors are not failures".
- [x] 6.3 Test failure handling: start indexing against the compose stack and stop the `neo4j` container partway through. Verify that the command exits with code 1 and prints an error to stderr ("Database lost mid-run").

## 7. End-to-end and docs

- [x] 7.1 Run the end-to-end check in containers: set `REPO_PATH` to this repo (or another local Python project) and run `podman compose run --rm app codegraph index /repos --repo-name <name>`. Verify that it exits 0, the summary counts are non-zero, and a Cypher spot-check in the browser returns a known class with its `CONTAINS` children.
- [x] 7.2 Run the end-to-end check on the host: `uv run codegraph index <path>` against `bolt://localhost:7687` for the same repo. Verify that the `File.path` values match the container run ("Paths are portable").
- [x] 7.3 Update the README with prerequisites (Podman and `podman machine`), setup (`.env`), the Windows `REPO_PATH` form from 2.3, the index commands, how to run the unit and integration tests, and a few example Cypher queries. Verify by following it from a clean clone.
- [x] 7.4 Run the full suite and lint: `uv run pytest` with the stack up, so no `neo4j` tests are skipped, then `uv run ruff check`. Verify that both pass and that `openspec validate add-infra-and-indexer --strict` passes.
