"""Command line: `python -m transcription_bot [run|login]`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from dotenv import find_dotenv, load_dotenv

from .app import EXIT_CONFIG, EXIT_FAILURE, arm_hard_exit, login, run_bot
from .config import BotSettings, ConfigError, TelegramSettings

log = logging.getLogger("transcription_bot")


def setup_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if level != "DEBUG":
        logging.getLogger("telethon").setLevel(logging.WARNING)


async def _run(settings: BotSettings) -> int:
    code = EXIT_FAILURE
    try:
        code = await run_bot(settings)
    finally:
        arm_hard_exit(code)
    return code


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="transcription_bot", description=__doc__)
    parser.add_argument("command", nargs="?", default="run", choices=["run", "login"],
                        help="run the bot (default) or log in to Telegram interactively")
    args = parser.parse_args(argv)

    load_dotenv(find_dotenv(usecwd=True))
    setup_logging()
    try:
        if args.command == "login":
            code = asyncio.run(login(TelegramSettings.from_env()))
        else:
            code = asyncio.run(_run(BotSettings.from_env()))
    except ConfigError as exc:
        log.error("configuration error: %s", exc)
        code = EXIT_CONFIG
    except KeyboardInterrupt:
        code = EXIT_FAILURE
    except Exception:
        log.exception("bot crashed")
        code = EXIT_FAILURE
    logging.shutdown()
    sys.stdout.flush()
    # Hard exit: lingering threads or tasks must not keep a dead bot alive.
    os._exit(code)


if __name__ == "__main__":
    main()
