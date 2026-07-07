"""
Integration tests for the full daemon lifecycle.

Uses the Unix socket to exercise start → write events → query → stop.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
import pytest
from pathlib import Path

from cia.daemon import Daemon
from cia.schema import Event, Phase


def _short_socket() -> Path:
    """
    macOS caps Unix socket paths at 104 bytes.
    pytest's tmp_path can exceed that, so we always put the socket in /tmp.
    """
    return Path(f"/tmp/cia_{uuid.uuid4().hex[:8]}.sock")


# ------------------------------------------------------------------ #
# Async socket helper                                                  #
# ------------------------------------------------------------------ #

async def _send_cmd(socket_path: Path, cmd: dict) -> dict:
    reader, writer = await asyncio.open_unix_connection(path=str(socket_path))
    try:
        writer.write(json.dumps(cmd).encode() + b"\n")
        await writer.drain()
        data = await asyncio.wait_for(reader.readline(), timeout=5.0)
        return json.loads(data.decode())
    finally:
        writer.close()
        await writer.wait_closed()


# ------------------------------------------------------------------ #
# Fixture                                                              #
# ------------------------------------------------------------------ #

@pytest.fixture
async def running_daemon(tmp_path):
    """
    Start a daemon without the proxy thread (proxy_port=0) so tests
    don't need mitmproxy or network access.
    """
    socket_path = _short_socket()
    d = Daemon(
        db_path=tmp_path / "test.db",
        jsonl_path=None,
        proxy_port=0,       # 0 = skip proxy thread
        hook_port=0,        # random port, not needed by these tests
        otlp_port=0,        # 0 = skip OTLP receiver (4318 may be a live daemon)
        socket_path=socket_path,
        watch_dirs=[],
    )

    task = asyncio.create_task(d.run())
    # Wait for the socket server to signal it's ready (up to 3 s)
    await asyncio.wait_for(d.started.wait(), timeout=3.0)

    yield d, socket_path

    d.stop()
    await asyncio.gather(task, return_exceptions=True)
    socket_path.unlink(missing_ok=True)


# ------------------------------------------------------------------ #
# Tests                                                                #
# ------------------------------------------------------------------ #

class TestDaemonLifecycle:
    async def test_socket_created(self, running_daemon):
        _, socket_path = running_daemon
        assert socket_path.exists()

    async def test_status_running(self, running_daemon):
        _, socket_path = running_daemon
        result = await _send_cmd(socket_path, {"cmd": "status"})
        assert result["ok"] is True
        assert result["running"] is True
        assert result["events"] == 0

    async def test_direct_event_write_and_query(self, running_daemon):
        daemon, socket_path = running_daemon
        await daemon.store.add(Event(phase=Phase.SESSION_START, session_id="s1"))
        await daemon.store.add(Event(phase=Phase.API_RESPONSE_END, session_id="s1"))

        result = await _send_cmd(socket_path, {"cmd": "status"})
        assert result["events"] == 2
        assert "s1" in result["sessions"]

    async def test_export_jsonl(self, running_daemon):
        daemon, socket_path = running_daemon
        await daemon.store.add(Event(phase=Phase.TOOL_CALL_START, session_id="s2", tool="Bash"))

        result = await _send_cmd(socket_path, {"cmd": "export", "format": "jsonl"})
        assert result["ok"] is True
        lines = result["data"].strip().splitlines()
        assert len(lines) == 1
        evt = json.loads(lines[0])
        assert evt["phase"] == "tool_call_start"
        assert evt["tool"] == "Bash"

    async def test_export_since_seq_and_max_seq(self, running_daemon):
        """The tail's cursor protocol: page by insert order so an event
        committed late with an *earlier* timestamp is still delivered."""
        daemon, socket_path = running_daemon
        await daemon.store.add(Event(phase=Phase.NETWORK_REQUEST, ts=2000.0))

        result = await _send_cmd(socket_path, {"cmd": "export", "format": "jsonl"})
        assert result["max_seq"] == 1
        assert json.loads(result["data"].strip())["seq"] == 1

        # Late commit, older timestamp — a ts cursor at 2000.0 would skip it.
        await daemon.store.add(Event(phase=Phase.OTEL_EVENT, ts=1000.0))
        result = await _send_cmd(
            socket_path, {"cmd": "export", "format": "jsonl", "since_seq": 1})
        lines = result["data"].strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["phase"] == "otel_event"
        assert result["max_seq"] == 2

    async def test_export_csv(self, running_daemon):
        daemon, socket_path = running_daemon
        await daemon.store.add(Event(phase=Phase.FILE_CHANGE))

        result = await _send_cmd(socket_path, {"cmd": "export", "format": "csv"})
        assert result["ok"] is True
        lines = result["data"].strip().splitlines()
        assert lines[0].startswith("phase,ts")

    async def test_export_filter_by_session(self, running_daemon):
        daemon, socket_path = running_daemon
        await daemon.store.add(Event(phase=Phase.SESSION_START, session_id="alpha"))
        await daemon.store.add(Event(phase=Phase.SESSION_START, session_id="beta"))

        result = await _send_cmd(socket_path, {
            "cmd": "export", "format": "jsonl", "session_id": "alpha"
        })
        lines = result["data"].strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["session_id"] == "alpha"

    async def test_clear(self, running_daemon):
        daemon, socket_path = running_daemon
        await daemon.store.add(Event(phase=Phase.SESSION_START))
        result = await _send_cmd(socket_path, {"cmd": "clear"})
        assert result["ok"] is True
        status = await _send_cmd(socket_path, {"cmd": "status"})
        assert status["events"] == 0

    async def test_backup(self, running_daemon, tmp_path):
        daemon, socket_path = running_daemon
        await daemon.store.add(Event(phase=Phase.SESSION_START, session_id="x"))
        dest = tmp_path / "snap"
        result = await _send_cmd(socket_path, {"cmd": "backup", "dir": str(dest)})
        assert result["ok"] is True
        assert result["events"] == 1
        assert Path(result["db"]).exists()
        # Backed-up DB is a real SQLite copy with the event in it.
        from cia.store import Store
        restored = Store(Path(result["db"]))
        await restored.open()
        assert await restored.count() == 1
        await restored.close()

    async def test_backup_requires_dir(self, running_daemon):
        _, socket_path = running_daemon
        result = await _send_cmd(socket_path, {"cmd": "backup"})
        assert result["ok"] is False

    async def test_sessions_command(self, running_daemon):
        daemon, socket_path = running_daemon
        await daemon.store.add(Event(phase=Phase.SESSION_START, session_id="x"))
        await daemon.store.add(Event(phase=Phase.SESSION_START, session_id="y"))
        result = await _send_cmd(socket_path, {"cmd": "sessions"})
        assert result["ok"] is True
        assert sorted(result["sessions"]) == ["x", "y"]

    async def test_unknown_command(self, running_daemon):
        _, socket_path = running_daemon
        result = await _send_cmd(socket_path, {"cmd": "nope"})
        assert result["ok"] is False

    async def test_stop_command(self, running_daemon):
        daemon, socket_path = running_daemon
        result = await _send_cmd(socket_path, {"cmd": "stop"})
        assert result["ok"] is True
        await asyncio.sleep(0.2)
        assert not daemon.running

    async def test_event_queue_persisted_to_store(self, running_daemon):
        """Events injected via _emit() go through the drain loop to the store."""
        daemon, socket_path = running_daemon
        daemon._emit(Event(phase=Phase.FILE_CHANGE, meta={"path": "/tmp/x"}))
        # Give the drain loop one cycle
        await asyncio.sleep(0.1)
        result = await _send_cmd(socket_path, {"cmd": "status"})
        assert result["events"] == 1
