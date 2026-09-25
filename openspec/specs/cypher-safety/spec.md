# cypher-safety Specification

## Purpose
Guarantees that Cypher coming from an LLM or typed by a UI user cannot change the database, reach administrative functions, or run without bounds. This is needed because Neo4j Community Edition has no read-only roles.

## Requirements

### Requirement: Single read-only statement
A query SHALL be accepted only if it is one statement and Neo4j classifies it as read-only. The classification MUST come from the database's own analysis (the query type that `EXPLAIN` reports), not only from text matching. Anything else MUST be rejected with a reason and not executed, including write, read-write, schema and administrative queries, and multiple statements.

#### Scenario: Write rejected
- **WHEN** the query `MATCH (n) DETACH DELETE n` is submitted
- **THEN** it is rejected as not read-only and the database is unchanged

#### Scenario: Hidden write rejected
- **WHEN** a query that reads first and then runs `SET n.x = 1` is submitted
- **THEN** it is rejected as not read-only

#### Scenario: Multiple statements rejected
- **WHEN** a query containing two statements separated by `;` is submitted
- **THEN** it is rejected

#### Scenario: Read query accepted
- **WHEN** `MATCH (c:Class {repo: $repo}) RETURN c.qualified_name` is submitted
- **THEN** it is accepted and executed

### Requirement: Read access-mode execution
Accepted queries SHALL run inside a READ access-mode transaction, so that the server rejects any write that got past validation.

#### Scenario: Defense in depth
- **WHEN** a write query reaches execution through a defect in validation
- **THEN** Neo4j rejects it because of the READ access mode, and nothing is written

### Requirement: Procedure and clause deny-list
Queries that call procedures or functions in the `dbms.`, `db.create`, `db.index.fulltext.create`, `apoc.`, `gds.` or `tx.` namespaces, or that use `LOAD CSV`, `USE`, or `CALL { ... } IN TRANSACTIONS`, SHALL be rejected. `db.index.fulltext.queryNodes`, `db.labels`, `db.relationshipTypes`, `db.propertyKeys` and `db.schema.visualization` MUST remain allowed.

#### Scenario: Admin procedure rejected
- **WHEN** a query calls `dbms.listConfig()`
- **THEN** it is rejected with a reason that names the disallowed procedure

#### Scenario: Full-text search allowed
- **WHEN** a query calls `db.index.fulltext.queryNodes('symbol_text', 'retry')`
- **THEN** it is accepted

### Requirement: Bounded execution
Every accepted query SHALL run with a transaction timeout of `NLQ_TIMEOUT_SECONDS`, and at most `NLQ_MAX_ROWS` rows MUST be returned to the caller, whatever `LIMIT` the query itself contains. The result MUST say whether more rows were available. A query that exceeds the timeout MUST be stopped and reported as a timeout error.

#### Scenario: Unbounded match
- **WHEN** a query matches every node in a repository with thousands of nodes
- **THEN** exactly `NLQ_MAX_ROWS` rows are returned and the result is marked truncated

#### Scenario: Slow query
- **WHEN** a query runs longer than `NLQ_TIMEOUT_SECONDS`
- **THEN** it is terminated and a timeout error is returned

### Requirement: Query logging
Every query submitted for validation, whether accepted or rejected, SHALL be logged with a UTC timestamp, the repository, its origin (`generated`, `repaired` or `user-edited`), the outcome (`accepted`, `rejected` with reason, `error`, `timeout`) and the row count. Logs MUST NOT contain API keys or database passwords.

#### Scenario: Rejected query logged
- **WHEN** a generated query is rejected as not read-only
- **THEN** a log entry records the query text, origin `generated` and outcome `rejected` with the reason
