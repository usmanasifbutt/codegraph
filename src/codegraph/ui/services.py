"""Everything the Streamlit page needs, behind one object (design D8).

The page only talks to a `Services` instance. Tests set `OVERRIDE` to a fake before running the
page with `AppTest`, which also lets them feed audio through `chat_input` (AppTest can only type
text into a real chat input).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from codegraph import repos
from codegraph.config import (
    ConfigError,
    ConnectError,
    connect,
    load_llm_settings,
    load_nlq_settings,
    load_repo_settings,
    load_settings,
    load_speech_settings,
)
from codegraph.model import Summary
from codegraph.nlq.audit import configure_audit_file
from codegraph.nlq.engine import DriverExecutor, Engine, QuestionResult
from codegraph.nlq.llm import LangChainQueryLLM
from codegraph.speech import ModelLoader, transcribe

OVERRIDE: Any = None  # tests: a fake with the same interface as Services

UI_CONNECT_TIMEOUT = 10.0


class Services:
    def __init__(self, dotenv_dir: Path | None = None) -> None:
        self.neo4j_error: str | None = None
        self.llm_error: str | None = None
        self.driver: Any = None
        self._engine: Engine | None = None
        try:
            neo = load_settings(dotenv_dir=dotenv_dir)
            neo = dataclasses.replace(
                neo, connect_timeout=min(neo.connect_timeout, UI_CONNECT_TIMEOUT)
            )
            self.neo4j_uri = neo.uri
            self.driver = connect(neo)
        except (ConfigError, ConnectError) as exc:
            self.neo4j_error = str(exc)
        try:
            self.nlq = load_nlq_settings(dotenv_dir=dotenv_dir)
            self.llm_settings = load_llm_settings(dotenv_dir=dotenv_dir)
            self.llm_label = f"{self.llm_settings.provider} · {self.llm_settings.model}"
        except ConfigError as exc:
            self.llm_error = str(exc)
            self.llm_label = "not configured"
        self.repo_settings = load_repo_settings(dotenv_dir=dotenv_dir)
        self.speech = ModelLoader(load_speech_settings(dotenv_dir=dotenv_dir))
        if not self.llm_error:
            configure_audit_file(self.nlq.audit_log)

    # -- engine ---------------------------------------------------------------------------
    @property
    def engine(self) -> Engine:
        if self._engine is None:
            if self.llm_error:
                raise ConfigError(self.llm_error)
            self._engine = Engine(
                LangChainQueryLLM(self.llm_settings),
                DriverExecutor(self.driver, self.nlq),
                self.nlq,
            )
        return self._engine

    def prepare(self, question: str, repo: str) -> QuestionResult:
        return self.engine.prepare(question, repo)

    def prepare_cypher(self, cypher: str, repo: str, question: str) -> QuestionResult:
        return self.engine.prepare_cypher(cypher, repo, question)

    def answer_stream(self, result: QuestionResult) -> Iterator[str]:
        return self.engine.answer_stream(result)

    # -- repositories ---------------------------------------------------------------------
    def list_repos(self) -> list[dict[str, Any]]:
        return repos.list_repos(self.driver)

    def index_local(self, path: str, name: str, excludes: list[str], progress) -> Summary:
        return repos.index_local(
            self.driver, path, name, self.repo_settings, excludes, progress=progress
        )

    def index_git(
        self, url: str, branch: str | None, name: str, excludes: list[str], progress
    ) -> Summary:
        return repos.index_git(
            self.driver, url, name, self.repo_settings, branch, excludes, progress=progress
        )

    def reindex(self, name: str, progress) -> Summary:
        return repos.reindex(self.driver, name, self.repo_settings, progress=progress)

    # -- voice ----------------------------------------------------------------------------
    @property
    def voice_error(self) -> str | None:
        return self.speech.error

    @property
    def voice_model_loaded(self) -> bool:
        return self.speech.loaded

    def transcribe(self, audio: bytes) -> str:
        return transcribe(self.speech.get(), audio)

    # -- widgets --------------------------------------------------------------------------
    def chat_input(self, placeholder: str, *, disabled: bool, accept_audio: bool) -> Any:
        import streamlit as st

        return st.chat_input(
            placeholder, key="question", disabled=disabled, accept_audio=accept_audio
        )
