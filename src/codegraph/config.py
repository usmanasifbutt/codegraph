"""Neo4j connection settings from the environment (and `.env`), plus a retrying connect."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from neo4j import Driver, GraphDatabase
from neo4j.exceptions import (
    AuthError,
    ClientError,
    DriverError,
    Neo4jError,
    ServiceUnavailable,
    SessionExpired,
    TransientError,
)

DEFAULT_URI = "bolt://localhost:7687"
DEFAULT_USER = "neo4j"
DEFAULT_CONNECT_TIMEOUT = 60.0


class ConfigError(Exception):
    """Missing or invalid configuration (a usage error, exit code 2)."""


class ConnectError(Exception):
    """Neo4j could not be reached or refused the credentials (exit code 1)."""


@dataclass(frozen=True)
class Settings:
    uri: str
    user: str
    password: str
    connect_timeout: float


def load_settings(env: Mapping[str, str] | None = None, dotenv_dir: Path | None = None) -> Settings:
    """Read settings; a `.env` in `dotenv_dir` (default: cwd) never overrides real env vars."""
    if env is None:
        dotenv = (dotenv_dir or Path.cwd()) / ".env"
        if dotenv.is_file():
            load_dotenv(dotenv, override=False)
        env = os.environ
    password = env.get("NEO4J_PASSWORD")
    if not password:
        raise ConfigError("NEO4J_PASSWORD is not set (set it in the environment or in .env)")
    raw_timeout = env.get("NEO4J_CONNECT_TIMEOUT") or str(DEFAULT_CONNECT_TIMEOUT)
    try:
        timeout = float(raw_timeout)
    except ValueError:
        raise ConfigError(f"NEO4J_CONNECT_TIMEOUT must be a number, got {raw_timeout!r}") from None
    return Settings(
        uri=env.get("NEO4J_URI") or DEFAULT_URI,
        user=env.get("NEO4J_USER") or DEFAULT_USER,
        password=password,
        connect_timeout=timeout,
    )


RETRYABLE = (ServiceUnavailable, SessionExpired, TransientError, OSError)

AUTH_HINT = (
    "NEO4J_PASSWORD must match the database's current password. NEO4J_AUTH only sets it "
    "when the neo4j-data volume is first created, so after editing .env either change it in "
    "Neo4j (ALTER CURRENT USER SET PASSWORD ...) and recreate the container with "
    "`podman compose up -d`, or reset with `podman compose down -v`."
)


def _is_auth_failure(exc: Exception) -> bool:
    code = getattr(exc, "code", None) or ""
    return isinstance(exc, AuthError) or code.startswith("Neo.ClientError.Security.")


def connect(
    settings: Settings,
    driver_factory: Callable[..., Any] = GraphDatabase.driver,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Driver:
    """Create a driver and wait (exponential backoff) until Neo4j answers, up to the timeout."""
    driver = driver_factory(settings.uri, auth=(settings.user, settings.password))
    deadline = clock() + settings.connect_timeout
    delay = 0.5
    while True:
        try:
            driver.verify_connectivity()
            return driver
        except (AuthError, ClientError) as exc:
            driver.close()
            if _is_auth_failure(exc):
                raise ConnectError(
                    f"Neo4j rejected the credentials for {settings.user!r} "
                    f"({getattr(exc, 'code', None) or type(exc).__name__}). {AUTH_HINT}"
                ) from exc
            raise ConnectError(f"Neo4j refused the connection: {exc}") from exc
        except RETRYABLE as exc:
            remaining = deadline - clock()
            if remaining <= 0:
                driver.close()
                raise ConnectError(
                    f"Neo4j at {settings.uri} not reachable after {settings.connect_timeout:g}s: "
                    f"{exc}"
                ) from exc
            sleep(min(delay, remaining))
            delay = min(delay * 2, 5.0)
        except (Neo4jError, DriverError) as exc:  # anything else: fail cleanly, no traceback
            driver.close()
            raise ConnectError(f"could not connect to Neo4j at {settings.uri}: {exc}") from exc

