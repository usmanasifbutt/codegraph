"""Connect repositories (local folders, public https Git URLs) and index them (design D11)."""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from codegraph.config import RepoSettings
from codegraph.model import Summary
from codegraph.pipeline import extract
from codegraph.store.schema import ensure_schema
from codegraph.store.writer import write_batch

Progress = Callable[[str, str], None]  # (step, detail)

PUBLIC_ONLY = "Only public repositories are supported (no credentials are used)."
_AUTH_OR_MISSING = re.compile(
    r"authentication failed|could not read (username|password)|terminal prompts disabled|"
    r"repository not found|not found|access denied|\b40[134]\b|permission denied",
    re.I,
)
_CREDENTIALS_IN_URL = re.compile(r"(https?://)[^/@\s]+@")
_INDEX_LOCK = threading.Lock()


class RepoError(Exception):
    """A user-facing problem connecting or indexing a repository."""


class IndexBusy(RepoError):
    pass


@dataclass(frozen=True)
class RepoSource:
    kind: str  # "local" | "git"
    path: Path  # folder that gets indexed
    url: str | None = None
    branch: str | None = None
    excludes: tuple[str, ...] = ()

    def repo_props(self) -> dict[str, Any]:
        return {
            "source": self.kind,
            "source_url": self.url,
            "branch": self.branch,
            "excludes": list(self.excludes),
        }


# -- local folders ---------------------------------------------------------------------
def resolve_local(path: str | Path, repos_root: Path | None = None) -> Path:
    raw = str(path).strip()
    if not raw:
        raise RepoError("Enter a folder path.")
    try:
        resolved = Path(raw).expanduser().resolve(strict=True)
    except (FileNotFoundError, OSError):
        raise RepoError(f"Folder not found: {raw}") from None
    if not resolved.is_dir():
        raise RepoError(f"Not a folder: {raw}")
    if repos_root is not None:
        root = Path(repos_root).resolve()
        if not resolved.is_relative_to(root):
            raise RepoError(f"{raw} is outside the repos root {root}; pick a folder under it.")
    return resolved


def list_root_folders(repos_root: Path | None) -> list[Path]:
    if repos_root is None or not Path(repos_root).is_dir():
        return []
    return sorted(
        p for p in Path(repos_root).iterdir() if p.is_dir() and not p.name.startswith(".")
    )


# -- git URLs --------------------------------------------------------------------------
def _safe(segment: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", segment).strip(".") or "_"


def validate_git_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        raise RepoError("Enter a Git URL.")
    if re.search(r"\s", url) or url.startswith("-"):
        raise RepoError("That does not look like a Git URL.")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise RepoError("Only https:// Git URLs are supported (not ssh, git, http or file).")
    if parts.username or parts.password or "@" in parts.netloc:
        raise RepoError("URLs with embedded credentials are not allowed. " + PUBLIC_ONLY)
    if not parts.hostname:
        raise RepoError("The URL has no host.")
    if parts.query or parts.fragment:
        raise RepoError("Remove the ?query / #fragment from the URL.")
    if not [s for s in parts.path.split("/") if s]:
        raise RepoError("The URL has no repository path.")
    return url


def repo_name_from_url(url: str) -> str:
    segments = [s for s in urlsplit(url).path.split("/") if s]
    name = segments[-1] if segments else "repo"
    return name[:-4] if name.endswith(".git") else name


def clone_target(workspace: Path, url: str) -> Path:
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    if segments and segments[-1].endswith(".git"):
        segments[-1] = segments[-1][:-4]
    return Path(workspace) / _safe(parts.hostname or "host") / Path(*[_safe(s) for s in segments])


def _rmtree(path: Path) -> None:
    def onexc(func, p, _exc):  # read-only files in .git on Windows
        os.chmod(p, stat.S_IWRITE)
        func(p)

    if not path.exists():
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=onexc)
    else:  # pragma: no cover
        shutil.rmtree(path, onerror=lambda f, p, _e: onexc(f, p, None))


def _redact(text: str) -> str:
    return _CREDENTIALS_IN_URL.sub(r"\1***@", text)


def clone(
    url: str,
    workspace: Path,
    *,
    branch: str | None = None,
    timeout: float = 180.0,
    git: str = "git",
    _allow_file_protocol: bool = False,  # tests only: clone a local bare repo
) -> Path:
    """Shallow-clone a public repo into the workspace; replaces an earlier clone of the URL."""
    if _allow_file_protocol:
        target = Path(workspace) / "local" / _safe(PurePosixPath(url).name.removesuffix(".git"))
        protocol = ["-c", "protocol.allow=never", "-c", "protocol.file.allow=always"]
    else:
        url = validate_git_url(url)
        target = clone_target(workspace, url)
        protocol = ["-c", "protocol.allow=never", "-c", "protocol.https.allow=always"]
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".tmp-{target.name}-{uuid.uuid4().hex[:8]}"
    cmd = [
        git,
        *protocol,
        "-c", "credential.helper=",
        "-c", "core.symlinks=false",
        "-c", "core.hooksPath=/dev/null",
        "clone", "--depth", "1", "--single-branch", "--no-recurse-submodules",
        *(["--branch", branch] if branch else []),
        "--", url, str(tmp),
    ]  # fmt: skip
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, env=env, check=False
        )
    except subprocess.TimeoutExpired:
        _rmtree(tmp)
        raise RepoError(f"Cloning {url} timed out after {timeout:g}s.") from None
    except FileNotFoundError:
        _rmtree(tmp)
        raise RepoError("git is not installed or not on PATH.") from None
    except BaseException:
        _rmtree(tmp)
        raise
    if proc.returncode != 0:
        _rmtree(tmp)
        lines = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = _redact(lines[-1]) if lines else ""
        if branch and re.search(r"remote branch .* not found", proc.stderr or "", re.I):
            raise RepoError(f"Branch '{branch}' not found in {url}.")
        if _AUTH_OR_MISSING.search(proc.stderr or ""):
            raise RepoError(
                f"Could not clone {url}: repository not found or private. {PUBLIC_ONLY}"
            )
        raise RepoError(f"Could not clone {url}: {detail or 'git failed'}")
    try:
        _rmtree(target)
        tmp.rename(target)
    except BaseException:
        _rmtree(tmp)
        raise
    return target


# -- indexing --------------------------------------------------------------------------
def repo_exists(driver: Any, name: str) -> bool:
    with driver.session() as session:
        return (
            session.run("MATCH (r:Repo {name: $n}) RETURN count(r) AS c", n=name).single()["c"] > 0
        )


def list_repos(driver: Any) -> list[dict[str, Any]]:
    with driver.session() as session:
        return [
            dict(r)
            for r in session.run(
                "MATCH (r:Repo) RETURN r.name AS name, r.status AS status, "
                "r.indexed_at AS indexed_at, r.source AS source, r.root AS root, "
                "r.source_url AS source_url, r.branch AS branch, r.file_count AS file_count, "
                "r.excludes AS excludes "
                "ORDER BY name"
            )
        ]


def _noop(_step: str, _detail: str) -> None:
    pass


def _index_locked(driver: Any, source: RepoSource, name: str, progress: Progress) -> Summary:
    started = time.monotonic()
    progress("parse", f"Parsing {source.path} ...")
    batch = extract(source.path, name, source.excludes)
    progress(
        "write",
        f"Writing {sum(batch.node_counts().values())} nodes, {len(batch.rels)} relationships ...",
    )
    ensure_schema(driver)
    write_batch(driver, batch, root=str(source.path), repo_props=source.repo_props())
    progress("done", "Indexed.")
    return Summary(
        repo=name,
        nodes=batch.node_counts(),
        relationships=batch.rel_counts(),
        parse_errors=batch.parse_errors,
        warnings=batch.warnings,
        elapsed_seconds=round(time.monotonic() - started, 3),
    )


def _locked(fn: Callable[[], Summary]) -> Summary:
    if not _INDEX_LOCK.acquire(blocking=False):
        raise IndexBusy("Another indexing job is running; try again when it finishes.")
    try:
        return fn()
    finally:
        _INDEX_LOCK.release()


def index_local(
    driver: Any, path: str | Path, name: str, settings: RepoSettings,
    excludes: Iterable[str] = (), progress: Progress = _noop,
) -> Summary:  # fmt: skip
    folder = resolve_local(path, settings.repos_root)
    source = RepoSource("local", folder, excludes=tuple(excludes))
    return _locked(lambda: _index_locked(driver, source, name, progress))


def index_git(
    driver: Any, url: str, name: str, settings: RepoSettings, branch: str | None = None,
    excludes: Iterable[str] = (), progress: Progress = _noop, **clone_kwargs: Any,
) -> Summary:  # fmt: skip
    def run() -> Summary:
        progress("clone", f"Cloning {url} ...")
        folder = clone(
            url,
            settings.workspace,
            branch=branch or None,
            timeout=settings.clone_timeout,
            **clone_kwargs,
        )
        source = RepoSource("git", folder, url=url, branch=branch or None, excludes=tuple(excludes))
        return _index_locked(driver, source, name, progress)

    return _locked(run)


def reindex(
    driver: Any, name: str, settings: RepoSettings, progress: Progress = _noop,
    **clone_kwargs: Any,
) -> Summary:  # fmt: skip
    """Re-index from what the Repo node recorded; the graph is untouched if the source is gone."""
    info = next((r for r in list_repos(driver) if r["name"] == name), None)
    if info is None:
        raise RepoError(f"Repository '{name}' is not indexed.")
    excludes = tuple(info.get("excludes") or ())
    if info.get("source") == "git" and info.get("source_url"):
        return index_git(
            driver,
            info["source_url"],
            name,
            settings,
            info.get("branch"),
            excludes=excludes,
            progress=progress,
            **clone_kwargs,
        )
    root = info.get("root")
    if not root or not Path(root).is_dir():
        raise RepoError(
            f"The recorded folder for '{name}' no longer exists here: {root}. "
            "Connect it again with its current path."
        )
    return index_local(driver, root, name, settings, excludes=excludes, progress=progress)
