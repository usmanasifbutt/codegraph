FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# git: clone public repositories connected from the UI.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    # Whisper model cache and Git clone workspace live on named volumes (compose.yaml).
    HF_HOME=/cache/huggingface \
    CODEGRAPH_WORKSPACE=/workspace \
    CODEGRAPH_REPOS_ROOT=/repos

# Dependency layer: only rebuilt when the lockfile changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8501
CMD ["codegraph", "ui", "--address", "0.0.0.0", "--port", "8501"]
