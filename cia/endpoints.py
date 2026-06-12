"""
Catalog of network endpoints Claude Code is known to contact, so every
outbound request observed by the proxy can be tagged with *why* it happened.

Claude Code talks to more than the inference API: on boot it probes
connectivity, fetches remotely-managed settings, syncs its account profile
and feature gates, and throughout a session it ships telemetry to Statsig
and crash reports to Sentry.  ``classify_endpoint`` maps (host, path) to a
category + human-readable purpose; unrecognized traffic is still reported,
just tagged ``unknown``.
"""
from __future__ import annotations

# (host suffix, path prefix, category, purpose) — first match wins.
# A path prefix of "" matches any path on that host.
_CATALOG: list[tuple[str, str, str, str]] = [
    # --- inference & tokenizer (covered by dedicated api_* events) -------
    ("api.anthropic.com", "/v1/messages/count_tokens", "tokenizer",
     "server-side token counting for context-window management"),
    ("api.anthropic.com", "/v1/messages", "inference",
     "model inference call (the actual conversation)"),
    ("api.anthropic.com", "/v1/complete", "inference",
     "legacy text-completion inference call"),

    # --- boot / housekeeping check-ins -----------------------------------
    ("api.anthropic.com", "/api/hello", "health",
     "connectivity probe: verifies the API is reachable at startup"),
    ("api.anthropic.com", "/api/claude_code/settings", "config",
     "fetch remotely-managed Claude Code settings/policies "
     "(404 = none configured for this account)"),
    ("api.anthropic.com", "/api/claude_cli_profile", "account",
     "sync CLI account profile and feature gates"),
    ("api.anthropic.com", "/api/oauth/profile", "account",
     "fetch OAuth account profile (plan, organization)"),
    ("api.anthropic.com", "/api/oauth/claude_cli/roles", "account",
     "check subscription roles/entitlements for the CLI"),
    ("api.anthropic.com", "/api/oauth/usage", "account",
     "fetch usage/quota status"),
    ("api.anthropic.com", "/api/oauth", "auth",
     "OAuth flow (token exchange/refresh)"),
    ("api.anthropic.com", "/api/claude_code", "config",
     "Claude Code backend service call"),
    ("api.anthropic.com", "", "api_other",
     "Anthropic API call (unrecognized path)"),
    ("console.anthropic.com", "/v1/oauth/token", "auth",
     "OAuth token refresh"),
    ("console.anthropic.com", "", "auth",
     "Anthropic Console (account/billing/auth)"),

    # --- telemetry & diagnostics -----------------------------------------
    ("statsig.anthropic.com", "/v1/initialize", "feature_flags",
     "Statsig: fetch feature flags / experiment assignments"),
    ("statsig.anthropic.com", "/v1/rgstr", "telemetry",
     "Statsig: log gate-exposure / usage events"),
    ("statsig.anthropic.com", "/v1/log_event", "telemetry",
     "Statsig: batch telemetry event upload"),
    ("statsig.anthropic.com", "", "telemetry",
     "Statsig feature-flag / telemetry traffic"),
    ("statsigapi.net", "", "telemetry",
     "Statsig feature-flag / telemetry traffic (direct)"),
    ("sentry.io", "", "error_reporting",
     "Sentry: crash / internal-error report upload"),

    # --- updates -----------------------------------------------------------
    ("registry.npmjs.org", "", "update",
     "npm registry: check for / download Claude Code updates"),
    ("storage.googleapis.com", "", "update",
     "GCS download (Claude Code native build / assets)"),

    # --- claude.ai web backend ---------------------------------------------
    ("claude.ai", "", "account",
     "claude.ai web backend (subscription auth / sync)"),
]

_UNKNOWN = ("unknown", "unrecognized endpoint")


def classify_endpoint(host: str, path: str) -> dict[str, str]:
    """Return ``{"category", "purpose"}`` for a request to host+path.

    ``path`` may include a query string; it is ignored for matching.
    """
    host = (host or "").lower()
    path = (path or "").split("?", 1)[0]
    for host_suffix, prefix, category, purpose in _CATALOG:
        if (host == host_suffix or host.endswith("." + host_suffix)) \
                and path.startswith(prefix):
            return {"category": category, "purpose": purpose}
    return {"category": _UNKNOWN[0], "purpose": _UNKNOWN[1]}


def is_inference_traffic(category: str) -> bool:
    """True for flows already covered by the dedicated api_*/tokenizer_*
    events, which network_request must not duplicate."""
    return category in ("inference", "tokenizer")
