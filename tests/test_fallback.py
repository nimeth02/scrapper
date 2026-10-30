"""Unit tests for the fallback chain using in-memory fake chat models."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel

from app.llm.errors import AllModelsFailedError, ErrorKind, classify_error
from app.llm.fallback import FallbackChatModel, FallbackPolicy, HealthTracker, ModelCandidate


# --------------------------------------------------------------------------- fakes
class StatusError(Exception):
    def __init__(self, status_code: int, message: str = "boom"):
        super().__init__(message)
        self.status_code = status_code


class FakeChat(BaseChatModel):
    """Returns ``reply`` or raises ``error``; records every call."""

    reply: str = "ok"
    error: Exception | None = None
    fail_times: int = 10**9  # raise `error` for the first N calls, then succeed
    calls: int = 0
    last_kwargs: dict = {}

    @property
    def _llm_type(self) -> str:
        return "fake"

    def _maybe_raise(self, **kwargs: Any) -> None:
        self.calls += 1
        self.last_kwargs = kwargs
        if self.error is not None and self.calls <= self.fail_times:
            raise self.error

    def _generate(self, messages, stop=None, run_manager: CallbackManagerForLLMRun | None = None, **kwargs) -> ChatResult:
        self._maybe_raise(**kwargs)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])

    def _stream(self, messages, stop=None, run_manager=None, **kwargs) -> Iterator[ChatGenerationChunk]:
        self._maybe_raise(**kwargs)
        for token in self.reply.split(" "):
            yield ChatGenerationChunk(message=AIMessageChunk(content=token + " "))

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=list(tools), **kwargs)

    def with_structured_output(self, schema, **kwargs):
        def _run(_input):
            self._maybe_raise()
            return schema(answer=self.reply)

        return RunnableLambda(_run)


def cand(provider: str, model: str, **kw) -> ModelCandidate:
    return ModelCandidate(provider=provider, model=model, chat_model=FakeChat(**kw))


def chain(*candidates: ModelCandidate, **policy_kw) -> FallbackChatModel:
    policy_kw.setdefault("backoff_seconds", 0)
    policy_kw.setdefault("health", HealthTracker())
    return FallbackChatModel(candidates=list(candidates), policy=FallbackPolicy(**policy_kw))


MSG = [HumanMessage(content="hi")]


# --------------------------------------------------------------------------- tests
def test_primary_used_when_healthy():
    a, b = cand("gemini", "a", reply="A"), cand("gemini", "b", reply="B")
    out = chain(a, b).invoke(MSG)
    assert out.content == "A"
    assert out.response_metadata["llm_fallback"]["model"] == "a"
    assert b.chat_model.calls == 0


def test_transient_error_falls_through_and_cools_down_primary():
    a = cand("gemini", "a", error=StatusError(429, "quota"))
    b = cand("gemini", "b", reply="B")
    llm = chain(a, b, cooldown_seconds=60)

    assert llm.invoke(MSG).content == "B"
    assert a.chat_model.calls == 1
    assert not llm.policy.health.is_available("gemini:a")

    # Second call skips the cooled-down primary without even trying it.
    assert llm.invoke(MSG).content == "B"
    assert a.chat_model.calls == 1


def test_auth_error_skips_whole_provider():
    g1 = cand("gemini", "g1", error=StatusError(401, "API key not valid"))
    g2 = cand("gemini", "g2", reply="G2")
    q = cand("groq", "q", reply="Q")
    out = chain(g1, g2, q).invoke(MSG)
    assert out.content == "Q"
    assert g2.chat_model.calls == 0


def test_model_unavailable_falls_through_with_long_cooldown():
    a = cand("gemini", "a", error=StatusError(404, "models/a is not found"))
    b = cand("gemini", "b", reply="B")
    llm = chain(a, b, cooldown_seconds=1, unavailable_cooldown_seconds=1000)
    assert llm.invoke(MSG).content == "B"
    assert llm.policy.health.snapshot()["gemini:a"] > 500


def test_fatal_error_raises_immediately():
    a = cand("gemini", "a", error=StatusError(400, "Invalid JSON payload"))
    b = cand("gemini", "b", reply="B")
    with pytest.raises(StatusError):
        chain(a, b).invoke(MSG)
    assert b.chat_model.calls == 0


def test_all_failed_raises_aggregate_error():
    a = cand("gemini", "a", error=StatusError(503))
    b = cand("groq", "b", error=StatusError(429))
    with pytest.raises(AllModelsFailedError) as info:
        chain(a, b).invoke(MSG)
    assert [n for n, _ in info.value.failures] == ["gemini:a", "groq:b"]
    assert isinstance(info.value.last_exception, StatusError)


def test_retries_same_model_before_moving_on():
    a = cand("gemini", "a", reply="A", error=StatusError(503), fail_times=1)
    b = cand("gemini", "b", reply="B")
    out = chain(a, b, retries_per_model=2).invoke(MSG)
    assert out.content == "A"
    assert a.chat_model.calls == 2
    assert b.chat_model.calls == 0


def test_cooled_down_models_are_still_last_resort():
    a = cand("gemini", "a", reply="A", error=StatusError(429), fail_times=1)
    b = cand("gemini", "b", error=StatusError(429))
    llm = chain(a, b, cooldown_seconds=60)
    with pytest.raises(AllModelsFailedError):
        llm.invoke(MSG)
    # both blocked -> order falls back to original order and a now succeeds
    assert llm.invoke(MSG).content == "A"


def test_success_clears_cooldown():
    a = cand("gemini", "a", reply="A", error=StatusError(429), fail_times=1)
    b = cand("gemini", "b", error=StatusError(429))
    llm = chain(a, b, cooldown_seconds=60)
    with pytest.raises(AllModelsFailedError):
        llm.invoke(MSG)
    assert llm.invoke(MSG).content == "A"
    assert llm.policy.health.is_available("gemini:a")


def test_stream_falls_back_when_first_model_fails_before_first_chunk():
    a = cand("gemini", "a", error=StatusError(429))
    b = cand("gemini", "b", reply="hello from b")
    chunks = list(chain(a, b).stream(MSG))
    assert "".join(c.content for c in chunks).strip() == "hello from b"
    assert chunks[0].response_metadata["llm_fallback"]["model"] == "b"


async def test_async_invoke_falls_back():
    a = cand("gemini", "a", error=StatusError(429))
    b = cand("gemini", "b", reply="B")
    out = await chain(a, b).ainvoke(MSG)
    assert out.content == "B"


async def test_async_stream_falls_back():
    a = cand("gemini", "a", error=StatusError(503))
    b = cand("gemini", "b", reply="x y z")
    text = "".join([c.content async for c in chain(a, b).astream(MSG)])
    assert text.strip() == "x y z"


def test_bind_tools_binds_every_candidate_and_shares_health():
    a = cand("gemini", "a", error=StatusError(429))
    b = cand("gemini", "b", reply="B")
    llm = chain(a, b)

    def my_tool(x: int) -> int:
        """Doubles x."""
        return 2 * x

    bound = llm.bind_tools([my_tool])
    assert isinstance(bound, FallbackChatModel)
    assert bound.invoke(MSG).content == "B"
    assert b.chat_model.last_kwargs["tools"] == [my_tool]
    assert bound.policy.health is llm.policy.health
    assert not llm.policy.health.is_available("gemini:a")


def test_structured_output_falls_back():
    class Answer(BaseModel):
        answer: str

    a = cand("gemini", "a", error=StatusError(429))
    b = cand("gemini", "b", reply="structured")
    structured = chain(a, b).with_structured_output(Answer)
    out = structured.invoke(MSG)
    assert isinstance(out, Answer)
    assert out.answer == "structured"


def test_health_tracker_expires():
    h = HealthTracker()
    h.block("m", 0.0)
    assert h.is_available("m")
    h.block("m", 1000)
    assert not h.is_available("m")
    h.clear("m")
    assert h.is_available("m")


# --------------------------------------------------------------------------- classifier
@pytest.mark.parametrize(
    ("exc", "kind"),
    [
        (StatusError(429), ErrorKind.TRANSIENT),
        (StatusError(503), ErrorKind.TRANSIENT),
        (StatusError(500), ErrorKind.TRANSIENT),
        (StatusError(401), ErrorKind.AUTH),
        (StatusError(403), ErrorKind.AUTH),
        (StatusError(404), ErrorKind.MODEL_UNAVAILABLE),
        (StatusError(400, "API key not valid. Please pass a valid API key."), ErrorKind.AUTH),
        (StatusError(400, "models/gemini-x is not found for API version v1beta"), ErrorKind.MODEL_UNAVAILABLE),
        (StatusError(400, "The model `foo` has been decommissioned"), ErrorKind.MODEL_UNAVAILABLE),
        (StatusError(400, "Invalid JSON payload received"), ErrorKind.FATAL),
        (StatusError(422, "invalid tool schema"), ErrorKind.FATAL),
        (TimeoutError("read timed out"), ErrorKind.TRANSIENT),
        (ConnectionError("connection reset"), ErrorKind.TRANSIENT),
        (RuntimeError("Resource has been exhausted (e.g. check quota)."), ErrorKind.TRANSIENT),
        (RuntimeError("The model is overloaded. Please try again later."), ErrorKind.TRANSIENT),
        (RuntimeError("something nobody has seen before"), ErrorKind.TRANSIENT),
    ],
)
def test_classify_error(exc, kind):
    assert classify_error(exc) is kind


def test_classify_wrapped_exception_uses_cause():
    try:
        try:
            raise StatusError(429, "quota")
        except StatusError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert classify_error(outer) is ErrorKind.TRANSIENT


def test_classify_real_provider_errors():
    genai_errors = pytest.importorskip("google.genai.errors")

    e = genai_errors.APIError(429, {"error": {"message": "quota exceeded", "status": "RESOURCE_EXHAUSTED"}})
    assert classify_error(e) is ErrorKind.TRANSIENT
    e = genai_errors.APIError(403, {"error": {"message": "permission denied"}})
    assert classify_error(e) is ErrorKind.AUTH
