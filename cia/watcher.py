"""
Wraps fswatch (must be installed: brew install fswatch) to emit
FILE_CHANGE events for a watched directory.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

from cia.claude_paths import classify_path
from cia.schema import Event, Phase


class FsWatcher:
    def __init__(
        self,
        watch_dir: Path,
        emit: Callable[[Event], None],
        latency: float = 0.5,
    ) -> None:
        self._dir = watch_dir
        self._emit = emit
        self._latency = latency
        self._proc: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        cmd = [
            "fswatch",
            "--recursive",
            f"--latency={self._latency}",
            "--event=Created",
            "--event=Updated",
            "--event=Removed",
            "--event=Renamed",
            str(self._dir),
        ]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert self._proc.stdout is not None
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                path = line.decode("utf-8", errors="replace").strip()
                if path:
                    meta = {"path": path, "watch_dir": str(self._dir)}
                    category = classify_path(path)
                    if category:
                        meta["category"] = category
                        meta["filename"] = Path(path).name
                    self._emit(Event(phase=Phase.FILE_CHANGE, meta=meta))
        except (FileNotFoundError, asyncio.CancelledError):
            pass
        finally:
            await self.stop()

    async def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except Exception:
                pass
