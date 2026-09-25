# codegraph

Ask questions about a Python codebase in plain language, typed or spoken, and get answers from a **code graph** instead of an LLM reading your files.

codegraph parses a repo with Python's `ast` (never running it) into a Neo4j graph of files, modules, classes and functions, linked by `CONTAINS`, `IMPORTS` and `INHERITS` edges that record where they come from. In the web UI you connect a local folder or a public Git URL, index it, and ask things like *"which classes inherit from BaseDTO?"*. An LLM turns the question into a read-only Cypher query, and the answer comes from the returned rows, with `path:line` citations.

## codegraph vs Claude Code

The same 8 structural questions (subclasses, importers, methods, dependencies, tests) about a 70-file backend, with ground truth computed from the code:

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/codegraph-vs-claude-code-dark.svg">
  <img alt="codegraph vs Claude Code: cost per question $0.00099 vs $0.123, tokens per question 5.0k vs 91.9k, 8/8 vs 8/8 correct, 7.1 s vs 19.9 s" src="docs/codegraph-vs-claude-code-light.svg">
</picture>

| Per question | codegraph (gpt-4o-mini) | Claude Code (Sonnet 5) |
|---|---|---|
| Cost | **$0.00099** | $0.123 |
| Tokens | **5.0k** | 91.9k |
| Correct | **8/8** | **8/8** |
| Latency | **7.1 s** | 19.9 s |

- **The same accuracy at about 1/124th of the cost, and about 3× faster.** codegraph sends a small fixed prompt and one query. Claude Code resends its context on every turn and reads source files to find the answer. Indexing takes about a second and uses no LLM.
- **List answers are checked.** If the model leaves out an item that the query returned, codegraph adds it to the answer automatically. If results hit the row cap, it rewrites the query once to aggregate them.
- **Different models:** the two runs used different models (gpt-4o-mini vs Sonnet 5), so part of the cost gap comes from the model itself.
- **Structural questions only.** codegraph can't yet answer questions that need code text, such as environment variable names or what a function does.
- **Reproduce it:** `uv run python -m bench.run_codegraph --repo PATH --name NAME`, then `bench.run_claude_code` and `bench.report` (see `bench/`).

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) with Compose v2 (`docker compose`), **or** [Podman](https://podman.io/) 5.x with `podman compose`. The `compose.yaml` and `Containerfile` work with both. The examples below use `docker compose`; with Podman, run `podman compose` instead.
- An OpenAI or [OpenRouter](https://openrouter.ai/) API key
- Optional, to run on the host: [uv](https://docs.astral.sh/uv/) and `git`

## Setup

```bash
cp .env.example .env
```

In `.env`, set:

- `NEO4J_PASSWORD` (at least 8 characters)
- `REPO_PATH`: a parent folder of your projects, e.g. `D:/Projects`
- the LLM: `LLM_PROVIDER=openai` with `OPENAI_API_KEY`, or `LLM_PROVIDER=openrouter` with `OPENROUTER_API_KEY` and `LLM_MODEL=openai/gpt-4o-mini`

```bash
docker compose up -d        # or: podman compose up -d
```

Open **http://localhost:8501**. The Neo4j browser is on http://localhost:7474 (user `neo4j`).

Notes:
- **Password changes:** `NEO4J_AUTH` only applies when the data volume is created, so to change the password later, also change it in Neo4j (`ALTER CURRENT USER SET PASSWORD ...`) and then run `docker compose up -d`.
- **Windows:** `REPO_PATH` accepts `D:/...` or `D:\...` (and, with Podman, `/mnt/d/...`). In Git Bash, prefix container commands with `MSYS_NO_PATHCONV=1`.

## Usage

- **UI:**
  1. **Connect repository:** pick a folder under `/repos`, or paste a public `https://` Git URL.
  2. Click **Index**.
  3. Ask a question in the chat box, or click the mic (local Whisper; audio never leaves your machine).

  Each answer shows the Cypher, which you can edit and re-run. Queries are read-only (Neo4j checks them with `EXPLAIN`, and they run in READ mode), with a row cap and a timeout.
- **CLI:**
  - `docker compose exec app codegraph index /repos/my-repo`
  - or on the host: `uv run codegraph index path/to/repo`
  - `uv run codegraph ui` runs the UI without containers.
- **Tests:** `uv run pytest`. Opt-in suites: `-m network`, `-m whisper`, `-m llm`.
- **Privacy:** questions, the schema description and result rows (names, paths, signatures) go to your LLM provider. Source code and audio do not.
- **Roadmap and design:** [SPEC.md](SPEC.md) (call graph, git history, a tool-using agent, the benchmark) and [openspec/](openspec/).
