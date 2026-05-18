"""
Minimal asyncio HTTP server that receives Claude Code hook POSTs.

Claude Code hooks are shell scripts installed in .claude/settings.json.
Each script reads JSON from stdin and POSTs it here.

Endpoints:
  POST /hook/pre   — PreToolUse
  POST /hook/post  — PostToolUse
  POST /hook/stop  — Stop
"""
from __future__ import annotations

import asyncio
import json
from typing import Callable

from cia.schema import Event, Phase

_ROUTE_PHASE: dict[str, Phase] = {
    "/hook/pre":  Phase.TOOL_CALL_START,
    "/hook/post": Phase.TOOL_CALL_END,
    "/hook/stop": Phase.SESSION_END,
}

_OK = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
_BAD = b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


class HookReceiver:
    def __init__(
        self,
        emit: Callable[[Event], None],
        host: str = "127.0.0.1",
        port: int = 7171,
    ) -> None:
        self._emit = emit
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self._host, self._port
        )
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            raw = await asyncio.wait_for(reader.read(65_536), timeout=2.0)
            path, body = _parse_http(raw)
            phase = _ROUTE_PHASE.get(path)
            if phase is not None and body is not None:
                payload = json.loads(body)
                event = Event.from_hook_payload(phase, payload)
                self._emit(event)
                writer.write(_OK)
            else:
                writer.write(_BAD)
        except Exception:
            writer.write(_BAD)
        finally:
            try:
                await writer.drain()
                writer.close()
            except Exception:
                pass


def _parse_http(raw: bytes) -> tuple[str, bytes | None]:
    """Extract (path, body) from a raw HTTP/1.x request."""
    header_end = raw.find(b"\r\n\r\n")
    if header_end == -1:
        return "", None
    header_block = raw[:header_end]
    body = raw[header_end + 4:]
    first_line = header_block.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    parts = first_line.split()
    path = parts[1] if len(parts) >= 2 else ""
    return path, body
