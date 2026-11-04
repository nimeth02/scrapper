"""Hashtag -> posts and the accounts behind them, read off Instagram's own search page.

Instagram retired the hashtag JSON endpoint (``/api/v1/tags/web_info/`` now answers ``"data": null``)
and ``/explore/tags/<tag>/`` redirects to the keyword-search page
``/explore/search/keyword/?q=%23<tag>``. That page fills its grid through GraphQL
(``PolarisKeywordSearchExplorePageRelayQuery`` first, then ``...PaginationQuery`` on every scroll).
Rather than rebuild those requests by hand, :func:`crawl_hashtag` opens the page like a user, scrolls,
and parses the responses the page itself receives. Each one carries 24 posts in the usual feed-item
shape, owner included, so the request pattern is exactly the web app's own.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Response

from app.instagram.api import BASE_URL, LoginRequired, RateLimited, parse_feed_item, parse_user
from app.instagram.browser import BrowserSession

log = logging.getLogger(__name__)

# ``x-fb-friendly-name`` of the search page's own GraphQL calls (first page, then each pagination page).
SEARCH_QUERY_PREFIX = "PolarisKeywordSearchExplorePage"
_RATE_LIMIT_TEXT = ("please wait a few minutes", "try again later")
_ACCOUNT_FIELDS = ("username", "user_id", "full_name", "is_private", "is_verified", "profile_pic_url", "profile_url")


# ---- pure helpers --------------------------------------------------------------
def normalize_tag(tag: str) -> str:
    name = tag.strip().lstrip("#").strip()
    if not name:
        raise ValueError("empty hashtag")
    return name


def parse_tags(tags: str | list[str]) -> list[str]:
    """``"tea, #Coffee ceylon"`` or ``["tea", "#coffee"]`` -> ``["tea", "coffee", "ceylon"]`` (lower-cased, deduplicated)."""
    raw = tags if isinstance(tags, list) else [tags]
    seen: dict[str, None] = {}
    for item in raw:
        for part in str(item).replace(",", " ").split():
            name = part.strip().lstrip("#").strip().lower()
            if name:
                seen.setdefault(name, None)
    if not seen:
        raise ValueError("no hashtags given")
    return list(seen)


def hashtag_search_url(tag: str) -> str:
    return f"{BASE_URL}/explore/search/keyword/?q={quote('#' + normalize_tag(tag), safe='')}"


@dataclass
class SearchPage:
    posts: list[dict] = field(default_factory=list)
    users: dict[str, dict] = field(default_factory=dict)  # username -> account row
    has_next_page: bool = False


def _account_row(user: dict) -> dict:
    return parse_user(user)


def _items(node: dict) -> list[dict]:
    """Grid units carry ``items``; tolerate the older sectioned ``layout_content.medias[].media`` shape too."""
    items = [i for i in node.get("items") or [] if isinstance(i, dict) and (i.get("code") or i.get("pk"))]
    if items:
        return items
    medias = (node.get("layout_content") or {}).get("medias") or []
    return [m["media"] for m in medias if isinstance(m, dict) and isinstance(m.get("media"), dict)]


def parse_search_results(data: Any) -> SearchPage:
    """Posts and accounts in one search-page GraphQL payload (the first page or a pagination page)."""
    page = SearchPage()
    root = (data.get("data") or {}).get("xdt_fbsearch__top_serp_graphql") if isinstance(data, dict) else None
    if not isinstance(root, dict):
        return page
    for edge in root.get("edges") or []:
        node = (edge or {}).get("node") or {}
        for item in _items(node):
            page.posts.append(parse_feed_item(item))
            user = item.get("user") or item.get("owner") or {}
            if user.get("username") and user["username"] not in page.users:
                page.users[user["username"]] = _account_row(user)
    page.has_next_page = bool((root.get("page_info") or {}).get("has_next_page"))
    return page


def summarize_users(posts: list[dict], accounts: dict[str, dict], *, sample_posts: int = 3) -> list[dict]:
    """One row per account, most posts first, with up to ``sample_posts`` post URLs each."""
    rows: dict[str, dict] = {}
    for post in posts:
        username = post.get("owner")
        if not username:
            continue
        row = rows.get(username)
        if row is None:
            account = accounts.get(username) or _account_row({"username": username, "pk": post.get("owner_id")})
            row = rows[username] = {**account, "post_count": 0, "posts": []}
        row["post_count"] += 1
        if post.get("url") and len(row["posts"]) < sample_posts:
            row["posts"].append(post["url"])
    return sorted(rows.values(), key=lambda r: (-r["post_count"], r["username"]))


# ---- browser-driven crawl --------------------------------------------------------
async def _settle(predicate: Callable[[], bool], timeout: float, pending: set[asyncio.Task]) -> bool:
    """Poll ``predicate`` until it holds or ``timeout`` seconds pass, then let in-flight parsers finish."""
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        await asyncio.sleep(0.25)
    if pending:
        await asyncio.gather(*list(pending), return_exceptions=True)
    return predicate()


async def _explain_empty_page(page: Page, tag: str) -> str:
    if "/accounts/login" in page.url:
        raise LoginRequired(f"Instagram sent the #{tag} page to the login form; the session has expired.")
    text = (await page.evaluate("() => document.body ? document.body.innerText : ''")).lower()
    if any(marker in text for marker in _RATE_LIMIT_TEXT):
        raise RateLimited(f"Instagram is throttling this account/IP (while loading #{tag}); wait a while before retrying.")
    return "The search page loaded but returned no posts (unknown tag, or Instagram changed the page)."


async def crawl_hashtag(
    session: BrowserSession,
    tag: str,
    *,
    max_posts: int = 100,
    max_scrolls: int = 10,
    settle_seconds: float = 8.0,
    include_posts: bool = True,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """Open the hashtag's search page, scroll until ``max_posts`` posts or ``max_scrolls`` scrolls, and
    return the posts plus one row per account that posted them (most posts first).

    ``on_progress`` is called after every page of results with the running counts."""
    name = normalize_tag(tag)
    url = hashtag_search_url(name)
    page = await session.page()
    posts: dict[str, dict] = {}
    accounts: dict[str, dict] = {}
    state = {"pages": 0, "has_next": False, "scrolls": 0}
    pending: set[asyncio.Task] = set()

    async def ingest(response: Response) -> None:
        try:
            data = json.loads(await response.text())
        except (PlaywrightError, ValueError):
            return
        result = parse_search_results(data)
        for post in result.posts:
            key = post.get("shortcode") or post.get("id")
            if key:
                posts.setdefault(key, post)
        for username, row in result.users.items():
            accounts.setdefault(username, row)
        state["has_next"] = result.has_next_page
        state["pages"] += 1
        log.debug("#%s: page %d, %d posts so far", name, state["pages"], len(posts))
        if on_progress is not None:
            on_progress(
                {"tag": name, "pages": state["pages"], "scrolls": state["scrolls"], "posts": len(posts), "users": len(accounts)}
            )

    def on_response(response: Response) -> None:
        if response.request.headers.get("x-fb-friendly-name", "").startswith(SEARCH_QUERY_PREFIX):
            task = asyncio.ensure_future(ingest(response))
            pending.add(task)
            task.add_done_callback(pending.discard)

    page.on("response", on_response)
    scrolls = 0
    message: str | None = None
    try:
        await session.goto(url)
        if not await _settle(lambda: state["pages"] > 0, settle_seconds, pending):
            message = await _explain_empty_page(page, name)
        idle = 0
        while len(posts) < max_posts and state["has_next"] and scrolls < max_scrolls:
            before_posts, before_pages = len(posts), state["pages"]
            await session.throttle()
            await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            scrolls += 1
            state["scrolls"] = scrolls
            await _settle(lambda: state["pages"] > before_pages, settle_seconds, pending)
            idle = idle + 1 if len(posts) == before_posts else 0
            if idle >= 2:  # two scrolls brought nothing new: the page has stopped loading
                break
    finally:
        page.remove_listener("response", on_response)
        if pending:
            await asyncio.gather(*list(pending), return_exceptions=True)

    ordered = list(posts.values())[:max_posts]
    users = summarize_users(ordered, accounts)
    result: dict[str, Any] = {
        "tag": name,
        "url": url,
        "post_count": len(ordered),
        "user_count": len(users),
        "has_more": state["has_next"] or len(posts) > len(ordered),
        "scrolls": scrolls,
        "users": users,
    }
    if include_posts:
        result["posts"] = ordered
    if message:
        result["message"] = message
    return result


def summarize_tag(result: dict) -> dict:
    """The per-hashtag part of a :func:`crawl_hashtag` result, without its posts and users."""
    summary = {k: result[k] for k in ("tag", "url", "post_count", "user_count", "has_more", "scrolls")}
    if "message" in result:
        summary["message"] = result["message"]
    return summary


def merge_results(results: list[dict]) -> dict:
    """Merge several :func:`crawl_hashtag` results.

    Accounts become one row per username with the hashtags it was seen under (most hashtags, then most
    posts, first). When the results carry posts, those are merged too, one row per post with its hashtags.
    """
    merged: dict[str, dict] = {}
    posts: dict[str, dict] = {}
    for result in results:
        name = result["tag"]
        for user in result["users"]:
            row = merged.get(user["username"])
            if row is None:
                row = merged[user["username"]] = {k: user.get(k) for k in _ACCOUNT_FIELDS}
                row.update(post_count=0, hashtags=[], posts=[])
            row["post_count"] += user["post_count"]
            row["hashtags"].append(name)
            row["posts"] = (row["posts"] + user["posts"])[:3]
        for post in result.get("posts") or []:
            key = post.get("shortcode") or post.get("id") or post.get("url")
            if key is None:
                continue
            entry = posts.get(key)
            if entry is None:
                entry = posts[key] = {**post, "hashtags": []}
            if name not in entry["hashtags"]:
                entry["hashtags"].append(name)
    users = sorted(merged.values(), key=lambda r: (-len(r["hashtags"]), -r["post_count"], r["username"]))
    out: dict[str, Any] = {
        "tags": [summarize_tag(r) for r in results],
        "user_count": len(users),
        "usernames": [u["username"] for u in users],
        "users": users,
    }
    if any("posts" in r for r in results):
        out["post_count"] = len(posts)
        out["posts"] = list(posts.values())
    return out


async def crawl_hashtags(
    session: BrowserSession,
    tags: str | list[str],
    *,
    max_posts_per_tag: int = 100,
    max_scrolls: int = 10,
    include_posts: bool = False,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """Crawl several hashtags one after another and merge them (see :func:`merge_results`)."""
    kwargs: dict[str, Any] = {"max_posts": max_posts_per_tag, "max_scrolls": max_scrolls, "include_posts": include_posts}
    if on_progress is not None:
        kwargs["on_progress"] = on_progress
    results = [await crawl_hashtag(session, name, **kwargs) for name in parse_tags(tags)]
    return merge_results(results)


async def collect_hashtag_users(
    session: BrowserSession,
    tags: str | list[str],
    *,
    max_posts_per_tag: int = 100,
    max_scrolls: int = 10,
) -> dict:
    """Accounts posting under one or more hashtags, merged and deduplicated (no posts)."""
    return await crawl_hashtags(
        session, tags, max_posts_per_tag=max_posts_per_tag, max_scrolls=max_scrolls, include_posts=False
    )
