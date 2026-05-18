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
    API_THINKING_START   = "api_thinking_start"
    API_THINKING_END     = "api_thinking_end"
    API_GENERATION_START = "api_generation_start"
    API_RESPONSE_END     = "api_response_end"
    API_REQUEST_ERROR    = "api_request_error"

    # Tool calls (from Claude Code hooks)
    TOOL_CALL_START  = "tool_call_start"
    TOOL_CALL_END    = "tool_call_end"
    TOOL_CALL_ERROR  = "tool_call_error"

    # File system (from fswatch)
    FILE_CHANGE = "file_change"

    # Process tracking
    PROCESS_SPAWN = "process_spawn"
    SESSION_START = "session_start"
    SESSION_END   = "session_end"


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
        return cls(
            phase=phase,
            session_id=payload.get("session_id"),
            tool=payload.get("tool_name"),
            tool_input=payload.get("tool_input"),
            error=_extract_hook_error(phase, payload),
            meta={k: v for k, v in payload.items()
                  if k not in ("session_id", "tool_name", "tool_input",
                                "tool_response", "hook_event_name")},
        )


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
