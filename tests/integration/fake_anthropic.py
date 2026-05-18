"""
Minimal fake Anthropic SSE server for integration tests.

Starts a real HTTP server on a random port that serves the canned
streaming SSE response (with or without thinking blocks).
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator


_SSE_SIMPLE = [
    {"type": "message_start", "message": {
        "id": "msg_fake", "type": "message", "role": "assistant",
        "model": "claude-sonnet-4-6", "content": [],
        "usage": {"input_tokens": 10},
    }},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta",  "index": 0, "delta": {"type": "text_delta", "text": "Hello!"}},
    {"type": "content_block_stop",   "index": 0},
    {"type": "message_delta",  "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
    {"type": "message_stop"},
]

_SSE_WITH_THINKING = [
    {"type": "message_start", "message": {
        "id": "msg_think", "type": "message", "role": "assistant",
        "model": "claude-sonnet-4-6", "content": [],
        "usage": {"input_tokens": 20},
    }},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
    {"type": "content_block_delta",  "index": 0, "delta": {"type": "thinking_delta", "thinking": "Hmm..."}},
    {"type": "content_block_stop",   "index": 0},
    {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta",  "index": 1, "delta": {"type": "text_delta", "text": "Sure!"}},
    {"type": "content_block_stop",   "index": 1},
    {"type": "message_delta",  "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 8}},
    {"type": "message_stop"},
]


def _build_sse_body(events: list[dict]) -> bytes:
    lines = []
    for evt in events:
        lines.append(f"event: {evt['type']}")
        lines.append(f"data: {json.dumps(evt)}")
        lines.append("")
        lines.append("")
    return "\n".join(lines).encode()


class FakeAnthropicServer:
    """Async context manager; yields (host, port)."""

    def __init__(self, with_thinking: bool = False) -> None:
        self._with_thinking = with_thinking
        self._server: asyncio.Server | None = None
        self.host = "127.0.0.1"
        self.port = 0

    async def __aenter__(self):
        self._server = await asyncio.start_server(
            self._handle, self.host, 0
        )
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await reader.read(8192)  # consume request
        events = _SSE_WITH_THINKING if self._with_thinking else _SSE_SIMPLE
        body = _build_sse_body(events)
        response = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: text/event-stream\r\n"
            b"Cache-Control: no-cache\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"\r\n"
            + body
        )
        writer.write(response)
        await writer.drain()
        writer.close()
