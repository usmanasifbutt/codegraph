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
