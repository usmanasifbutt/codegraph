"""Engine + real safety gate + real Neo4j, with a fake LLM."""

import pytest
from fakes import FakeQueryLLM, q

from codegraph.config import NLQSettings
from codegraph.nlq.engine import DriverExecutor, Engine
from codegraph.nlq.schema_prompt import EXAMPLES
from codegraph.pipeline import extract
from codegraph.store.readonly import classify
from codegraph.store.writer import write_batch

pytestmark = pytest.mark.neo4j

INHERITS_BASE = next(e.cypher for e in EXAMPLES if "inherit" in e.question)


@pytest.fixture(scope="module")
def indexed(neo4j_driver, sample_repo):
    repo = "cgtest-engine"
    write_batch(neo4j_driver, extract(sample_repo, repo), root=str(sample_repo))
    yield repo
    with neo4j_driver.session() as s:
        s.run("MATCH (n {repo: $r}) DETACH DELETE n", r=repo).consume()


def test_structural_question(neo4j_driver, indexed):
    llm = FakeQueryLLM([q(INHERITS_BASE)], answer="Three subclasses.")
    engine = Engine(llm, DriverExecutor(neo4j_driver, NLQSettings()), NLQSettings())
    result = engine.ask("which classes inherit from Base?", indexed)
    assert result.ok, result.error
    names = {r["subclass"] for r in result.rows}
    assert {"mypkg.user.User", "mypkg.user.Admin", "mypkg.core.engine.Service"} <= names
    assert "$repo" in result.cypher and indexed not in result.cypher  # repo passed as parameter
    assert llm.answer_calls[0]["rows"] == result.rows


@pytest.mark.parametrize("example", [e for e in EXAMPLES if e.answerable], ids=lambda e: e.question)
def test_prompt_examples_are_valid_read_queries(neo4j_driver, indexed, example):
    verdict = classify(neo4j_driver, example.cypher, {"repo": indexed})
    assert verdict.ok, verdict.reason


def test_truncated_result_repaired_against_neo4j(neo4j_driver, indexed, capsys):
    import json

    per_import = (
        "MATCH (m:Module {repo: $repo})-[i:IMPORTS]->(x:Module {repo: $repo, is_external: true})\n"
        "RETURN split(x.qualified_name, '.')[0] AS package, i.source_file AS file, i.line AS line\n"
        "ORDER BY file, line"
    )
    distinct = (
        "MATCH (m:Module {repo: $repo})-[i:IMPORTS]->(x:Module {repo: $repo, is_external: true})\n"
        "WITH DISTINCT split(x.qualified_name, '.')[0] AS package\n"
        "WHERE NOT package IN ['typing', 'json']  // narrow to third-party\n"
        "RETURN package ORDER BY package"
    )
    nlq = NLQSettings(max_rows=3)
    llm = FakeQueryLLM([q(per_import), q(distinct)], answer="See the packages.")
    engine = Engine(llm, DriverExecutor(neo4j_driver, nlq), nlq)
    result = engine.prepare("Which third-party packages are imported?", indexed)
    assert result.ok and not result.truncated and result.repairs == 1
    packages = [r["package"] for r in result.rows]
    assert packages == ["pydantic", "requests"]
    audit = [
        json.loads(line) for line in capsys.readouterr().err.splitlines() if '"outcome"' in line
    ]
    assert [(a["origin"], a["outcome"]) for a in audit[-2:]] == [
        ("generated", "accepted"),
        ("repaired", "accepted"),
    ]


LIST_EXAMPLES = [
    e
    for e in EXAMPLES
    if e.answerable and e.question.split()[0] in {"Which", "List", "Who", "What"}
]


@pytest.mark.parametrize("example", LIST_EXAMPLES, ids=lambda e: e.question)
def test_list_examples_return_one_row_per_item(neo4j_driver, indexed, example):
    from codegraph.nlq.engine import LOCATION_KEYS
    from codegraph.store.readonly import run_readonly

    rows = run_readonly(neo4j_driver, example.cypher, {"repo": indexed}, max_rows=500, timeout=10)
    assert rows.records, example.question
    item = next(c for c in rows.columns if c not in LOCATION_KEYS)
    values = [r[item] for r in rows.records]
    assert len(values) == len(set(map(str, values))), f"duplicate {item!r} rows: {values}"
