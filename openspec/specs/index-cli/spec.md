# index-cli Specification

## Purpose
Provides the `codegraph index` command that users and scripts (including the later benchmark) run to build or rebuild a repository's graph in Neo4j and get a summary of what was indexed.

## Requirements

### Requirement: Index command
The CLI SHALL provide `codegraph index <path> [--repo-name NAME] [--exclude GLOB ...] [--json]`. `<path>` is the repository root. The repository name defaults to the final component of the resolved `<path>`. The command MUST ensure the graph schema (constraints and indexes) exists before writing.

#### Scenario: Index a repository
- **WHEN** a user runs `codegraph index ./sample` against a reachable, empty Neo4j database
- **THEN** the schema is created, the graph for repository `sample` is written, a `Repo` node named `sample` has `status` `complete`, and the command exits with code 0

#### Scenario: Custom repository name
- **WHEN** a user runs `codegraph index /repos --repo-name ai-lab`
- **THEN** all written nodes carry `repo` `ai-lab`

### Requirement: Connection configuration
The CLI SHALL read the Neo4j connection from the environment variables `NEO4J_URI`, `NEO4J_USER` and `NEO4J_PASSWORD`, loading a `.env` file from the current directory if one is present. Environment variables already set MUST take precedence over `.env` values. If the database is not reachable, the CLI MUST retry for up to a bounded wait (default 60 seconds, configurable through an environment variable) before failing.

#### Scenario: Database starting up
- **WHEN** the index command starts while Neo4j is still booting and Neo4j becomes reachable 20 seconds later
- **THEN** the command connects and completes normally

#### Scenario: Missing password
- **WHEN** `NEO4J_PASSWORD` is unset and no `.env` provides it
- **THEN** the command exits with code 2 and an error naming the missing variable

### Requirement: Full replacement on re-index
Indexing a repository name that already exists in the database SHALL replace that repository's graph completely: nodes and relationships from the earlier run that no longer correspond to source MUST NOT remain. Data belonging to other repository names MUST NOT be modified.

#### Scenario: Deleted function disappears
- **WHEN** repository `sample` is indexed, a function is removed from its source, and `sample` is indexed again
- **THEN** the removed function's node no longer exists

#### Scenario: Other repos untouched
- **WHEN** repositories `a` and `b` are indexed and then `a` is re-indexed
- **THEN** node and relationship counts for `b` are unchanged

### Requirement: Failure reporting
If indexing fails after writing has started, the CLI SHALL set the `Repo` node's `status` to `failed` when the database is still reachable, print the error to stderr and exit with code 1. A `Repo` whose `status` is not `complete` MUST be treated by consumers as incomplete.

#### Scenario: Database lost mid-run
- **WHEN** the Neo4j connection drops permanently while nodes are being written
- **THEN** the command prints an error to stderr and exits with code 1

### Requirement: Input validation and exit codes
The CLI SHALL exit with code 2 for usage errors, including a `<path>` that does not exist or is not a directory, and missing required configuration. It SHALL exit with code 0 on success, even when some files had parse errors, and with code 1 on runtime failures.

#### Scenario: Nonexistent path
- **WHEN** a user runs `codegraph index ./does-not-exist`
- **THEN** the command exits with code 2 and prints an error naming the path, without contacting the database

#### Scenario: Parse errors are not failures
- **WHEN** the indexed repository contains one file with a syntax error
- **THEN** the command exits with code 0 and the summary reports one parse error

### Requirement: Summary output
On success the CLI SHALL print a summary with the repository name, counts per node label, counts per relationship type, the number of files with parse errors (with their paths), warnings such as module-name collisions, and elapsed time. With `--json`, it MUST print only a single JSON object with the same information to stdout.

#### Scenario: Human summary
- **WHEN** indexing succeeds without `--json`
- **THEN** stdout lists the counts for `File`, `Module`, `Class`, `Function`, `CONTAINS`, `IMPORTS` and `INHERITS`, and the elapsed time

#### Scenario: JSON summary
- **WHEN** indexing succeeds with `--json`
- **THEN** stdout parses as one JSON object containing `repo`, `nodes`, `relationships`, `parse_errors`, `warnings` and `elapsed_seconds`
