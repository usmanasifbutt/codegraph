## Purpose

Turns a Python repository on disk into the structural graph defined by `graph-schema` (files, modules, classes, functions, containment, imports and inheritance) by static parsing only, with no code execution and no LLM.

## ADDED Requirements

### Requirement: File discovery
The indexer SHALL index every file ending in `.py` under the repository root, except files inside directories named `.git`, `.hg`, `.venv`, `venv`, `env`, `__pycache__`, `node_modules`, `.tox`, `.nox`, `.mypy_cache`, `.pytest_cache`, `build`, `dist`, or `site-packages`, and except paths matching user-supplied exclude glob patterns. Symbolic links MUST NOT be followed.

#### Scenario: Virtualenv skipped
- **WHEN** a repository contains `app/main.py` and `.venv/lib/python3.11/site-packages/requests/api.py`
- **THEN** a `File` node exists for `app/main.py` and none exists for any path under `.venv`

#### Scenario: User exclude
- **WHEN** the indexer runs with exclude pattern `migrations/**` on a repository containing `migrations/0001_initial.py`
- **THEN** no `File` node exists for `migrations/0001_initial.py`

### Requirement: No code execution
The indexer SHALL determine all nodes and relationships by parsing source text. It MUST NOT import, execute or evaluate any code from the indexed repository.

#### Scenario: Side-effecting module
- **WHEN** a repository contains a module whose top level writes a file or raises an exception on import
- **THEN** indexing completes, the module is indexed normally, and the side effect does not occur

### Requirement: Module naming
The indexer SHALL derive each file's module name by walking up from the file through ancestor directories that contain an `__init__.py`. The module name is those package directory names followed by the file stem, joined with dots, with the stem omitted for `__init__.py`, which gives the package's own name. If two files in the same repository would derive the same module name, the indexer MUST instead name each of those files by its full relative path with separators replaced by dots and the `.py` suffix removed, and report a warning.

#### Scenario: src layout
- **WHEN** a repository has `src/mypkg/__init__.py` and `src/mypkg/core/engine.py` with `src/mypkg/core/__init__.py`, and `src/` has no `__init__.py`
- **THEN** the module for `engine.py` is named `mypkg.core.engine` and the module for `src/mypkg/__init__.py` is named `mypkg` with `is_package` true

#### Scenario: Colliding script names
- **WHEN** a repository contains `scripts/run.py` and `tools/run.py`, and neither directory is a package
- **THEN** the modules are named `scripts.run` and `tools.run` and a collision warning is reported

### Requirement: Definitions and containment
For every parseable file, the indexer SHALL create one `File` node and one `Module` node linked by `CONTAINS`. For every `class` and `def`/`async def` at any nesting depth, it SHALL create a `Class` or `Function` node whose `qualified_name` is the enclosing module, class and function names plus its own name, joined by dots. Each such node SHALL be linked by `CONTAINS` from its directly enclosing module, class or function. When the same qualified name is defined more than once in a file (for example a property getter and setter, or a conditional redefinition), the indexer MUST create a single node that records the first definition's location.

#### Scenario: Method and nested function
- **WHEN** `pkg/svc.py` defines class `Service` with method `run`, and `run` defines an inner function `helper`
- **THEN** nodes `pkg.svc.Service` (Class), `pkg.svc.Service.run` (Function, `is_method` true) and `pkg.svc.Service.run.helper` (Function, `is_method` false) exist, linked by `CONTAINS` in that order from `Module` `pkg.svc`

#### Scenario: Property getter and setter
- **WHEN** a class defines `value` under `@property` and again under `@value.setter`
- **THEN** exactly one `Function` node `<class>.value` exists and indexing does not fail

### Requirement: Test function detection
The indexer SHALL set `is_test` true on a `Function` when the function name starts with `test`, it is defined in a file named `test_*.py` or `*_test.py` or in a file under a directory named `tests` or `test`, and it is either at module level or a method of a class whose name starts with `Test`. All other functions MUST have `is_test` false.

#### Scenario: Pytest function
- **WHEN** `tests/test_io.py` defines module-level `test_load` and helper `make_fixture`
- **THEN** `test_load` has `is_test` true and `make_fixture` has `is_test` false

#### Scenario: Test-named function outside tests
- **WHEN** `pkg/util.py` defines `test_connection`
- **THEN** `test_connection` has `is_test` false

### Requirement: Import edges
For every `import` and `from ... import` statement, including those nested in functions, classes and conditional blocks, the indexer SHALL create an `IMPORTS` relationship from the importing module:

- Relative imports MUST be resolved against the importing module's package.
- If the imported name resolves to a module in the repository, the target is that `Module` and `resolution` is `exact`.
- For `from M import n`, if `M.n` is a module in the repository the target is that module. Otherwise, if `n` is a class or function defined at the top level of in-repo module `M`, the target is that node. Otherwise the target is `Module` `M`. In all three cases `resolution` is `exact`.
- If an absolute import names a module whose top-level package is not in the repository, the target is an external `Module` node (`is_external` true) named by the imported module path, and `resolution` is `external`.
- If an import cannot be resolved, the target is a `Module` node named by the best-effort dotted path with `is_external` false and no `file`, and `resolution` is `unresolved`. This applies to a relative import that goes beyond the top-level package or names a module missing from the repository, and to an absolute import whose top-level package is in the repository but whose module is not.
- Imports inside an `if TYPE_CHECKING:` block MUST have `is_type_checking` true.

#### Scenario: Relative import to sibling module
- **WHEN** `pkg/a.py` contains `from .b import helper` and `pkg/b.py` defines top-level function `helper`
- **THEN** an `IMPORTS` edge exists from `Module` `pkg.a` to `Function` `pkg.b.helper` with `resolution` `exact`, `names` `["helper"]` and the statement's line

#### Scenario: Third-party import
- **WHEN** `pkg/a.py` contains `import requests`
- **THEN** an `IMPORTS` edge exists from `pkg.a` to a `Module` named `requests` with `is_external` true and `resolution` `external`

#### Scenario: Type-checking import
- **WHEN** `pkg/a.py` imports `pkg.models` inside `if TYPE_CHECKING:`
- **THEN** the corresponding `IMPORTS` edge has `is_type_checking` true

### Requirement: Inheritance edges
For each class base expression, the indexer SHALL create an `INHERITS` relationship to an in-repository `Class` when the base name resolves through a class defined at the top level of the same module, or through an explicit import into that module (including aliased and dotted-module forms such as `models.Base`). Bases that do not resolve to an in-repository class MUST NOT produce an edge but MUST still appear in the class's `bases` property.

#### Scenario: Imported base class
- **WHEN** `pkg/models.py` defines `class Base` and `pkg/user.py` contains `from pkg.models import Base as B` and `class User(B):`
- **THEN** an `INHERITS` edge exists from `pkg.user.User` to `pkg.models.Base`

#### Scenario: External base class
- **WHEN** a class is declared as `class Config(BaseModel):` with `BaseModel` imported from `pydantic`
- **THEN** no `INHERITS` edge is created for that base and the class's `bases` contains `BaseModel`

### Requirement: Tolerance of unparseable files
A file that cannot be decoded or parsed as Python SHALL NOT stop indexing. The indexer MUST still create its `File` node with `parse_error` set to a short description including the line number when available, MUST create no `Module`, `Class` or `Function` nodes for it, and MUST continue with the remaining files.

#### Scenario: Syntax error in one file
- **WHEN** a repository contains a valid `pkg/ok.py` and `pkg/broken.py` with a syntax error on line 3
- **THEN** `pkg/ok.py` is fully indexed, the `File` node for `pkg/broken.py` has a `parse_error` mentioning line 3, and indexing completes successfully

### Requirement: Deterministic output
Indexing the same unchanged repository twice SHALL produce the same set of nodes (by label and identity) and relationships (by type, endpoints and line), with the same property values apart from `Repo.indexed_at`.

#### Scenario: Re-index is stable
- **WHEN** a repository is indexed, its graph exported, the repository indexed again unchanged, and the graph exported again
- **THEN** the two exports are identical except for `Repo.indexed_at`
