"""
Derived performance analytics computed from recorded CIA events.

All functions take a time-sorted list of Event objects (as returned by
Store.query, or parsed from an exported JSONL file) and return plain
dicts/lists so results can be rendered as tables or dumped as JSON.

Correlation caveat: proxy-derived events (api_*) carry no session_id, so
turn-level attribution matches them to hook events by time window.  With
multiple concurrent Claude sessions, one session's api_* events can land
in another session's turn; single-session captures are exact.
"""
from __future__ import annotations

from typing import Any, Optional

from cia.schema import Event, Phase

_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

# Notification messages containing one of these are treated as permission
# prompts (resolved by the next tool call); everything else is treated as
# "waiting for user input" (resolved by the next prompt).
_PERMISSION_MARKERS = ("permission", "approve")


# ------------------------------------------------------------------ #
# Shared helpers                                                       #
# ------------------------------------------------------------------ #

def _percentile(sorted_vals: list[float], p: float) -> Optional[float]:
    """Linear-interpolated percentile; expects pre-sorted values."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p / 100.0
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _context_tokens(e: Event) -> int:
    """Full context size of an api_request_start: fresh + cached input."""
    meta = e.meta or {}
    usage = meta.get("usage") or {}
    return (
        (e.tokens_input or 0)
        + (meta.get("cache_read_input_tokens") or usage.get("cache_read_input_tokens") or 0)
        + (meta.get("cache_creation_input_tokens") or usage.get("cache_creation_input_tokens") or 0)
    )


def pair_tool_calls(events: list[Event]) -> list[dict]:
    """Match tool_call_start/tool_call_end into completed calls.

    Pairs by meta.tool_use_id when both sides carry it, falling back to the
    most recent unmatched start with the same (session_id, tool).  Starts
    that never see an end (still running, or the PostToolUse hook was lost)
    are dropped.
    """
    by_use_id: dict[str, Event] = {}
    by_key: dict[tuple, list[Event]] = {}
    pairs: list[dict] = []

    for e in events:
        if e.phase == Phase.TOOL_CALL_START:
            use_id = (e.meta or {}).get("tool_use_id")
            if use_id:
                by_use_id[use_id] = e
            else:
                by_key.setdefault((e.session_id, e.tool), []).append(e)
        elif e.phase in (Phase.TOOL_CALL_END, Phase.TOOL_CALL_ERROR):
            use_id = (e.meta or {}).get("tool_use_id")
            start = by_use_id.pop(use_id, None) if use_id else None
            if start is None:
                stack = by_key.get((e.session_id, e.tool))
                start = stack.pop() if stack else None
            if start is None:
                continue
            result = (e.meta or {}).get("tool_result") or {}
            tool_input = start.tool_input or {}
            pairs.append({
                "tool": e.tool,
                "session_id": e.session_id,
                "start_ts": start.ts,
                "end_ts": e.ts,
                "duration_ms": (e.ts - start.ts) * 1000,
                "is_error": bool(e.error) or bool(result.get("is_error")),
                "output_bytes": result.get("output_bytes"),
                "file_path": tool_input.get("file_path"),
            })
    pairs.sort(key=lambda p: p["start_ts"])
    return pairs


# ------------------------------------------------------------------ #
# Per-tool performance profiles                                        #
# ------------------------------------------------------------------ #

def tool_profiles(events: list[Event]) -> list[dict]:
    """Duration percentiles, error rates and output sizes per tool."""
    groups: dict[str, list[dict]] = {}
    for p in pair_tool_calls(events):
        groups.setdefault(p["tool"] or "?", []).append(p)

    profiles = []
    for tool, calls in sorted(groups.items()):
        durations = sorted(c["duration_ms"] for c in calls)
        errors = sum(1 for c in calls if c["is_error"])
        sizes = [c["output_bytes"] for c in calls if c["output_bytes"] is not None]
        profiles.append({
            "tool": tool,
            "calls": len(calls),
            "errors": errors,
            "error_rate": errors / len(calls),
            "p50_ms": _percentile(durations, 50),
            "p90_ms": _percentile(durations, 90),
            "p99_ms": _percentile(durations, 99),
            "max_ms": durations[-1],
            "total_ms": sum(durations),
            "avg_output_bytes": (sum(sizes) / len(sizes)) if sizes else None,
        })
    profiles.sort(key=lambda p: p["total_ms"], reverse=True)
    return profiles


# ------------------------------------------------------------------ #
# Human latency                                                        #
# ------------------------------------------------------------------ #

def human_latency(events: list[Event]) -> dict:
    """Time burned waiting on the human.

    permission_waits: notification mentioning permission → next
    tool_call_start in the same session (the user approved), or next
    user_prompt (the user denied / redirected — resolution "prompt").
    input_waits: other notifications → next user_prompt.
    think_times: turn_end → next user_prompt in the same session.
    """
    permission_waits: list[dict] = []
    input_waits: list[dict] = []
    think_times: list[dict] = []

    sessions = {e.session_id for e in events if e.session_id}
    for sid in sessions:
        sev = [e for e in events if e.session_id == sid]
        for i, e in enumerate(sev):
            if e.phase == Phase.NOTIFICATION:
                message = ((e.meta or {}).get("message") or "").lower()
                is_permission = any(m in message for m in _PERMISSION_MARKERS)
                for nxt in sev[i + 1:]:
                    if is_permission and nxt.phase == Phase.TOOL_CALL_START:
                        permission_waits.append({
                            "session_id": sid, "ts": e.ts,
                            "wait_s": nxt.ts - e.ts, "resolution": "approved",
                            "tool": nxt.tool,
                        })
                        break
                    if nxt.phase == Phase.USER_PROMPT:
                        (permission_waits if is_permission else input_waits).append({
                            "session_id": sid, "ts": e.ts,
                            "wait_s": nxt.ts - e.ts, "resolution": "prompt",
                            "tool": None,
                        })
                        break
            elif e.phase == Phase.TURN_END:
                for nxt in sev[i + 1:]:
                    if nxt.phase == Phase.USER_PROMPT:
                        think_times.append({
                            "session_id": sid, "ts": e.ts,
                            "wait_s": nxt.ts - e.ts,
                        })
                        break
                    if nxt.phase == Phase.SESSION_END:
                        break

    def _summary(waits: list[dict]) -> dict:
        vals = sorted(w["wait_s"] for w in waits)
        return {
            "count": len(vals),
            "total_s": sum(vals),
            "mean_s": (sum(vals) / len(vals)) if vals else None,
            "p50_s": _percentile(vals, 50),
            "max_s": vals[-1] if vals else None,
        }

    return {
        "permission_waits": permission_waits,
        "input_waits": input_waits,
        "think_times": think_times,
        "summary": {
            "permission": _summary(permission_waits),
            "input": _summary(input_waits),
            "think": _summary(think_times),
        },
    }


# ------------------------------------------------------------------ #
# Compaction cost                                                      #
# ------------------------------------------------------------------ #

def compaction_cost(events: list[Event], window_s: float = 600.0,
                    lookahead: int = 5) -> list[dict]:
    """Context reclaimed by each compaction.

    context_before is the last api_request_start before the PreCompact
    hook.  The first request *after* compaction is often the summarisation
    call itself (still large), so context_after takes the minimum context
    over the next `lookahead` requests within `window_s`.
    """
    starts = [(e.ts, _context_tokens(e)) for e in events
              if e.phase == Phase.API_REQUEST_START]
    results = []
    for e in events:
        if e.phase != Phase.CONTEXT_COMPACT:
            continue
        before = [c for ts, c in starts if ts <= e.ts]
        after = [(ts, c) for ts, c in starts if e.ts < ts <= e.ts + window_s][:lookahead]
        context_before = before[-1] if before else None
        context_after = min((c for _, c in after), default=None)
        results.append({
            "ts": e.ts,
            "session_id": e.session_id,
            "trigger": (e.meta or {}).get("trigger"),
            "context_before": context_before,
            "context_after": context_after,
            "reclaimed_tokens": (
                context_before - context_after
                if context_before is not None and context_after is not None
                else None
            ),
            "recovery_s": (after[0][0] - e.ts) if after else None,
        })
    return results


# ------------------------------------------------------------------ #
# Turn anatomy                                                         #
# ------------------------------------------------------------------ #

def turn_anatomy(events: list[Event]) -> list[dict]:
    """Break each turn's wall-clock into model / tool / human components.

    A turn is user_prompt → the next turn_end in the same session.  api_*
    events are attributed by time window (see module docstring), tool calls
    and permission waits by session + time window.

    A turn still open at the end of the capture (no turn_end yet — in
    progress, or the session died) is closed at the last event seen for
    its session and marked ``complete: False`` rather than dropped.
    """
    pairs = pair_tool_calls(events)
    hl = human_latency(events)

    turns: list[dict] = []
    sessions = {e.session_id for e in events if e.session_id}
    for sid in sessions:
        sev = [e for e in events if e.session_id == sid]
        open_prompt: Optional[Event] = None
        for e in sev:
            if e.phase == Phase.USER_PROMPT and open_prompt is None:
                open_prompt = e
            elif e.phase == Phase.TURN_END and open_prompt is not None:
                turns.append(_one_turn(open_prompt, e, events, pairs, hl))
                open_prompt = None
        if open_prompt is not None and sev[-1].ts > open_prompt.ts:
            turn = _one_turn(open_prompt, sev[-1], events, pairs, hl)
            turn["complete"] = False
            turns.append(turn)

    turns.sort(key=lambda t: t["start_ts"])
    return turns


def _one_turn(prompt: Event, end: Event, events: list[Event],
              pairs: list[dict], hl: dict) -> dict:
    sid = prompt.session_id
    t0, t1 = prompt.ts, end.ts

    def in_window(ts: float) -> bool:
        return t0 <= ts <= t1

    api_ends = [e for e in events
                if e.phase == Phase.API_RESPONSE_END and in_window(e.ts)]
    api_ms = sum(e.duration_ms or 0 for e in api_ends)
    thinking_ms = sum(e.duration_ms or 0 for e in events
                      if e.phase == Phase.API_THINKING_END and in_window(e.ts))
    generation_ms = sum(e.duration_ms or 0 for e in events
                        if e.phase == Phase.API_GENERATION_END and in_window(e.ts))
    tokens_out = sum(e.tokens_output or 0 for e in api_ends)

    req_starts = [e for e in events
                  if e.phase == Phase.API_REQUEST_START and in_window(e.ts)]
    context_tokens = _context_tokens(req_starts[-1]) if req_starts else None

    turn_pairs = [p for p in pairs
                  if p["session_id"] == sid and in_window(p["start_ts"])]
    tool_ms = sum(p["duration_ms"] for p in turn_pairs)

    permission_s = sum(
        w["wait_s"] for w in hl["permission_waits"]
        if w["session_id"] == sid and in_window(w["ts"])
    )

    edits = sum(1 for p in turn_pairs
                if p["tool"] in _EDIT_TOOLS and p["file_path"])

    wall_ms = (t1 - t0) * 1000
    other_ms = max(0.0, wall_ms - api_ms - tool_ms - permission_s * 1000)

    return {
        "complete": True,
        "session_id": sid,
        "start_ts": t0,
        "wall_ms": wall_ms,
        "api_ms": api_ms,
        "thinking_ms": thinking_ms,
        "generation_ms": generation_ms,
        "tool_ms": tool_ms,
        "permission_wait_ms": permission_s * 1000,
        "other_ms": other_ms,
        "api_calls": len(api_ends),
        "tool_calls": len(turn_pairs),
        "edits": edits,
        "tokens_output": tokens_out,
        "context_tokens": context_tokens,
        "prompt": ((prompt.meta or {}).get("prompt") or "")[:80],
    }


# ------------------------------------------------------------------ #
# Rework detection                                                     #
# ------------------------------------------------------------------ #

def rework(events: list[Event], threshold: int = 3) -> list[dict]:
    """Files edited repeatedly — thrash signal.

    Counts Edit/Write-family tool calls per file (whole capture and worst
    single turn), corroborated with fswatch file_change counts when the
    file was under a watched dir.  Files at or above `threshold` edits in
    one turn are flagged.
    """
    pairs = [p for p in pair_tool_calls(events)
             if p["tool"] in _EDIT_TOOLS and p["file_path"]]
    turns = [(t["session_id"], t["start_ts"], t["start_ts"] + t["wall_ms"] / 1000)
             for t in turn_anatomy(events)]
    fs_counts: dict[str, int] = {}
    for e in events:
        if e.phase == Phase.FILE_CHANGE:
            path = (e.meta or {}).get("path")
            if path:
                fs_counts[path] = fs_counts.get(path, 0) + 1

    files: dict[str, dict] = {}
    for p in pairs:
        f = files.setdefault(p["file_path"], {"edits": 0, "per_turn": {}})
        f["edits"] += 1
        for i, (sid, t0, t1) in enumerate(turns):
            if p["session_id"] == sid and t0 <= p["start_ts"] <= t1:
                f["per_turn"][i] = f["per_turn"].get(i, 0) + 1
                break

    results = []
    for path, f in files.items():
        worst = max(f["per_turn"].values(), default=0)
        results.append({
            "file": path,
            "edits": f["edits"],
            "max_edits_one_turn": worst,
            "file_changes": fs_counts.get(path),
            "flagged": worst >= threshold,
        })
    results.sort(key=lambda r: (r["flagged"], r["edits"]), reverse=True)
    return results


# ------------------------------------------------------------------ #
# Session stories                                                      #
# ------------------------------------------------------------------ #

def session_stories(events: list[Event]) -> list[dict]:
    """Per-session rollup with explicit coverage diagnostics.

    Aggregates turns, API usage, tool activity and human latency for each
    session, and reports which collectors actually contributed data —
    so an all-zero API column reads as "this session was not proxied"
    instead of silent dashes.
    """
    turns = turn_anatomy(events)
    hl = human_latency(events)
    pairs = pair_tool_calls(events)

    stories = []
    for sid in sorted({e.session_id for e in events if e.session_id}):
        sev = [e for e in events if e.session_id == sid]
        t0, t1 = sev[0].ts, sev[-1].ts

        def in_window(ts: float) -> bool:
            return t0 <= ts <= t1

        api_ends = [e for e in events
                    if e.phase == Phase.API_RESPONSE_END and in_window(e.ts)]
        api_starts = [e for e in events
                      if e.phase == Phase.API_REQUEST_START and in_window(e.ts)]
        has_proxy = bool(api_ends or api_starts or any(
            e.phase in (Phase.TOKENIZER_START, Phase.TOKENIZER_END)
            and in_window(e.ts) for e in events))
        has_fswatch = any(e.phase == Phase.FILE_CHANGE and in_window(e.ts)
                          for e in events)

        cache_read = sum((e.meta or {}).get("cache_read_input_tokens") or 0
                         for e in api_starts)
        session_turns = [t for t in turns if t["session_id"] == sid]
        tool_calls = [p for p in pairs if p["session_id"] == sid]
        end_event = next((e for e in reversed(sev)
                          if e.phase == Phase.SESSION_END), None)

        gaps = []
        if not has_proxy:
            gaps.append("no proxy data — session not routed through CIA "
                        "(launch claude with HTTPS_PROXY / NODE_EXTRA_CA_CERTS); "
                        "API, thinking and tokenizer fields are blank")
        if not has_fswatch:
            gaps.append("no fswatch data — daemon started without --watch-dir")
        if not any(e.phase == Phase.TURN_END for e in sev):
            gaps.append("no turn_end events — Stop hook missing or session "
                        "still on its first turn")

        stories.append({
            "session_id": sid,
            "start_ts": t0,
            "end_ts": t1,
            "duration_s": t1 - t0,
            "ended": end_event is not None,
            "end_reason": (end_event.meta or {}).get("reason") if end_event else None,
            "turns": len(session_turns),
            "incomplete_turns": sum(1 for t in session_turns if not t["complete"]),
            "prompts": sum(1 for e in sev if e.phase == Phase.USER_PROMPT),
            "api_calls": len(api_ends),
            "tokens_input": sum(e.tokens_input or 0 for e in api_ends),
            "tokens_output": sum(e.tokens_output or 0 for e in api_ends),
            "cache_read_tokens": cache_read,
            "thinking_ms": sum(e.duration_ms or 0 for e in events
                               if e.phase == Phase.API_THINKING_END
                               and in_window(e.ts)),
            "models": sorted({e.model for e in api_ends if e.model}),
            "tool_calls": len(tool_calls),
            "tool_errors": sum(1 for p in tool_calls if p["is_error"]),
            "tool_ms": sum(p["duration_ms"] for p in tool_calls),
            "edits": sum(1 for p in tool_calls
                         if p["tool"] in _EDIT_TOOLS and p["file_path"]),
            "permission_wait_s": sum(w["wait_s"] for w in hl["permission_waits"]
                                     if w["session_id"] == sid),
            "think_time_s": sum(w["wait_s"] for w in hl["think_times"]
                                if w["session_id"] == sid),
            "compactions": sum(1 for e in sev
                               if e.phase == Phase.CONTEXT_COMPACT),
            "subagents": sum(1 for e in sev if e.phase == Phase.SUBAGENT_END),
            "coverage": {"hooks": True, "proxy": has_proxy,
                         "fswatch": has_fswatch},
            "gaps": gaps,
        })
    return stories


# ------------------------------------------------------------------ #
# Full report                                                          #
# ------------------------------------------------------------------ #

def full_report(events: list[Event]) -> dict[str, Any]:
    """All derived analytics in one JSON-serialisable dict."""
    return {
        "sessions": session_stories(events),
        "turns": turn_anatomy(events),
        "tools": tool_profiles(events),
        "human": human_latency(events),
        "compactions": compaction_cost(events),
        "rework": rework(events),
    }
