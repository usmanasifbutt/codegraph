import re
from pathlib import Path

import pytest

from codegraph.config import (
    OPENROUTER_BASE_URL,
    ConfigError,
    Settings,
    load_llm_settings,
    load_nlq_settings,
    load_repo_settings,
    load_speech_settings,
)

ROOT = Path(__file__).parent.parent


def test_llm_defaults_openai():
    s = load_llm_settings({"OPENAI_API_KEY": "sk-test"})
    assert (s.provider, s.model, s.base_url) == ("openai", "gpt-4o-mini", None)
    assert "sk-test" not in repr(s)


def test_openrouter_selected():
    s = load_llm_settings(
        {"LLM_PROVIDER": "OpenRouter", "OPENROUTER_API_KEY": "or-key", "LLM_MODEL": "x/y"}
    )
    assert (s.provider, s.model, s.api_key, s.base_url) == (
        "openrouter",
        "x/y",
        "or-key",
        OPENROUTER_BASE_URL,
    )


def test_missing_key_names_variable():
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        load_llm_settings({"LLM_PROVIDER": "openai", "OPENAI_API_KEY": ""})
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        load_llm_settings({"LLM_PROVIDER": "openrouter", "OPENAI_API_KEY": "sk"})


def test_unknown_provider():
    with pytest.raises(ConfigError, match="LLM_PROVIDER"):
        load_llm_settings({"LLM_PROVIDER": "gemini", "OPENAI_API_KEY": "sk"})


def test_nlq_defaults_and_overrides():
    assert load_nlq_settings({}) == load_nlq_settings({"NLQ_MAX_ROWS": ""})
    s = load_nlq_settings(
        {"NLQ_MAX_ROWS": "50", "NLQ_TIMEOUT_SECONDS": "2.5", "NLQ_MAX_REPAIRS": "0"}
    )
    assert (s.max_rows, s.timeout_seconds, s.max_repairs) == (50, 2.5, 0)


@pytest.mark.parametrize(
    "env",
    [
        {"NLQ_MAX_ROWS": "lots"},
        {"NLQ_MAX_ROWS": "0"},
        {"NLQ_TIMEOUT_SECONDS": "-1"},
        {"NLQ_MAX_REPAIRS": "-1"},
        {"CODEGRAPH_CLONE_TIMEOUT_SECONDS": "x"},
    ],
)
def test_invalid_numbers(env):
    with pytest.raises(ConfigError, match=next(iter(env))):
        load_nlq_settings(env)
        load_repo_settings(env)


def test_speech_settings_need_no_key():
    s = load_speech_settings({})
    assert (s.whisper_model, s.device, s.compute_type) == ("small", "cpu", "int8")


def test_repo_settings():
    s = load_repo_settings({})
    assert s.repos_root is None and s.workspace == Path(".codegraph/clones")
    s = load_repo_settings({"CODEGRAPH_REPOS_ROOT": "/repos", "CODEGRAPH_WORKSPACE": "/workspace"})
    assert s.repos_root == Path("/repos") and s.workspace == Path("/workspace")


def test_neo4j_password_not_in_repr():
    assert "hunter2" not in repr(Settings("bolt://x", "neo4j", "hunter2"))


def test_env_example_lists_every_variable():
    source = (ROOT / "src/codegraph/config.py").read_text()
    used = set(re.findall(r'env\.get\("([A-Z0-9_]+)"', source))
    used |= set(re.findall(r'_number\(env, "([A-Z0-9_]+)"', source))
    used |= {"OPENAI_API_KEY", "OPENROUTER_API_KEY"}
    example = (ROOT / ".env.example").read_text()
    documented = set(re.findall(r"^#?\s*([A-Z0-9_]+)=", example, re.M))
    assert used - documented == set()
