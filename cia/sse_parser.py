"""
Parse Anthropic streaming SSE responses and emit CIA Events.

Anthropic SSE format reference:
  event: message_start          → api_request_start (with model + input tokens)
  event: content_block_start    → api_thinking_start (if type=thinking)
                                   or api_generation_start (if type=text)
  event: content_block_stop     → api_thinking_end (if the block was thinking)
  event: message_delta          → captures output tokens
  event: message_stop           → api_response_end (with durations + tokens)
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
        self._request_start_ts: Optional[float] = None
        self._thinking_start_ts: Optional[float] = None
        self._generation_start_ts: Optional[float] = None

        # track what kind of block is at each index
        self._block_types: dict[int, str] = {}

        self._model: Optional[str] = None
        self._tokens_input: int = 0
        self._tokens_output: int = 0
        self._thinking_tokens: int = 0

    def set_request_start(self, ts: float) -> None:
        self._request_start_ts = ts

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
        event_type: Optional[str] = None
        data_str: Optional[str] = None
        for line in text.strip().splitlines():
            if line.startswith("event: "):
                event_type = line[7:].strip()
            elif line.startswith("data: "):
                data_str = line[6:].strip()

        if not data_str or data_str == "[DONE]":
            return
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            return

        self._dispatch(data)

    def _dispatch(self, data: dict) -> None:
        dtype = data.get("type")
        now = time.time()

        if dtype == "message_start":
            self._on_message_start(data, now)

        elif dtype == "content_block_start":
            self._on_block_start(data, now)

        elif dtype == "content_block_stop":
            self._on_block_stop(data, now)

        elif dtype == "message_delta":
            usage = data.get("usage", {})
            self._tokens_output = usage.get("output_tokens", self._tokens_output)

        elif dtype == "message_stop":
            self._on_message_stop(now)

    # ------------------------------------------------------------------ #
    # Event handlers                                                       #
    # ------------------------------------------------------------------ #

    def _on_message_start(self, data: dict, now: float) -> None:
        msg = data.get("message", {})
        self._model = msg.get("model")
        usage = msg.get("usage", {})
        self._tokens_input = usage.get("input_tokens", 0)
        ts = self._request_start_ts or now
        self._emit(Event(
            phase=Phase.API_REQUEST_START,
            ts=ts,
            session_id=self._session_id,
            model=self._model,
            tokens_input=self._tokens_input,
            meta={"flow_id": self._flow_id, "message_id": msg.get("id", "")},
        ))

    def _on_block_start(self, data: dict, now: float) -> None:
        idx = data.get("index", 0)
        block = data.get("content_block", {})
        btype = block.get("type", "")
        self._block_types[idx] = btype

        if btype == "thinking":
            self._thinking_start_ts = now
            self._emit(Event(
                phase=Phase.API_THINKING_START,
                ts=now,
                session_id=self._session_id,
                model=self._model,
                meta={"flow_id": self._flow_id, "block_index": idx},
            ))
        elif btype == "text":
            self._generation_start_ts = now
            self._emit(Event(
                phase=Phase.API_GENERATION_START,
                ts=now,
                session_id=self._session_id,
                model=self._model,
                meta={"flow_id": self._flow_id, "block_index": idx},
            ))

    def _on_block_stop(self, data: dict, now: float) -> None:
        idx = data.get("index", 0)
        btype = self._block_types.pop(idx, None)

        if btype == "thinking" and self._thinking_start_ts is not None:
            duration_ms = (now - self._thinking_start_ts) * 1000
            self._emit(Event(
                phase=Phase.API_THINKING_END,
                ts=now,
                session_id=self._session_id,
                model=self._model,
                duration_ms=duration_ms,
                meta={"flow_id": self._flow_id},
            ))
            self._thinking_start_ts = None

    def _on_message_stop(self, now: float) -> None:
        duration_ms: Optional[float] = None
        if self._request_start_ts is not None:
            duration_ms = (now - self._request_start_ts) * 1000

        self._emit(Event(
            phase=Phase.API_RESPONSE_END,
            ts=now,
            session_id=self._session_id,
            model=self._model,
            tokens_input=self._tokens_input,
            tokens_output=self._tokens_output,
            duration_ms=duration_ms,
            meta={"flow_id": self._flow_id},
        ))
