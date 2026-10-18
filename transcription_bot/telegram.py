"""Telegram client setup (shared with other services) and the Telethon adapter for the processor."""

from __future__ import annotations

import asyncio
import fcntl
import logging
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

from telethon import TelegramClient, errors, events, functions, types, utils

from .config import TelegramSettings
from .errors import CaptionTooLong, FatalError, FloodWait, PermanentError, TransientError
from .processor import STATUS_MARKERS, VoiceMessage

log = logging.getLogger(__name__)

CONNECTION_RETRIES = 5
RETRY_DELAY = 5
REQUEST_TIMEOUT = 30
STARTUP_TIMEOUT = 60
SWEEP_PAGE_SIZE = 100
SWEEP_MAX_PAGES = 20


def create_client(
    settings: TelegramSettings,
    *,
    catch_up: bool = False,
    receive_updates: bool = True,
    client_class: type[TelegramClient] = TelegramClient,
    **extra,
) -> TelegramClient:
    """Client with an SQLite session file at `settings.session_path`; `extra` goes to `client_class`."""
    settings.session_path.parent.mkdir(parents=True, exist_ok=True)
    return client_class(
        str(settings.session_path),
        settings.api_id,
        settings.api_hash,
        timeout=REQUEST_TIMEOUT,
        connection_retries=CONNECTION_RETRIES,
        retry_delay=RETRY_DELAY,
        auto_reconnect=True,
        catch_up=catch_up,
        receive_updates=receive_updates,
        **extra,
    )


def login_hint(settings: TelegramSettings) -> str:
    return (
        f"Telegram session {settings.session_path} is not authorized. Log in interactively with "
        "`docker compose run --rm bot transcription-bot login` (locally: `uv run transcription-bot login`)."
    )


async def connect_authorized(client: TelegramClient, hint: str) -> None:
    """Connect and make sure the session is logged in; raises FatalError with `hint` otherwise.

    Other errors (network, Telegram server errors, flood waits) propagate: they are not a reason to
    log in again. (`client.is_user_authorized()` would report them as "not logged in".)
    """
    if client.session.auth_key is None:
        raise FatalError(hint)  # no session yet: no need to contact Telegram at all
    await client.connect()
    try:
        await client(functions.updates.GetStateRequest())
    except (errors.UnauthorizedError, errors.AuthKeyError) as exc:
        raise FatalError(f"{hint} ({exc.__class__.__name__})") from exc
    client._authorized = True  # what is_user_authorized() would have cached


@contextmanager
def session_lock(settings: TelegramSettings) -> Iterator[None]:
    """Hold an exclusive lock next to the session file, so two processes never share one session.

    Telegram may invalidate a session (AUTH_KEY_DUPLICATED) used by two connections at once.
    Raises FatalError if another process holds the lock, OSError if the file cannot be created.
    """
    path = settings.session_path.with_name(settings.session_path.name + ".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FatalError(
                f"another instance is running with the session {settings.session_path} (lock file {path}); "
                "stop it first"
            ) from None
        yield


async def check_connection(client: TelegramClient) -> None:
    """Cheap request used by the watchdog."""
    if not client.is_connected():
        raise ConnectionError("Telegram client is disconnected")
    await client(functions.updates.GetStateRequest())


@contextmanager
def translate_errors() -> Iterator[None]:
    """Map Telethon errors onto the processor's error kinds."""
    try:
        yield
    except errors.FloodError as exc:
        raise FloodWait(getattr(exc, "seconds", None) or 30) from exc
    except (errors.MediaCaptionTooLongError, errors.MessageTooLongError) as exc:
        raise CaptionTooLong() from exc
    except (errors.UnauthorizedError, errors.AuthKeyError) as exc:
        raise FatalError(f"Telegram session is no longer valid: {exc}") from exc
    except (errors.ServerError, errors.TimedOutError, errors.RpcCallFailError) as exc:
        raise TransientError(f"Telegram server error: {exc}") from exc
    except errors.RPCError as exc:
        # 400/403/404: message deleted, editing forbidden, chat unavailable and so on.
        raise PermanentError(f"Telegram error {exc.message}") from exc
    except ValueError as exc:
        # Telethon could not resolve the chat from the session's entity cache.
        raise PermanentError(f"Telegram chat not found: {exc}") from exc
    except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
        raise TransientError(f"Telegram connection error: {exc!r}") from exc


def is_owner_voice(message: types.Message) -> bool:
    """A voice note recorded by the account owner (outgoing from any device, not forwarded)."""
    return bool(message.out and message.voice and not message.fwd_from)


def owner_voice_event() -> events.NewMessage:
    """New voice messages sent by the account owner (from any of their devices)."""
    return events.NewMessage(outgoing=True, func=lambda event: is_owner_voice(event.message))


async def find_untranscribed(client: TelegramClient, since: datetime) -> list[tuple[int, int]]:
    """(chat_id, message_id) of the owner's voice notes since `since` that have no transcription yet.

    One `messages.searchGlobal` with the voice filter covers private chats, groups, supergroups and
    channels (not secret chats). It cannot filter by sender, so outgoing messages are picked here.
    """
    found: list[tuple[int, int]] = []
    offset_rate, offset_peer, offset_id = 0, types.InputPeerEmpty(), 0
    for _ in range(SWEEP_MAX_PAGES):
        result = await client(functions.messages.SearchGlobalRequest(
            q="", filter=types.InputMessagesFilterVoice(), min_date=since, max_date=None,
            offset_rate=offset_rate, offset_peer=offset_peer, offset_id=offset_id, limit=SWEEP_PAGE_SIZE,
        ))
        messages = [m for m in result.messages if isinstance(m, types.Message)]
        for message in messages:
            if is_owner_voice(message) and (not message.message or message.message in STATUS_MARKERS):
                found.append((utils.get_peer_id(message.peer_id), message.id))
        if len(result.messages) < SWEEP_PAGE_SIZE or not messages:
            return found
        last = messages[-1]
        entities = {utils.get_peer_id(x): x for x in [*result.users, *result.chats]}
        peer = entities.get(utils.get_peer_id(last.peer_id))
        if peer is None:
            log.warning("startup sweep: cannot page past chat %s; older voice messages were not checked", last.peer_id)
            return found
        offset_rate = getattr(result, "next_rate", None) or int(last.date.timestamp())
        offset_peer, offset_id = utils.get_input_peer(peer), last.id
    log.warning("startup sweep: stopped after %d pages; older voice messages were not checked", SWEEP_MAX_PAGES)
    return found


class TelethonOps:
    def __init__(self, client: TelegramClient):
        self.client = client

    async def fetch(self, chat_id: int, message_id: int) -> VoiceMessage | None:
        with translate_errors():
            message = await self.client.get_messages(chat_id, ids=message_id)
        if message is None or isinstance(message, types.MessageEmpty):
            return None
        return VoiceMessage(
            chat_id=chat_id,
            message_id=message_id,
            caption=message.message or "",
            size=message.file.size if message.file else None,
            is_voice=is_owner_voice(message),
            raw=message,
        )

    async def download(self, message: VoiceMessage) -> bytes:
        with translate_errors():
            data = await self.client.download_media(message.raw, file=bytes)
        if not data:
            raise PermanentError("voice message has no downloadable audio")
        return data

    async def set_caption(self, chat_id: int, message_id: int, text: str, *, italic: bool = False) -> None:
        entities = [types.MessageEntityItalic(0, _utf16_len(text))] if italic else []
        with translate_errors():
            try:
                await self.client.edit_message(chat_id, message_id, text, parse_mode=None, formatting_entities=entities)
            except errors.MessageNotModifiedError:
                pass  # the caption already has this text

    async def reply(self, chat_id: int, message_id: int, text: str) -> None:
        with translate_errors():
            await self.client.send_message(
                chat_id, text, reply_to=message_id, parse_mode=None, link_preview=False
            )


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2
