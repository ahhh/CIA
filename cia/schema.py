from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class Phase(str, Enum):
    # API lifecycle
    API_REQUEST_START    = "api_request_start"
    API_FIRST_TOKEN      = "api_first_token"
    API_THINKING_START   = "api_thinking_start"
    API_THINKING_END     = "api_thinking_end"
    API_GENERATION_START = "api_generation_start"
    API_GENERATION_END   = "api_generation_end"
    API_RESPONSE_END     = "api_response_end"
    API_REQUEST_ERROR    = "api_request_error"

    # Tokenizer (server-side token counting via /v1/messages/count_tokens)
    TOKENIZER_START = "tokenizer_start"
    TOKENIZER_END   = "tokenizer_end"

    # Tool calls (from Claude Code hooks)
    TOOL_CALL_START  = "tool_call_start"
    TOOL_CALL_END    = "tool_call_end"
    TOOL_CALL_ERROR  = "tool_call_error"

    # File system (from fswatch)
    FILE_CHANGE = "file_change"

    # Session / agent lifecycle (from Claude Code hooks)
    PROCESS_SPAWN   = "process_spawn"
    SESSION_START   = "session_start"
    USER_PROMPT     = "user_prompt"
    TURN_END        = "turn_end"          # assistant finished one response (Stop)
    SUBAGENT_END    = "subagent_end"      # a Task subagent finished (SubagentStop)
    CONTEXT_COMPACT = "context_compact"   # context being compacted (PreCompact)
    NOTIFICATION    = "notification"      # Claude waiting / permission (Notification)
    SESSION_END     = "session_end"       # session actually ended (SessionEnd)


@dataclass
class Event:
    phase: Phase
    ts: float                         = field(default_factory=time.time)
    id: str                           = field(default_factory=lambda: f"evt_{uuid.uuid4().hex[:16]}")
    session_id: Optional[str]         = None
    pid: Optional[int]                = None
    duration_ms: Optional[float]      = None
    tool: Optional[str]               = None
    tool_input: Optional[dict]        = None
    model: Optional[str]              = None
    tokens_input: Optional[int]       = None
    tokens_output: Optional[int]      = None
    thinking_tokens: Optional[int]    = None
    error: Optional[str]              = None
    meta: dict[str, Any]              = field(default_factory=dict)

    # ------------------------------------------------------------------ #
    # Serialisation                                                        #
    # ------------------------------------------------------------------ #

    def to_dict(self) -> dict:
        d = asdict(self)
        d["phase"] = self.phase.value
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        d = d.copy()
        d["phase"] = Phase(d["phase"])
        return cls(**d)

    # ------------------------------------------------------------------ #
    # Factory helpers                                                      #
    # ------------------------------------------------------------------ #

    @classmethod
    def from_hook_payload(cls, phase: Phase, payload: dict) -> "Event":
        """Build an Event from the JSON that Claude Code POSTs to a hook."""
        meta = {k: v for k, v in payload.items()
                if k not in ("session_id", "tool_name", "tool_input",
                              "tool_response", "hook_event_name")}
        result = _summarize_tool_response(phase, payload)
        if result is not None:
            meta["tool_result"] = result
        return cls(
            phase=phase,
            session_id=payload.get("session_id"),
            tool=payload.get("tool_name"),
            tool_input=payload.get("tool_input"),
            error=_extract_hook_error(phase, payload),
            meta=meta,
        )


def _summarize_tool_response(phase: Phase, payload: dict) -> Optional[dict]:
    """Compact summary of a PostToolUse tool_response: success + output size."""
    if phase != Phase.TOOL_CALL_END:
        return None
    resp = payload.get("tool_response")
    if resp is None:
        return None
    summary: dict[str, Any] = {}
    if isinstance(resp, dict):
        if "is_error" in resp:
            summary["is_error"] = bool(resp.get("is_error"))
        if resp.get("interrupted"):
            summary["interrupted"] = True
        try:
            summary["output_bytes"] = len(json.dumps(resp, default=str))
        except Exception:
            summary["output_bytes"] = len(str(resp))
    elif isinstance(resp, str):
        summary["output_bytes"] = len(resp)
        summary["is_error"] = False
    else:
        summary["output_bytes"] = len(str(resp))
    return summary or None


def _extract_hook_error(phase: Phase, payload: dict) -> Optional[str]:
    if phase != Phase.TOOL_CALL_END:
        return None
    resp = payload.get("tool_response", {})
    if isinstance(resp, dict) and resp.get("is_error"):
        content = resp.get("content", "")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
        return str(content)[:500] if content else "tool_error"
    return None
