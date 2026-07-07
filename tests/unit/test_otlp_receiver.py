"""Unit tests for the OTLP receiver: payload mapping and HTTP roundtrip."""
from __future__ import annotations

import asyncio
import json

import pytest

from cia.otlp_receiver import (
    OTLPReceiver,
    parse_logs_payload,
    parse_metrics_payload,
    parse_traces_payload,
)
from cia.schema import Phase

NANO = 1_716_000_000_000_000_000  # 2024-05-18T03:20:00Z in ns


def metrics_payload() -> dict:
    return {
        "resourceMetrics": [{
            "scopeMetrics": [{
                "metrics": [
                    {
                        "name": "claude_code.token.usage",
                        "unit": "tokens",
                        "sum": {"dataPoints": [{
                            "asInt": "892",
                            "timeUnixNano": str(NANO),
                            "attributes": [
                                {"key": "session.id",
                                 "value": {"stringValue": "sess-42"}},
                                {"key": "type",
                                 "value": {"stringValue": "output"}},
                                {"key": "model",
                                 "value": {"stringValue": "claude-sonnet-4-6"}},
                            ],
                        }]},
                    },
                    {
                        "name": "claude_code.cost.usage",
                        "unit": "USD",
                        "sum": {"dataPoints": [{
                            "asDouble": 0.0731,
                            "timeUnixNano": str(NANO),
                            "attributes": [
                                {"key": "session.id",
                                 "value": {"stringValue": "sess-42"}},
                            ],
                        }]},
                    },
                ],
            }],
        }],
    }


def logs_payload() -> dict:
    return {
        "resourceLogs": [{
            "scopeLogs": [{
                "logRecords": [{
                    "timeUnixNano": str(NANO),
                    "severityText": "INFO",
                    "body": {"stringValue": "claude_code.api_request"},
                    "attributes": [
                        {"key": "event.name",
                         "value": {"stringValue": "api_request"}},
                        {"key": "session.id",
                         "value": {"stringValue": "sess-42"}},
                        {"key": "duration_ms", "value": {"intValue": "2410"}},
                    ],
                }],
            }],
        }],
    }


def test_parse_metrics_maps_values_sessions_and_time():
    events = parse_metrics_payload(metrics_payload())
    assert len(events) == 2
    tok, cost = events
    assert tok.phase == Phase.OTEL_METRIC
    assert tok.session_id == "sess-42"
    assert tok.ts == NANO / 1e9
    assert tok.meta["name"] == "claude_code.token.usage"
    assert tok.meta["value"] == 892
    assert tok.meta["attributes"]["type"] == "output"
    assert cost.meta["value"] == pytest.approx(0.0731)
    assert cost.meta["unit"] == "USD"


def test_parse_logs_maps_event_name_and_attributes():
    events = parse_logs_payload(logs_payload())
    assert len(events) == 1
    e = events[0]
    assert e.phase == Phase.OTEL_EVENT
    assert e.session_id == "sess-42"
    assert e.meta["name"] == "api_request"
    assert e.meta["attributes"]["duration_ms"] == 2410
    assert e.meta["severity"] == "INFO"


def test_empty_payloads_yield_no_events():
    assert parse_metrics_payload({}) == []
    assert parse_logs_payload({}) == []
    assert parse_traces_payload({}) == []


def test_parse_metrics_records_aggregation_temporality():
    payload = metrics_payload()
    metric = payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]
    metric["sum"]["aggregationTemporality"] = 1          # numeric delta
    events = parse_metrics_payload(payload)
    assert events[0].meta["temporality"] == "delta"
    # second metric had no temporality → key absent
    assert "temporality" not in events[1].meta

    metric["sum"]["aggregationTemporality"] = \
        "AGGREGATION_TEMPORALITY_CUMULATIVE"             # string form
    events = parse_metrics_payload(payload)
    assert events[0].meta["temporality"] == "cumulative"


def traces_payload() -> dict:
    return {
        "resourceSpans": [{
            "scopeSpans": [{
                "spans": [{
                    "name": "query",
                    "traceId": "aaaa",
                    "spanId": "bbbb",
                    "parentSpanId": "cccc",
                    "startTimeUnixNano": str(NANO),
                    "endTimeUnixNano": str(NANO + 1_500_000_000),
                    "status": {"code": 2, "message": "boom"},
                    "attributes": [
                        {"key": "session.id",
                         "value": {"stringValue": "sess-42"}},
                    ],
                }],
            }],
        }],
    }


def test_parse_traces_maps_spans_with_duration_and_error():
    events = parse_traces_payload(traces_payload())
    assert len(events) == 1
    e = events[0]
    assert e.phase == Phase.OTEL_SPAN
    assert e.session_id == "sess-42"
    assert e.ts == NANO / 1e9
    assert e.duration_ms == pytest.approx(1500.0)
    assert e.meta["name"] == "query"
    assert e.meta["parent_span_id"] == "cccc"
    assert e.error == "boom"                    # STATUS_CODE_ERROR


def test_parse_traces_ok_span_has_no_error():
    payload = traces_payload()
    payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["status"] = {}
    events = parse_traces_payload(payload)
    assert events[0].error is None


@pytest.mark.asyncio
async def test_http_roundtrip_emits_events():
    received = []
    receiver = OTLPReceiver(received.append, port=0)
    server = await asyncio.start_server(receiver._handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    body = json.dumps(metrics_payload()).encode()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(
        b"POST /v1/metrics HTTP/1.1\r\nHost: x\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode() + body
    )
    await writer.drain()
    response = await reader.read(4096)
    writer.close()
    server.close()
    await server.wait_closed()

    assert b"200 OK" in response
    assert len(received) == 2
    assert received[0].meta["name"] == "claude_code.token.usage"


@pytest.mark.asyncio
async def test_unknown_path_is_rejected():
    received = []
    receiver = OTLPReceiver(received.append, port=0)
    server = await asyncio.start_server(receiver._handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"POST /v1/nope HTTP/1.1\r\nContent-Length: 2\r\n\r\n{}")
    await writer.drain()
    response = await reader.read(4096)
    writer.close()
    server.close()
    await server.wait_closed()

    assert b"400" in response
    assert received == []
