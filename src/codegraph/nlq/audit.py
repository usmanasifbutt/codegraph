"""JSON-lines audit log of every Cypher gate decision (design D6).

Only the fields below are ever written; settings objects are never passed in, so API keys
and database passwords cannot reach the log.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

LOGGER_NAME = "codegraph.nlq.audit"
ORIGINS = ("generated", "repaired", "user-edited")
OUTCOMES = ("accepted", "rejected", "error", "timeout")


class _CurrentStderrHandler(logging.StreamHandler):
    """Writes to whatever `sys.stderr` is at emit time (so test capture and reassignment work)."""

    def __init__(self) -> None:
        super().__init__()

    @property
    def stream(self):  # type: ignore[override]
        return sys.stderr

    @stream.setter
    def stream(self, _value) -> None:
        pass


def _logger() -> logging.Logger:
    logger = logging.getLogger(LOGGER_NAME)
    if not getattr(logger, "_codegraph_configured", False):
        logger.setLevel(logging.INFO)
        logger.propagate = False
        handler = _CurrentStderrHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        logger._codegraph_configured = True  # type: ignore[attr-defined]
    return logger


def configure_audit_file(path: str | Path | None) -> None:
    """Also append audit lines to `path` (idempotent; None removes a previously added file)."""
    logger = _logger()
    for h in [h for h in logger.handlers if isinstance(h, logging.FileHandler)]:
        logger.removeHandler(h)
        h.close()
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(fh)


def audit(
    *,
    repo: str | None,
    origin: str,
    outcome: str,
    query: str,
    reason: str | None = None,
    rows: int | None = None,
) -> None:
    if origin not in ORIGINS or outcome not in OUTCOMES:
        raise ValueError(f"bad audit origin/outcome: {origin!r}/{outcome!r}")
    entry = {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "repo": repo,
        "origin": origin,
        "outcome": outcome,
        "reason": reason,
        "rows": rows,
        "query": query,
    }
    _logger().info(json.dumps(entry, ensure_ascii=False))
