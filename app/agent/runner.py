"""The agent loop: a chat model with the Instagram tools, run until it answers.

Plain LangChain tool calling on top of :func:`app.llm.get_llm`, so one request falls back across the
Gemini and Groq chain like every other LLM call in this project. The loop adds what a scraping agent
needs: a budget of tool-calling turns, an immediate stop when Instagram starts throttling (the model is
then asked to answer with what it has), and a run tree so a CLI or a web page can show every step,
including the model's own reasoning (:func:`app.llm.get_reasoning_llm`).

Events passed to ``on_event`` are dicts with a ``type``:

``run_start`` / ``run_end``  one run of the tree in :mod:`app.agent.trace`, typed like LangSmith's:
                the question is the root ``chain`` run; each model call an ``llm`` run (its ``end``
                carries the ``model``, ``reasoning``, ``text``, ``tool_calls`` and token ``usage``); the
                calls of one turn a ``tools`` ``chain`` run holding one ``tool`` run each (its ``end``
                carries a one-line ``summary``, the ``output`` the model read and the full ``result``)
``final``       the answer ``text``, the ``model`` that wrote it, ``steps``, ``tool_calls``, ``tokens``
                and ``seconds``
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langsmith import utils as langsmith_utils
from pydantic import ValidationError

from app.agent.tools import Acquire, build_tools, describe_result
from app.agent.trace import Run, RunTree
from app.instagram.api import InstagramAPIError, LoginRequired, RateLimited
from app.llm import LLMConfigurationError, get_llm, get_reasoning_llm
from app.llm.errors import ErrorKind, classify_error

log = logging.getLogger(__name__)

MAX_STEPS = 8  # model turns that call tools; one turn may hold several calls
MAX_HISTORY = 10  # earlier chat turns replayed to the model
MAX_OUTPUT_CHARS = 12000  # tool output echoed to the UI (the model gets all of it)
PREVIEW_CHARS = 280  # per message, in the inputs of an llm run
AGENT_NAME = "Hashtag Scout agent"  # the root run
TRACE_TAGS = ["hashtag-scout"]
_THINK_TAGS = re.compile(r"<think>(.*?)</think>", re.S)

SYSTEM_PROMPT = """You are Hashtag Scout, a research assistant for Instagram hashtags. You answer only with data you fetched through your tools, never from memory: every username, count or quote in your answer must appear in a tool result.

How to work
- Start with search_hashtags for the hashtags the user names. Default to 24-48 posts per hashtag (max_scrolls 0-1) and widen only when the user asks for more or the first page is clearly too thin.
- When the user wants engagement (who comments, who likes, "active followers", "fans", "engaged accounts"), call get_engagement on the posts search_hashtags returned, 10-20 posts at a time, preferring the posts with the most comments and likes.
- Use search_instagram to discover related hashtags, get_profile or get_user_posts to check one account, get_post for one post.
- Every tool call spends requests of a real Instagram account that can be rate-limited or locked. Do what the question needs and no more. If a tool reports throttling or a login problem, stop calling tools and answer with what you already have, saying so.
- Instagram shows only the first ~100 likers of a post and search pages are not exhaustive; say so when it matters.

How to answer
- Lead with the result: the usernames as @username, grouped the way the user asked (posters / commenters / likers), with the counts that rank them. Use short markdown lists or a table.
- Then one or two sentences on coverage: hashtags crawled, posts seen, what was cut short.
- Be concise. No preamble, no repeating the question."""

STOP_PROMPTS = {
    "throttled": "Instagram is throttling this account. Do not call any more tools; answer now from the results above and say that the crawl stopped early.",
    "login": "The Instagram session is no longer valid. Do not call any more tools; answer from the results above and say that a new login is needed.",
    "budget": "You have used the tool budget for this request. Do not call any more tools; answer now from the results above and say what is still missing.",
}
NO_ANSWER = "The run ended before the model wrote an answer. The step results above are what was gathered."

EventSink = Callable[[dict[str, Any]], None]


@dataclass
class ToolOutcome:
    content: str  # what the model reads
    summary: str  # what a person reads
    artifact: Any = None  # the full result, for a UI
    error: str | None = None
    stop: str | None = None  # "throttled" / "login" when the run must end


class ToolRefused(Exception):
    """Raised by a tool that declines a call (a bad target, a spent budget); the model reads the message.
    ``stop`` ends the run the way throttling does (one of :data:`STOP_PROMPTS`)."""

    def __init__(self, message: str, *, stop: str | None = None) -> None:
        super().__init__(message)
        self.stop = stop


def describe() -> dict:
    """Whether an LLM is configured and which models the chain holds (no network)."""
    try:
        llm = get_llm()
    except LLMConfigurationError as exc:
        return {"configured": False, "models": [], "message": str(exc)}
    langsmith = langsmith_utils.get_tracer_project() if langsmith_utils.tracing_is_enabled() else None
    return {"configured": True, "models": llm.model_names, "message": None, "langsmith": langsmith}


def history_messages(history: Iterable[dict] | None) -> list[BaseMessage]:
    """Earlier chat turns (``{"role": "user" | "assistant", "content": str}``) as messages, newest last."""
    out: list[BaseMessage] = []
    for turn in list(history or [])[-MAX_HISTORY:]:
        if not isinstance(turn, dict):
            continue
        content = str(turn.get("content") or "").strip()
        if not content:
            continue
        out.append(HumanMessage(content) if turn.get("role") == "user" else AIMessage(content))
    return out


def _error_json(message: str, *, stop: bool = False) -> str:
    payload: dict[str, Any] = {"error": message}
    if stop:
        payload["instruction"] = "Do not call any more tools. Answer with what you already have."
    return json.dumps(payload, ensure_ascii=False)


def _model_name(message: AIMessage) -> str | None:
    return ((message.response_metadata or {}).get("llm_fallback") or {}).get("name")


def split_reply(message: AIMessage) -> tuple[str, str]:
    """``(reasoning, text)`` of a model reply.

    Reasoning comes as standard ``reasoning`` content blocks (Gemini thought summaries, Groq's
    ``reasoning_content``) or, for models that inline it, as ``<think>...</think>`` in the text."""
    parts: list[str] = []
    try:
        blocks = message.content_blocks
    except Exception:  # noqa: BLE001 - an unfamiliar provider format: no reasoning, text only
        blocks = []
    for block in blocks:
        if block.get("type") == "reasoning" and str(block.get("reasoning") or "").strip():
            parts.append(str(block["reasoning"]).strip())
    text = message.text or ""
    inline = [m.strip() for m in _THINK_TAGS.findall(text) if m.strip()]
    if inline:
        parts.extend(inline)
        text = _THINK_TAGS.sub("", text)
    return "\n\n".join(parts), text.strip()


def message_view(message: BaseMessage) -> dict:
    """A short view of one message, for the inputs of an llm run."""
    role = {"system": "system", "human": "user", "ai": "assistant", "tool": "tool"}.get(message.type, message.type)
    text = message.text if isinstance(message.text, str) else str(message.content)
    view: dict[str, Any] = {"role": role, "chars": len(text), "preview": text[:PREVIEW_CHARS]}
    if isinstance(message, ToolMessage):
        view["name"] = message.name
        view["status"] = message.status
    calls = getattr(message, "tool_calls", None)
    if calls:
        view["tool_calls"] = [c.get("name") for c in calls]
    return view


def usage_of(message: AIMessage) -> dict | None:
    """Token counts the provider reported, if any."""
    usage = message.usage_metadata
    if not usage:
        return None
    return {
        "input": usage.get("input_tokens"),
        "output": usage.get("output_tokens"),
        "total": usage.get("total_tokens"),
        "reasoning": (usage.get("output_token_details") or {}).get("reasoning"),
    }


async def execute_tool(
    by_name: dict[str, BaseTool],
    call: dict,
    config: RunnableConfig | None = None,
    describe: Callable[[str, Any], str] = describe_result,
) -> ToolOutcome:
    """Run one tool call; every failure becomes a message the model can act on instead of an exception."""
    name = call.get("name") or ""
    tool = by_name.get(name)
    if tool is None:
        error = f"unknown tool {name!r}; available: {', '.join(sorted(by_name))}"
        return ToolOutcome(_error_json(error), error, error=error)
    tool_call = {"name": name, "args": call.get("args") or {}, "id": call.get("id") or name, "type": "tool_call"}
    try:
        message = await tool.ainvoke(tool_call, config=config)
    except RateLimited as exc:
        error = f"Instagram is throttling this account: {exc}"
        return ToolOutcome(_error_json(error, stop=True), error, error=error, stop="throttled")
    except LoginRequired as exc:
        error = f"The Instagram session is not valid: {exc}"
        return ToolOutcome(_error_json(error, stop=True), error, error=error, stop="login")
    except ToolRefused as exc:
        error = str(exc)
        return ToolOutcome(_error_json(error, stop=exc.stop is not None), error, error=error, stop=exc.stop)
    except ValidationError as exc:
        details = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        error = f"invalid arguments for {name}: {details}"
        return ToolOutcome(_error_json(error), error, error=error)
    except InstagramAPIError as exc:
        error = str(exc)
        return ToolOutcome(_error_json(error), error, error=error)
    except Exception as exc:  # noqa: BLE001 - browser / network trouble; the model decides what to do next
        log.exception("tool %s failed", name)
        error = f"{type(exc).__name__}: {exc}"
        return ToolOutcome(_error_json(error), error, error=error)
    artifact = getattr(message, "artifact", None)
    content = message.content if isinstance(message.content, str) else json.dumps(message.content, default=str)
    return ToolOutcome(content, describe(name, artifact), artifact=artifact)


async def run_agent(
    question: str,
    *,
    acquire: Acquire | None = None,
    llm: BaseChatModel | None = None,
    history: Iterable[dict] | None = None,
    on_event: EventSink | None = None,
    max_steps: int = MAX_STEPS,
    tools: list[BaseTool] | None = None,
    system_prompt: str = SYSTEM_PROMPT,
    name: str = AGENT_NAME,
    trace_tags: list[str] | None = None,
    describe_tool: Callable[[str, Any], str] = describe_result,
) -> dict:
    """Answer ``question`` with the Instagram tools.

    Another agent reuses the loop by passing its own ``tools`` (then ``acquire`` is not needed),
    ``system_prompt``, root run ``name``, ``trace_tags`` and ``describe_tool`` (one line per tool result).

    Returns ``{"answer", "model", "steps", "tool_calls", "tokens", "stopped", "seconds"}``; ``stopped``
    is ``None`` when the model finished on its own, otherwise why the loop ended it ("throttled",
    "login", "budget"). ``tokens`` is the total the providers reported (``None`` if none did). Without
    ``llm`` the reasoning variant of the project's model chain is used, and the plain chain if a model
    rejects the reasoning request.
    """
    question = question.strip()
    if not question:
        raise ValueError("empty question")
    emit: EventSink = on_event or (lambda event: None)
    started = time.monotonic()
    base, spare = (llm, None) if llm is not None else (get_reasoning_llm(), get_llm())
    if tools is None:
        if acquire is None:
            raise ValueError("run_agent needs either tools or acquire")
        tools = build_tools(acquire)
    by_name = {t.name: t for t in tools}
    past = history_messages(history)
    messages: list[BaseMessage] = [SystemMessage(system_prompt), *past, HumanMessage(question)]

    steps = 0
    tool_calls = 0
    tokens: int | None = None
    stop: str | None = None
    tree = RunTree(emit, tags=trace_tags or TRACE_TAGS)
    root = await tree.start("chain", name, inputs={"question": question, "history_turns": len(past)})

    async def call_model(**bind: Any) -> tuple[AIMessage, str]:
        """One llm run; ``(reply, text)`` with the reasoning split off the text."""
        nonlocal base, spare, tokens
        while True:
            inputs: dict[str, Any] = {"messages": [message_view(m) for m in messages]}
            if getattr(base, "model_names", None):
                inputs["models"] = list(base.model_names)
            inputs.update(bind)
            run = await tree.start("llm", base.get_name(), parent=root, step=steps + 1, inputs=inputs)
            try:
                reply = await base.bind_tools(tools, **bind).ainvoke(messages, config=tree.config(run))
            except Exception as exc:
                await tree.end(run, error=exc)
                if spare is None or classify_error(exc) is not ErrorKind.FATAL:
                    raise
                log.warning("the model rejected the reasoning request (%s); continuing without reasoning", exc)
                base, spare = spare, None
                continue
            reasoning, text = split_reply(reply)
            usage = usage_of(reply)
            if usage and usage.get("total") is not None:
                tokens = (tokens or 0) + usage["total"]
            await tree.end(
                run,
                model=_model_name(reply),
                reasoning=reasoning,
                text=text,
                tool_calls=[{"name": c.get("name"), "args": c.get("args") or {}} for c in reply.tool_calls],
                usage=usage,
            )
            messages.append(reply)
            return reply, text

    async def finish(reason: str) -> tuple[AIMessage, str]:
        """One more model turn without tools, so the run always ends in an answer."""
        messages.append(HumanMessage(STOP_PROMPTS[reason]))
        try:
            return await call_model(tool_choice="none")
        except Exception as exc:  # noqa: BLE001 - a provider that rejects tool_choice="none"
            log.warning("finishing without tool_choice=none (%s: %s)", type(exc).__name__, exc)
            return await call_model()

    async def run_tool(group: Run, call: dict) -> bool:
        """One tool run inside the turn's ``tools`` chain; its result goes back to the model.
        Returns whether it failed."""
        nonlocal stop
        tool_name = call.get("name") or "tool"
        run = await tree.start("tool", tool_name, parent=group, step=steps, inputs=call.get("args") or {})
        if stop:
            error = "the run is ending; no more tools are executed"
            outcome = ToolOutcome(_error_json(error, stop=True), error, error=error)
        else:
            outcome = await execute_tool(by_name, call, config=tree.config(run), describe=describe_tool)
            stop = outcome.stop or stop
        messages.append(
            ToolMessage(
                content=outcome.content,
                tool_call_id=call.get("id") or str(run.id),
                name=tool_name,
                status="error" if outcome.error else "success",
            )
        )
        await tree.end(
            run,
            error=outcome.error,
            summary=outcome.summary,
            output=outcome.content[:MAX_OUTPUT_CHARS],
            result=outcome.artifact,
        )
        return outcome.error is not None

    async def loop() -> tuple[AIMessage, str]:
        nonlocal steps, tool_calls, stop
        while True:
            reply, text = await call_model()
            if not reply.tool_calls:
                return reply, text
            steps += 1
            names = [c.get("name") for c in reply.tool_calls]
            group = await tree.start("chain", "tools", parent=root, step=steps, inputs={"tool_calls": names})
            failed = 0
            for call in reply.tool_calls:
                tool_calls += 1
                failed += await run_tool(group, call)
            await tree.end(group, outputs={"calls": len(names), "failed": failed, "stopped": stop})
            if stop is None and steps >= max_steps:
                stop = "budget"
            if stop:
                return await finish(stop)

    try:
        reply, text = await loop()
    except BaseException as exc:  # incl. cancellation: close the open runs so LangSmith and the page see why
        await tree.abort(exc)
        raise
    result = {
        "answer": text or NO_ANSWER,
        "model": _model_name(reply),
        "steps": steps,
        "tool_calls": tool_calls,
        "tokens": tokens,
        "stopped": stop,
        "seconds": round(time.monotonic() - started, 1),
    }
    await tree.end(root, outputs={"answer": result["answer"], "stopped": stop}, tokens=tokens)
    emit({"type": "final", "text": result["answer"], **{k: v for k, v in result.items() if k != "answer"}})
    return result
