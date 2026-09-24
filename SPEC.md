# codegraph: Spec

Status: draft v0.1. Written to hand off to a fresh implementation session.

## 1. Summary

Build a deterministic **code graph** from a Python repository (files, modules, classes, functions, imports, calls, tests, plus git history), and let an LLM agent answer questions about the codebase by calling **graph tools** alongside plain grep/file reading.

The project is also an experiment: does a graph-backed agent beat a grep-only agent on structural questions? The evaluation (section 9) decides whether this is worth turning into a product.

## 2. Why this idea

- **Data is free.** Any repo is a dataset: start with our own projects, then a handful of open-source Python repos.
- **No LLM needed to build the graph.** Nodes and edges come from the syntax tree, so there is no extraction hallucination, indexing is fast, and cost is near zero.
- **Ground truth is computable.** Questions such as "what calls `X`?" have exact answers derivable from the AST, so evaluation needs no hand labeling.
- **Evidence.** One paper compared vector-only RAG, LLM-extracted knowledge graphs, and AST-derived graphs on Java repos. The AST graph was the most accurate, cheapest, and quickest to build (see Sources [1]). The benchmark is small (15 queries per repo, Java only), so treat it as a strong hint, not proof.

## 3. Honest risks

1. **Agents already navigate code well without a pre-built graph.** Modern coding agents use grep, symbol lookup, imports, and file reads, with vector search as one tool among several (Sources [2], [3]). The graph must beat this baseline on some class of question, or the project has no reason to exist.
2. **Crowded space.** Sourcegraph, Greptile, Cursor-style indexing and others already offer "chat with your repo". A generic chat wrapper loses. A product needs a specific workflow (section 10).
3. **Python is dynamic.** Dynamic dispatch, decorators, `getattr`, and dependency injection make call graphs approximate. The tool must report *confidence* and *unresolved* edges instead of pretending completeness.
4. **Freshness.** The index must update incrementally when files change.
5. **Language scope.** Multi-language support multiplies the work; v1 is Python only.

## 4. Goals and non-goals

Goals (v1):
- Index a Python repo into a queryable graph in seconds to minutes.
- Answer structural questions via graph tools with cited evidence (file, line).
- Beat or match a grep-only agent baseline on a benchmark with AST-computed ground truth.

Non-goals (v1):
- Multi-language support, IDE plugin, hosted service, auth, multi-tenant anything.
- Code generation or automated refactoring.
- Whole-program type inference or perfect call resolution.

## 5. Graph model

Nodes:
| Type | Key attributes |
|---|---|
| `File` | path, language, LOC, last_modified |
| `Module` | dotted name, file |
| `Class` | qualified name, file, start/end line, bases, decorators |
| `Function` | qualified name, file, start/end line, signature, decorators, is_method, is_test, docstring |
| `Author` | name/email (from git) |
| `Commit` | hash, date, message (optional, for co-change) |

Edges:
| Edge | Meaning | Notes |
|---|---|---|
| `CONTAINS` | File/Module/Class contains Function/Class | structural |
| `IMPORTS` | Module imports Module/symbol | resolved via import statements |
| `CALLS` | Function calls Function | best effort; attribute `resolution` in {`exact`, `heuristic`, `unresolved`} |
| `INHERITS` | Class inherits Class | |
| `TESTS` | test Function -> Function | derived from CALLS from `test_*` functions and naming conventions |
| `TOUCHES` | Commit modifies File | from `git log --name-only` |
| `AUTHORED` | Author wrote Commit | |
| `CO_CHANGES_WITH` | File <-> File | derived: files that change in the same commits, weighted |

Every edge stores `source_file` and `line` so answers can cite evidence.

## 6. Architecture

```
repo on disk (mounted read-only into the app container)
   |
   v
indexer (ast or tree-sitter, git log)  --> Neo4j (property graph, Cypher)
                                                   |
                                                   v
                     LangChain agent (langchain-neo4j + tool calling)
                       tools: Cypher-backed graph tools + grep + read_file
                                                   |
                                                   v
                                     CLI first, Streamlit UI later
```

Both Neo4j and the app run as containers via `podman compose` (section 6.2).

- **Parser:** start with Python's stdlib `ast` (zero dependencies, sufficient for Python). Tree-sitter is the upgrade path if we add languages.
- **Storage:** Neo4j holds the graph natively. Multi-hop traversals (callers/impact/closure) are Cypher variable-length path queries, so there is no separate in-memory graph library. Full-text indexes on names and docstrings use Neo4j's built-in full-text index support.
- **Incremental indexing:** hash each file (stored on the `File` node); re-parse only changed files and their dependents, then delete and rewrite that file's nodes/edges in one transaction.
- **Agent:** LangChain agent using tool calling. The model chooses between graph tools and grep. Tool results always include file and line.
- **Graph access via LangChain:** `langchain-neo4j` provides the `Neo4jGraph` connection and schema introspection. Two access styles, compared in the benchmark:
  1. **Curated tools (default):** fixed, parameterized Cypher behind each tool in section 7. Predictable, safe, easy to test.
  2. **Text-to-Cypher chain (`GraphCypherQAChain`) as a fallback tool** for questions the curated tools do not cover. It must run with a **read-only Neo4j user**, and generated Cypher is logged.
- **Model:** configurable via env vars (OpenAI or OpenRouter), same convention as our other projects. Default to a cheap model; the graph does the heavy lifting.

### 6.1 Neo4j schema

Labels: `File`, `Module`, `Class`, `Function`, `Author`, `Commit` (attributes as in section 5). Relationships: `CONTAINS`, `IMPORTS`, `CALLS` (property `resolution`, `line`), `INHERITS`, `TESTS`, `TOUCHES`, `AUTHORED`, `CO_CHANGES_WITH` (property `weight`).

- Uniqueness constraints on `qualified_name` for `Function`/`Class`/`Module` and on `path` for `File`.
- Full-text index over `name` and `docstring`.
- Every `Function` and `Class` carries `repo`, so several repos can live in one database and queries filter by `repo`.
- Example impact query: `MATCH (f:Function {qualified_name:$q})<-[:CALLS*1..$depth]-(caller:Function) RETURN DISTINCT caller`.

### 6.2 Local deployment (podman compose)

`compose.yaml` (run with `podman compose up`) defines two services:

- `neo4j`: official Neo4j image, ports 7474 (browser) and 7687 (bolt), a named volume for `/data`, auth from env vars, and a healthcheck. Add the APOC plugin only if a query needs it.
- `app`: built from a `Containerfile` using a `uv` base, waits for `neo4j` to be healthy, mounts the target repo read-only at `/repos`, and reads config from `.env` (`NEO4J_URI=bolt://neo4j:7687`, `NEO4J_USER`, `NEO4J_PASSWORD`, model and API-key vars). Exposes the Streamlit port when the UI exists.

Notes:
- Podman is the standard here, not Docker. Use fully qualified image names (for example `docker.io/library/neo4j`) because Podman does not assume a default registry.
- Podman runs rootless on Windows via `podman machine`; bind-mount paths from the Windows host need the machine's path form, so verify the repo mount early (M0).
- `.env` stays out of git; ship `.env.example`.
- Provide a second read-only Neo4j user for the text-to-Cypher tool.

## 7. Agent tools

| Tool | Purpose |
|---|---|
| `find_symbol(name)` | resolve a name to candidate nodes (with file/line) |
| `callers_of(symbol, depth=1)` | who calls this, transitively up to depth |
| `callees_of(symbol, depth=1)` | what this calls |
| `impact_of(symbol_or_file)` | reverse-dependency closure: everything that could break if this changes |
| `tests_for(symbol)` | tests that reach this symbol |
| `owners_of(path_or_symbol)` | top authors by git blame/commits, recency-weighted |
| `co_changes(path)` | files that usually change together with this one |
| `module_deps(module)` | imports in and out, with cycle detection |
| `grep(pattern)` | text search fallback |
| `read_file(path, start, end)` | read source to verify and explain |

Design rule: tools return compact, structured results with an explicit `resolution`/confidence field, and the system prompt tells the agent to say so when edges are heuristic or unresolved.

## 8. Question types to support

1. Structural: "Who calls `X`?", "What does `Y` depend on?", "Where is `Z` defined and used?"
2. Impact: "What breaks if I change this function's signature?", "Which tests should I run for this diff?"
3. Ownership: "Who knows this module best?", "Who changed this last?"
4. Architecture: "Which modules depend on the database layer?", "Any import cycles?"
5. Cross-cutting: "Where do we read environment variables?", "Where is user input validated?" (graph plus grep)

The "which tests should I run for this diff" case ties to an earlier idea (test selection). Static `TESTS` edges complement coverage-based tools rather than replace them.

## 9. Evaluation (the gate)

Build the benchmark before the UI.

- **Corpus:** 3 to 5 Python repos of varied size (our own `ai-lab` projects, plus small and medium open-source repos).
- **Auto-generated questions with AST ground truth:** callers of function F, callees of F, importers of module M, subclasses of class C, tests that reach F, files that import X transitively. Generate several hundred programmatically.
- **Baselines:** (a) grep/read-only agent, (b) vector-RAG over code chunks, (c) graph-tool agent (ours, curated Cypher tools), (d) text-to-Cypher only (`GraphCypherQAChain`).
- **Metrics:** precision/recall against ground truth, answer correctness, number of tool calls, tokens, latency, cost.
- **Caveat to track:** AST ground truth shares blind spots with the graph (both miss dynamic calls), so also hand-verify a small set (about 30) of questions on dynamic-dispatch-heavy code to expose over-confidence.
- **Gate:** continue to a product only if the graph agent beats the grep-only baseline meaningfully (proposal: at least 15 points recall on multi-hop/impact questions, at comparable or lower cost). If not, the finding itself is the deliverable.

## 10. Product angles (only if the gate passes)

Generic "chat with your repo" is not a wedge. Candidates with a sharper edge:
- **Onboarding to an unfamiliar or legacy client codebase** (outsourcing and consulting teams).
- **PR blast-radius report** posted on pull requests: affected callers, tests to run, suggested reviewers.
- **Compliance/audit queries:** "where do we touch personal data or secrets?"
- **Migration planning:** find all usages of a deprecated API and estimate effort.
- **Multi-repo / monorepo cross-reference** at a scale where grep alone struggles.

Pick one to validate with 3 to 5 target users before building further.

## 11. Milestones

- **M0, Infra and indexer:** `compose.yaml` + `Containerfile` bring up Neo4j and the app with `podman compose up`; parse a Python repo into Neo4j (files, classes, functions, imports, CONTAINS/IMPORTS/INHERITS) with constraints and indexes. CLI: `index <path>`. Verify the repo bind mount on Windows.
- **M1, Calls and tests:** CALLS with resolution levels, TESTS edges, graph stats command.
- **M2, Git layer:** authors, commits, TOUCHES, CO_CHANGES_WITH.
- **M3, Agent:** LangChain tool-calling agent with the Cypher-backed tools in section 7 plus the read-only text-to-Cypher fallback, CLI `ask "<question>"`, evidence-cited answers.
- **M4, Benchmark:** question generator, baselines, results table. Decision on the gate.
- **M5 (optional):** Streamlit UI with a graph view and answer citations; incremental indexing; a product-angle prototype.

## 12. Tech choices

- Python 3.11+, `uv` for project management (same as our other projects).
- stdlib `ast` first; `tree-sitter` later if adding languages.
- Neo4j (Cypher) as the graph store; official `neo4j` Python driver for bulk indexing (batched `UNWIND` writes).
- LangChain (`langchain`, `langchain-neo4j`, plus the chat-model package) for the agent and graph querying.
- Podman + `podman compose` for Neo4j and the app.
- `git` via subprocess or `GitPython`/`pygit2`.
- Config via `.env` and environment variables; never commit secrets.

## 13. Open questions

- How far do we take call resolution (import-aware name resolution only, or add light type inference)?
- Is a benchmark on Python-only repos convincing enough, or should one Java/TypeScript repo be added to compare against the paper?
- Does the agent need a "graph summary" (community/module summaries) for architecture questions, or are traversal tools enough?
- Does the text-to-Cypher fallback earn its place, or do curated tools cover everything? (Measure in M4: count how often it is called and how often its Cypher is wrong.)
- Neo4j Community vs. a hosted option (Aura) if this becomes a product; Community is enough for local use.
- Which single product angle is worth validating first?

## Sources

1. [Reliable Graph-RAG for Codebases: AST-Derived Graphs vs LLM-Extracted Knowledge Graphs (arXiv 2601.08773)](https://arxiv.org/pdf/2601.08773). Basis for the claim that deterministic AST graphs beat vector-only RAG and LLM-extracted graphs on accuracy, cost, and indexing speed. Small Java-only benchmark.
2. [RAG Is Not Always the Answer Anymore: How AI Agents Search Code in 2026 (DEV Community)](https://dev.to/nimay_04/rag-is-not-always-the-answer-anymore-how-ai-agents-search-code-in-2026-43m3). Basis for the "agentic grep/search is a strong baseline" risk.
3. [Grep vs. Graph: Agentic Search Is Powerful, but Enterprise AI Needs Governed Knowledge (Medium)](https://medium.com/@yu-joshua/grep-vs-graph-agentic-search-is-powerful-but-enterprise-ai-needs-governed-knowledge-8de709c31451). Basis for the argument that graphs add value for governed, large-scale, multi-repo use cases.

Implementation references (not researched for this spec; check the current docs and versions before coding, since LangChain's Neo4j integration has moved between packages):
4. [Neo4j documentation (Cypher, drivers, full-text indexes)](https://neo4j.com/docs/)
5. [langchain-neo4j (LangChain's Neo4j integration)](https://github.com/langchain-ai/langchain-neo4j)
6. [podman-compose](https://github.com/containers/podman-compose)

Note for the implementation session: sources 2 and 3 are opinion/blog posts, not peer-reviewed. Re-verify claims against primary material (and re-read source 1's methodology) before relying on them for product decisions.
