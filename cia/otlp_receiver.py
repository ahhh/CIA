"""
Minimal OTLP/HTTP (JSON) receiver for Claude Code's native telemetry.

Claude Code exports OpenTelemetry metrics and log events when launched with:

  CLAUDE_CODE_ENABLE_TELEMETRY=1
  OTEL_METRICS_EXPORTER=otlp
  OTEL_LOGS_EXPORTER=otlp
  OTEL_EXPORTER_OTLP_PROTOCOL=http/json
  OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318

(`cia run` sets all of these.)  This receiver accepts POST /v1/metrics and
POST /v1/logs in the OTLP JSON encoding and maps every data point / log
record to a CIA Event:

  otel_metric — e.g. claude_code.token.usage, claude_code.cost.usage,
                claude_code.lines_of_code.count, claude_code.commit.count
  otel_event  — e.g. api_request, api_error, tool_result, user_prompt

Claude Code stamps its telemetry with a session.id attribute, which is
mapped onto Event.session_id — so this stream is fully session-attributed.
"""
from __future__ import annotations

import asyncio
import gzip
import json
import os
import sys
from typing import Callable, Optional

from cia.schema import Event, Phase

_DEBUG = os.environ.get("CIA_DEBUG", "").lower() in ("1", "true", "yes", "on")


def _dlog(msg: str) -> None:
    if _DEBUG:
        print(f"[cia.otlp] {msg}", file=sys.stderr, flush=True)


_OK = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
       b"Content-Length: 2\r\nConnection: close\r\n\r\n{}")
_BAD = (b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n"
        b"Connection: close\r\n\r\n")


# ------------------------------------------------------------------ #
# OTLP JSON → Events                                                  #
# ------------------------------------------------------------------ #

def _attr_value(v: dict):
    """Unwrap an OTLP AnyValue ({"stringValue": ...}, {"intValue": ...} …)."""
    if not isinstance(v, dict):
        return v
    for key in ("stringValue", "boolValue"):
        if key in v:
            return v[key]
    for key in ("intValue", "doubleValue"):
        if key in v:
            try:
                return float(v[key]) if key == "doubleValue" else int(v[key])
            except (TypeError, ValueError):
                return v[key]
    if "arrayValue" in v:
        return [_attr_value(x) for x in v["arrayValue"].get("values", [])]
    if "kvlistValue" in v:
        return _attrs_to_dict(v["kvlistValue"].get("values", []))
    return v


def _attrs_to_dict(attrs: list) -> dict:
    return {a.get("key"): _attr_value(a.get("value", {}))
            for a in attrs if isinstance(a, dict)}


def _session_id(attrs: dict) -> Optional[str]:
    return attrs.get("session.id") or attrs.get("session_id")


def _ts_from_nano(nano, fallback_required: bool = True) -> Optional[float]:
    try:
        ts = int(nano) / 1e9
        return ts if ts > 0 else None
    except (TypeError, ValueError):
        return None


def parse_metrics_payload(payload: dict) -> list[Event]:
    """Map an OTLP ExportMetricsServiceRequest (JSON) to otel_metric events."""
    events = []
    for rm in payload.get("resourceMetrics", []):
        for sm in rm.get("scopeMetrics", []):
            for metric in sm.get("metrics", []):
                name = metric.get("name", "?")
                unit = metric.get("unit")
                body = metric.get("sum") or metric.get("gauge") \
                    or metric.get("histogram") or {}
                for dp in body.get("dataPoints", []):
                    attrs = _attrs_to_dict(dp.get("attributes", []))
                    if "sum" in dp or "count" in dp:      # histogram point
                        value = dp.get("sum")
                    elif "asDouble" in dp:
                        value = dp.get("asDouble")
                    else:
                        value = _attr_value({"intValue": dp.get("asInt")}) \
                            if dp.get("asInt") is not None else None
                    event = Event(
                        phase=Phase.OTEL_METRIC,
                        session_id=_session_id(attrs),
                        meta={"name": name, "value": value, "unit": unit,
                              "attributes": attrs},
                    )
                    ts = _ts_from_nano(dp.get("timeUnixNano"))
                    if ts:
                        event.ts = ts
                    events.append(event)
    return events


def parse_logs_payload(payload: dict) -> list[Event]:
    """Map an OTLP ExportLogsServiceRequest (JSON) to otel_event events."""
    events = []
    for rl in payload.get("resourceLogs", []):
        for sl in rl.get("scopeLogs", []):
            for rec in sl.get("logRecords", []):
                attrs = _attrs_to_dict(rec.get("attributes", []))
                body = _attr_value(rec.get("body", {}))
                name = (attrs.get("event.name")
                        or (body if isinstance(body, str) else None)
                        or "log")
                event = Event(
                    phase=Phase.OTEL_EVENT,
                    session_id=_session_id(attrs),
                    error=str(attrs.get("error"))[:500]
                          if attrs.get("error") else None,
                    meta={"name": name, "body": body, "attributes": attrs,
                          "severity": rec.get("severityText")},
                )
                ts = _ts_from_nano(rec.get("timeUnixNano"))
                if ts:
                    event.ts = ts
                events.append(event)
    return events


# ------------------------------------------------------------------ #
# HTTP server                                                          #
# ------------------------------------------------------------------ #

class OTLPReceiver:
    """Accepts OTLP/HTTP JSON exports on /v1/metrics and /v1/logs."""

    def __init__(
        self,
        emit: Callable[[Event], None],
        host: str = "127.0.0.1",
        port: int = 4318,
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
            path, headers, body = await asyncio.wait_for(
                _read_request(reader), timeout=10.0
            )
            if headers.get("content-encoding") == "gzip":
                body = gzip.decompress(body)
            payload = json.loads(body) if body else {}

            if path == "/v1/metrics":
                events = parse_metrics_payload(payload)
            elif path == "/v1/logs":
                events = parse_logs_payload(payload)
            elif path == "/v1/traces":
                events = []          # accepted but not mapped (yet)
            else:
                writer.write(_BAD)
                return
            for e in events:
                self._emit(e)
            _dlog(f"{path}: {len(events)} events")
            writer.write(_OK)
        except Exception as exc:
            _dlog(f"error: {exc!r}")
            writer.write(_BAD)
        finally:
            try:
                await writer.drain()
                writer.close()
            except Exception:
                pass


async def _read_request(
    reader: asyncio.StreamReader,
) -> tuple[str, dict, bytes]:
    """Read one HTTP/1.x request honouring Content-Length (OTLP payloads
    regularly exceed a single read)."""
    raw = b""
    while b"\r\n\r\n" not in raw:
        chunk = await reader.read(65_536)
        if not chunk:
            break
        raw += chunk
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    first = lines[0].decode("ascii", errors="replace").split()
    path = first[1].split("?", 1)[0] if len(first) >= 2 else ""
    headers = {}
    for line in lines[1:]:
        k, _, v = line.decode("latin-1", errors="replace").partition(":")
        headers[k.strip().lower()] = v.strip()
    want = int(headers.get("content-length", 0) or 0)
    while len(body) < want:
        chunk = await reader.read(want - len(body))
        if not chunk:
            break
        body += chunk
    return path, headers, body
