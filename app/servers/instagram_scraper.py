"""MCP server: scrape Instagram with Playwright using the session saved by the login server.

Run (stdio)::

    python -m app.servers.instagram_scraper

Runs headless by default (``INSTAGRAM_HEADLESS=true``). Every tool needs a saved session; run the
``login`` tool of the instagram-login server first.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from mcp.server.fastmcp import FastMCP, Image

from app.instagram import BrowserSession, SharedSession, api, get_instagram_settings
from app.instagram.api import LoginRequired
from app.servers import _logging

_logging.configure()

settings = get_instagram_settings()
shared = SharedSession(settings, headless=settings.headless)

NO_SESSION = (
    f"No saved Instagram session at {settings.session_file}. "
    "Run the `login` tool of the instagram-login server first."
)
RELOGIN = "The saved Instagram session is missing or expired. Run `login` on the instagram-login server, then retry."

mcp = FastMCP(
    "instagram-scraper",
    instructions=(
        "Reads public Instagram data (profiles, posts, comments, hashtags, search) through a logged-in "
        "Playwright browser. Needs the session file written by the instagram-login server. "
        "Keep request volume modest: Instagram rate-limits and may lock accounts that scrape aggressively."
    ),
)

T = TypeVar("T")


async def _scrape(fn: Callable[[BrowserSession], Awaitable[T]]) -> T:
    if not settings.session_file.exists():
        raise LoginRequired(NO_SESSION)
    async with shared.acquire() as session:
        try:
            return await fn(session)
        except LoginRequired as exc:
            raise LoginRequired(f"{exc}. {RELOGIN}") from exc


@mcp.tool()
async def get_profile(username: str) -> dict:
    """Profile of an account (bio, follower/following/post counts, flags, 12 most recent posts).

    Accepts a username, "@username" or a profile URL.
    """
    return await _scrape(lambda s: api.get_profile(s, username))


@mcp.tool()
async def get_posts(username: str, limit: int = 12, max_id: str | None = None) -> dict:
    """Newest-first posts of an account with caption, counts and media URLs.

    Returns up to `limit` posts (1-100). Pass the returned `next_max_id` back as `max_id` for the next page.
    """
    limit = max(1, min(limit, 100))
    return await _scrape(lambda s: api.get_user_posts(s, username, limit=limit, max_id=max_id))


@mcp.tool()
async def get_post(post: str) -> dict:
    """One post (or reel) by shortcode or URL, e.g. "CxYz1Ab" or https://www.instagram.com/p/CxYz1Ab/."""
    return await _scrape(lambda s: api.get_post(s, post))


@mcp.tool()
async def get_comments(post: str, limit: int = 50, min_id: str | None = None) -> dict:
    """Comments on a post (shortcode or URL), up to `limit` (1-500). Pass `next_min_id` back as `min_id` for more."""
    limit = max(1, min(limit, 500))
    return await _scrape(lambda s: api.get_comments(s, post, limit=limit, min_id=min_id))


@mcp.tool()
async def get_hashtag(tag: str) -> dict:
    """A hashtag's media count plus its current top and recent posts."""
    return await _scrape(lambda s: api.get_hashtag(s, tag))


@mcp.tool()
async def search(query: str) -> dict:
    """Instagram's blended search: matching users, hashtags and places."""
    return await _scrape(lambda s: api.search(s, query))


@mcp.tool()
async def fetch_api(path: str, params: dict[str, str] | None = None) -> Any:
    """Raw GET on an Instagram web-API path, for anything the other tools do not cover.

    Example: path="/api/v1/users/web_profile_info/", params={"username": "natgeo"}. Returns the JSON as-is.
    """
    return await _scrape(lambda s: api.fetch_json(s, path, params))


@mcp.tool()
async def get_page_text(url: str, max_chars: int = 20000) -> dict:
    """Open an Instagram URL (or username) in the browser and return the page's visible text.

    Fallback for content the JSON endpoints do not expose.
    """
    return await _scrape(lambda s: api.page_text(s, url, max_chars=max(500, max_chars)))


@mcp.tool()
async def screenshot(url: str | None = None) -> Image:
    """PNG screenshot of an Instagram URL (or of the current page when url is omitted)."""
    png = await _scrape(lambda s: api.screenshot(s, url))
    return Image(data=png, format="png")


@mcp.tool()
async def session_status() -> dict:
    """Whether a saved session exists and which account it belongs to."""
    info: dict = {
        "session_file": str(settings.session_file),
        "session_file_exists": settings.session_file.exists(),
        "browser_open": shared.is_started,
        "headless": settings.headless,
        "logged_in": False,
    }
    if not info["session_file_exists"]:
        info["message"] = NO_SESSION
        return info
    async with shared.acquire() as session:
        try:
            identity = await api.whoami(session)
        except LoginRequired as exc:
            info["message"] = f"{exc}. {RELOGIN}"
            return info
    if identity:
        info.update(identity, logged_in=True)
    else:
        info["message"] = RELOGIN
    return info


@mcp.tool()
async def close_browser() -> dict:
    """Close the scraper's browser. It is reopened on the next call."""
    await shared.close()
    return {"browser_open": False}


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
