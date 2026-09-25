import json

import pytest
from typer.testing import CliRunner

from codegraph.cli import app

runner = CliRunner()


@pytest.fixture
def no_db_env(tmp_path, monkeypatch):
    """Empty cwd (no .env) and no Neo4j variables."""
    monkeypatch.chdir(tmp_path)
    for var in ("NEO4J_PASSWORD", "NEO4J_URI", "NEO4J_USER"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def test_nonexistent_path(no_db_env, monkeypatch):
    monkeypatch.setenv("NEO4J_PASSWORD", "unused")
    monkeypatch.setenv("NEO4J_URI", "bolt://127.0.0.1:1")  # would fail if contacted
    result = runner.invoke(app, ["index", "./does-not-exist"])
    assert result.exit_code == 2
    assert "does-not-exist" in result.stderr
    assert "connecting" not in result.stderr


def test_path_is_a_file(no_db_env):
    (no_db_env / "f.py").write_text("x = 1\n")
    result = runner.invoke(app, ["index", "f.py"])
    assert result.exit_code == 2 and "not a directory" in result.stderr


def test_missing_password(no_db_env, sample_repo):
    result = runner.invoke(app, ["index", str(sample_repo)])
    assert result.exit_code == 2
    assert "NEO4J_PASSWORD" in result.stderr


def test_unreachable_database_is_runtime_failure(no_db_env, sample_repo, monkeypatch):
    monkeypatch.setenv("NEO4J_PASSWORD", "x")
    monkeypatch.setenv("NEO4J_URI", "bolt://127.0.0.1:1")
    monkeypatch.setenv("NEO4J_CONNECT_TIMEOUT", "1")
    result = runner.invoke(app, ["index", str(sample_repo)])
    assert result.exit_code == 1 and "not reachable" in result.stderr


# -- against Neo4j --------------------------------------------------------------------
@pytest.fixture
def project_cwd(neo4j_driver, monkeypatch):
    """Run the CLI from the project root so it picks up the project .env."""
    from conftest import PROJECT_ROOT

    monkeypatch.chdir(PROJECT_ROOT)


@pytest.mark.neo4j
def test_custom_repository_name(neo4j_driver, repo_names, sample_repo, project_cwd):
    repo = repo_names("cgcli")
    result = runner.invoke(app, ["index", str(sample_repo), "--repo-name", repo])
    assert result.exit_code == 0, result.stderr
    with neo4j_driver.session() as s:
        repos = s.run(
            "MATCH (f:Function {qualified_name: 'mypkg.core.helpers.retry'}) "
            "WHERE f.repo = $r RETURN count(f) AS c",
            r=repo,
        ).single()["c"]
        status = s.run("MATCH (r:Repo {name: $r}) RETURN r.status AS s", r=repo).single()["s"]
    assert repos == 1 and status == "complete"


@pytest.mark.neo4j
def test_human_summary(repo_names, sample_repo, project_cwd):
    result = runner.invoke(app, ["index", str(sample_repo), "--repo-name", repo_names("cgcli")])
    assert result.exit_code == 0, result.stderr
    out = result.stdout
    for word in ("File", "Module", "Class", "Function", "CONTAINS", "IMPORTS", "INHERITS"):
        assert word in out
    assert "Indexed repo" in out and "s\n" in out.splitlines()[0] + "\n"


@pytest.mark.neo4j
def test_json_summary_and_parse_errors_are_not_failures(repo_names, sample_repo, project_cwd):
    repo = repo_names("cgcli")
    result = runner.invoke(app, ["index", str(sample_repo), "--repo-name", repo, "--json"])
    assert result.exit_code == 0, result.stderr
    summary = json.loads(result.stdout)
    assert set(summary) == {
        "repo",
        "nodes",
        "relationships",
        "parse_errors",
        "warnings",
        "elapsed_seconds",
    }
    assert summary["repo"] == repo
    assert summary["nodes"]["Function"] > 0 and summary["relationships"]["IMPORTS"] > 0
    assert [e["path"] for e in summary["parse_errors"]] == ["src/mypkg/broken.py"]
    assert any("collision" in w for w in summary["warnings"])


def test_connect_error_exits_1_without_traceback(no_db_env, sample_repo, monkeypatch):
    import codegraph.cli as cli
    from codegraph.config import ConnectError

    def boom(settings):
        raise ConnectError("Neo4j rejected the credentials for 'neo4j'")

    monkeypatch.setenv("NEO4J_PASSWORD", "wrong")
    monkeypatch.setattr(cli, "connect", boom)
    result = runner.invoke(app, ["index", str(sample_repo)])
    assert result.exit_code == 1
    assert "rejected the credentials" in result.stderr and "Traceback" not in result.output


def test_ui_command_line(monkeypatch):
    import codegraph.cli as cli

    seen = {}

    def fake_call(cmd):
        seen["cmd"] = cmd
        return 0

    monkeypatch.setattr(cli.subprocess, "call", fake_call)
    result = runner.invoke(app, ["ui", "--port", "8600"])
    assert result.exit_code == 0
    cmd = seen["cmd"]
    assert cmd[1:4] == ["-m", "streamlit", "run"] and cmd[4].endswith("app.py")
    assert cmd[cmd.index("--server.port") + 1] == "8600"
    assert cmd[cmd.index("--server.address") + 1] == "127.0.0.1"
