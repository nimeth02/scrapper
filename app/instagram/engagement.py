"""Who engages with posts: the commenters and likers of each post, merged into one list of people.

Both reads go through the web endpoints Instagram's own post page uses: ``/api/v1/media/<id>/comments/``
(paged with ``next_min_id``) and ``/api/v1/media/<id>/likers/`` (the first hundred or so likers plus the
total; Instagram offers no further pages on the web).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from app.instagram import api
from app.instagram.api import InstagramAPIError, RateLimited, post_url
from app.instagram.browser import BrowserSession
from app.instagram.codec import shortcode_from_url

log = logging.getLogger(__name__)

_ACCOUNT_FIELDS = ("username", "user_id", "full_name", "is_private", "is_verified", "profile_pic_url", "profile_url")


def summarize_commenters(comments: list[dict], *, sample_texts: int = 3) -> list[dict]:
    """One row per commenting account, most comments first, with up to ``sample_texts`` of their comments."""
    rows: dict[str, dict] = {}
    for comment in comments:
        username = comment.get("username")
        if not username:
            continue
        row = rows.get(username)
        if row is None:
            row = rows[username] = {
                "username": username,
                "user_id": comment.get("user_id"),
                "full_name": comment.get("full_name"),
                "is_private": comment.get("is_private"),
                "is_verified": comment.get("is_verified"),
                "profile_pic_url": comment.get("profile_pic_url"),
                "profile_url": f"{api.BASE_URL}/{username}/",
                "comment_count": 0,
                "comments": [],
            }
        row["comment_count"] += 1
        if comment.get("text") and len(row["comments"]) < sample_texts:
            row["comments"].append(comment["text"])
    return sorted(rows.values(), key=lambda r: (-r["comment_count"], r["username"]))


async def post_engagement(
    session: BrowserSession, post: str, *, max_comments: int = 50, include_likers: bool = True
) -> dict:
    """Comments (with their authors summarised as ``commenters``) and likers of one post (shortcode or URL)."""
    code = shortcode_from_url(post)
    result: dict[str, Any] = {
        "shortcode": code,
        "url": post_url(code),
        "comment_count": None,
        "comments": [],
        "commenters": [],
        "comments_truncated": False,
        "like_count": None,
        "likers": [],
        "likers_truncated": False,
    }
    if max_comments > 0:
        comments = await api.get_comments(session, code, limit=max_comments)
        result.update(
            comment_count=comments["comment_count"],
            comments=comments["comments"],
            commenters=summarize_commenters(comments["comments"]),
            comments_truncated=bool(comments.get("next_min_id")),
        )
    if include_likers:
        likers = await api.get_likers(session, code)
        result.update(
            like_count=likers["like_count"],
            likers=likers["likers"],
            likers_truncated=(likers["like_count"] or 0) > len(likers["likers"]),
        )
    return result


def merge_engagement(results: list[dict]) -> dict:
    """One row per person across several :func:`post_engagement` results: how many comments they left,
    how many of the posts they liked, and which posts. Most active first."""
    people: dict[str, dict] = {}

    def row_for(user: dict) -> dict:
        row = people.get(user["username"])
        if row is None:
            row = people[user["username"]] = {k: user.get(k) for k in _ACCOUNT_FIELDS}
            row.update(comment_count=0, liked_count=0, posts_commented=[], posts_liked=[])
        return row

    for result in results:
        code = result["shortcode"]
        for commenter in result.get("commenters") or []:
            row = row_for(commenter)
            row["comment_count"] += commenter["comment_count"]
            row["posts_commented"].append(code)
        for liker in result.get("likers") or []:
            if not liker.get("username"):
                continue
            row = row_for(liker)
            row["liked_count"] += 1
            row["posts_liked"].append(code)
    ordered = sorted(people.values(), key=lambda r: (-(r["comment_count"] + r["liked_count"]), r["username"]))
    return {
        "post_count": len(results),
        "people_count": len(ordered),
        "commenter_count": sum(1 for r in ordered if r["comment_count"]),
        "liker_count": sum(1 for r in ordered if r["liked_count"]),
        "people": ordered,
    }


async def collect_engagement(
    session: BrowserSession,
    posts: list[str],
    *,
    max_comments: int = 50,
    include_likers: bool = True,
    on_post: Callable[[dict], None] | None = None,
    on_progress: Callable[[dict], None] | None = None,
) -> dict:
    """Commenters and likers of several posts (shortcodes or URLs), merged with :func:`merge_engagement`.

    Stops early, keeping what it has, as soon as Instagram starts throttling (``message`` says so). A
    deleted or private post is recorded with its ``error`` and the crawl moves on. ``on_progress`` fires
    before each post, ``on_post`` with each finished result.
    """
    results: list[dict] = []
    message: str | None = None
    for index, post in enumerate(posts):
        code = shortcode_from_url(post)
        if on_progress is not None:
            on_progress({"index": index, "total": len(posts), "shortcode": code})
        try:
            result = await post_engagement(session, code, max_comments=max_comments, include_likers=include_likers)
        except RateLimited as exc:
            message = f"Stopped after {len(results)} of {len(posts)} posts: {exc}"
            log.warning(message)
            break
        except InstagramAPIError as exc:
            result = {"shortcode": code, "url": post_url(code), "error": str(exc), "comments": [], "commenters": [], "likers": []}
        results.append(result)
        if on_post is not None:
            on_post(result)
    merged = merge_engagement(results)
    merged["posts"] = results
    if message:
        merged["message"] = message
    return merged
