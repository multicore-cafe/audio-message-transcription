"""Telegram client setup (shared with other services) and the Telethon adapter for the processor."""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from typing import Iterator

from telethon import TelegramClient, errors, events, functions, types

from .config import TelegramSettings
from .errors import CaptionTooLong, FatalError, FloodWait, PermanentError, TransientError
from .processor import VoiceMessage

log = logging.getLogger(__name__)


def create_client(
    settings: TelegramSettings, *, catch_up: bool = False, receive_updates: bool = True
) -> TelegramClient:
    """Client with an SQLite session file at `settings.session_path`."""
    settings.session_path.parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(
        str(settings.session_path),
        settings.api_id,
        settings.api_hash,
        timeout=settings.request_timeout,
        connection_retries=settings.connection_retries,
        retry_delay=settings.retry_delay,
        auto_reconnect=True,
        catch_up=catch_up,
        receive_updates=receive_updates,
    )


def login_hint(settings: TelegramSettings) -> str:
    return (
        f"Telegram session {settings.session_path} is not authorized. Log in interactively with "
        "`docker compose run --rm bot transcription-bot login` (locally: `uv run transcription-bot login`)."
    )


async def connect_authorized(client: TelegramClient, settings: TelegramSettings) -> None:
    """Connect and make sure the session is logged in; raises FatalError otherwise."""
    await client.connect()
    if not await client.is_user_authorized():
        raise FatalError(login_hint(settings))


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
    """An outgoing (sent by the account owner from any device) voice note."""
    return bool(message.out and message.voice)


def owner_voice_event() -> events.NewMessage:
    """New voice messages sent by the account owner (from any of their devices)."""
    return events.NewMessage(outgoing=True, func=lambda event: is_owner_voice(event.message))


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
