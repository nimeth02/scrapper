"""Ask the agent from the terminal::

    python -m app.agent "who posts under #ceylontea and who comments on their posts?"
    python -m app.agent --steps 4 "accounts under #tea"

Prints the run tree as it grows (chain / llm / tool runs, like LangSmith), then the answer. Needs the saved Instagram session and an LLM
API key in ``.env``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from app.agent.runner import MAX_STEPS, run_agent
from app.instagram import SharedSession, get_instagram_settings


def _args_text(args: dict) -> str:
    return " ".join(f"{k}={','.join(map(str, v)) if isinstance(v, list) else v}" for k, v in (args or {}).items())


def _short(text: str, limit: int = 400) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class TreePrinter:
    """Prints the run tree as it grows: ``[chain]``, ``[llm]`` and ``[tool]`` runs, indented by depth."""

    def __init__(self) -> None:
        self.depth: dict[str, int] = {}
        self.args: dict[str, dict] = {}

    def __call__(self, event: dict) -> None:
        kind = event["type"]
        if kind == "run_start":
            parent = event.get("parent_id")
            self.depth[event["run_id"]] = self.depth.get(parent, -1) + 1 if parent else 0
            pad = "  " * self.depth[event["run_id"]]
            if event["run_type"] == "chain":
                step = f" · step {event['step']}" if event.get("step") else ""
                print(f"{pad}[chain] {event['name']}{step}", flush=True)
            elif event["run_type"] == "tool":
                print(f"{pad}[tool]  {event['name']}({_args_text(event['inputs'])})", flush=True)
        elif kind == "run_end":
            pad = "  " * self.depth.get(event["run_id"], 0)
            mark = "✗" if event.get("error") else "✓"
            if event["run_type"] == "llm":
                usage = event.get("usage") or {}
                meta = [event.get("model") or event["name"], f"step {event['step']}", f"{event['seconds']}s"]
                if usage.get("total") is not None:
                    meta.append(f"{usage['total']} tokens")
                print(f"{pad}[llm]   {mark} {' · '.join(meta)}", flush=True)
                if event.get("error"):
                    print(f"{pad}        {_short(event['error'])}", flush=True)
                if event.get("reasoning"):
                    print(f"{pad}        💭 {_short(event['reasoning'])}", flush=True)
                calls = event.get("tool_calls") or []
                if calls and event.get("text"):
                    print(f"{pad}        · {_short(event['text'])}", flush=True)
                if calls:
                    print(f"{pad}        → calls {', '.join(c['name'] for c in calls)}", flush=True)
            elif event["run_type"] == "tool":
                print(f"{pad}        {mark} {event.get('summary') or event.get('error')} ({event['seconds']}s)", flush=True)
            elif event.get("parent_id") is None:
                tokens = f" · {event['tokens']} tokens" if event.get("tokens") is not None else ""
                print(f"{pad}[chain] {mark} {event['name']} · {event['seconds']}s{tokens}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Ask the Instagram hashtag agent a question")
    parser.add_argument("question", nargs="+", help="the question, in plain words")
    parser.add_argument("--steps", type=int, default=MAX_STEPS, help=f"tool-calling turns allowed (default {MAX_STEPS})")
    parser.add_argument("-v", "--verbose", action="store_true", help="show the scraper's log lines too")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    settings = get_instagram_settings()
    if not settings.session_file.exists():
        print(f"No saved Instagram session at {settings.session_file}. Run `python scripts/check_instagram.py login` first.")
        return 2
    shared = SharedSession(settings, headless=settings.headless)
    question = " ".join(args.question)

    async def go() -> dict:
        try:
            return await run_agent(question, acquire=shared.acquire, on_event=TreePrinter(), max_steps=args.steps)
        finally:
            await shared.close()

    print(f"Q: {question}", flush=True)
    result = asyncio.run(go())
    print()
    print(result["answer"])
    tail = f"[{result['model']} · {result['tool_calls']} tool call(s) in {result['steps']} turn(s) · {result['seconds']}s"
    print(tail + (f" · stopped: {result['stopped']}]" if result["stopped"] else "]"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
