"""Unit tests for cia.analytics — derived metrics over synthetic event streams."""
from __future__ import annotations

from cia.analytics import (
    compaction_cost,
    full_report,
    human_latency,
    pair_tool_calls,
    rework,
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


def test_full_report_shape():
    events = (
        [E(Phase.USER_PROMPT, 0, session_id=SID, meta={"prompt": "x"})]
        + api_call(1)
        + tool_pair(4, "Bash", "t1")
        + [E(Phase.TURN_END, 6, session_id=SID)]
    )
    report = full_report(events)
    assert set(report) == {"turns", "tools", "human", "compactions", "rework"}
    assert len(report["turns"]) == 1
    assert report["tools"][0]["tool"] == "Bash"
