"""Unit tests for cia.analytics — derived metrics over synthetic event streams."""
from __future__ import annotations

from cia.analytics import (
    cache_economics,
    compaction_cost,
    context_pressure,
    cost_attribution,
    full_report,
    human_latency,
    network_overhead,
    pair_tool_calls,
    rework,
    session_stories,
    thinking_calibration,
    throughput,
    tool_chains,
    tool_profiles,
    turn_anatomy,
)
from cia.schema import Event, Phase

SID = "sess-1"
T0 = 1_000_000.0


def E(phase: Phase, ts: float, **kw) -> Event:
    return Event(phase=phase, ts=T0 + ts, **kw)


def tool_pair(ts: float, tool: str, use_id: str, dur: float = 1.0,
              is_error: bool = False, file_path: str | None = None,
              output_bytes: int = 100) -> list[Event]:
    tool_input = {"file_path": file_path} if file_path else {"command": "x"}
    return [
        E(Phase.TOOL_CALL_START, ts, session_id=SID, tool=tool,
          tool_input=tool_input, meta={"tool_use_id": use_id}),
        E(Phase.TOOL_CALL_END, ts + dur, session_id=SID, tool=tool,
          error="boom" if is_error else None,
          meta={"tool_use_id": use_id,
                "tool_result": {"is_error": is_error, "output_bytes": output_bytes}}),
    ]


def api_call(ts: float, dur: float = 2.0, tokens_in: int = 1000,
             tokens_out: int = 50, cache_read: int = 0,
             thinking: float = 0.5) -> list[Event]:
    events = [
        E(Phase.API_REQUEST_START, ts, model="m", tokens_input=tokens_in,
          meta={"cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": 0}),
    ]
    if thinking:
        events += [
            E(Phase.API_THINKING_START, ts + 0.2, model="m"),
            E(Phase.API_THINKING_END, ts + 0.2 + thinking, model="m",
              duration_ms=thinking * 1000),
        ]
    events += [
        E(Phase.API_GENERATION_END, ts + dur - 0.1, model="m",
          duration_ms=(dur - 0.3 - thinking) * 1000),
        E(Phase.API_RESPONSE_END, ts + dur, model="m", duration_ms=dur * 1000,
          tokens_input=tokens_in, tokens_output=tokens_out,
          meta={"cache_read_input_tokens": cache_read}),
    ]
    return events


# ------------------------------------------------------------------ #
# pair_tool_calls / tool_profiles                                      #
# ------------------------------------------------------------------ #

def test_pairs_by_tool_use_id_even_when_interleaved():
    events = [
        E(Phase.TOOL_CALL_START, 0, session_id=SID, tool="Bash",
          meta={"tool_use_id": "a"}),
        E(Phase.TOOL_CALL_START, 1, session_id=SID, tool="Bash",
          meta={"tool_use_id": "b"}),
        E(Phase.TOOL_CALL_END, 5, session_id=SID, tool="Bash",
          meta={"tool_use_id": "a"}),
        E(Phase.TOOL_CALL_END, 2, session_id=SID, tool="Bash",
          meta={"tool_use_id": "b"}),
    ]
    pairs = sorted(pair_tool_calls(events), key=lambda p: p["start_ts"])
    assert [round(p["duration_ms"]) for p in pairs] == [5000, 1000]


def test_pairs_fall_back_to_session_and_tool():
    events = [
        E(Phase.TOOL_CALL_START, 0, session_id=SID, tool="Read"),
        E(Phase.TOOL_CALL_END, 2, session_id=SID, tool="Read"),
    ]
    pairs = pair_tool_calls(events)
    assert len(pairs) == 1 and round(pairs[0]["duration_ms"]) == 2000


def test_unmatched_start_is_dropped():
    events = [E(Phase.TOOL_CALL_START, 0, session_id=SID, tool="Bash",
                meta={"tool_use_id": "zombie"})]
    assert pair_tool_calls(events) == []


def test_tool_profiles_percentiles_and_errors():
    events = []
    for i, dur in enumerate([1, 2, 3, 4, 10]):
        events += tool_pair(i * 100, "Bash", f"b{i}", dur=dur,
                            is_error=(i == 0))
    profiles = tool_profiles(events)
    assert len(profiles) == 1
    p = profiles[0]
    assert p["tool"] == "Bash"
    assert p["calls"] == 5
    assert p["errors"] == 1
    assert p["error_rate"] == 0.2
    assert p["p50_ms"] == 3000
    assert p["max_ms"] == 10000
    assert p["avg_output_bytes"] == 100


# ------------------------------------------------------------------ #
# human_latency                                                        #
# ------------------------------------------------------------------ #

def test_permission_wait_resolved_by_tool_call():
    events = [
        E(Phase.NOTIFICATION, 0, session_id=SID,
          meta={"message": "Claude needs your permission to use Bash"}),
        E(Phase.TOOL_CALL_START, 12, session_id=SID, tool="Bash",
          meta={"tool_use_id": "x"}),
    ]
    hl = human_latency(events)
    assert len(hl["permission_waits"]) == 1
    w = hl["permission_waits"][0]
    assert round(w["wait_s"]) == 12
    assert w["resolution"] == "approved"
    assert w["tool"] == "Bash"


def test_permission_wait_denied_resolves_at_next_prompt():
    events = [
        E(Phase.NOTIFICATION, 0, session_id=SID,
          meta={"message": "Claude needs your permission to use Bash"}),
        E(Phase.USER_PROMPT, 30, session_id=SID, meta={"prompt": "no, do Y"}),
    ]
    hl = human_latency(events)
    assert hl["permission_waits"][0]["resolution"] == "prompt"
    assert round(hl["permission_waits"][0]["wait_s"]) == 30


def test_think_time_between_turns_not_across_session_end():
    events = [
        E(Phase.TURN_END, 0, session_id=SID),
        E(Phase.USER_PROMPT, 90, session_id=SID, meta={"prompt": "next"}),
        E(Phase.TURN_END, 100, session_id=SID),
        E(Phase.SESSION_END, 110, session_id=SID),
        E(Phase.USER_PROMPT, 7200, session_id=SID, meta={"prompt": "tomorrow"}),
    ]
    hl = human_latency(events)
    assert len(hl["think_times"]) == 1
    assert round(hl["think_times"][0]["wait_s"]) == 90
    assert hl["summary"]["think"]["count"] == 1


# ------------------------------------------------------------------ #
# compaction_cost                                                      #
# ------------------------------------------------------------------ #

def test_compaction_reclaim_uses_min_context_after():
    events = (
        api_call(0, tokens_in=1000, cache_read=150_000)      # context 151k
        + [E(Phase.CONTEXT_COMPACT, 10, session_id=SID,
             meta={"trigger": "auto"})]
        + api_call(12, tokens_in=160_000)                     # summarisation call
        + api_call(20, tokens_in=30_000)                      # post-compact context
    )
    costs = compaction_cost(events)
    assert len(costs) == 1
    c = costs[0]
    assert c["trigger"] == "auto"
    assert c["context_before"] == 151_000
    assert c["context_after"] == 30_000
    assert c["reclaimed_tokens"] == 121_000
    assert round(c["recovery_s"]) == 2


def test_compaction_with_no_following_requests():
    events = [E(Phase.CONTEXT_COMPACT, 0, session_id=SID, meta={"trigger": "manual"})]
    c = compaction_cost(events)[0]
    assert c["context_before"] is None
    assert c["reclaimed_tokens"] is None


# ------------------------------------------------------------------ #
# turn_anatomy                                                         #
# ------------------------------------------------------------------ #

def test_turn_anatomy_breakdown():
    events = (
        [E(Phase.USER_PROMPT, 0, session_id=SID, meta={"prompt": "fix the bug"})]
        + api_call(1, dur=4.0, tokens_out=100, thinking=1.5)
        + tool_pair(6, "Bash", "t1", dur=3.0)
        + [E(Phase.NOTIFICATION, 10, session_id=SID,
             meta={"message": "Claude needs your permission to use Edit"})]
        + tool_pair(15, "Edit", "t2", dur=1.0, file_path="/tmp/x.py")
        + api_call(17, dur=2.0, tokens_out=50, thinking=0)
        + [E(Phase.TURN_END, 20, session_id=SID)]
    )
    turns = turn_anatomy(events)
    assert len(turns) == 1
    t = turns[0]
    assert round(t["wall_ms"]) == 20_000
    assert round(t["api_ms"]) == 6000          # 4s + 2s
    assert round(t["thinking_ms"]) == 1500
    assert round(t["tool_ms"]) == 4000         # 3s + 1s
    assert round(t["permission_wait_ms"]) == 5000   # notification@10 → start@15
    assert t["api_calls"] == 2
    assert t["tool_calls"] == 2
    assert t["edits"] == 1
    assert t["tokens_output"] == 150
    assert t["prompt"] == "fix the bug"
    assert t["other_ms"] == 20_000 - 6000 - 4000 - 5000


def test_turns_are_per_session_and_sequential():
    events = [
        E(Phase.USER_PROMPT, 0, session_id="s1", meta={"prompt": "a"}),
        E(Phase.USER_PROMPT, 1, session_id="s2", meta={"prompt": "b"}),
        E(Phase.TURN_END, 5, session_id="s1"),
        E(Phase.TURN_END, 6, session_id="s2"),
        E(Phase.USER_PROMPT, 10, session_id="s1", meta={"prompt": "c"}),
        E(Phase.TURN_END, 12, session_id="s1"),
    ]
    turns = turn_anatomy(events)
    assert len(turns) == 3
    assert [t["prompt"] for t in turns] == ["a", "b", "c"]


# ------------------------------------------------------------------ #
# rework                                                               #
# ------------------------------------------------------------------ #

def test_rework_flags_repeated_edits_in_one_turn():
    events = [E(Phase.USER_PROMPT, 0, session_id=SID, meta={"prompt": "go"})]
    for i in range(4):
        events += tool_pair(1 + i, "Edit", f"e{i}", dur=0.5,
                            file_path="/src/thrash.py")
    events += tool_pair(8, "Edit", "e9", dur=0.5, file_path="/src/fine.py")
    events += [
        E(Phase.TURN_END, 10, session_id=SID),
        E(Phase.FILE_CHANGE, 2, meta={"path": "/src/thrash.py"}),
    ]
    results = rework(events, threshold=3)
    by_file = {r["file"]: r for r in results}
    assert by_file["/src/thrash.py"]["flagged"] is True
    assert by_file["/src/thrash.py"]["edits"] == 4
    assert by_file["/src/thrash.py"]["max_edits_one_turn"] == 4
    assert by_file["/src/thrash.py"]["file_changes"] == 1
    assert by_file["/src/fine.py"]["flagged"] is False


def test_open_turn_is_kept_and_marked_incomplete():
    events = (
        [E(Phase.USER_PROMPT, 0, session_id=SID, meta={"prompt": "first"}),
         E(Phase.TURN_END, 5, session_id=SID),
         E(Phase.USER_PROMPT, 10, session_id=SID, meta={"prompt": "still going"})]
        + tool_pair(11, "Bash", "t1", dur=2.0)
    )
    turns = turn_anatomy(events)
    assert len(turns) == 2
    assert turns[0]["complete"] is True
    open_turn = turns[1]
    assert open_turn["complete"] is False
    assert open_turn["prompt"] == "still going"
    assert open_turn["tool_calls"] == 1
    assert round(open_turn["wall_ms"]) == 3000   # closed at last session event


# ------------------------------------------------------------------ #
# session_stories                                                      #
# ------------------------------------------------------------------ #

def test_session_story_aggregates_and_full_coverage():
    events = (
        [E(Phase.SESSION_START, 0, session_id=SID, meta={"source": "startup"}),
         E(Phase.USER_PROMPT, 1, session_id=SID, meta={"prompt": "go"})]
        + api_call(2, dur=3.0, tokens_in=500, tokens_out=80, cache_read=2000,
                   thinking=1.0)
        + tool_pair(6, "Edit", "t1", dur=1.0, file_path="/x.py", is_error=True)
        + [E(Phase.FILE_CHANGE, 6.5, meta={"path": "/x.py"}),
           E(Phase.TURN_END, 8, session_id=SID),
           E(Phase.SESSION_END, 9, session_id=SID, meta={"reason": "exit"})]
    )
    stories = session_stories(events)
    assert len(stories) == 1
    s = stories[0]
    assert s["turns"] == 1 and s["incomplete_turns"] == 0
    assert s["api_calls"] == 1
    assert s["tokens_input"] == 500 and s["tokens_output"] == 80
    assert s["cache_read_tokens"] == 2000
    assert round(s["thinking_ms"]) == 1000
    assert s["tool_calls"] == 1 and s["tool_errors"] == 1 and s["edits"] == 1
    assert s["ended"] is True and s["end_reason"] == "exit"
    assert s["models"] == ["m"]
    assert s["coverage"] == {"hooks": True, "proxy": True, "fswatch": True}
    assert s["gaps"] == []


def test_session_story_flags_missing_proxy_coverage():
    events = [
        E(Phase.USER_PROMPT, 0, session_id=SID, meta={"prompt": "hi"}),
        E(Phase.TURN_END, 5, session_id=SID),
    ]
    s = session_stories(events)[0]
    assert s["coverage"]["proxy"] is False
    assert any("no proxy data" in g for g in s["gaps"])
    assert s["api_calls"] == 0


def test_full_report_shape():
    events = (
        [E(Phase.USER_PROMPT, 0, session_id=SID, meta={"prompt": "x"})]
        + api_call(1)
        + tool_pair(4, "Bash", "t1")
        + [E(Phase.TURN_END, 6, session_id=SID)]
    )
    report = full_report(events)
    assert set(report) == {"sessions", "turns", "tools", "chains", "human",
                           "compactions", "rework", "cache", "thinking",
                           "context", "cost", "throughput", "network"}
    assert len(report["turns"]) == 1
    assert report["tools"][0]["tool"] == "Bash"


# ------------------------------------------------------------------ #
# cache_economics                                                      #
# ------------------------------------------------------------------ #

def cached_call(ts: float, cache_read: int, cache_creation: int,
                fresh: int = 1000, ttfb: float | None = None,
                dur: float = 2.0) -> list[Event]:
    meta = {"cache_read_input_tokens": cache_read,
            "cache_creation_input_tokens": cache_creation}
    if ttfb is not None:
        meta["ttfb_ms"] = ttfb
    return [
        E(Phase.API_REQUEST_START, ts, model="m", tokens_input=fresh, meta=meta),
        E(Phase.API_RESPONSE_END, ts + dur, model="m", duration_ms=dur * 1000,
          tokens_input=fresh, tokens_output=100,
          meta={"usage": {"cache_read_input_tokens": cache_read,
                          "cache_creation_input_tokens": cache_creation}}),
    ]


def test_cache_hit_rate_and_warm_cold_ttfb():
    events = (
        cached_call(0, 0, 50_000, ttfb=2000)
        + cached_call(10, 51_000, 1_000, ttfb=400)
        + cached_call(20, 52_000, 1_000, ttfb=600)
    )
    c = cache_economics(events)
    assert c["requests"] == 3
    assert c["warm_requests"] == 2
    assert abs(c["hit_rate"] - 2 / 3) < 1e-9
    assert c["ttfb_ms"]["warm"]["p50"] == 500
    assert c["ttfb_ms"]["cold"]["p50"] == 2000
    assert c["busts"] == []
    assert c["ttl"]["expiries"] == 0


def test_cache_bust_attributed_to_ttl_expiry():
    events = cached_call(0, 0, 50_000) + cached_call(400, 0, 52_000)
    c = cache_economics(events)
    assert len(c["busts"]) == 1
    b = c["busts"][0]
    assert b["cause"] == "ttl_expired"
    assert b["retokenized_tokens"] == 52_000
    assert round(b["idle_s"]) == 398          # gap from response end at ts=2
    assert c["ttl"]["expiries"] == 1
    assert c["ttl"]["retokenized_tokens"] == 52_000


def test_cache_bust_compaction_takes_priority_over_ttl():
    events = (
        cached_call(0, 0, 50_000)
        + [E(Phase.CONTEXT_COMPACT, 350, session_id=SID, meta={"trigger": "auto"})]
        + cached_call(400, 0, 30_000)
    )
    c = cache_economics(events)
    assert c["busts"][0]["cause"] == "compaction"


def test_cache_bust_attributed_to_prompt_change():
    r1 = cached_call(0, 0, 50_000)
    r1[0].meta["request"] = {"system_chars": 100, "tools_chars": 5}
    r2 = cached_call(10, 0, 50_000)
    r2[0].meta["request"] = {"system_chars": 999, "tools_chars": 5}
    c = cache_economics(r1 + r2)
    assert c["busts"][0]["cause"] == "prompt_change"


# ------------------------------------------------------------------ #
# thinking_calibration                                                 #
# ------------------------------------------------------------------ #

def think_response(ts: float, fired: bool = True, util: float | None = None,
                   effort: str | None = None,
                   interrupted: bool = False) -> Event:
    return E(Phase.API_RESPONSE_END, ts, model="m", duration_ms=2000,
             meta={"thinking": {
                 "thinking_requested": True,
                 "thinking_fired": fired,
                 "budget_utilization": util,
                 "requested_effort": effort,
                 "interrupted": interrupted,
                 "thinking_ms": 1000.0 if fired else None,
             }})


def test_thinking_fire_rate_budget_and_decisiveness():
    events = [
        think_response(0, fired=True, util=0.4, effort="high"),
        think_response(10, fired=False, effort="high"),
        E(Phase.API_GENERATION_START, 1, model="m",
          meta={"thinking_to_tool_ms": 100.0}),
        E(Phase.API_GENERATION_START, 2, model="m",
          meta={"thinking_to_tool_ms": 300.0}),
    ]
    th = thinking_calibration(events)
    assert th["thinking_requested"] == 2
    assert th["thinking_fired"] == 1
    assert th["fire_rate"] == 0.5
    assert th["budget"]["utilization_p50"] == 0.4
    assert th["by_effort"]["high"]["fire_rate"] == 0.5
    assert th["decisiveness_ms"]["m"]["p50"] == 200
    assert th["turn_split"] is None   # fewer than 2 turns


def test_thinking_turn_split_compares_downstream_errors():
    events = (
        [E(Phase.USER_PROMPT, 0, session_id=SID, meta={"prompt": "a"})]
        + api_call(1, thinking=2.0)
        + tool_pair(4, "Bash", "ok1")
        + [E(Phase.TURN_END, 6, session_id=SID),
           E(Phase.USER_PROMPT, 10, session_id=SID, meta={"prompt": "b"})]
        + api_call(11, thinking=0)
        + tool_pair(14, "Bash", "bad1", is_error=True)
        + [E(Phase.TURN_END, 16, session_id=SID)]
    )
    split = thinking_calibration(events)["turn_split"]
    assert split["high_thinking"]["turns"] == 1
    assert split["high_thinking"]["mean_tool_errors"] == 0
    assert split["low_thinking"]["mean_tool_errors"] == 1


# ------------------------------------------------------------------ #
# context_pressure                                                     #
# ------------------------------------------------------------------ #

def test_context_pressure_growth_bloat_and_projection():
    events = (
        [E(Phase.USER_PROMPT, 0, session_id=SID, meta={"prompt": "a"})]
        + api_call(1, tokens_in=10_000)
        + [E(Phase.TURN_END, 5, session_id=SID),
           E(Phase.USER_PROMPT, 10, session_id=SID, meta={"prompt": "b"})]
        + api_call(11, tokens_in=15_000)
        + tool_pair(13, "Read", "r1", output_bytes=4096)
        + [E(Phase.TURN_END, 15, session_id=SID)]
    )
    cp = context_pressure(events, compaction_threshold=20_000)
    rows = cp["turns"]
    assert rows[0]["context_delta"] is None
    assert rows[1]["context_delta"] == 5_000
    assert rows[1]["tool_output_bytes"] == 4096
    assert rows[1]["top_tool"] == "Read"
    assert cp["growth_per_turn_p50"] == 5_000
    assert cp["projected_turns_to_compaction"][SID] == 1.0
    assert cp["bloat_by_tool"][0]["tool"] == "Read"


def test_context_pressure_threshold_inferred_from_compaction():
    events = (
        [E(Phase.USER_PROMPT, 0, session_id=SID, meta={"prompt": "a"})]
        + api_call(1, tokens_in=150_000)
        + [E(Phase.TURN_END, 5, session_id=SID),
           E(Phase.CONTEXT_COMPACT, 6, session_id=SID, meta={"trigger": "auto"})]
        + api_call(8, tokens_in=30_000)
    )
    cp = context_pressure(events)
    assert cp["compaction_threshold"] == 150_000


# ------------------------------------------------------------------ #
# tool_chains                                                          #
# ------------------------------------------------------------------ #

def test_retry_loop_detection():
    events = []
    for i in range(3):
        events += tool_pair(i * 2, "Bash", f"r{i}", is_error=(i < 2))
    loops = tool_chains(events)["retry_loops"]
    assert len(loops) == 1
    assert loops[0]["tool"] == "Bash"
    assert loops[0]["repeats"] == 3
    assert loops[0]["errors"] == 2
    assert loops[0]["target"] == "x"


def test_transitions_and_search_thrash():
    events = [E(Phase.USER_PROMPT, 0, session_id=SID, meta={"prompt": "find it"})]
    for i in range(3):
        events += [
            E(Phase.TOOL_CALL_START, 1 + i, session_id=SID, tool="Grep",
              tool_input={"pattern": f"p{i}"},
              meta={"tool_use_id": f"g{i}", "pattern": f"p{i}"}),
            E(Phase.TOOL_CALL_END, 1.5 + i, session_id=SID, tool="Grep",
              meta={"tool_use_id": f"g{i}",
                    "tool_result": {"is_error": False, "output_bytes": 10}}),
        ]
    events += tool_pair(5, "Read", "rd", file_path="/a.py")
    events += [E(Phase.TURN_END, 8, session_id=SID)]
    chains = tool_chains(events)
    st = chains["search_thrash"]
    assert st["searches"] == 3 and st["reads"] == 1
    assert len(st["thrash_turns"]) == 1
    assert st["thrash_turns"][0]["searches_before_first_read"] == 3
    trans = {(t["from"], t["to"]): t["count"] for t in chains["transitions"]}
    assert trans[("Grep", "Grep")] == 2
    assert trans[("Grep", "Read")] == 1
    assert chains["retry_loops"] == []   # distinct patterns, no loop


def test_error_recovery_time_and_calls():
    events = (
        tool_pair(0, "Bash", "e1", dur=1.0, is_error=True)
        + tool_pair(5, "Edit", "ok", dur=1.0, file_path="/a.py")
    )
    er = tool_chains(events)["error_recovery"]
    assert er["errors"] == 1 and er["recovered"] == 1
    assert er["unrecovered"] == 0
    assert er["recovery_calls_p50"] == 1
    assert round(er["recovery_ms_p50"]) == 5000   # end@1 → end@6


# ------------------------------------------------------------------ #
# cost_attribution                                                     #
# ------------------------------------------------------------------ #

def metric(ts: float, name: str, value: float, sid: str = SID, **attrs) -> Event:
    return E(Phase.OTEL_METRIC, ts, session_id=sid,
             meta={"name": name, "value": value,
                   "attributes": {"session.id": sid, **attrs}})


def test_cost_attribution_cumulative_series_turns_and_rework():
    events = (
        [E(Phase.USER_PROMPT, 0, session_id=SID, meta={"prompt": "a"})]
        + tool_pair(1, "Edit", "e1", file_path="/x.py")
        + [E(Phase.TURN_END, 5, session_id=SID),
           E(Phase.USER_PROMPT, 10, session_id=SID, meta={"prompt": "b"})]
        + tool_pair(11, "Edit", "e2", file_path="/x.py")
        + [E(Phase.TURN_END, 15, session_id=SID),
           metric(3, "claude_code.cost.usage", 0.5),
           metric(12, "claude_code.cost.usage", 0.8),
           metric(3, "claude_code.token.usage", 1000, type="input"),
           metric(12, "claude_code.token.usage", 2500, type="input"),
           metric(14, "claude_code.lines_of_code.count", 120, type="added"),
           metric(14, "claude_code.commit.count", 2)]
    )
    cost = cost_attribution(events)
    assert cost["available"]
    s = cost["sessions"][SID]
    assert abs(s["cost_usd"] - 0.8) < 1e-9       # cumulative series → last total
    assert s["tokens"]["input"] == 2500
    assert s["lines_added"] == 120 and s["commits"] == 2
    t1, t2 = cost["turns"]
    assert abs(t1["cost_usd"] - 0.5) < 1e-9
    assert abs(t2["cost_usd"] - 0.3) < 1e-9
    assert t1["rework"] is False
    assert t2["rework"] is True                  # /x.py re-edited across turns
    assert abs(cost["rework_cost_usd"] - 0.3) < 1e-9
    assert abs(cost["cost_per_commit_usd"] - 0.4) < 1e-9
    assert abs(cost["cost_per_line_added_usd"] - 0.8 / 120) < 1e-9


def test_cost_delta_series_passed_through_and_unattributed():
    events = [metric(1, "claude_code.cost.usage", 0.5),
              metric(2, "claude_code.cost.usage", 0.3)]   # not monotonic → delta
    cost = cost_attribution(events)
    assert abs(cost["total_cost_usd"] - 0.8) < 1e-9
    assert abs(cost["unattributed_usd"] - 0.8) < 1e-9     # no turns to attach to


def test_cost_unavailable_without_otel_metrics():
    assert cost_attribution([E(Phase.USER_PROMPT, 0, session_id=SID,
                               meta={"prompt": "x"})]) == {"available": False}


# ------------------------------------------------------------------ #
# throughput                                                           #
# ------------------------------------------------------------------ #

def lat_response(ts: float, tok_s: float | None = None,
                 ttfb: float | None = None, dur: float = 2.0) -> Event:
    return E(Phase.API_RESPONSE_END, ts, model="m", duration_ms=dur * 1000,
             tokens_output=100,
             meta={"latency": {"output_tokens_per_sec": tok_s,
                               "ttfb_ms": ttfb}})


def test_throughput_by_model_slow_requests_and_sag():
    events = [
        lat_response(0, tok_s=10, ttfb=500),
        lat_response(10, tok_s=20, ttfb=700),
        lat_response(20, tok_s=30, ttfb=900),
        E(Phase.API_PROGRESS, 5, model="m", duration_ms=5000,
          meta={"flow_id": "f1", "est_output_tokens": 100}),
        E(Phase.API_PROGRESS, 10, model="m", duration_ms=10_000,
          meta={"flow_id": "f1", "est_output_tokens": 300}),
        E(Phase.API_PROGRESS, 15, model="m", duration_ms=15_000,
          meta={"flow_id": "f1", "est_output_tokens": 350}),
    ]
    tp = throughput(events)
    m = tp["by_model"]["m"]
    assert m["requests"] == 3
    assert m["tok_per_sec"]["p50"] == 20
    assert m["ttfb_ms"]["p50"] == 700
    assert tp["slow_requests"][0]["ttfb_ms"] == 900
    sag = tp["sag"]
    assert sag["flows"] == 1
    assert sag["early_tok_per_sec"] == 40    # (300-100)/5s
    assert sag["late_tok_per_sec"] == 10     # (350-300)/5s
    assert abs(sag["late_to_early_ratio"] - 0.25) < 1e-9


# ------------------------------------------------------------------ #
# network_overhead                                                     #
# ------------------------------------------------------------------ #

def net_req(ts: float, category: str, status: int = 200, dur: float = 100.0,
            host: str = "statsig.anthropic.com") -> Event:
    return E(Phase.NETWORK_REQUEST, ts, duration_ms=dur,
             error=f"HTTP {status}" if status >= 400 else None,
             meta={"category": category, "host": host, "path": "/x",
                   "status": status, "request_bytes": 100,
                   "response_bytes": 200})


def test_network_overhead_categories_totals_and_failures():
    events = [
        E(Phase.API_REQUEST_START, 0, model="m", meta={"flow_id": "f1"}),
        net_req(1, "telemetry", status=500, dur=50),
        E(Phase.API_RESPONSE_END, 4, model="m", duration_ms=4000,
          meta={"flow_id": "f1"}),
        net_req(10, "telemetry", dur=100),
        net_req(11, "feature_flags", dur=150, host="statsig.com"),
    ]
    net = network_overhead(events)
    cats = {c["category"]: c for c in net["by_category"]}
    assert cats["telemetry"]["requests"] == 2
    assert cats["telemetry"]["errors"] == 1
    assert cats["telemetry"]["total_bytes"] == 600
    assert cats["feature_flags"]["total_ms"] == 150
    tot = net["totals"]
    assert tot["overhead_requests"] == 3
    assert tot["overhead_ms"] == 300
    assert abs(tot["overhead_time_frac"] - 300 / 4300) < 1e-9
    assert len(net["failures"]) == 1
    assert net["failures"][0]["during_api_call"] is True   # inside f1's window
