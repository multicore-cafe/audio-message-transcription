"""Command line: `telegram-mcp [serve|login]` (also `python -m transcription_bot.mcp_server`)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from dotenv import find_dotenv, load_dotenv

from ..__main__ import setup_logging
from ..app import EXIT_CONFIG, EXIT_FAILURE, arm_hard_exit
from ..config import ConfigError
from .app import login, serve
from .config import DISABLED_MESSAGE, McpSettings, mcp_enabled

log = logging.getLogger("transcription_bot.mcp_server")


async def _serve(settings: McpSettings) -> int:
    code = EXIT_FAILURE
    try:
        code = await serve(settings)
    finally:
        arm_hard_exit(code)
    return code


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="telegram-mcp", description="Read-only Telegram MCP server")
    parser.add_argument("command", nargs="?", default="serve", choices=["serve", "login"],
                        help="run the MCP server (default) or log in to Telegram interactively")
    args = parser.parse_args(argv)

    load_dotenv(find_dotenv(usecwd=True))
    setup_logging()
    code = EXIT_CONFIG
    if not mcp_enabled():
        # The service is off unless .env says TELEGRAM_MCP_ENABLED=1. Covers `serve` and `login`,
        # including `docker compose run mcp ...`, which ignores the compose `scale` setting.
        log.error(DISABLED_MESSAGE)
    else:
        try:
            settings = McpSettings.from_env()
            if args.command == "login":
                code = asyncio.run(login(settings.telegram))
            else:
                code = asyncio.run(_serve(settings))
        except ConfigError as exc:
            log.error("configuration error: %s", exc)
            code = EXIT_CONFIG
        except KeyboardInterrupt:
            code = EXIT_FAILURE
        except Exception:
            log.exception("MCP server crashed")
            code = EXIT_FAILURE
    logging.shutdown()
    sys.stdout.flush()
    os._exit(code)


if __name__ == "__main__":
    main()
