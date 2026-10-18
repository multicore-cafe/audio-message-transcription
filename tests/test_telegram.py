import pytest
from telethon import errors, events, types

from transcription_bot.errors import CaptionTooLong, FatalError, FloodWait, PermanentError, TransientError
from transcription_bot.telegram import TelethonOps, is_owner_voice, owner_voice_event, translate_errors


def make_message(*, out=True, voice=True, media=True, text=""):
    document = types.Document(
        id=1, access_hash=1, file_reference=b"", date=None, mime_type="audio/ogg", size=1234, dc_id=2,
        attributes=[types.DocumentAttributeAudio(duration=3, voice=voice)],
    )
    return types.Message(
        id=10, peer_id=types.PeerUser(42), date=None, message=text, out=out,
        media=types.MessageMediaDocument(document=document) if media else None,
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
