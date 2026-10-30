"""A lazily started Chromium browser with one tab, reused across calls.

The login server and the scraper server each own one ``BrowserSession``. They talk through the
session file: the login server writes it (``save_storage_state``), the scraper loads it and reloads
it whenever the file changes (``storage_state_changed`` / ``reload_storage_state``).
"""

from __future__ import annotations

import asyncio
import logging
import random
import sys
import time
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from app.instagram.settings import InstagramSettings

log = logging.getLogger(__name__)

INSTAGRAM_ORIGIN = "https://www.instagram.com"


class BrowserSession:
    def __init__(
        self,
        settings: InstagramSettings,
        *,
        headless: bool,
        storage_state: Path | None = None,
    ) -> None:
        self.settings = settings
        self.headless = headless
        self.storage_state = storage_state
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._loaded_state_mtime: float | None = None
        self._last_request = 0.0

    # ---- lifecycle ---------------------------------------------------------
    @property
    def started(self) -> bool:
        return self._context is not None

    async def start(self) -> None:
        if self.started:
            return
        log.info("Launching Chromium (headless=%s)", self.headless)
        self._pw = await async_playwright().start()
        # channel="chromium" is the full browser build (new headless mode) rather than the
        # stripped-down headless shell.
        self._browser = await self._pw.chromium.launch(
            channel="chromium",
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        await self._new_context()

    def _default_user_agent(self) -> str | None:
        """Headless Chromium announces itself as ``HeadlessChrome``; present the matching regular Chrome."""
        if not self.headless or self._browser is None:
            return None
        major = self._browser.version.split(".")[0]
        platform = {
            "win32": "Windows NT 10.0; Win64; x64",
            "darwin": "Macintosh; Intel Mac OS X 10_15_7",
        }.get(sys.platform, "X11; Linux x86_64")
        return f"Mozilla/5.0 ({platform}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"

    async def _new_context(self) -> None:
        assert self._browser is not None
        kwargs: dict = {
            "locale": self.settings.locale,
            "viewport": {"width": 1280, "height": 900},
        }
        user_agent = self.settings.user_agent or self._default_user_agent()
        if user_agent:
            kwargs["user_agent"] = user_agent
        if self.storage_state and self.storage_state.exists():
            kwargs["storage_state"] = str(self.storage_state)
            self._loaded_state_mtime = self.storage_state.stat().st_mtime
            log.info("Loaded session from %s", self.storage_state)
        else:
            self._loaded_state_mtime = None
        self._context = await self._browser.new_context(**kwargs)
        timeout_ms = self.settings.nav_timeout_seconds * 1000
        self._context.set_default_timeout(timeout_ms)
        self._context.set_default_navigation_timeout(timeout_ms)
        self._page = await self._context.new_page()

    def storage_state_changed(self) -> bool:
        """True when the session file on disk differs from the one this context was built from."""
        if not self.storage_state:
            return False
        if not self.storage_state.exists():
            return self._loaded_state_mtime is not None
        return self.storage_state.stat().st_mtime != self._loaded_state_mtime

    async def reload_storage_state(self) -> None:
        """Rebuild the context from the current session file (after a fresh login or a logout)."""
        if self._context is not None:
            await self._context.close()
        self._context = None
        self._page = None
        await self._new_context()

    async def close(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:  # noqa: BLE001 - best effort on shutdown
                    log.debug("ignoring error while closing browser", exc_info=True)
        if self._pw is not None:
            await self._pw.stop()
        self._pw = self._browser = self._context = self._page = None
        self._loaded_state_mtime = None

    # ---- page access -------------------------------------------------------
    async def page(self) -> Page:
        await self.start()
        assert self._context is not None
        if self._page is None or self._page.is_closed():
            self._page = await self._context.new_page()
        return self._page

    async def goto(self, url: str, *, wait_until: str = "domcontentloaded") -> Page:
        page = await self.page()
        await self.throttle()
        await page.goto(url, wait_until=wait_until)  # type: ignore[arg-type]
        return page

    async def throttle(self) -> None:
        """Keep at least ``request_delay_seconds`` (jittered) between two requests to Instagram."""
        delay = self.settings.request_delay_seconds
        if delay > 0:
            wait = self._last_request + delay * random.uniform(0.8, 1.4) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
        self._last_request = time.monotonic()

    # ---- cookies / session file --------------------------------------------
    async def cookies(self) -> dict[str, str]:
        await self.start()
        assert self._context is not None
        return {c["name"]: c["value"] for c in await self._context.cookies(INSTAGRAM_ORIGIN)}

    async def clear_cookies(self) -> None:
        if self._context is not None:
            await self._context.clear_cookies()

    async def save_storage_state(self, path: Path | None = None) -> Path:
        target = path or self.storage_state
        if target is None:
            raise ValueError("no session file configured")
        assert self._context is not None, "browser not started"
        target.parent.mkdir(parents=True, exist_ok=True)
        await self._context.storage_state(path=str(target))
        self._loaded_state_mtime = target.stat().st_mtime
        log.info("Saved session to %s", target)
        return target
