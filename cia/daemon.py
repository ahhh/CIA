"""
CIA Daemon — orchestrates all monitoring components.

Component topology:
  ProxyThread        (own thread + loop) → emit_threadsafe → event_queue
  HookReceiver       (asyncio task)      → emit            → event_queue
  FsWatcher          (asyncio task)      → emit            → event_queue
  SocketServer       (asyncio task)      → reads store
  _drain_loop        (asyncio task)      → event_queue     → Store
"""
from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Optional

from cia.hook_receiver import HookReceiver
from cia.proxy import ProxyThread
from cia.schema import Event
from cia.socket_server import SocketServer
from cia.store import Store
from cia.watcher import FsWatcher

CIA_DIR = Path.home() / ".cia"


class Daemon:
    def __init__(
        self,
        db_path: Path = CIA_DIR / "cia.db",
        jsonl_path: Optional[Path] = CIA_DIR / "events.jsonl",
        proxy_host: str = "127.0.0.1",
        proxy_port: int = 8080,
        hook_host: str = "127.0.0.1",
        hook_port: int = 7171,
        socket_path: Path = CIA_DIR / "cia.sock",
        watch_dirs: Optional[list[Path]] = None,
    ) -> None:
        self.store = Store(db_path, jsonl_path)
        self._proxy_host = proxy_host
        self._proxy_port = proxy_port
        self._hook_host = hook_host
        self._hook_port = hook_port
        self._socket_path = socket_path
        self._watch_dirs: list[Path] = watch_dirs or []

        self._event_queue: asyncio.Queue[Event] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._proxy_thread: Optional[ProxyThread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.running = False
        self.started: asyncio.Event = asyncio.Event()  # set when socket is ready

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self.store.open()
        self.running = True

        # Only start the proxy thread when a real port is requested.
        # Tests pass proxy_port=0 to skip the mitmproxy thread entirely.
        if self._proxy_port > 0:
            self._proxy_thread = ProxyThread(
                emit=self._emit_threadsafe,
                host=self._proxy_host,
                port=self._proxy_port,
            )
            self._proxy_thread.start()

        hook_receiver = HookReceiver(self._emit, self._hook_host, self._hook_port)
        socket_server = SocketServer(self._socket_path, self)

        self._tasks = [
            asyncio.create_task(self._drain_loop(), name="drain"),
            asyncio.create_task(hook_receiver.start(), name="hooks"),
            asyncio.create_task(socket_server.start(), name="socket"),
        ]
        for d in self._watch_dirs:
            watcher = FsWatcher(d, self._emit)
            self._tasks.append(
                asyncio.create_task(watcher.start(), name=f"watch:{d}")
            )

        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            pass
        finally:
            await self._shutdown()

    def stop(self) -> None:
        self.running = False
        for t in self._tasks:
            t.cancel()

    # ------------------------------------------------------------------ #
    # Event ingestion                                                      #
    # ------------------------------------------------------------------ #

    def _emit(self, event: Event) -> None:
        """Called from the main asyncio loop."""
        self._event_queue.put_nowait(event)

    def _emit_threadsafe(self, event: Event) -> None:
        """Called from the proxy thread; bridges to the main loop."""
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._event_queue.put_nowait, event)

    async def _drain_loop(self) -> None:
        while self.running:
            try:
                event = await asyncio.wait_for(self._event_queue.get(), timeout=1.0)
                await self.store.add(event)
                self._event_queue.task_done()
            except asyncio.TimeoutError:
                pass

    # ------------------------------------------------------------------ #
    # Cleanup                                                              #
    # ------------------------------------------------------------------ #

    async def _shutdown(self) -> None:
        if self._proxy_thread:
            self._proxy_thread.stop()
        # drain remaining events
        while not self._event_queue.empty():
            event = self._event_queue.get_nowait()
            try:
                await self.store.add(event)
            except Exception:
                pass
        await self.store.close()


# ------------------------------------------------------------------ #
# Entry point (used by `cia start --foreground` and tests)            #
# ------------------------------------------------------------------ #

async def run_daemon(**kwargs) -> None:
    d = Daemon(**kwargs)

    loop = asyncio.get_running_loop()

    def _sigterm(*_):
        d.stop()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _sigterm)
        except NotImplementedError:
            pass

    await d.run()
