"""Tiny fake OpenAI-compatible server for integration tests. No network, no real model.

First chat completion requests a tool call to `telegram_list_dialogs`; later ones answer with
plain text, so the agent loop finishes. Prints `READY <port>` when it listens on 127.0.0.1.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TOOL_CALL = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "telegram_list_dialogs", "arguments": "{}"},
}


def chunk(delta: dict, finish: str | None = None, usage: bool = False) -> bytes:
    body = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "fake",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }
    if usage:
        body["usage"] = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    return f"data: {json.dumps(body)}\n\n".encode()


class Handler(BaseHTTPRequestHandler):
    calls = 0
    lock = threading.Lock()

    def log_message(self, *args) -> None:  # keep the test output clean
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._send(200, json.dumps({"object": "list", "data": []}).encode(), "application/json")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        request = json.loads(self.rfile.read(length) or b"{}")
        with Handler.lock:
            Handler.calls += 1
            first = Handler.calls == 1
        stream = bool(request.get("stream"))
        if not first:
            message = {"role": "assistant", "content": "The tool call was refused; nothing to report."}
            if stream:
                self._send(200, chunk({"role": "assistant", "content": message["content"]})
                           + chunk({}, finish="stop", usage=True) + b"data: [DONE]\n\n",
                           "text/event-stream")
            else:
                self._send(200, json.dumps({
                    "id": "chatcmpl-fake", "object": "chat.completion", "created": 0, "model": "fake",
                    "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                }).encode(), "application/json")
            return
        if stream:
            body = (
                chunk({"role": "assistant", "content": None,
                       "tool_calls": [{**TOOL_CALL, "index": 0,
                                       "function": {**TOOL_CALL["function"], "arguments": ""}}]})
                + chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]})
                + chunk({}, finish="tool_calls", usage=True)
                + b"data: [DONE]\n\n"
            )
            self._send(200, body, "text/event-stream")
        else:
            self._send(200, json.dumps({
                "id": "chatcmpl-fake", "object": "chat.completion", "created": 0, "model": "fake",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": None,
                                                     "tool_calls": [TOOL_CALL]},
                             "finish_reason": "tool_calls"}],
            }).encode(), "application/json")


def main() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"READY {port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
