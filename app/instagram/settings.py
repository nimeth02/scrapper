"""Instagram settings, read from ``INSTAGRAM_*`` environment variables / the project-root ``.env``."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import PROJECT_ROOT


class InstagramSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INSTAGRAM_",
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Only the login server needs these; the scraper reuses the saved session.
    username: str | None = None
    password: str | None = None

    # Playwright storage state (cookies + localStorage). Written by the login server, read by the
    # scraper server. Relative paths are resolved against the project root.
    session_file: Path = Path("data/instagram/session.json")

    # Scraper browser. Headless is fine once a session exists.
    headless: bool = True
    # Login browser. Headed by default so 2FA prompts and "suspicious login" checkpoints can be
    # finished by hand in the window.
    login_headless: bool = False

    # How long login() waits for Instagram to accept the password before giving up.
    login_wait_seconds: float = Field(default=45.0, gt=0)
    # Page navigation / selector timeout.
    nav_timeout_seconds: float = Field(default=30.0, gt=0)
    # Minimum pause between two requests to Instagram (jittered). Keep it >= 1s to avoid blocks.
    request_delay_seconds: float = Field(default=1.5, ge=0)

    locale: str = "en-US"
    user_agent: str | None = None  # leave empty to use Chromium's own

    @field_validator("username", "password", "user_agent", mode="before")
    @classmethod
    def _blank_to_none(cls, v):  # noqa: ANN001
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("session_file", mode="after")
    @classmethod
    def _absolute(cls, v: Path) -> Path:
        return v if v.is_absolute() else (PROJECT_ROOT / v).resolve()


@lru_cache(maxsize=1)
def get_instagram_settings() -> InstagramSettings:
    return InstagramSettings()
