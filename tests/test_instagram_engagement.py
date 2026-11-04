"""Commenter / liker summaries and merging (pure functions, fakes for the network)."""

import pytest

from app.instagram import engagement
from app.instagram.api import InstagramAPIError, RateLimited, parse_comment, parse_user
from app.instagram.engagement import collect_engagement, merge_engagement, post_engagement, summarize_commenters

USER = {"pk": 7, "id": "7", "username": "danfetea", "full_name": "Danfe Tea", "is_private": False, "is_verified": True, "profile_pic_url": "https://cdn/p.jpg"}


def test_parse_user_builds_the_account_row() -> None:
    assert parse_user(USER) == {
        "username": "danfetea",
        "user_id": "7",
        "full_name": "Danfe Tea",
        "is_private": False,
        "is_verified": True,
        "profile_pic_url": "https://cdn/p.jpg",
        "profile_url": "https://www.instagram.com/danfetea/",
    }
    assert parse_user(None)["username"] is None and parse_user({})["profile_url"] is None


def test_parse_comment_carries_the_author_details() -> None:
    comment = parse_comment({"pk": "1", "text": "hi", "created_at": 1700000000, "user": USER, "comment_like_count": 2, "child_comment_count": 0})
    assert comment["username"] == "danfetea" and comment["full_name"] == "Danfe Tea" and comment["is_verified"] is True
    assert comment["profile_pic_url"] == "https://cdn/p.jpg" and comment["likes"] == 2


def test_summarize_commenters_counts_and_samples() -> None:
    comments = [
        {"username": "a", "user_id": "1", "text": "one", "full_name": "A"},
        {"username": "b", "user_id": "2", "text": "two"},
        {"username": "a", "user_id": "1", "text": "three"},
        {"username": None, "text": "ghost"},
    ]
    rows = summarize_commenters(comments, sample_texts=1)
    assert [(r["username"], r["comment_count"]) for r in rows] == [("a", 2), ("b", 1)]
    assert rows[0]["comments"] == ["one"] and rows[0]["profile_url"] == "https://www.instagram.com/a/"


def test_merge_engagement_counts_roles_per_person() -> None:
    results = [
        {"shortcode": "P1", "commenters": [{**parse_user(USER), "comment_count": 2, "comments": ["x"]}], "likers": [parse_user({"username": "liker1", "pk": 1})]},
        {"shortcode": "P2", "commenters": [], "likers": [parse_user(USER), parse_user({"username": "liker1", "pk": 1}), {"username": None}]},
        {"shortcode": "P3", "error": "gone", "commenters": [], "likers": []},
    ]
    merged = merge_engagement(results)
    assert merged["post_count"] == 3 and merged["people_count"] == 2
    assert merged["commenter_count"] == 1 and merged["liker_count"] == 2
    top = merged["people"][0]
    assert top["username"] == "danfetea" and top["comment_count"] == 2 and top["liked_count"] == 1
    assert top["posts_commented"] == ["P1"] and top["posts_liked"] == ["P2"]
    assert merged["people"][1]["posts_liked"] == ["P1", "P2"]


async def test_post_engagement_combines_comments_and_likers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_comments(session, code, *, limit):  # noqa: ANN001
        assert (code, limit) == ("CxYz1Ab", 10)
        return {"shortcode": code, "comment_count": 25, "comments": [{"username": "a", "user_id": "1", "text": "hey"}], "next_min_id": "cursor"}

    async def fake_likers(session, code):  # noqa: ANN001
        return {"shortcode": code, "like_count": 500, "likers": [parse_user({"username": "l", "pk": 2})]}

    monkeypatch.setattr(engagement.api, "get_comments", fake_comments)
    monkeypatch.setattr(engagement.api, "get_likers", fake_likers)
    result = await post_engagement(object(), "https://www.instagram.com/p/CxYz1Ab/", max_comments=10)  # type: ignore[arg-type]
    assert result["shortcode"] == "CxYz1Ab" and result["url"] == "https://www.instagram.com/p/CxYz1Ab/"
    assert result["comment_count"] == 25 and result["comments_truncated"] is True
    assert result["commenters"][0]["username"] == "a"
    assert result["like_count"] == 500 and result["likers_truncated"] is True and result["likers"][0]["username"] == "l"

    skipped = await post_engagement(object(), "CxYz1Ab", max_comments=0, include_likers=False)  # type: ignore[arg-type]
    assert skipped["comments"] == [] and skipped["likers"] == [] and skipped["like_count"] is None


async def test_collect_engagement_records_errors_and_stops_on_throttling(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def fake_post(session, code, *, max_comments, include_likers):  # noqa: ANN001
        calls.append(code)
        if code == "GONE":
            raise InstagramAPIError("No data for post GONE")
        if code == "SLOW":
            raise RateLimited("HTTP 429")
        return {"shortcode": code, "commenters": [{**parse_user({"username": "c", "pk": 1}), "comment_count": 1, "comments": []}], "likers": []}

    monkeypatch.setattr(engagement, "post_engagement", fake_post)
    started: list[int] = []
    finished: list[str] = []
    result = await collect_engagement(
        object(),  # type: ignore[arg-type]
        ["OK1", "GONE", "SLOW", "NEVER"],
        max_comments=5,
        on_progress=lambda p: started.append(p["index"]),
        on_post=lambda r: finished.append(r["shortcode"]),
    )
    assert calls == ["OK1", "GONE", "SLOW"]
    assert started == [0, 1, 2] and finished == ["OK1", "GONE"]
    assert [p["shortcode"] for p in result["posts"]] == ["OK1", "GONE"]
    assert result["posts"][1]["error"].startswith("No data")
    assert result["people"][0]["username"] == "c" and result["post_count"] == 2
    assert result["message"].startswith("Stopped after 2 of 4 posts")
