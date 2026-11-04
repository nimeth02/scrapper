"""Instagram login flow: password form, 2FA code, checkpoints, and saving the session."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import asdict, dataclass
from typing import Literal

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from app.instagram.browser import BrowserSession

LOGIN_URL = "https://www.instagram.com/accounts/login/"

LoginState = Literal["logged_in", "two_factor", "challenge", "bad_credentials", "pending"]

_COOKIE_BUTTONS = re.compile(r"^(allow all cookies|only allow essential cookies|decline optional cookies|accept all)$", re.I)
_DISMISS_BUTTONS = re.compile(r"^(not now|cancel)$", re.I)
_CONFIRM_BUTTONS = re.compile(r"^(confirm|continue|next|submit)$", re.I)
_LOGIN_ERRORS = re.compile(
    r"password was incorrect|doesn't belong to an account|incorrect username|"
    r"please wait a few minutes|try again later|there was a problem logging you in",
    re.I,
)
_CODE_INPUT = 'input[name="verificationCode"], input[autocomplete="one-time-code"]'
# Instagram has shipped two login forms: the classic one (username / password / <button type=submit>)
# and the 2026 one (email / pass / <input type=submit>). Match both.
_USERNAME_INPUT = 'input[name="username"], input[name="email"], input[autocomplete^="username"]'
_PASSWORD_INPUT = 'input[name="password"], input[name="pass"], input[type="password"]'
_SUBMIT = 'form button[type="submit"]:visible, form input[type="submit"]:visible'
_LOGIN_BUTTON = re.compile(r"^log in$", re.I)


class LoginError(Exception):
    """Instagram refused the login, or it did not finish in time."""


@dataclass
class LoginOutcome:
    status: Literal["logged_in", "already_logged_in", "two_factor_required", "challenge_required"]
    message: str
    username: str | None = None
    user_id: str | None = None
    session_file: str | None = None
    current_url: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


# ---- state helpers -----------------------------------------------------------
async def is_logged_in(session: BrowserSession) -> bool:
    """Instagram sets ``sessionid`` and ``ds_user_id`` only once a login fully succeeded."""
    cookies = await session.cookies()
    return bool(cookies.get("sessionid")) and bool(cookies.get("ds_user_id"))


async def _click_if_present(page: Page, locator: Locator, *, timeout_ms: int = 1500) -> bool:
    try:
        if await locator.count() == 0:
            return False
        await locator.first.click(timeout=timeout_ms)
        return True
    except PlaywrightError:
        return False


async def dismiss_dialogs(page: Page, *, rounds: int = 2) -> None:
    """Close the cookie banner, "Save your login info?" and "Turn on notifications" prompts."""
    for _ in range(rounds):
        clicked = await _click_if_present(page, page.get_by_role("button", name=_COOKIE_BUTTONS))
        clicked |= await _click_if_present(page, page.get_by_role("button", name=_DISMISS_BUTTONS))
        if not clicked:
            return
        await asyncio.sleep(1.0)


async def classify_state(page: Page, session: BrowserSession) -> LoginState:
    url = page.url
    if "/challenge/" in url or "/checkpoint/" in url:
        return "challenge"
    if "two_factor" in url or "auth_platform/codeentry" in url:
        return "two_factor"
    if await page.locator(_CODE_INPUT).count():
        return "two_factor"
    if await is_logged_in(session):
        return "logged_in"
    if await page.get_by_text(_LOGIN_ERRORS).count():
        return "bad_credentials"
    return "pending"


async def wait_for_state(page: Page, session: BrowserSession, timeout_seconds: float) -> LoginState:
    deadline = time.monotonic() + timeout_seconds
    while True:
        state = await classify_state(page, session)
        if state != "pending" or time.monotonic() >= deadline:
            return state
        await asyncio.sleep(1.0)


async def _error_text(page: Page) -> str:
    try:
        return (await page.get_by_text(_LOGIN_ERRORS).first.inner_text(timeout=1000)).strip()
    except PlaywrightError:
        return "unknown error"


# ---- flows -------------------------------------------------------------------
async def _submit_login_form(page: Page) -> None:
    """Click whichever submit control is visible; the 2026 form hides its <input type=submit> and
    renders a role=button "Log in" instead. Enter in the password field always works as a last resort."""
    for locator in (page.locator(_SUBMIT), page.get_by_role("button", name=_LOGIN_BUTTON)):
        if await _click_if_present(page, locator, timeout_ms=5000):
            return
    await page.locator(_PASSWORD_INPUT).first.press("Enter")


async def login(session: BrowserSession, username: str, password: str, *, wait_seconds: float) -> LoginOutcome:
    """Submit the password form. Returns quickly with ``two_factor_required`` / ``challenge_required``
    when Instagram asks for more; finish those with :func:`submit_two_factor_code` or by hand."""
    page = await session.goto(LOGIN_URL)
    await dismiss_dialogs(page)

    on_login_form = "/accounts/login" in page.url
    if await is_logged_in(session) and not on_login_form:
        path = await session.save_storage_state()
        return LoginOutcome(
            status="already_logged_in",
            message="The saved session is still valid.",
            user_id=(await session.cookies()).get("ds_user_id"),
            session_file=str(path),
            current_url=page.url,
        )
    if on_login_form and await session.cookies():
        # Stale cookies (expired server-side). Start clean so the form behaves normally.
        await session.clear_cookies()
        page = await session.goto(LOGIN_URL)
        await dismiss_dialogs(page)

    await page.wait_for_selector(_USERNAME_INPUT, state="visible")
    await page.locator(_USERNAME_INPUT).first.fill(username)
    await page.locator(_PASSWORD_INPUT).first.fill(password)
    await _submit_login_form(page)

    state = await wait_for_state(page, session, wait_seconds)
    return await _finish(session, page, state, username, wait_seconds)


async def submit_two_factor_code(session: BrowserSession, code: str, *, wait_seconds: float) -> LoginOutcome:
    page = await session.page()
    box = page.locator(_CODE_INPUT).first
    if await box.count() == 0:
        raise LoginError(f"No 2FA code field on the current page ({page.url}). Call login first.")
    await box.fill(code.strip().replace(" ", ""))
    if not await _click_if_present(page, page.get_by_role("button", name=_CONFIRM_BUTTONS), timeout_ms=3000):
        await box.press("Enter")

    state = await wait_for_state(page, session, wait_seconds)
    if state == "two_factor":
        raise LoginError(f"Instagram did not accept the code: {await _error_text(page)}")
    return await _finish(session, page, state, None, wait_seconds)


async def _finish(
    session: BrowserSession, page: Page, state: LoginState, username: str | None, wait_seconds: float
) -> LoginOutcome:
    if state == "logged_in":
        await asyncio.sleep(1.5)  # let the post-login redirect ("Save your login info?") settle
        await dismiss_dialogs(page, rounds=3)
        path = await session.save_storage_state()
        return LoginOutcome(
            status="logged_in",
            message="Logged in and session saved.",
            username=username,
            user_id=(await session.cookies()).get("ds_user_id"),
            session_file=str(path),
            current_url=page.url,
        )
    if state == "bad_credentials":
        raise LoginError(f"Instagram rejected the login: {await _error_text(page)}")
    if state == "two_factor":
        return LoginOutcome(
            status="two_factor_required",
            message="Instagram asks for a two-factor code. Call submit_two_factor_code(code), "
            "or type it into the browser window and then call login_status.",
            username=username,
            current_url=page.url,
        )
    if state == "challenge":
        return LoginOutcome(
            status="challenge_required",
            message="Instagram raised a checkpoint (suspicious login / captcha / confirm it's you). "
            "Finish it in the browser window (run with INSTAGRAM_LOGIN_HEADLESS=false), then call login_status.",
            username=username,
            current_url=page.url,
        )
    raise LoginError(
        f"Login did not complete within {wait_seconds:.0f}s; the page is still {page.url}. "
        "Call screenshot to see what Instagram is showing."
    )


async def logout(session: BrowserSession) -> None:
    """Drop the cookies and delete the saved session file."""
    if session.started:
        await session.clear_cookies()
    if session.storage_state and session.storage_state.exists():
        session.storage_state.unlink()
