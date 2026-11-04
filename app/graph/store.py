"""Hashtag maps in an embedded graph database: LadybugDB, files on this machine, Cypher, no server.

Schema::

    (:HashtagMap {id, seeds, max_nodes, max_searches, status, summary, searches, posts, created_at, updated_at})
        -[:INCLUDES {count, seed, explored, posts_read, depth, found_via, degree, ord}]->(:Hashtag {tag})
    (:Hashtag)-[:CO_OCCURS {map, count}]->(:Hashtag)

A hashtag is one node however many maps include it. What belongs to one map (the hashtag's count there,
whether it was searched) sits on that map's ``INCLUDES`` relationship, and every ``CO_OCCURS`` edge
carries the id of the map it belongs to. Saving a map replaces its relationships wholesale.

LadybugDB locks its files while a process holds them open, so the store opens the database for each
operation and closes it straight after: the web server, a CLI run and the tests can take turns.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

import real_ladybug as lb

T = TypeVar("T")

BACKEND = f"LadybugDB {getattr(lb, '__version__', '')}".strip()
LOCK_WAIT_SECONDS = 10.0  # how long to wait for another process to release the files

SCHEMA = (
    "CREATE NODE TABLE IF NOT EXISTS HashtagMap(id STRING, seeds STRING[], max_nodes INT64, max_searches INT64,"
    " status STRING, summary STRING, searches INT64, posts INT64, created_at TIMESTAMP, updated_at TIMESTAMP,"
    " PRIMARY KEY(id))",
    "CREATE NODE TABLE IF NOT EXISTS Hashtag(tag STRING, PRIMARY KEY(tag))",
    "CREATE REL TABLE IF NOT EXISTS INCLUDES(FROM HashtagMap TO Hashtag, count INT64, seed BOOLEAN, explored BOOLEAN,"
    " posts_read INT64, depth INT64, found_via STRING, degree INT64, ord INT64)",
    "CREATE REL TABLE IF NOT EXISTS CO_OCCURS(FROM Hashtag TO Hashtag, map STRING, count INT64)",
)

_MAP_FIELDS = ("seeds", "max_nodes", "max_searches", "status", "summary", "searches", "posts", "created_at", "updated_at")
_NODE_FIELDS = ("tag", "count", "seed", "explored", "posts_read", "depth", "found_via", "degree", "order")


class GraphStoreError(RuntimeError):
    """The graph database could not be opened, read or written."""


def _rows(result: Any) -> list[list[Any]]:
    rows = []
    while result.has_next():
        rows.append(result.get_next())
    return rows


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


class GraphStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()  # one operation at a time within this process

    def describe(self) -> dict[str, str]:
        return {"backend": BACKEND, "path": str(self.path)}

    # ---- plumbing ------------------------------------------------------------------------------
    def _open(self) -> lb.Database:
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                return lb.Database(str(self.path))
            except Exception as exc:  # noqa: BLE001 - the driver raises plain RuntimeErrors
                if "lock" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise GraphStoreError(f"cannot open the graph database at {self.path}: {exc}") from exc
                time.sleep(0.2)

    def _with_connection(self, work: Callable[[lb.Connection], T]) -> T:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            db = self._open()
            conn = lb.Connection(db)
            try:
                for statement in SCHEMA:
                    conn.execute(statement)
                return work(conn)
            except GraphStoreError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise GraphStoreError(f"graph database error: {exc}") from exc
            finally:
                conn.close()
                db.close()

    async def _run(self, work: Callable[[lb.Connection], T]) -> T:
        return await asyncio.to_thread(self._with_connection, work)

    # ---- operations ------------------------------------------------------------------------------
    async def save(self, snapshot: dict[str, Any]) -> None:
        """Store a :meth:`app.graph.model.HashtagGraph.snapshot`, replacing the map's earlier version."""
        await self._run(lambda conn: _save(conn, snapshot))

    async def load(self, map_id: str) -> dict[str, Any] | None:
        """The stored map in the snapshot's shape (plus ``created_at`` / ``updated_at``), or ``None``."""
        return await self._run(lambda conn: _load(conn, map_id))

    async def list_maps(self) -> list[dict[str, Any]]:
        """Every stored map without its nodes and edges, most recently updated first."""
        return await self._run(_list_maps)

    async def delete(self, map_id: str) -> bool:
        return await self._run(lambda conn: _delete(conn, map_id))


# ---- queries (run on a connection, inside the store's lock) ----------------------------------------
def _save(conn: lb.Connection, snap: dict[str, Any]) -> None:
    map_id = snap["id"]
    params = {
        "id": map_id,
        "seeds": list(snap["seeds"]),
        "max_nodes": int(snap["max_nodes"]),
        "max_searches": int(snap.get("max_searches") or 0),
        "status": str(snap.get("status") or ""),
        "summary": str(snap.get("summary") or ""),
        "searches": int(snap.get("searches") or 0),
        "posts": int(snap.get("posts") or 0),
    }
    nodes = [
        {
            "tag": n["tag"],
            "count": int(n["count"]),
            "seed": bool(n["seed"]),
            "explored": bool(n["explored"]),
            "posts_read": int(n["posts_read"]),
            "depth": int(n["depth"]),
            "found_via": n.get("found_via") or "",  # "" rather than null: the driver types lists by their values
            "degree": int(n.get("degree") or 0),
            "ord": int(n["order"]),
        }
        for n in snap["nodes"]
    ]
    edges = [{"source": e["source"], "target": e["target"], "count": int(e["count"])} for e in snap["edges"]]
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute("MERGE (m:HashtagMap {id: $id}) ON CREATE SET m.created_at = current_timestamp()", {"id": map_id})
        conn.execute(
            "MATCH (m:HashtagMap {id: $id}) SET m.seeds = $seeds, m.max_nodes = $max_nodes, m.max_searches = $max_searches,"
            " m.status = $status, m.summary = $summary, m.searches = $searches, m.posts = $posts,"
            " m.updated_at = current_timestamp()",
            params,
        )
        conn.execute("MATCH (:HashtagMap {id: $id})-[r:INCLUDES]->(:Hashtag) DELETE r", {"id": map_id})
        conn.execute("MATCH (:Hashtag)-[e:CO_OCCURS]->(:Hashtag) WHERE e.map = $id DELETE e", {"id": map_id})
        if nodes:
            conn.execute("UNWIND $rows AS r MERGE (:Hashtag {tag: r.tag})", {"rows": [{"tag": n["tag"]} for n in nodes]})
            conn.execute(
                "UNWIND $rows AS r MATCH (m:HashtagMap {id: $id}), (h:Hashtag {tag: r.tag})"
                " CREATE (m)-[:INCLUDES {count: r.count, seed: r.seed, explored: r.explored, posts_read: r.posts_read,"
                " depth: r.depth, found_via: r.found_via, degree: r.degree, ord: r.ord}]->(h)",
                {"rows": nodes, "id": map_id},
            )
        if edges:
            conn.execute(
                "UNWIND $rows AS r MATCH (a:Hashtag {tag: r.source}), (b:Hashtag {tag: r.target})"
                " CREATE (a)-[:CO_OCCURS {map: $id, count: r.count}]->(b)",
                {"rows": edges, "id": map_id},
            )
        _drop_orphans(conn)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _load(conn: lb.Connection, map_id: str) -> dict[str, Any] | None:
    found = _rows(
        conn.execute(
            "MATCH (m:HashtagMap {id: $id}) RETURN m.seeds, m.max_nodes, m.max_searches, m.status, m.summary,"
            " m.searches, m.posts, m.created_at, m.updated_at",
            {"id": map_id},
        )
    )
    if not found:
        return None
    meta = dict(zip(_MAP_FIELDS, found[0], strict=True))
    nodes = _rows(
        conn.execute(
            "MATCH (:HashtagMap {id: $id})-[r:INCLUDES]->(h:Hashtag) RETURN h.tag, r.count, r.seed, r.explored,"
            " r.posts_read, r.depth, r.found_via, r.degree, r.ord ORDER BY r.ord",
            {"id": map_id},
        )
    )
    edges = _rows(
        conn.execute(
            "MATCH (a:Hashtag)-[e:CO_OCCURS]->(b:Hashtag) WHERE e.map = $id RETURN a.tag, b.tag, e.count"
            " ORDER BY e.count DESC, a.tag, b.tag",
            {"id": map_id},
        )
    )
    out: dict[str, Any] = {"id": map_id, **{k: _iso(v) for k, v in meta.items()}}
    out["max_searches"] = out["max_searches"] or None
    out["nodes"] = []
    for row in nodes:
        node = dict(zip(_NODE_FIELDS, row, strict=True))
        node["found_via"] = node["found_via"] or None
        out["nodes"].append(node)
    out["edges"] = [{"source": a, "target": b, "count": n} for a, b, n in edges]
    return out


def _list_maps(conn: lb.Connection) -> list[dict[str, Any]]:
    rows = _rows(
        conn.execute(
            "MATCH (m:HashtagMap) OPTIONAL MATCH (m)-[r:INCLUDES]->(:Hashtag)"
            " RETURN m.id, m.seeds, m.status, m.max_nodes, m.searches, m.posts, m.updated_at, count(r) AS nodes"
            " ORDER BY m.updated_at DESC"
        )
    )
    keys = ("id", "seeds", "status", "max_nodes", "searches", "posts", "updated_at", "nodes")
    return [{k: _iso(v) for k, v in zip(keys, row, strict=True)} for row in rows]


def _delete(conn: lb.Connection, map_id: str) -> bool:
    if not _rows(conn.execute("MATCH (m:HashtagMap {id: $id}) RETURN m.id", {"id": map_id})):
        return False
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute("MATCH (:Hashtag)-[e:CO_OCCURS]->(:Hashtag) WHERE e.map = $id DELETE e", {"id": map_id})
        conn.execute("MATCH (m:HashtagMap {id: $id}) DETACH DELETE m", {"id": map_id})
        _drop_orphans(conn)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return True


def _drop_orphans(conn: lb.Connection) -> None:
    """Hashtags no map includes any more (a map was deleted, or saved again with other nodes)."""
    conn.execute("MATCH (h:Hashtag) WHERE NOT EXISTS { MATCH (:HashtagMap)-[:INCLUDES]->(h) } DETACH DELETE h")
