"""The hashtag-mapping agent: seed hashtags in, a graph of related hashtags out, kept in the graph database.

The model drives the exploration of one :class:`app.graph.HashtagGraph` with two tools:

``explore_hashtag``  one Instagram search: the newest posts under a hashtag. The other hashtags in their
                     captions link it to the graph's nodes and become candidates.
``add_hashtags``     admit candidates as nodes (only hashtags seen in searched posts; no requests)

It judges which candidates belong to the seeds' topic and which node to search next, hashtag by hashtag,
until the graph holds ``max_nodes`` hashtags or the search budget is spent. After every change the map
is saved to the graph database and a ``graph`` event carries its snapshot, so a page can draw the graph
as it grows. The loop is :func:`app.agent.runner.run_agent`, so the run tree (chain / llm / tool runs),
LangSmith tracing and the ``final`` event are the same as the Q&A agent's.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.agent.runner import EventSink, ToolRefused, run_agent
from app.agent.tools import Acquire
from app.graph import GraphStore, GraphStoreError, HashtagGraph, clean_tag
from app.graph.model import ranked
from app.instagram import hashtags

log = logging.getLogger(__name__)

AGENT_NAME = "Hashtag Mapper agent"  # the root run
TRACE_TAGS = ["hashtag-scout", "hashtag-map"]
MAX_NODES = 50
MAX_SEARCHES = 10
POSTS_PER_SEARCH = 24  # one page of the search grid
MAX_SEEDS = 3

# What the model gets to see of a result.
MODEL_CANDIDATES = 30  # new hashtags listed per search
MODEL_LINKS = 20  # graph nodes a search linked to
MODEL_FRONTIER = 12  # unexplored nodes and best candidates, in every result
MODEL_TOP = 10  # the graph's most frequent hashtags, in every result

SYSTEM_PROMPT = """You are Hashtag Mapper. You build a map of related Instagram hashtags around the seed hashtags: a graph stored in a graph database. Every node is a hashtag. An edge A -> B means posts found by searching #A also carry #B. A node's count is how many collected posts, found while searching other hashtags, carry it.

Your tools
- explore_hashtag searches one hashtag: it reads the newest posts under it, links it to the graph's nodes that appear in their captions, and lists the new candidate hashtags with how many of those posts carried them.
- add_hashtags adds candidates to the graph as nodes. Only hashtags that appeared in searched posts can be added. It costs no Instagram requests.

How to work
1. Explore every seed hashtag first.
2. After each search, add about {per_search} candidates: the ones more posts carried that belong to the seeds' topic (the same scene, place, event, genre, artists, bands, venues, organisers or brands). Adding only a few per search lets the graph grow hashtag by hashtag, so later searches still have room to bring something new. Skip generic engagement hashtags (#love, #instagood, #photooftheday, #reels, #viral, #explore, #fyp, #trending, #follow, #like4like and the like) and spam, unless the seeds are about them.
3. Then go hashtag by hashtag: explore the most promising unexplored node (on topic, high count), add the related hashtags it newly discovers, and repeat. Call add_hashtags and the next explore_hashtag in the same turn.
   Keep the map broad: prefer hashtags that open another part of the topic (another artist, venue, city, genre, event) over near-duplicates of one already explored (spellings, fan tags, tour names of the same artist); the near-duplicates get linked anyway. Do not search hashtags that are also everyday words (a song called #yellow, #music, #tour): their posts are mostly about something else.
4. The graph holds at most {max_nodes} nodes. When it is full and searches are left, keep exploring the unexplored nodes with the highest counts: each search links hashtags already in the graph. When no searches are left and the graph still has free slots, fill them with add_hashtags from best_candidates (on topic, most frequent first; it costs no requests), then stop.
5. You have {max_searches} searches and each one spends real Instagram requests. Never explore a hashtag twice. If a tool reports throttling or a login problem, stop at once.

When you stop, write a short summary in markdown: the clusters you see (groups of related hashtags), the top hashtags by count, which hashtags were explored, and anything that was cut short. Counts grow with every search, so take them from top_nodes in the latest tool result. Use only hashtags and numbers from the tool results. No preamble."""


class ExploreArgs(BaseModel):
    tag: str = Field(min_length=1, description="The hashtag to search, with or without '#'.")


class AddArgs(BaseModel):
    tags: list[str] = Field(min_length=1, max_length=100, description="Candidate hashtags to add as nodes.")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def describe_map_result(name: str, artifact: dict | None) -> str:
    """One line per tool result, for the trace."""
    if not artifact:
        return "done"
    graph = f"graph {artifact['nodes']}/{artifact['max_nodes']}"
    if name == "explore_hashtag":
        return (
            f"#{artifact['tag']}: {_plural(artifact['posts'], 'post')}, {_plural(artifact['found'], 'other hashtag')}"
            f" ({artifact['new']} new) · {graph}"
        )
    if name == "add_hashtags":
        added = artifact.get("added") or []
        text = f"added {len(added)}"
        if added:
            text += ": " + ", ".join(f"#{t}" for t in added[:6]) + ("…" if len(added) > 6 else "")
        if artifact.get("skipped"):
            text += f" · {artifact['skipped']} skipped"
        return f"{text} · {graph}"
    return graph


class Mapper:
    """One mapping run: the graph, its persistence, and the tools the model drives it with."""

    def __init__(
        self,
        graph: HashtagGraph,
        *,
        acquire: Acquire,
        store: GraphStore,
        emit: EventSink,
        posts_per_search: int = POSTS_PER_SEARCH,
    ) -> None:
        self.graph = graph
        self.acquire = acquire
        self.store = store
        self.emit = emit
        self.posts_per_search = posts_per_search
        self.save_error: str | None = None

    async def changed(self) -> None:
        """Save the map and tell the page. A database failure is reported, not fatal: the run goes on."""
        snap = self.graph.snapshot()
        try:
            await self.store.save(snap)
            self.save_error = None
        except GraphStoreError as exc:
            log.warning("could not save map %s: %s", self.graph.id, exc)
            self.save_error = str(exc)
        self.emit({"type": "graph", **snap, "saved": self.save_error is None, "save_error": self.save_error})

    def status(self, counts: Any = None) -> dict[str, Any]:
        """Where the graph stands, appended to every tool result."""
        g = self.graph
        counts = g.counts() if counts is None else counts
        view: dict[str, Any] = {
            "nodes": len(g.nodes),
            "max_nodes": g.max_nodes,
            "edges": len(g.edges()),
            "searches_used": g.searches,
            "searches_left": g.searches_left,
            "top_nodes": [
                {"tag": n.tag, "count": counts[n.tag], "explored": n.explored}
                for n in sorted(g.nodes.values(), key=lambda n: (-counts[n.tag], n.order))[:MODEL_TOP]
            ],
            "unexplored_nodes": g.unexplored(counts, MODEL_FRONTIER),
        }
        if g.full:
            view["note"] = "The graph is full: no more hashtags can be added. Exploring a node now only adds links between hashtags already in it."
        else:
            view["best_candidates"] = g.candidates(counts, MODEL_FRONTIER)
            if g.searches_left == 0:
                free = g.max_nodes - len(g.nodes)
                view["note"] = (
                    f"No searches left. Fill the {free} free slots with add_hashtags from best_candidates "
                    "(on topic, most frequent first), then write the summary."
                )
        return view

    def _artifact(self, **fields: Any) -> dict[str, Any]:
        return {**fields, "nodes": len(self.graph.nodes), "max_nodes": self.graph.max_nodes}

    async def explore(self, tag: str) -> tuple[str, dict]:
        g = self.graph
        name = clean_tag(tag)
        if not name:
            raise ToolRefused("empty hashtag")
        if name in g.found:
            raise ToolRefused(f"#{name} was already explored; explore another node")
        if g.searches_left == 0:
            raise ToolRefused(f"the search budget is spent ({g.max_searches} searches)", stop="budget")
        try:
            g.admit_for_search(name)
        except ValueError as exc:
            raise ToolRefused(str(exc)) from None
        async with self.acquire() as session:
            result = await hashtags.crawl_hashtag(
                session,
                name,
                max_posts=self.posts_per_search,
                max_scrolls=max(0, math.ceil(self.posts_per_search / 24) - 1),
                include_posts=True,
            )
        stats = g.record_search(name, result.get("posts") or [])
        await self.changed()
        counts = g.counts()
        found = ranked(stats["found"])
        view: dict[str, Any] = {
            "hashtag": name,
            "posts_read": stats["posts"],
            "posts_with_other_hashtags": stats["tagged_posts"],
            "other_hashtags_found": len(found),
            "linked_to_graph": [{"tag": t, "posts": n} for t, n in found if t in g.nodes][:MODEL_LINKS],
            "new_candidates": [{"tag": t, "posts": n, "count": counts[t]} for t, n in found if t not in g.nodes][
                :MODEL_CANDIDATES
            ],
            "graph": self.status(counts),
        }
        if result.get("message"):
            view["note"] = result["message"]
        new = sum(1 for t, _ in found if t not in g.nodes)
        artifact = self._artifact(tag=name, posts=stats["posts"], found=len(found), new=new)
        return json.dumps(view, ensure_ascii=False), artifact

    async def add(self, tags: list[str]) -> tuple[str, dict]:
        added, skipped = self.graph.add(tags)
        if added:
            await self.changed()
        view = {"added": added, "skipped": skipped, "graph": self.status()}
        artifact = self._artifact(added=[a["tag"] for a in added], skipped=len(skipped))
        return json.dumps(view, ensure_ascii=False), artifact

    def tools(self) -> list[StructuredTool]:
        return [
            StructuredTool.from_function(
                coroutine=self.explore,
                name="explore_hashtag",
                description=(
                    "Search one hashtag on Instagram (one search from the budget, about "
                    f"{self.posts_per_search} newest posts). Links it to the graph's nodes found in those posts' "
                    "captions and lists the new candidate hashtags with how many of the posts carried them. "
                    "The hashtag must be a seed, a node, or a candidate from an earlier result."
                ),
                args_schema=ExploreArgs,
                response_format="content_and_artifact",
            ),
            StructuredTool.from_function(
                coroutine=self.add,
                name="add_hashtags",
                description=(
                    "Add candidate hashtags to the graph as nodes, linked to every explored hashtag whose posts "
                    "carried them. Only hashtags that appeared in searched posts can be added. No Instagram requests."
                ),
                args_schema=AddArgs,
                response_format="content_and_artifact",
            ),
        ]


def per_search(graph: HashtagGraph, max_searches: int) -> int:
    """How many hashtags to add after each search so the graph fills up over the whole budget."""
    return max(5, math.ceil((graph.max_nodes - len(graph.seeds)) / max(1, max_searches)))


async def run_mapper(
    seeds: str | list[str],
    *,
    acquire: Acquire,
    store: GraphStore,
    llm: BaseChatModel | None = None,
    on_event: EventSink | None = None,
    max_nodes: int = MAX_NODES,
    max_searches: int = MAX_SEARCHES,
    posts_per_search: int = POSTS_PER_SEARCH,
) -> dict:
    """Build (or rebuild) the map for ``seeds`` and keep it in ``store``.

    Events are the agent's (``run_start`` / ``run_end`` / ``final``) plus a ``graph`` snapshot after every
    change. Returns the agent's result plus the ``map`` id, its ``nodes`` and ``edges`` counts, and
    whether the last save worked.
    """
    names = hashtags.parse_tags(seeds)
    if len(names) > MAX_SEEDS:
        raise ValueError(f"at most {MAX_SEEDS} seed hashtags")
    emit: EventSink = on_event or (lambda event: None)
    graph = HashtagGraph(names, max_nodes=max_nodes, max_searches=max_searches)
    mapper = Mapper(graph, acquire=acquire, store=store, emit=emit, posts_per_search=posts_per_search)
    await mapper.changed()
    question = (
        f"Map the hashtags related to {', '.join('#' + n for n in names)}: up to {graph.max_nodes} nodes, "
        f"at most {max_searches} searches of about {posts_per_search} posts each."
    )
    try:
        result = await run_agent(
            question,
            llm=llm,
            on_event=emit,
            max_steps=2 * max_searches + 4,  # a turn to add and one to search, should the model split them
            tools=mapper.tools(),
            system_prompt=SYSTEM_PROMPT.format(
                max_nodes=graph.max_nodes, max_searches=max_searches, per_search=per_search(graph, max_searches)
            ),
            name=AGENT_NAME,
            trace_tags=TRACE_TAGS,
            describe_tool=describe_map_result,
        )
    except asyncio.CancelledError:
        graph.status = "cancelled"
        await mapper.changed()
        raise
    except Exception as exc:
        graph.status = "failed"
        graph.summary = f"{type(exc).__name__}: {exc}"
        await mapper.changed()
        raise
    graph.status = "done" if result["stopped"] in (None, "budget") else f"stopped ({result['stopped']})"
    graph.summary = result["answer"]
    await mapper.changed()
    return {
        **result,
        "map": graph.id,
        "nodes": len(graph.nodes),
        "edges": len(graph.edges()),
        "saved": mapper.save_error is None,
    }
