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

from app.instagram import BrowserSession, SharedSession, api, engagement, get_instagram_settings, hashtags, profiles
from app.instagram.api import InstagramAPIError, LoginRequired
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
        "For 'which accounts post under these hashtags' use get_hashtag_users; for who comments on and "
        "likes those posts use get_post_engagement or get_hashtag_engagement. "
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
    """Profile of an account: name, bio, bio links, follower/following/post counts, business / private /
    verified flags, city, and its 12 newest posts.

    Accepts a username, "@username" or a profile URL. Read off the profile page the way the web app loads
    it, so it works while the JSON profile endpoint is rate-limited.
    """
    return await _scrape(lambda s: profiles.get_profile(s, username))


@mcp.tool()
async def get_posts(username: str, limit: int = 12, max_id: str | None = None) -> dict:
    """Newest-first posts of an account with caption, counts and media URLs.

    Returns up to `limit` posts (1-100). Without `max_id` the posts come from the profile page (about 12 per
    scroll). Passing a `max_id` cursor uses the JSON feed endpoint instead, which may be rate-limited.
    """
    limit = max(1, min(limit, 100))
    if max_id:
        return await _scrape(lambda s: api.get_user_posts(s, username, limit=limit, max_id=max_id))
    return await _scrape(lambda s: profiles.get_user_posts(s, username, limit=limit))


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
async def get_likers(post: str) -> dict:
    """Who liked a post (shortcode or URL): the first ~100 likers Instagram's web endpoint exposes, plus the total."""
    return await _scrape(lambda s: api.get_likers(s, post))


@mcp.tool()
async def get_post_engagement(post: str, max_comments: int = 50, include_likers: bool = True) -> dict:
    """Commenters (with their comments) and likers of one post (shortcode or URL).

    Costs about one request per 20 comments plus one for the likers. `comments_truncated` /
    `likers_truncated` say whether Instagram had more than was returned.
    """
    max_comments = max(0, min(max_comments, 500))
    return await _scrape(
        lambda s: engagement.post_engagement(s, post, max_comments=max_comments, include_likers=include_likers)
    )


@mcp.tool()
async def get_hashtag_engagement(
    tags: list[str] | str,
    max_posts: int = 20,
    max_comments_per_post: int = 30,
    include_likers: bool = True,
    max_scrolls: int = 3,
) -> dict:
    """Everything in one go: crawl the hashtags, then fetch the commenters and likers of the first
    `max_posts` posts found (1-100).

    Returns the hashtag result (`tags`, `users` = the posters, `posts`) plus `engagement`: `people`
    (commenters and likers merged, with the posts each engaged with), per-post details and counts; its
    `message` is set when Instagram started throttling and the crawl stopped early. Budget: about
    1 + scrolls requests per hashtag and 2 per post, so keep `max_posts` modest.
    """
    max_posts = max(1, min(max_posts, 100))
    max_comments_per_post = max(0, min(max_comments_per_post, 500))
    max_scrolls = max(0, min(max_scrolls, 30))

    async def run(s: BrowserSession) -> dict:
        crawl = await hashtags.crawl_hashtags(
            s, tags, max_posts_per_tag=max_posts, max_scrolls=max_scrolls, include_posts=True
        )
        codes = [p["shortcode"] for p in crawl.get("posts") or [] if p.get("shortcode")][:max_posts]
        crawl["engagement"] = await engagement.collect_engagement(
            s, codes, max_comments=max_comments_per_post, include_likers=include_likers
        )
        return crawl

    return await _scrape(run)


@mcp.tool()
async def get_hashtag(tag: str, max_posts: int = 60, max_scrolls: int = 5) -> dict:
    """Posts under a hashtag plus one row per account that posted them (most posts first).

    Loads Instagram's hashtag search page and scrolls it; every scroll adds about 24 posts and counts
    as one request. `max_posts` 1-500, `max_scrolls` 0-30. `has_more` tells whether more would load.
    """
    max_posts, max_scrolls = max(1, min(max_posts, 500)), max(0, min(max_scrolls, 30))
    return await _scrape(lambda s: hashtags.crawl_hashtag(s, tag, max_posts=max_posts, max_scrolls=max_scrolls))


@mcp.tool()
async def get_hashtag_users(tags: list[str] | str, max_posts_per_tag: int = 100, max_scrolls: int = 10) -> dict:
    """Accounts that post under one or more hashtags, merged and deduplicated.

    `tags` is a list or a comma/space separated string ("tea, #coffee"). Returns `usernames` (accounts seen
    under the most hashtags first), `users` with id, name, private/verified flags, post count, hashtags and
    sample post URLs, and a per-hashtag summary in `tags`. Each hashtag costs 1 + scrolls requests.
    """
    max_posts_per_tag, max_scrolls = max(1, min(max_posts_per_tag, 500)), max(0, min(max_scrolls, 30))
    return await _scrape(
        lambda s: hashtags.collect_hashtag_users(s, tags, max_posts_per_tag=max_posts_per_tag, max_scrolls=max_scrolls)
    )


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
        cookies = await session.cookies()
        if not (cookies.get("sessionid") and cookies.get("ds_user_id")):
            info["message"] = RELOGIN
            return info
        info.update(user_id=cookies["ds_user_id"], logged_in=True)
        try:
            identity = await api.whoami(session)
        except LoginRequired as exc:
            info.update(logged_in=False, message=f"{exc}. {RELOGIN}")
        except InstagramAPIError as exc:
            # Throttled or a changed endpoint: the cookies still say we are logged in.
            info["message"] = f"Session cookies present; account lookup skipped: {exc}"
        else:
            if identity:
                info.update(identity)
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
