"""Live text-to-Cypher eval against a real LLM (`uv run pytest -m llm -s`).

Expected answers are computed from the indexer's own GraphBatch for the fixture repo, so the
eval needs no hand labels. A question passes when every expected value appears in the rows
(as a qualified name, its last segment, or a path). Results are printed as a table; a pass rate
below 70% is a finding to report, not a test failure.
"""

from __future__ import annotations

import time

import pytest

from codegraph.config import ConfigError, NLQSettings, load_llm_settings
from codegraph.nlq.engine import DriverExecutor, Engine
from codegraph.nlq.llm import LangChainQueryLLM
from codegraph.pipeline import extract
from codegraph.store.writer import write_batch

pytestmark = [pytest.mark.llm, pytest.mark.neo4j]

# USD per 1M tokens (input, output); used only for the printed estimate.
PRICES = {"gpt-4o-mini": (0.15, 0.60)}


def ground_truth(batch):
    rels = batch.rels
    fns = {n["qualified_name"]: n for n in batch.nodes["Function"]}
    classes = {n["qualified_name"]: n for n in batch.nodes["Class"]}

    def subclasses(base):
        return {r.src for r in rels if r.type == "INHERITS" and r.tgt == base}

    return [
        ("Which classes inherit from the class Base?", subclasses("mypkg.models.Base")),
        (
            "Which modules import mypkg.models?",
            {r.src for r in rels if r.type == "IMPORTS" and r.tgt == "mypkg.models"},
        ),
        (
            "What methods does the Service class have?",
            {
                q
                for q, f in fns.items()
                if f["is_method"]
                and q.startswith("mypkg.core.engine.Service.")
                and q.count(".") == 4
            },
        ),
        ("List all test functions.", {q for q, f in fns.items() if f["is_test"]}),
        ("Which functions are async?", {q for q, f in fns.items() if f["is_async"]}),
        (
            "Which files failed to parse?",
            {n["path"] for n in batch.nodes["File"] if n.get("parse_error")},
        ),
        (
            "Which classes are defined in the module mypkg.user?",
            {q for q in classes if q.rsplit(".", 1)[0] == "mypkg.user"},
        ),
        (
            "Which in-repo functions or classes does mypkg.core.engine import?",
            {
                r.tgt
                for r in rels
                if r.type == "IMPORTS"
                and r.src == "mypkg.core.engine"
                and r.tgt_label in ("Class", "Function")
            },
        ),
        ("Where is retry with exponential backoff implemented?", {"mypkg.core.helpers.retry"}),
        (
            "Which third-party packages are imported?",
            {"requests", "pydantic"},
        ),
        # library questions are answered from external imports, not treated as off-topic
        ("Which library do we use to make HTTP requests?", {"requests"}),
        ("Who calls the retry function?", None),  # not answerable yet (no CALLS edges)
    ]


def values(rows):
    out: set[str] = set()
    for row in rows:
        for v in row.values():
            for item in v if isinstance(v, list) else [v]:
                if isinstance(item, str):
                    out.add(item)
                    out.add(item.split(".")[0])
    return out


def hit(expected: str, seen: set[str]) -> bool:
    return expected in seen or expected.rsplit(".", 1)[-1] in seen


@pytest.fixture(scope="module")
def setup(neo4j_driver, sample_repo):
    try:
        settings = load_llm_settings()
    except ConfigError as exc:
        pytest.skip(f"no LLM configured: {exc}")
    repo = "cgtest-live-eval"
    batch = extract(sample_repo, repo)
    write_batch(neo4j_driver, batch, root=str(sample_repo))
    nlq = NLQSettings()
    engine = Engine(LangChainQueryLLM(settings), DriverExecutor(neo4j_driver, nlq), nlq)
    yield engine, batch, repo, settings
    with neo4j_driver.session() as s:
        s.run("MATCH (n {repo: $r}) DETACH DELETE n", r=repo).consume()


def test_live_eval(setup):
    from langchain_core.callbacks import get_usage_metadata_callback

    engine, batch, repo, settings = setup
    rows_out, passed = [], 0
    with get_usage_metadata_callback() as usage:
        for question, expected in ground_truth(batch):
            t0 = time.perf_counter()
            result = engine.ask(question, repo)
            elapsed = time.perf_counter() - t0
            if expected is None:
                ok = result.ok and not result.executed
            else:
                seen = values(result.rows)
                ok = result.ok and bool(expected) and all(hit(e, seen) for e in expected)
            passed += ok
            rows_out.append((ok, elapsed, result.repairs, question, result.error or ""))
    total = len(rows_out)
    tokens_in = sum(u.get("input_tokens", 0) for u in usage.usage_metadata.values())
    tokens_out = sum(u.get("output_tokens", 0) for u in usage.usage_metadata.values())
    price = PRICES.get(settings.model.split("/")[-1])
    cost = (tokens_in * price[0] + tokens_out * price[1]) / 1e6 if price else None
    print(f"\nLive eval: {settings.provider} {settings.model}")
    for ok, elapsed, repairs, question, error in rows_out:
        print(
            f"  {'PASS' if ok else 'FAIL'}  {elapsed:5.1f}s  repairs={repairs}  {question}  {error}"
        )
    print(
        f"  pass rate {passed}/{total} = {passed / total:.0%} · avg latency "
        f"{sum(r[1] for r in rows_out) / total:.1f}s · tokens in/out {tokens_in}/{tokens_out}"
        + (f" · est. cost ${cost:.4f}" if cost is not None else "")
    )
    assert all(r[4] == "" or "can't" in r[4] for r in rows_out if r[0]), "passing rows had errors"
