"""Build the fallback chat model from :class:`app.config.Settings`.

Chain order
-----------
1. Every model of the primary provider (``LLM_PROVIDER``): ``*_MODEL`` first,
   then ``*_FALLBACK_MODELS`` in the order given.
2. Every model of the other provider, if its API key is set.

So with the default ``.env`` the chain is::

    gemini:gemini-3.5-flash-lite -> gemini:gemini-3.1-flash-lite -> gemini:gemini-3.5-flash
    -> groq:openai/gpt-oss-120b -> groq:openai/gpt-oss-20b -> groq:qwen/qwen3.8-27b
"""

from __future__ import annotations

import logging
import re
import warnings
from functools import lru_cache

from langchain_core.language_models import BaseChatModel

from app.config import Settings, get_settings
from app.llm.fallback import FallbackChatModel, FallbackPolicy, HealthTracker, ModelCandidate

logger = logging.getLogger("app.llm")

# Some Gemini models ignore `temperature` and langchain-google-genai warns about it on
# *every* request. One shared temperature setting across the chain is intended, so hide it.
warnings.filterwarnings("ignore", message=".*fixed sampling defaults.*", category=UserWarning)

# Shared across every model built by this module so a 429 seen by one caller
# is remembered by the next one.
_SHARED_HEALTH = HealthTracker()

_API_VERSION_SUFFIX = re.compile(r"^(?P<base>.+?)/(?P<version>v\d+(?:alpha|beta)?\d*)/?$")


class LLMConfigurationError(RuntimeError):
    """Raised when no usable provider/API key is configured."""


def split_gemini_base_url(url: str | None) -> tuple[str | None, str | None]:
    """Split ``https://host/v1beta`` into ``("https://host/", "v1beta")``.

    The google-genai SDK takes the API version separately from the host and
    would otherwise produce ``.../v1beta/v1beta/models/...``.
    """
    if not url or not url.strip():
        return None, None
    url = url.strip()
    match = _API_VERSION_SUFFIX.match(url)
    if match:
        return match.group("base").rstrip("/") + "/", match.group("version")
    return url.rstrip("/") + "/", None


def _gemini_models(settings: Settings, temperature: float) -> list[ModelCandidate]:
    from langchain_google_genai import ChatGoogleGenerativeAI

    base_url, api_version = split_gemini_base_url(settings.gemini_base_url)
    common: dict = {
        "google_api_key": settings.gemini_api_key,
        "temperature": temperature,
        "timeout": settings.llm_timeout_seconds,
        # The chain handles retries/fallback; keep the SDK from stacking its own backoff.
        "max_retries": 0,
    }
    if base_url:
        common["base_url"] = base_url
    if api_version:
        common["api_version"] = api_version

    return [
        ModelCandidate(provider="gemini", model=name, chat_model=ChatGoogleGenerativeAI(model=name, **common))
        for name in settings.gemini_models
    ]


def _groq_models(settings: Settings, temperature: float) -> list[ModelCandidate]:
    from langchain_groq import ChatGroq

    return [
        ModelCandidate(
            provider="groq",
            model=name,
            chat_model=ChatGroq(
                model=name,
                api_key=settings.groq_api_key,
                temperature=temperature,
                request_timeout=settings.llm_timeout_seconds,
                max_retries=0,
            ),
        )
        for name in settings.groq_models
    ]


_BUILDERS = {"gemini": _gemini_models, "groq": _groq_models}


def build_candidates(settings: Settings | None = None, *, temperature: float | None = None) -> list[ModelCandidate]:
    """Return the ordered candidate list described in the module docstring."""
    settings = settings or get_settings()
    temperature = settings.llm_temperature if temperature is None else temperature

    keys = {"gemini": settings.gemini_api_key, "groq": settings.groq_api_key}
    order = [settings.llm_provider] + [p for p in _BUILDERS if p != settings.llm_provider]

    if not keys[settings.llm_provider]:
        logger.warning(
            "LLM_PROVIDER=%s but its API key is not set; using the other provider(s) only.",
            settings.llm_provider,
        )

    candidates: list[ModelCandidate] = []
    for provider in order:
        if keys[provider]:
            candidates.extend(_BUILDERS[provider](settings, temperature))

    if not candidates:
        raise LLMConfigurationError("No LLM provider configured. Set GEMINI_API_KEY and/or GROQ_API_KEY in .env.")

    logger.info("LLM fallback chain: %s", " -> ".join(c.name for c in candidates))
    return candidates


def build_llm(
    settings: Settings | None = None,
    *,
    temperature: float | None = None,
    health: HealthTracker | None = None,
) -> FallbackChatModel:
    """Build a fresh :class:`FallbackChatModel` (not cached)."""
    settings = settings or get_settings()
    policy = FallbackPolicy(
        retries_per_model=settings.llm_retries_per_model,
        cooldown_seconds=settings.llm_cooldown_seconds,
        unavailable_cooldown_seconds=settings.llm_unavailable_cooldown_seconds,
        health=health or _SHARED_HEALTH,
    )
    return FallbackChatModel(candidates=build_candidates(settings, temperature=temperature), policy=policy)


@lru_cache(maxsize=1)
def get_llm() -> FallbackChatModel:
    """Process-wide default model built from ``.env``.

    Use :func:`build_llm` when you need a different temperature for one call site.
    """
    return build_llm()


def primary_chat_model(llm: FallbackChatModel | None = None) -> BaseChatModel:
    """The raw provider model at the head of the chain (useful for introspection)."""
    return (llm or get_llm()).candidates[0].chat_model
