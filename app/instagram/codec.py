"""Pure helpers: post shortcode <-> media id, and pulling usernames / shortcodes out of URLs."""

from __future__ import annotations

import re

_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}

_POST_URL = re.compile(r"instagram\.com/(?:[^/?#]+/)?(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")
_PROFILE_URL = re.compile(r"instagram\.com/([A-Za-z0-9._]+)/?")
_USERNAME = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_RESERVED = {"p", "reel", "reels", "tv", "explore", "accounts", "stories", "direct", "api"}


def shortcode_from_url(value: str) -> str:
    """``https://www.instagram.com/p/CxYz1/`` or ``p/CxYz1`` or ``CxYz1`` -> ``CxYz1``."""
    value = value.strip()
    match = _POST_URL.search(value)
    code = match.group(1) if match else value.strip("/").rsplit("/", 1)[-1]
    if not code or any(c not in _INDEX for c in code):
        raise ValueError(f"not an Instagram post URL or shortcode: {value!r}")
    return code


def username_from_url(value: str) -> str:
    """``https://www.instagram.com/natgeo/`` or ``@natgeo`` or ``natgeo`` -> ``natgeo``."""
    value = value.strip()
    match = _PROFILE_URL.search(value)
    name = match.group(1) if match else value.lstrip("@").strip("/")
    if name in _RESERVED or not _USERNAME.match(name):
        raise ValueError(f"not an Instagram username or profile URL: {value!r}")
    return name


def shortcode_to_media_id(shortcode: str) -> int:
    """Decode a post shortcode (base-64 with Instagram's alphabet) into its numeric media id."""
    n = 0
    for ch in shortcode_from_url(shortcode):
        n = n * 64 + _INDEX[ch]
    return n


def media_id_to_shortcode(media_id: int | str) -> str:
    """Inverse of :func:`shortcode_to_media_id`. Accepts ``"123_456"`` (``pk_userid``) too."""
    n = int(str(media_id).split("_", 1)[0])
    if n < 0:
        raise ValueError("media id must be non-negative")
    if n == 0:
        return _ALPHABET[0]
    out: list[str] = []
    while n:
        n, rem = divmod(n, 64)
        out.append(_ALPHABET[rem])
    return "".join(reversed(out))
