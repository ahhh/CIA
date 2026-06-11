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
