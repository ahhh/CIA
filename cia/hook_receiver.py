"""
Minimal asyncio HTTP server that receives Claude Code hook POSTs.

Claude Code hooks are shell scripts installed in .claude/settings.json.
Each script reads JSON from stdin and POSTs it here.

Endpoints:
  POST /hook/session-start — SessionStart
  POST /hook/prompt        — UserPromptSubmit
  POST /hook/pre           — PreToolUse
  POST /hook/post          — PostToolUse
  POST /hook/notification  — Notification
  POST /hook/pre-compact   — PreCompact
  POST /hook/subagent-stop — SubagentStop
  POST /hook/stop          — Stop          (assistant turn end)
  POST /hook/session-end   — SessionEnd
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Callable

from cia.schema import Event, Phase

_ROUTE_PHASE: dict[str, Phase] = {
    "/hook/session-start": Phase.SESSION_START,
    "/hook/prompt":        Phase.USER_PROMPT,
    "/hook/pre":           Phase.TOOL_CALL_START,
    "/hook/post":          Phase.TOOL_CALL_END,
    "/hook/notification":  Phase.NOTIFICATION,
    "/hook/pre-compact":   Phase.CONTEXT_COMPACT,
    "/hook/subagent-stop": Phase.SUBAGENT_END,
    "/hook/stop":          Phase.TURN_END,
    "/hook/session-end":   Phase.SESSION_END,
}

_DEBUG = os.environ.get("CIA_DEBUG", "").lower() in ("1", "true", "yes", "on")


def _dlog(msg: str) -> None:
    if _DEBUG:
        print(f"[cia.hooks] {msg}", file=sys.stderr, flush=True)

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
                _dlog(_describe(phase, payload))
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


def _describe(phase: Phase, payload: dict) -> str:
    """One-line debug summary of an incoming hook."""
    sid = (payload.get("session_id") or "")[:8]
    if phase is Phase.USER_PROMPT:
        prompt = (payload.get("prompt") or "").replace("\n", " ")
        return f"{phase.value} [{sid}] prompt={prompt[:120]!r}"
    if phase is Phase.SESSION_START:
        return f"{phase.value} [{sid}] source={payload.get('source')}"
    if phase is Phase.SESSION_END:
        return f"{phase.value} [{sid}] reason={payload.get('reason')}"
    if phase is Phase.CONTEXT_COMPACT:
        return f"{phase.value} [{sid}] trigger={payload.get('trigger')}"
    if phase is Phase.NOTIFICATION:
        msg = (payload.get("message") or "").replace("\n", " ")
        return f"{phase.value} [{sid}] {msg[:120]!r}"
    if phase in (Phase.TOOL_CALL_START, Phase.TOOL_CALL_END):
        return f"{phase.value} [{sid}] tool={payload.get('tool_name')}"
    return f"{phase.value} [{sid}]"


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
