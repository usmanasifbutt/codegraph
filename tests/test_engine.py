import pytest
from fakes import FakeExecutor, FakeQueryLLM, q

from codegraph.config import LLMSettings, NLQSettings
from codegraph.model import NODE_KEYS
from codegraph.nlq.engine import Engine
from codegraph.nlq.llm import GeneratedQuery, LangChainQueryLLM, LLMOutputError, parse_fallback
from codegraph.nlq.schema_prompt import (
    EXAMPLES,
    SCHEMA_TEXT,
    answer_messages,
    generation_messages,
    rows_for_prompt,
)
from codegraph.store.readonly import QueryRejected, QueryTimeout, Rows, check_text

GOOD = "MATCH (c:Class {repo: $repo}) RETURN c.qualified_name AS name"
BAD_SYNTAX = "MATCH (c:Class {repo: $repo} RETURN c"
NLQ = NLQSettings(max_rows=200, timeout_seconds=5, max_repairs=2)


def engine(llm, executor=None, nlq=NLQ):
    return Engine(llm, executor or FakeExecutor(), nlq)


# -- 3.1 schema prompt -------------------------------------------------------------------
def test_schema_prompt_covers_graph():
    for label in [*NODE_KEYS, "Repo"]:
        assert f":{label}" in SCHEMA_TEXT, label
    for rel in ("CONTAINS", "IMPORTS", "INHERITS"):
        assert f":{rel}" in SCHEMA_TEXT, rel
    assert "symbol_text" in SCHEMA_TEXT and "CALLS" in SCHEMA_TEXT


def test_examples_pass_text_checks():
    answerable = [e for e in EXAMPLES if e.answerable]
    assert len(answerable) >= 7 and any(not e.answerable for e in EXAMPLES)
    for e in answerable:
        assert check_text(e.cypher).ok, e.question


def test_generation_messages_include_repo_and_feedback():
    msgs = generation_messages("who imports x?", "myrepo", [(BAD_SYNTAX, "SyntaxError")])
    assert msgs[0][0] == "system" and "myrepo" in msgs[1][1]
    assert BAD_SYNTAX in msgs[2][1] and "SyntaxError" in msgs[3][1]


# -- 3.2 LLM client --------------------------------------------------------------------
def test_build_chat_openai_and_openrouter():
    oa = LangChainQueryLLM(LLMSettings("openai", "gpt-4o-mini", "sk-test", None))
    assert oa.chat.model_name == "gpt-4o-mini"
    orr = LangChainQueryLLM(
        LLMSettings("openrouter", "x/y", "or-key", "https://openrouter.ai/api/v1")
    )
    assert orr.chat.model_name == "x/y"
    assert str(orr.chat.openai_api_base).startswith("https://openrouter.ai/api/v1")


@pytest.mark.parametrize(
    "reply, expected",
    [
        ('{"answerable": true, "cypher": "MATCH (n) RETURN n"}', "MATCH (n) RETURN n"),
        (
            "Here:\n```cypher\nMATCH (n {repo: $repo}) RETURN n\n```",
            "MATCH (n {repo: $repo}) RETURN n",
        ),
        ("```\nRETURN 1\n```", "RETURN 1"),
    ],
)
def test_fallback_parser(reply, expected):
    assert parse_fallback(reply).cypher == expected


def test_fallback_parser_gives_up():
    with pytest.raises(LLMOutputError):
        parse_fallback("I am not sure.")


# -- 3.3 engine ------------------------------------------------------------------------
def test_syntax_error_repaired():
    llm = FakeQueryLLM([q(BAD_SYNTAX), q(GOOD, "classes")], answer="Two classes.")
    ex = FakeExecutor({BAD_SYNTAX: QueryRejected("Neo.ClientError.Statement.SyntaxError", True)})
    r = engine(llm, ex).ask("which classes?", "repo1")
    assert r.ok and r.repairs == 1 and r.cypher == GOOD and r.answer == "Two classes."
    assert [c[2] for c in ex.calls] == ["generated", "repaired"]
    assert llm.generate_calls[1][2] == [(BAD_SYNTAX, "Neo.ClientError.Statement.SyntaxError")]


def test_write_query_never_executed():
    delete = "MATCH (n {repo: $repo}) DETACH DELETE n"
    llm = FakeQueryLLM([q(delete)])
    ex = FakeExecutor({delete: QueryRejected("not read-only (Neo4j classifies it as write)")})
    r = engine(llm, ex).ask("delete everything", "repo1")
    assert not r.ok and "not read-only" in r.error and r.cypher == delete
    assert len(llm.generate_calls) == NLQ.max_repairs + 1
    assert llm.answer_calls == []  # nothing ran, so nothing to answer from


def test_unrelated_question():
    llm = FakeQueryLLM([GeneratedQuery(answerable=False, reason="Not about the code.")])
    ex = FakeExecutor()
    r = engine(llm, ex).ask("what's the weather in Lahore?", "repo1")
    assert r.ok and not r.answerable and not r.executed and ex.calls == []
    assert "can't answer" in r.answer and "Not about the code." in r.answer


def test_empty_result_goes_to_answer_step():
    llm = FakeQueryLLM([q(GOOD)], answer=lambda rows, t: "Nothing matched." if not rows else "?")
    r = engine(llm, FakeExecutor({GOOD: Rows(["name"], [], False)})).ask("x?", "repo1")
    assert llm.answer_calls[0]["rows"] == [] and r.answer == "Nothing matched."


def test_truncated_rows_reported():
    rows = Rows(["name"], [{"name": str(i)} for i in range(200)], True)
    llm = FakeQueryLLM([q(GOOD)])
    r = engine(llm, FakeExecutor({GOOD: rows})).ask("all?", "repo1")
    assert r.truncated and len(r.rows) == 200 and llm.answer_calls[0]["truncated"] is True


def test_timeout_not_repaired():
    llm = FakeQueryLLM([q(GOOD)])
    r = engine(llm, FakeExecutor({GOOD: QueryTimeout("query exceeded 5s")})).ask("x", "r")
    assert "Timed out" in r.error and len(llm.generate_calls) == 1


def test_llm_failure_is_reported_not_raised():
    r = engine(FakeQueryLLM([RuntimeError("401 bad key")])).ask("x", "r")
    assert "LLM request failed" in r.error and "401" in r.error


def test_result_fields():
    llm = FakeQueryLLM([q(GOOD, "Lists classes.")], answer="The classes are A and B.")
    r = engine(llm, FakeExecutor({GOOD: Rows(["name"], [{"name": "A"}, {"name": "B"}], False)}))
    res = r.ask("which classes?", "repo1")
    assert (res.question, res.repo, res.origin) == ("which classes?", "repo1", "generated")
    assert res.cypher and res.explanation == "Lists classes." and res.columns == ["name"]
    assert res.rows == [{"name": "A"}, {"name": "B"}] and res.truncated is False
    assert res.answer and res.error is None and res.repairs == 0
    assert set(res.timings) == {"generate", "execute", "answer"} and res.elapsed >= 0


def test_run_cypher_user_edited():
    ex = FakeExecutor()
    r = engine(FakeQueryLLM([q(GOOD)]), ex).run_cypher(GOOD.replace("name", "n"), "r", "q?")
    assert r.origin == "user-edited" and ex.calls[0][2] == "user-edited" and r.answer
    bad = engine(FakeQueryLLM([q(GOOD)]), ex).run_cypher("MATCH (n) DELETE n", "r")
    assert not bad.ok and "rejected" in bad.error


# -- 3.4 answer prompt -------------------------------------------------------------------
def test_answer_prompt_includes_rows_and_notes():
    rows = [{"importer": "a.b", "file": "a/b.py", "line": 3}]
    human = answer_messages("who imports x?", GOOD, rows, truncated=True)[1][1]
    assert '"file": "a/b.py"' in human and "truncated" in human and GOOD in human
    system = answer_messages("q", GOOD, rows, False)[0][1]
    assert "path:line" in system and "ONLY" in system
    assert "no rows" in answer_messages("q", GOOD, [], False)[1][1]


def test_answer_prompt_char_budget():
    rows = [{"v": "x" * 500} for _ in range(100)]
    text, included = rows_for_prompt(rows, budget=5_000)
    assert included < 100 and len(text) <= 5_000
    human = answer_messages("q", GOOD, rows, False, budget=5_000)[1][1]
    assert f"Only the first {included} of 100 rows" in human


def test_citations_contract():
    rows = [{"importer": "a", "source_file": "pkg/a.py", "line": 7}]
    llm = FakeQueryLLM(
        [q(GOOD)], answer=lambda rows, t: f"Imported at {rows[0]['source_file']}:{rows[0]['line']}."
    )
    r = engine(llm, FakeExecutor({GOOD: Rows(list(rows[0]), rows, False)})).ask("who?", "r")
    assert "pkg/a.py:7" in r.answer


def test_sources_added_when_answer_lacks_path_line():
    rows = [
        {"importer": "a", "file": "src/a.py", "line": 7},
        {"importer": "b", "file": "src/b.py", "line": 2},
    ]
    llm = FakeQueryLLM([q(GOOD)], answer="It is imported in the file src/a.py at line 7.")
    r = engine(llm, FakeExecutor({GOOD: Rows(list(rows[0]), rows, False)})).ask("who?", "r")
    assert r.answer.endswith("Sources: `src/a.py:7`, `src/b.py:2`")


def test_no_sources_suffix_when_cited_or_no_locations():
    rows = [{"file": "src/a.py", "start_line": 3}]
    cited = FakeQueryLLM([q(GOOD)], answer="See `src/a.py:3`.")
    r = engine(cited, FakeExecutor({GOOD: Rows(["file", "start_line"], rows, False)})).ask("?", "r")
    assert "Sources" not in r.answer
    plain = FakeQueryLLM([q(GOOD)], answer="Two classes.")
    r = engine(plain, FakeExecutor({GOOD: Rows(["name"], [{"name": "A"}], False)})).ask("?", "r")
    assert r.answer == "Two classes."


def test_sources_prefer_rows_the_answer_mentions():
    rows = [
        {"package": "dotenv", "imported_names": ["load_dotenv"], "file": "utils.py", "line": 1},
        {
            "package": "langchain",
            "imported_names": ["init_chat_model"],
            "file": "chat/rag.py",
            "line": 5,
        },
    ]
    llm = FakeQueryLLM([q(GOOD)], answer="Chat models come from `langchain` (`init_chat_model`).")
    r = engine(llm, FakeExecutor({GOOD: Rows(list(rows[0]), rows, False)})).ask("llm?", "r")
    assert r.answer.endswith("Sources: `chat/rag.py:5`")
