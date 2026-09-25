from codegraph.parse import is_test_function, parse_file


def defs(facts):
    return {d.local_name: d for d in facts.definitions}


def test_method_and_nested_function(sample_repo):
    d = defs(parse_file(sample_repo, "src/mypkg/core/engine.py"))
    assert d["Service"].kind == "class"
    run = d["Service.run"]
    assert run.is_method and run.parent == "Service" and run.decorators == ("retry",)
    assert run.signature == "(self, n: int) -> int"
    helper = d["Service.run.helper"]
    assert helper.kind == "function" and not helper.is_method and helper.parent == "Service.run"
    assert d["fetch"].is_async


def test_property_getter_and_setter(sample_repo):
    facts = parse_file(sample_repo, "src/mypkg/user.py")
    values = [x for x in facts.definitions if x.local_name == "User.value"]
    assert len(values) == 1
    assert values[0].decorators == ("property",)  # first definition wins


def test_syntax_error_in_one_file(sample_repo):
    facts = parse_file(sample_repo, "src/mypkg/broken.py")
    assert facts.parse_error and "line 3" in facts.parse_error
    assert facts.definitions == [] and facts.imports == []
    assert facts.loc == 4 and len(facts.sha256) == 64


def test_latin1_encoding_cookie(sample_repo):
    facts = parse_file(sample_repo, "legacy_latin1.py")
    assert facts.parse_error is None
    assert facts.docstring == "Café helpers."
    assert "greet" in defs(facts)


def test_docstrings_and_bases(sample_repo):
    d = defs(parse_file(sample_repo, "src/mypkg/user.py"))
    assert d["User"].docstring == "A user account."
    assert [b.dotted for b in d["User"].bases] == ["B"]
    assert [b.dotted for b in d["Admin"].bases] == ["m.Base"]


def test_imports(sample_repo):
    imports = parse_file(sample_repo, "src/mypkg/user.py").imports
    by_line = {i.line: i for i in imports}
    assert by_line[3].kind == "import" and by_line[3].module == "requests"
    rel = by_line[7]
    assert rel.kind == "from" and rel.module is None and rel.level == 1
    assert rel.names == (("models", "m"),)
    assert by_line[10].is_type_checking and not by_line[5].is_type_checking


def test_import_inside_function(sample_repo):
    imports = parse_file(sample_repo, "src/mypkg/core/engine.py").imports
    assert any(i.module == "json" and i.line == 16 for i in imports)


def test_no_code_execution(sample_repo, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    facts = parse_file(sample_repo, "src/mypkg/side_effect.py")
    assert facts.parse_error is None
    assert not (tmp_path / "SIDE_EFFECT_SHOULD_NOT_EXIST").exists()


def test_pytest_function(sample_repo):
    d = defs(parse_file(sample_repo, "tests/test_engine.py"))
    assert d["test_run"].is_test
    assert not d["make_fixture"].is_test
    assert d["TestService.test_start"].is_test
    assert not d["TestService.helper"].is_test


def test_test_named_function_outside_tests(sample_repo):
    d = defs(parse_file(sample_repo, "src/mypkg/core/helpers.py"))
    assert not d["test_connection"].is_test


def test_is_test_file_rules():
    assert is_test_function("pkg/foo_test.py", "test_x", None)
    assert is_test_function("test/unit/helpers.py", "test_x", None)
    assert not is_test_function("pkg/foo.py", "test_x", None)
