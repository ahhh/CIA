"""
Derived performance analytics computed from recorded CIA events.

All functions take a time-sorted list of Event objects (as returned by
Store.query, or parsed from an exported JSONL file) and return plain
dicts/lists so results can be rendered as tables or dumped as JSON.

Correlation: proxy-derived events (api_*) carry no session_id of their
own.  When Claude Code's native telemetry was captured (cia run), the
Anthropic request-id — recorded by the proxy from the response header and
by the otel api_request event alongside session.id — joins them to their
session exactly.  Events without that join (telemetry off, or pre-join
captures) fall back to time-window matching, which can misattribute
across concurrent sessions; single-session captures are exact either way.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from cia.schema import Event, Phase
from cia.transcripts import is_sandbox_path as _is_sandbox_path
from cia.transcripts import project_display as _project_display

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


def _pctl_summary(vals: list[float]) -> dict:
    """count / p50 / p90 / max of a value list (unsorted accepted)."""
    vs = sorted(vals)
    return {
        "count": len(vs),
        "p50": _percentile(vs, 50),
        "p90": _percentile(vs, 90),
        "max": vs[-1] if vs else None,
    }


def _context_tokens(e: Event) -> int:
    """Full context size of an api_request_start: fresh + cached input."""
    meta = e.meta or {}
    usage = meta.get("usage") or {}
    return (
        (e.tokens_input or 0)
        + (meta.get("cache_read_input_tokens") or usage.get("cache_read_input_tokens") or 0)
        + (meta.get("cache_creation_input_tokens") or usage.get("cache_creation_input_tokens") or 0)
    )


_API_PHASES = {
    Phase.API_REQUEST_START, Phase.API_FIRST_TOKEN, Phase.API_THINKING_START,
    Phase.API_THINKING_END, Phase.API_GENERATION_START,
    Phase.API_GENERATION_END, Phase.API_RESPONSE_END, Phase.API_REQUEST_ERROR,
    Phase.API_PROGRESS,
}


def api_flow_sessions(events: list[Event]) -> dict[str, str]:
    """Exact flow_id → session_id map for proxy API events.

    Claude Code's native api_request / api_error telemetry carries both the
    Anthropic request-id and the session.id; the proxy records the same
    request-id from the response header.  Joining the two pins every proxied
    flow to its session — no time-window guessing.  Flows absent from the
    map (native telemetry off, or the header was missing) keep the
    time-window fallback.
    """
    rid_sid: dict[str, str] = {}
    for e in events:
        if e.phase != Phase.OTEL_EVENT:
            continue
        meta = e.meta or {}
        if meta.get("name") not in ("api_request", "api_error"):
            continue
        attrs = meta.get("attributes") or {}
        rid = attrs.get("request_id")
        sid = e.session_id or attrs.get("session.id")
        if rid and sid:
            rid_sid[str(rid)] = str(sid)

    flow_sid: dict[str, str] = {}
    for e in events:
        if e.phase not in _API_PHASES:
            continue
        meta = e.meta or {}
        rid, fid = meta.get("request_id"), meta.get("flow_id")
        if rid and fid:
            sid = rid_sid.get(str(rid))
            if sid:
                flow_sid[fid] = sid
    return flow_sid


def _session_filter(flow_sid: dict[str, str], sid) -> "callable":
    """Predicate: does this proxy API event belong to session `sid`?

    True when the flow was exactly joined to `sid`, or was never joined at
    all (fallback: caller still applies its time window); False only when
    the flow is known to belong to a *different* session.
    """
    def ok(e: Event) -> bool:
        known = flow_sid.get((e.meta or {}).get("flow_id"))
        return known is None or known == sid
    return ok


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
            start_meta = start.meta or {}
            pairs.append({
                "tool": e.tool,
                "session_id": e.session_id,
                "start_ts": start.ts,
                "end_ts": e.ts,
                "duration_ms": (e.ts - start.ts) * 1000,
                "is_error": bool(e.error) or bool(result.get("is_error")),
                "output_bytes": result.get("output_bytes"),
                "file_path": tool_input.get("file_path"),
                "command": tool_input.get("command") or start_meta.get("command"),
                "pattern": start_meta.get("pattern") or tool_input.get("pattern"),
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
        "native_active_time": _native_active_time(events),
    }


def _native_active_time(events: list[Event]) -> Optional[dict]:
    """Claude Code's own active-time split (claude_code.active_time.total):
    seconds the user was at the keyboard vs. the CLI doing work — an
    independent cross-check on the hook-derived wait estimates above."""
    series: dict[tuple, list] = {}
    temporality: dict[tuple, str] = {}
    for e in events:
        if e.phase != Phase.OTEL_METRIC:
            continue
        meta = e.meta or {}
        if meta.get("name") != "claude_code.active_time.total" \
                or meta.get("value") is None:
            continue
        attrs = meta.get("attributes") or {}
        key = (e.session_id, str(attrs.get("type") or "?"))
        series.setdefault(key, []).append((e.ts, float(meta["value"])))
        if meta.get("temporality"):
            temporality[key] = meta["temporality"]
    if not series:
        return None
    totals: dict[str, float] = {}
    for key, points in series.items():
        points.sort()
        total = sum(d for _, d in _series_deltas(points, temporality.get(key)))
        totals[key[1]] = totals.get(key[1], 0.0) + total
    return {"user_s": totals.get("user", 0.0), "cli_s": totals.get("cli", 0.0)}


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

    When native telemetry was captured, each row is additionally enriched
    with Claude Code's own `compaction` event (exact pre/post token counts,
    duration and success) matched by session + time proximity; native
    compactions with no PreCompact hook nearby become rows of their own.
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
            "native": None,
        })

    for e, attrs in _named_otel_events(events, "compaction"):
        native = {
            "trigger": attrs.get("trigger"),
            "success": str(attrs.get("success")).lower() != "false",
            "duration_ms": _num(attrs.get("duration_ms")),
            "pre_tokens": _num(attrs.get("pre_tokens")),
            "post_tokens": _num(attrs.get("post_tokens")),
        }
        host = min(
            (r for r in results if r["native"] is None
             and (r["session_id"] is None or e.session_id is None
                  or r["session_id"] == e.session_id)
             and abs(r["ts"] - e.ts) <= 120.0),
            key=lambda r: abs(r["ts"] - e.ts), default=None)
        if host is not None:
            host["native"] = native
            if native["pre_tokens"] is not None and native["post_tokens"] is not None:
                host["reclaimed_tokens"] = int(
                    native["pre_tokens"] - native["post_tokens"])
        else:
            results.append({
                "ts": e.ts,
                "session_id": e.session_id,
                "trigger": attrs.get("trigger"),
                "context_before": native["pre_tokens"],
                "context_after": native["post_tokens"],
                "reclaimed_tokens": (
                    int(native["pre_tokens"] - native["post_tokens"])
                    if native["pre_tokens"] is not None
                    and native["post_tokens"] is not None else None),
                "recovery_s": None,
                "native": native,
            })
    results.sort(key=lambda r: r["ts"])
    return results


# ------------------------------------------------------------------ #
# Turn anatomy                                                         #
# ------------------------------------------------------------------ #

def turn_anatomy(events: list[Event]) -> list[dict]:
    """Break each turn's wall-clock into model / tool / human components.

    A turn is user_prompt → the next turn_end in the same session.  api_*
    events are attributed exactly via the request-id join when native
    telemetry was captured, by time window otherwise (see module
    docstring); tool calls and permission waits by session + time window.

    A turn still open at the end of the capture (no turn_end yet — in
    progress, or the session died) is closed at the last event seen for
    its session and marked ``complete: False`` rather than dropped.
    """
    pairs = pair_tool_calls(events)
    hl = human_latency(events)
    flow_sid = api_flow_sessions(events)

    turns: list[dict] = []
    sessions = {e.session_id for e in events if e.session_id}
    for sid in sessions:
        sev = [e for e in events if e.session_id == sid]
        open_prompt: Optional[Event] = None
        for e in sev:
            if e.phase == Phase.USER_PROMPT and open_prompt is None:
                open_prompt = e
            elif e.phase == Phase.TURN_END and open_prompt is not None:
                turns.append(_one_turn(open_prompt, e, events, pairs, hl,
                                       flow_sid))
                open_prompt = None
        if open_prompt is not None and sev[-1].ts > open_prompt.ts:
            turn = _one_turn(open_prompt, sev[-1], events, pairs, hl, flow_sid)
            turn["complete"] = False
            turns.append(turn)

    turns.sort(key=lambda t: t["start_ts"])
    return turns


def _one_turn(prompt: Event, end: Event, events: list[Event],
              pairs: list[dict], hl: dict,
              flow_sid: Optional[dict[str, str]] = None) -> dict:
    sid = prompt.session_id
    t0, t1 = prompt.ts, end.ts
    mine = _session_filter(flow_sid or {}, sid)

    def in_window(ts: float) -> bool:
        return t0 <= ts <= t1

    api_ends = [e for e in events
                if e.phase == Phase.API_RESPONSE_END and in_window(e.ts)
                and mine(e)]
    api_ms = sum(e.duration_ms or 0 for e in api_ends)
    thinking_ms = sum(e.duration_ms or 0 for e in events
                      if e.phase == Phase.API_THINKING_END and in_window(e.ts)
                      and mine(e))
    generation_ms = sum(e.duration_ms or 0 for e in events
                        if e.phase == Phase.API_GENERATION_END
                        and in_window(e.ts) and mine(e))
    tokens_out = sum(e.tokens_output or 0 for e in api_ends)

    req_starts = [e for e in events
                  if e.phase == Phase.API_REQUEST_START and in_window(e.ts)
                  and mine(e)]
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

def session_stories(events: list[Event],
                    transcripts: Optional[dict] = None,
                    insights: Optional[dict] = None) -> list[dict]:
    """Per-session rollup with explicit coverage diagnostics.

    Aggregates turns, API usage, tool activity and human latency for each
    session, and reports which collectors actually contributed data —
    so an all-zero API column reads as "this session was not proxied"
    instead of silent dashes.

    ``transcripts`` / ``insights`` (from cia.transcripts) add session
    identity (title, project, outcome) and fill the token/model columns
    from the on-disk transcript when the session was never proxied.
    """
    transcripts = transcripts or {}
    insights = insights or {}
    turns = turn_anatomy(events)
    hl = human_latency(events)
    pairs = pair_tool_calls(events)
    flow_sid = api_flow_sessions(events)

    stories = []
    for sid in sorted({e.session_id for e in events if e.session_id}):
        sev = [e for e in events if e.session_id == sid]
        t0, t1 = sev[0].ts, sev[-1].ts
        mine = _session_filter(flow_sid, sid)

        def in_window(ts: float) -> bool:
            return t0 <= ts <= t1

        api_ends = [e for e in events
                    if e.phase == Phase.API_RESPONSE_END and in_window(e.ts)
                    and mine(e)]
        api_starts = [e for e in events
                      if e.phase == Phase.API_REQUEST_START and in_window(e.ts)
                      and mine(e)]
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

        tr = transcripts.get(sid)
        ins = insights.get(sid) or {}

        tokens_input = sum(e.tokens_input or 0 for e in api_ends)
        tokens_output = sum(e.tokens_output or 0 for e in api_ends)
        models = sorted({e.model for e in api_ends if e.model})
        token_source = "proxy" if has_proxy else None
        if not has_proxy and tr and tr["usage"]["output"]:
            # Session never went through the proxy, but its transcript holds
            # the exact usage Claude Code saved from each API response.
            tokens_input = tr["usage"]["input"]
            tokens_output = tr["usage"]["output"]
            cache_read = tr["usage"]["cache_read"]
            models = sorted(m for m in tr["by_model"] if m != "?")
            token_source = "transcript"

        gaps = []
        if not has_proxy:
            filled = (" — token/model columns filled from the session "
                      "transcript" if token_source == "transcript" else
                      "; API, thinking and tokenizer fields are blank")
            gaps.append("no proxy data — session not routed through CIA "
                        "(launch claude with HTTPS_PROXY / NODE_EXTRA_CA_CERTS)"
                        + filled)
        if not has_fswatch:
            gaps.append("no fswatch data — daemon started without --watch-dir")
        if not any(e.phase == Phase.TURN_END for e in sev):
            gaps.append("no turn_end events — Stop hook missing or session "
                        "still on its first turn")

        stories.append({
            "session_id": sid,
            "title": tr["title"] if tr else None,
            "project": (_project_display(tr["project_path"])
                        if tr and tr["project_path"] else None),
            "outcome": ins.get("outcome"),
            "start_ts": t0,
            "end_ts": t1,
            "duration_s": t1 - t0,
            "ended": end_event is not None,
            "end_reason": (end_event.meta or {}).get("reason") if end_event else None,
            "turns": len(session_turns),
            "incomplete_turns": sum(1 for t in session_turns if not t["complete"]),
            "prompts": sum(1 for e in sev if e.phase == Phase.USER_PROMPT),
            "api_calls": len(api_ends),
            "tokens_input": tokens_input,
            "tokens_output": tokens_output,
            "token_source": token_source,
            "cache_read_tokens": cache_read,
            "thinking_ms": sum(e.duration_ms or 0 for e in events
                               if e.phase == Phase.API_THINKING_END
                               and in_window(e.ts) and mine(e)),
            "api_flows_joined": sum(1 for s in flow_sid.values() if s == sid),
            "models": models,
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
                         "fswatch": has_fswatch,
                         "transcripts": tr is not None},
            "gaps": gaps,
        })
    return stories


# ------------------------------------------------------------------ #
# Cache economics                                                      #
# ------------------------------------------------------------------ #

_CACHE_TTL_S = 300.0      # Anthropic prompt-cache TTL (5 minutes)
_BUST_MIN_TOKENS = 1024   # ignore "busts" when barely anything was cached


def cache_economics(events: list[Event]) -> dict:
    """Prompt-cache effectiveness: hit rate, latency value, bust forensics.

    Works off api_request_start events (cache token counts, request anatomy
    and streaming TTFB) in time order; subject to the same multi-session
    caveat as the rest of the proxy-derived analytics.

    A "bust" is a request whose cache_read falls below half of what the
    previous request left cached — the prefix was rebuilt.  Causes are
    checked in order: a compaction fired since the previous request; the
    idle gap exceeded the cache TTL; the system prompt / tool definitions
    changed size.
    """
    reqs = []
    for e in events:
        if e.phase != Phase.API_REQUEST_START:
            continue
        meta = e.meta or {}
        reqs.append({
            "ts": e.ts,
            "model": e.model,
            "fresh": e.tokens_input or 0,
            "cache_read": meta.get("cache_read_input_tokens") or 0,
            "cache_creation": meta.get("cache_creation_input_tokens") or 0,
            "context": _context_tokens(e),
            "ttfb_ms": meta.get("ttfb_ms"),
            "anatomy": meta.get("request") or {},
        })
    response_ends = [e.ts for e in events if e.phase == Phase.API_RESPONSE_END]
    compactions = [e.ts for e in events if e.phase == Phase.CONTEXT_COMPACT]

    warm_requests = 0
    warm_ttfb: list[float] = []
    cold_ttfb: list[float] = []
    for r in reqs:
        is_warm = r["context"] > 0 and r["cache_read"] >= 0.5 * r["context"]
        warm_requests += is_warm
        if r["ttfb_ms"] is not None:
            (warm_ttfb if is_warm else cold_ttfb).append(r["ttfb_ms"])

    busts: list[dict] = []
    ttl_expiries: list[dict] = []
    for prev, cur in zip(reqs, reqs[1:]):
        last_end = max((t for t in response_ends if t <= cur["ts"]),
                       default=prev["ts"])
        idle_s = cur["ts"] - last_end
        if idle_s > _CACHE_TTL_S:
            ttl_expiries.append({"ts": cur["ts"], "idle_s": round(idle_s, 1),
                                 "retokenized_tokens": cur["cache_creation"]})
        prev_cached = prev["cache_read"] + prev["cache_creation"]
        if prev_cached < _BUST_MIN_TOKENS or cur["cache_read"] >= 0.5 * prev_cached:
            continue
        if any(prev["ts"] < t <= cur["ts"] for t in compactions):
            cause = "compaction"
        elif idle_s > _CACHE_TTL_S:
            cause = "ttl_expired"
        elif (prev["anatomy"].get("system_chars") != cur["anatomy"].get("system_chars")
              or prev["anatomy"].get("tools_chars") != cur["anatomy"].get("tools_chars")):
            cause = "prompt_change"
        else:
            cause = "unknown"
        busts.append({
            "ts": cur["ts"],
            "cause": cause,
            "idle_s": round(idle_s, 1),
            "cached_before": prev_cached,
            "cache_read": cur["cache_read"],
            "retokenized_tokens": cur["cache_creation"],
        })

    total_read = sum(r["cache_read"] for r in reqs)
    total_context = sum(r["context"] for r in reqs)
    return {
        "requests": len(reqs),
        "warm_requests": warm_requests,
        "hit_rate": warm_requests / len(reqs) if reqs else None,
        "tokens": {
            "fresh_input": sum(r["fresh"] for r in reqs),
            "cache_read": total_read,
            "cache_creation": sum(r["cache_creation"] for r in reqs),
            "read_ratio": total_read / total_context if total_context else None,
        },
        "ttfb_ms": {"warm": _pctl_summary(warm_ttfb),
                    "cold": _pctl_summary(cold_ttfb)},
        "busts": busts,
        "ttl": {
            "expiries": len(ttl_expiries),
            "retokenized_tokens": sum(t["retokenized_tokens"] for t in ttl_expiries),
            "longest_idle_s": max((t["idle_s"] for t in ttl_expiries), default=None),
        },
    }


# ------------------------------------------------------------------ #
# Thinking calibration                                                 #
# ------------------------------------------------------------------ #

def thinking_calibration(events: list[Event]) -> dict:
    """Did thinking fire when requested, fill its budget, and pay off?

    Aggregates the per-response meta.thinking summaries plus the
    thinking→tool decisiveness gap, and splits turns at their median
    thinking time to compare downstream tool errors / repeat edits.
    """
    summaries = [
        (e.meta or {}).get("thinking") for e in events
        if e.phase == Phase.API_RESPONSE_END and (e.meta or {}).get("thinking")
    ]
    requested = [t for t in summaries if t.get("thinking_requested")]
    fired = [t for t in requested if t.get("thinking_fired")]

    by_effort: dict[str, dict] = {}
    for t in requested:
        key = str(t.get("requested_effort")
                  or t.get("requested_thinking_type") or "?")
        g = by_effort.setdefault(key, {"requests": 0, "fired": 0,
                                       "thinking_ms": []})
        g["requests"] += 1
        if t.get("thinking_fired"):
            g["fired"] += 1
        if t.get("thinking_ms"):
            g["thinking_ms"].append(t["thinking_ms"])
    by_effort = {
        k: {
            "requests": g["requests"],
            "fired": g["fired"],
            "fire_rate": g["fired"] / g["requests"],
            "mean_thinking_ms": (sum(g["thinking_ms"]) / len(g["thinking_ms"]))
                                if g["thinking_ms"] else None,
        }
        for k, g in by_effort.items()
    }

    utilizations = sorted(t["budget_utilization"] for t in summaries
                          if t.get("budget_utilization") is not None)
    interrupted = sum(1 for t in summaries if t.get("interrupted"))

    decisiveness: dict[str, list[float]] = {}
    for e in events:
        if e.phase != Phase.API_GENERATION_START:
            continue
        gap = (e.meta or {}).get("thinking_to_tool_ms")
        if gap is not None:
            decisiveness.setdefault(e.model or "?", []).append(gap)

    return {
        "responses": len(summaries),
        "thinking_requested": len(requested),
        "thinking_fired": len(fired),
        "fire_rate": len(fired) / len(requested) if requested else None,
        "by_effort": by_effort,
        "budget": {
            "samples": len(utilizations),
            "utilization_p50": _percentile(utilizations, 50),
            "utilization_max": utilizations[-1] if utilizations else None,
            "interrupted": interrupted,
            "interruption_rate": (interrupted / len(summaries))
                                 if summaries else None,
        },
        "decisiveness_ms": {m: _pctl_summary(v)
                            for m, v in sorted(decisiveness.items())},
        "turn_split": _thinking_turn_split(events),
    }


def _thinking_turn_split(events: list[Event]) -> Optional[dict]:
    """Turns above vs below median thinking time, compared on downstream
    quality signals (tool errors, files edited more than once in the turn)."""
    pairs = pair_tool_calls(events)
    turns = [t for t in turn_anatomy(events) if t["api_calls"]]
    if len(turns) < 2:
        return None

    rows = []
    for t in turns:
        t0, t1 = t["start_ts"], t["start_ts"] + t["wall_ms"] / 1000
        tp = [p for p in pairs
              if p["session_id"] == t["session_id"] and t0 <= p["start_ts"] <= t1]
        edit_counts: dict[str, int] = {}
        for p in tp:
            if p["tool"] in _EDIT_TOOLS and p["file_path"]:
                edit_counts[p["file_path"]] = edit_counts.get(p["file_path"], 0) + 1
        rows.append({
            "thinking_ms": t["thinking_ms"],
            "tool_errors": sum(1 for p in tp if p["is_error"]),
            "repeat_edit_files": sum(1 for c in edit_counts.values() if c >= 2),
        })

    median = _percentile(sorted(r["thinking_ms"] for r in rows), 50)
    high = [r for r in rows if r["thinking_ms"] > median]
    low = [r for r in rows if r["thinking_ms"] <= median]

    def bucket(rs: list[dict]) -> dict:
        return {
            "turns": len(rs),
            "mean_tool_errors": (sum(r["tool_errors"] for r in rs) / len(rs))
                                if rs else None,
            "mean_repeat_edit_files": (sum(r["repeat_edit_files"] for r in rs)
                                       / len(rs)) if rs else None,
        }

    return {"median_thinking_ms": median,
            "high_thinking": bucket(high),
            "low_thinking": bucket(low)}


# ------------------------------------------------------------------ #
# Context pressure                                                     #
# ------------------------------------------------------------------ #

def context_pressure(events: list[Event],
                     compaction_threshold: Optional[int] = None) -> dict:
    """Context growth per turn and which tools inflate it fastest.

    context_delta is the change in the turn's final request context vs the
    previous turn of the same session (compaction turns excluded from the
    growth statistic).  When no threshold is passed, the largest context
    observed going *into* a compaction is used to project how many turns of
    median growth remain before the next one.
    """
    turns = sorted(turn_anatomy(events), key=lambda t: t["start_ts"])
    pairs = pair_tool_calls(events)
    compact_ts = [e.ts for e in events if e.phase == Phase.CONTEXT_COMPACT]

    rows: list[dict] = []
    last_ctx: dict[str, int] = {}
    last_seen: dict[str, float] = {}
    for t in turns:
        sid = t["session_id"]
        t0, t1 = t["start_ts"], t["start_ts"] + t["wall_ms"] / 1000
        by_tool: dict[str, int] = {}
        for p in pairs:
            if (p["session_id"] == sid and t0 <= p["start_ts"] <= t1
                    and p["output_bytes"]):
                by_tool[p["tool"]] = by_tool.get(p["tool"], 0) + p["output_bytes"]
        ctx = t["context_tokens"]
        prev = last_ctx.get(sid)
        compacted = any(last_seen.get(sid, t0) < c <= t1 for c in compact_ts)
        rows.append({
            "session_id": sid,
            "start_ts": t0,
            "context_tokens": ctx,
            "context_delta": (ctx - prev)
                             if ctx is not None and prev is not None else None,
            "tool_output_bytes": sum(by_tool.values()),
            "top_tool": max(by_tool, key=by_tool.get) if by_tool else None,
            "compacted": compacted,
        })
        if ctx is not None:
            last_ctx[sid] = ctx
        last_seen[sid] = t1

    growth = sorted(r["context_delta"] for r in rows
                    if r["context_delta"] is not None
                    and r["context_delta"] > 0 and not r["compacted"])
    bloat: dict[str, int] = {}
    for p in pairs:
        if p["output_bytes"]:
            bloat[p["tool"]] = bloat.get(p["tool"], 0) + p["output_bytes"]

    if compaction_threshold is None:
        observed = [c["context_before"] for c in compaction_cost(events)
                    if c["context_before"]]
        compaction_threshold = max(observed) if observed else None

    growth_p50 = _percentile(growth, 50)
    projected: dict[str, float] = {}
    if compaction_threshold and growth_p50:
        for sid, ctx in last_ctx.items():
            projected[sid] = max(
                0.0, round((compaction_threshold - ctx) / growth_p50, 1))

    return {
        "turns": rows,
        "growth_per_turn_p50": growth_p50,
        "bloat_by_tool": sorted(
            ({"tool": k, "output_bytes": v} for k, v in bloat.items()),
            key=lambda b: b["output_bytes"], reverse=True),
        "compaction_threshold": compaction_threshold,
        "projected_turns_to_compaction": projected or None,
    }


# ------------------------------------------------------------------ #
# Tool chains                                                          #
# ------------------------------------------------------------------ #

_SEARCH_TOOLS = {"Grep", "Glob"}


def _pair_target(p: dict) -> Optional[str]:
    """The identity of what a tool call acted on, for retry detection."""
    return p.get("command") or p.get("file_path") or p.get("pattern")


def _flush_run(run: list[dict], loops: list[dict]) -> None:
    """Record a run of consecutive same-tool same-target calls if it looks
    like a retry loop (something errored, or 3+ repeats)."""
    if len(run) < 2:
        return
    errors = sum(1 for p in run if p["is_error"])
    if errors or len(run) >= 3:
        loops.append({
            "session_id": run[0]["session_id"],
            "tool": run[0]["tool"],
            "target": _pair_target(run[0]),
            "first_ts": run[0]["start_ts"],
            "repeats": len(run),
            "errors": errors,
        })


def tool_chains(events: list[Event], thrash_threshold: int = 3) -> dict:
    """Sequence patterns over the tool stream: transitions, retry loops,
    search thrash and recovery after errors.

    A retry loop is a run of consecutive calls to the same tool with the
    same target (command / file / pattern) — flagged when something in the
    run errored, or the run is 3+ long.  Search thrash counts Grep/Glob
    calls before the first Read of each turn.
    """
    pairs = pair_tool_calls(events)
    by_session: dict[Optional[str], list[dict]] = {}
    for p in pairs:
        by_session.setdefault(p["session_id"], []).append(p)

    transitions: dict[tuple, int] = {}
    loops: list[dict] = []
    recoveries: list[dict] = []
    unrecovered = 0

    for sp in by_session.values():
        for a, b in zip(sp, sp[1:]):
            key = (a["tool"], b["tool"])
            transitions[key] = transitions.get(key, 0) + 1

        run: list[dict] = []
        for p in sp:
            if (run and p["tool"] == run[-1]["tool"]
                    and _pair_target(p) is not None
                    and _pair_target(p) == _pair_target(run[-1])):
                run.append(p)
            else:
                _flush_run(run, loops)
                run = [p]
        _flush_run(run, loops)

        for i, p in enumerate(sp):
            if not p["is_error"]:
                continue
            nxt = next(((j, q) for j, q in enumerate(sp[i + 1:], i + 1)
                        if not q["is_error"]), None)
            if nxt is None:
                unrecovered += 1
            else:
                j, q = nxt
                recoveries.append({"calls": j - i,
                                   "ms": (q["end_ts"] - p["end_ts"]) * 1000})

    thrash_turns: list[dict] = []
    for t in turn_anatomy(events):
        t0, t1 = t["start_ts"], t["start_ts"] + t["wall_ms"] / 1000
        searches = 0
        for p in by_session.get(t["session_id"], []):
            if not (t0 <= p["start_ts"] <= t1):
                continue
            if p["tool"] == "Read":
                break
            if p["tool"] in _SEARCH_TOOLS:
                searches += 1
        if searches >= thrash_threshold:
            thrash_turns.append({
                "session_id": t["session_id"],
                "start_ts": t0,
                "searches_before_first_read": searches,
                "prompt": t["prompt"],
            })

    total_searches = sum(1 for p in pairs if p["tool"] in _SEARCH_TOOLS)
    total_reads = sum(1 for p in pairs if p["tool"] == "Read")
    rec_calls = sorted(r["calls"] for r in recoveries)
    rec_ms = sorted(r["ms"] for r in recoveries)
    top = sorted(transitions.items(), key=lambda kv: kv[1], reverse=True)[:10]

    return {
        "transitions": [{"from": a, "to": b, "count": n}
                        for (a, b), n in top],
        "retry_loops": sorted(loops, key=lambda l: (l["errors"], l["repeats"]),
                              reverse=True),
        "search_thrash": {
            "searches": total_searches,
            "reads": total_reads,
            "search_to_read_ratio": (total_searches / total_reads)
                                    if total_reads else None,
            "thrash_turns": thrash_turns,
        },
        "error_recovery": {
            "errors": len(recoveries) + unrecovered,
            "recovered": len(recoveries),
            "unrecovered": unrecovered,
            "recovery_calls_p50": _percentile(rec_calls, 50),
            "recovery_ms_p50": _percentile(rec_ms, 50),
            "recovery_ms_max": rec_ms[-1] if rec_ms else None,
        },
    }


# ------------------------------------------------------------------ #
# Cost attribution                                                     #
# ------------------------------------------------------------------ #

_OTEL_COST = "claude_code.cost.usage"
_OTEL_TOKENS = "claude_code.token.usage"
_OTEL_LINES = "claude_code.lines_of_code.count"
_OTEL_COMMITS = "claude_code.commit.count"
_COST_METRICS = (_OTEL_COST, _OTEL_TOKENS, _OTEL_LINES, _OTEL_COMMITS)


def _series_deltas(points: list[tuple[float, float]],
                   temporality: Optional[str] = None) -> list[tuple[float, float]]:
    """Per-export increments of an OTLP counter series.

    When the export's aggregation temporality is known (cia run requests
    delta; the receiver records what actually arrived), it decides exactly:
    delta series pass through, cumulative series are differenced.  With no
    recorded temporality (older captures), fall back to the heuristic — a
    non-decreasing series is treated as cumulative and differenced.
    """
    vals = [v for _, v in points]
    if temporality == "delta":
        deltas = vals
    elif temporality == "cumulative" or (
            temporality is None and len(vals) >= 2
            and all(b >= a for a, b in zip(vals, vals[1:]))):
        deltas = [vals[0]] + [b - a for a, b in zip(vals, vals[1:])]
    else:
        deltas = vals
    return [(points[i][0], deltas[i]) for i in range(len(points))]


def cost_attribution(events: list[Event]) -> dict:
    """Join Claude Code's native cost/token/LoC telemetry onto turns.

    Each cost increment is attributed to the most recent turn of its
    session that started before the metric export (exports lag the work by
    up to the export interval — 2s under cia run).  A rework turn re-edits a file
    already edited earlier in the session, or edits the same file more
    than once.
    """
    series: dict[tuple, list] = {}
    temporality: dict[tuple, str] = {}
    for e in events:
        if e.phase != Phase.OTEL_METRIC:
            continue
        meta = e.meta or {}
        name = meta.get("name")
        if name not in _COST_METRICS or meta.get("value") is None:
            continue
        attrs = meta.get("attributes") or {}
        attr_key = tuple(sorted((k, str(v)) for k, v in attrs.items()
                                if k not in ("session.id", "session_id")))
        key = (e.session_id, name, attr_key)
        series.setdefault(key, []).append((e.ts, float(meta["value"])))
        if meta.get("temporality"):
            temporality[key] = meta["temporality"]

    if not series:
        return {"available": False}

    sessions: dict[Optional[str], dict] = {}
    cost_deltas: list[tuple[Optional[str], float, float]] = []
    for (sid, name, attr_key), points in series.items():
        points.sort()
        attrs = dict(attr_key)
        s = sessions.setdefault(sid, {
            "cost_usd": 0.0, "tokens": {}, "lines_added": 0,
            "lines_removed": 0, "commits": 0,
        })
        deltas = _series_deltas(points, temporality.get((sid, name, attr_key)))
        total = sum(d for _, d in deltas)
        if name == _OTEL_COST:
            s["cost_usd"] += total
            cost_deltas.extend((sid, ts, d) for ts, d in deltas if d)
        elif name == _OTEL_TOKENS:
            ttype = str(attrs.get("type", "?"))
            s["tokens"][ttype] = s["tokens"].get(ttype, 0) + total
        elif name == _OTEL_LINES:
            if attrs.get("type") == "added":
                s["lines_added"] += total
            elif attrs.get("type") == "removed":
                s["lines_removed"] += total
        elif name == _OTEL_COMMITS:
            s["commits"] += total

    turns = sorted(turn_anatomy(events), key=lambda t: t["start_ts"])
    pairs = pair_tool_calls(events)
    seen_files: dict[str, set] = {}
    turn_rows: list[dict] = []
    for t in turns:
        sid = t["session_id"]
        t0, t1 = t["start_ts"], t["start_ts"] + t["wall_ms"] / 1000
        edited: dict[str, int] = {}
        for p in pairs:
            if (p["session_id"] == sid and p["tool"] in _EDIT_TOOLS
                    and p["file_path"] and t0 <= p["start_ts"] <= t1):
                edited[p["file_path"]] = edited.get(p["file_path"], 0) + 1
        seen = seen_files.setdefault(sid, set())
        rework_turn = (any(c >= 2 for c in edited.values())
                       or bool(set(edited) & seen))
        seen.update(edited)
        turn_rows.append({"session_id": sid, "start_ts": t0,
                          "prompt": t["prompt"], "cost_usd": 0.0,
                          "rework": rework_turn})

    unattributed = 0.0
    for sid, ts, d in cost_deltas:
        candidates = [r for r in turn_rows
                      if r["session_id"] == sid and r["start_ts"] <= ts]
        if candidates:
            candidates[-1]["cost_usd"] += d
        else:
            unattributed += d

    total_cost = sum(s["cost_usd"] for s in sessions.values())
    lines_added = sum(s["lines_added"] for s in sessions.values())
    commits = sum(s["commits"] for s in sessions.values())
    return {
        "available": True,
        "sessions": sessions,
        "total_cost_usd": total_cost,
        "turns": turn_rows,
        "unattributed_usd": unattributed,
        "rework_cost_usd": sum(r["cost_usd"] for r in turn_rows if r["rework"]),
        "cost_per_commit_usd": (total_cost / commits) if commits else None,
        "cost_per_line_added_usd": (total_cost / lines_added)
                                   if lines_added else None,
    }


# ------------------------------------------------------------------ #
# Throughput                                                           #
# ------------------------------------------------------------------ #

def throughput(events: list[Event]) -> dict:
    """Generation speed and latency by model, hour of day, and within long
    responses (api_progress ticks expose mid-response speed sag that the
    end-of-response average hides)."""
    rows = []
    for e in events:
        if e.phase != Phase.API_RESPONSE_END:
            continue
        lat = (e.meta or {}).get("latency") or {}
        rows.append({
            "ts": e.ts,
            "model": e.model or "?",
            "tok_per_sec": lat.get("output_tokens_per_sec"),
            "ttfb_ms": lat.get("ttfb_ms"),
            "ttft_ms": lat.get("ttft_ms"),
            "total_ms": e.duration_ms,
            "tokens_output": e.tokens_output,
        })

    by_model: dict[str, list[dict]] = {}
    for r in rows:
        by_model.setdefault(r["model"], []).append(r)
    model_stats = {
        m: {
            "requests": len(rs),
            "tok_per_sec": _pctl_summary(
                [r["tok_per_sec"] for r in rs if r["tok_per_sec"]]),
            "ttfb_ms": _pctl_summary(
                [r["ttfb_ms"] for r in rs if r["ttfb_ms"] is not None]),
            "ttft_ms": _pctl_summary(
                [r["ttft_ms"] for r in rs if r["ttft_ms"] is not None]),
        }
        for m, rs in sorted(by_model.items())
    }

    by_hour: dict[int, list[float]] = {}
    for r in rows:
        if r["tok_per_sec"]:
            by_hour.setdefault(time.localtime(r["ts"]).tm_hour, []).append(
                r["tok_per_sec"])
    hour_stats = {h: {"requests": len(v),
                      "tok_per_sec_p50": _percentile(sorted(v), 50)}
                  for h, v in sorted(by_hour.items())}

    slow = sorted((r for r in rows if r["ttfb_ms"] is not None),
                  key=lambda r: r["ttfb_ms"], reverse=True)[:5]

    # Intra-response sag: rate between consecutive progress ticks, first
    # half of a response vs second half, averaged over responses long
    # enough to have 3+ ticks.
    prog: dict[str, list[tuple[float, float]]] = {}
    for e in events:
        if e.phase != Phase.API_PROGRESS:
            continue
        meta = e.meta or {}
        if e.duration_ms is not None and meta.get("est_output_tokens") is not None:
            prog.setdefault(meta.get("flow_id") or "?", []).append(
                (e.duration_ms, meta["est_output_tokens"]))
    early: list[float] = []
    late: list[float] = []
    for pts in prog.values():
        pts.sort()
        rates = []
        for (e0, c0), (e1, c1) in zip(pts, pts[1:]):
            dt = (e1 - e0) / 1000
            if dt > 0:
                rates.append((c1 - c0) / dt)
        if len(rates) >= 2:
            half = len(rates) // 2
            early.append(sum(rates[:half]) / half)
            late.append(sum(rates[half:]) / (len(rates) - half))
    sag = None
    if early:
        mean_early = sum(early) / len(early)
        mean_late = sum(late) / len(late)
        sag = {
            "flows": len(early),
            "early_tok_per_sec": mean_early,
            "late_tok_per_sec": mean_late,
            "late_to_early_ratio": (mean_late / mean_early)
                                   if mean_early else None,
        }

    return {"requests": len(rows), "by_model": model_stats,
            "by_hour": hour_stats, "slow_requests": slow, "sag": sag}


# ------------------------------------------------------------------ #
# Network overhead                                                     #
# ------------------------------------------------------------------ #

def network_overhead(events: list[Event]) -> dict:
    """Non-inference traffic share: requests/bytes/time per category,
    failures, and whether each failure landed while an inference call was
    in flight."""
    api_windows: list[tuple[float, float]] = []
    open_starts: dict[str, float] = {}
    pending: list[float] = []
    for e in events:
        fid = (e.meta or {}).get("flow_id")
        if e.phase == Phase.API_REQUEST_START:
            if fid:
                open_starts[fid] = e.ts
            else:
                pending.append(e.ts)
        elif e.phase == Phase.API_RESPONSE_END:
            start = open_starts.pop(fid, None) if fid else None
            if start is None and pending:
                start = pending.pop(0)
            if start is not None:
                api_windows.append((start, e.ts))

    cats: dict[str, dict] = {}
    failures: list[dict] = []
    overhead_ms = 0.0
    for e in events:
        if e.phase != Phase.NETWORK_REQUEST:
            continue
        meta = e.meta or {}
        cat = meta.get("category") or "unknown"
        c = cats.setdefault(cat, {"requests": 0, "errors": 0, "total_ms": 0.0,
                                  "total_bytes": 0, "hosts": {}})
        c["requests"] += 1
        host = meta.get("host") or "?"
        c["hosts"][host] = c["hosts"].get(host, 0) + 1
        if e.duration_ms:
            c["total_ms"] += e.duration_ms
            overhead_ms += e.duration_ms
        c["total_bytes"] += ((meta.get("request_bytes") or 0)
                             + (meta.get("response_bytes") or 0))
        if e.error:
            c["errors"] += 1
            failures.append({
                "ts": e.ts,
                "host": host,
                "path": meta.get("path"),
                "status": meta.get("status"),
                "category": cat,
                "during_api_call": any(a <= e.ts <= b for a, b in api_windows),
            })

    by_category = [
        {"category": cat, "requests": c["requests"], "errors": c["errors"],
         "error_rate": c["errors"] / c["requests"],
         "total_ms": c["total_ms"], "total_bytes": c["total_bytes"],
         "top_hosts": sorted(c["hosts"], key=c["hosts"].get, reverse=True)[:3]}
        for cat, c in sorted(cats.items(),
                             key=lambda kv: kv[1]["total_ms"], reverse=True)
    ]
    inference_ms = sum(e.duration_ms or 0 for e in events
                       if e.phase == Phase.API_RESPONSE_END)
    return {
        "by_category": by_category,
        "totals": {
            "overhead_requests": sum(c["requests"] for c in by_category),
            "overhead_ms": overhead_ms,
            "overhead_bytes": sum(c["total_bytes"] for c in by_category),
            "inference_ms": inference_ms,
            "overhead_time_frac": (overhead_ms / (overhead_ms + inference_ms))
                                  if (overhead_ms + inference_ms) else None,
        },
        "failures": failures,
    }


# ------------------------------------------------------------------ #
# Native-telemetry insights                                            #
# ------------------------------------------------------------------ #

# tool_decision sources that mean nobody was interrupted
_AUTO_DECISION_SOURCES = {"config", "hook"}


def _num(v) -> Optional[float]:
    """Coerce an OTLP attribute to a float (attrs often arrive as strings)."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _named_otel_events(events: list[Event], name: str) -> list[tuple[Event, dict]]:
    return [(e, (e.meta or {}).get("attributes") or {})
            for e in events
            if e.phase == Phase.OTEL_EVENT and (e.meta or {}).get("name") == name]


def otel_insights(events: list[Event]) -> dict:
    """Analytics over Claude Code's own telemetry events (otel_event):
    permission economics, API reliability, cost by subsystem, hook
    overhead, MCP connection health, internal errors and session starts.
    Everything here is unavailable unless the session ran under cia run.
    """
    permissions = _permission_economics(events)
    reliability = _api_reliability(events)
    subsystems = _cost_by_subsystem(events)
    hooks = _hook_overhead(events)
    mcp = _mcp_health(events)
    errors = _internal_errors(events)
    starts = _session_starts(events)
    available = any((permissions["decisions"], reliability["available"],
                     subsystems["available"], hooks["hooks"], mcp["attempts"],
                     errors["internal_errors"] or errors["auth_failures"],
                     starts))
    return {
        "available": available,
        "permissions": permissions,
        "api_reliability": reliability,
        "subsystems": subsystems,
        "hooks": hooks,
        "mcp": mcp,
        "errors": errors,
        "session_starts": starts,
    }


def _permission_economics(events: list[Event]) -> dict:
    """tool_decision events: who approved what, and how often it was free
    (auto-approved by config/hook) vs. an interruption."""
    by_source: dict[str, int] = {}
    by_tool: dict[str, dict] = {}
    total = accepts = auto = 0
    for _, attrs in _named_otel_events(events, "tool_decision"):
        total += 1
        decision = str(attrs.get("decision") or "?")
        source = str(attrs.get("source") or "?")
        tool = str(attrs.get("tool_name") or "?")
        accepts += decision == "accept"
        auto += source in _AUTO_DECISION_SOURCES
        by_source[source] = by_source.get(source, 0) + 1
        t = by_tool.setdefault(tool, {"accepts": 0, "rejects": 0})
        t["accepts" if decision == "accept" else "rejects"] += 1
    return {
        "decisions": total,
        "accepts": accepts,
        "rejects": total - accepts,
        "auto_approved": auto,
        "auto_rate": auto / total if total else None,
        "by_source": by_source,
        "by_tool": by_tool,
    }


def _api_reliability(events: list[Event]) -> dict:
    """api_error / api_retries_exhausted / api_refusal telemetry."""
    errors = []
    for e, attrs in _named_otel_events(events, "api_error"):
        errors.append({
            "ts": e.ts,
            "model": attrs.get("model"),
            "status_code": attrs.get("status_code"),
            "attempt": _num(attrs.get("attempt")),
            "duration_ms": _num(attrs.get("duration_ms")),
            "error": str(attrs.get("error") or "")[:200],
        })
    by_status: dict[str, int] = {}
    for r in errors:
        key = str(r["status_code"] if r["status_code"] is not None else "non-http")
        by_status[key] = by_status.get(key, 0) + 1

    exhausted = []
    for e, attrs in _named_otel_events(events, "api_retries_exhausted"):
        exhausted.append({
            "ts": e.ts,
            "model": attrs.get("model"),
            "status_code": attrs.get("status_code"),
            "total_attempts": _num(attrs.get("total_attempts")),
            "retry_ms": _num(attrs.get("total_retry_duration_ms")),
        })

    refusals = []
    for e, attrs in _named_otel_events(events, "api_refusal"):
        refusals.append({
            "ts": e.ts,
            "model": attrs.get("model"),
            "category": attrs.get("category"),   # only under --detail
            "has_category": attrs.get("has_category"),
            "server_fallback_hop": attrs.get("server_fallback_hop"),
        })

    return {
        "available": bool(errors or exhausted or refusals),
        "errors": len(errors),
        "errors_by_status": by_status,
        "error_ms": sum(r["duration_ms"] or 0 for r in errors),
        "retries_exhausted": exhausted,
        "retry_ms_lost": sum(x["retry_ms"] or 0 for x in exhausted),
        "refusals": refusals,
    }


_ATTRIBUTION_KEYS = ("agent.name", "skill.name", "mcp_server.name",
                     "plugin.name")


def _cost_by_subsystem(events: list[Event]) -> dict:
    """api_request telemetry aggregated by query_source (main thread vs
    subagent vs compaction vs auxiliary) and by agent/skill/MCP attribution."""
    by_source: dict[str, dict] = {}
    attribution: dict[str, dict] = {k: {} for k in _ATTRIBUTION_KEYS}
    requests = 0
    for _, attrs in _named_otel_events(events, "api_request"):
        requests += 1
        qs = str(attrs.get("query_source") or "?")
        g = by_source.setdefault(qs, {
            "requests": 0, "cost_usd": 0.0, "input_tokens": 0,
            "output_tokens": 0, "cache_read_tokens": 0, "api_ms": 0.0,
        })
        g["requests"] += 1
        g["cost_usd"] += _num(attrs.get("cost_usd")) or 0.0
        g["input_tokens"] += int(_num(attrs.get("input_tokens")) or 0)
        g["output_tokens"] += int(_num(attrs.get("output_tokens")) or 0)
        g["cache_read_tokens"] += int(_num(attrs.get("cache_read_tokens")) or 0)
        g["api_ms"] += _num(attrs.get("duration_ms")) or 0.0
        for key in _ATTRIBUTION_KEYS:
            val = attrs.get(key)
            if val:
                a = attribution[key].setdefault(
                    str(val), {"requests": 0, "cost_usd": 0.0})
                a["requests"] += 1
                a["cost_usd"] += _num(attrs.get("cost_usd")) or 0.0
    return {
        "available": requests > 0,
        "requests": requests,
        "by_query_source": by_source,
        "attribution": {k: v for k, v in attribution.items() if v},
    }


def _hook_overhead(events: list[Event]) -> dict:
    """hook_execution_complete telemetry: what each hook (including CIA's
    own instrumentation hooks) costs the session."""
    by_hook: dict[str, dict] = {}
    for _, attrs in _named_otel_events(events, "hook_execution_complete"):
        name = str(attrs.get("hook_name") or attrs.get("hook_event") or "?")
        g = by_hook.setdefault(name, {"runs": 0, "total_ms": 0.0,
                                      "durations": [], "blocking": 0,
                                      "errors": 0})
        dur = _num(attrs.get("total_duration_ms")) or 0.0
        g["runs"] += 1
        g["total_ms"] += dur
        g["durations"].append(dur)
        g["blocking"] += int(_num(attrs.get("num_blocking")) or 0)
        g["errors"] += int(_num(attrs.get("num_non_blocking_error")) or 0)
    hooks = [
        {"hook": name, "runs": g["runs"], "total_ms": g["total_ms"],
         "p50_ms": _percentile(sorted(g["durations"]), 50),
         "max_ms": max(g["durations"]) if g["durations"] else None,
         "blocking": g["blocking"], "errors": g["errors"]}
        for name, g in by_hook.items()
    ]
    hooks.sort(key=lambda h: h["total_ms"], reverse=True)
    return {"hooks": hooks,
            "total_ms": sum(h["total_ms"] for h in hooks)}


def _mcp_health(events: list[Event]) -> dict:
    """mcp_server_connection telemetry: connect times and failures."""
    by_status: dict[str, int] = {}
    connect_ms: list[float] = []
    failures: list[dict] = []
    attempts = 0
    for e, attrs in _named_otel_events(events, "mcp_server_connection"):
        attempts += 1
        status = str(attrs.get("status") or "?")
        by_status[status] = by_status.get(status, 0) + 1
        dur = _num(attrs.get("duration_ms"))
        if status == "connected" and dur is not None:
            connect_ms.append(dur)
        if status == "failed":
            failures.append({
                "ts": e.ts,
                "server": attrs.get("server_name"),   # only under --detail
                "transport": attrs.get("transport_type"),
                "error_code": attrs.get("error_code"),
                "duration_ms": dur,
            })
    return {
        "attempts": attempts,
        "by_status": by_status,
        "connect_ms": _pctl_summary(connect_ms),
        "failures": failures,
    }


def _internal_errors(events: list[Event]) -> dict:
    """internal_error and failed auth telemetry."""
    internal: dict[str, int] = {}
    for _, attrs in _named_otel_events(events, "internal_error"):
        key = "/".join(str(attrs.get(k)) for k in ("error_name", "error_code")
                       if attrs.get(k)) or "?"
        internal[key] = internal.get(key, 0) + 1
    auth_failures = sum(
        1 for _, attrs in _named_otel_events(events, "auth")
        if str(attrs.get("success")).lower() == "false")
    return {"internal_errors": internal, "auth_failures": auth_failures}


def _session_starts(events: list[Event]) -> dict:
    """claude_code.session.count metric: sessions by start_type (fresh /
    resume / continue), counted as distinct session ids per type."""
    seen: dict[str, set] = {}
    for e in events:
        if e.phase != Phase.OTEL_METRIC:
            continue
        meta = e.meta or {}
        if meta.get("name") != "claude_code.session.count":
            continue
        attrs = meta.get("attributes") or {}
        start_type = str(attrs.get("start_type") or "?")
        sid = e.session_id or "?"
        seen.setdefault(start_type, set()).add(sid)
    return {k: len(v) for k, v in sorted(seen.items())}


# ------------------------------------------------------------------ #
# Transcript insights                                                  #
# ------------------------------------------------------------------ #

def _otel_output_tokens(events: list[Event]) -> dict[Optional[str], float]:
    """Per-session output-token totals from claude_code.token.usage."""
    series: dict[tuple, list] = {}
    temporality: dict[tuple, str] = {}
    for e in events:
        if e.phase != Phase.OTEL_METRIC:
            continue
        meta = e.meta or {}
        attrs = meta.get("attributes") or {}
        if (meta.get("name") != "claude_code.token.usage"
                or attrs.get("type") != "output" or meta.get("value") is None):
            continue
        attr_key = tuple(sorted((k, str(v)) for k, v in attrs.items()))
        key = (e.session_id, attr_key)
        series.setdefault(key, []).append((e.ts, float(meta["value"])))
        if meta.get("temporality"):
            temporality[key] = meta["temporality"]
    totals: dict[Optional[str], float] = {}
    for key, points in series.items():
        points.sort()
        total = sum(d for _, d in _series_deltas(points, temporality.get(key)))
        totals[key[0]] = totals.get(key[0], 0.0) + total
    return totals


def transcript_insights(events: list[Event],
                        transcripts: Optional[dict] = None,
                        insights: Optional[dict] = None) -> dict:
    """Analytics over Claude Code's on-disk transcripts and /insights data
    for the sessions in the event store: session identity, exact usage,
    per-agent-type subagent economics, delivery stats, and a three-way
    output-token agreement check (transcript vs proxy vs native telemetry).
    """
    sids = sorted({e.session_id for e in events if e.session_id})
    if transcripts is None or insights is None:
        from cia.transcripts import load_insights, session_transcripts
        if transcripts is None:
            transcripts = session_transcripts(sids)
        if insights is None:
            insights = load_insights(sids)
    if not transcripts and not insights:
        return {"available": False}

    flow_sid = api_flow_sessions(events)
    otel_output = _otel_output_tokens(events)

    sessions: dict[str, dict] = {}
    subagents: dict[str, dict] = {}
    for sid in sids:
        tr = transcripts.get(sid)
        ins = insights.get(sid) or {}
        if not tr and not ins:
            continue

        proxy_output = 0
        if tr or ins:
            sev = [e for e in events if e.session_id == sid]
            t0, t1 = sev[0].ts, sev[-1].ts
            mine = _session_filter(flow_sid, sid)
            proxy_output = sum(
                e.tokens_output or 0 for e in events
                if e.phase == Phase.API_RESPONSE_END and t0 <= e.ts <= t1
                and mine(e))

        transcript_output = tr["usage"]["output"] if tr else 0
        sources = {"transcript": transcript_output,
                   "proxy": proxy_output,
                   "otel": otel_output.get(sid, 0.0)}
        present = {k: v for k, v in sources.items() if v}
        disagreement = None
        if len(present) >= 2:
            lo, hi = min(present.values()), max(present.values())
            disagreement = round((hi - lo) / hi, 4) if hi else 0.0

        entry = {
            "title": tr["title"] if tr else None,
            "project": _project_display(tr["project_path"]) if tr else None,
            "sandbox": _is_sandbox_path(tr["project_path"]) if tr else False,
            "usage": tr["usage"] if tr else None,
            "by_model": tr["by_model"] if tr else {},
            "prompts": len(tr["prompts"]) if tr else 0,
            "subagents": len(tr["subagents"]) if tr else 0,
            "insights": ins or None,
            "agreement": {"output_tokens": sources,
                          "disagreement_frac": disagreement},
        }
        sessions[sid] = entry

        for sub in (tr["subagents"] if tr else []):
            g = subagents.setdefault(sub["agent_type"], {
                "runs": 0, "output_tokens": 0, "input_tokens": 0,
                "cache_read_tokens": 0, "tool_calls": 0,
            })
            g["runs"] += 1
            g["output_tokens"] += sub["usage"]["output"]
            g["input_tokens"] += sub["usage"]["input"]
            g["cache_read_tokens"] += sub["usage"]["cache_read"]
            g["tool_calls"] += sub["tool_calls"]

    return {
        "available": bool(sessions),
        "sessions": sessions,
        "subagent_economics": subagents,
    }


# ------------------------------------------------------------------ #
# Full report                                                          #
# ------------------------------------------------------------------ #

def full_report(events: list[Event],
                use_transcripts: bool = True) -> dict[str, Any]:
    """All derived analytics in one JSON-serialisable dict.

    ``use_transcripts=False`` skips reading Claude Code's on-disk
    transcripts / usage-data (cia report --no-transcripts).
    """
    if use_transcripts:
        from cia.transcripts import load_insights, session_transcripts
        sids = sorted({e.session_id for e in events if e.session_id})
        transcripts = session_transcripts(sids)
        insights = load_insights(sids)
    else:
        transcripts, insights = {}, {}
    return {
        "sessions": session_stories(events, transcripts, insights),
        "turns": turn_anatomy(events),
        "tools": tool_profiles(events),
        "chains": tool_chains(events),
        "human": human_latency(events),
        "compactions": compaction_cost(events),
        "rework": rework(events),
        "cache": cache_economics(events),
        "thinking": thinking_calibration(events),
        "context": context_pressure(events),
        "cost": cost_attribution(events),
        "throughput": throughput(events),
        "network": network_overhead(events),
        "otel": otel_insights(events),
        "transcripts": transcript_insights(events, transcripts, insights),
    }
