"""LLM seam for the engine (design D1/D2): a small protocol plus the LangChain implementation."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any, Protocol

from pydantic import BaseModel, Field

from codegraph.config import LLMSettings
from codegraph.nlq.schema_prompt import answer_messages, generation_messages


class GeneratedQuery(BaseModel):
    """Structured output of the generation step."""

    answerable: bool = Field(description="False when the graph cannot answer the question")
    cypher: str = Field(default="", description="One read-only Cypher statement using $repo")
    explanation: str = Field(default="", description="One sentence describing the query")
    reason: str = Field(default="", description="Why the question is not answerable, if so")


class LLMOutputError(Exception):
    """The model's reply could not be turned into a GeneratedQuery."""


class QueryLLM(Protocol):
    def generate(
        self, question: str, repo: str, feedback: list[tuple[str, str]]
    ) -> GeneratedQuery: ...

    def answer(
        self, question: str, cypher: str, rows: list[dict[str, Any]], truncated: bool
    ) -> Iterator[str]: ...


FENCE = re.compile(r"```(?:cypher)?\s*\n(.*?)```", re.S | re.I)


def parse_fallback(text: str) -> GeneratedQuery:
    """Recover a query from a free-form reply: JSON object first, then a fenced block."""
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return GeneratedQuery.model_validate(json.loads(text[start : end + 1]))
        except (ValueError, TypeError):
            pass
    m = FENCE.search(text)
    if m and m.group(1).strip():
        return GeneratedQuery(answerable=True, cypher=m.group(1).strip())
    raise LLMOutputError("the model did not return a Cypher query")


def build_chat(settings: LLMSettings, **overrides: Any):
    """ChatOpenAI for OpenAI or OpenRouter (OpenAI-compatible base_url)."""
    from langchain_openai import ChatOpenAI

    kwargs: dict[str, Any] = {
        "model": settings.model,
        "api_key": settings.api_key,
        "temperature": 0,
        "timeout": 60,
        "max_retries": 2,
    }
    if settings.base_url:
        kwargs["base_url"] = settings.base_url
    kwargs.update(overrides)
    return ChatOpenAI(**kwargs)


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # content blocks
        return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
    return str(content or "")


class LangChainQueryLLM:
    def __init__(self, settings: LLMSettings, chat: Any | None = None) -> None:
        self.settings = settings
        self.chat = chat if chat is not None else build_chat(settings)

    def generate(self, question: str, repo: str, feedback: list[tuple[str, str]]) -> GeneratedQuery:
        messages = generation_messages(question, repo, feedback)
        structured = self.chat.with_structured_output(
            GeneratedQuery, method="function_calling", include_raw=True
        )
        result = structured.invoke(messages)
        parsed = result.get("parsed") if isinstance(result, dict) else result
        if isinstance(parsed, GeneratedQuery):
            return parsed
        raw = result.get("raw") if isinstance(result, dict) else None
        return parse_fallback(_text(getattr(raw, "content", "")))

    def answer(
        self, question: str, cypher: str, rows: list[dict[str, Any]], truncated: bool
    ) -> Iterator[str]:
        for chunk in self.chat.stream(answer_messages(question, cypher, rows, truncated)):
            piece = _text(chunk.content)
            if piece:
                yield piece
