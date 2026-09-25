import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from codegraph import repos
from codegraph.config import RepoSettings
from codegraph.repos import (
    IndexBusy,
    RepoError,
    clone,
    clone_target,
    index_git,
    index_local,
    list_root_folders,
    reindex,
    repo_name_from_url,
    resolve_local,
    validate_git_url,
)

GIT = [
    "git",
    "-c",
    "user.name=t",
    "-c",
    "user.email=t@example.com",
    "-c",
    "init.defaultBranch=main",
]


def git(*args, cwd):
    subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def bare_repo(tmp_path, sample_repo):
    """A local bare repo (served over file://) holding a copy of the fixture repo."""
    work = tmp_path / "work"
    shutil.copytree(sample_repo, work)
    git("init", cwd=work)
    git("add", "-A", cwd=work)
    git("commit", "-m", "init", cwd=work)
    bare = tmp_path / "sample.git"
    git("clone", "--bare", str(work), str(bare), cwd=tmp_path)
    return work, bare


# -- 4.1 local folders -----------------------------------------------------------------
def test_host_folder(tmp_path):
    assert resolve_local(str(tmp_path)) == tmp_path.resolve()


def test_container_root(tmp_path):
    root = tmp_path / "repos"
    (root / "ai-lab").mkdir(parents=True)
    (root / "codegraph").mkdir()
    (root / ".hidden").mkdir()
    (tmp_path / "etc").mkdir()
    assert [p.name for p in list_root_folders(root)] == ["ai-lab", "codegraph"]
    assert resolve_local(root / "ai-lab", root) == (root / "ai-lab").resolve()
    for outside in (tmp_path / "etc", root / ".." / "etc"):
        with pytest.raises(RepoError, match="outside the repos root"):
            resolve_local(outside, root)


def test_symlink_escape(tmp_path):
    root = tmp_path / "repos"
    root.mkdir()
    (tmp_path / "secret").mkdir()
    link = root / "link"
    try:
        os.symlink(tmp_path / "secret", link, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not permitted on this system")
    with pytest.raises(RepoError, match="outside"):
        resolve_local(link, root)


def test_missing_folder_and_file(tmp_path):
    with pytest.raises(RepoError, match="Folder not found"):
        resolve_local(tmp_path / "nope")
    (tmp_path / "f.py").write_text("x = 1\n")
    with pytest.raises(RepoError, match="Not a folder"):
        resolve_local(tmp_path / "f.py")


# -- 4.2 URL validation ----------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:org/repo.git",
        "ssh://git@github.com/org/repo.git",
        "git://github.com/org/repo.git",
        "file:///etc",
        "http://github.com/org/repo",
        "https://user:token@github.com/org/repo",
        "https://token@github.com/org/repo",
        "https://github.com/",
        "https://github.com/org/repo?x=1",
        "--upload-pack=evil",
        "",
    ],
)
def test_disallowed_url(url):
    with pytest.raises(RepoError):
        validate_git_url(url)


@pytest.mark.parametrize(
    "url, folder, name",
    [
        (
            "https://github.com/pallets/itsdangerous",
            "github.com/pallets/itsdangerous",
            "itsdangerous",
        ),
        (
            "https://github.com/pallets/itsdangerous.git",
            "github.com/pallets/itsdangerous",
            "itsdangerous",
        ),
        ("https://gitlab.com/group/sub/proj.git", "gitlab.com/group/sub/proj", "proj"),
    ],
)
def test_accepted_urls_map_to_folders(url, folder, name, tmp_path):
    assert validate_git_url(url) == url
    assert clone_target(tmp_path, url) == tmp_path / Path(folder)
    assert repo_name_from_url(url) == name


# -- 4.3 clone ------------------------------------------------------------------------
def test_clone_local_bare_repo_and_replace(tmp_path, bare_repo):
    work, bare = bare_repo
    ws = tmp_path / "ws"
    target = clone(bare.as_uri(), ws, _allow_file_protocol=True)
    assert (target / "src/mypkg/core/engine.py").is_file()
    assert (target / ".git/shallow").exists()  # shallow clone
    (work / "NEW.md").write_text("new\n")
    git("add", "-A", cwd=work)
    git("commit", "-m", "second", cwd=work)
    git("push", str(bare), "HEAD:main", cwd=work)
    again = clone(bare.as_uri(), ws, _allow_file_protocol=True)
    assert again == target and (target / "NEW.md").is_file()
    assert not [p for p in target.parent.iterdir() if p.name.startswith(".tmp-")]


def test_file_protocol_blocked_without_override(tmp_path, bare_repo):
    with pytest.raises(RepoError, match="https"):
        clone(bare_repo[1].as_uri(), tmp_path / "ws")


class FakeRun:
    """Stands in for subprocess.run: creates the temp clone dir, then fails."""

    def __init__(self, stderr="", returncode=128, exc=None):
        self.stderr, self.returncode, self.exc = stderr, returncode, exc

    def __call__(self, cmd, **kwargs):
        tmp = Path(cmd[-1])
        (tmp / ".git").mkdir(parents=True)
        (tmp / ".git" / "HEAD").write_text("ref")
        assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(cmd, self.returncode, "", self.stderr)


def _no_leftovers(ws: Path):
    return not any(p.name.startswith(".tmp-") for p in ws.rglob("*"))


@pytest.mark.parametrize(
    "stderr",
    [
        "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
        "remote: Repository not found.\nfatal: repository 'https://github.com/a/b/' not found",
    ],
)
def test_private_or_missing_repository(tmp_path, monkeypatch, stderr):
    monkeypatch.setattr(repos.subprocess, "run", FakeRun(stderr))
    ws = tmp_path / "ws"
    with pytest.raises(RepoError, match="only public|Only public"):
        clone("https://github.com/a/b", ws)
    assert _no_leftovers(ws) and not (ws / "github.com/a/b").exists()


def test_clone_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(repos.subprocess, "run", FakeRun(exc=subprocess.TimeoutExpired("git", 1)))
    ws = tmp_path / "ws"
    with pytest.raises(RepoError, match="timed out"):
        clone("https://github.com/a/b", ws, timeout=1)
    assert _no_leftovers(ws)


def test_clone_error_redacts_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(
        repos.subprocess, "run", FakeRun("fatal: unable to access 'https://x:secret@h/r/': boom")
    )
    with pytest.raises(RepoError) as info:
        clone("https://github.com/a/b", tmp_path / "ws")
    assert "secret" not in str(info.value)


# -- 4.4 indexing (Neo4j) --------------------------------------------------------------
def settings(tmp_path, root=None):
    return RepoSettings(repos_root=root, workspace=tmp_path / "ws", clone_timeout=60)


def repo_node(driver, name):
    return next(r for r in repos.list_repos(driver) if r["name"] == name)


def fn_count(driver, repo, name):
    with driver.session() as s:
        return s.run(
            "MATCH (f:Function {repo: $r, name: $n}) RETURN count(f) AS c", r=repo, n=name
        ).single()["c"]


@pytest.mark.neo4j
def test_index_new_folder_and_reindex_after_changes(
    neo4j_driver, repo_names, sample_repo, tmp_path
):
    folder = tmp_path / "proj"
    shutil.copytree(sample_repo, folder)
    name = repo_names()
    steps = []
    summary = index_local(
        neo4j_driver,
        folder,
        name,
        settings(tmp_path),
        excludes=["migrations/**"],
        progress=lambda s, d: steps.append(s),
    )
    assert steps == ["parse", "write", "done"] and summary.nodes["Function"] > 0
    info = repo_node(neo4j_driver, name)
    assert info["source"] == "local" and info["status"] == "complete"
    assert info["excludes"] == ["migrations/**"]
    (folder / "src/mypkg/newmod.py").write_text("def brand_new():\n    return 1\n")
    reindex(neo4j_driver, name, settings(tmp_path))
    assert fn_count(neo4j_driver, name, "brand_new") == 1


@pytest.mark.neo4j
def test_git_repository_updated(neo4j_driver, repo_names, bare_repo, tmp_path):
    work, bare = bare_repo
    name = repo_names()
    index_git(neo4j_driver, bare.as_uri(), name, settings(tmp_path), _allow_file_protocol=True)
    info = repo_node(neo4j_driver, name)
    assert info["source"] == "git" and info["source_url"] == bare.as_uri()
    (work / "src/mypkg/later.py").write_text("def added_later():\n    pass\n")
    git("add", "-A", cwd=work)
    git("commit", "-m", "later", cwd=work)
    git("push", str(bare), "HEAD:main", cwd=work)
    reindex(neo4j_driver, name, settings(tmp_path), _allow_file_protocol=True)
    assert fn_count(neo4j_driver, name, "added_later") == 1


@pytest.mark.neo4j
def test_missing_recorded_path_leaves_graph(neo4j_driver, repo_names, sample_repo, tmp_path):
    folder = tmp_path / "gone"
    shutil.copytree(sample_repo, folder)
    name = repo_names()
    index_local(neo4j_driver, folder, name, settings(tmp_path))
    before = fn_count(neo4j_driver, name, "retry")
    shutil.rmtree(folder)
    with pytest.raises(RepoError, match="no longer exists"):
        reindex(neo4j_driver, name, settings(tmp_path))
    assert fn_count(neo4j_driver, name, "retry") == before == 1
    assert repo_node(neo4j_driver, name)["status"] == "complete"


@pytest.mark.neo4j
def test_second_concurrent_index_is_refused(neo4j_driver, repo_names, sample_repo, tmp_path):
    started, release = threading.Event(), threading.Event()

    def slow_progress(step, _detail):
        if step == "parse":
            started.set()
            release.wait(10)

    name = repo_names()
    t = threading.Thread(
        target=index_local,
        args=(neo4j_driver, sample_repo, name, settings(tmp_path)),
        kwargs={"progress": slow_progress},
    )
    t.start()
    assert started.wait(10)
    try:
        with pytest.raises(IndexBusy, match="Another indexing job"):
            index_local(neo4j_driver, sample_repo, repo_names(), settings(tmp_path))
    finally:
        release.set()
        t.join(30)


# -- 4.5 real network clone ------------------------------------------------------------
@pytest.mark.network
@pytest.mark.neo4j
def test_public_github_repository(neo4j_driver, repo_names, tmp_path):
    name = repo_names("cgnet")
    summary = index_git(
        neo4j_driver, "https://github.com/pallets/itsdangerous", name, settings(tmp_path)
    )
    assert summary.nodes["Module"] > 0
    assert repo_node(neo4j_driver, name)["source"] == "git"
