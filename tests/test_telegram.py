import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from telethon import errors, events, functions, types

from transcription_bot.config import TelegramSettings
from transcription_bot.errors import CaptionTooLong, FatalError, FloodWait, PermanentError, TransientError
from transcription_bot.processor import STATUS_TEXT
from transcription_bot.telegram import (
    TelethonOps,
    connect_authorized,
    find_untranscribed,
    is_owner_voice,
    owner_voice_event,
    session_lock,
    translate_errors,
)

DATE = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


def make_message(*, out=True, voice=True, media=True, text="", id=10, peer=None, forwarded=False):
    document = types.Document(
        id=1, access_hash=1, file_reference=b"", date=None, mime_type="audio/ogg", size=1234, dc_id=2,
        attributes=[types.DocumentAttributeAudio(duration=3, voice=voice)],
    )
    return types.Message(
        id=id, peer_id=peer or types.PeerUser(42), date=DATE, message=text, out=out,
        media=types.MessageMediaDocument(document=document) if media else None,
        fwd_from=types.MessageFwdHeader(date=DATE, from_name="someone") if forwarded else None,
    )


def passes(message) -> bool:
    builder = owner_voice_event()
    builder.resolved = True
    return bool(builder.filter(events.NewMessage.Event(message)))


def test_handler_only_takes_outgoing_voice_messages():
    assert passes(make_message())
    assert not passes(make_message(out=False))  # someone else's voice
    assert not passes(make_message(voice=False))  # audio file, not a voice note
    assert not passes(make_message(media=False, text="hi"))
    assert not passes(make_message(forwarded=True))  # forwarded by the owner, recorded by someone else
    assert is_owner_voice(make_message()) and not is_owner_voice(make_message(out=False))


@pytest.mark.parametrize(
    "exc, kind",
    [
        (errors.FloodWaitError(None, capture=5), FloodWait),
        (errors.MediaCaptionTooLongError(None), CaptionTooLong),
        (errors.MessageIdInvalidError(None), PermanentError),
        (errors.MessageEditTimeExpiredError(None), PermanentError),
        (errors.AuthKeyUnregisteredError(None), FatalError),
        (errors.ServerError(None, "INTERNAL"), TransientError),
        (ConnectionError("reset"), TransientError),
        (TimeoutError(), TransientError),
        (ValueError("Could not find the input entity"), PermanentError),
    ],
)
def test_translate_errors(exc, kind):
    with pytest.raises(kind):
        with translate_errors():
            raise exc


def test_flood_wait_keeps_seconds():
    with pytest.raises(FloodWait) as info:
        with translate_errors():
            raise errors.FloodWaitError(None, capture=17)
    assert info.value.seconds == 17


class FakeClient:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def edit_message(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error

    async def get_messages(self, chat_id, ids):
        return make_message(text="caption")


async def test_set_caption_ignores_not_modified_and_sets_italic():
    client = FakeClient(errors.MessageNotModifiedError(None))
    await TelethonOps(client).set_caption(42, 10, "Привет", italic=True)
    (args, kwargs), = client.calls
    assert args == (42, 10, "Привет")
    assert kwargs["parse_mode"] is None
    assert kwargs["formatting_entities"] == [types.MessageEntityItalic(0, 6)]


async def test_set_caption_plain_text_is_not_parsed():
    client = FakeClient()
    await TelethonOps(client).set_caption(42, 10, "**not bold** [x](y)")
    (_, kwargs), = client.calls
    assert kwargs["parse_mode"] is None and kwargs["formatting_entities"] == []


async def test_fetch_maps_message():
    message = await TelethonOps(FakeClient()).fetch(42, 10)
    assert (message.caption, message.size, message.is_voice) == ("caption", 1234, True)


class AuthClient:
    def __init__(self, error=None, auth_key=b"key"):
        self.session = SimpleNamespace(auth_key=auth_key)
        self.error = error
        self.connected = False
        self.requests = []

    async def connect(self):
        self.connected = True

    async def __call__(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return types.updates.State(pts=1, qts=0, date=DATE, seq=0, unread_count=0)


auth_client = AuthClient


async def test_connect_authorized_accepts_a_logged_in_session():
    client = auth_client()
    await connect_authorized(client, "log in")
    assert client.connected and isinstance(client.requests[0], functions.updates.GetStateRequest)


@pytest.mark.parametrize("error", [errors.AuthKeyUnregisteredError(None), errors.SessionRevokedError(None)])
async def test_connect_authorized_reports_a_logged_out_session(error):
    with pytest.raises(FatalError, match="log in"):
        await connect_authorized(auth_client(error), "log in")


async def test_connect_authorized_without_session_does_not_contact_telegram():
    client = auth_client(auth_key=None)
    with pytest.raises(FatalError, match="log in"):
        await connect_authorized(client, "log in")
    assert not client.connected


@pytest.mark.parametrize("error", [errors.ServerError(None, "INTERNAL"), errors.FloodWaitError(None, capture=5),
                                   ConnectionError("reset")])
async def test_connect_authorized_does_not_blame_the_session_for_outages(error):
    # Telethon's is_user_authorized() would report these as "not logged in".
    with pytest.raises(type(error)):
        await connect_authorized(auth_client(error), "log in")


def test_session_lock_blocks_a_second_instance(tmp_path):
    settings = TelegramSettings(api_id=1, api_hash="x", session_path=tmp_path / "data" / "bot.session")
    with session_lock(settings):
        with pytest.raises(FatalError, match="another instance is running"):
            with session_lock(settings):
                pass
    with session_lock(settings):  # released again
        pass
    other = TelegramSettings(api_id=1, api_hash="x", session_path=tmp_path / "data" / "mcp.session")
    with session_lock(settings), session_lock(other):  # another session in the same directory is fine
        pass


def test_session_lock_works_across_processes(tmp_path):
    settings = TelegramSettings(api_id=1, api_hash="x", session_path=tmp_path / "bot.session")
    code = (
        "import sys; from pathlib import Path\n"
        "from transcription_bot.config import TelegramSettings\n"
        "from transcription_bot.errors import FatalError\n"
        "from transcription_bot.telegram import session_lock\n"
        f"s = TelegramSettings(1, 'x', Path({str(settings.session_path)!r}))\n"
        "try:\n"
        "    with session_lock(s): sys.exit(0)\n"
        "except FatalError: sys.exit(2)\n"
    )
    with session_lock(settings):
        assert subprocess.run([sys.executable, "-c", code]).returncode == 2
    assert subprocess.run([sys.executable, "-c", code]).returncode == 0


ALICE = types.User(id=42, access_hash=1, first_name="Alice")
GROUP = types.Channel(id=77, access_hash=2, title="Group", photo=types.ChatPhotoEmpty(), date=DATE, megagroup=True)


class SearchClient:
    """Answers messages.searchGlobal with fixed pages."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.requests = []

    async def __call__(self, request):
        assert isinstance(request, functions.messages.SearchGlobalRequest)
        bytes(request)  # serializes with the installed TL schema
        self.requests.append(request)
        messages = self.pages.pop(0)
        return types.messages.MessagesSlice(count=99, messages=messages, chats=[GROUP], users=[ALICE], topics=[],
                                            next_rate=1234)


async def test_sweep_finds_only_own_untranscribed_voice_messages():
    group = types.PeerChannel(77)
    page = [
        make_message(id=1),  # own, no caption: wanted
        make_message(id=2, text=STATUS_TEXT, peer=group),  # left with the status caption: wanted
        make_message(id=3, text="already transcribed"),
        make_message(id=4, out=False),  # someone else's voice
        make_message(id=5, forwarded=True),  # forwarded by the owner
        types.MessageEmpty(id=6, peer_id=types.PeerUser(42)),
    ]
    client = SearchClient([page])
    since = DATE - timedelta(hours=3)
    assert await find_untranscribed(client, since) == [(42, 1), (-1000000000077, 2)]
    (request,) = client.requests
    assert request.q == "" and isinstance(request.filter, types.InputMessagesFilterVoice)
    assert request.min_date == since and request.max_date is None
    assert isinstance(request.offset_peer, types.InputPeerEmpty) and request.offset_id == 0


async def test_sweep_pages_through_the_results(monkeypatch):
    monkeypatch.setattr("transcription_bot.telegram.SWEEP_PAGE_SIZE", 2)
    group = types.PeerChannel(77)
    client = SearchClient([[make_message(id=9), make_message(id=8, peer=group)], [make_message(id=7)]])
    assert await find_untranscribed(client, DATE) == [(42, 9), (-1000000000077, 8), (42, 7)]
    second = client.requests[1]
    assert (second.offset_rate, second.offset_id) == (1234, 8)
    assert isinstance(second.offset_peer, types.InputPeerChannel) and second.offset_peer.channel_id == 77
