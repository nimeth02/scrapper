"""Local web UI for the hashtag crawler: type hashtags, watch the crawl, browse posts and accounts, ask the
agent, and map related hashtags into the graph database (the "Hashtag map" tab).

Run::

    python -m app.web            # http://127.0.0.1:8000

Needs the session file written by the login server (``scripts/check_instagram.py login`` or the
instagram-login MCP server). The crawl runs in the same headless Playwright browser the MCP scraper
uses, one crawl at a time, and streams its progress to the page as server-sent events.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from app.agent import mapper
from app.agent import runner as agent
from app.config import get_settings
from app.graph import GraphStore, GraphStoreError
from app.instagram import SharedSession, engagement, get_instagram_settings, hashtags, profiles
from app.instagram.api import InstagramAPIError, LoginRequired, RateLimited
from app.instagram.codec import shortcode_from_url, username_from_url

log = logging.getLogger(__name__)

settings = get_instagram_settings()
shared = SharedSession(settings, headless=settings.headless)
graph_store = GraphStore(get_settings().graph_db_path)
STATIC = Path(__file__).parent / "static"
IMAGE_HOSTS = (".cdninstagram.com", ".fbcdn.net")
MAX_ENGAGEMENT_POSTS = 100
# Hashtag map knobs: (default, min, max).
MAP_LIMITS = {
    "max_nodes": (mapper.MAX_NODES, 5, 100),
    "max_searches": (mapper.MAX_SEARCHES, 1, 30),
    "posts_per_search": (mapper.POSTS_PER_SEARCH, 12, 96),
}
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
NO_SESSION = (
    f"No saved Instagram session at {settings.session_file}. "
    "Run `python scripts/check_instagram.py login` (or the instagram-login MCP server) first."
)

Queue = asyncio.Queue  # of str | None


def session_cookies(path: Path) -> dict[str, str]:
    """Instagram cookies from a Playwright storage-state file, without starting a browser."""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {c["name"]: c["value"] for c in state.get("cookies", []) if "instagram.com" in c.get("domain", "")}


def _clamp(raw: str | None, default: int, lo: int, hi: int) -> int:
    try:
        value = int(raw) if raw not in (None, "") else default
    except ValueError:
        value = default
    return max(lo, min(value, hi))


def _event(name: str, payload: object) -> str:
    return "event: " + name + "\ndata: " + json.dumps(payload, default=str) + "\n\n"


def parse_posts(raw: str) -> list[str]:
    """Shortcodes from a comma/space separated list of shortcodes or post URLs, deduplicated and capped."""
    codes: list[str] = []
    for part in raw.replace(",", " ").split():
        try:
            code = shortcode_from_url(part)
        except ValueError:
            continue
        if code not in codes:
            codes.append(code)
    return codes[:MAX_ENGAGEMENT_POSTS]


def _sse(run: Callable[[Queue], Awaitable[None]]) -> StreamingResponse:
    """Run ``run`` in the background and stream whatever it puts on the queue until it puts ``None``.

    Any exception becomes a ``failed`` event, so the page always hears how a crawl ended.
    """
    queue: Queue = asyncio.Queue()

    async def guarded() -> None:
        try:
            await run(queue)
        except Exception as exc:  # noqa: BLE001 - whatever went wrong, the page needs to hear about it
            log.exception("crawl failed")
            queue.put_nowait(_event("failed", {"kind": type(exc).__name__, "message": str(exc)}))
        finally:
            queue.put_nowait(None)

    async def stream():
        task = asyncio.create_task(guarded())
        try:
            while (item := await queue.get()) is not None:
                yield item
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def index(request: Request) -> Response:
    # Never cache the page: it pins the script/style versions, and a stale copy renders an empty UI.
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def status(request: Request) -> Response:
    cookies = session_cookies(settings.session_file) if settings.session_file.exists() else {}
    logged_in = bool(cookies.get("sessionid") and cookies.get("ds_user_id"))
    return JSONResponse(
        {
            "session_file": str(settings.session_file),
            "session_file_exists": settings.session_file.exists(),
            "logged_in": logged_in,
            "user_id": cookies.get("ds_user_id"),
            "browser_open": shared.is_started,
            "message": None if logged_in else NO_SESSION,
            "agent": agent.describe(),
            "graph": graph_store.describe(),
        }
    )


async def scrape(request: Request) -> Response:
    """Server-sent events: ``tag_start`` / ``progress`` / ``tag_done`` per hashtag, then ``done`` or ``failed``."""
    try:
        names = hashtags.parse_tags(request.query_params.get("tags", ""))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not settings.session_file.exists():
        return JSONResponse({"error": NO_SESSION}, status_code=409)
    max_posts = _clamp(request.query_params.get("max_posts"), 60, 1, 500)
    max_scrolls = _clamp(request.query_params.get("max_scrolls"), 5, 0, 30)

    async def run(queue: Queue) -> None:
        results: list[dict] = []
        async with shared.acquire() as session:
            for index, name in enumerate(names):
                queue.put_nowait(_event("tag_start", {"tag": name, "index": index, "total": len(names)}))
                result = await hashtags.crawl_hashtag(
                    session,
                    name,
                    max_posts=max_posts,
                    max_scrolls=max_scrolls,
                    on_progress=lambda progress: queue.put_nowait(_event("progress", progress)),
                )
                results.append(result)
                queue.put_nowait(_event("tag_done", hashtags.summarize_tag(result)))
        queue.put_nowait(_event("done", hashtags.merge_results(results)))

    return _sse(run)


async def engagement_stream(request: Request) -> Response:
    """Server-sent events: ``post_start`` / ``post_done`` per post, then ``done`` (people merged) or ``failed``.

    ``posts`` is a comma/space separated list of shortcodes or post URLs (at most 100).
    """
    codes = parse_posts(request.query_params.get("posts", ""))
    if not codes:
        return JSONResponse({"error": "no posts given (shortcodes or post URLs, comma separated)"}, status_code=400)
    if not settings.session_file.exists():
        return JSONResponse({"error": NO_SESSION}, status_code=409)
    max_comments = _clamp(request.query_params.get("max_comments"), 30, 0, 500)
    include_likers = request.query_params.get("likers", "1").lower() not in ("0", "false", "no")

    async def run(queue: Queue) -> None:
        async with shared.acquire() as session:
            result = await engagement.collect_engagement(
                session,
                codes,
                max_comments=max_comments,
                include_likers=include_likers,
                on_progress=lambda progress: queue.put_nowait(_event("post_start", progress)),
                on_post=lambda post: queue.put_nowait(_event("post_done", post)),
            )
        result.pop("posts", None)  # already streamed one by one
        queue.put_nowait(_event("done", result))

    return _sse(run)


async def profile(request: Request) -> Response:
    """One account's profile (name, bio, links, counts, flags, newest posts), read off its profile page."""
    try:
        name = username_from_url(request.query_params.get("username", ""))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not settings.session_file.exists():
        return JSONResponse({"error": NO_SESSION}, status_code=409)
    try:
        async with shared.acquire() as session:
            result = await profiles.get_profile(session, name)
    except profiles.ProfileNotFound as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except LoginRequired as exc:
        return JSONResponse({"error": f"{exc} Log in again, then retry."}, status_code=401)
    except RateLimited as exc:
        return JSONResponse({"error": str(exc)}, status_code=429)
    except InstagramAPIError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse(result)


MAX_HISTORY_TURNS = 20


async def ask_agent(request: Request) -> Response:
    """POST ``{"question": str, "history": [{"role", "content"}, ...]}``.

    Server-sent events named after the agent's event types: ``run_start`` / ``run_end`` for every run of
    the trace (chain, llm and tool runs) while it works, then ``final`` with the answer, or ``failed``.
    Tool results ride along in the tool runs' ``run_end`` so the page can show the posts and accounts the
    agent found.
    """
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "the body must be JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "the body must be a JSON object"}, status_code=400)
    question = str(body.get("question") or "").strip()
    if not question:
        return JSONResponse({"error": "question is required"}, status_code=400)
    history = body.get("history") or []
    if not isinstance(history, list) or not all(isinstance(turn, dict) for turn in history):
        return JSONResponse({"error": "history must be a list of {role, content} objects"}, status_code=400)
    history = history[-MAX_HISTORY_TURNS:]
    if not settings.session_file.exists():
        return JSONResponse({"error": NO_SESSION}, status_code=409)
    info = agent.describe()
    if not info["configured"]:
        return JSONResponse({"error": info["message"]}, status_code=503)

    async def run(queue: Queue) -> None:
        await agent.run_agent(
            question,
            acquire=shared.acquire,
            history=history,
            on_event=lambda event: queue.put_nowait(_event(event["type"], event)),
        )

    return _sse(run)


async def _json_object(request: Request) -> dict | JSONResponse:
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse({"error": "the body must be JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "the body must be a JSON object"}, status_code=400)
    return body


async def build_map(request: Request) -> Response:
    """POST ``{"seeds": "concertslk", "max_nodes": 50, "max_searches": 10, "posts_per_search": 24}``.

    The mapping agent explores hashtag by hashtag and stores the map in the graph database. Server-sent
    events: ``run_start`` / ``run_end`` (its trace), ``graph`` (the map's snapshot after every change),
    then ``final`` with its summary, or ``failed``.
    """
    body = await _json_object(request)
    if isinstance(body, JSONResponse):
        return body
    try:
        seeds = hashtags.parse_tags(str(body.get("seeds") or ""))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if len(seeds) > mapper.MAX_SEEDS:
        return JSONResponse({"error": f"at most {mapper.MAX_SEEDS} seed hashtags"}, status_code=400)
    limits = {name: _clamp(body.get(name), *bounds) for name, bounds in MAP_LIMITS.items()}
    if not settings.session_file.exists():
        return JSONResponse({"error": NO_SESSION}, status_code=409)
    info = agent.describe()
    if not info["configured"]:
        return JSONResponse({"error": info["message"]}, status_code=503)

    async def run(queue: Queue) -> None:
        await mapper.run_mapper(
            seeds,
            acquire=shared.acquire,
            store=graph_store,
            on_event=lambda event: queue.put_nowait(_event(event["type"], event)),
            **limits,
        )

    return _sse(run)


async def list_maps(request: Request) -> Response:
    """The maps stored in the graph database, newest first, without their nodes."""
    try:
        maps = await graph_store.list_maps()
    except GraphStoreError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    return JSONResponse({"maps": maps, **graph_store.describe()})


async def map_detail(request: Request) -> Response:
    """GET one stored map (nodes and edges), or DELETE it."""
    map_id = request.path_params["map_id"]
    try:
        if request.method == "DELETE":
            if not await graph_store.delete(map_id):
                return JSONResponse({"error": f"no map {map_id!r}"}, status_code=404)
            return JSONResponse({"deleted": map_id})
        snapshot = await graph_store.load(map_id)
    except GraphStoreError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    if snapshot is None:
        return JSONResponse({"error": f"no map {map_id!r}"}, status_code=404)
    return JSONResponse(snapshot)


async def image(request: Request) -> Response:
    """Proxy for Instagram CDN images, for when the browser refuses to hot-link them."""
    url = request.query_params.get("url", "")
    host = urlparse(url).hostname or ""
    if not url.startswith("https://") or not host.endswith(IMAGE_HOSTS):
        return Response("only Instagram CDN images are proxied", status_code=403)
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        upstream = await client.get(url, headers={"User-Agent": BROWSER_UA, "Referer": "https://www.instagram.com/"})
    return Response(
        upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "private, max-age=3600"},
    )


@asynccontextmanager
async def lifespan(_: Starlette) -> AsyncIterator[None]:
    yield
    await shared.close()  # the browser is opened lazily by the first crawl


app = Starlette(
    routes=[
        Route("/", index),
        Route("/api/status", status),
        Route("/api/scrape", scrape),
        Route("/api/engagement", engagement_stream),
        Route("/api/agent", ask_agent, methods=["POST"]),
        Route("/api/profile", profile),
        Route("/api/map", build_map, methods=["POST"]),
        Route("/api/maps", list_maps),
        Route("/api/maps/{map_id}", map_detail, methods=["GET", "DELETE"]),
        Route("/api/image", image),
        Mount("/static", StaticFiles(directory=str(STATIC)), name="static"),
    ],
    lifespan=lifespan,
)
