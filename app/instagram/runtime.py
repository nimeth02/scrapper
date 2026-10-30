"""One browser per server process, started on first use and handed to one tool call at a time."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.instagram.browser import BrowserSession
from app.instagram.settings import InstagramSettings


class SharedSession:
    def __init__(self, settings: InstagramSettings, *, headless: bool) -> None:
        self.settings = settings
        self.headless = headless
        self._session: BrowserSession | None = None
        self._lock = asyncio.Lock()

    @property
    def is_started(self) -> bool:
        return self._session is not None and self._session.started

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[BrowserSession]:
        """Tools share a single tab, so they run one at a time."""
        async with self._lock:
            if self._session is None:
                self._session = BrowserSession(
                    self.settings, headless=self.headless, storage_state=self.settings.session_file
                )
            elif self._session.started and self._session.storage_state_changed():
                # The other server logged in (or out) since this context was built.
                await self._session.reload_storage_state()
            yield self._session

    async def close(self) -> None:
        async with self._lock:
            if self._session is not None:
                await self._session.close()
                self._session = None
