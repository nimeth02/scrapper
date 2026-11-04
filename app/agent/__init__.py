"""An LLM agent over the Instagram scraper: ask in plain words, it picks the lookups and answers.

::

    from app.agent import run_agent
    from app.instagram import SharedSession, get_instagram_settings

    shared = SharedSession(get_instagram_settings(), headless=True)
    result = await run_agent("who posts under #ceylontea?", acquire=shared.acquire)
    print(result["answer"])

The model comes from :func:`app.llm.get_llm` (Gemini / Groq fallback chain); the tools wrap
:mod:`app.instagram`. ``python -m app.agent "question"`` runs it from the terminal, and the web UI's
"Ask the agent" panel streams the same loop into the page. :func:`run_mapper` reuses the loop to map
related hashtags into the graph database (the web UI's "Hashtag map" tab).
"""

from app.agent.runner import MAX_STEPS, SYSTEM_PROMPT, describe, history_messages, run_agent, split_reply
from app.agent.mapper import run_mapper
from app.agent.tools import build_tools, describe_result

__all__ = [
    "MAX_STEPS",
    "SYSTEM_PROMPT",
    "build_tools",
    "describe",
    "describe_result",
    "history_messages",
    "run_agent",
    "run_mapper",
    "split_reply",
]
