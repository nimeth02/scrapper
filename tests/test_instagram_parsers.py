from app.instagram.api import (
    build_url,
    collect_medias,
    parse_comment,
    parse_feed_item,
    parse_hashtag,
    parse_profile,
    parse_search,
)

FEED_ITEM = {
    "pk": "3200000000000000001",
    "id": "3200000000000000001_12345",
    "code": "CxYz1-_Ab",
    "media_type": 8,
    "product_type": "carousel_container",
    "taken_at": 1700000000,
    "caption": {"text": "Hello #tea"},
    "like_count": 42,
    "comment_count": 3,
    "user": {"pk": 12345, "username": "natgeo"},
    "location": {"name": "Kandy"},
    "image_versions2": {"candidates": [{"url": "https://cdn/img-cover.jpg"}]},
    "carousel_media": [
        {"media_type": 1, "image_versions2": {"candidates": [{"url": "https://cdn/img1.jpg"}]}},
        {"media_type": 2, "image_versions2": {"candidates": [{"url": "https://cdn/thumb2.jpg"}]}, "video_versions": [{"url": "https://cdn/v2.mp4"}]},
    ],
}


def test_parse_feed_item_carousel() -> None:
    post = parse_feed_item(FEED_ITEM)
    assert post["id"] == "3200000000000000001"
    assert post["shortcode"] == "CxYz1-_Ab"
    assert post["url"] == "https://www.instagram.com/p/CxYz1-_Ab/"
    assert post["type"] == "carousel"
    assert post["taken_at"] == "2023-11-14T22:13:20+00:00"
    assert post["caption"] == "Hello #tea"
    assert (post["likes"], post["comments"]) == (42, 3)
    assert post["owner"] == "natgeo"
    assert post["owner_id"] == "12345"
    assert post["location"] == "Kandy"
    assert post["image_url"] == "https://cdn/img-cover.jpg"
    assert post["carousel"] == [
        {"type": "photo", "image_url": "https://cdn/img1.jpg", "video_url": None},
        {"type": "video", "image_url": "https://cdn/thumb2.jpg", "video_url": "https://cdn/v2.mp4"},
    ]


def test_parse_feed_item_tolerates_missing_fields() -> None:
    post = parse_feed_item({"pk": 7, "media_type": 2, "video_versions": [{"url": "v.mp4"}], "play_count": 99})
    assert post["id"] == "7"
    assert post["type"] == "video"
    assert post["video_url"] == "v.mp4"
    assert post["views"] == 99
    assert post["caption"] is None
    assert post["url"] is None
    assert "carousel" not in post


def test_parse_profile_with_graphql_posts() -> None:
    user = {
        "id": "12345",
        "username": "natgeo",
        "full_name": "National Geographic",
        "biography": "Bio",
        "external_url": "https://natgeo.com",
        "is_private": False,
        "is_verified": True,
        "category_name": "Media",
        "profile_pic_url_hd": "https://cdn/hd.jpg",
        "edge_followed_by": {"count": 1000},
        "edge_follow": {"count": 10},
        "edge_owner_to_timeline_media": {
            "count": 2,
            "edges": [
                {
                    "node": {
                        "id": "1",
                        "shortcode": "AAA",
                        "__typename": "GraphSidecar",
                        "is_video": False,
                        "taken_at_timestamp": 1700000000,
                        "display_url": "https://cdn/a.jpg",
                        "edge_media_to_caption": {"edges": [{"node": {"text": "cap"}}]},
                        "edge_liked_by": {"count": 5},
                        "edge_media_to_comment": {"count": 1},
                    }
                },
                {"node": {"id": "2", "shortcode": "BBB", "is_video": True, "video_view_count": 77, "edge_media_preview_like": {"count": 9}}},
            ],
        },
    }
    profile = parse_profile(user)
    assert profile["username"] == "natgeo"
    assert (profile["followers"], profile["following"], profile["posts_count"]) == (1000, 10, 2)
    assert profile["category"] == "Media"
    assert profile["profile_pic_url"] == "https://cdn/hd.jpg"
    first, second = profile["recent_posts"]
    assert first["type"] == "carousel"
    assert first["caption"] == "cap"
    assert first["likes"] == 5
    assert first["url"] == "https://www.instagram.com/p/AAA/"
    assert second["type"] == "video"
    assert second["views"] == 77
    assert second["likes"] == 9
    assert second["caption"] is None


def test_parse_comment() -> None:
    comment = parse_comment(
        {"pk": 555, "text": "Nice", "created_at": 1700000000, "user": {"pk": 1, "username": "bob"}, "comment_like_count": 2, "child_comment_count": 1}
    )
    assert comment == {
        "id": "555",
        "text": "Nice",
        "created_at": "2023-11-14T22:13:20+00:00",
        "username": "bob",
        "user_id": "1",
        "likes": 2,
        "replies": 1,
    }


def test_collect_medias_walks_nested_sections() -> None:
    sections = [
        {"layout_type": "media_grid", "layout_content": {"medias": [{"media": {"pk": 1, "code": "A"}}, {"media": {"pk": 2, "code": "B"}}]}},
        {"layout_type": "two_by_two_right", "layout_content": {"fill_items": [{"media": {"pk": 3, "code": "C"}}], "two_by_two_item": {"media": {"pk": 4, "code": "D"}}}},
        {"layout_content": {"one_by_two_item": {"clips": {"items": [{"media": {"pk": 5, "code": "E"}}]}}}},
    ]
    assert [m["pk"] for m in collect_medias(sections)] == [1, 2, 3, 4, 5]
    assert collect_medias(None) == []


def test_parse_hashtag() -> None:
    data = {
        "id": "17841563",
        "name": "tea",
        "media_count": 12345,
        "top": {"sections": [{"layout_content": {"medias": [{"media": {"pk": 1, "code": "A", "media_type": 1}}]}}]},
        "recent": {"sections": []},
    }
    tag = parse_hashtag(data)
    assert (tag["name"], tag["media_count"]) == ("tea", 12345)
    assert [p["shortcode"] for p in tag["top_posts"]] == ["A"]
    assert tag["recent_posts"] == []


def test_parse_search() -> None:
    data = {
        "users": [{"user": {"pk": 1, "username": "teafarm", "full_name": "Tea Farm", "is_verified": False, "is_private": False}}],
        "hashtags": [{"hashtag": {"name": "tea", "media_count": 5}}],
        "places": [{"place": {"location": {"pk": 9, "name": "Kandy", "address": "Sri Lanka"}}}],
    }
    result = parse_search(data)
    assert result["users"][0]["username"] == "teafarm"
    assert result["hashtags"] == [{"name": "tea", "media_count": 5}]
    assert result["places"] == [{"id": "9", "name": "Kandy", "address": "Sri Lanka"}]


def test_build_url() -> None:
    assert build_url("/api/v1/x/", {"a": 1, "b": None}) == "https://www.instagram.com/api/v1/x/?a=1"
    assert build_url("api/v1/x/?q=1", {"a": "b c"}) == "https://www.instagram.com/api/v1/x/?q=1&a=b+c"
    assert build_url("https://www.instagram.com/api/v1/y/") == "https://www.instagram.com/api/v1/y/"


# ---- response classification -------------------------------------------------
import json

import pytest

from app.instagram.api import InstagramAPIError, LoginRequired, RateLimited, parse_response


def test_parse_response_returns_json() -> None:
    assert parse_response("/x", 200, json.dumps({"data": {"user": {"id": 1}}})) == {"data": {"user": {"id": 1}}}
    assert parse_response("/x", 200, "[1, 2]") == [1, 2]


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, '{"message": "Please wait a few minutes before you try again.", "status": "fail"}'),
        (429, ""),
        (400, '{"message": "feedback_required", "status": "fail"}'),
    ],
)
def test_parse_response_rate_limited(status: int, body: str) -> None:
    with pytest.raises(RateLimited):
        parse_response("/x", status, body)


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, '{"message": "login_required", "status": "fail"}'),
        (403, ""),
        (400, '{"message": "checkpoint_required", "status": "fail"}'),
    ],
)
def test_parse_response_login_required(status: int, body: str) -> None:
    with pytest.raises(LoginRequired):
        parse_response("/x", status, body)


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, "<!DOCTYPE html><html>login page</html>"),
        (200, '{"status": "fail", "message": "Media not found"}'),
        (404, '{"message": "Not Found"}'),
        (500, ""),
    ],
)
def test_parse_response_other_errors(status: int, body: str) -> None:
    with pytest.raises(InstagramAPIError) as exc_info:
        parse_response("/x", status, body)
    assert type(exc_info.value) is InstagramAPIError
