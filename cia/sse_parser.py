"""
Parse Anthropic streaming SSE responses and emit CIA Events.

Anthropic SSE format reference:
  event: message_start          → api_request_start (model, input + cache tokens)
  event: content_block_start    → api_thinking_start / api_generation_start
  event: content_block_delta    → first one fires api_first_token (time to first token)
  event: content_block_stop     → api_thinking_end / api_generation_end (per-block duration)
  event: message_delta          → captures output tokens + stop_reason
  event: message_stop           → api_response_end (full latency + throughput breakdown)

Timing vocabulary (all measured from when the request left, i.e. set_request_start):
  ttfb_ms     request_start → message_start   (server queue + prefill before the stream opens)
  ttft_ms     request_start → first token     (prefill + first content block latency)
  thinking_ms summed duration of all thinking blocks
  generation_ms  first text token → last text token
  total_ms    request_start → message_stop
  output_tokens_per_sec   output_tokens / generation window
"""
from __future__ import annotations

import json
import time
from typing import Callable, Optional

from cia.schema import Event, Phase


class SSEParser:
    """
    Feed raw bytes from an Anthropic SSE response stream.
    Emits Event objects via the provided callback.
    """

    def __init__(
        self,
        flow_id: str,
        emit: Callable[[Event], None],
        session_id: Optional[str] = None,
    ) -> None:
        self._flow_id = flow_id
        self._emit = emit
        self._session_id = session_id

        self._buf = b""

        # Timeline (epoch seconds; None until the corresponding event arrives)
        self._request_start_ts: Optional[float] = None
        self._message_start_ts: Optional[float] = None
        self._first_token_ts: Optional[float] = None
        self._first_text_token_ts: Optional[float] = None
        self._last_text_token_ts: Optional[float] = None

        # Per-block bookkeeping: index → (type, start_ts)
        self._block_open: dict[int, tuple[str, float]] = {}
        # Completed blocks: list of {"type", "duration_ms"}
        self._blocks: list[dict] = []

        self._model: Optional[str] = None
        self._request_anatomy: dict = {}
        self._tokens_input: int = 0
        self._tokens_output: int = 0
        self._cache_read: int = 0
        self._cache_creation: int = 0
        self._thinking_ms: float = 0.0
        self._stop_reason: Optional[str] = None

    def set_request_start(self, ts: float) -> None:
        self._request_start_ts = ts

    def set_request_info(self, anatomy: dict) -> None:
        """Attach request-body anatomy; included in api_request_start meta."""
        self._request_anatomy = anatomy

    # ------------------------------------------------------------------ #
    # Ingestion                                                            #
    # ------------------------------------------------------------------ #

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        # SSE events are separated by a blank line (\n\n)
        while b"\n\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n\n", 1)
            self._process_raw_event(raw.decode("utf-8", errors="replace"))

    def flush(self) -> None:
        """Process any trailing data that lacks a trailing blank line."""
        if self._buf.strip():
            self._process_raw_event(self._buf.decode("utf-8", errors="replace"))
            self._buf = b""

    # ------------------------------------------------------------------ #
    # Internal                                                             #
    # ------------------------------------------------------------------ #

    def _process_raw_event(self, text: str) -> None:
        data_str: Optional[str] = None
        for line in text.strip().splitlines():
            if line.startswith("data: "):
                data_str = line[6:].strip()

        if not data_str or data_str == "[DONE]":
            return
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            return

        self._dispatch(data)

    def _ms_since_start(self, now: float) -> Optional[float]:
        if self._request_start_ts is None:
            return None
        return (now - self._request_start_ts) * 1000

    def _dispatch(self, data: dict) -> None:
        dtype = data.get("type")
        now = time.time()

        if dtype == "message_start":
            self._on_message_start(data, now)
        elif dtype == "content_block_start":
            self._on_block_start(data, now)
        elif dtype == "content_block_delta":
            self._on_block_delta(data, now)
        elif dtype == "content_block_stop":
            self._on_block_stop(data, now)
        elif dtype == "message_delta":
            usage = data.get("usage", {})
            self._tokens_output = usage.get("output_tokens", self._tokens_output)
            delta = data.get("delta", {})
            if delta.get("stop_reason"):
                self._stop_reason = delta["stop_reason"]
        elif dtype == "message_stop":
            self._on_message_stop(now)

    # ------------------------------------------------------------------ #
    # Event handlers                                                       #
    # ------------------------------------------------------------------ #

    def _on_message_start(self, data: dict, now: float) -> None:
        self._message_start_ts = now
        msg = data.get("message", {})
        self._model = msg.get("model")
        usage = msg.get("usage", {})
        self._tokens_input = usage.get("input_tokens", 0)
        self._cache_read = usage.get("cache_read_input_tokens", 0) or 0
        self._cache_creation = usage.get("cache_creation_input_tokens", 0) or 0
        # output_tokens sometimes already present in the initial usage
        self._tokens_output = usage.get("output_tokens", self._tokens_output) or self._tokens_output

        ttfb_ms = self._ms_since_start(now)
        meta = {
            "flow_id": self._flow_id,
            "message_id": msg.get("id", ""),
            "ttfb_ms": ttfb_ms,
            "cache_read_input_tokens": self._cache_read,
            "cache_creation_input_tokens": self._cache_creation,
        }
        if self._request_anatomy:
            meta["request"] = self._request_anatomy
        self._emit(Event(
            phase=Phase.API_REQUEST_START,
            ts=self._request_start_ts or now,
            session_id=self._session_id,
            model=self._model,
            tokens_input=self._tokens_input,
            meta=meta,
        ))

    def _on_block_start(self, data: dict, now: float) -> None:
        idx = data.get("index", 0)
        block = data.get("content_block", {})
        btype = block.get("type", "")
        self._block_open[idx] = (btype, now)

        if btype == "thinking":
            self._emit(Event(
                phase=Phase.API_THINKING_START,
                ts=now,
                session_id=self._session_id,
                model=self._model,
                meta={"flow_id": self._flow_id, "block_index": idx,
                      "since_request_ms": self._ms_since_start(now)},
            ))
        elif btype in ("text", "tool_use"):
            self._emit(Event(
                phase=Phase.API_GENERATION_START,
                ts=now,
                session_id=self._session_id,
                model=self._model,
                meta={"flow_id": self._flow_id, "block_index": idx,
                      "block_type": btype,
                      "tool": block.get("name") if btype == "tool_use" else None,
                      "since_request_ms": self._ms_since_start(now)},
            ))

    def _on_block_delta(self, data: dict, now: float) -> None:
        idx = data.get("index", 0)
        delta = data.get("delta", {})
        dtype = delta.get("type", "")

        # First token of any kind → time-to-first-token milestone.
        if self._first_token_ts is None:
            self._first_token_ts = now
            self._emit(Event(
                phase=Phase.API_FIRST_TOKEN,
                ts=now,
                session_id=self._session_id,
                model=self._model,
                duration_ms=self._ms_since_start(now),  # TTFT from request start
                meta={"flow_id": self._flow_id, "delta_type": dtype},
            ))

        # Track the text-generation window for throughput.
        if dtype == "text_delta":
            if self._first_text_token_ts is None:
                self._first_text_token_ts = now
            self._last_text_token_ts = now
        # tool_use arguments stream as input_json_delta; count them as generation too
        elif dtype == "input_json_delta":
            self._last_text_token_ts = now

    def _on_block_stop(self, data: dict, now: float) -> None:
        idx = data.get("index", 0)
        opened = self._block_open.pop(idx, None)
        if opened is None:
            return
        btype, start_ts = opened
        duration_ms = (now - start_ts) * 1000
        self._blocks.append({"type": btype, "index": idx, "duration_ms": duration_ms})

        if btype == "thinking":
            self._thinking_ms += duration_ms
            self._emit(Event(
                phase=Phase.API_THINKING_END,
                ts=now,
                session_id=self._session_id,
                model=self._model,
                duration_ms=duration_ms,
                meta={"flow_id": self._flow_id, "block_index": idx},
            ))
        elif btype in ("text", "tool_use"):
            self._emit(Event(
                phase=Phase.API_GENERATION_END,
                ts=now,
                session_id=self._session_id,
                model=self._model,
                duration_ms=duration_ms,
                meta={"flow_id": self._flow_id, "block_index": idx,
                      "block_type": btype},
            ))

    def _on_message_stop(self, now: float) -> None:
        total_ms = self._ms_since_start(now)
        ttfb_ms = (
            (self._message_start_ts - self._request_start_ts) * 1000
            if self._message_start_ts is not None and self._request_start_ts is not None
            else None
        )
        ttft_ms = (
            (self._first_token_ts - self._request_start_ts) * 1000
            if self._first_token_ts is not None and self._request_start_ts is not None
            else None
        )
        generation_ms: Optional[float] = None
        if self._first_text_token_ts is not None and self._last_text_token_ts is not None:
            generation_ms = (self._last_text_token_ts - self._first_text_token_ts) * 1000

        tok_per_sec: Optional[float] = None
        if generation_ms and generation_ms > 0 and self._tokens_output:
            tok_per_sec = round(self._tokens_output / (generation_ms / 1000), 1)

        latency = {
            "ttfb_ms": round(ttfb_ms, 1) if ttfb_ms is not None else None,
            "ttft_ms": round(ttft_ms, 1) if ttft_ms is not None else None,
            "thinking_ms": round(self._thinking_ms, 1) if self._thinking_ms else None,
            "generation_ms": round(generation_ms, 1) if generation_ms is not None else None,
            "total_ms": round(total_ms, 1) if total_ms is not None else None,
            "output_tokens_per_sec": tok_per_sec,
        }
        usage = {
            "input_tokens": self._tokens_input,
            "output_tokens": self._tokens_output,
            "cache_read_input_tokens": self._cache_read,
            "cache_creation_input_tokens": self._cache_creation,
        }

        self._emit(Event(
            phase=Phase.API_RESPONSE_END,
            ts=now,
            session_id=self._session_id,
            model=self._model,
            tokens_input=self._tokens_input,
            tokens_output=self._tokens_output,
            duration_ms=total_ms,
            meta={
                "flow_id": self._flow_id,
                "stop_reason": self._stop_reason,
                "latency": latency,
                "usage": usage,
                "content_blocks": self._blocks,
            },
        ))
