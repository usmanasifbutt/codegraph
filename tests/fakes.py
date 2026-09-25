"""Test doubles shared by engine and UI tests."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from codegraph.nlq.llm import GeneratedQuery
from codegraph.store.readonly import QueryRejected, Rows, check_text


class FakeQueryLLM:
    """Returns scripted GeneratedQuery objects in order; records every call."""

    def __init__(self, replies: list[GeneratedQuery | Exception], answer: str | Callable = "ok"):
        self.replies = list(replies)
        self.answer_text = answer
        self.generate_calls: list[tuple[str, str, list[tuple[str, str]]]] = []
        self.answer_calls: list[dict[str, Any]] = []

    def generate(self, question, repo, feedback):
        self.generate_calls.append((question, repo, list(feedback)))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply

    def answer(self, question, cypher, rows, truncated) -> Iterator[str]:
        self.answer_calls.append(
            {"question": question, "cypher": cypher, "rows": rows, "truncated": truncated}
        )
        text = self.answer_text(rows, truncated) if callable(self.answer_text) else self.answer_text
        yield from text.split(" ")[:1]
        rest = text[len(text.split(" ")[0]) :]
        if rest:
            yield rest


def q(cypher: str, explanation: str = "") -> GeneratedQuery:
    return GeneratedQuery(answerable=True, cypher=cypher, explanation=explanation)


class FakeExecutor:
    """Applies the text checks, then answers from a {query: Rows | Exception} table."""

    def __init__(
        self, table: dict[str, Rows | Exception] | None = None, default: Rows | None = None
    ):
        self.table = table or {}
        self.default = default or Rows(["x"], [{"x": 1}], False)
        self.calls: list[tuple[str, dict, str]] = []

    def run(self, query, params, *, origin, repo):
        self.calls.append((query, params, origin))
        verdict = check_text(query)
        if not verdict.ok:
            raise QueryRejected(verdict.reason, verdict.repairable)
        outcome = self.table.get(query, self.default)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


# -- UI -------------------------------------------------------------------------------
class FakeAudio:
    def __init__(self, data: bytes):
        self._data = data

    def getvalue(self) -> bytes:
        return self._data


class FakeChatValue:
    """Mimics streamlit's ChatInputValue (text + audio) for accept_audio chat inputs."""

    def __init__(self, text: str = "", audio: bytes | None = None):
        self.text = text
        self.audio = FakeAudio(audio) if audio is not None else None


class FakeServices:
    """Same interface as codegraph.ui.services.Services, backed by fakes."""

    def __init__(
        self,
        *,
        repos=None,
        llm=None,
        executor=None,
        llm_error=None,
        neo4j_error=None,
        repos_root=None,
        voice_error=None,
        transcript: str | Exception = "hello",
    ):
        from codegraph.config import NLQSettings, RepoSettings
        from codegraph.nlq.engine import Engine

        self.neo4j_error, self.llm_error, self.voice_error = neo4j_error, llm_error, voice_error
        self.llm_label = "fake · model"
        self.repo_settings = RepoSettings(repos_root=repos_root, workspace=Path("ws"))
        self.repos = list(repos or [])
        self.llm = llm or FakeQueryLLM([q("MATCH (c:Class {repo: $repo}) RETURN c.name AS name")])
        self.executor = executor or FakeExecutor()
        self.engine = Engine(self.llm, self.executor, NLQSettings(max_repairs=2))
        self.index_calls: list[tuple] = []
        self.index_outcome: Any = None  # Summary | Exception
        self.next_input: Any = None
        self.chat_input_calls: list[dict] = []
        self.transcript = transcript
        self.voice_model_loaded = False
        self.transcribed: list[bytes] = []

    # engine
    def prepare(self, question, repo):
        return self.engine.prepare(question, repo)

    def prepare_cypher(self, cypher, repo, question):
        return self.engine.prepare_cypher(cypher, repo, question)

    def answer_stream(self, result):
        return self.engine.answer_stream(result)

    # repos
    def list_repos(self):
        return list(self.repos)

    def _index(self, kind, name, progress, **kw):
        self.index_calls.append((kind, name, kw))
        progress("parse", "Parsing ...")
        if isinstance(self.index_outcome, Exception):
            raise self.index_outcome
        progress("write", "Writing ...")
        summary = self.index_outcome or make_summary(name)
        if not any(r["name"] == name for r in self.repos):
            self.repos.append({"name": name, "status": "complete", "root": "x", "source": kind})
        return summary

    def index_local(self, path, name, excludes, progress):
        return self._index("local", name, progress, path=path, excludes=excludes)

    def index_git(self, url, branch, name, excludes, progress):
        return self._index("git", name, progress, url=url, branch=branch, excludes=excludes)

    def reindex(self, name, progress):
        return self._index("reindex", name, progress)

    # voice
    def transcribe(self, audio):
        self.transcribed.append(audio)
        if isinstance(self.transcript, Exception):
            raise self.transcript
        return self.transcript

    # widgets
    def chat_input(self, placeholder, *, disabled, accept_audio):
        self.chat_input_calls.append(
            {"placeholder": placeholder, "disabled": disabled, "accept_audio": accept_audio}
        )
        value, self.next_input = self.next_input, None
        return None if disabled else value


def make_summary(name: str, parse_errors=None, warnings=None):
    from codegraph.model import Summary

    return Summary(
        repo=name,
        nodes={"File": 3, "Module": 4, "Class": 2, "Function": 9},
        relationships={"CONTAINS": 12, "IMPORTS": 5, "INHERITS": 1},
        parse_errors=parse_errors or [],
        warnings=warnings or [],
        elapsed_seconds=0.42,
    )
