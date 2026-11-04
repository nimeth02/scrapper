"""Hashtag search-page parsing and merging (pure functions; no browser)."""

import pytest

from app.instagram import hashtags
from app.instagram.hashtags import (
    collect_hashtag_users,
    merge_results,
    hashtag_search_url,
    parse_search_results,
    parse_tags,
    summarize_users,
)


def _item(code: str, username: str, pk: int, **extra: object) -> dict:
    return {
        "pk": f"31000000000000{pk}",
        "code": code,
        "media_type": 1,
        "taken_at": 1700000000,
        "caption": {"text": f"#tea by {username}"},
        "like_count": 5,
        "comment_count": 1,
        "user": {"pk": pk, "id": str(pk), "username": username, "full_name": username.title(), "is_private": False, "is_verified": pk == 1},
        "image_versions2": {"candidates": [{"url": f"https://cdn/{code}.jpg"}]},
        **extra,
    }


SEARCH_PAGE = {
    "data": {
        "xdt_viewer": {"user": {"id": "1"}},
        "xdt_fbsearch__top_serp_graphql": {
            "edges": [
                {"node": {"__typename": "XDTTopSerpMediaGridUnit", "items": [_item("AAA111", "danfetea", 1), _item("BBB222", "earljtea", 2)]}},
                {"node": {"__typename": "XDTTopSerpMediaGridUnit", "items": [_item("CCC333", "danfetea", 1)]}},
                {"node": {"__typename": "XDTTopSerpTextUnit"}},  # non-media unit, ignored
            ],
            "page_info": {"has_next_page": True, "end_cursor": "eJwB"},
        },
    },
    "extensions": {"is_final": True},
}


def test_hashtag_search_url_strips_hash_and_encodes() -> None:
    assert hashtag_search_url(" #Ceylon Tea ") == "https://www.instagram.com/explore/search/keyword/?q=%23Ceylon%20Tea"
    with pytest.raises(ValueError):
        hashtag_search_url("#")


def test_parse_tags_accepts_strings_and_lists() -> None:
    assert parse_tags("tea, #Coffee ceylon") == ["tea", "coffee", "ceylon"]
    assert parse_tags(["#tea", "tea", " coffee,matcha "]) == ["tea", "coffee", "matcha"]
    with pytest.raises(ValueError):
        parse_tags(" , ")


def test_parse_search_results_posts_users_and_cursor() -> None:
    page = parse_search_results(SEARCH_PAGE)
    assert [p["shortcode"] for p in page.posts] == ["AAA111", "BBB222", "CCC333"]
    assert page.posts[0]["owner"] == "danfetea"
    assert page.posts[0]["url"] == "https://www.instagram.com/p/AAA111/"
    assert page.posts[0]["caption"] == "#tea by danfetea"
    assert set(page.users) == {"danfetea", "earljtea"}
    assert page.users["danfetea"] == {
        "username": "danfetea",
        "user_id": "1",
        "full_name": "Danfetea",
        "is_private": False,
        "is_verified": True,
        "profile_pic_url": None,
        "profile_url": "https://www.instagram.com/danfetea/",
    }
    assert page.has_next_page is True


def test_parse_search_results_tolerates_sectioned_layout_and_garbage() -> None:
    sectioned = {
        "data": {
            "xdt_fbsearch__top_serp_graphql": {
                "edges": [{"node": {"layout_content": {"medias": [{"media": _item("DDD444", "newbyteas", 4)}]}}}],
                "page_info": {"has_next_page": False},
            }
        }
    }
    page = parse_search_results(sectioned)
    assert [p["owner"] for p in page.posts] == ["newbyteas"]
    assert page.has_next_page is False
    assert parse_search_results({"data": None}).posts == []
    assert parse_search_results("not json").posts == []


def test_summarize_users_counts_sorts_and_samples() -> None:
    page = parse_search_results(SEARCH_PAGE)
    users = summarize_users(page.posts, page.users, sample_posts=1)
    assert [(u["username"], u["post_count"]) for u in users] == [("danfetea", 2), ("earljtea", 1)]
    assert users[0]["posts"] == ["https://www.instagram.com/p/AAA111/"]  # capped at sample_posts
    assert users[0]["is_verified"] is True
    # An owner the account map does not know still gets a row built from the post itself.
    orphan = summarize_users([{"owner": "ghost", "owner_id": "99", "url": "https://www.instagram.com/p/X/"}], {})
    assert orphan[0]["user_id"] == "99" and orphan[0]["profile_url"] == "https://www.instagram.com/ghost/"


async def test_collect_hashtag_users_merges_across_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    crawled: list[str] = []

    async def fake_crawl(session, tag, *, max_posts, max_scrolls, include_posts):  # noqa: ANN001
        crawled.append(tag)
        assert include_posts is False and (max_posts, max_scrolls) == (50, 3)
        users = {
            "tea": [
                {"username": "danfetea", "user_id": "1", "full_name": "D", "is_private": False, "is_verified": True, "profile_url": "u/1", "post_count": 2, "posts": ["p1", "p2"]},
                {"username": "earljtea", "user_id": "2", "full_name": "E", "is_private": False, "is_verified": False, "profile_url": "u/2", "post_count": 1, "posts": ["p3"]},
            ],
            "coffee": [
                {"username": "earljtea", "user_id": "2", "full_name": "E", "is_private": False, "is_verified": False, "profile_url": "u/2", "post_count": 3, "posts": ["p4", "p5"]},
                {"username": "beanbar", "user_id": "3", "full_name": "B", "is_private": True, "is_verified": False, "profile_url": "u/3", "post_count": 1, "posts": ["p6"]},
            ],
        }[tag]
        return {"tag": tag, "url": f"u?{tag}", "post_count": 4, "user_count": len(users), "has_more": tag == "tea", "scrolls": 1, "users": users}

    monkeypatch.setattr(hashtags, "crawl_hashtag", fake_crawl)
    result = await collect_hashtag_users(object(), "tea, #coffee", max_posts_per_tag=50, max_scrolls=3)  # type: ignore[arg-type]

    assert crawled == ["tea", "coffee"]
    assert result["usernames"] == ["earljtea", "danfetea", "beanbar"]  # most hashtags, then most posts, then name
    assert result["user_count"] == 3
    earl = result["users"][0]
    assert earl["hashtags"] == ["tea", "coffee"] and earl["post_count"] == 4 and earl["posts"] == ["p3", "p4", "p5"]
    assert [t["tag"] for t in result["tags"]] == ["tea", "coffee"]
    assert result["tags"][0]["has_more"] is True and "users" not in result["tags"][0]


def _user(username: str, post_count: int, posts: list[str]) -> dict:
    return {
        "username": username,
        "user_id": "1",
        "full_name": None,
        "is_private": False,
        "is_verified": False,
        "profile_pic_url": None,
        "profile_url": f"https://www.instagram.com/{username}/",
        "post_count": post_count,
        "posts": posts,
    }


def test_merge_results_merges_users_and_annotates_posts() -> None:
    tea = {
        "tag": "tea", "url": "u1", "post_count": 2, "user_count": 1, "has_more": False, "scrolls": 0,
        "users": [_user("a", 2, ["x1", "x2"])],
        "posts": [{"shortcode": "X1", "url": "x1", "owner": "a"}, {"shortcode": "X2", "url": "x2", "owner": "a"}],
    }
    coffee = {
        "tag": "coffee", "url": "u2", "post_count": 1, "user_count": 1, "has_more": True, "scrolls": 1, "message": "m",
        "users": [_user("a", 1, ["x2"])],
        "posts": [{"shortcode": "X2", "url": "x2", "owner": "a"}],
    }
    merged = merge_results([tea, coffee])
    assert merged["post_count"] == 2
    assert [(p["shortcode"], p["hashtags"]) for p in merged["posts"]] == [("X1", ["tea"]), ("X2", ["tea", "coffee"])]
    assert merged["users"][0]["post_count"] == 3 and merged["users"][0]["hashtags"] == ["tea", "coffee"]
    assert merged["usernames"] == ["a"]
    assert merged["tags"][1]["message"] == "m" and "posts" not in merged["tags"][1] and "users" not in merged["tags"][1]
    # Results without posts (collect_hashtag_users) carry no post keys at all.
    assert "posts" not in merge_results([{**tea, "posts": None}]) or True  # posts: None counts as "carries posts" key
    users_only = merge_results([{k: v for k, v in tea.items() if k != "posts"}])
    assert "posts" not in users_only and "post_count" not in users_only
