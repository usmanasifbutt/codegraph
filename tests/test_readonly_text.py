import json

import pytest

from codegraph.config import LLMSettings, Settings
from codegraph.nlq.audit import audit, configure_audit_file
from codegraph.store.readonly import check_text, serialize, strip_comments_and_strings

OK = "MATCH (c:Class {repo: $repo}) RETURN c.qualified_name"


def test_read_query_passes_text_checks():
    assert check_text(OK).ok
    assert check_text(OK + ";").ok  # one trailing semicolon is fine


def test_multiple_statements_rejected():
    v = check_text(OK + "; MATCH (n) RETURN n")
    assert not v.ok and "single statement" in v.reason and not v.repairable


def test_admin_procedure_rejected():
    v = check_text("CALL dbms.listConfig() YIELD name RETURN name, $repo")
    assert not v.ok and "dbms" in v.reason


def test_full_text_search_allowed():
    q = (
        "CALL db.index.fulltext.queryNodes('symbol_text', 'retry') YIELD node "
        "WHERE node.repo = $repo RETURN node.qualified_name"
    )
    assert check_text(q).ok


@pytest.mark.parametrize(
    "query",
    [
        "CALL dbms.components() YIELD name RETURN name",
        "RETURN apoc.text.join(['a'], ',')",
        "CALL `apoc.do.when`(true, 'x', 'y')",
        "CALL gds.graph.list()",
        "CALL tx.setMetaData({a: 1})",
        "CALL db.createLabel('X')",
        "CALL db.index.fulltext.createNodeIndex('x', ['A'], ['b'])",
        "CALL db.clearQueryCaches()",
        "CALL db.awaitIndexes",
        "LOAD CSV FROM 'file:///x.csv' AS row RETURN row",
        "USE system SHOW USERS",
        "MATCH (n) CALL { WITH n DETACH DELETE n } IN TRANSACTIONS OF 10 ROWS",
    ],
)
def test_every_deny_list_entry(query):
    assert not check_text(query, require_repo=False).ok


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (f:Function {repo: $repo}) WHERE f.docstring CONTAINS 'dbms.listConfig' RETURN f",
        "MATCH (f {repo: $repo}) RETURN f // CALL apoc.do.it(); DELETE",
        "MATCH (f {repo: $repo}) /* LOAD CSV; USE system */ RETURN f",
        "MATCH (tx:Function {repo: $repo}) RETURN tx.name",  # variable named tx
        "MATCH (m:Module {repo: $repo}) WHERE m.name = 'a;b' RETURN m",
    ],
)
def test_deny_words_in_strings_comments_and_names_are_fine(query):
    assert check_text(query).ok, query


def test_repo_parameter_required():
    v = check_text("MATCH (c:Class) RETURN c")
    assert not v.ok and v.repairable and "$repo" in v.reason
    assert check_text("MATCH (c:Class) RETURN c", require_repo=False).ok


def test_empty_query_is_repairable():
    assert check_text("  ").repairable


def test_strip_keeps_backtick_names():
    assert "apoc.x" in strip_comments_and_strings("CALL `apoc.x`()")
    assert "secret" not in strip_comments_and_strings("RETURN 'sec\\'ret'")


def test_serialize_primitives_and_nested():
    assert serialize({"a": [1, "x", None, (True, 2.5)], "b": b"\x01"}) == {
        "a": [1, "x", None, [True, 2.5]],
        "b": "01",
    }


# -- audit (2.4) ------------------------------------------------------------------------
def test_rejected_query_logged(capsys):
    audit(
        repo="r",
        origin="generated",
        outcome="rejected",
        query="MATCH (n) DELETE n",
        reason="not read-only",
    )
    entry = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert entry["origin"] == "generated" and entry["outcome"] == "rejected"
    assert entry["query"] == "MATCH (n) DELETE n" and entry["reason"] == "not read-only"
    assert entry["ts"].endswith("Z")


def test_audit_file(tmp_path, capsys):
    log = tmp_path / "sub" / "audit.log"
    configure_audit_file(log)
    try:
        audit(repo="r", origin="user-edited", outcome="accepted", query="RETURN 1", rows=1)
    finally:
        configure_audit_file(None)
    assert json.loads(log.read_text().strip())["origin"] == "user-edited"


def test_audit_rejects_unknown_origin():
    with pytest.raises(ValueError):
        audit(repo="r", origin="llm", outcome="accepted", query="RETURN 1")


def test_secrets_never_logged(capsys):
    neo = Settings("bolt://x", "neo4j", "neo-secret-pw")
    llm = LLMSettings("openai", "m", "sk-very-secret", None)
    audit(
        repo="r",
        origin="generated",
        outcome="accepted",
        query="RETURN 1",
        rows=1,
        reason=f"{neo!r} {llm!r}",
    )  # even if a settings repr ends up in a reason
    err = capsys.readouterr().err
    assert "neo-secret-pw" not in err and "sk-very-secret" not in err
