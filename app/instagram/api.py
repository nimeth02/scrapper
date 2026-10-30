"""Read Instagram through the JSON endpoints its own web app calls, from inside a logged-in page.

Requests are issued with ``fetch`` *inside* the Playwright page, so they carry the real browser's
cookies, user agent and TLS fingerprint. The endpoints are undocumented and change without notice:
every parser here is defensive, and :func:`fetch_json` is exposed as an escape hatch for anything
not wrapped.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from app.instagram.browser import INSTAGRAM_ORIGIN, BrowserSession
from app.instagram.codec import shortcode_from_url, shortcode_to_media_id, username_from_url

log = logging.getLogger(__name__)

BASE_URL = INSTAGRAM_ORIGIN
IG_APP_ID = "936619743392459"  # the web app's public app id; /api/v1/* refuses requests without it

_FETCH_JS = r"""
async ({ url, appId }) => {
  const csrf = (document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/) || [])[1] || "";
  const res = await fetch(url, {
    credentials: "include",
    headers: {
      "accept": "*/*",
      "x-ig-app-id": appId,
      "x-csrftoken": csrf,
      "x-requested-with": "XMLHttpRequest",
      "x-asbd-id": "129477",
    },
  });
  return { status: res.status, body: await res.text() };
}
"""

_LOGIN_MESSAGES = {"login_required", "checkpoint_required", "challenge_required", "consent_required"}
_RATE_MESSAGES = {"feedback_required", "rate_limit_error"}
MEDIA_TYPES = {1: "photo", 2: "video", 8: "carousel"}


class InstagramAPIError(Exception):
    """Instagram answered, but not with the data we asked for."""

    def __init__(self, message: str, *, status: int | None = None, path: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.path = path


class LoginRequired(InstagramAPIError):
    """The session is missing, expired or needs a checkpoint; log in again."""


class RateLimited(InstagramAPIError):
    """Instagram is throttling or has temporarily blocked this account / IP."""


# ---- transport -----------------------------------------------------------------
def build_url(path: str, params: dict[str, Any] | None = None) -> str:
    if path.startswith("http"):
        url = path
    else:
        url = BASE_URL + (path if path.startswith("/") else "/" + path)
    clean = {k: v for k, v in (params or {}).items() if v is not None}
    if clean:
        url += ("&" if "?" in url else "?") + urlencode(clean)
    return url


async def fetch_json(session: BrowserSession, path: str, params: dict[str, Any] | None = None) -> Any:
    """GET an Instagram web-API path (e.g. ``/api/v1/users/web_profile_info/``) and return the JSON."""
    page = await session.page()
    if not page.url.startswith(BASE_URL):
        await session.goto(BASE_URL)
    url = build_url(path, params)
    await session.throttle()
    log.debug("GET %s", url)
    result = await page.evaluate(_FETCH_JS, {"url": url, "appId": IG_APP_ID})
    return parse_response(path, int(result["status"]), result.get("body") or "")


def parse_response(path: str, status: int, body: str) -> Any:
    """Turn a raw response into JSON, or raise the matching :class:`InstagramAPIError` subclass."""
    data: Any = None
    if body.lstrip()[:1] in ("{", "["):
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = None
    message = data.get("message") if isinstance(data, dict) else None
    lowered = message.lower() if isinstance(message, str) else ""

    # Checked first: Instagram's throttling reply is a 401 too ("Please wait a few minutes...").
    if status == 429 or message in _RATE_MESSAGES or "wait a few minutes" in lowered:
        raise RateLimited(
            f"GET {path} -> HTTP {status} ({message}). Instagram is throttling this account/IP; "
            "wait a while before retrying and raise INSTAGRAM_REQUEST_DELAY_SECONDS.",
            status=status,
            path=path,
        )
    if status in (401, 403) or message in _LOGIN_MESSAGES:
        raise LoginRequired(f"GET {path} -> HTTP {status} ({message or 'not logged in'})", status=status, path=path)
    if status != 200 or data is None:
        raise InstagramAPIError(f"GET {path} -> HTTP {status}: {body[:300]!r}", status=status, path=path)
    if isinstance(data, dict) and data.get("status") == "fail":
        raise InstagramAPIError(f"GET {path} failed: {message or data}", status=status, path=path)
    return data


# ---- parsers (pure) ------------------------------------------------------------
def _ts(epoch: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(epoch), tz=UTC).isoformat() if epoch else None
    except (TypeError, ValueError, OSError):
        return None


def _str_id(value: Any) -> str | None:
    return str(value).split("_", 1)[0] if value not in (None, "") else None


def _best_image(item: dict) -> str | None:
    candidates = (item.get("image_versions2") or {}).get("candidates") or []
    return candidates[0].get("url") if candidates else item.get("display_url")


def _video_url(item: dict) -> str | None:
    versions = item.get("video_versions") or []
    return versions[0].get("url") if versions else item.get("video_url")


def post_url(shortcode: str | None) -> str | None:
    return f"{BASE_URL}/p/{shortcode}/" if shortcode else None


def parse_feed_item(item: dict) -> dict:
    """A post as returned by the ``/api/v1/`` endpoints (feed, media info, hashtags)."""
    code = item.get("code")
    user = item.get("user") or item.get("owner") or {}
    out = {
        "id": _str_id(item.get("pk") or item.get("id")),
        "shortcode": code,
        "url": post_url(code),
        "type": MEDIA_TYPES.get(item.get("media_type"), str(item.get("media_type"))),
        "product_type": item.get("product_type"),  # feed / clips (reel) / igtv
        "taken_at": _ts(item.get("taken_at")),
        "caption": (item.get("caption") or {}).get("text"),
        "likes": item.get("like_count"),
        "comments": item.get("comment_count"),
        "views": item.get("play_count") or item.get("view_count"),
        "owner": user.get("username"),
        "owner_id": _str_id(user.get("pk") or user.get("id")),
        "location": (item.get("location") or {}).get("name"),
        "image_url": _best_image(item),
        "video_url": _video_url(item),
    }
    if item.get("carousel_media"):
        out["carousel"] = [
            {
                "type": MEDIA_TYPES.get(m.get("media_type"), str(m.get("media_type"))),
                "image_url": _best_image(m),
                "video_url": _video_url(m),
            }
            for m in item["carousel_media"]
        ]
    return out


def parse_graphql_node(node: dict) -> dict:
    """A post as embedded in ``web_profile_info`` (GraphQL shape)."""
    code = node.get("shortcode")
    captions = (node.get("edge_media_to_caption") or {}).get("edges") or []
    if node.get("is_video"):
        kind = "video"
    elif node.get("__typename") == "GraphSidecar":
        kind = "carousel"
    else:
        kind = "photo"
    return {
        "id": _str_id(node.get("id")),
        "shortcode": code,
        "url": post_url(code),
        "type": kind,
        "product_type": node.get("product_type"),
        "taken_at": _ts(node.get("taken_at_timestamp")),
        "caption": (captions[0].get("node") or {}).get("text") if captions else None,
        "likes": ((node.get("edge_liked_by") or node.get("edge_media_preview_like")) or {}).get("count"),
        "comments": (node.get("edge_media_to_comment") or {}).get("count"),
        "views": node.get("video_view_count"),
        "image_url": node.get("display_url"),
        "video_url": node.get("video_url"),
    }


def parse_profile(user: dict) -> dict:
    timeline = user.get("edge_owner_to_timeline_media") or {}
    return {
        "id": _str_id(user.get("id") or user.get("pk")),
        "username": user.get("username"),
        "full_name": user.get("full_name"),
        "biography": user.get("biography"),
        "external_url": user.get("external_url"),
        "followers": (user.get("edge_followed_by") or {}).get("count", user.get("follower_count")),
        "following": (user.get("edge_follow") or {}).get("count", user.get("following_count")),
        "posts_count": timeline.get("count", user.get("media_count")),
        "is_private": user.get("is_private"),
        "is_verified": user.get("is_verified"),
        "is_business_account": user.get("is_business_account"),
        "is_professional_account": user.get("is_professional_account"),
        "category": user.get("category_name") or user.get("category"),
        "profile_pic_url": user.get("profile_pic_url_hd") or user.get("profile_pic_url"),
        "recent_posts": [parse_graphql_node(e["node"]) for e in (timeline.get("edges") or []) if e.get("node")],
    }


def parse_comment(comment: dict) -> dict:
    user = comment.get("user") or {}
    return {
        "id": _str_id(comment.get("pk")),
        "text": comment.get("text"),
        "created_at": _ts(comment.get("created_at")),
        "username": user.get("username"),
        "user_id": _str_id(user.get("pk") or user.get("id")),
        "likes": comment.get("comment_like_count"),
        "replies": comment.get("child_comment_count"),
    }


def collect_medias(sections: Any) -> list[dict]:
    """Hashtag results come in nested layout "sections"; pull out every ``media`` object."""
    found: list[dict] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            media = obj.get("media")
            if isinstance(media, dict) and ("pk" in media or "code" in media):
                found.append(media)
                return
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    walk(sections)
    return found


def parse_hashtag(data: dict) -> dict:
    return {
        "name": data.get("name"),
        "id": _str_id(data.get("id")),
        "media_count": data.get("media_count"),
        "top_posts": [parse_feed_item(m) for m in collect_medias((data.get("top") or {}).get("sections"))],
        "recent_posts": [parse_feed_item(m) for m in collect_medias((data.get("recent") or {}).get("sections"))],
    }


def parse_search(data: dict) -> dict:
    users = [u.get("user") or {} for u in data.get("users") or []]
    tags = [h.get("hashtag") or {} for h in data.get("hashtags") or []]
    places = [(p.get("place") or {}).get("location") or {} for p in data.get("places") or []]
    return {
        "users": [
            {
                "id": _str_id(u.get("pk") or u.get("id")),
                "username": u.get("username"),
                "full_name": u.get("full_name"),
                "is_verified": u.get("is_verified"),
                "is_private": u.get("is_private"),
                "profile_pic_url": u.get("profile_pic_url"),
            }
            for u in users
        ],
        "hashtags": [{"name": h.get("name"), "media_count": h.get("media_count")} for h in tags],
        "places": [{"id": _str_id(p.get("pk")), "name": p.get("name"), "address": p.get("address")} for p in places],
    }


# ---- high-level reads --------------------------------------------------------
async def whoami(session: BrowserSession) -> dict | None:
    """The account the session belongs to, or ``None`` when there is no session cookie."""
    user_id = (await session.cookies()).get("ds_user_id")
    if not user_id:
        return None
    data = await fetch_json(session, f"/api/v1/users/{user_id}/info/")
    user = data.get("user") or {}
    return {"user_id": user_id, "username": user.get("username"), "full_name": user.get("full_name")}


async def get_profile(session: BrowserSession, username: str) -> dict:
    name = username_from_url(username)
    data = await fetch_json(session, "/api/v1/users/web_profile_info/", {"username": name})
    user = (data.get("data") or {}).get("user")
    if not user:
        raise InstagramAPIError(f"No profile data for @{name}; does the account exist?")
    return parse_profile(user)


async def get_user_posts(
    session: BrowserSession, username: str, *, limit: int = 12, max_id: str | None = None
) -> dict:
    """Newest-first posts. Pass the returned ``next_max_id`` back in to fetch the next page."""
    name = username_from_url(username)
    limit = max(1, limit)
    posts: list[dict] = []
    cursor = max_id
    more = True
    user_id: str | None = None
    while len(posts) < limit and more:
        params = {"count": min(limit - len(posts), 12), "max_id": cursor}
        if user_id is None:
            try:
                data = await fetch_json(session, f"/api/v1/feed/user/{name}/username/", params)
            except (LoginRequired, RateLimited):
                raise
            except InstagramAPIError as exc:
                log.info("username feed endpoint failed (%s); retrying with the user id", exc)
                user_id = (await get_profile(session, name))["id"]
                data = await fetch_json(session, f"/api/v1/feed/user/{user_id}/", params)
        else:
            data = await fetch_json(session, f"/api/v1/feed/user/{user_id}/", params)
        items = data.get("items") or []
        posts.extend(parse_feed_item(i) for i in items)
        more = bool(data.get("more_available")) and bool(items)
        cursor = data.get("next_max_id")
    return {"username": name, "posts": posts[:limit], "next_max_id": cursor if more else None, "more_available": more}


async def get_post(session: BrowserSession, post: str) -> dict:
    """One post by shortcode or URL (``/p/``, ``/reel/``, ``/tv/``)."""
    code = shortcode_from_url(post)
    data = await fetch_json(session, f"/api/v1/media/{shortcode_to_media_id(code)}/info/")
    items = data.get("items") or []
    if not items:
        raise InstagramAPIError(f"No data for post {code}; is it deleted or private?")
    return parse_feed_item(items[0])


async def get_comments(
    session: BrowserSession, post: str, *, limit: int = 50, min_id: str | None = None
) -> dict:
    code = shortcode_from_url(post)
    media_id = shortcode_to_media_id(code)
    limit = max(1, limit)
    comments: list[dict] = []
    cursor = min_id
    total = None
    while len(comments) < limit:
        params = {"can_support_threading": "true", "permalink_enabled": "false", "min_id": cursor}
        data = await fetch_json(session, f"/api/v1/media/{media_id}/comments/", params)
        batch = data.get("comments") or []
        total = data.get("comment_count", total)
        comments.extend(parse_comment(c) for c in batch)
        next_cursor = data.get("next_min_id")
        if not batch or not next_cursor or next_cursor == cursor:
            cursor = None
            break
        cursor = next_cursor
    return {"shortcode": code, "comment_count": total, "comments": comments[:limit], "next_min_id": cursor}


async def get_hashtag(session: BrowserSession, tag: str) -> dict:
    name = tag.strip().lstrip("#")
    if not name:
        raise ValueError("empty hashtag")
    data = await fetch_json(session, "/api/v1/tags/web_info/", {"tag_name": name})
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    return parse_hashtag(payload)


async def search(session: BrowserSession, query: str) -> dict:
    data = await fetch_json(
        session,
        "/api/v1/web/search/topsearch/",
        {"context": "blended", "query": query, "include_reel": "false", "search_surface": "web_top_search"},
    )
    return parse_search(data)


def instagram_url(value: str) -> str:
    """Accept a full URL, a site-relative path or a bare username."""
    value = value.strip()
    if value.startswith("http"):
        return value
    if value.startswith("/"):
        return BASE_URL + value
    return f"{BASE_URL}/{username_from_url(value)}/"


async def page_text(session: BrowserSession, url: str, *, max_chars: int = 20000, settle_seconds: float = 2.5) -> dict:
    """Fallback for anything the JSON endpoints do not cover: the page's visible text."""
    page = await session.goto(instagram_url(url))
    await page.wait_for_timeout(settle_seconds * 1000)
    text = await page.evaluate("() => document.body ? document.body.innerText : ''")
    return {"url": page.url, "title": await page.title(), "text": text[:max_chars], "truncated": len(text) > max_chars}


async def screenshot(session: BrowserSession, url: str | None = None) -> bytes:
    page = await (session.goto(instagram_url(url)) if url else session.page())
    if url:
        await page.wait_for_timeout(1500)
    return await page.screenshot(type="png")
