from types import SimpleNamespace

import httpx2
import openai
import pytest

from transcription_bot.errors import PermanentError, TransientError
from transcription_bot.transcriber import PROMPT, Transcriber, classify_openai_error


def status_error(cls, status, body=None):
    request = httpx2.Request("POST", "https://api.openai.com/v1/audio/transcriptions")
    return cls("error", response=httpx2.Response(status, request=request), body=body)


@pytest.mark.parametrize(
    "exc, kind",
    [
        (openai.APIConnectionError(request=httpx2.Request("POST", "https://x")), TransientError),
        (openai.APITimeoutError(request=httpx2.Request("POST", "https://x")), TransientError),
        (status_error(openai.RateLimitError, 429), TransientError),
        # Account problems (quota, billing, key) are fixed by the owner: retry, do not fail the message.
        (status_error(openai.RateLimitError, 429, {"code": "insufficient_quota"}), TransientError),
        (status_error(openai.AuthenticationError, 401), TransientError),
        (status_error(openai.PermissionDeniedError, 403), TransientError),
        (status_error(openai.InternalServerError, 503), TransientError),
        (status_error(openai.BadRequestError, 400), PermanentError),
        (status_error(openai.NotFoundError, 404), PermanentError),
    ],
)
def test_classify_openai_error(exc, kind):
    assert isinstance(classify_openai_error(exc), kind)


class FakeClient:
    def __init__(self, result="  text  ", error=None):
        self.kwargs = None
        self.result = result
        self.error = error
        self.audio = SimpleNamespace(transcriptions=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.result


async def test_sends_ogg_bytes_with_prompt():
    client = FakeClient()
    assert await Transcriber(client, "whisper-1").transcribe(b"OggS...") == "text"
    assert client.kwargs["model"] == "whisper-1"
    assert client.kwargs["file"] == ("voice.ogg", b"OggS...", "audio/ogg")
    assert client.kwargs["prompt"] == PROMPT
    assert client.kwargs["response_format"] == "text"


async def test_api_errors_are_classified():
    client = FakeClient(error=status_error(openai.InternalServerError, 500))
    with pytest.raises(TransientError):
        await Transcriber(client).transcribe(b"OggS")
