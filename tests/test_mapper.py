"""The hashtag-mapping agent, with a scripted model, a fake crawl and a temporary graph database."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from test_agent import ScriptedModel, call, fake_acquire, runs

from app.agent import mapper
from app.graph import GraphStore, GraphStoreError
from app.instagram import hashtags
from app.instagram.api import RateLimited

POSTS = {
    "concertslk": [
        {"shortcode": "A", "caption": "#concertslk #srilanka #colombo #love"},
        {"shortcode": "B", "caption": "#concertslk #srilanka #livemusic"},
    ],
    "srilanka": [
        {"shortcode": "B", "caption": "#concertslk #srilanka #livemusic"},
        {"shortcode": "C", "caption": "#srilanka #colombo #travel"},
    ],
}


@pytest.fixture
def crawled(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    calls: list[dict] = []

    async def fake_crawl(session, tag, **kwargs):  # noqa: ANN001
        calls.append({"tag": tag, **kwargs})
        if tag == "throttled":
            raise RateLimited("HTTP 429")
        return {"tag": tag, "posts": POSTS.get(tag, [])}

    monkeypatch.setattr(hashtags, "crawl_hashtag", fake_crawl)
    return calls


async def test_the_agent_builds_and_stores_the_map(tmp_path: Path, crawled: list[dict]) -> None:
    model = ScriptedModel(replies=[
        AIMessage(content="", tool_calls=[call("explore_hashtag", {"tag": "#concertslk"}, "c1")]),
        AIMessage(content="", tool_calls=[
            call("add_hashtags", {"tags": ["srilanka", "colombo", "livemusic", "madeup"]}, "c2"),
            call("explore_hashtag", {"tag": "srilanka"}, "c3"),
        ]),
        AIMessage(content="", tool_calls=[call("explore_hashtag", {"tag": "concertslk"}, "c4")]),
        AIMessage(content="**Clusters**: #srilanka, #colombo"),
    ])
    store = GraphStore(tmp_path / "graph")
    events: list[dict] = []
    result = await mapper.run_mapper(
        "concertslk", acquire=fake_acquire, store=store, llm=model, on_event=events.append, max_nodes=4, max_searches=3,
        posts_per_search=48,
    )

    assert {k: result[k] for k in ("answer", "map", "nodes", "edges", "saved", "stopped", "tool_calls")} == {
        "answer": "**Clusters**: #srilanka, #colombo", "map": "concertslk", "nodes": 4, "edges": 6, "saved": True,
        "stopped": None, "tool_calls": 4,
    }
    assert crawled == [
        {"tag": "concertslk", "max_posts": 48, "max_scrolls": 1, "include_posts": True},
        {"tag": "srilanka", "max_posts": 48, "max_scrolls": 1, "include_posts": True},
    ]
    tools = runs(events, run_type="tool")
    assert [t["summary"] for t in tools] == [
        "#concertslk: 2 posts, 4 other hashtags (4 new) · graph 1/4",
        "added 3: #srilanka, #colombo, #livemusic · 1 skipped · graph 4/4",
        "#srilanka: 2 posts, 4 other hashtags (1 new) · graph 4/4",
        "#concertslk was already explored; explore another node",
    ]
    assert tools[3]["error"] and runs(events, run_type="chain")[-1]["name"] == mapper.AGENT_NAME

    first = json.loads(tools[0]["output"])
    assert first["new_candidates"][0] == {"tag": "srilanka", "posts": 2, "count": 2}
    assert first["graph"]["searches_left"] == 2 and first["graph"]["best_candidates"][0]["tag"] == "srilanka"
    assert first["graph"]["top_nodes"] == [{"tag": "concertslk", "count": 0, "explored": True}]
    assert json.loads(tools[2]["output"])["graph"]["top_nodes"][0] == {"tag": "srilanka", "count": 2, "explored": True}
    assert "note" in json.loads(tools[2]["output"])["graph"]  # full: exploring now only adds links

    graphs = [e for e in events if e["type"] == "graph"]
    assert [(len(g["nodes"]), g["status"]) for g in graphs] == [(1, "running"), (1, "running"), (4, "running"), (4, "running"), (4, "done")]
    assert all(g["saved"] for g in graphs)
    stored = await store.load("concertslk")
    assert stored["status"] == "done" and stored["summary"] == "**Clusters**: #srilanka, #colombo"
    assert [(n["tag"], n["count"], n["explored"]) for n in stored["nodes"]] == [
        ("concertslk", 1, True), ("srilanka", 2, True), ("colombo", 2, False), ("livemusic", 1, False),
    ]
    prompt = model.seen[0][0].content
    assert "at most 4 nodes" in prompt and "You have 3 searches" in prompt and "add about 5 candidates" in prompt
    assert mapper.per_search(mapper.HashtagGraph(["a"], max_nodes=50), 10) == 5
    assert mapper.per_search(mapper.HashtagGraph(["a"], max_nodes=100), 4) == 25


async def test_a_spent_search_budget_ends_the_run(tmp_path: Path, crawled: list[dict]) -> None:
    model = ScriptedModel(replies=[
        AIMessage(content="", tool_calls=[call("explore_hashtag", {"tag": "concertslk"}, "c1")]),
        AIMessage(content="", tool_calls=[call("explore_hashtag", {"tag": "srilanka"}, "c2")]),
        AIMessage(content="Mapped what one search allowed."),
    ])
    events: list[dict] = []
    result = await mapper.run_mapper(
        "concertslk", acquire=fake_acquire, store=GraphStore(tmp_path / "g"), llm=model, on_event=events.append, max_searches=1
    )
    assert result["stopped"] == "budget" and len(crawled) == 1
    last = json.loads(runs(events, run_type="tool")[0]["output"])["graph"]
    assert last["searches_left"] == 0 and last["note"].startswith("No searches left. Fill the 49 free slots")
    assert runs(events, run_type="tool")[1]["error"] == "the search budget is spent (1 searches)"
    assert [e for e in events if e["type"] == "graph"][-1]["status"] == "done"


async def test_throttling_stops_the_run_and_marks_the_map(tmp_path: Path, crawled: list[dict]) -> None:
    POSTS["concertslk"].append({"shortcode": "T", "caption": "#concertslk #throttled"})
    try:
        model = ScriptedModel(replies=[
            AIMessage(content="", tool_calls=[call("explore_hashtag", {"tag": "concertslk"}, "c1")]),
            AIMessage(content="", tool_calls=[call("explore_hashtag", {"tag": "throttled"}, "c2")]),
            AIMessage(content="Stopped early."),
        ])
        store = GraphStore(tmp_path / "g")
        result = await mapper.run_mapper("concertslk", acquire=fake_acquire, store=store, llm=model)
    finally:
        POSTS["concertslk"].pop()
    assert result["stopped"] == "throttled"
    assert (await store.load("concertslk"))["status"] == "stopped (throttled)"


async def test_a_failing_database_does_not_stop_the_map(tmp_path: Path, crawled: list[dict]) -> None:
    class BrokenStore(GraphStore):
        async def save(self, snapshot: dict) -> None:
            raise GraphStoreError("files are locked")

    events: list[dict] = []
    model = ScriptedModel(replies=[
        AIMessage(content="", tool_calls=[call("explore_hashtag", {"tag": "concertslk"}, "c1")]),
        AIMessage(content="done"),
    ])
    result = await mapper.run_mapper("concertslk", acquire=fake_acquire, store=BrokenStore(tmp_path / "g"), llm=model, on_event=events.append)
    assert result["saved"] is False and result["answer"] == "done"
    assert all(e["save_error"] == "files are locked" for e in events if e["type"] == "graph")


async def test_too_many_seeds(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at most 3"):
        await mapper.run_mapper("a b c d", acquire=fake_acquire, store=GraphStore(tmp_path / "g"), llm=ScriptedModel(replies=[]))
