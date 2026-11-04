"""Profiles read off the profile page: parsing the page's own GraphQL payloads, and the fallbacks."""

from __future__ import annotations

import pytest

from app.instagram import api, profiles
from app.instagram.api import InstagramAPIError, RateLimited
from app.instagram.profiles import ProfileNotFound, parse_page_profile, parse_timeline

PAGE_USER = {
    "pk": "797343551",
    "id": "797343551",
    "username": "dilmahceylontea",
    "full_name": "Dilmah Tea",
    "biography": "Garden fresh Ceylon Tea.",
    "external_url": "https://shop.dilmahtea.com/",
    "bio_links": [
        {"title": "Dilmah Global Webshop", "url": "https://shop.dilmahtea.com/"},
        {"title": "", "url": "http://www.dilmahtea.com"},
        {"title": "broken", "url": ""},
    ],
    "follower_count": 35684,
    "following_count": 86,
    "media_count": 4101,
    "is_private": False,
    "is_verified": False,
    "is_business": True,
    "is_professional_account": None,
    "account_type": 2,
    "category": "",
    "city_name": "Colombo, Sri Lanka",
    "profile_pic_url": "https://cdn/small.jpg",
    "hd_profile_pic_url_info": {"url": "https://cdn/hd.jpg"},
}


def node(code: str, owner: str, coauthors: list[str] | None = None) -> dict:
    return {
        "code": code,
        "pk": "1",
        "media_type": 1,
        "taken_at": 1793593809,
        "like_count": 31,
        "comment_count": 2,
        "caption": {"text": "Christmas tea"},
        "user": {"pk": "9", "username": owner},
        "coauthor_producers": [{"username": c} for c in coauthors or []],
    }


def test_parse_page_profile_maps_the_page_fields() -> None:
    profile = parse_page_profile(PAGE_USER)
    assert profile["id"] == "797343551" and profile["username"] == "dilmahceylontea" and profile["full_name"] == "Dilmah Tea"
    assert (profile["followers"], profile["following"], profile["posts_count"]) == (35684, 86, 4101)
    assert profile["is_business_account"] is True and profile["is_professional_account"] is True
    assert profile["category"] is None and profile["city"] == "Colombo, Sri Lanka"
    assert profile["profile_pic_url"] == "https://cdn/hd.jpg"
    assert profile["profile_url"] == "https://www.instagram.com/dilmahceylontea/"
    assert profile["bio_links"] == [
        {"title": "Dilmah Global Webshop", "url": "https://shop.dilmahtea.com/"},
        {"title": None, "url": "http://www.dilmahtea.com"},
    ]
    assert profile["recent_posts"] == []
    personal = parse_page_profile({"username": "x", "account_type": 1, "hd_profile_pic_url_info": None, "profile_pic_url": "p"})
    assert personal["is_professional_account"] is False and personal["profile_pic_url"] == "p"


def test_parse_timeline_reads_posts_and_coauthors() -> None:
    data = {
        "data": {
            profiles.TIMELINE_KEY: {
                "edges": [{"node": node("AAA", "dilmahceylontea")}, {"node": node("BBB", "teafriend", ["dilmahceylontea"])}, {"node": {}}],
                "page_info": {"has_next_page": True},
            }
        }
    }
    posts, more = parse_timeline(data)
    assert more is True and [p["shortcode"] for p in posts] == ["AAA", "BBB"]
    assert posts[0]["owner"] == "dilmahceylontea" and posts[0]["coauthors"] == [] and posts[0]["likes"] == 31
    assert posts[1]["owner"] == "teafriend" and posts[1]["coauthors"] == ["dilmahceylontea"]
    assert parse_timeline({"data": {}}) == ([], False)
    assert parse_timeline("nope") == ([], False)


async def test_get_profile_uses_the_page_and_falls_back_to_the_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def page_ok(session, username, **kwargs):  # noqa: ANN001
        calls.append("page")
        return {"username": username, "source": "page"}

    async def page_empty(session, username, **kwargs):  # noqa: ANN001
        calls.append("page")
        raise InstagramAPIError("loaded without its profile data")

    async def endpoint(session, username):  # noqa: ANN001
        calls.append("api")
        return {"username": username, "source": "api"}

    monkeypatch.setattr(api, "get_profile", endpoint)
    monkeypatch.setattr(profiles, "load_profile", page_ok)
    assert (await profiles.get_profile(object(), "tea"))["source"] == "page"  # type: ignore[arg-type]
    monkeypatch.setattr(profiles, "load_profile", page_empty)
    assert (await profiles.get_profile(object(), "tea"))["source"] == "api"  # type: ignore[arg-type]
    assert calls == ["page", "page", "api"]


@pytest.mark.parametrize("exc", [ProfileNotFound("@ghost does not exist"), RateLimited("HTTP 429")])
async def test_get_profile_does_not_fall_back_when_the_page_explains_itself(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    async def page(session, username, **kwargs):  # noqa: ANN001
        raise exc

    async def endpoint(session, username):  # noqa: ANN001
        raise AssertionError("the JSON endpoint must not be tried")

    monkeypatch.setattr(profiles, "load_profile", page)
    monkeypatch.setattr(api, "get_profile", endpoint)
    with pytest.raises(type(exc)):
        await profiles.get_profile(object(), "ghost")  # type: ignore[arg-type]


@pytest.mark.parametrize(("limit", "scrolls"), [(1, 0), (12, 0), (13, 1), (24, 1), (36, 2), (500, profiles.MAX_SCROLLS)])
async def test_get_user_posts_scrolls_for_more_than_a_page(monkeypatch: pytest.MonkeyPatch, limit: int, scrolls: int) -> None:
    seen: dict = {}

    async def page(session, username, *, max_posts, max_scrolls):  # noqa: ANN001
        seen.update(max_posts=max_posts, max_scrolls=max_scrolls)
        return {**parse_page_profile(PAGE_USER), "recent_posts": [{"shortcode": "AAA"}], "has_more_posts": True}

    monkeypatch.setattr(profiles, "load_profile", page)
    result = await profiles.get_user_posts(object(), "dilmahceylontea", limit=limit)  # type: ignore[arg-type]
    assert seen == {"max_posts": limit, "max_scrolls": scrolls}
    assert result == {
        "username": "dilmahceylontea",
        "full_name": "Dilmah Tea",
        "is_private": False,
        "posts_count": 4101,
        "posts": [{"shortcode": "AAA"}],
        "more_available": True,
        "next_max_id": None,
    }
