"""The Instagram lookups the agent may call.

Every tool runs against the shared Playwright session and hands back two things: a compact JSON view
for the model (captions trimmed, lists capped, media URLs dropped, so a crawl of a hundred posts stays
a few thousand tokens) and the full result as the tool message's ``artifact``, so a UI can render the
posts and accounts exactly as the manual flow does.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from app.instagram import api, engagement, hashtags, profiles
from app.instagram.browser import BrowserSession

Acquire = Callable[[], AbstractAsyncContextManager[BrowserSession]]

# Hard caps per call. The system prompt asks for less; these stop a runaway model from burning the
# account's request budget in one go.
MAX_POSTS_PER_TAG = 100
MAX_SCROLLS = 5
MAX_ENGAGEMENT_POSTS = 30
MAX_COMMENTS_PER_POST = 100
MAX_USER_POSTS = 36

# What the model gets to see of a result.
MODEL_POSTS = 40
MODEL_ACCOUNTS = 80
MODEL_PEOPLE = 100
CAPTION_CHARS = 140
BIO_CHARS = 300
COMMENT_CHARS = 120
SAMPLE_COMMENTS = 3


# ---- argument schemas ---------------------------------------------------------------------------
class SearchHashtagsArgs(BaseModel):
    tags: list[str] = Field(min_length=1, description="Hashtags to crawl, with or without the leading '#'.")
    max_posts_per_tag: int = Field(24, ge=1, le=MAX_POSTS_PER_TAG, description="Stop after this many posts per hashtag.")
    max_scrolls: int = Field(
        1, ge=0, le=MAX_SCROLLS, description="Extra pages per hashtag; each adds about 24 posts and costs one request."
    )


class GetEngagementArgs(BaseModel):
    posts: list[str] = Field(min_length=1, max_length=MAX_ENGAGEMENT_POSTS, description="Post shortcodes or URLs.")
    max_comments_per_post: int = Field(20, ge=0, le=MAX_COMMENTS_PER_POST, description="0 skips the comments.")
    include_likers: bool = Field(True, description="Also fetch who liked each post (the first ~100 likers).")


class UsernameArgs(BaseModel):
    username: str = Field(min_length=1, description="Username, '@username' or profile URL.")


class UserPostsArgs(UsernameArgs):
    limit: int = Field(12, ge=1, le=MAX_USER_POSTS, description="How many of the newest posts to return.")


class PostArgs(BaseModel):
    post: str = Field(min_length=1, description="Post shortcode or URL (/p/, /reel/).")


class QueryArgs(BaseModel):
    query: str = Field(min_length=1, description="Keyword to search for.")


# ---- compact views for the model ---------------------------------------------------------------------
def _trim(text: Any, limit: int) -> str | None:
    if not text:
        return None
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _present(row: dict) -> dict:
    return {k: v for k, v in row.items() if v is not None}


def _plural(n: Any, word: str) -> str:
    n = n or 0
    return f"{n} {word}{'' if n == 1 else 's'}"


def compact_post(post: dict) -> dict:
    row = {k: post.get(k) for k in ("shortcode", "url", "owner", "type", "taken_at", "likes", "comments", "views", "location")}
    row["caption"] = _trim(post.get("caption"), CAPTION_CHARS)
    for key in ("hashtags", "coauthors"):
        if post.get(key):
            row[key] = post[key]
    return _present(row)


def compact_account(user: dict) -> dict:
    return _present(
        {k: user.get(k) for k in ("username", "full_name", "post_count", "hashtags", "is_verified", "is_private")}
    )


def compact_search(result: dict) -> dict:
    """A :func:`app.instagram.hashtags.crawl_hashtags` result as the model sees it."""
    users = result.get("users") or []
    posts = result.get("posts") or []
    out: dict[str, Any] = {
        "tags": result.get("tags") or [],
        "post_count": result.get("post_count", len(posts)),
        "account_count": result.get("user_count", len(users)),
        "accounts": [compact_account(u) for u in users[:MODEL_ACCOUNTS]],
        "posts": [compact_post(p) for p in posts[:MODEL_POSTS]],
    }
    notes = []
    if len(users) > MODEL_ACCOUNTS:
        notes.append(f"accounts cut to the top {MODEL_ACCOUNTS} of {len(users)} (most hashtags, then most posts, first)")
    if len(posts) > MODEL_POSTS:
        notes.append(f"posts cut to the first {MODEL_POSTS} of {len(posts)}")
    if notes:
        out["note"] = "; ".join(notes)
    return out


def compact_engagement(result: dict) -> dict:
    """A :func:`app.instagram.engagement.collect_engagement` result as the model sees it."""
    posts = result.get("posts") or []
    people = result.get("people") or []
    out: dict[str, Any] = _present(
        {k: result.get(k) for k in ("post_count", "people_count", "commenter_count", "liker_count", "message")}
    )
    out["posts"] = [
        _present(
            {
                "shortcode": p.get("shortcode"),
                "error": p.get("error"),
                "comment_count": p.get("comment_count"),
                "comments_fetched": len(p.get("comments") or []),
                "like_count": p.get("like_count"),
                "likers_fetched": len(p.get("likers") or []),
                "sample_comments": [
                    {"username": c.get("username"), "text": _trim(c.get("text"), COMMENT_CHARS)}
                    for c in (p.get("comments") or [])[:SAMPLE_COMMENTS]
                ]
                or None,
            }
        )
        for p in posts
    ]
    out["people"] = [
        _present(
            {
                k: person.get(k)
                for k in (
                    "username",
                    "full_name",
                    "comment_count",
                    "liked_count",
                    "posts_commented",
                    "posts_liked",
                    "is_verified",
                    "is_private",
                )
            }
        )
        for person in people[:MODEL_PEOPLE]
    ]
    if len(people) > MODEL_PEOPLE:
        out["note"] = f"people cut to the most active {MODEL_PEOPLE} of {len(people)}"
    return out


def compact_profile(profile: dict) -> dict:
    row = {k: profile.get(k) for k in profile if k not in ("profile_pic_url", "recent_posts", "biography", "bio_links")}
    row["biography"] = _trim(profile.get("biography"), BIO_CHARS)
    row["bio_links"] = [link.get("url") for link in profile.get("bio_links") or []] or None
    row["recent_posts"] = [compact_post(p) for p in (profile.get("recent_posts") or [])[:12]]
    return _present(row)


def compact_user_posts(result: dict) -> dict:
    return _present(
        {
            "username": result.get("username"),
            "full_name": result.get("full_name"),
            "is_private": result.get("is_private"),
            "posts_count": result.get("posts_count"),
            "posts": [compact_post(p) for p in result.get("posts") or []],
            "more_available": bool(result.get("more_available")),
        }
    )


def compact_search_hits(result: dict) -> dict:
    return {
        "accounts": [
            _present({k: u.get(k) for k in ("username", "full_name", "is_verified", "is_private")})
            for u in result.get("users") or []
        ],
        "hashtags": result.get("hashtags") or [],
        "places": [p.get("name") for p in result.get("places") or [] if p.get("name")],
    }


def describe_result(name: str, artifact: dict | None, error: str | None = None) -> str:
    """One line for a step list: what the tool brought back."""
    if error:
        return error
    if not isinstance(artifact, dict):
        return "done"
    if name == "search_hashtags":
        tags = ", ".join("#" + str(t.get("tag")) for t in artifact.get("tags") or [])
        return f"{tags}: {_plural(artifact.get('post_count'), 'post')}, {_plural(artifact.get('user_count'), 'account')}"
    if name == "get_engagement":
        text = (
            f"{_plural(artifact.get('post_count'), 'post')}: {_plural(artifact.get('commenter_count'), 'commenter')}, "
            f"{_plural(artifact.get('liker_count'), 'liker')}"
        )
        return f"{text} · {artifact['message']}" if artifact.get("message") else text
    if name == "get_profile":
        who = f"@{artifact.get('username')}" + (f" ({artifact['full_name']})" if artifact.get("full_name") else "")
        return (
            f"{who}: {_plural(artifact.get('followers'), 'follower')}, {_plural(artifact.get('following'), 'following')}"
            f", {_plural(artifact.get('posts_count'), 'post')}"
        ).replace("followings", "following")
    if name == "get_user_posts":
        return f"@{artifact.get('username')}: {_plural(len(artifact.get('posts') or []), 'post')}"
    if name == "get_post":
        return (
            f"{artifact.get('shortcode')} by @{artifact.get('owner')}: {_plural(artifact.get('likes'), 'like')}, "
            f"{_plural(artifact.get('comments'), 'comment')}"
        )
    if name == "search_instagram":
        return (
            f"{_plural(len(artifact.get('users') or []), 'account')}, {_plural(len(artifact.get('hashtags') or []), 'hashtag')}, "
            f"{_plural(len(artifact.get('places') or []), 'place')}"
        )
    return "done"


def _dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


# ---- the tools ----------------------------------------------------------------------------------------
def build_tools(acquire: Acquire) -> list[StructuredTool]:
    """The agent's tools, bound to a way of borrowing the browser (``SharedSession.acquire``)."""

    async def search_hashtags(tags: list[str], max_posts_per_tag: int = 24, max_scrolls: int = 1) -> tuple[str, dict]:
        async with acquire() as session:
            result = await hashtags.crawl_hashtags(
                session, tags, max_posts_per_tag=max_posts_per_tag, max_scrolls=max_scrolls, include_posts=True
            )
        return _dump(compact_search(result)), result

    async def get_engagement(
        posts: list[str], max_comments_per_post: int = 20, include_likers: bool = True
    ) -> tuple[str, dict]:
        async with acquire() as session:
            result = await engagement.collect_engagement(
                session, posts, max_comments=max_comments_per_post, include_likers=include_likers
            )
        return _dump(compact_engagement(result)), result

    async def get_profile(username: str) -> tuple[str, dict]:
        async with acquire() as session:
            result = await profiles.get_profile(session, username)
        return _dump(compact_profile(result)), result

    async def get_user_posts(username: str, limit: int = 12) -> tuple[str, dict]:
        async with acquire() as session:
            result = await profiles.get_user_posts(session, username, limit=limit)
        return _dump(compact_user_posts(result)), result

    async def get_post(post: str) -> tuple[str, dict]:
        async with acquire() as session:
            result = await api.get_post(session, post)
        return _dump(compact_post(result)), result

    async def search_instagram(query: str) -> tuple[str, dict]:
        async with acquire() as session:
            result = await api.search(session, query)
        return _dump(compact_search_hits(result)), result

    def tool(coroutine: Callable, name: str, description: str, args_schema: type[BaseModel]) -> StructuredTool:
        return StructuredTool.from_function(
            coroutine=coroutine,
            name=name,
            description=description,
            args_schema=args_schema,
            response_format="content_and_artifact",
        )

    return [
        tool(
            search_hashtags,
            "search_hashtags",
            "Posts under one or more hashtags plus the accounts that posted them (most posts first). "
            "Cost: 1 + max_scrolls requests per hashtag; the first page holds about 24 posts and every scroll adds "
            "about 24 more. Returns tags (per-hashtag counts), accounts (username, name, post_count, hashtags) and "
            "posts (shortcode, owner, likes, comments, caption). Use the shortcodes with get_engagement.",
            SearchHashtagsArgs,
        ),
        tool(
            get_engagement,
            "get_engagement",
            "Who commented on and who liked the given posts. Cost: about 2 requests per post, so pass 10-20 posts at a "
            "time. Returns people (one row per account with comment_count, liked_count and the posts involved), "
            "per-post counts and sample comments. Instagram exposes at most about 100 likers per post.",
            GetEngagementArgs,
        ),
        tool(
            get_profile,
            "get_profile",
            "One account's profile: full name, bio, bio links, followers, following, post count, verified / private / "
            "business flags, city and its 12 newest posts. Cost: one profile page view. Use it to answer who an "
            "account is or how big it is; check a handful of accounts, not dozens.",
            UsernameArgs,
        ),
        tool(
            get_user_posts,
            "get_user_posts",
            "Newest-first posts of one account with caption and counts. Cost: one profile page view, plus one "
            "scroll per 12 posts beyond the first 12.",
            UserPostsArgs,
        ),
        tool(get_post, "get_post", "One post by shortcode or URL: owner, caption, counts, date. Cost: 1 request.", PostArgs),
        tool(
            search_instagram,
            "search_instagram",
            "Instagram's own search: accounts, hashtags (with their post counts) and places matching a keyword. "
            "Cost: 1 request. Good for finding related hashtags before crawling.",
            QueryArgs,
        ),
    ]
