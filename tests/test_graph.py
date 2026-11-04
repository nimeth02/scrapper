"""The hashtag map (pure graph logic) and its graph database (LadybugDB in a temporary directory)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.graph import GraphStore, HashtagGraph, extract_hashtags
from app.graph.store import _rows


def post(code: str, caption: str) -> dict:
    return {"shortcode": code, "caption": caption}


def test_extract_hashtags() -> None:
    caption = "Live at #ConcertSL! #colombo,#2024 #ශ්‍රීලංකා #live_music #concertsl\n#மேடை"
    assert extract_hashtags(caption) == ["concertsl", "colombo", "ශ්‍රීලංකා", "live_music", "மேடை"]
    assert extract_hashtags(None) == [] and extract_hashtags("no tags, just a # sign") == []


def build() -> HashtagGraph:
    g = HashtagGraph(["#ConcertSLK"], max_nodes=4, max_searches=3)
    g.record_search("concertslk", [
        post("A", "#concertslk #srilanka #colombo"),
        post("B", "#concertslk #srilanka #music"),
        post("C", "no hashtags"),
    ])
    return g


def test_a_search_counts_the_other_hashtags_of_its_posts() -> None:
    g = build()
    assert g.id == "concertslk" and g.nodes["concertslk"].explored and g.nodes["concertslk"].posts_read == 3
    assert g.found["concertslk"] == {"srilanka": 2, "colombo": 1, "music": 1}
    assert g.counts() == {"srilanka": 2, "colombo": 1, "music": 1}  # the seed's own search does not count it
    assert g.candidates() == [{"tag": "srilanka", "count": 2}, {"tag": "colombo", "count": 1}, {"tag": "music", "count": 1}]


def test_add_admits_seen_hashtags_until_the_graph_is_full() -> None:
    g = build()
    added, skipped = g.add(["srilanka", "#Colombo", "madeup", "srilanka", "music"])
    assert [a["tag"] for a in added] == ["srilanka", "colombo", "music"]
    assert added[0] == {"tag": "srilanka", "count": 2, "found_via": "concertslk"}
    assert skipped == [{"tag": "madeup", "reason": "not in any searched post"}, {"tag": "srilanka", "reason": "already in the graph"}]
    assert g.full and g.add(["music"])[1][0]["reason"] == "already in the graph"
    assert g.edges() == [
        {"source": "concertslk", "target": "srilanka", "count": 2},
        {"source": "concertslk", "target": "colombo", "count": 1},
        {"source": "concertslk", "target": "music", "count": 1},
    ]


def test_a_second_search_links_back_and_counts_posts_once() -> None:
    g = build()
    g.add(["srilanka", "colombo"])
    g.record_search("srilanka", [post("B", "#concertslk #srilanka #music"), post("D", "#srilanka #colombo #travel")])
    counts = g.counts()
    # B was found under both searches: it counts once for #music and now also for the seed
    assert counts["music"] == 1 and counts["concertslk"] == 1 and counts["colombo"] == 2 and counts["srilanka"] == 2
    assert {"source": "srilanka", "target": "concertslk", "count": 1} in g.edges()
    assert g.unexplored() == [{"tag": "colombo", "count": 2}]
    assert g.searches == 2 and g.searches_left == 1
    with pytest.raises(ValueError, match="did not appear"):
        g.admit_for_search("nowhere")
    g.admit_for_search("travel")  # a candidate becomes a node when it is explored
    assert g.nodes["travel"].found_via == "srilanka" and g.nodes["travel"].depth == 2
    with pytest.raises(ValueError, match="full"):
        g.admit_for_search("music")


def test_snapshot() -> None:
    g = build()
    g.add(["srilanka"])
    snap = g.snapshot()
    assert {k: snap[k] for k in ("id", "seeds", "max_nodes", "max_searches", "status", "searches", "posts")} == {
        "id": "concertslk", "seeds": ["concertslk"], "max_nodes": 4, "max_searches": 3, "status": "running",
        "searches": 1, "posts": 3,
    }
    assert snap["nodes"][1] == {
        "tag": "srilanka", "count": 2, "seed": False, "explored": False, "posts_read": 0, "depth": 1,
        "found_via": "concertslk", "degree": 1, "order": 1,
    }


async def test_store_round_trip_list_and_delete(tmp_path: Path) -> None:
    store = GraphStore(tmp_path / "graph")
    g = build()
    g.add(["srilanka", "colombo", "music"])
    snap = g.snapshot()
    await store.save(snap)
    loaded = await store.load("concertslk")
    assert loaded["nodes"] == snap["nodes"]
    assert sorted(loaded["edges"], key=str) == sorted(snap["edges"], key=str)
    assert loaded["seeds"] == ["concertslk"] and loaded["max_searches"] == 3 and loaded["created_at"]

    g.status, g.summary = "done", "**Clusters**: #srilanka"
    g.record_search("srilanka", [post("D", "#srilanka #colombo")])
    await store.save(g.snapshot())  # saving again replaces the map
    again = await store.load("concertslk")
    assert again["status"] == "done" and again["summary"] == "**Clusters**: #srilanka" and again["searches"] == 2
    assert len(again["edges"]) == len(g.edges())

    await store.save(HashtagGraph(["music"]).snapshot())
    maps = await store.list_maps()
    assert [m["id"] for m in maps] == ["music", "concertslk"] and maps[1]["nodes"] == 4

    assert await store.delete("concertslk") and not await store.delete("concertslk")
    assert await store.load("concertslk") is None
    # one Hashtag node per hashtag: #music stays for the other map, the rest are gone
    left = await store._run(lambda conn: sorted(r[0] for r in _rows(conn.execute("MATCH (h:Hashtag) RETURN h.tag"))))
    assert left == ["music"]
