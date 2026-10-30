"""Keep the unit tests independent of the developer's shell environment."""

from __future__ import annotations

import os

import pytest

_PREFIXES = ("GEMINI_", "GROQ_", "LLM_", "LANGSMITH_", "INSTAGRAM_")


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.upper().startswith(_PREFIXES):
            monkeypatch.delenv(key, raising=False)
