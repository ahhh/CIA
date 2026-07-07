"""Unit tests for the `cia run` environment wiring."""
from pathlib import Path

from cia.cli import _build_run_env


def test_run_env_routes_proxy_and_otlp():
    env = _build_run_env(proxy_port=9999, otlp_port=4444,
                         cert=Path("/tmp/ca.pem"))
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:9999"
    assert env["NODE_EXTRA_CA_CERTS"] == "/tmp/ca.pem"
    assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:4444"
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/json"
    assert env["OTEL_METRICS_EXPORTER"] == "otlp"
    assert env["OTEL_LOGS_EXPORTER"] == "otlp"


def test_run_env_requests_delta_temporality():
    env = _build_run_env()
    assert env["OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"] == "delta"


def test_run_env_detail_and_trace_are_opt_in():
    env = _build_run_env()
    assert "OTEL_LOG_TOOL_DETAILS" not in env
    assert "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA" not in env
    assert "OTEL_TRACES_EXPORTER" not in env

    env = _build_run_env(detail=True, trace=True)
    assert env["OTEL_LOG_TOOL_DETAILS"] == "1"
    assert env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] == "1"
    assert env["OTEL_TRACES_EXPORTER"] == "otlp"
