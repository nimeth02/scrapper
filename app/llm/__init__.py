"""LLM service: one chat model that falls back across Gemini and Groq models.

Typical use::

    from app.llm import get_llm

    llm = get_llm()                       # built once from .env
    reply = llm.invoke("Summarise this page: ...")
    print(reply.text, reply.response_metadata["llm_fallback"])
"""

from app.llm.errors import AllModelsFailedError, ErrorKind, classify_error
from app.llm.factory import LLMConfigurationError, build_candidates, build_llm, get_llm, primary_chat_model
from app.llm.fallback import FallbackChatModel, FallbackPolicy, HealthTracker, ModelCandidate

__all__ = [
    "AllModelsFailedError",
    "ErrorKind",
    "FallbackChatModel",
    "FallbackPolicy",
    "HealthTracker",
    "LLMConfigurationError",
    "ModelCandidate",
    "build_candidates",
    "build_llm",
    "classify_error",
    "get_llm",
    "primary_chat_model",
]
