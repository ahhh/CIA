"""Unit tests for CIAAddon: tokenizer (count_tokens) and non-streaming API timing."""
from __future__ import annotations

import json

from mitmproxy.test import tflow, tutils

from cia.proxy import CIAAddon
from cia.schema import Event, Phase


def _make_flow(path: str, req_body: dict, resp_body: dict | None = None,
               status: int = 200, host: str = "api.anthropic.com",
               sse: bool = False):
    req = tutils.treq(
        host=host, port=443, method=b"POST", path=path,
        content=json.dumps(req_body).encode(),
    )
    resp = tutils.tresp(
        status_code=status,
        content=json.dumps(resp_body or {}).encode(),
    )
    flow = tflow.tflow(req=req, resp=resp)
    flow.response.headers["content-type"] = (
        "text/event-stream" if sse else "application/json"
    )
    return flow


def _run(flow) -> list[Event]:
    events: list[Event] = []
    addon = CIAAddon(events.append)
    addon.request(flow)
    addon.responseheaders(flow)
    addon.response(flow)
    return events


def test_count_tokens_emits_tokenizer_start_and_end():
    flow = _make_flow(
        "/v1/messages/count_tokens",
        req_body={"model": "claude-sonnet-4-6", "messages": []},
        resp_body={"input_tokens": 1234},
    )
    events = _run(flow)

    assert [e.phase for e in events] == [Phase.TOKENIZER_START, Phase.TOKENIZER_END]
    start, end = events
    assert start.model == "claude-sonnet-4-6"
    assert end.model == "claude-sonnet-4-6"
    assert end.tokens_input == 1234
    assert end.duration_ms is not None and end.duration_ms >= 0
    assert end.ts >= start.ts


def test_non_streaming_messages_emits_request_start_and_response_end():
    flow = _make_flow(
        "/v1/messages",
        req_body={"model": "claude-haiku-4-5-20251001", "messages": [], "stream": False},
        resp_body={
            "id": "msg_abc",
            "model": "claude-haiku-4-5-20251001",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 50, "output_tokens": 7,
                      "cache_read_input_tokens": 32},
        },
    )
    events = _run(flow)

    assert [e.phase for e in events] == [Phase.API_REQUEST_START, Phase.API_RESPONSE_END]
    start, end = events
    assert start.model == "claude-haiku-4-5-20251001"
    assert start.tokens_input == 50
    assert start.meta["streaming"] is False
    assert end.tokens_output == 7
    assert end.duration_ms is not None and end.duration_ms >= 0
    assert end.meta["stop_reason"] == "end_turn"
    assert end.meta["usage"]["cache_read_input_tokens"] == 32
    # request_start ts is backdated to when the request left the client
    assert start.ts <= end.ts


def test_query_string_is_ignored_when_matching_endpoint():
    flow = _make_flow(
        "/v1/messages/count_tokens?beta=true",
        req_body={"model": "m"},
        resp_body={"input_tokens": 9},
    )
    events = _run(flow)
    assert [e.phase for e in events] == [Phase.TOKENIZER_START, Phase.TOKENIZER_END]


def test_non_anthropic_host_emits_network_request():
    flow = _make_flow("/v1/initialize", req_body={},
                      resp_body={}, host="statsig.anthropic.com")
    events = _run(flow)
    assert [e.phase for e in events] == [Phase.NETWORK_REQUEST]
    evt = events[0]
    assert evt.meta["host"] == "statsig.anthropic.com"
    assert evt.meta["category"] == "feature_flags"
    assert evt.meta["status"] == 200
    assert evt.error is None
    assert evt.duration_ms is not None and evt.duration_ms >= 0


def test_unknown_host_is_still_reported():
    flow = _make_flow("/whatever", req_body={}, resp_body={}, host="example.com")
    events = _run(flow)
    assert [e.phase for e in events] == [Phase.NETWORK_REQUEST]
    assert events[0].meta["category"] == "unknown"


def test_boot_checkin_error_emits_network_request_with_purpose():
    # The /api/claude_cli_profile 400 seen on every Claude Code boot: it must
    # surface as a categorised check-in, not as an inference api_request_error.
    flow = _make_flow(
        "/api/claude_cli_profile?account_uuid=abc",
        req_body={},
        resp_body={"type": "error",
                   "error": {"message": "API key creator does not match"}},
        status=400,
    )
    events = _run(flow)
    assert [e.phase for e in events] == [Phase.NETWORK_REQUEST]
    evt = events[0]
    assert evt.error == "HTTP 400"
    assert evt.meta["category"] == "account"
    assert "profile" in evt.meta["purpose"]
    assert "does not match" in evt.meta["body"]


def test_settings_checkin_404_is_categorised_config():
    flow = _make_flow("/api/claude_code/settings", req_body={},
                      resp_body={"type": "error"}, status=404)
    events = _run(flow)
    assert [e.phase for e in events] == [Phase.NETWORK_REQUEST]
    assert events[0].meta["category"] == "config"
    assert "404" in events[0].meta["purpose"] or "settings" in events[0].meta["purpose"]


def test_sse_response_does_not_emit_json_roundtrip_events():
    flow = _make_flow("/v1/messages", req_body={"model": "m", "stream": True},
                      resp_body={}, sse=True)
    events = _run(flow)
    # The SSE parser owns streaming flows; the JSON roundtrip path must not
    # double-emit api_request_start / api_response_end.
    assert all(e.phase not in (Phase.API_REQUEST_START, Phase.API_RESPONSE_END,
                               Phase.TOKENIZER_START, Phase.TOKENIZER_END)
               for e in events)


def test_http_error_emits_api_request_error_only():
    flow = _make_flow("/v1/messages", req_body={"model": "m"},
                      resp_body={"error": {"message": "bad request"}}, status=400)
    events = _run(flow)
    assert [e.phase for e in events] == [Phase.API_REQUEST_ERROR]
    assert events[0].error == "HTTP 400"


def test_unparseable_request_body_still_times_count_tokens():
    req = tutils.treq(host="api.anthropic.com", port=443, method=b"POST",
                      path="/v1/messages/count_tokens", content=b"\x00not json")
    resp = tutils.tresp(status_code=200, content=b'{"input_tokens": 3}')
    flow = tflow.tflow(req=req, resp=resp)
    flow.response.headers["content-type"] = "application/json"
    events = _run(flow)

    assert [e.phase for e in events] == [Phase.TOKENIZER_START, Phase.TOKENIZER_END]
    assert events[0].model is None
    assert events[1].tokens_input == 3
