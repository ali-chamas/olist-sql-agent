from __future__ import annotations

import pytest

from sqlagent.config import ConfigError, Settings, build_llm, load_dotenv
from sqlagent.llm import AnthropicClient, OpenAICompatClient
from sqlagent.prompts import TOOLS, system_prompt

ENV_KEYS = ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL", "DB_PATH", "MAX_STEPS")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # Keep a developer's real .env out of the tests.
    monkeypatch.setattr("sqlagent.config.ROOT", tmp_path)


def test_groq_is_the_default_provider(monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "k")
    settings = Settings.from_env()
    assert settings.provider == "groq" and settings.base_url == "https://api.groq.com/openai/v1"
    assert isinstance(build_llm(settings), OpenAICompatClient)


def test_anthropic_provider_builds_the_claude_client(monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_API_KEY", "k")
    client = build_llm(Settings.from_env())
    assert isinstance(client, AnthropicClient) and client.model.startswith("claude")


def test_ollama_needs_a_model_but_no_key(monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    with pytest.raises(ConfigError, match="LLM_MODEL"):
        Settings.from_env()
    monkeypatch.setenv("LLM_MODEL", "some-local-model")
    assert Settings.from_env().api_key == ""


def test_missing_key_and_unknown_provider_are_explained(monkeypatch) -> None:
    with pytest.raises(ConfigError, match="LLM_API_KEY"):
        Settings.from_env()
    monkeypatch.setenv("LLM_PROVIDER", "nope")
    with pytest.raises(ConfigError, match="Unknown LLM_PROVIDER"):
        Settings.from_env()


def test_custom_base_url_works_for_any_openai_compatible_server(monkeypatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "custom")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.internal/v1")
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("LLM_API_KEY", "k")
    assert Settings.from_env().base_url == "https://llm.internal/v1"


def test_dotenv_is_read_without_overriding_real_environment(monkeypatch, tmp_path) -> None:
    env = tmp_path / ".env"
    env.write_text("# comment\nLLM_MODEL='from-file'\nLLM_API_KEY=file-key\n\nBROKEN LINE\n")
    monkeypatch.setenv("LLM_API_KEY", "real-key")
    load_dotenv(env)
    settings = Settings.from_env()
    assert settings.model == "from-file" and settings.api_key == "real-key"


def test_prompt_contains_every_table_and_the_three_tools() -> None:
    prompt = system_prompt()
    for table in ("sales", "customers", "products", "sellers", "dates"):
        assert f"TABLE {table}:" in prompt
    assert "COUNT(DISTINCT order_id)" in prompt
    assert [t.name for t in TOOLS] == ["run_sql", "submit_answer", "cannot_answer"]
    assert all(t.parameters["type"] == "object" for t in TOOLS)
