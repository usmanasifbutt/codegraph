# codegraph

Build a deterministic code graph from a Python repo and ask questions about it with an LLM agent that uses graph tools (callers, impact, tests, owners) alongside grep.

Status: spec only. See [SPEC.md](SPEC.md) for the design, evaluation plan, milestones, and sources.

Stack: Neo4j + LangChain for the graph and querying, [uv](https://docs.astral.sh/uv/) for Python, `podman compose` to run Neo4j and the app.
