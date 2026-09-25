"""The benchmark's own logic: question generation, ground truth and scoring."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from bench.questions import Question, questions
from bench.scoring import found, score
from codegraph.pipeline import extract

ROOT = Path(__file__).parent.parent


@pytest.fixture(scope="module")
def fixture_questions(sample_repo):
    return {q.text: q for q in questions(extract(sample_repo, "bench-test"))}


def test_fixture_ground_truth_matches_hand_checked_sets(fixture_questions):
    subs = {"mypkg.user.User", "mypkg.user.Admin", "mypkg.core.engine.Service"}
    expected = {
        "Which classes directly subclass `Base` (mypkg.models.Base)?": subs,
        "Which classes inherit from `Base` directly or indirectly?": subs,
        # `from mypkg.models import Base` and `from ..models import Base` count as importing it
        "Which modules import the module `mypkg.models`?": {"mypkg.user", "mypkg.core.engine"},
        "List the methods defined in class `test_engine.TestService`.": {"test_start", "helper"},
        "Which third-party (non-standard-library) packages does this repo import?": {
            "requests",
            "pydantic",
        },
        "Which modules import the `requests` package?": {"mypkg.user"},
        "How many test functions are there? List their names.": {"test_run", "test_start"},
        "Which non-test functions are async?": {"fetch"},
    }
    assert {t: set(q.expected) for t, q in fixture_questions.items()} == expected


def test_questions_round_trip_through_json(fixture_questions):
    for q in fixture_questions.values():
        assert Question.from_dict(json.loads(json.dumps(q.to_dict()))) == q


@pytest.mark.parametrize(
    "answer",
    ["- `src.mypkg.user`", "mypkg.user imports it", "see src/mypkg/user.py", "mypkg/user"],
)
def test_scoring_accepts_prefixed_and_path_forms(answer):
    assert found("mypkg.user", answer, "qual")


def test_scoring_accepts_tests_prefix_for_top_level_test_modules():
    assert found("test_cli", "- `tests.test_cli`", "qual")


def test_scoring_rejects_bare_tail_for_modules_and_substrings():
    assert not found("mypkg.user", "the user module", "qual")  # bare tail: ambiguous
    assert not found("run", "rerun the job", "short")  # substring, not a whole word
    assert found("mypkg.core.engine.Service", "`Service`", "short")  # short: tail allowed


def test_score_recall():
    assert score({"a", "b"}, "a and b", "short") == (True, 1.0)
    assert score({"a", "b"}, "only a", "short") == (False, 0.5)
    assert score(set(), "anything", "short") == (True, 1.0)


def test_results_folder_is_git_ignored():
    out = subprocess.run(
        ["git", "check-ignore", "bench/results/backend/codegraph.json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.returncode == 0, "bench/results/ must be git-ignored (may hold private repo data)"


def test_bench_not_packaged():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'packages = ["src/codegraph"]' in pyproject
    assert sys.modules["bench"].__file__.startswith(str(ROOT))


def test_report_from_hand_made_results(tmp_path):
    from dataclasses import asdict

    from bench.common import Row
    from bench.report import markdown

    def rows(approach, model, oks, cost):
        return [
            asdict(Row(approach=approach, model=model, question=f"q{i}", expected=2, ok=ok,
                       recall=1.0 if ok else 0.5, tokens_in=1000, tokens_out=100,
                       cost_usd=cost, secs=2.0))
            for i, ok in enumerate(oks)
        ]  # fmt: skip

    (tmp_path / "codegraph.json").write_text(json.dumps(rows("codegraph", "m1", [1, 0], 0.001)))
    (tmp_path / "claude_code.json").write_text(json.dumps(rows("claude_code", "m2", [1, 1], 0.1)))
    (tmp_path / "notes.json").write_text("[]")  # unknown files are ignored
    table = markdown(tmp_path, per_question=True)
    assert "| codegraph | m1 | 1/2 | 75% | 1.1k | $0.00100 | $0.0020 | 2.0s |" in table
    assert "| Claude Code | m2 | 2/2 | 100% |" in table
    assert "| q1 | ✗ 50% | ✓ 100% |" in table


def test_dry_run_prints_questions_without_llm(sample_repo, tmp_path, monkeypatch, capsys):
    import bench.common
    from bench import run_codegraph

    monkeypatch.setattr(bench.common, "RESULTS", tmp_path)
    run_codegraph.main(["--repo", str(sample_repo), "--name", "fixture", "--dry-run"])
    out = capsys.readouterr().out
    assert "Which classes directly subclass `Base`" in out and "3 expected" in out
    assert (tmp_path / "fixture" / "questions.json").exists()
