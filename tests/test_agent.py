"""The agent loop with a scripted model and fake Instagram functions: no network, no browser, no LLM."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tracers.context import collect_runs
from pydantic import Field

from app.agent import runner, tools
from app.agent.runner import describe, history_messages, run_agent, split_reply
from app.agent.tools import build_tools, compact_search, describe_result
from app.instagram import engagement, hashtags, profiles
from app.instagram.api import InstagramAPIError, RateLimited
from app.llm import LLMConfigurationError


class ScriptedModel(BaseChatModel):
    """Returns the scripted replies in order and records what it was asked."""

    replies: list[AIMessage]
    seen: list[list[Any]] = Field(default_factory=list)
    bound: list[dict] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001
        self.bound.append({"tools": [t.name for t in tools], **kwargs})
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        self.seen.append(list(messages))
        if not self.replies:
            raise AssertionError("the model was called more often than scripted")
        return ChatResult(generations=[ChatGeneration(message=self.replies.pop(0))])


def call(name: str, args: dict, id: str = "c1") -> dict:  # noqa: A002
    return {"name": name, "args": args, "id": id, "type": "tool_call"}


@asynccontextmanager
async def fake_acquire():
    yield object()


def runs(events: list[dict], kind: str = "run_end", run_type: str | None = None) -> list[dict]:
    return [e for e in events if e["type"] == kind and (run_type is None or e["run_type"] == run_type)]


def shape(events: list[dict]) -> list[tuple]:
    """The run tree as ``(event, run_type, name, depth)``, depth taken from the parent ids."""
    depth: dict[str | None, int] = {None: -1}
    out = []
    for e in events:
        if e["type"] == "run_start":
            depth[e["run_id"]] = depth[e["parent_id"]] + 1
        if e["type"] in ("run_start", "run_end"):
            out.append((e["type"][4:], e["run_type"], e["name"], depth[e["run_id"]]))
    return out


CRAWL = {
    "tags": [{"tag": "tea", "url": "https://www.instagram.com/explore/search/keyword/?q=%23tea", "post_count": 2, "user_count": 2, "has_more": True, "scrolls": 0}],
    "user_count": 2,
    "usernames": ["danfetea", "brewer"],
    "users": [
        {"username": "danfetea", "user_id": "7", "full_name": "Danfe Tea", "is_private": False, "is_verified": True, "profile_pic_url": "https://cdn/p.jpg", "profile_url": "https://www.instagram.com/danfetea/", "post_count": 1, "hashtags": ["tea"], "posts": ["https://www.instagram.com/p/AAA/"]},
        {"username": "brewer", "user_id": "8", "full_name": None, "is_private": False, "is_verified": False, "profile_pic_url": None, "profile_url": "https://www.instagram.com/brewer/", "post_count": 1, "hashtags": ["tea"], "posts": ["https://www.instagram.com/p/BBB/"]},
    ],
    "post_count": 2,
    "posts": [
        {"shortcode": "AAA", "url": "https://www.instagram.com/p/AAA/", "owner": "danfetea", "likes": 10, "comments": 2, "type": "photo", "taken_at": "2026-01-01T00:00:00+00:00", "caption": "x" * 400, "image_url": "https://cdn/a.jpg", "hashtags": ["tea"]},
        {"shortcode": "BBB", "url": "https://www.instagram.com/p/BBB/", "owner": "brewer", "likes": 5, "comments": 0, "type": "video", "taken_at": None, "caption": None, "image_url": None, "hashtags": ["tea"]},
    ],
}


async def test_runs_a_tool_and_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    received: dict = {}

    async def fake_crawl(session, tags, **kwargs):  # noqa: ANN001
        received.update(tags=tags, **kwargs)
        return CRAWL

    monkeypatch.setattr(hashtags, "crawl_hashtags", fake_crawl)
    model = ScriptedModel(
        replies=[
            AIMessage(
                content="",
                tool_calls=[call("search_hashtags", {"tags": ["#tea"]})],
                usage_metadata={"input_tokens": 900, "output_tokens": 40, "total_tokens": 940, "output_token_details": {"reasoning": 25}},
            ),
            AIMessage(content="@danfetea and @brewer post under #tea.", usage_metadata={"input_tokens": 1500, "output_tokens": 20, "total_tokens": 1520}),
        ]
    )
    events: list[dict] = []
    result = await run_agent("who posts under #tea?", acquire=fake_acquire, llm=model, on_event=events.append)

    assert isinstance(result.pop("seconds"), float)
    assert result == {"answer": "@danfetea and @brewer post under #tea.", "model": None, "steps": 1, "tool_calls": 1, "tokens": 2460, "stopped": None}
    assert shape(events) == [
        ("start", "chain", runner.AGENT_NAME, 0),
        ("start", "llm", "ScriptedModel", 1),
        ("end", "llm", "ScriptedModel", 1),
        ("start", "chain", "tools", 1),
        ("start", "tool", "search_hashtags", 2),
        ("end", "tool", "search_hashtags", 2),
        ("end", "chain", "tools", 1),
        ("start", "llm", "ScriptedModel", 1),
        ("end", "llm", "ScriptedModel", 1),
        ("end", "chain", runner.AGENT_NAME, 0),
    ]
    assert events[-1]["type"] == "final" and events[-1]["tokens"] == 2460

    root = runs(events, "run_start")[0]
    assert root["inputs"] == {"question": "who posts under #tea?", "history_turns": 0} and root["offset"] == 0
    first_llm, second_llm = runs(events, "run_end", "llm")
    assert first_llm["step"] == 1 and second_llm["step"] == 2
    assert first_llm["tool_calls"] == [{"name": "search_hashtags", "args": {"tags": ["#tea"]}}] and first_llm["error"] is None
    assert first_llm["usage"] == {"input": 900, "output": 40, "total": 940, "reasoning": 25}
    assert second_llm["text"] == "@danfetea and @brewer post under #tea." and second_llm["tool_calls"] == []
    llm_inputs = runs(events, "run_start", "llm")[1]["inputs"]
    assert [m["role"] for m in llm_inputs["messages"]] == ["system", "user", "assistant", "tool"]
    assert llm_inputs["messages"][2]["tool_calls"] == ["search_hashtags"] and llm_inputs["messages"][3]["name"] == "search_hashtags"
    assert len(llm_inputs["messages"][0]["preview"]) == runner.PREVIEW_CHARS < llm_inputs["messages"][0]["chars"]

    (tool_start,) = runs(events, "run_start", "tool")
    (tool_end,) = runs(events, "run_end", "tool")
    assert tool_start["inputs"] == {"tags": ["#tea"]} and tool_start["step"] == 1
    assert tool_end["summary"] == "#tea: 2 posts, 2 accounts" and tool_end["result"] == CRAWL and tool_end["error"] is None
    assert json.loads(tool_end["output"])["account_count"] == 2 and tool_end["seconds"] >= 0
    group = [e for e in runs(events) if e["name"] == "tools"][0]
    assert group["outputs"] == {"calls": 1, "failed": 0, "stopped": None}
    assert received == {"tags": ["#tea"], "max_posts_per_tag": 24, "max_scrolls": 1, "include_posts": True}

    first, second = model.seen
    assert isinstance(first[0], SystemMessage) and isinstance(first[-1], HumanMessage) and first[-1].content == "who posts under #tea?"
    tool_msg = second[-1]
    assert isinstance(tool_msg, ToolMessage) and tool_msg.name == "search_hashtags" and tool_msg.status == "success"
    view = json.loads(tool_msg.content)
    assert [a["username"] for a in view["accounts"]] == ["danfetea", "brewer"]
    assert len(view["posts"][0]["caption"]) == tools.CAPTION_CHARS and "image_url" not in view["posts"][0]
    assert "caption" not in view["posts"][1]  # None values are dropped
    assert model.bound[0]["tools"] == ["search_hashtags", "get_engagement", "get_profile", "get_user_posts", "get_post", "search_instagram"]


def test_history_messages_replays_recent_turns() -> None:
    turns = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}, {"role": "user", "content": "  "}, "junk"]
    msgs = history_messages(turns)
    assert [type(m) for m in msgs] == [HumanMessage, AIMessage] and msgs[0].content == "hi" and msgs[1].content == "hello"
    assert len(history_messages([{"role": "user", "content": str(i)} for i in range(30)])) == runner.MAX_HISTORY
    assert history_messages(None) == []


async def test_history_is_placed_before_the_question() -> None:
    model = ScriptedModel(replies=[AIMessage(content="again: @a")])
    await run_agent("and now?", acquire=fake_acquire, llm=model, history=[{"role": "user", "content": "who?"}, {"role": "assistant", "content": "@a"}])
    kinds = [type(m).__name__ for m in model.seen[0]]
    assert kinds == ["SystemMessage", "HumanMessage", "AIMessage", "HumanMessage"]
    assert model.seen[0][-1].content == "and now?"


async def test_bad_tool_calls_are_reported_to_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    async def missing_profile(session, username):  # noqa: ANN001
        raise InstagramAPIError(f"No profile data for @{username}; does the account exist?")

    monkeypatch.setattr(profiles, "get_profile", missing_profile)
    model = ScriptedModel(
        replies=[
            AIMessage(
                content="",
                tool_calls=[
                    call("nope", {}, "c1"),
                    call("search_hashtags", {"tags": "tea"}, "c2"),
                    call("get_profile", {"username": "ghost"}, "c3"),
                ],
            ),
            AIMessage(content="Nothing worked."),
        ]
    )
    events: list[dict] = []
    result = await run_agent("?", acquire=fake_acquire, llm=model, on_event=events.append)

    assert result["answer"] == "Nothing worked." and result["tool_calls"] == 3 and result["stopped"] is None
    tool_msgs = [m for m in model.seen[1] if isinstance(m, ToolMessage)]
    assert [m.status for m in tool_msgs] == ["error", "error", "error"]
    assert "unknown tool 'nope'" in json.loads(tool_msgs[0].content)["error"]
    assert "invalid arguments for search_hashtags" in json.loads(tool_msgs[1].content)["error"]
    assert json.loads(tool_msgs[2].content)["error"].startswith("No profile data for @ghost")
    ends = runs(events, "run_end", "tool")
    assert all(e["error"] and e["result"] is None for e in ends) and len(ends) == 3
    assert [e["name"] for e in ends] == ["nope", "search_hashtags", "get_profile"]
    assert [e for e in runs(events) if e["name"] == "tools"][0]["outputs"] == {"calls": 3, "failed": 3, "stopped": None}


async def test_throttling_ends_the_run_with_an_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    async def throttled(session, tags, **kwargs):  # noqa: ANN001
        raise RateLimited("HTTP 429")

    monkeypatch.setattr(hashtags, "crawl_hashtags", throttled)
    model = ScriptedModel(
        replies=[
            AIMessage(content="Let me look.", tool_calls=[call("search_hashtags", {"tags": ["tea"]}, "c1"), call("search_hashtags", {"tags": ["coffee"]}, "c2")]),
            AIMessage(content="Instagram throttled me; nothing yet."),
        ]
    )
    events: list[dict] = []
    result = await run_agent("?", acquire=fake_acquire, llm=model, on_event=events.append)

    assert result["stopped"] == "throttled" and result["answer"] == "Instagram throttled me; nothing yet."
    first_llm, last_llm = runs(events, "run_end", "llm")
    assert first_llm["text"] == "Let me look." and last_llm["text"] == "Instagram throttled me; nothing yet."
    tool_ends = runs(events, "run_end", "tool")
    assert "throttling" in tool_ends[0]["error"]
    assert "no more tools" in tool_ends[1]["error"]  # the second call of the same turn was not executed
    assert runs(events, "run_start", "llm")[-1]["inputs"]["tool_choice"] == "none"
    assert [e for e in runs(events) if e["name"] == "tools"][0]["outputs"]["stopped"] == "throttled"
    finish = model.seen[1]
    assert isinstance(finish[-1], HumanMessage) and "throttling" in finish[-1].content
    assert model.bound[-1].get("tool_choice") == "none"


async def test_step_budget_forces_an_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_crawl(session, tags, **kwargs):  # noqa: ANN001
        return CRAWL

    monkeypatch.setattr(hashtags, "crawl_hashtags", fake_crawl)
    model = ScriptedModel(
        replies=[
            AIMessage(content="", tool_calls=[call("search_hashtags", {"tags": ["tea"]})]),
            AIMessage(content="Budget spent: @danfetea, @brewer."),
        ]
    )
    result = await run_agent("?", acquire=fake_acquire, llm=model, max_steps=1)
    assert result["stopped"] == "budget" and result["steps"] == 1 and result["answer"].startswith("Budget spent")
    assert "tool budget" in model.seen[1][-1].content


async def test_empty_reply_gets_a_placeholder_answer() -> None:
    model = ScriptedModel(replies=[AIMessage(content="")])
    result = await run_agent("?", acquire=fake_acquire, llm=model)
    assert result["answer"] == runner.NO_ANSWER


async def test_engagement_tool_compacts_and_describes(monkeypatch: pytest.MonkeyPatch) -> None:
    merged = {
        "post_count": 2,
        "people_count": 2,
        "commenter_count": 1,
        "liker_count": 2,
        "message": "Stopped after 2 of 3 posts: HTTP 429",
        "people": [
            {"username": "fan", "full_name": None, "comment_count": 2, "liked_count": 1, "posts_commented": ["AAA"], "posts_liked": ["AAA"], "is_verified": False, "is_private": None, "profile_pic_url": None, "user_id": "1", "profile_url": "https://www.instagram.com/fan/"},
            {"username": "liker", "comment_count": 0, "liked_count": 1, "posts_commented": [], "posts_liked": ["BBB"]},
        ],
        "posts": [
            {"shortcode": "AAA", "comment_count": 40, "comments": [{"username": "fan", "text": "y" * 300}, {"username": "fan", "text": "ok"}], "like_count": 500, "likers": [{"username": "fan"}], "likers_truncated": True},
            {"shortcode": "BBB", "error": "gone", "comments": [], "likers": []},
        ],
    }
    seen: dict = {}

    async def fake_collect(session, posts, **kwargs):  # noqa: ANN001
        seen.update(posts=posts, **kwargs)
        return merged

    monkeypatch.setattr(engagement, "collect_engagement", fake_collect)
    tool = {t.name: t for t in build_tools(fake_acquire)}["get_engagement"]
    msg = await tool.ainvoke(call("get_engagement", {"posts": ["AAA", "BBB"], "max_comments_per_post": 5}))

    assert seen == {"posts": ["AAA", "BBB"], "max_comments": 5, "include_likers": True}
    view = json.loads(msg.content)
    assert view["message"] == merged["message"] and view["commenter_count"] == 1
    assert view["posts"][0]["comments_fetched"] == 2 and len(view["posts"][0]["sample_comments"][0]["text"]) == tools.COMMENT_CHARS
    assert view["posts"][1] == {"shortcode": "BBB", "error": "gone", "comments_fetched": 0, "likers_fetched": 0}
    assert [p["username"] for p in view["people"]] == ["fan", "liker"] and "profile_url" not in view["people"][0]
    assert msg.artifact == merged
    assert describe_result("get_engagement", merged) == "2 posts: 1 commenter, 2 likers · Stopped after 2 of 3 posts: HTTP 429"


def test_compact_search_caps_the_lists() -> None:
    big = {
        **CRAWL,
        "users": [{"username": f"u{i}", "post_count": 1} for i in range(100)],
        "user_count": 100,
        "posts": [{"shortcode": f"P{i}", "caption": "hi"} for i in range(50)],
        "post_count": 50,
    }
    view = compact_search(big)
    assert len(view["accounts"]) == tools.MODEL_ACCOUNTS and len(view["posts"]) == tools.MODEL_POSTS
    assert "top 80 of 100" in view["note"] and "first 40 of 50" in view["note"]
    assert "note" not in compact_search(CRAWL)
    assert describe_result("search_hashtags", CRAWL) == "#tea: 2 posts, 2 accounts"
    assert describe_result("search_hashtags", None, "boom") == "boom"
    assert describe_result("get_profile", {"username": "a", "followers": 1200, "following": 1, "posts_count": 1}) == "@a: 1200 followers, 1 following, 1 post"
    assert describe_result("search_instagram", {"users": [{}], "hashtags": [], "places": [{}, {}]}) == "1 account, 0 hashtags, 2 places"


def test_describe_reports_a_missing_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_llm():
        raise LLMConfigurationError("No LLM provider configured")

    monkeypatch.setattr(runner, "get_llm", no_llm)
    assert describe() == {"configured": False, "models": [], "message": "No LLM provider configured"}


def test_describe_names_the_langsmith_project(monkeypatch: pytest.MonkeyPatch) -> None:
    class Chain:
        model_names = ["gemini:a", "groq:b"]

    monkeypatch.setattr(runner, "get_llm", Chain)
    assert describe() == {"configured": True, "models": ["gemini:a", "groq:b"], "message": None, "langsmith": None}
    # patched rather than set in the environment: langsmith caches what it reads from there
    monkeypatch.setattr(runner.langsmith_utils, "tracing_is_enabled", lambda: True)
    monkeypatch.setattr(runner.langsmith_utils, "get_tracer_project", lambda: "scrapper")
    assert describe()["langsmith"] == "scrapper"


async def test_blank_question_is_rejected() -> None:
    with pytest.raises(ValueError):
        await run_agent("   ", acquire=fake_acquire, llm=ScriptedModel(replies=[]))


def test_split_reply_separates_reasoning_from_the_answer() -> None:
    blocks = AIMessage(content=[{"type": "reasoning", "reasoning": "Crawl first."}, {"type": "text", "text": "@a posts most."}])
    assert split_reply(blocks) == ("Crawl first.", "@a posts most.")
    groq = AIMessage(content="Answer.", additional_kwargs={"reasoning_content": "Use the tool."})
    assert split_reply(groq) == ("Use the tool.", "Answer.")
    inline = AIMessage(content="<think>\nweigh the options\n</think>\n\nFinal answer.")
    assert split_reply(inline) == ("weigh the options", "Final answer.")
    assert split_reply(AIMessage(content="plain")) == ("", "plain")


async def test_reasoning_is_emitted_per_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_crawl(session, tags, **kwargs):  # noqa: ANN001
        return CRAWL

    monkeypatch.setattr(hashtags, "crawl_hashtags", fake_crawl)
    model = ScriptedModel(
        replies=[
            AIMessage(
                content=[{"type": "reasoning", "reasoning": "Start with the hashtag crawl."}, {"type": "text", "text": "Crawling #tea."}],
                tool_calls=[call("search_hashtags", {"tags": ["tea"]})],
            ),
            AIMessage(content=[{"type": "reasoning", "reasoning": "Two accounts, rank them."}, {"type": "text", "text": "@danfetea, @brewer"}]),
        ]
    )
    events: list[dict] = []
    result = await run_agent("?", acquire=fake_acquire, llm=model, on_event=events.append)

    first, second = runs(events, "run_end", "llm")
    assert first["reasoning"] == "Start with the hashtag crawl." and first["text"] == "Crawling #tea." and first["step"] == 1
    assert second["reasoning"] == "Two accounts, rank them." and second["text"] == "@danfetea, @brewer" and second["step"] == 2
    assert result["answer"] == "@danfetea, @brewer"  # reasoning never leaks into the answer


class FailingModel(ScriptedModel):
    """Rejects every request with an HTTP 400, like a model that does not accept a thinking budget."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        error = RuntimeError("400 INVALID_ARGUMENT: thinking is not supported by this model")
        error.status_code = 400  # type: ignore[attr-defined]
        raise error


async def test_a_rejected_reasoning_request_falls_back_to_the_plain_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    plain = ScriptedModel(replies=[AIMessage(content="answered without reasoning")])
    monkeypatch.setattr(runner, "get_reasoning_llm", lambda: FailingModel(replies=[]))
    monkeypatch.setattr(runner, "get_llm", lambda: plain)
    events: list[dict] = []
    result = await run_agent("?", acquire=fake_acquire, on_event=events.append)
    assert result["answer"] == "answered without reasoning" and len(plain.seen) == 1
    rejected, answered = runs(events, "run_end", "llm")  # the rejected call stays visible as a failed llm run
    assert rejected["name"] == "FailingModel" and "INVALID_ARGUMENT" in rejected["error"]
    assert answered["name"] == "ScriptedModel" and answered["error"] is None
    assert runs(events)[-1]["run_type"] == "chain" and runs(events)[-1]["error"] is None


async def test_a_rejected_request_without_a_spare_is_raised() -> None:
    events: list[dict] = []
    with pytest.raises(RuntimeError, match="INVALID_ARGUMENT"):
        await run_agent("?", acquire=fake_acquire, llm=FailingModel(replies=[]), on_event=events.append)
    # every run that was opened is closed with the error, root last
    assert shape(events) == [
        ("start", "chain", runner.AGENT_NAME, 0),
        ("start", "llm", "FailingModel", 1),
        ("end", "llm", "FailingModel", 1),
        ("end", "chain", runner.AGENT_NAME, 0),
    ]
    assert all("INVALID_ARGUMENT" in e["error"] for e in runs(events))


async def test_profile_tool_reads_the_profile_page(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = {
        "id": "797343551", "username": "dilmahceylontea", "full_name": "Dilmah Tea", "biography": "b" * 500,
        "bio_links": [{"title": "Shop", "url": "https://shop.dilmahtea.com/"}], "followers": 35684, "following": 86,
        "posts_count": 4101, "is_private": False, "profile_pic_url": "https://cdn/hd.jpg",
        "recent_posts": [{"shortcode": "AAA", "owner": "teafriend", "coauthors": ["dilmahceylontea"], "image_url": "x"}],
    }

    async def fake_profile(session, username):  # noqa: ANN001
        assert username == "@dilmahceylontea"
        return profile

    monkeypatch.setattr(profiles, "get_profile", fake_profile)
    tool = {t.name: t for t in build_tools(fake_acquire)}["get_profile"]
    msg = await tool.ainvoke(call("get_profile", {"username": "@dilmahceylontea"}))
    view = json.loads(msg.content)
    assert view["full_name"] == "Dilmah Tea" and view["followers"] == 35684 and "profile_pic_url" not in view
    assert len(view["biography"]) == tools.BIO_CHARS and view["bio_links"] == ["https://shop.dilmahtea.com/"]
    assert view["recent_posts"] == [{"shortcode": "AAA", "owner": "teafriend", "coauthors": ["dilmahceylontea"]}]
    assert msg.artifact == profile
    assert describe_result("get_profile", profile) == "@dilmahceylontea (Dilmah Tea): 35684 followers, 86 following, 4101 posts"


async def test_langchain_records_the_same_run_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """What LangSmith would record (LangChain's own run collector) matches the events, run for run."""

    async def fake_crawl(session, tags, **kwargs):  # noqa: ANN001
        return CRAWL

    monkeypatch.setattr(hashtags, "crawl_hashtags", fake_crawl)
    model = ScriptedModel(
        replies=[
            AIMessage(content="", tool_calls=[call("search_hashtags", {"tags": ["tea"]})]),
            AIMessage(content="@danfetea"),
        ]
    )
    events: list[dict] = []
    with collect_runs() as collector:
        await run_agent("who?", acquire=fake_acquire, llm=model, on_event=events.append)

    (root,) = collector.traced_runs

    def tree(run) -> tuple:  # noqa: ANN001
        kids = sorted(run.child_runs, key=lambda r: r.dotted_order)
        return (run.run_type, run.name, str(run.id), [tree(k) for k in kids])

    started = {e["run_id"]: e for e in runs(events, "run_start")}
    by_parent: dict = {}
    for e in started.values():
        by_parent.setdefault(e["parent_id"], []).append(e)

    def expected(run_id: str) -> tuple:
        e = started[run_id]
        return (e["run_type"], e["name"], run_id, [expected(k["run_id"]) for k in by_parent.get(run_id, [])])

    assert tree(root) == expected(str(root.id))
    assert [r.run_type for r in root.child_runs] == ["llm", "chain", "llm"]
    assert root.name == runner.AGENT_NAME and root.tags == runner.TRACE_TAGS and root.outputs["answer"] == "@danfetea"
