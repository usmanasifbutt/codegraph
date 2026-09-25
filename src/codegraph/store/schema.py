"""Constraints and indexes for the code graph (idempotent: safe to run on every index)."""

from __future__ import annotations

from neo4j import Driver

from codegraph.model import NODE_KEYS

SCHEMA_STATEMENTS: list[str] = [
    # Identity is unique per repo, so several repos can share one database.
    *(
        f"CREATE CONSTRAINT {label.lower()}_key IF NOT EXISTS "
        f"FOR (n:{label}) REQUIRE (n.repo, n.{key}) IS UNIQUE"
        for label, key in NODE_KEYS.items()
    ),
    "CREATE CONSTRAINT repo_name IF NOT EXISTS FOR (n:Repo) REQUIRE n.name IS UNIQUE",
    # Fast per-repo scans for re-index deletes and repo-filtered queries.
    *(
        f"CREATE INDEX {label.lower()}_repo IF NOT EXISTS FOR (n:{label}) ON (n.repo)"
        for label in NODE_KEYS
    ),
    "CREATE FULLTEXT INDEX symbol_text IF NOT EXISTS "
    "FOR (n:Module|Class|Function) ON EACH [n.name, n.docstring]",
]


def ensure_schema(driver: Driver) -> None:
    with driver.session() as session:
        for statement in SCHEMA_STATEMENTS:
            session.run(statement).consume()
        session.run("CALL db.awaitIndexes(300)").consume()
