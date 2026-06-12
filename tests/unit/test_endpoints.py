"""Unit tests for the network-endpoint catalog."""
from __future__ import annotations

from cia.endpoints import classify_endpoint, is_inference_traffic


def test_inference_and_tokenizer():
    assert classify_endpoint("api.anthropic.com", "/v1/messages")["category"] == "inference"
    assert classify_endpoint("api.anthropic.com",
                             "/v1/messages/count_tokens")["category"] == "tokenizer"
    assert is_inference_traffic("inference")
    assert is_inference_traffic("tokenizer")
    assert not is_inference_traffic("config")


def test_query_string_is_ignored():
    kind = classify_endpoint("api.anthropic.com",
                             "/api/claude_cli_profile?account_uuid=x")
    assert kind["category"] == "account"


def test_boot_checkins():
    assert classify_endpoint("api.anthropic.com", "/api/hello")["category"] == "health"
    assert classify_endpoint("api.anthropic.com",
                             "/api/claude_code/settings")["category"] == "config"
    assert classify_endpoint("api.anthropic.com",
                             "/api/oauth/profile")["category"] == "account"


def test_telemetry_hosts():
    assert classify_endpoint("statsig.anthropic.com",
                             "/v1/initialize")["category"] == "feature_flags"
    assert classify_endpoint("statsig.anthropic.com",
                             "/v1/rgstr")["category"] == "telemetry"
    # subdomain suffix match
    assert classify_endpoint("o1158394.ingest.us.sentry.io",
                             "/api/123/envelope/")["category"] == "error_reporting"


def test_suffix_match_requires_label_boundary():
    # evilanthropic.com must not match anthropic.com endpoints
    assert classify_endpoint("evil-api.anthropic.com.attacker.net",
                             "/v1/messages")["category"] == "unknown"
    assert classify_endpoint("notclaude.ai", "/x")["category"] == "unknown"


def test_unknown_endpoint():
    kind = classify_endpoint("example.com", "/anything")
    assert kind["category"] == "unknown"
    assert kind["purpose"]


def test_anthropic_unrecognized_path_falls_back():
    assert classify_endpoint("api.anthropic.com",
                             "/api/some_new_thing")["category"] == "api_other"
