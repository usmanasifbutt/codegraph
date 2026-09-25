"""codegraph web UI: connect and index repositories, then ask questions about them.

Run with `codegraph ui` (or `streamlit run src/codegraph/ui/app.py`). All backend work goes
through `codegraph.ui.services` so tests can swap in fakes.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import streamlit as st

from codegraph.repos import (
    RepoError,
    list_root_folders,
    repo_name_from_url,
    resolve_local,
    validate_git_url,
)
from codegraph.speech import AudioError
from codegraph.ui import services as services_module

# Streamlit's file watcher walks every imported module looking for sources to hot-reload;
# ctranslate2/transformers' optional submodules make that noisy. Same fix as voice-notes.
logging.getLogger("streamlit.watcher.local_sources_watcher").setLevel(logging.ERROR)

st.set_page_config(page_title="codegraph", page_icon=":material/hub:", layout="wide")


@st.cache_resource(show_spinner="Connecting to Neo4j ...")
def _real_services():
    return services_module.Services()


def get_services():
    return services_module.OVERRIDE or _real_services()


svc = get_services()
state = st.session_state
state.setdefault("history", [])  # list of dicts: {"question", "spoken", "result"}
state.setdefault("index_job", None)  # pending indexing job (runs at the end of the script)
state.setdefault("last_index", None)  # {"summary": dict} | {"error": str}
state.setdefault("voice_message", None)

st.title("codegraph")
st.caption("Ask questions about a Python codebase. Answers come from its code graph in Neo4j.")

# -- startup checks -----------------------------------------------------------------------
if svc.neo4j_error:
    st.error(f"Neo4j is unreachable, so nothing can be shown.\n\n{svc.neo4j_error}")
    if st.button("Retry connection"):
        _real_services.clear()
        st.rerun()
    st.stop()

busy = state.index_job is not None
repo_rows = svc.list_repos()
repo_names = [r["name"] for r in repo_rows]
if "pending_select" in state:  # a repo indexed in the previous run becomes the selection
    state.repo = state.pop("pending_select")
if state.get("repo") not in repo_names:
    state.repo = repo_names[0] if repo_names else None


def _repo_label(name: str) -> str:
    info = next(r for r in repo_rows if r["name"] == name)
    mark = "" if info.get("status") == "complete" else " ⚠ incomplete"
    return f"{name}{mark}"


# -- sidebar: repositories, settings, history ---------------------------------------------
with st.sidebar:
    st.header("Repository")
    if repo_names:
        st.selectbox(
            "Ask questions about",
            repo_names,
            key="repo",
            format_func=_repo_label,
            disabled=busy,
        )
        info = next(r for r in repo_rows if r["name"] == state.repo)
        source = info.get("source_url") or info.get("root") or "unknown source"
        st.caption(
            f"Status: {info.get('status') or 'unknown'} · indexed {info.get('indexed_at') or '—'}"
            f"  \nSource: {source}" + (f" (branch {info['branch']})" if info.get("branch") else "")
        )
        if st.button("Re-index", icon=":material/refresh:", disabled=busy, key="reindex"):
            state.index_job = {"kind": "reindex", "name": state.repo}
            st.rerun()
    else:
        st.info("No repository indexed yet.")

    st.divider()
    st.caption(f"LLM: {svc.llm_label}")
    if svc.voice_error:
        st.warning(svc.voice_error)
    else:
        st.caption("Voice: local Whisper (audio stays on this machine)")
    if st.button("Clear conversation", icon=":material/delete_sweep:", disabled=busy):
        state.history = []
        st.rerun()


# -- connect panel ------------------------------------------------------------------------
def _excludes(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _replace_ok(name: str, key: str) -> bool:
    if name and name in repo_names:
        return st.checkbox(
            f"Replace the existing graph for '{name}'", key=f"{key}_replace", disabled=busy
        )
    return True


with st.expander(
    "Connect repository",
    expanded=not repo_names or busy or bool(state.last_index),
    icon=":material/add_link:",
):
    local_tab, git_tab = st.tabs(["Local folder", "Git URL"])
    with local_tab:
        root = svc.repo_settings.repos_root
        path_value = ""
        if root is not None:
            choices = [str(p) for p in list_root_folders(root)]
            picked = st.selectbox(
                f"Folder under {root}",
                ["(type a path below)", *choices],
                key="local_pick",
                disabled=busy,
            )
            if picked != "(type a path below)":
                path_value = picked
        typed = st.text_input(
            "Folder path",
            key="local_path",
            placeholder=str(root / "my-project") if root else r"D:\Projects\my-project",
            disabled=busy,
        )
        path_value = typed.strip() or path_value
        path_error = None
        if path_value:
            try:
                resolve_local(path_value, root)
            except RepoError as exc:
                path_error = str(exc)
                st.error(path_error)
        default_name = Path(path_value).name if path_value and not path_error else ""
        local_name = (
            st.text_input(
                "Repository name",
                key="local_name",
                placeholder=default_name or "name",
                disabled=busy,
            ).strip()
            or default_name
        )
        local_ex = st.text_area(
            "Exclude globs (one per line)",
            key="local_ex",
            placeholder="migrations/**",
            height=68,
            disabled=busy,
        )
        ok = _replace_ok(local_name, "local")
        if st.button(
            "Index folder",
            key="index_local",
            type="primary",
            disabled=busy or not path_value or bool(path_error) or not local_name or not ok,
        ):
            state.index_job = {
                "kind": "local",
                "path": path_value,
                "name": local_name,
                "excludes": _excludes(local_ex),
            }
            st.rerun()

    with git_tab:
        url = st.text_input(
            "Public https Git URL",
            key="git_url",
            placeholder="https://github.com/pallets/itsdangerous",
            disabled=busy,
        ).strip()
        branch = st.text_input(
            "Branch (optional)", key="git_branch", placeholder="default branch", disabled=busy
        ).strip()
        url_error = None
        if url:
            try:
                validate_git_url(url)
            except RepoError as exc:
                url_error = str(exc)
                st.error(url_error)
        default_git_name = repo_name_from_url(url) if url and not url_error else ""
        git_name = (
            st.text_input(
                "Repository name",
                key="git_name",
                placeholder=default_git_name or "name",
                disabled=busy,
            ).strip()
            or default_git_name
        )
        git_ex = st.text_area(
            "Exclude globs (one per line)", key="git_ex", height=68, disabled=busy
        )
        ok = _replace_ok(git_name, "git")
        st.caption("Only public repositories. The latest commit is shallow-cloned.")
        if st.button(
            "Clone and index",
            key="index_git",
            type="primary",
            disabled=busy or not url or bool(url_error) or not git_name or not ok,
        ):
            state.index_job = {
                "kind": "git",
                "url": url,
                "branch": branch or None,
                "name": git_name,
                "excludes": _excludes(git_ex),
            }
            st.rerun()

    status_slot = st.container()
    last = state.last_index
    if last and not busy:
        with status_slot:
            if last.get("error"):
                st.error(last["error"])
            else:
                s = last["summary"]
                st.success(f"Indexed '{s['repo']}' in {s['elapsed_seconds']:.1f}s")
                counts = {**s["nodes"], **s["relationships"]}
                st.dataframe(
                    [{"kind": k, "count": v} for k, v in counts.items()],
                    hide_index=True,
                    width="content",
                )
                if s["parse_errors"]:
                    with st.expander(f"Parse errors ({len(s['parse_errors'])})"):
                        for e in s["parse_errors"]:
                            st.markdown(f"- `{e['path']}`: {e['error']}")
                if s["warnings"]:
                    with st.expander(f"Warnings ({len(s['warnings'])})"):
                        for w in s["warnings"]:
                            st.markdown(f"- {w}")


# -- conversation -------------------------------------------------------------------------
def _cell(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _table(result) -> None:
    if not result.columns:
        return
    st.dataframe(
        [{c: _cell(row.get(c)) for c in result.columns} for row in result.rows],
        hide_index=True,
        width="stretch",
    )


def _caption(result) -> str:
    parts = [f"{len(result.rows)} row{'s' if len(result.rows) != 1 else ''}"]
    if result.truncated:
        parts[0] += " (truncated at the row limit)"
    if result.origin == "user-edited":
        parts.append("edited query")
    else:
        parts.append(f"{result.repairs} repair{'s' if result.repairs != 1 else ''}")
    parts.append(f"{result.elapsed:.1f}s")
    return " · ".join(parts)


def _render_result(i: int, entry: dict[str, Any], stream: bool = False) -> None:
    result = entry["result"]
    if result.error:
        st.error(result.error)
        if result.cypher:
            st.code(result.cypher, language="cypher")
        return
    if stream and result.executed:
        result.answer = st.write_stream(svc.answer_stream(result)) or result.answer
        if result.error:
            st.error(result.error)
    else:
        st.markdown(result.answer or "_(no answer)_")
    if not result.executed:
        return
    with st.expander("Cypher and results", expanded=False, icon=":material/code:"):
        if result.explanation:
            st.caption(result.explanation)
        st.code(result.cypher, language="cypher")
        edited = st.text_area(
            "Edit query", value=result.cypher, key=f"edit_{i}", height=120, disabled=busy
        )
        if st.button("Run edited query", key=f"run_{i}", disabled=busy or not state.repo):
            with st.spinner("Running edited query ..."):
                new = svc.prepare_cypher(edited, result.repo, result.question)
                for _ in svc.answer_stream(new):
                    pass
            state.history.append(
                {"question": result.question, "spoken": False, "result": new, "edited": True}
            )
            st.rerun()
    _table(result)
    st.caption(_caption(result))


for i, entry in enumerate(state.history):
    with st.chat_message("user"):
        prefix = "🎤 *(spoken)* " if entry.get("spoken") else ""
        prefix += "✏️ *(edited query)* " if entry.get("edited") else ""
        st.markdown(prefix + entry["question"])
    with st.chat_message("assistant"):
        _render_result(i, entry)

if state.voice_message:
    st.error(state.voice_message)
    state.voice_message = None

# -- question input -----------------------------------------------------------------------
if not repo_names:
    st.info("Connect and index a repository above to start asking questions.")
if svc.llm_error:
    st.warning(f"Questions are disabled until the LLM is configured: {svc.llm_error}")

can_ask = bool(state.repo) and not svc.llm_error and not busy
value = svc.chat_input(
    f"Ask about {state.repo}" if state.repo else "Connect a repository first",
    disabled=not can_ask,
    accept_audio=can_ask and not svc.voice_error,
)

if value and can_ask:
    audio = getattr(value, "audio", None)
    text = (getattr(value, "text", value) if not isinstance(value, str) else value) or ""
    question, spoken = text.strip(), False
    if audio is not None:
        data = audio.getvalue() if hasattr(audio, "getvalue") else bytes(audio)
        label = (
            "Transcribing ..."
            if svc.voice_model_loaded
            else "Loading transcription model (first run only) ..."
        )
        try:
            with st.spinner(label):
                question, spoken = svc.transcribe(data), True
        except AudioError as exc:
            state.voice_message = str(exc)
            question = ""
            st.rerun()
    if question:
        state.last_index = None  # the indexing summary has been seen; collapse the panel
        with st.chat_message("user"):
            st.markdown(("🎤 *(spoken)* " if spoken else "") + question)
        with st.chat_message("assistant"):
            with st.spinner("Writing a Cypher query ..."):
                result = svc.prepare(question, state.repo)
            entry = {"question": question, "spoken": spoken, "result": result}
            _render_result(len(state.history), entry, stream=True)
        state.history.append(entry)

# -- pending indexing job (runs last so every control above is already disabled) ----------
job = state.index_job
if job:
    with status_slot:
        with st.status(f"Indexing '{job['name']}' ...", expanded=True) as status:

            def progress(step: str, detail: str) -> None:
                status.write(detail)

            try:
                if job["kind"] == "local":
                    summary = svc.index_local(job["path"], job["name"], job["excludes"], progress)
                elif job["kind"] == "git":
                    summary = svc.index_git(
                        job["url"], job["branch"], job["name"], job["excludes"], progress
                    )
                else:
                    summary = svc.reindex(job["name"], progress)
            except RepoError as exc:
                status.update(label="Indexing failed", state="error")
                state.last_index = {"error": str(exc)}
            except Exception as exc:  # database/write failures
                status.update(label="Indexing failed", state="error")
                state.last_index = {"error": f"Indexing failed: {type(exc).__name__}: {exc}"}
            else:
                status.update(label=f"Indexed '{job['name']}'", state="complete")
                state.last_index = {"summary": summary.to_dict()}
                state.pending_select = job["name"]
    state.index_job = None
    st.rerun()
