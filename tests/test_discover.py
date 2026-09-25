from codegraph.discover import discover


def test_virtualenv_skipped(sample_repo):
    paths = discover(sample_repo)
    assert "src/mypkg/core/engine.py" in paths
    assert not any(p.startswith(".venv/") for p in paths)


def test_user_exclude(sample_repo):
    assert "migrations/0001_initial.py" in discover(sample_repo)
    assert "migrations/0001_initial.py" not in discover(sample_repo, ["migrations/**"])


def test_exclude_double_star_prefix_matches_root(sample_repo):
    paths = discover(sample_repo, ["**/run.py"])
    assert "scripts/run.py" not in paths and "tools/run.py" not in paths


def test_paths_sorted_and_posix(sample_repo):
    paths = discover(sample_repo)
    assert paths == sorted(paths)
    assert all("\\" not in p for p in paths)


def test_non_python_ignored(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    (tmp_path / "notes.txt").write_text("hi\n")
    assert discover(tmp_path) == ["a.py"]
