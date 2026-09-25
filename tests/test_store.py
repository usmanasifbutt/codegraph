"""Integration tests against a real Neo4j (the compose stack). Skipped when unreachable."""

import shutil

import pytest
from neo4j.exceptions import ConstraintError

from codegraph.model import GraphBatch, Rel
from codegraph.pipeline import extract
from codegraph.store.schema import ensure_schema
from codegraph.store.writer import WriteError, write_batch

pytestmark = pytest.mark.neo4j


def index(driver, root, repo):
    write_batch(driver, extract(root, repo), root=str(root))


def scalar(driver, query, **params):
    with driver.session() as session:
        return session.run(query, **params).single()[0]


def schema_snapshot(driver):
    with driver.session() as session:
        constraints = sorted(r["name"] for r in session.run("SHOW CONSTRAINTS YIELD name"))
        indexes = sorted(r["name"] for r in session.run("SHOW INDEXES YIELD name"))
    return constraints, indexes


def export(driver, repo):
    """Sorted, comparable dump of a repo's graph (ignores Repo.indexed_at)."""
    with driver.session() as session:
        nodes = sorted(
            (sorted(r["labels"]), sorted(r["props"].items()))
            for r in session.run(
                "MATCH (n {repo: $r}) RETURN labels(n) AS labels, properties(n) AS props",
                r=repo,
            )
        )
        rels = sorted(
            (r["t"], r["a"], r["b"], sorted(r["props"].items()))
            for r in session.run(
                "MATCH (a {repo: $r})-[x]->(b) RETURN type(x) AS t, "
                "coalesce(a.qualified_name, a.path) AS a, coalesce(b.qualified_name, b.path) AS b, "
                "properties(x) AS props",
                r=repo,
            )
        )
    nodes = [
        (labels, [(k, v) for k, v in props if not (labels == ["Repo"] and k == "indexed_at")])
        for labels, props in nodes
    ]
    return nodes, rels


# -- graph-schema ---------------------------------------------------------------------
def test_setup_twice(neo4j_driver):
    ensure_schema(neo4j_driver)
    first = schema_snapshot(neo4j_driver)
    ensure_schema(neo4j_driver)
    assert schema_snapshot(neo4j_driver) == first
    constraints, indexes = first
    assert {"file_key", "module_key", "class_key", "function_key", "repo_name"} <= set(constraints)
    assert "symbol_text" in indexes


def test_duplicate_rejected(neo4j_driver, repo_names):
    repo = repo_names()
    create = "CREATE (:Function {repo: $r, qualified_name: 'a.b'})"
    with neo4j_driver.session() as session:
        session.run(create, r=repo).consume()
        with pytest.raises(ConstraintError):
            session.run(create, r=repo).consume()


def test_same_module_name_in_two_repos(neo4j_driver, repo_names, sample_repo):
    a, b = repo_names(), repo_names()
    index(neo4j_driver, sample_repo, a)
    index(neo4j_driver, sample_repo, b)
    count = scalar(
        neo4j_driver,
        "MATCH (m:Module {qualified_name: 'mypkg'}) WHERE m.repo IN $rs RETURN count(m)",
        rs=[a, b],
    )
    assert count == 2
    cross = scalar(
        neo4j_driver,
        "MATCH (x)-[r]->(y) WHERE x.repo IN $rs AND x.repo <> y.repo RETURN count(r)",
        rs=[a, b],
    )
    assert cross == 0  # "No cross-repo edges"


def test_search_by_docstring_word(neo4j_driver, repo_names, sample_repo):
    repo = repo_names()
    index(neo4j_driver, sample_repo, repo)
    hits = scalar(
        neo4j_driver,
        "CALL db.index.fulltext.queryNodes('symbol_text', 'backoff') YIELD node "
        "WHERE node.repo = $r RETURN collect(node.qualified_name)",
        r=repo,
    )
    assert "mypkg.core.helpers.retry" in hits


# -- writer / re-index ----------------------------------------------------------------
def test_repo_status_and_counts(neo4j_driver, repo_names, sample_repo):
    repo = repo_names()
    batch = extract(sample_repo, repo)
    write_batch(neo4j_driver, batch, root=str(sample_repo))
    with neo4j_driver.session() as session:
        r = session.run("MATCH (r:Repo {name: $r}) RETURN r", r=repo).single()["r"]
        assert r["status"] == "complete" and r["file_count"] == len(batch.nodes["File"])
        assert r["indexed_at"].endswith("Z")
        for label, n in batch.node_counts().items():
            assert (
                scalar(neo4j_driver, f"MATCH (n:{label} {{repo: $r}}) RETURN count(n)", r=repo) == n
            )
        total_rels = scalar(neo4j_driver, "MATCH ({repo: $r})-[x]->() RETURN count(x)", r=repo)
        assert total_rels == len(batch.rels)


def test_deleted_function_disappears(neo4j_driver, repo_names, sample_repo, tmp_path):
    root = tmp_path / "repo"
    shutil.copytree(sample_repo, root)
    repo = repo_names()
    index(neo4j_driver, root, repo)
    q = "MATCH (f:Function {repo: $r, qualified_name: 'mypkg.core.helpers.test_connection'}) "
    assert scalar(neo4j_driver, q + "RETURN count(f)", r=repo) == 1
    helpers = root / "src/mypkg/core/helpers.py"
    helpers.write_text(helpers.read_text().split("def test_connection")[0])
    index(neo4j_driver, root, repo)
    assert scalar(neo4j_driver, q + "RETURN count(f)", r=repo) == 0


def test_other_repos_untouched(neo4j_driver, repo_names, sample_repo):
    a, b = repo_names(), repo_names()
    index(neo4j_driver, sample_repo, a)
    index(neo4j_driver, sample_repo, b)
    before = export(neo4j_driver, b)
    index(neo4j_driver, sample_repo, a)
    assert export(neo4j_driver, b) == before


def test_failed_write_marks_repo_failed(neo4j_driver, repo_names):
    repo = repo_names()
    bad = GraphBatch(
        repo=repo,
        nodes={"File": [{"path": "a.py", "repo": repo}]},
        rels=[
            Rel("CONTAINS", "File", "a.py", "Module", "missing", {"line": 1, "source_file": "a.py"})
        ],
    )
    with pytest.raises(WriteError, match="missing endpoints"):
        write_batch(neo4j_driver, bad, root="/nowhere")
    assert scalar(neo4j_driver, "MATCH (r:Repo {name: $r}) RETURN r.status", r=repo) == "failed"


# -- determinism (5.4) ----------------------------------------------------------------
def test_reindex_is_stable(neo4j_driver, repo_names, sample_repo):
    repo = repo_names()
    index(neo4j_driver, sample_repo, repo)
    first = export(neo4j_driver, repo)
    index(neo4j_driver, sample_repo, repo)
    assert export(neo4j_driver, repo) == first
    assert first[0] and first[1]
