"""Write a GraphBatch to Neo4j, replacing whatever that repo had before (design D5)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any, TypeVar

from neo4j import Driver, ManagedTransaction

from codegraph.model import NODE_KEYS, GraphBatch

BATCH_SIZE = 1000
DELETE_CHUNK = 5000

T = TypeVar("T")


class WriteError(Exception):
    """The database did not end up with what the batch described."""


def _chunks(rows: Sequence[T], size: int = BATCH_SIZE) -> Iterator[Sequence[T]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _create_nodes(tx: ManagedTransaction, label: str, rows: Sequence[dict[str, Any]]) -> int:
    query = f"UNWIND $rows AS row CREATE (n:{label}) SET n = row RETURN count(n) AS c"
    return tx.run(query, rows=list(rows)).single()["c"]


def _create_rels(
    tx: ManagedTransaction,
    repo: str,
    rel_type: str,
    src_label: str,
    tgt_label: str,
    rows: Sequence[dict[str, Any]],
) -> int:
    query = (
        f"UNWIND $rows AS row "
        f"MATCH (a:{src_label} {{repo: $repo, {NODE_KEYS[src_label]}: row.src}}) "
        f"MATCH (b:{tgt_label} {{repo: $repo, {NODE_KEYS[tgt_label]}: row.tgt}}) "
        f"CREATE (a)-[r:{rel_type}]->(b) SET r = row.props RETURN count(r) AS c"
    )
    return tx.run(query, rows=list(rows), repo=repo).single()["c"]


def delete_repo(driver: Driver, repo: str) -> None:
    """Remove every node (and its relationships) that belongs to `repo`, except its Repo node."""
    with driver.session() as session:
        for label in NODE_KEYS:
            session.run(
                f"MATCH (n:{label} {{repo: $repo}}) "
                f"CALL (n) {{ DETACH DELETE n }} IN TRANSACTIONS OF {DELETE_CHUNK} ROWS",
                repo=repo,
            ).consume()


def _set_status(driver: Driver, repo: str, status: str, **extra: Any) -> None:
    with driver.session() as session:
        session.run(
            "MERGE (r:Repo {name: $repo}) SET r.repo = $repo, r.status = $status, r += $extra",
            repo=repo,
            status=status,
            extra=extra,
        ).consume()


def write_batch(
    driver: Driver, batch: GraphBatch, root: str, repo_props: dict[str, Any] | None = None
) -> None:
    """Replace the graph of `batch.repo` with the contents of `batch`.

    Not atomic: `Repo.status` is `indexing` while this runs and `complete` only at the end;
    on failure it is set to `failed` when the database is still reachable. `repo_props` (e.g.
    `source`, `source_url`, `branch` from the UI) are stored on the Repo node.
    """
    repo = batch.repo
    _set_status(driver, repo, "indexing", root=root)
    try:
        delete_repo(driver, repo)
        with driver.session() as session:
            for label in NODE_KEYS:
                for chunk in _chunks(batch.nodes.get(label, [])):
                    created = session.execute_write(_create_nodes, label, chunk)
                    if created != len(chunk):
                        raise WriteError(f"created {created} of {len(chunk)} {label} nodes")

            groups: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
            for r in batch.rels:
                groups[(r.type, r.src_label, r.tgt_label)].append(
                    {"src": r.src, "tgt": r.tgt, "props": r.props}
                )
            for (rel_type, src_label, tgt_label), rows in sorted(groups.items()):
                for chunk in _chunks(rows):
                    created = session.execute_write(
                        _create_rels, repo, rel_type, src_label, tgt_label, chunk
                    )
                    if created != len(chunk):
                        raise WriteError(
                            f"created {created} of {len(chunk)} {rel_type} "
                            f"({src_label}->{tgt_label}) relationships: missing endpoints"
                        )
        _set_status(
            driver,
            repo,
            "complete",
            indexed_at=_now(),
            file_count=len(batch.nodes.get("File", [])),
            **(repo_props or {}),
        )
    except Exception:
        try:
            _set_status(driver, repo, "failed")
        except Exception:  # database gone: nothing more we can record
            pass
        raise
