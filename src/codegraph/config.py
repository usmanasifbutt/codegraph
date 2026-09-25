"""Neo4j connection settings from the environment (and `.env`), plus a retrying connect."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
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
    password: str = field(repr=False)
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT


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


# -- UI / text-to-Cypher / speech / repo settings (add-nl-query-ui) --------------------
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
PROVIDERS = ("openai", "openrouter")


def _env(env: Mapping[str, str] | None, dotenv_dir: Path | None) -> Mapping[str, str]:
    if env is not None:
        return env
    dotenv = (dotenv_dir or Path.cwd()) / ".env"
    if dotenv.is_file():
        load_dotenv(dotenv, override=False)
    return os.environ


def _number(env: Mapping[str, str], name: str, default: float, cast: type = float) -> Any:
    raw = env.get(name) or ""
    if not raw.strip():
        return default
    try:
        value = cast(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if value <= 0:
        raise ConfigError(f"{name} must be greater than 0, got {raw!r}")
    return value


@dataclass(frozen=True)
class LLMSettings:
    provider: str
    model: str
    api_key: str = field(repr=False)
    base_url: str | None


@dataclass(frozen=True)
class NLQSettings:
    max_rows: int = 200
    timeout_seconds: float = 15.0
    max_repairs: int = 2
    audit_log: str | None = None


@dataclass(frozen=True)
class SpeechSettings:
    whisper_model: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"


@dataclass(frozen=True)
class RepoSettings:
    repos_root: Path | None
    workspace: Path
    clone_timeout: float = 180.0


def load_llm_settings(
    env: Mapping[str, str] | None = None, dotenv_dir: Path | None = None
) -> LLMSettings:
    env = _env(env, dotenv_dir)
    provider = (env.get("LLM_PROVIDER") or "openai").strip().lower()
    if provider not in PROVIDERS:
        raise ConfigError(f"LLM_PROVIDER must be one of {', '.join(PROVIDERS)}, got {provider!r}")
    key_var = "OPENAI_API_KEY" if provider == "openai" else "OPENROUTER_API_KEY"
    api_key = (env.get(key_var) or "").strip()
    if not api_key:
        raise ConfigError(f"{key_var} is not set (needed for LLM_PROVIDER={provider})")
    base_url = None
    if provider == "openrouter":
        base_url = env.get("OPENROUTER_BASE_URL") or OPENROUTER_BASE_URL
    return LLMSettings(
        provider=provider,
        model=(env.get("LLM_MODEL") or "gpt-4o-mini").strip(),
        api_key=api_key,
        base_url=base_url,
    )


def load_nlq_settings(
    env: Mapping[str, str] | None = None, dotenv_dir: Path | None = None
) -> NLQSettings:
    env = _env(env, dotenv_dir)
    repairs_raw = (env.get("NLQ_MAX_REPAIRS") or "").strip()
    if repairs_raw:
        try:
            repairs = int(repairs_raw)
        except ValueError:
            raise ConfigError(f"NLQ_MAX_REPAIRS must be a number, got {repairs_raw!r}") from None
        if repairs < 0:
            raise ConfigError(f"NLQ_MAX_REPAIRS must be 0 or more, got {repairs_raw!r}")
    else:
        repairs = 2
    return NLQSettings(
        max_rows=_number(env, "NLQ_MAX_ROWS", 200, int),
        timeout_seconds=_number(env, "NLQ_TIMEOUT_SECONDS", 15.0),
        max_repairs=repairs,
        audit_log=(env.get("NLQ_AUDIT_LOG") or "").strip() or None,
    )


def load_speech_settings(
    env: Mapping[str, str] | None = None, dotenv_dir: Path | None = None
) -> SpeechSettings:
    """Local Whisper settings; never needs an API key."""
    env = _env(env, dotenv_dir)
    return SpeechSettings(
        whisper_model=(env.get("WHISPER_MODEL") or "small").strip(),
        device=(env.get("WHISPER_DEVICE") or "cpu").strip(),
        compute_type=(env.get("WHISPER_COMPUTE_TYPE") or "int8").strip(),
    )


def load_repo_settings(
    env: Mapping[str, str] | None = None, dotenv_dir: Path | None = None
) -> RepoSettings:
    env = _env(env, dotenv_dir)
    root = (env.get("CODEGRAPH_REPOS_ROOT") or "").strip()
    workspace = (env.get("CODEGRAPH_WORKSPACE") or "").strip() or ".codegraph/clones"
    return RepoSettings(
        repos_root=Path(root) if root else None,
        workspace=Path(workspace),
        clone_timeout=_number(env, "CODEGRAPH_CLONE_TIMEOUT_SECONDS", 180.0),
    )
