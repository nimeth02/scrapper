"""MCP server: log in to Instagram with Playwright and save the session for the scraper server.

Run (stdio)::

    python -m app.servers.instagram_login

The browser is headed by default (``INSTAGRAM_LOGIN_HEADLESS=false``) so 2FA prompts and
"suspicious login" checkpoints can be finished by hand in the window.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP, Image

from app.instagram import SharedSession, api, auth, get_instagram_settings
from app.instagram.api import InstagramAPIError, LoginRequired
from app.servers import _logging

_logging.configure()

settings = get_instagram_settings()
shared = SharedSession(settings, headless=settings.login_headless)

mcp = FastMCP(
    "instagram-login",
    instructions=(
        "Logs in to Instagram in a real Chromium browser and saves the session file that the "
        "instagram-scraper server reads. Typical flow: login -> (submit_two_factor_code) -> login_status. "
        "Credentials come from INSTAGRAM_USERNAME / INSTAGRAM_PASSWORD in .env unless passed explicitly."
    ),
)


async def _identity(session, result: dict) -> dict:
    """Attach username / user_id of the logged-in account when we can look them up."""
    try:
        result.update(await api.whoami(session) or {})
    except InstagramAPIError as exc:
        result["whoami_error"] = str(exc)
    return result


@mcp.tool()
async def login(username: str | None = None, password: str | None = None, wait_seconds: float | None = None) -> dict:
    """Log in to Instagram and save the browser session for the scraper server.

    Credentials default to INSTAGRAM_USERNAME / INSTAGRAM_PASSWORD from .env. Returns a status of
    "logged_in", "already_logged_in", "two_factor_required" (then call submit_two_factor_code) or
    "challenge_required" (finish the checkpoint in the browser window, then call login_status).
    """
    user = username or settings.username
    pwd = password or settings.password
    if not user or not pwd:
        raise ValueError("No credentials: pass username/password or set INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD in .env")
    async with shared.acquire() as session:
        outcome = await auth.login(session, user, pwd, wait_seconds=wait_seconds or settings.login_wait_seconds)
        result = outcome.as_dict()
        if outcome.status in ("logged_in", "already_logged_in"):
            result = await _identity(session, result)
        return result


@mcp.tool()
async def submit_two_factor_code(code: str, wait_seconds: float | None = None) -> dict:
    """Enter the two-factor code Instagram asked for after login(), then save the session."""
    async with shared.acquire() as session:
        outcome = await auth.submit_two_factor_code(session, code, wait_seconds=wait_seconds or settings.login_wait_seconds)
        result = outcome.as_dict()
        if outcome.status == "logged_in":
            result = await _identity(session, result)
        return result


@mcp.tool()
async def login_status() -> dict:
    """Is there a working Instagram session? Checks the saved session file (and the open browser, if any).

    Also refreshes the saved session file when the browser turns out to be logged in, so call this
    after finishing a 2FA prompt or checkpoint by hand in the browser window.
    """
    info: dict = {
        "session_file": str(settings.session_file),
        "session_file_exists": settings.session_file.exists(),
        "browser_open": shared.is_started,
        "logged_in": False,
    }
    if not info["browser_open"] and not info["session_file_exists"]:
        info["message"] = "No session yet. Call login."
        return info
    async with shared.acquire() as session:
        try:
            identity = await api.whoami(session)
        except LoginRequired as exc:
            info["message"] = f"Session is not valid ({exc}). Call login."
            return info
        if not identity:
            info["message"] = "Browser is not logged in. Call login."
            return info
        await session.save_storage_state()
        info.update(identity, logged_in=True, session_file_exists=True)
        return info


@mcp.tool()
async def screenshot() -> Image:
    """Screenshot of the login browser's current page, to see a 2FA prompt, checkpoint or error."""
    async with shared.acquire() as session:
        return Image(data=await api.screenshot(session), format="png")


@mcp.tool()
async def logout() -> dict:
    """Clear the cookies, delete the saved session file and close the browser."""
    async with shared.acquire() as session:
        await auth.logout(session)
    await shared.close()
    return {"logged_in": False, "session_file": str(settings.session_file), "session_file_exists": settings.session_file.exists()}


@mcp.tool()
async def close_browser() -> dict:
    """Close the login browser window. The saved session file is kept."""
    await shared.close()
    return {"browser_open": False}


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
