"""Running the MCP service and its separate interactive login."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sqlite3

import uvicorn
from telethon import errors

from ..app import EXIT_CONFIG, EXIT_FAILURE, EXIT_OK, interactive_login, report_file_error
from ..config import TelegramSettings
from ..errors import FatalError
from ..telegram import STARTUP_TIMEOUT, check_connection, connect_authorized, create_client, session_lock
from ..watchdog import Watchdog
from .config import DISABLED_MESSAGE, McpSettings, mcp_enabled
from .export import Exporter
from .guard import LOGIN_REQUESTS, READ_ONLY_REQUESTS, ReadOnlyTelegramClient
from .reader import TelegramReader
from .server import build_server, http_app

log = logging.getLogger(__name__)

LOGIN_COMMAND = "docker compose run --rm mcp telegram-mcp login"
LOCAL_LOGIN_COMMAND = "uv run telegram-mcp login"


def login_hint(settings: TelegramSettings) -> str:
    return (
        f"Telegram session {settings.session_path} is not authorized. Log in interactively with "
        f"`{LOGIN_COMMAND}` (locally: `{LOCAL_LOGIN_COMMAND}`)."
    )


def create_readonly_client(settings: TelegramSettings, *, allow_login: bool = False) -> ReadOnlyTelegramClient:
    allowed = READ_ONLY_REQUESTS | LOGIN_REQUESTS if allow_login else READ_ONLY_REQUESTS
    return create_client(settings, receive_updates=False, client_class=ReadOnlyTelegramClient, allowed=allowed)


class _Server(uvicorn.Server):
    """uvicorn server whose shutdown is driven by us (no signal capture, no re-raised signals)."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


async def serve(settings: McpSettings) -> int:
    if not mcp_enabled():
        log.error(DISABLED_MESSAGE)
        return EXIT_CONFIG
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(session_lock(settings.telegram))
            client = create_readonly_client(settings.telegram)
        except (FatalError, OSError, sqlite3.Error) as exc:
            return report_file_error(exc, settings.telegram.session_path.parent)
        return await _serve(settings, client)


async def _serve(settings: McpSettings, client: ReadOnlyTelegramClient) -> int:
    client.flood_sleep_threshold = settings.flood_sleep_threshold
    try:
        await asyncio.wait_for(connect_authorized(client, login_hint(settings.telegram)), STARTUP_TIMEOUT)
    except FatalError as exc:
        log.error("%s", exc)
        await client.disconnect()
        return EXIT_CONFIG
    except (OSError, TimeoutError, errors.RPCError) as exc:
        log.error("cannot connect to Telegram: %r", exc)
        await client.disconnect()
        return EXIT_FAILURE

    reader = TelegramReader(client, max_response_chars=settings.max_response_chars,
                            dialog_cache_seconds=settings.dialog_cache_seconds)
    exporter = Exporter(reader, settings.export_dir, settings.export_host_dir,
                        max_messages=settings.export_max_messages, media_max_bytes=settings.media_max_bytes,
                        media_max_total_bytes=settings.media_max_total_bytes)
    try:
        me = await asyncio.wait_for(reader.me(), STARTUP_TIMEOUT)
    except (OSError, TimeoutError, errors.RPCError) as exc:
        log.error("cannot connect to Telegram: %r", exc)
        await client.disconnect()
        return EXIT_FAILURE
    log.info("logged in as %s (id %s); read-only MCP server on http://%s:%d/mcp",
             me.username or me.first_name, me.id, settings.host, settings.port)

    # McpSettings.from_env refuses to start without a token, so auth_token is always set here.
    app = http_app(build_server(reader, exporter), allowed_hosts=settings.allowed_hosts,
                   allowed_origins=settings.allowed_origins, auth_token=settings.auth_token)
    server = _Server(uvicorn.Config(app, host=settings.host, port=settings.port, log_level="info",
                                    access_log=False, lifespan="on"))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    watchdog = Watchdog(lambda: check_connection(client))

    tasks = {
        "server": asyncio.ensure_future(server.serve()),
        "stop": asyncio.ensure_future(stop.wait()),
        "watchdog": asyncio.ensure_future(watchdog.run()),
        "disconnected": asyncio.ensure_future(client.disconnected),
    }
    code = EXIT_FAILURE
    try:
        await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_COMPLETED)
        if tasks["stop"].done():
            log.info("stop requested")
            code = EXIT_OK
        elif tasks["server"].done():
            log.error("HTTP server stopped unexpectedly (is port %d free?)", settings.port)
        elif tasks["watchdog"].done():
            log.error("exiting: watchdog detected a dead Telegram connection")
        else:
            log.error("exiting: Telegram connection lost for good")
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(tasks["server"], 10)
        for task in tasks.values():
            task.cancel()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
        try:
            await asyncio.wait_for(client.disconnect(), 10)
        except Exception as exc:
            log.warning("disconnect failed: %r", exc)
    return code


async def login(settings: TelegramSettings) -> int:
    """Interactive login that creates the MCP server's own session file."""
    if not mcp_enabled():
        log.error(DISABLED_MESSAGE)
        return EXIT_CONFIG
    return await interactive_login(settings, lambda: create_readonly_client(settings, allow_login=True))
