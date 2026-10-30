"""A LangChain chat model that walks an ordered chain of candidate models.

``FallbackChatModel`` looks like a single ``BaseChatModel`` to the rest of the
app (chains, ``bind_tools``, ``with_structured_output``, streaming) while
transparently trying ``gemini:a -> gemini:b -> groq:c ...`` until one of them
answers.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel, LanguageModelInput
from langchain_core.messages import AIMessageChunk, BaseMessage, BaseMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable, RunnableBinding, RunnableConfig
from pydantic import ConfigDict, Field

from app.llm.errors import AllModelsFailedError, ErrorKind, classify_error

logger = logging.getLogger("app.llm")

T = TypeVar("T")
METADATA_KEY = "llm_fallback"


# --------------------------------------------------------------------------- health
class HealthTracker:
    """Remembers which candidates recently failed so they are tried last.

    Thread-safe; one instance is normally shared by every model in the process.
    """

    def __init__(self) -> None:
        self._blocked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def is_available(self, name: str) -> bool:
        with self._lock:
            until = self._blocked_until.get(name)
            if until is None:
                return True
            if time.monotonic() >= until:
                del self._blocked_until[name]
                return True
            return False

    def block(self, name: str, seconds: float) -> None:
        if seconds <= 0:
            return
        with self._lock:
            until = time.monotonic() + seconds
            self._blocked_until[name] = max(self._blocked_until.get(name, 0.0), until)

    def clear(self, name: str) -> None:
        with self._lock:
            self._blocked_until.pop(name, None)

    def snapshot(self) -> dict[str, float]:
        """Remaining cool-down seconds per blocked candidate."""
        now = time.monotonic()
        with self._lock:
            return {n: round(t - now, 1) for n, t in self._blocked_until.items() if t > now}


# --------------------------------------------------------------------------- candidates
@dataclass(frozen=True)
class ModelCandidate:
    provider: str
    model: str
    chat_model: BaseChatModel
    # Set when the chain has been specialised (bind_tools / with_structured_output).
    runnable: Runnable | None = None

    @property
    def name(self) -> str:
        return f"{self.provider}:{self.model}"

    @property
    def target(self) -> Runnable:
        return self.runnable if self.runnable is not None else self.chat_model


# --------------------------------------------------------------------------- policy
@dataclass
class FallbackPolicy:
    """Decides what to do after each failure. Shared by chat and structured chains."""

    retries_per_model: int = 1
    backoff_seconds: float = 1.0
    cooldown_seconds: float = 30.0
    unavailable_cooldown_seconds: float = 600.0
    health: HealthTracker = field(default_factory=HealthTracker)

    # -- ordering -----------------------------------------------------------
    def order(self, candidates: Sequence[ModelCandidate]) -> list[ModelCandidate]:
        """Healthy candidates first (original order), cooled-down ones as last resort."""
        healthy = [c for c in candidates if self.health.is_available(c.name)]
        blocked = [c for c in candidates if not self.health.is_available(c.name)]
        return healthy + blocked

    # -- sync / async drivers ----------------------------------------------
    def run(
        self,
        candidates: Sequence[ModelCandidate],
        call: Callable[[ModelCandidate], T],
    ) -> tuple[ModelCandidate, T]:
        failures: list[tuple[str, BaseException]] = []
        dead_providers: set[str] = set()
        for cand in self.order(candidates):
            if cand.provider in dead_providers:
                continue
            for attempt in range(1, self.retries_per_model + 1):
                try:
                    result = call(cand)
                except Exception as exc:  # noqa: BLE001 - classified below
                    action, wait = self._on_failure(cand, attempt, exc, failures, dead_providers, candidates)
                    if action == "raise":
                        raise
                    if action == "retry":
                        time.sleep(wait)
                        continue
                    break
                else:
                    self.health.clear(cand.name)
                    return cand, result
        raise AllModelsFailedError(failures)

    async def arun(
        self,
        candidates: Sequence[ModelCandidate],
        call: Callable[[ModelCandidate], Awaitable[T]],
    ) -> tuple[ModelCandidate, T]:
        failures: list[tuple[str, BaseException]] = []
        dead_providers: set[str] = set()
        for cand in self.order(candidates):
            if cand.provider in dead_providers:
                continue
            for attempt in range(1, self.retries_per_model + 1):
                try:
                    result = await call(cand)
                except Exception as exc:  # noqa: BLE001
                    action, wait = self._on_failure(cand, attempt, exc, failures, dead_providers, candidates)
                    if action == "raise":
                        raise
                    if action == "retry":
                        await asyncio.sleep(wait)
                        continue
                    break
                else:
                    self.health.clear(cand.name)
                    return cand, result
        raise AllModelsFailedError(failures)

    # -- failure handling ---------------------------------------------------
    def _on_failure(
        self,
        cand: ModelCandidate,
        attempt: int,
        exc: BaseException,
        failures: list[tuple[str, BaseException]],
        dead_providers: set[str],
        candidates: Sequence[ModelCandidate],
    ) -> tuple[str, float]:
        kind = classify_error(exc)
        failures.append((cand.name, exc))
        summary = f"{type(exc).__name__}: {str(exc)[:200]}"

        if kind is ErrorKind.FATAL:
            logger.error("%s failed with a non-retryable error, not falling back: %s", cand.name, summary)
            return "raise", 0.0

        if kind is ErrorKind.AUTH:
            logger.error("%s rejected credentials; skipping provider %r: %s", cand.name, cand.provider, summary)
            dead_providers.add(cand.provider)
            for other in candidates:
                if other.provider == cand.provider:
                    self.health.block(other.name, self.cooldown_seconds)
            return "next", 0.0

        if kind is ErrorKind.MODEL_UNAVAILABLE:
            logger.warning(
                "%s is unavailable, skipping it for %.0fs: %s",
                cand.name,
                self.unavailable_cooldown_seconds,
                summary,
            )
            self.health.block(cand.name, self.unavailable_cooldown_seconds)
            return "next", 0.0

        # TRANSIENT
        if attempt < self.retries_per_model:
            wait = self.backoff_seconds * (2 ** (attempt - 1))
            logger.warning(
                "%s transient error (attempt %d/%d), retrying in %.1fs: %s",
                cand.name,
                attempt,
                self.retries_per_model,
                wait,
                summary,
            )
            return "retry", wait
        logger.warning(
            "%s transient error, falling back to next model (cool-down %.0fs): %s",
            cand.name,
            self.cooldown_seconds,
            summary,
        )
        self.health.block(cand.name, self.cooldown_seconds)
        return "next", 0.0


# --------------------------------------------------------------------------- helpers
def _tag(message: BaseMessage, cand: ModelCandidate) -> BaseMessage:
    """Record which model produced the message."""
    message.response_metadata = {
        **(message.response_metadata or {}),
        METADATA_KEY: {"provider": cand.provider, "model": cand.model, "name": cand.name},
    }
    return message


def _as_chunk(message: BaseMessage) -> BaseMessageChunk:
    if isinstance(message, BaseMessageChunk):
        return message
    return AIMessageChunk(
        content=message.content,
        additional_kwargs=message.additional_kwargs,
        response_metadata=message.response_metadata,
        id=message.id,
    )


def _call_kwargs(stop: list[str] | None, kwargs: dict[str, Any]) -> tuple[RunnableConfig, dict[str, Any]]:
    """Config for the *generic* path (candidate is not a plain chat model).

    Used only when :func:`_unwrap` fails. Nested LLM runs confuse LangSmith's
    tracer, so this path intentionally passes no callbacks.
    """
    config: RunnableConfig = {}
    call_kwargs = dict(kwargs)
    if stop:
        call_kwargs["stop"] = stop
    return config, call_kwargs


def _unwrap(runnable: Runnable) -> tuple[BaseChatModel, dict[str, Any]] | None:
    """Resolve ``model.bind_tools(...)`` (nested ``RunnableBinding``s) back to the
    underlying chat model plus the bound kwargs, outermost binding winning."""
    kwargs: dict[str, Any] = {}
    current: Any = runnable
    while isinstance(current, RunnableBinding):
        kwargs = {**current.kwargs, **kwargs}
        current = current.bound
    if isinstance(current, BaseChatModel):
        return current, kwargs
    return None


def _tag_result(result: ChatResult, cand: ModelCandidate) -> ChatResult:
    for gen in result.generations:
        _tag(gen.message, cand)
    return result


# --------------------------------------------------------------------------- chat model
class FallbackChatModel(BaseChatModel):
    """Chat model that tries each candidate in turn (see module docstring)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    candidates: list[ModelCandidate]
    policy: FallbackPolicy = Field(default_factory=FallbackPolicy)

    # -- identity -----------------------------------------------------------
    @property
    def _llm_type(self) -> str:
        return "fallback_chat_model"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"models": self.model_names}

    @property
    def model_names(self) -> list[str]:
        return [c.name for c in self.candidates]

    @property
    def primary(self) -> ModelCandidate:
        return self.candidates[0]

    # -- generate -----------------------------------------------------------
    # Each candidate is driven through its *internal* ``_generate`` / ``_stream``
    # with this model's own run manager, so a call is a single LLM run in
    # LangSmith (nested LLM runs break the tracer under LangGraph). The
    # ``invoke``/``stream`` path is kept for exotic candidates only.
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        config, call_kwargs = _call_kwargs(stop, kwargs)

        def call(c: ModelCandidate) -> ChatResult:
            direct = _unwrap(c.target)
            if direct is not None:
                model, bound = direct
                return model._generate(messages, stop=stop, run_manager=run_manager, **{**bound, **kwargs})
            message = c.target.invoke(messages, config=config, **call_kwargs)
            return ChatResult(generations=[ChatGeneration(message=message)])

        cand, result = self.policy.run(self.candidates, call)
        return _tag_result(result, cand)

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        config, call_kwargs = _call_kwargs(stop, kwargs)

        async def call(c: ModelCandidate) -> ChatResult:
            direct = _unwrap(c.target)
            if direct is not None:
                model, bound = direct
                return await model._agenerate(messages, stop=stop, run_manager=run_manager, **{**bound, **kwargs})
            message = await c.target.ainvoke(messages, config=config, **call_kwargs)
            return ChatResult(generations=[ChatGeneration(message=message)])

        cand, result = await self.policy.arun(self.candidates, call)
        return _tag_result(result, cand)

    # -- stream -------------------------------------------------------------
    # Fallback happens only if a model fails *before* its first chunk; once
    # tokens have been yielded to the caller we cannot switch models.
    # ``run_manager`` is deliberately not forwarded: BaseChatModel.stream()
    # already emits on_llm_new_token for every chunk we yield.
    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        config, call_kwargs = _call_kwargs(stop, kwargs)

        def chunks_of(c: ModelCandidate) -> Iterator[ChatGenerationChunk]:
            direct = _unwrap(c.target)
            if direct is None:
                for message in c.target.stream(messages, config=config, **call_kwargs):
                    yield ChatGenerationChunk(message=_as_chunk(message))
                return
            model, bound = direct
            merged = {**bound, **kwargs}
            if type(model)._stream is BaseChatModel._stream:  # provider has no native streaming
                result = model._generate(messages, stop=stop, run_manager=None, **merged)
                yield ChatGenerationChunk(message=_as_chunk(result.generations[0].message))
                return
            yield from model._stream(messages, stop=stop, run_manager=None, **merged)

        def start(c: ModelCandidate) -> tuple[ChatGenerationChunk | None, Iterator[ChatGenerationChunk]]:
            iterator = chunks_of(c)
            return next(iterator, None), iterator

        cand, (first, iterator) = self.policy.run(self.candidates, start)
        if first is None:
            return
        _tag(first.message, cand)
        yield first
        yield from iterator

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        config, call_kwargs = _call_kwargs(stop, kwargs)

        async def chunks_of(c: ModelCandidate) -> AsyncIterator[ChatGenerationChunk]:
            direct = _unwrap(c.target)
            if direct is None:
                async for message in c.target.astream(messages, config=config, **call_kwargs):
                    yield ChatGenerationChunk(message=_as_chunk(message))
                return
            model, bound = direct
            merged = {**bound, **kwargs}
            if type(model)._stream is BaseChatModel._stream and type(model)._astream is BaseChatModel._astream:
                result = await model._agenerate(messages, stop=stop, run_manager=None, **merged)
                yield ChatGenerationChunk(message=_as_chunk(result.generations[0].message))
                return
            async for chunk in model._astream(messages, stop=stop, run_manager=None, **merged):
                yield chunk

        async def start(c: ModelCandidate) -> tuple[ChatGenerationChunk | None, AsyncIterator[ChatGenerationChunk]]:
            iterator = chunks_of(c)
            return await anext(iterator, None), iterator

        cand, (first, iterator) = await self.policy.arun(self.candidates, start)
        if first is None:
            return
        _tag(first.message, cand)
        yield first
        async for chunk in iterator:
            yield chunk

    # -- specialisation -----------------------------------------------------
    def bind_tools(self, tools: Sequence[Any], *, tool_choice: Any = None, **kwargs: Any) -> FallbackChatModel:
        """Bind tools on every candidate; the result is still a fallback chain."""
        extra = dict(kwargs)
        if tool_choice is not None:
            extra["tool_choice"] = tool_choice
        bound = [replace(c, runnable=c.chat_model.bind_tools(tools, **extra)) for c in self.candidates]
        return self.model_copy(update={"candidates": bound})

    def with_structured_output(self, schema: Any, *, include_raw: bool = False, **kwargs: Any) -> Runnable:
        """Use each provider's native structured output, with the same fallback policy."""
        structured = [
            replace(c, runnable=c.chat_model.with_structured_output(schema, include_raw=include_raw, **kwargs))
            for c in self.candidates
        ]
        return FallbackRunnable(structured, self.policy, name="FallbackStructuredOutput")


# --------------------------------------------------------------------------- generic runnable
class FallbackRunnable(Runnable[LanguageModelInput, Any]):
    """Fallback over arbitrary runnables (used for structured output)."""

    def __init__(self, candidates: Sequence[ModelCandidate], policy: FallbackPolicy, name: str = "FallbackRunnable"):
        self.candidates = list(candidates)
        self.policy = policy
        self.name = name

    def invoke(self, input: LanguageModelInput, config: RunnableConfig | None = None, **kwargs: Any) -> Any:
        _, result = self.policy.run(self.candidates, lambda c: c.target.invoke(input, config=config, **kwargs))
        return result

    async def ainvoke(self, input: LanguageModelInput, config: RunnableConfig | None = None, **kwargs: Any) -> Any:
        async def call(c: ModelCandidate) -> Any:
            return await c.target.ainvoke(input, config=config, **kwargs)

        _, result = await self.policy.arun(self.candidates, call)
        return result
