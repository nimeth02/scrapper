"""Smoke-test the configured LLM fallback chain against the real providers.

Usage (from the project root)::

    python scripts/check_llm.py            # plain chat + tool call + structured output
    python scripts/check_llm.py --stream   # also test streaming
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import BaseModel, Field  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.llm import build_llm  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


class Verdict(BaseModel):
    answer: str = Field(description="One-word answer")
    confidence: float = Field(ge=0, le=1)


def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


def main() -> int:
    settings = get_settings()
    llm = build_llm(settings)
    print("Chain:", " -> ".join(llm.model_names))

    t0 = time.perf_counter()
    reply = llm.invoke("Reply with the single word OK.")
    used = reply.response_metadata.get("llm_fallback", {})
    # Gemini 3.x returns a list of content blocks; `.text` flattens it to a string.
    print(f"[invoke] {used.get('name')} answered in {time.perf_counter() - t0:.1f}s: {reply.text!r}")

    t0 = time.perf_counter()
    tool_reply = llm.bind_tools([add]).invoke("Use the add tool to compute 17 + 25.")
    used = tool_reply.response_metadata.get("llm_fallback", {})
    print(f"[tools]  {used.get('name')} in {time.perf_counter() - t0:.1f}s: tool_calls={tool_reply.tool_calls}")

    t0 = time.perf_counter()
    verdict = llm.with_structured_output(Verdict).invoke("Is water wet? Answer yes or no with a confidence.")
    print(f"[struct] in {time.perf_counter() - t0:.1f}s: {verdict!r}")

    if "--stream" in sys.argv:
        print("[stream] ", end="", flush=True)
        for chunk in llm.stream("Count from 1 to 5, separated by spaces."):
            print(chunk.text, end="", flush=True)
        print()

    blocked = llm.policy.health.snapshot()
    print("Cooled-down models:", blocked or "none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
