"""Run the real read-only MCP HTTP app against a fake Telegram account, for pi extension tests.

Usage: uv run python integrations/pi/tests/fake_mcp_server.py <token-file>
Prints `READY <port>` once it listens on 127.0.0.1. Never touches the real Telegram account.
"""

from __future__ import annotations

import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

from tests.mcp_fakes import FakeTelegramClient, basic_group, user  # noqa: E402
from transcription_bot.mcp_server.export import Exporter  # noqa: E402
from transcription_bot.mcp_server.reader import TelegramReader  # noqa: E402
from transcription_bot.mcp_server.server import build_server, http_app  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> None:
    token = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
    client = FakeTelegramClient()
    alice = client.add_dialog(user(1, "Alice", username="alice"), unread=2)
    group = client.add_dialog(basic_group(10, "Family"))
    for i in range(1, 6):
        client.add_message(group, i, f"hello {i}", sender=alice)
    export_dir = Path(tempfile.mkdtemp(prefix="fake-mcp-exports-"))
    reader = TelegramReader(client)
    app = http_app(build_server(reader, Exporter(reader, export_dir, export_dir)), auth_token=token)

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           access_log=False, lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    else:
        raise SystemExit("fake MCP server did not start")
    print(f"READY {port}", flush=True)
    try:
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        server.should_exit = True


if __name__ == "__main__":
    main()
