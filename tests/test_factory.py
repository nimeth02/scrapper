"""Chain construction from settings. No network: provider clients are only instantiated."""

from __future__ import annotations

import pytest

from app.config import Settings
from app.llm.factory import LLMConfigurationError, build_candidates, build_llm, split_gemini_base_url


def settings(**overrides) -> Settings:
    values = dict(
        gemini_model="g-main",
        gemini_fallback_models="g-fb1, g-fb2,g-main",  # duplicate of the primary is dropped
        groq_model="q-main",
        groq_fallback_models="q-fb1",
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (None, (None, None)),
        ("", (None, None)),
        ("https://generativelanguage.googleapis.com/v1beta", ("https://generativelanguage.googleapis.com/", "v1beta")),
        ("https://generativelanguage.googleapis.com/v1beta/", ("https://generativelanguage.googleapis.com/", "v1beta")),
        ("https://proxy.example.com/gemini", ("https://proxy.example.com/gemini/", None)),
    ],
)
def test_split_gemini_base_url(url, expected):
    assert split_gemini_base_url(url) == expected


def test_gemini_primary_then_groq_as_cross_provider_fallback():
    names = [c.name for c in build_candidates(settings(gemini_api_key="g", groq_api_key="q"))]
    assert names == ["gemini:g-main", "gemini:g-fb1", "gemini:g-fb2", "groq:q-main", "groq:q-fb1"]


def test_groq_primary_puts_groq_first():
    s = settings(llm_provider="groq", gemini_api_key="g", groq_api_key="q")
    names = [c.name for c in build_candidates(s)]
    assert names == ["groq:q-main", "groq:q-fb1", "gemini:g-main", "gemini:g-fb1", "gemini:g-fb2"]


def test_provider_without_key_is_left_out():
    names = [c.name for c in build_candidates(settings(gemini_api_key="g"))]
    assert names == ["gemini:g-main", "gemini:g-fb1", "gemini:g-fb2"]


def test_blank_key_counts_as_missing():
    names = [c.name for c in build_candidates(settings(gemini_api_key="g", groq_api_key="   "))]
    assert names == ["gemini:g-main", "gemini:g-fb1", "gemini:g-fb2"]


def test_no_keys_raises():
    with pytest.raises(LLMConfigurationError):
        build_candidates(settings())


def test_build_llm_applies_fallback_settings():
    llm = build_llm(
        settings(
            gemini_api_key="g",
            llm_retries_per_model=3,
            llm_cooldown_seconds=7,
            llm_unavailable_cooldown_seconds=99,
        )
    )
    assert llm.policy.retries_per_model == 3
    assert llm.policy.cooldown_seconds == 7
    assert llm.policy.unavailable_cooldown_seconds == 99
    assert llm.model_names[0] == "gemini:g-main"


def test_build_llm_shares_health_across_instances():
    s = settings(gemini_api_key="g")
    assert build_llm(s).policy.health is build_llm(s).policy.health
