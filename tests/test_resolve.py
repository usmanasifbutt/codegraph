import pytest

from codegraph.pipeline import extract
from codegraph.resolve import module_names


@pytest.fixture(scope="module")
def batch(sample_repo):
    return extract(sample_repo, "sample")


def node(batch, label, key):
    from codegraph.model import NODE_KEYS

    matches = [n for n in batch.nodes[label] if n[NODE_KEYS[label]] == key]
    assert matches, f"no {label} {key}"
    return matches[0]


def rels(batch, type_, src=None, tgt=None):
    return [
        r
        for r in batch.rels
        if r.type == type_ and (src is None or r.src == src) and (tgt is None or r.tgt == tgt)
    ]


# -- 4.1 module naming ----------------------------------------------------------------
def test_src_layout(batch):
    engine = node(batch, "Module", "mypkg.core.engine")
    assert engine["file"] == "src/mypkg/core/engine.py" and engine["is_package"] is False
    pkg = node(batch, "Module", "mypkg")
    assert pkg["file"] == "src/mypkg/__init__.py" and pkg["is_package"] is True


def test_colliding_script_names(batch):
    assert node(batch, "Module", "scripts.run")["file"] == "scripts/run.py"
    assert node(batch, "Module", "tools.run")["file"] == "tools/run.py"
    assert any("collision 'run'" in w for w in batch.warnings)


def test_module_names_package_init_collision():
    paths = ["a/pkg/__init__.py", "b/pkg/__init__.py"]
    names, warnings = module_names(paths, set(paths))
    assert names == {"a/pkg/__init__.py": "a.pkg", "b/pkg/__init__.py": "b.pkg"}
    assert warnings


# -- 4.2 nodes, containment, evidence -------------------------------------------------
def test_every_node_scoped_and_every_edge_has_evidence(batch):
    for label, rows in batch.nodes.items():
        assert rows, label
        assert all(r["repo"] == "sample" for r in rows)
    assert batch.rels
    for r in batch.rels:
        assert r.props["source_file"]
        assert isinstance(r.props["line"], int) and r.props["line"] > 0


def test_contains_chain(batch):
    assert rels(batch, "CONTAINS", "src/mypkg/core/engine.py", "mypkg.core.engine")
    assert rels(batch, "CONTAINS", "mypkg.core.engine", "mypkg.core.engine.Service")
    assert rels(batch, "CONTAINS", "mypkg.core.engine.Service", "mypkg.core.engine.Service.run")
    inner = rels(
        batch, "CONTAINS", "mypkg.core.engine.Service.run", "mypkg.core.engine.Service.run.helper"
    )
    assert inner and inner[0].src_label == "Function"


def test_function_properties(batch):
    fn = node(batch, "Function", "mypkg.user.fetch_user")
    assert fn["file"] == "src/mypkg/user.py" and fn["start_line"] == 29
    assert "user_id: int" in fn["signature"] and "-> 'User'" in fn["signature"]


def test_broken_file_has_file_node_only(batch):
    f = node(batch, "File", "src/mypkg/broken.py")
    assert "line 3" in f["parse_error"]
    assert not any(m.get("file") == "src/mypkg/broken.py" for m in batch.nodes["Module"])
    assert batch.parse_errors == [{"path": "src/mypkg/broken.py", "error": f["parse_error"]}]


def test_docstring_search_target_present(batch):
    assert "backoff" in node(batch, "Function", "mypkg.core.helpers.retry")["docstring"]


# -- 4.3 imports ----------------------------------------------------------------------
def test_relative_import_to_sibling(batch):
    [edge] = rels(batch, "IMPORTS", "mypkg.core.engine", "mypkg.core.helpers.retry")
    assert edge.tgt_label == "Function"
    assert edge.props["resolution"] == "exact" and edge.props["names"] == ["retry"]
    assert edge.props["line"] == 2


def test_parent_relative_import_to_class(batch):
    [edge] = rels(batch, "IMPORTS", "mypkg.core.engine", "mypkg.models.Base")
    assert edge.tgt_label == "Class"


def test_from_package_import_submodule_with_alias(batch):
    edges = {e.props["line"]: e for e in rels(batch, "IMPORTS", "mypkg.user", "mypkg.models")}
    assert sorted(edges) == [7, 10]  # `from . import models as m` and the TYPE_CHECKING import
    assert edges[7].props["alias"] == "m" and "alias" not in edges[10].props


def test_third_party_import(batch):
    [edge] = rels(batch, "IMPORTS", "mypkg.user", "requests")
    assert edge.props["resolution"] == "external"
    assert node(batch, "Module", "requests")["is_external"] is True


def test_type_checking_import(batch):
    edges = rels(batch, "IMPORTS", "mypkg.user")
    tc = [e for e in edges if e.props["is_type_checking"]]
    assert [e.props["line"] for e in tc] == [10]


def test_unresolvable_relative_imports(batch):
    [missing] = rels(batch, "IMPORTS", "mypkg.core.helpers", "mypkg.core.missing")
    assert missing.props["resolution"] == "unresolved"
    stub = node(batch, "Module", "mypkg.core.missing")
    assert stub["is_external"] is False and "file" not in stub
    [beyond] = rels(batch, "IMPORTS", "mypkg.core.engine", "...outside")
    assert beyond.props["resolution"] == "unresolved"


def test_import_inside_function_recorded(batch):
    [edge] = rels(batch, "IMPORTS", "mypkg.core.engine", "json")
    assert edge.props["line"] == 16


# -- 4.4 inheritance ------------------------------------------------------------------
def test_imported_base_class(batch):
    assert rels(batch, "INHERITS", "mypkg.user.User", "mypkg.models.Base")  # via alias B
    assert rels(batch, "INHERITS", "mypkg.user.Admin", "mypkg.models.Base")  # via m.Base
    assert rels(batch, "INHERITS", "mypkg.core.engine.Service", "mypkg.models.Base")  # relative


def test_external_base_class(batch):
    assert not rels(batch, "INHERITS", "mypkg.models.Config")
    assert node(batch, "Class", "mypkg.models.Config")["bases"] == ["BaseModel"]


# -- 4.5 determinism ------------------------------------------------------------------
def test_deterministic(sample_repo):
    assert extract(sample_repo, "sample") == extract(sample_repo, "sample")
