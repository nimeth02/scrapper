import pytest

from app.instagram.codec import (
    media_id_to_shortcode,
    shortcode_from_url,
    shortcode_to_media_id,
    username_from_url,
)


@pytest.mark.parametrize(("code", "media_id"), [("A", 0), ("B", 1), ("BA", 64), ("-", 62), ("_", 63), ("BAA", 4096)])
def test_shortcode_media_id_known_values(code: str, media_id: int) -> None:
    assert shortcode_to_media_id(code) == media_id
    assert media_id_to_shortcode(media_id) == code


@pytest.mark.parametrize("media_id", [1, 12345, 2 ** 40 + 7, 3_200_000_000_000_000_000])
def test_shortcode_round_trip(media_id: int) -> None:
    assert shortcode_to_media_id(media_id_to_shortcode(media_id)) == media_id


def test_media_id_with_user_suffix() -> None:
    assert media_id_to_shortcode("64_123456") == "BA"


@pytest.mark.parametrize(
    "value",
    [
        "CxYz1-_Ab",
        "https://www.instagram.com/p/CxYz1-_Ab/",
        "https://www.instagram.com/reel/CxYz1-_Ab/?utm_source=ig_web",
        "https://www.instagram.com/natgeo/p/CxYz1-_Ab/",
        "instagram.com/tv/CxYz1-_Ab",
        "/p/CxYz1-_Ab/",
    ],
)
def test_shortcode_from_url(value: str) -> None:
    assert shortcode_from_url(value) == "CxYz1-_Ab"


def test_shortcode_from_url_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        shortcode_from_url("not a shortcode!")


@pytest.mark.parametrize(
    "value",
    ["natgeo", "@natgeo", "https://www.instagram.com/natgeo/", "https://instagram.com/natgeo?hl=en", "natgeo/"],
)
def test_username_from_url(value: str) -> None:
    assert username_from_url(value) == "natgeo"


@pytest.mark.parametrize("value", ["https://www.instagram.com/p/CxYz1/", "has space", "", "x" * 31])
def test_username_from_url_rejects_non_usernames(value: str) -> None:
    with pytest.raises(ValueError):
        username_from_url(value)
