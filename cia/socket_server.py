"""
Unix domain socket IPC server.

Protocol: newline-delimited JSON.  Client sends one JSON command,
server responds with one JSON object.

Commands:
  {"cmd": "status"}
  {"cmd": "stop"}
  {"cmd": "clear"}
  {"cmd": "backup", "dir": "/path/to/dest"}
  {"cmd": "sessions"}
  {"cmd": "export", "format": "jsonl"|"csv", "session_id": "...", "since": 0.0, "until": 0.0}
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cia.daemon import Daemon


class SocketServer:
    def __init__(self, socket_path: Path, daemon: "Daemon") -> None:
        self._path = socket_path
        self._daemon = daemon
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        if self._path.exists():
            self._path.unlink()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._server = await asyncio.start_unix_server(
            self._handle, path=str(self._path)
        )
        # Signal the daemon that the socket is ready.
        self._daemon.started.set()
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._path.exists():
            self._path.unlink(missing_ok=True)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=5.0)
            cmd = json.loads(line.decode())
            result = await self._dispatch(cmd)
        except Exception as exc:
            result = {"ok": False, "error": str(exc)}
        try:
            writer.write(json.dumps(result).encode() + b"\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    async def _dispatch(self, cmd: dict) -> dict:
        action = cmd.get("cmd", "")

        if action == "status":
            return {
                "ok": True,
                "running": self._daemon.running,
                "events": await self._daemon.store.count(),
                "sessions": await self._daemon.store.sessions(),
            }

        if action == "stop":
            self._daemon.stop()
            return {"ok": True, "stopped": True}

        if action == "clear":
            await self._daemon.store.clear()
            return {"ok": True, "cleared": True}

        if action == "backup":
            dest = cmd.get("dir")
            if not dest:
                return {"ok": False, "error": "backup requires a 'dir'"}
            info = await self._daemon.store.backup(Path(dest))
            return {"ok": True, **info}

        if action == "sessions":
            return {"ok": True, "sessions": await self._daemon.store.sessions()}

        if action == "export":
            fmt = cmd.get("format", "jsonl")
            kwargs = {
                k: cmd[k]
                for k in ("session_id", "since", "until")
                if cmd.get(k) is not None
            }
            if fmt == "csv":
                data = await self._daemon.store.export_csv(**kwargs)
            else:
                data = await self._daemon.store.export_jsonl(**kwargs)
            return {"ok": True, "data": data}

        return {"ok": False, "error": f"unknown command: {action!r}"}
