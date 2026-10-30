"""Instagram on Playwright: a shared browser session, the login flow and web-API scraping.

The MCP servers in ``app.servers`` are thin wrappers over this package::

    from app.instagram import BrowserSession, api, auth, get_instagram_settings

    settings = get_instagram_settings()
    session = BrowserSession(settings, headless=False, storage_state=settings.session_file)
    await auth.login(session, "user", "pass", wait_seconds=45)
    profile = await api.get_profile(session, "natgeo")
"""

from app.instagram import api, auth
from app.instagram.api import InstagramAPIError, LoginRequired, RateLimited
from app.instagram.auth import LoginError, LoginOutcome
from app.instagram.browser import BrowserSession
from app.instagram.runtime import SharedSession
from app.instagram.settings import InstagramSettings, get_instagram_settings

__all__ = [
    "BrowserSession",
    "InstagramAPIError",
    "InstagramSettings",
    "LoginError",
    "LoginOutcome",
    "LoginRequired",
    "RateLimited",
    "SharedSession",
    "api",
    "auth",
    "get_instagram_settings",
]
