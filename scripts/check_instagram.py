"""Drive the Instagram library directly (no MCP) to check login and scraping against the live site.

Usage (from the project root)::

    python scripts/check_instagram.py login                 # headed browser, credentials from .env
    python scripts/check_instagram.py status
    python scripts/check_instagram.py profile natgeo
    python scripts/check_instagram.py posts natgeo --limit 5
    python scripts/check_instagram.py post https://www.instagram.com/p/<shortcode>/
    python scripts/check_instagram.py comments <shortcode> --limit 20
    python scripts/check_instagram.py hashtag tea
    python scripts/check_instagram.py search "ceylon tea"
    python scripts/check_instagram.py text natgeo           # visible page text (fallback path)
    python scripts/check_instagram.py logout
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.instagram import BrowserSession, api, auth, get_instagram_settings  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def _print(data: object) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


async def run(args: argparse.Namespace) -> int:
    settings = get_instagram_settings()
    headless = settings.login_headless if args.command == "login" else settings.headless
    if args.headed:
        headless = False
    session = BrowserSession(settings, headless=headless, storage_state=settings.session_file)
    try:
        if args.command == "login":
            if not settings.username or not settings.password:
                print("Set INSTAGRAM_USERNAME and INSTAGRAM_PASSWORD in .env first.", file=sys.stderr)
                return 2
            outcome = await auth.login(session, settings.username, settings.password, wait_seconds=settings.login_wait_seconds)
            _print(outcome.as_dict())
            if outcome.status == "two_factor_required":
                code = input("2FA code: ").strip()
                _print((await auth.submit_two_factor_code(session, code, wait_seconds=settings.login_wait_seconds)).as_dict())
            elif outcome.status == "challenge_required":
                input("Finish the checkpoint in the browser window, then press Enter... ")
                if await auth.is_logged_in(session):
                    await session.save_storage_state()
                    print("Session saved.")
            return 0

        if args.command == "logout":
            await auth.logout(session)
            print("Logged out; session file removed.")
            return 0

        if not settings.session_file.exists():
            print(f"No session file at {settings.session_file}; run `login` first.", file=sys.stderr)
            return 2

        if args.command == "status":
            _print(await api.whoami(session) or {"logged_in": False})
        elif args.command == "profile":
            _print(await api.get_profile(session, args.target))
        elif args.command == "posts":
            _print(await api.get_user_posts(session, args.target, limit=args.limit))
        elif args.command == "post":
            _print(await api.get_post(session, args.target))
        elif args.command == "comments":
            _print(await api.get_comments(session, args.target, limit=args.limit))
        elif args.command == "hashtag":
            _print(await api.get_hashtag(session, args.target))
        elif args.command == "search":
            _print(await api.search(session, args.target))
        elif args.command == "text":
            _print(await api.page_text(session, args.target, max_chars=args.limit * 100))
        return 0
    finally:
        await session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["login", "logout", "status", "profile", "posts", "post", "comments", "hashtag", "search", "text"])
    parser.add_argument("target", nargs="?", help="username / shortcode / URL / hashtag / query")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--headed", action="store_true", help="show the browser window")
    args = parser.parse_args()
    if args.command not in ("login", "logout", "status") and not args.target:
        parser.error(f"{args.command} needs a target")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
