"""Settings loaded from environment variables / the project-root ``.env``."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _split_csv(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- LLM provider selection -------------------------------------------
    # Primary provider. The other provider's models are appended to the chain
    # when its API key is set, so a provider-wide outage still gets an answer.
    llm_provider: Literal["gemini", "groq"] = "gemini"

    gemini_api_key: str | None = None
    gemini_base_url: str | None = None  # e.g. https://generativelanguage.googleapis.com/v1beta
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_fallback_models: str = ""  # comma-separated, tried in order after gemini_model

    groq_api_key: str | None = None
    groq_model: str = "openai/gpt-oss-120b"
    groq_fallback_models: str = ""  # comma-separated, tried in order after groq_model

    # ---- Fallback behaviour ------------------------------------------------
    llm_temperature: float = 0.0
    llm_timeout_seconds: float = 60.0
    # Attempts on the *same* model before moving to the next one.
    llm_retries_per_model: int = Field(default=1, ge=1)
    # After a model fails with a transient error (429 / 5xx / timeout) it is skipped for this long.
    llm_cooldown_seconds: float = Field(default=30.0, ge=0)
    # After a model turns out to be unknown or retired it is skipped for this long.
    llm_unavailable_cooldown_seconds: float = Field(default=600.0, ge=0)
    # Reasoning shown by the agent: Gemini thinks (and returns a summary of its thoughts) only with a
    # thinking budget. 0 turns Gemini's thoughts off; Groq's reasoning models return theirs regardless.
    llm_thinking_budget: int = Field(default=1024, ge=0)

    # ---- Tracing (optional) ------------------------------------------------
    langsmith_tracing: bool = False
    langsmith_api_key: str | None = None
    langsmith_project: str = "scrapper"
    langsmith_endpoint: str = "https://api.smith.langchain.com"

    # ---- Hashtag maps ------------------------------------------------------
    # Embedded graph database (LadybugDB) holding the hashtag maps; relative to the project root.
    graph_db_path: Path = Path("data/hashtag_graph")

    @field_validator("gemini_api_key", "groq_api_key", "gemini_base_url", "langsmith_api_key", mode="before")
    @classmethod
    def _blank_to_none(cls, v):  # noqa: ANN001
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("graph_db_path", mode="after")
    @classmethod
    def _under_project(cls, v: Path) -> Path:
        return v if v.is_absolute() else PROJECT_ROOT / v

    # ---- Derived helpers ---------------------------------------------------
    @property
    def gemini_models(self) -> list[str]:
        """Primary Gemini model followed by its fallbacks, de-duplicated."""
        return _dedupe([self.gemini_model, *_split_csv(self.gemini_fallback_models)])

    @property
    def groq_models(self) -> list[str]:
        """Primary Groq model followed by its fallbacks, de-duplicated."""
        return _dedupe([self.groq_model, *_split_csv(self.groq_fallback_models)])


def configure_tracing(settings: Settings) -> None:
    """Mirror the LangSmith settings into ``os.environ``.

    LangChain reads them from the environment, not from this object, so this
    has to run before the first LLM call. Does nothing when tracing is off.
    """
    if not settings.langsmith_tracing:
        return
    os.environ["LANGSMITH_TRACING"] = "true"
    if settings.langsmith_api_key:
        os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    configure_tracing(settings)
    return settings
