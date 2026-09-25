# graph-schema Specification

## Purpose
Defines the Neo4j graph contract (labels, relationship types, properties, constraints and indexes) that the indexer writes and that later query tools, the agent and the benchmark read.

## Requirements

### Requirement: Node labels and properties
The graph SHALL use the node labels below. Every node, whatever its label, MUST carry a `repo` property naming the repository it belongs to.

- `Repo`: `name`, `root` (path indexed), `status` (`indexing` | `complete` | `failed`), `indexed_at` (UTC timestamp), `file_count`
- `File`: `path` (relative to the repo root, forward slashes), `language` (`python`), `loc`, `last_modified` (UTC timestamp), `sha256`, and optionally `parse_error` (message), set when the file could not be parsed
- `Module`: `qualified_name` (dotted), `name` (last dotted segment), `file` (relative path; absent for external modules), `is_external` (boolean), `is_package` (boolean), `docstring` (optional)
- `Class`: `qualified_name`, `name`, `file`, `start_line`, `end_line`, `bases` (source text of each base expression), `decorators` (source text of each decorator), `docstring` (optional)
- `Function`: `qualified_name`, `name`, `file`, `start_line`, `end_line`, `signature`, `decorators`, `is_method`, `is_async`, `is_test`, `docstring` (optional)

Line numbers MUST be 1-based.

#### Scenario: Function node properties
- **WHEN** a repository containing `def load(path: str) -> dict:` at line 10 of `pkg/io.py` is indexed
- **THEN** a `Function` node exists with `qualified_name` `pkg.io.load`, `name` `load`, `file` `pkg/io.py`, `start_line` 10, a `signature` containing `path: str` and `-> dict`, and the `repo` property set

#### Scenario: Paths are portable
- **WHEN** the same repository is indexed once from a Windows host path and once from `/repos` in the container
- **THEN** the `File.path` values are identical and use forward slashes

### Requirement: Relationship types and evidence
The graph SHALL use these relationship types in M0:

- `CONTAINS`: `File`→`Module`, `Module`→`Class`/`Function`, `Class`→`Class`/`Function`, `Function`→`Class`/`Function`
- `IMPORTS`: `Module`→`Module`/`Class`/`Function`, with `line`, `names` (list of imported names, `*` for star imports), `alias` (optional), `resolution` (`exact` | `external` | `unresolved`) and `is_type_checking` (boolean)
- `INHERITS`: `Class`→`Class`, with `line`

Every relationship MUST carry `source_file` (relative path) and `line` (1-based) identifying the source location that justifies it.

#### Scenario: Edge evidence present
- **WHEN** any relationship created by the indexer is read
- **THEN** it has a non-null `source_file` and a positive integer `line`

### Requirement: Repository scoping
Several repositories SHALL be able to coexist in one database. Node identity MUST be unique per repository, not globally: `(repo, path)` for `File`, `(repo, qualified_name)` for `Module`, `Class` and `Function`, and `name` for `Repo`. Relationships MUST connect only nodes with the same `repo` value.

#### Scenario: Same module name in two repos
- **WHEN** two repositories that both define a module `utils.helpers` are indexed into the same database
- **THEN** two distinct `Module` nodes with `qualified_name` `utils.helpers` exist, each with a different `repo` value, and neither indexing run fails

#### Scenario: No cross-repo edges
- **WHEN** two repositories are indexed into the same database
- **THEN** no relationship connects a node of one repository to a node of the other

### Requirement: Uniqueness constraints
The database SHALL enforce the identity rules from "Repository scoping" with uniqueness constraints, so that a write creating a duplicate identity fails instead of silently creating a second node.

#### Scenario: Duplicate rejected
- **WHEN** a client attempts to create a second `Function` node with an existing `(repo, qualified_name)` pair
- **THEN** Neo4j rejects the write with a constraint violation

### Requirement: Full-text symbol index
The database SHALL provide a full-text index over the `name` and `docstring` properties of `Module`, `Class` and `Function` nodes.

#### Scenario: Search by docstring word
- **WHEN** a repository with a function whose docstring contains "retry backoff" is indexed and the full-text index is queried for `backoff`
- **THEN** that function's node is among the results

### Requirement: Idempotent schema setup
Creating the constraints and indexes SHALL be safe to repeat: running setup against a database that already has them MUST succeed without changing them or raising errors.

#### Scenario: Setup twice
- **WHEN** schema setup runs twice against the same database
- **THEN** both runs succeed and the set of constraints and indexes is the same after each run
