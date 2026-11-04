"""The MCP servers register the expected tools and refuse to scrape without a saved session.

Nothing here launches a browser.
"""

from pathlib import Path

import pytest

from app.servers import instagram_login, instagram_scraper


async def test_login_server_tools() -> None:
    names = {t.name for t in await instagram_login.mcp.list_tools()}
    assert names == {"login", "submit_two_factor_code", "login_status", "screenshot", "logout", "close_browser"}


async def test_scraper_server_tools() -> None:
    names = {t.name for t in await instagram_scraper.mcp.list_tools()}
    assert names == {
        "get_profile",
        "get_posts",
        "get_post",
        "get_comments",
        "get_likers",
        "get_post_engagement",
        "get_hashtag_engagement",
        "get_hashtag",
        "get_hashtag_users",
        "search",
        "fetch_api",
        "get_page_text",
        "screenshot",
        "session_status",
        "close_browser",
    }


async def test_scraper_refuses_without_session_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(instagram_scraper.settings, "session_file", tmp_path / "missing.json")
    with pytest.raises(instagram_scraper.LoginRequired):
        await instagram_scraper.get_profile("natgeo")
    status = await instagram_scraper.session_status()
    assert status["logged_in"] is False
    assert status["session_file_exists"] is False
    assert status["browser_open"] is False


async def test_login_status_without_session_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(instagram_login.settings, "session_file", tmp_path / "missing.json")
    status = await instagram_login.login_status()
    assert status["logged_in"] is False
    assert status["browser_open"] is False


async def test_login_requires_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(instagram_login.settings, "username", None)
    monkeypatch.setattr(instagram_login.settings, "password", None)
    with pytest.raises(ValueError, match="INSTAGRAM_USERNAME"):
        await instagram_login.login()


def test_mcp_json_points_at_the_servers() -> None:
    import json

    from app.config import PROJECT_ROOT

    config = json.loads((PROJECT_ROOT / ".mcp.json").read_text(encoding="utf-8"))
    servers = config["mcpServers"]
    assert servers["instagram-login"]["args"] == ["-m", "app.servers.instagram_login"]
    assert servers["instagram-scraper"]["args"] == ["-m", "app.servers.instagram_scraper"]
