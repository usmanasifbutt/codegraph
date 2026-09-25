"""cypher-safety against a real Neo4j (compose stack); skipped when unreachable."""

import pytest

from codegraph.pipeline import extract
from codegraph.store.readonly import (
    QueryRejected,
    QueryTimeout,
    classify,
    guarded_run,
    run_readonly,
)
from codegraph.store.writer import write_batch

pytestmark = pytest.mark.neo4j


@pytest.fixture(scope="module")
def indexed(neo4j_driver, sample_repo):
    repo = "cgtest-readonly"
    write_batch(neo4j_driver, extract(sample_repo, repo), root=str(sample_repo))
    yield repo
    with neo4j_driver.session() as s:
        s.run("MATCH (n {repo: $r}) DETACH DELETE n", r=repo).consume()


def count(driver, repo):
    with driver.session() as s:
        return s.run("MATCH (n {repo: $r}) RETURN count(n) AS c", r=repo).single()["c"]


def test_write_rejected(neo4j_driver, indexed):
    v = classify(neo4j_driver, "MATCH (n {repo: $repo}) DETACH DELETE n", {"repo": indexed})
    assert not v.ok and "not read-only" in v.reason and not v.repairable


def test_hidden_write_rejected(neo4j_driver, indexed):
    q = "MATCH (n:Module {repo: $repo}) WITH n LIMIT 1 SET n.x = 1 RETURN n.qualified_name"
    assert not classify(neo4j_driver, q, {"repo": indexed}).ok
    before = count(neo4j_driver, indexed)
    with pytest.raises(QueryRejected, match="not read-only"):
        guarded_run(
            neo4j_driver,
            q,
            {"repo": indexed},
            max_rows=10,
            timeout=5,
            origin="generated",
            repo=indexed,
        )
    with neo4j_driver.session() as s:
        assert (
            s.run(
                "MATCH (n {repo: $r}) WHERE n.x IS NOT NULL RETURN count(n) AS c", r=indexed
            ).single()["c"]
            == 0
        )
    assert count(neo4j_driver, indexed) == before


def test_read_query_accepted(neo4j_driver, indexed):
    q = "MATCH (c:Class {repo: $repo}) RETURN c.qualified_name AS name ORDER BY name"
    rows = guarded_run(
        neo4j_driver, q, {"repo": indexed}, max_rows=50, timeout=5, origin="generated", repo=indexed
    )
    assert rows.columns == ["name"] and not rows.truncated
    assert "mypkg.user.User" in [r["name"] for r in rows.records]


def test_syntax_error_is_repairable(neo4j_driver, indexed):
    v = classify(neo4j_driver, "MATCH (c:Class {repo: $repo} RETURN c", {"repo": indexed})
    assert not v.ok and v.repairable and "SyntaxError" in v.reason


def test_unbounded_match_truncated(neo4j_driver, indexed):
    rows = run_readonly(
        neo4j_driver, "MATCH (n {repo: $repo}) RETURN n", {"repo": indexed}, max_rows=5, timeout=5
    )
    assert len(rows.records) == 5 and rows.truncated
    assert "_labels" in rows.records[0]["n"]


def test_slow_query_times_out(neo4j_driver):
    with pytest.raises(QueryTimeout):
        run_readonly(
            neo4j_driver,
            "UNWIND range(1, 1000000000) AS x RETURN sum(x) AS s",
            {},
            max_rows=5,
            timeout=1,
        )


def test_defense_in_depth_read_mode(neo4j_driver, indexed):
    before = count(neo4j_driver, indexed)
    with pytest.raises(QueryRejected, match="not read-only"):
        run_readonly(
            neo4j_driver,
            "MATCH (n {repo: $repo}) DETACH DELETE n",
            {"repo": indexed},
            max_rows=5,
            timeout=5,
        )
    assert count(neo4j_driver, indexed) == before


def test_full_text_allowed_end_to_end(neo4j_driver, indexed):
    q = (
        "CALL db.index.fulltext.queryNodes('symbol_text', 'backoff') YIELD node "
        "WHERE node.repo = $repo RETURN node.qualified_name AS name"
    )
    rows = guarded_run(
        neo4j_driver,
        q,
        {"repo": indexed},
        max_rows=10,
        timeout=5,
        origin="user-edited",
        repo=indexed,
    )
    assert "mypkg.core.helpers.retry" in [r["name"] for r in rows.records]
