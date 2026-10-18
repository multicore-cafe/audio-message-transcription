"""Error kinds that decide how a voice message job continues."""


class TransientError(Exception):
    """Temporary failure (network, timeout, rate limit, server error): retry with backoff."""


class FloodWait(TransientError):
    """Telegram asked to wait `seconds` before the next request."""

    def __init__(self, seconds: float):
        super().__init__(f"flood wait for {seconds} s")
        self.seconds = seconds


class PermanentError(Exception):
    """Retrying will not help; `note` is shown to the user in the caption."""

    def __init__(self, note: str):
        super().__init__(note)
        self.note = note


class CaptionTooLong(Exception):
    """The text does not fit into a media caption."""


class FatalError(Exception):
    """The bot cannot continue at all (for example, the session was revoked)."""
