"""
Integration tests for HookReceiver.

Spins up the server and sends raw HTTP POST requests, verifying that
events are emitted and HTTP responses are correct.
"""
from __future__ import annotations

import asyncio
import json
import pytest

from cia.hook_receiver import HookReceiver
from cia.schema import Event, Phase


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

async def _post(port: int, path: str, payload: dict) -> int:
    """Send an HTTP POST and return the status code."""
    body = json.dumps(payload).encode()
    request = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"\r\n"
    ).encode() + body

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(request)
        await writer.drain()
        response = await asyncio.wait_for(reader.read(256), timeout=2.0)
        status_line = response.split(b"\r\n")[0].decode()
        return int(status_line.split()[1])
    finally:
        writer.close()
        await writer.wait_closed()


# ------------------------------------------------------------------ #
# Fixtures                                                             #
# ------------------------------------------------------------------ #

@pytest.fixture
async def receiver_and_events():
    events: list[Event] = []
    r = HookReceiver(events.append, host="127.0.0.1", port=0)

    # Patch start_server to get the actual assigned port
    original_start = asyncio.start_server

    server_ref: list[asyncio.Server] = []

    async def patched_start(handler, host, port):
        srv = await original_start(handler, host, 0)
        server_ref.append(srv)
        return srv

    import unittest.mock as mock
    with mock.patch("asyncio.start_server", patched_start):
        task = asyncio.create_task(r.start())
        await asyncio.sleep(0.05)

    port = server_ref[0].sockets[0].getsockname()[1]
    yield port, events

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# ------------------------------------------------------------------ #
# Tests                                                                #
# ------------------------------------------------------------------ #

class TestHookEndpoints:
    async def test_pre_tool_use(self, receiver_and_events):
        port, events = receiver_and_events
        payload = {
            "session_id": "ses_001",
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi"},
            "hook_event_name": "PreToolUse",
        }
        status = await _post(port, "/hook/pre", payload)
        assert status == 200
        await asyncio.sleep(0.05)
        assert len(events) == 1
        assert events[0].phase is Phase.TOOL_CALL_START
        assert events[0].tool == "Bash"
        assert events[0].session_id == "ses_001"

    async def test_post_tool_use(self, receiver_and_events):
        port, events = receiver_and_events
        payload = {
            "session_id": "ses_002",
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/x"},
            "tool_response": {"is_error": False, "content": "data"},
            "hook_event_name": "PostToolUse",
        }
        status = await _post(port, "/hook/post", payload)
        assert status == 200
        await asyncio.sleep(0.05)
        assert events[0].phase is Phase.TOOL_CALL_END
        assert events[0].error is None

    async def test_stop_hook(self, receiver_and_events):
        port, events = receiver_and_events
        payload = {"session_id": "ses_003", "hook_event_name": "Stop"}
        status = await _post(port, "/hook/stop", payload)
        assert status == 200
        await asyncio.sleep(0.05)
        # Stop fires at the end of each assistant turn, not session end.
        assert events[0].phase is Phase.TURN_END

    async def test_session_end_hook(self, receiver_and_events):
        port, events = receiver_and_events
        payload = {"session_id": "ses_003b", "reason": "exit",
                   "hook_event_name": "SessionEnd"}
        status = await _post(port, "/hook/session-end", payload)
        assert status == 200
        await asyncio.sleep(0.05)
        assert events[0].phase is Phase.SESSION_END
        assert events[0].meta.get("reason") == "exit"

    async def test_new_lifecycle_hooks(self, receiver_and_events):
        port, events = receiver_and_events
        for path, name, phase in [
            ("/hook/notification",  "Notification", Phase.NOTIFICATION),
            ("/hook/pre-compact",   "PreCompact",   Phase.CONTEXT_COMPACT),
            ("/hook/subagent-stop", "SubagentStop", Phase.SUBAGENT_END),
        ]:
            status = await _post(port, path, {"session_id": "s", "hook_event_name": name})
            assert status == 200
        await asyncio.sleep(0.05)
        phases = {e.phase for e in events}
        assert {Phase.NOTIFICATION, Phase.CONTEXT_COMPACT, Phase.SUBAGENT_END} <= phases

    async def test_unknown_path_returns_400(self, receiver_and_events):
        port, events = receiver_and_events
        status = await _post(port, "/unknown", {"x": 1})
        assert status == 400
        await asyncio.sleep(0.05)
        assert len(events) == 0

    async def test_post_tool_use_error_captured(self, receiver_and_events):
        port, events = receiver_and_events
        payload = {
            "session_id": "ses_004",
            "tool_name": "Bash",
            "tool_input": {"command": "bad"},
            "tool_response": {
                "is_error": True,
                "content": [{"type": "text", "text": "command not found: bad"}],
            },
            "hook_event_name": "PostToolUse",
        }
        await _post(port, "/hook/post", payload)
        await asyncio.sleep(0.05)
        assert events[0].error == "command not found: bad"
