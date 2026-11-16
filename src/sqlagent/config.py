"""Configuration from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .agent import SQLAgent
from .app import AppConfig
from .db import ReadOnlyDatabase
from .llm import AnthropicClient, LLMClient, OpenAICompatClient
from .workspace import Workspace

ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Preset:
    base_url: str
    default_model: str  # empty means LLM_MODEL is required
    needs_key: bool = True


# All of these except "anthropic" speak the OpenAI /chat/completions format.
PRESETS: dict[str, Preset] = {
    "groq": Preset("https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
    "gemini": Preset("https://generativelanguage.googleapis.com/v1beta/openai", "gemini-flash-latest"),
    "openrouter": Preset("https://openrouter.ai/api/v1", "openrouter/free"),
    "ollama": Preset("http://localhost:11434/v1", "", needs_key=False),
    "openai": Preset("https://api.openai.com/v1", ""),
    "anthropic": Preset("https://api.anthropic.com", "claude-haiku-5-5"),
}


class ConfigError(Exception):
    pass


def load_dotenv(path: Path) -> None:
    """Minimal .env reader: KEY=VALUE lines, '#' comments, no overriding of real env vars."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    provider: str
    base_url: str
    api_key: str
    model: str
    db_path: Path
    max_steps: int
    max_rows: int
    query_timeout_s: float
    app_db_path: Path

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(ROOT / ".env")
        provider = os.environ.get("LLM_PROVIDER", "groq").strip().lower()
        base_url = os.environ.get("LLM_BASE_URL", "").strip()
        if provider not in PRESETS and not base_url:
            known = ", ".join(sorted(PRESETS))
            raise ConfigError(f"Unknown LLM_PROVIDER {provider!r}. Use one of: {known}, or set LLM_BASE_URL.")
        preset = PRESETS.get(provider, Preset(base_url, ""))
        model = os.environ.get("LLM_MODEL", "").strip() or preset.default_model
        if not model:
            raise ConfigError(f"Set LLM_MODEL for provider {provider!r}.")
        api_key = os.environ.get("LLM_API_KEY", "").strip()
        if preset.needs_key and not api_key:
            raise ConfigError(f"Set LLM_API_KEY for provider {provider!r}.")
        return cls(
            provider=provider,
            base_url=base_url or preset.base_url,
            api_key=api_key,
            model=model,
            db_path=Path(os.environ.get("DB_PATH", str(ROOT / "data" / "olist.db"))),
            max_steps=int(os.environ.get("MAX_STEPS", "6")),
            max_rows=int(os.environ.get("MAX_ROWS", "200")),
            query_timeout_s=float(os.environ.get("QUERY_TIMEOUT_S", "5")),
            app_db_path=Path(os.environ.get("APP_DB_PATH", str(ROOT / "data" / "app.db"))),
        )


def build_llm(settings: Settings) -> LLMClient:
    if settings.provider == "anthropic":
        return AnthropicClient(api_key=settings.api_key, model=settings.model, base_url=settings.base_url)
    return OpenAICompatClient(base_url=settings.base_url, api_key=settings.api_key, model=settings.model)


def build_database(settings: Settings) -> ReadOnlyDatabase:
    return ReadOnlyDatabase(settings.db_path, max_rows=settings.max_rows, timeout_s=settings.query_timeout_s)


def build_workspace(settings: Settings) -> Workspace:
    """The writable app database for saved dashboards (separate from the read-only data)."""
    return Workspace(settings.app_db_path)


def build_agent(settings: Settings | None = None) -> SQLAgent:
    settings = settings or Settings.from_env()
    return SQLAgent(build_llm(settings), build_database(settings), max_steps=settings.max_steps)


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def app_config() -> AppConfig:
    """Web settings. The defaults suit local use; a public demo sets a budget and TRUST_PROXY."""
    return AppConfig(
        ask_per_minute=int(os.environ.get("ASK_PER_MINUTE", "10")),
        query_per_minute=int(os.environ.get("QUERY_PER_MINUTE", "60")),
        daily_token_budget=int(os.environ.get("DAILY_TOKEN_BUDGET", "0")),
        cache_ttl_s=float(os.environ.get("CACHE_TTL_S", "86400")),
        trust_proxy=_flag("TRUST_PROXY"),
        warm_up=True,
        password=os.environ.get("APP_PASSWORD", ""),
    )
