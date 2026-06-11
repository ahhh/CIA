"""
mitmproxy addon that intercepts Anthropic API calls.

Runs in a daemon thread with its own event loop because mitmproxy's
DumpMaster.run() calls asyncio.run() internally.  Events are bridged
back to the daemon's main loop via loop.call_soon_threadsafe().
"""
from __future__ import annotations

import asyncio
import json
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
_MESSAGES_PATH = "/v1/messages"
_COUNT_TOKENS_PATH = "/v1/messages/count_tokens"
_DEBUG = os.environ.get("CIA_DEBUG", "").lower() in ("1", "true", "yes", "on")


def _dlog(msg: str) -> None:
    """Print a debug line to the daemon log when CIA_DEBUG is set."""
    if _DEBUG:
        print(f"[cia.proxy] {msg}", file=sys.stderr, flush=True)


def _is_anthropic(flow: http.HTTPFlow) -> bool:
    return _ANTHROPIC_HOST in flow.request.pretty_host


def _is_sse(flow: http.HTTPFlow) -> bool:
    return "text/event-stream" in flow.response.headers.get("content-type", "")


def _endpoint(flow: http.HTTPFlow) -> str:
    return flow.request.path.split("?", 1)[0]


def _parse_json_body(message) -> dict:
    """Best-effort JSON parse of a request/response body; {} on failure."""
    try:
        text = message.get_text(strict=False)
        if not text:
            return {}
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ------------------------------------------------------------------ #
# mitmproxy addon                                                      #
# ------------------------------------------------------------------ #

class CIAAddon:
    """mitmproxy addon; all hooks are called inside the proxy thread's loop."""

    def __init__(self, emit: Callable[[Event], None]) -> None:
        self._emit = emit
        self._parsers: dict[str, SSEParser] = {}
        self._req_info: dict[str, dict] = {}

    def request(self, flow: http.HTTPFlow) -> None:
        if not _is_anthropic(flow):
            return
        now = time.time()
        endpoint = _endpoint(flow)
        body = _parse_json_body(flow.request)
        self._req_info[flow.id] = {
            "ts": now,
            "endpoint": endpoint,
            "model": body.get("model"),
        }
        _dlog(f"→ {flow.request.method} {flow.request.path}  (flow {flow.id[:8]})")

        if endpoint == _COUNT_TOKENS_PATH:
            self._emit(Event(
                phase=Phase.TOKENIZER_START,
                ts=now,
                model=body.get("model"),
                meta={"flow_id": flow.id, "path": endpoint},
            ))

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        if not _is_anthropic(flow):
            return
        ctype = flow.response.headers.get("content-type", "?")
        sse = _is_sse(flow)
        _dlog(f"← {flow.response.status_code} {ctype}  sse={sse}  (flow {flow.id[:8]})")
        if sse:
            parser = SSEParser(flow.id, self._emit)
            info = self._req_info.get(flow.id, {})
            parser.set_request_start(info.get("ts", time.time()))
            self._parsers[flow.id] = parser
            # Attach a streaming transformer so we see chunks in real time
            flow.response.stream = _make_stream_transformer(parser)

    def response(self, flow: http.HTTPFlow) -> None:
        if not _is_anthropic(flow):
            return
        info = self._req_info.pop(flow.id, {})
        req_ts = info.get("ts")
        parser = self._parsers.pop(flow.id, None)

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
                ts=req_ts or time.time(),
                error=f"HTTP {flow.response.status_code}",
                meta={"flow_id": flow.id, "path": flow.request.path,
                      "body": body[:500]},
            ))
        elif parser is not None:
            # Streaming flow: flush any remaining buffer; the SSE parser
            # has already emitted the full API timing breakdown.
            parser.flush()
            _dlog(f"⇲ stream complete (flow {flow.id[:8]})")
        else:
            self._on_json_response(flow, info, req_ts)

    def _on_json_response(self, flow: http.HTTPFlow, info: dict,
                          req_ts: Optional[float]) -> None:
        """Emit timing for successful non-SSE responses (count_tokens and
        non-streaming /v1/messages calls), which the SSE parser never sees."""
        now = time.time()
        duration_ms = (now - req_ts) * 1000 if req_ts is not None else None
        endpoint = info.get("endpoint") or _endpoint(flow)
        body = _parse_json_body(flow.response)

        if endpoint == _COUNT_TOKENS_PATH:
            self._emit(Event(
                phase=Phase.TOKENIZER_END,
                ts=now,
                duration_ms=duration_ms,
                model=info.get("model"),
                tokens_input=body.get("input_tokens"),
                meta={"flow_id": flow.id, "path": endpoint},
            ))
            _dlog(f"⏱ count_tokens {duration_ms:.0f}ms (flow {flow.id[:8]})"
                  if duration_ms is not None else "⏱ count_tokens done")
        elif endpoint == _MESSAGES_PATH:
            usage = body.get("usage") or {}
            model = body.get("model") or info.get("model")
            self._emit(Event(
                phase=Phase.API_REQUEST_START,
                ts=req_ts or now,
                model=model,
                tokens_input=usage.get("input_tokens"),
                meta={"flow_id": flow.id, "streaming": False,
                      "message_id": body.get("id", ""),
                      "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0) or 0,
                      "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0) or 0},
            ))
            self._emit(Event(
                phase=Phase.API_RESPONSE_END,
                ts=now,
                duration_ms=duration_ms,
                model=model,
                tokens_input=usage.get("input_tokens"),
                tokens_output=usage.get("output_tokens"),
                meta={"flow_id": flow.id, "streaming": False,
                      "stop_reason": body.get("stop_reason"),
                      "usage": usage},
            ))


def _make_stream_transformer(parser: SSEParser):
    """Return a mitmproxy stream callable that feeds each chunk into the parser.

    mitmproxy >= 7 calls this once per chunk as ``stream(data: bytes)`` and
    sends ``b""`` at end-of-message; the chunk must be returned unmodified.
    """
    def transform(data: bytes) -> bytes:
        if data:
            parser.feed(data)
        return data
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
