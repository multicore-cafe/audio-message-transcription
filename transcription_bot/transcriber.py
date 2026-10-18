"""Speech-to-text through the OpenAI transcription API."""

from __future__ import annotations

import openai

from .errors import PermanentError, TransientError

PROMPT = (
    "Вот мой ответ. Он может быть на русском, or maybe english or a combination of both. "
    "Иногда английские слова могут встречаться в русском тексте, такие как usecases, language models, etc. "
    "Some text may also be adjusted muuuuch better match the intonation. It's reeeeaally convenient."
)

# Upload limit of the OpenAI transcription endpoint.
MAX_FILE_SIZE = 25 * 1024 * 1024
TOO_LARGE_NOTE = "voice message is larger than 25 MB"


def create_openai_client(api_key: str, organization: str | None, timeout: float) -> openai.AsyncOpenAI:
    """Retries are done by the processor, so the SDK's own retries are off. OPENAI_BASE_URL is read by the SDK."""
    return openai.AsyncOpenAI(api_key=api_key, organization=organization, timeout=timeout, max_retries=0)


def classify_openai_error(exc: openai.OpenAIError) -> Exception:
    if isinstance(exc, openai.APIConnectionError):  # includes timeouts
        return TransientError(f"OpenAI connection error: {exc}")
    if isinstance(exc, openai.RateLimitError):
        if exc.code == "insufficient_quota":
            return PermanentError("OpenAI quota exceeded")
        return TransientError("OpenAI rate limit")
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code >= 500 or exc.status_code in (408, 409):
            return TransientError(f"OpenAI server error {exc.status_code}")
        return PermanentError(f"OpenAI error {exc.status_code}")
    return PermanentError(f"OpenAI error: {type(exc).__name__}")


class Transcriber:
    def __init__(self, client: openai.AsyncOpenAI, model: str = "whisper-1", prompt: str = PROMPT):
        self._client = client
        self._model = model
        self._prompt = prompt

    async def transcribe(self, audio: bytes) -> str:
        """Transcribe OGG/Opus bytes as Telegram stores voice messages."""
        if len(audio) > MAX_FILE_SIZE:
            raise PermanentError(TOO_LARGE_NOTE)
        try:
            result = await self._client.audio.transcriptions.create(
                model=self._model,
                file=("voice.ogg", audio, "audio/ogg"),
                response_format="text",
                prompt=self._prompt,
            )
        except openai.OpenAIError as exc:
            raise classify_openai_error(exc) from exc
        text = result if isinstance(result, str) else getattr(result, "text", "")
        return text.strip()
