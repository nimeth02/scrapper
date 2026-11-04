"""Profiles and their posts, read off the profile page the way the web app loads it.

The JSON profile endpoints (``/api/v1/users/web_profile_info/``, ``/api/v1/users/<id>/info/``) answer
HTTP 429 for this account while the profile page itself still loads. That page fills itself through
GraphQL: ``PolarisProfilePageContentQuery`` carries the account (name, bio, links, follower / following /
post counts, flags) and ``PolarisProfilePostsQuery`` the newest 12 posts in the usual feed-item shape,
with one more page per scroll. :func:`load_profile` opens the page like a user and parses those
responses, so the request pattern is exactly the web app's own (same approach as :mod:`hashtags`).

:func:`get_profile` and :func:`get_user_posts` are what the tools call: the page first, and the JSON
endpoint only when the page loaded without showing the profile data.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Response

from app.instagram import api
from app.instagram.api import BASE_URL, InstagramAPIError, LoginRequired, RateLimited, parse_feed_item
from app.instagram.browser import BrowserSession
from app.instagram.codec import username_from_url
from app.instagram.hashtags import _RATE_LIMIT_TEXT, _settle

log = logging.getLogger(__name__)

# ``x-fb-friendly-name`` of the profile page's own GraphQL calls.
PROFILE_QUERY = "PolarisProfilePageContentQuery"
POSTS_QUERY_PREFIX = "PolarisProfilePosts"  # first page, then a pagination query per scroll
TIMELINE_KEY = "xdt_api__v1__feed__user_timeline_graphql_connection"
POSTS_PER_PAGE = 12
MAX_SCROLLS = 10
_NOT_FOUND_TEXT = ("sorry, this page isn't available", "the link you followed may be broken", "profile isn't available")
# account_type on the profile query: 1 personal, 2 business, 3 creator.
_PROFESSIONAL_TYPES = {2, 3}


class ProfileNotFound(InstagramAPIError):
    """The profile page says the account does not exist (or is not visible to this account)."""


def profile_url(username: str) -> str:
    return f"{BASE_URL}/{username}/"


def _str_id(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def parse_page_profile(user: dict) -> dict:
    """The account from ``PolarisProfilePageContentQuery``, in the shape of :func:`api.parse_profile`."""
    username = user.get("username")
    account_type = user.get("account_type")
    professional = user.get("is_professional_account")
    if professional is None and account_type is not None:
        professional = account_type in _PROFESSIONAL_TYPES
    return {
        "id": _str_id(user.get("pk") or user.get("id")),
        "username": username,
        "full_name": user.get("full_name") or None,
        "biography": user.get("biography") or None,
        "external_url": user.get("external_url") or None,
        "bio_links": [
            {"title": link.get("title") or None, "url": link.get("url")}
            for link in user.get("bio_links") or []
            if link.get("url")
        ],
        "followers": user.get("follower_count"),
        "following": user.get("following_count"),
        "posts_count": user.get("media_count"),
        "is_private": user.get("is_private"),
        "is_verified": user.get("is_verified"),
        "is_business_account": user.get("is_business"),
        "is_professional_account": professional,
        "category": user.get("category") or user.get("category_name") or None,
        "city": user.get("city_name") or None,
        "profile_pic_url": (user.get("hd_profile_pic_url_info") or {}).get("url") or user.get("profile_pic_url"),
        "profile_url": profile_url(username) if username else None,
        "recent_posts": [],
    }


def parse_timeline(data: Any) -> tuple[list[dict], bool]:
    """Posts and ``has_next_page`` from a profile posts query response."""
    connection = ((data or {}).get("data") or {}).get(TIMELINE_KEY) if isinstance(data, dict) else None
    if not isinstance(connection, dict):
        return [], False
    posts = []
    for edge in connection.get("edges") or []:
        node = (edge or {}).get("node")
        if isinstance(node, dict) and (node.get("code") or node.get("pk")):
            post = parse_feed_item(node)
            # Collab posts sit on every co-author's grid while ``owner`` is the account that published them.
            post["coauthors"] = [c["username"] for c in node.get("coauthor_producers") or [] if c.get("username")]
            posts.append(post)
    return posts, bool((connection.get("page_info") or {}).get("has_next_page"))


async def _explain_missing_profile(session: BrowserSession, username: str) -> None:
    """Raise the reason the profile page showed no profile data, if the page says so."""
    page = await session.page()
    if "/accounts/login" in page.url:
        raise LoginRequired(f"Instagram sent the @{username} page to the login form; the session has expired.")
    text = (await page.evaluate("() => document.body ? document.body.innerText : ''")).lower()
    if any(marker in text for marker in _RATE_LIMIT_TEXT):
        raise RateLimited(f"Instagram is throttling this account/IP (while loading @{username}); wait a while before retrying.")
    if any(marker in text for marker in _NOT_FOUND_TEXT):
        raise ProfileNotFound(f"@{username} does not exist or is not available to this account.")


async def load_profile(
    session: BrowserSession,
    username: str,
    *,
    max_posts: int = POSTS_PER_PAGE,
    max_scrolls: int = 0,
    settle_seconds: float = 8.0,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """Open ``instagram.com/<username>/`` and return the profile with up to ``max_posts`` newest posts.

    The first 12 posts come with the page; every scroll (at most ``max_scrolls``) adds about 12 more.
    ``recent_posts`` is empty for private accounts. ``has_more_posts`` says whether more would load.
    Raises :class:`ProfileNotFound` for a missing account and :class:`InstagramAPIError` when the page
    loaded without its profile data (Instagram changed the page)."""
    name = username_from_url(username)
    wanted = name.lower()
    state: dict[str, Any] = {"user": None, "posts": {}, "pages": 0, "has_next": False}
    pending: set[asyncio.Task] = set()

    async def ingest(response: Response, query: str) -> None:
        try:
            data = json.loads(await response.text())
        except (PlaywrightError, ValueError):
            return
        if query == PROFILE_QUERY:
            user = ((data.get("data") or {}).get("user") if isinstance(data, dict) else None) or {}
            if str(user.get("username") or "").lower() == wanted:
                state["user"] = user
            return
        posts, has_next = parse_timeline(data)
        if not posts and not has_next:
            return
        for post in posts:
            members = {(post.get("owner") or wanted).lower(), *(c.lower() for c in post.get("coauthors") or [])}
            if wanted not in members:
                continue  # neither published nor co-authored by this account: not its grid
            key = post.get("shortcode") or post.get("id")
            if key:
                state["posts"].setdefault(key, post)
        state["has_next"] = has_next
        state["pages"] += 1
        if on_progress is not None:
            on_progress({"username": name, "pages": state["pages"], "posts": len(state["posts"])})

    def on_response(response: Response) -> None:
        query = response.request.headers.get("x-fb-friendly-name", "")
        if query == PROFILE_QUERY or query.startswith(POSTS_QUERY_PREFIX):
            task = asyncio.ensure_future(ingest(response, query))
            pending.add(task)
            task.add_done_callback(pending.discard)

    page = await session.page()
    page.on("response", on_response)
    scrolls = 0
    try:
        await session.goto(profile_url(name))
        await _settle(lambda: state["user"] is not None, settle_seconds, pending)
        if state["user"] is None:
            await _explain_missing_profile(session, name)
            raise InstagramAPIError(f"The @{name} page loaded without its profile data (Instagram may have changed the page).")
        if max_posts > 0 and not state["user"].get("is_private"):
            await _settle(lambda: state["pages"] > 0, settle_seconds, pending)
            idle = 0
            while len(state["posts"]) < max_posts and state["has_next"] and scrolls < max_scrolls:
                before_posts, before_pages = len(state["posts"]), state["pages"]
                await session.throttle()
                await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                scrolls += 1
                await _settle(lambda: state["pages"] > before_pages, settle_seconds, pending)
                idle = idle + 1 if len(state["posts"]) == before_posts else 0
                if idle >= 2:
                    break
    finally:
        page.remove_listener("response", on_response)
        if pending:
            await asyncio.gather(*list(pending), return_exceptions=True)

    profile = parse_page_profile(state["user"])
    posts = list(state["posts"].values())
    profile["recent_posts"] = posts[:max_posts]
    profile["has_more_posts"] = state["has_next"] or len(posts) > max_posts
    return profile


async def get_profile(session: BrowserSession, username: str, *, max_posts: int = POSTS_PER_PAGE) -> dict:
    """Profile with its newest posts: from the profile page, or the JSON endpoint if the page shows nothing."""
    try:
        return await load_profile(session, username, max_posts=max_posts)
    except (LoginRequired, RateLimited, ProfileNotFound):
        raise
    except InstagramAPIError as exc:
        log.info("profile page gave no data (%s); trying the JSON endpoint", exc)
        return await api.get_profile(session, username)


async def get_user_posts(session: BrowserSession, username: str, *, limit: int = POSTS_PER_PAGE) -> dict:
    """Newest-first posts of an account (same shape as :func:`api.get_user_posts`, without a cursor)."""
    limit = max(1, limit)
    scrolls = min(MAX_SCROLLS, max(0, -(-limit // POSTS_PER_PAGE) - 1))
    profile = await load_profile(session, username, max_posts=limit, max_scrolls=scrolls)
    return {
        "username": profile["username"],
        "full_name": profile["full_name"],
        "is_private": profile["is_private"],
        "posts_count": profile["posts_count"],
        "posts": profile["recent_posts"],
        "more_available": profile["has_more_posts"],
        "next_max_id": None,
    }
