"""A map of related hashtags, built from the posts that hashtag searches return.

Searching ``#A`` returns posts; the other hashtags in their captions are what ``#A`` co-occurs with.
The map keeps, for every searched hashtag, how many of its posts carried each other hashtag. From that:

* a **node** is a hashtag admitted to the map (the seeds, then whatever the agent adds), capped at
  ``max_nodes``;
* an **edge** ``A -> B`` (both nodes) means posts found by searching ``#A`` also carry ``#B``; its
  ``count`` is how many of them;
* a hashtag's **count** is how many distinct collected posts carry it, counting only posts that a
  search for some *other* hashtag returned (a hashtag's own search would trivially return it).
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

# Letters, digits and underscores in any script. ``\w`` misses the vowel signs of the Indic scripts
# (Sinhala, Tamil, Devanagari...), so their blocks are allowed explicitly, with the zero-width joiners.
_HASHTAG = re.compile(r"#([\wऀ-෿‌‍]+)")
MAX_TAG_CHARS = 100


def extract_hashtags(text: str | None) -> list[str]:
    """Hashtags in a caption, lower-cased and deduplicated in order of appearance (``#2024`` is not one)."""
    seen: dict[str, None] = {}
    for match in _HASHTAG.finditer(text or ""):
        tag = match.group(1).lower()
        if tag.isdigit() or len(tag) > MAX_TAG_CHARS:
            continue
        seen.setdefault(tag, None)
    return list(seen)


def ranked(counts: Counter[str]) -> list[tuple[str, int]]:
    """``(hashtag, n)`` most frequent first, ties alphabetical (a caption's hashtags come as a set)."""
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))


def clean_tag(raw: str) -> str:
    """``" #ConcertSL "`` -> ``"concertsl"``."""
    return str(raw).strip().lstrip("#").strip().lower()


@dataclass
class Node:
    tag: str
    order: int  # admission order
    depth: int  # 0 for a seed, else one more than the shallowest searched hashtag that led to it
    seed: bool = False
    found_via: str | None = None  # the searched hashtag whose posts carried it most
    explored: bool = False
    posts_read: int = 0  # posts its own search returned


class HashtagGraph:
    """The working state of one map; :meth:`snapshot` is what gets stored and drawn."""

    def __init__(self, seeds: list[str], *, max_nodes: int = 50, max_searches: int | None = None) -> None:
        cleaned = list(dict.fromkeys(t for t in (clean_tag(s) for s in seeds) if t))
        if not cleaned:
            raise ValueError("no seed hashtags given")
        self.seeds = cleaned
        self.id = "+".join(cleaned)
        self.max_nodes = max(max_nodes, len(cleaned))
        self.max_searches = max_searches
        self.status = "running"
        self.summary = ""
        self.nodes: dict[str, Node] = {}
        self.post_tags: dict[str, set[str]] = {}  # post -> hashtags in its caption
        self.post_sources: dict[str, set[str]] = {}  # post -> searched hashtags that returned it
        self.found: dict[str, Counter[str]] = {}  # searched hashtag -> other hashtags over its posts
        for tag in cleaned:
            self._admit(tag, depth=0, seed=True)

    # ---- state ----------------------------------------------------------------------------
    @property
    def full(self) -> bool:
        return len(self.nodes) >= self.max_nodes

    @property
    def searches(self) -> int:
        return len(self.found)

    @property
    def searches_left(self) -> int | None:
        return None if self.max_searches is None else max(0, self.max_searches - self.searches)

    def _admit(self, tag: str, *, depth: int, seed: bool = False, found_via: str | None = None) -> Node:
        node = self.nodes[tag] = Node(tag=tag, order=len(self.nodes), depth=depth, seed=seed, found_via=found_via)
        return node

    def counts(self) -> Counter[str]:
        """How many collected posts carry each hashtag, found while searching some other hashtag."""
        counts: Counter[str] = Counter()
        for post, tags in self.post_tags.items():
            sources = self.post_sources.get(post, set())
            for tag in tags:
                if sources - {tag}:
                    counts[tag] += 1
        return counts

    def seen_in(self, tag: str) -> list[tuple[str, int]]:
        """``(searched hashtag, posts)`` for every search whose posts carried ``tag``."""
        return [(source, found[tag]) for source, found in self.found.items() if found.get(tag)]

    # ---- changes ---------------------------------------------------------------------------
    def record_search(self, tag: str, posts: list[dict[str, Any]]) -> dict[str, Any]:
        """Take in the posts a search for ``tag`` returned.

        Returns ``{"posts", "tagged_posts", "found"}``: posts read, how many carried another hashtag,
        and the other hashtags with how many of these posts carried each."""
        tag = clean_tag(tag)
        found: Counter[str] = Counter()
        read = tagged = 0
        for post in posts:
            key = str(post.get("shortcode") or post.get("id") or "")
            if not key:
                continue
            read += 1
            tags = set(extract_hashtags(post.get("caption")))
            self.post_tags.setdefault(key, set()).update(tags)
            self.post_sources.setdefault(key, set()).add(tag)
            tags.discard(tag)
            if tags:
                tagged += 1
            found.update(tags)
        self.found[tag] = self.found.get(tag, Counter()) + found
        node = self.nodes.get(tag)
        if node is not None:
            node.explored = True
            node.posts_read += read
        return {"posts": read, "tagged_posts": tagged, "found": found}

    def admit_for_search(self, tag: str) -> None:
        """Make ``tag`` a node before it is searched; raises ``ValueError`` saying why it cannot be."""
        tag = clean_tag(tag)
        if tag in self.nodes:
            return
        seen = self.seen_in(tag)
        if not seen:
            raise ValueError(f"#{tag} did not appear in any searched post; explore a node of the graph or one of its candidates")
        if self.full:
            raise ValueError(f"the graph is full ({self.max_nodes} nodes); only hashtags already in it can be explored")
        self._admit(tag, depth=self._depth(seen), found_via=self._via(seen))

    def add(self, tags: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """Admit hashtags seen in searched posts. Returns ``(added, skipped)``."""
        counts = self.counts()
        added: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        for raw in tags:
            tag = clean_tag(raw)
            if not tag:
                continue
            if tag in self.nodes:
                skipped.append({"tag": tag, "reason": "already in the graph"})
                continue
            seen = self.seen_in(tag)
            if not seen:
                skipped.append({"tag": tag, "reason": "not in any searched post"})
                continue
            if self.full:
                skipped.append({"tag": tag, "reason": f"the graph is full ({self.max_nodes} nodes)"})
                continue
            node = self._admit(tag, depth=self._depth(seen), found_via=self._via(seen))
            added.append({"tag": tag, "count": counts[tag], "found_via": node.found_via})
        return added, skipped

    def _depth(self, seen: list[tuple[str, int]]) -> int:
        depths = [self.nodes[source].depth for source, _ in seen if source in self.nodes]
        return min(depths) + 1 if depths else 1

    @staticmethod
    def _via(seen: list[tuple[str, int]]) -> str:
        return max(seen, key=lambda item: item[1])[0]  # ties: the earliest search

    # ---- views -------------------------------------------------------------------------------
    def edges(self) -> list[dict[str, Any]]:
        out = []
        for source, found in self.found.items():
            if source not in self.nodes:
                continue
            for target, count in ranked(found):
                if target != source and target in self.nodes:
                    out.append({"source": source, "target": target, "count": count})
        return out

    def candidates(self, counts: Counter[str] | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        """Hashtags seen in searched posts that are not nodes yet, most frequent first."""
        counts = self.counts() if counts is None else counts
        rows = sorted(((t, n) for t, n in counts.items() if t not in self.nodes), key=lambda r: (-r[1], r[0]))
        return [{"tag": t, "count": n} for t, n in rows[:limit]]

    def unexplored(self, counts: Counter[str] | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        """Nodes not searched yet, most frequent first."""
        counts = self.counts() if counts is None else counts
        rows = sorted((n for n in self.nodes.values() if not n.explored), key=lambda n: (-counts[n.tag], n.order))
        return [{"tag": n.tag, "count": counts[n.tag]} for n in rows[:limit]]

    def snapshot(self) -> dict[str, Any]:
        counts = self.counts()
        edges = self.edges()
        degree: Counter[str] = Counter()
        for edge in edges:
            degree[edge["source"]] += 1
            degree[edge["target"]] += 1
        nodes = [
            {
                "tag": n.tag,
                "count": counts[n.tag],
                "seed": n.seed,
                "explored": n.explored,
                "posts_read": n.posts_read,
                "depth": n.depth,
                "found_via": n.found_via,
                "degree": degree[n.tag],
                "order": n.order,
            }
            for n in sorted(self.nodes.values(), key=lambda n: n.order)
        ]
        return {
            "id": self.id,
            "seeds": list(self.seeds),
            "max_nodes": self.max_nodes,
            "max_searches": self.max_searches,
            "status": self.status,
            "summary": self.summary,
            "searches": self.searches,
            "posts": len(self.post_tags),
            "nodes": nodes,
            "edges": edges,
        }
