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


# -- complete-list-answers: completeness suffix ----------------------------------------------
from codegraph.nlq.engine import completeness_suffix, item_values  # noqa: E402

ASYNC = [f"app.api.mod{i}.handler_{i}" for i in range(9)] + ["app.api.health._get_health_db"]


def _rows(values, key="function"):
    return [{key: v, "file": "app/x.py", "line": i + 1} for i, v in enumerate(values)]


def test_omitted_item_appended():
    answer = "The async functions are: " + ", ".join(f"`{v}`" for v in ASYNC[:9])
    suffix = completeness_suffix(answer, ["function", "file", "line"], _rows(ASYNC))
    assert suffix == "\n\nAlso in the results: `app.api.health._get_health_db`"


def test_short_names_count_as_mentioned():
    rows = _rows(["app.api.health.health", "app.api.ask.ask"])
    assert completeness_suffix("`health` and `ask`.", ["function"], rows) == ""


def test_long_omission_lists_are_capped():
    values = [f"pkg.m{i}.item_{i}" for i in range(200)]
    answer = " ".join(values[:165])
    suffix = completeness_suffix(answer, ["name"], [{"name": v} for v in values])
    assert suffix.count("`") == 40 and suffix.endswith("(+15 more in the results table)")


def test_deliberate_selection_is_not_padded():
    pkgs = [f"pkg{i}" for i in range(14)] + ["langchain", "langchain_openai", "openai"]
    answer = "LLM calls go through `langchain`, `langchain_openai` and `openai`."
    assert completeness_suffix(answer, ["package"], [{"package": p} for p in pkgs]) == ""


def test_nothing_to_append():
    rows = _rows(["a.b.alpha", "a.b.beta"])
    assert completeness_suffix("alpha and beta", ["function", "file", "line"], rows) == ""
    assert completeness_suffix("3 files", ["count"], [{"count": 3}]) == ""  # no text column
    assert completeness_suffix("anything", [], []) == ""


def test_item_column_skips_locations_and_non_text():
    rows = [{"file": "a.py", "line": 1, "uses": 3, "tags": ["x"], "package": "requests"}]
    assert item_values(["file", "line", "uses", "tags", "package"], rows) == ["requests"]


def test_whole_word_matching():
    rows = [{"name": "mod.run"}, {"name": "mod.stop"}, {"name": "mod.start"}]
    # "rerun" must not count as mentioning `run`; 2/3 mentioned -> listing -> run appended
    assert completeness_suffix("rerun, stop and start", ["name"], rows) == (
        "\n\nAlso in the results: `mod.run`"
    )


def test_answer_stream_appends_missing_items_then_sources():
    rows = [{"function": v, "file": f"app/f{i}.py", "line": i + 1} for i, v in enumerate(ASYNC)]
    listed = ", ".join(v.rsplit(".", 1)[-1] for v in ASYNC[:9])
    llm = FakeQueryLLM([q(GOOD)], answer=f"The async functions are {listed}.")
    r = engine(llm, FakeExecutor({GOOD: Rows(["function", "file", "line"], rows, False)})).ask(
        "which functions are async?", "r"
    )
    also, sources = r.answer.index("Also in the results"), r.answer.index("Sources:")
    assert r.answer.startswith("The async functions are") and also < sources
    assert "`app.api.health._get_health_db`" in r.answer[also:sources]
    assert "`app/f9.py:10`" in r.answer[sources:]  # appended item makes its row a relevant source


def test_no_suffix_for_errors_and_off_topic():
    off = engine(FakeQueryLLM([GeneratedQuery(answerable=False, reason="n/a")])).ask("x", "r")
    assert "Also in the results" not in off.answer
    bad = engine(FakeQueryLLM([q("MATCH (n) DELETE n")])).ask("x", "r")
    assert not bad.ok and bad.answer == ""


# -- complete-list-answers: truncated-result repair --------------------------------------------
PER_IMPORT = "MATCH (m:Module {repo: $repo})-[i:IMPORTS]->(x) RETURN x.qualified_name AS pkg"
DISTINCT = "MATCH (m:Module {repo: $repo})-[i:IMPORTS]->(x) RETURN DISTINCT x.name AS pkg"
TRUNC = Rows(["pkg"], [{"pkg": f"p{i % 3}"} for i in range(200)], True)
FULL = Rows(["pkg"], [{"pkg": "p0"}, {"pkg": "p1"}, {"pkg": "p2"}], False)


def test_aggregation_fixes_truncation():
    llm = FakeQueryLLM([q(PER_IMPORT), q(DISTINCT, "Distinct packages.")])
    ex = FakeExecutor({PER_IMPORT: TRUNC, DISTINCT: FULL})
    r = engine(llm, ex).prepare("which packages?", "r")
    assert r.cypher == DISTINCT and r.rows == FULL.records and not r.truncated
    assert r.repairs == 1 and r.explanation == "Distinct packages."
    assert [c[2] for c in ex.calls] == ["generated", "repaired"]
    assert "truncated at 200 rows" in llm.generate_calls[1][2][-1][1]


def test_repair_still_truncated_keeps_original():
    still = "MATCH (m:Module {repo: $repo})-[i:IMPORTS]->(x) RETURN x.name AS pkg"
    llm = FakeQueryLLM([q(PER_IMPORT), q(still)])
    ex = FakeExecutor({PER_IMPORT: TRUNC, still: TRUNC})
    r = engine(llm, ex).prepare("which packages?", "r")
    assert r.cypher == PER_IMPORT and r.truncated and r.repairs == 1


def test_repair_rejected_keeps_original():
    bad = "MATCH (n {repo: $repo}) DETACH DELETE n"
    llm = FakeQueryLLM([q(PER_IMPORT), q(bad)])
    ex = FakeExecutor({PER_IMPORT: TRUNC, bad: QueryRejected("not read-only")})
    r = engine(llm, ex).prepare("which packages?", "r")
    assert r.ok and r.cypher == PER_IMPORT and r.truncated
    assert ex.calls[-1][0] == bad and ex.calls[-1][2] == "repaired"


def test_no_repair_when_not_truncated():
    llm = FakeQueryLLM([q(DISTINCT)])
    r = engine(llm, FakeExecutor({DISTINCT: FULL})).prepare("which packages?", "r")
    assert len(llm.generate_calls) == 1 and r.repairs == 0


def test_truncation_repair_is_outside_max_repairs():
    llm = FakeQueryLLM([q(BAD_SYNTAX), q(PER_IMPORT), q(DISTINCT)])
    ex = FakeExecutor(
        {BAD_SYNTAX: QueryRejected("SyntaxError", True), PER_IMPORT: TRUNC, DISTINCT: FULL}
    )
    nlq = NLQSettings(max_rows=200, timeout_seconds=5, max_repairs=1)  # budget used by syntax fix
    r = engine(llm, ex, nlq).prepare("which packages?", "r")
    assert r.ok and r.cypher == DISTINCT and r.repairs == 2 and len(llm.generate_calls) == 3
