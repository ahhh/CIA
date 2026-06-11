"""
mitmproxy addon that intercepts Anthropic API calls.

Runs in a daemon thread with its own event loop because mitmproxy's
DumpMaster.run() calls asyncio.run() internally.  Events are bridged
back to the daemon's main loop via loop.call_soon_threadsafe().
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from typing import Callable, Optional

from mitmproxy import http
from mitmproxy.options import Options
from mitmproxy.tools.dump import DumpMaster

from cia.schema import Event, Phase
from cia.sse_parser import SSEParser

_ANTHROPIC_HOST = "api.anthropic.com"
_DEBUG = os.environ.get("CIA_DEBUG", "").lower() in ("1", "true", "yes", "on")


def _dlog(msg: str) -> None:
    """Print a debug line to the daemon log when CIA_DEBUG is set."""
    if _DEBUG:
        print(f"[cia.proxy] {msg}", file=sys.stderr, flush=True)


def _is_anthropic(flow: http.HTTPFlow) -> bool:
    return _ANTHROPIC_HOST in flow.request.pretty_host


def _is_sse(flow: http.HTTPFlow) -> bool:
    return "text/event-stream" in flow.response.headers.get("content-type", "")


# ------------------------------------------------------------------ #
# mitmproxy addon                                                      #
# ------------------------------------------------------------------ #

class CIAAddon:
    """mitmproxy addon; all hooks are called inside the proxy thread's loop."""

    def __init__(self, emit: Callable[[Event], None]) -> None:
        self._emit = emit
        self._parsers: dict[str, SSEParser] = {}
        self._req_ts: dict[str, float] = {}

    def request(self, flow: http.HTTPFlow) -> None:
        if not _is_anthropic(flow):
            return
        self._req_ts[flow.id] = time.time()
        _dlog(f"→ {flow.request.method} {flow.request.path}  (flow {flow.id[:8]})")

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        if not _is_anthropic(flow):
            return
        ctype = flow.response.headers.get("content-type", "?")
        sse = _is_sse(flow)
        _dlog(f"← {flow.response.status_code} {ctype}  sse={sse}  (flow {flow.id[:8]})")
        if sse:
            parser = SSEParser(flow.id, self._emit)
            parser.set_request_start(self._req_ts.get(flow.id, time.time()))
            self._parsers[flow.id] = parser
            # Attach a streaming transformer so we see chunks in real time
            flow.response.stream = _make_stream_transformer(parser)

    def response(self, flow: http.HTTPFlow) -> None:
        if not _is_anthropic(flow):
            return
        if flow.response.status_code >= 400 and not _is_sse(flow):
            # Capture the error body (helpful for diagnosing 400/404s).
            body = ""
            try:
                body = flow.response.get_text(strict=False) or ""
            except Exception:
                pass
            _dlog(f"✗ HTTP {flow.response.status_code} {flow.request.path}: {body[:300]}")
            self._emit(Event(
                phase=Phase.API_REQUEST_ERROR,
                ts=self._req_ts.get(flow.id, time.time()),
                error=f"HTTP {flow.response.status_code}",
                meta={"flow_id": flow.id, "path": flow.request.path,
                      "body": body[:500]},
            ))
        # Flush any remaining buffer and clean up
        parser = self._parsers.pop(flow.id, None)
        if parser:
            parser.flush()
            _dlog(f"⇲ stream complete (flow {flow.id[:8]})")
        self._req_ts.pop(flow.id, None)


def _make_stream_transformer(parser: SSEParser):
    """Return a mitmproxy stream callable that feeds each chunk into the parser."""
    def transform(chunks):
        for chunk in chunks:
            parser.feed(chunk)
            yield chunk
    return transform


# ------------------------------------------------------------------ #
# Thread runner                                                        #
# ------------------------------------------------------------------ #

class ProxyThread(threading.Thread):
    """Runs mitmproxy in a daemon thread with its own asyncio event loop."""

    def __init__(
        self,
        emit: Callable[[Event], None],
        host: str = "127.0.0.1",
        port: int = 8080,
    ) -> None:
        super().__init__(name="cia-proxy", daemon=True)
        self._emit = emit
        self._host = host
        self._port = port
        self._master: Optional[DumpMaster] = None
        self._started = threading.Event()
        self._error: Optional[Exception] = None

    def run(self) -> None:
        # mitmproxy's DumpMaster.__init__ calls asyncio.get_running_loop(),
        # and Master.run() is a coroutine, so this thread needs its own loop.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except Exception as exc:
            self._error = exc
            self._started.set()
        finally:
            loop.close()

    async def _serve(self) -> None:
        opts = Options(listen_host=self._host, listen_port=self._port)
        self._master = DumpMaster(opts, with_termlog=False, with_dumper=False)
        self._master.addons.add(CIAAddon(self._emit))
        self._started.set()
        await self._master.run()

    def wait_ready(self, timeout: float = 5.0) -> None:
        self._started.wait(timeout)
        if self._error:
            raise self._error

    def stop(self) -> None:
        if self._master:
            self._master.shutdown()
