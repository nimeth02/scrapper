"""The agent's run tree, in LangSmith's terms: every step is a run of type ``chain``, ``llm`` or ``tool``.

One question is a root *chain* run. Each model call is an *llm* run under it. The tool calls of one
model turn are grouped in a ``tools`` *chain* run (as in a LangGraph agent), with one *tool* run per
call. The same run ids go to LangChain's callback system, so with ``LANGSMITH_TRACING`` on LangSmith
records exactly the tree the page and the CLI show.

Events passed to the sink:

``run_start``  ``run_id``, ``parent_id``, ``run_type``, ``name``, ``step``, ``offset`` (seconds since the
               root started) and the run's ``inputs``
``run_end``    the same ids plus ``seconds``, ``error`` and whatever the run produced (``outputs`` for a
               chain; ``model``, ``reasoning``, ``text``, ``tool_calls``, ``usage`` for an llm run;
               ``summary``, ``output``, ``result`` for a tool run)
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from langchain_core.callbacks import AsyncCallbackManager, AsyncCallbackManagerForChainRun
from langchain_core.runnables import RunnableConfig

EventSink = Callable[[dict[str, Any]], None]
RUN_TYPES = ("chain", "llm", "tool")


@dataclass
class Run:
    id: UUID
    run_type: str
    name: str
    parent: Run | None
    step: int | None
    began: float = field(default_factory=time.monotonic)
    # Chain runs are opened here; llm and tool runs are opened by LangChain itself when the model or
    # tool is invoked with :meth:`RunTree.config`.
    manager: AsyncCallbackManagerForChainRun | None = None
    ended: bool = False


class RunTree:
    """Opens and closes runs, emitting an event for each and mirroring chain runs to LangChain."""

    def __init__(self, emit: EventSink, *, tags: list[str] | None = None) -> None:
        self.emit = emit
        self.tags = tags
        self.started = time.monotonic()
        self.open: list[Run] = []

    def _callbacks(self, parent: Run | None) -> AsyncCallbackManager:
        """Callbacks for a run under ``parent``, configured the way a Runnable configures its own, so the
        LangSmith tracer (and any handler LangChain adds from the context) is attached when enabled."""
        inherited = parent.manager.get_child() if parent is not None and parent.manager is not None else None
        return AsyncCallbackManager.configure(inheritable_callbacks=inherited, inheritable_tags=self.tags)

    async def start(
        self,
        run_type: str,
        name: str,
        *,
        parent: Run | None = None,
        step: int | None = None,
        inputs: dict[str, Any] | None = None,
    ) -> Run:
        if run_type not in RUN_TYPES:
            raise ValueError(f"unknown run type {run_type!r}")
        run = Run(uuid4(), run_type, name, parent, step)
        if run_type == "chain":
            run.manager = await self._callbacks(parent).on_chain_start({"name": name}, inputs or {}, run_id=run.id, name=name)
        self.open.append(run)
        self.emit(
            {
                "type": "run_start",
                "run_id": str(run.id),
                "parent_id": str(parent.id) if parent is not None else None,
                "run_type": run_type,
                "name": name,
                "step": step,
                "offset": round(run.began - self.started, 2),
                "inputs": inputs or {},
            }
        )
        return run

    def config(self, run: Run) -> RunnableConfig:
        """The config that makes LangChain record ``run`` (an llm or tool run) under its parent."""
        return {"callbacks": self._callbacks(run.parent), "run_id": run.id}

    async def end(self, run: Run, *, error: BaseException | str | None = None, **fields: Any) -> None:
        if run.ended:
            return
        run.ended = True
        if run in self.open:
            self.open.remove(run)
        message = None if error is None else (error if isinstance(error, str) else f"{type(error).__name__}: {error}")
        if run.manager is not None:
            if error is None:
                await run.manager.on_chain_end(fields.get("outputs") or {})
            else:
                await run.manager.on_chain_error(error if isinstance(error, BaseException) else RuntimeError(message))
        self.emit(
            {
                "type": "run_end",
                "run_id": str(run.id),
                "parent_id": str(run.parent.id) if run.parent is not None else None,
                "run_type": run.run_type,
                "name": run.name,
                "step": run.step,
                "seconds": round(time.monotonic() - run.began, 2),
                "error": message,
                **fields,
            }
        )

    async def abort(self, error: BaseException) -> None:
        """Close every run still open (innermost first), e.g. when the request is cancelled."""
        for run in reversed(list(self.open)):
            await self.end(run, error=error)
