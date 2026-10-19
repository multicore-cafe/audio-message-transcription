"""The allowed-models file: which pi provider/model pairs may use the Telegram MCP.

One entry per line: ``provider/model-id`` (exact) or ``provider/*`` (any model of that
provider).  Blank lines and lines starting with ``#`` are ignored.  Anything else is a
configuration error.  The file is not secret; it lives outside the repository (default
``~/.config/telegram-mcp/allowed-models``, override with ``MCP_ALLOWED_MODELS_FILE`` in the
container or ``TELEGRAM_MCP_ALLOWED_MODELS_FILE`` elsewhere).

Both the MCP server and the pi extension fail closed: a missing, unreadable, empty or invalid
file allows no model at all.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ..config import ConfigError

DEFAULT_ALLOWED_MODELS_FILE = Path("~/.config/telegram-mcp/allowed-models")
ALLOWED_MODELS_FILE_ENV = "MCP_ALLOWED_MODELS_FILE"
PI_ALLOWED_MODELS_FILE_ENV = "TELEGRAM_MCP_ALLOWED_MODELS_FILE"

# Fixed, generic sentence every non-allowed model must answer with.
REFUSAL = "I can't do this: access to Telegram data is restricted to the models allowed by the owner."
CREATE_HINT = (
    "Create it with `integrations/pi/install.sh --allow <provider/model>` (repeatable), or write "
    "one entry per line, e.g. `someprovider/*`."
)


@dataclass(frozen=True)
class AllowList:
    """Parsed entries in file order plus the providers that allow every model."""

    entries: tuple[str, ...]
    providers: frozenset[str]
    source: str = "allowed-models"

    @classmethod
    def parse(cls, text: str, *, source: str = "allowed-models") -> AllowList:
        entries: list[str] = []
        providers: set[str] = set()
        seen: set[str] = set()
        for lineno, raw in enumerate(text.splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            location = f"{source}: line {lineno}"
            if "/" not in line or any(ch.isspace() for ch in line):
                raise ConfigError(f"{location}: expected `provider/model-id` or `provider/*`, got {line!r}")
            provider, model = line.split("/", 1)
            if not provider or not model or provider == "*":
                raise ConfigError(f"{location}: expected `provider/model-id` or `provider/*`, got {line!r}")
            entry = f"{provider}/*" if model == "*" else f"{provider}/{model}"
            if entry in seen:
                continue
            seen.add(entry)
            entries.append(entry)
            if model == "*":
                providers.add(provider)
        if not entries:
            raise ConfigError(f"{source}: no allowed models configured; nothing may use the Telegram MCP. {CREATE_HINT}")
        return cls(tuple(entries), frozenset(providers), source)

    @property
    def models(self) -> str:
        """The list exactly as it appears in the generated policy text."""
        return ", ".join(self.entries)

    def is_allowed(self, provider: str | None, model: str | None) -> bool:
        if not provider or not model:
            return False
        return provider in self.providers or f"{provider}/{model}" in self.entries


def allowed_models_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    override = (env.get(ALLOWED_MODELS_FILE_ENV) or env.get(PI_ALLOWED_MODELS_FILE_ENV) or "").strip()
    return Path(override).expanduser() if override else DEFAULT_ALLOWED_MODELS_FILE.expanduser()


def load_allowlist(path: str | Path | None = None, *, env: Mapping[str, str] | None = None) -> AllowList:
    """Read and parse the allow-list, raising ConfigError (fail closed) on any problem."""
    resolved = Path(path).expanduser() if path is not None else allowed_models_path(env)
    try:
        text = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(
            f"cannot read the allowed-models file {resolved}: {exc}. {CREATE_HINT} Without it no "
            "model may use the Telegram MCP."
        ) from None
    return AllowList.parse(text, source=str(resolved))


def access_policy(allow_list: AllowList) -> str:
    """The policy sentence used as server instructions and as every tool description prefix."""
    return (
        f"{REFUSAL} Allowed models: {allow_list.models}. Only a model whose pi provider/model "
        "matches this list may use the Telegram tools; every other model must refuse and answer "
        "with exactly that sentence."
    )
