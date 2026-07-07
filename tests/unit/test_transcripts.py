"""Unit tests for cia.transcripts — on-disk transcript / usage-data parsing."""
from __future__ import annotations

import json

from cia.transcripts import (
    clean_prompt_text,
    find_session_files,
    friendly_model,
    friendly_tool,
    load_insights,
    parse_transcript,
    project_display,
    session_transcripts,
    tool_use_name,
)

SID = "aaaa1111-2222-3333-4444-555566667777"


def _user(text, ts="2026-07-07T10:00:00.000Z", **extra):
    return {"type": "user", "timestamp": ts, "cwd": "/Users/x/proj",
            "message": {"role": "user", "content": text}, **extra}


def _assistant(model="claude-sonnet-4-6", output=100, tools=(),
               ts="2026-07-07T10:00:05.000Z"):
    content = [{"type": "text", "text": "ok"}]
    content += [{"type": "tool_use", "name": name, "input": inp}
                for name, inp in tools]
    return {"type": "assistant", "timestamp": ts,
            "message": {"role": "assistant", "model": model,
                        "content": content,
                        "usage": {"input_tokens": 10, "output_tokens": output,
                                  "cache_read_input_tokens": 500,
                                  "cache_creation_input_tokens": 20}}}


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _make_session(tmp_path, sid=SID, with_subagent=True):
    proj = tmp_path / "projects" / "-Users-x-proj"
    _write_jsonl(proj / f"{sid}.jsonl", [
        {"type": "ai-title", "aiTitle": "Fix the flux capacitor", "sessionId": sid},
        _user("please fix it"),
        _user("<system-reminder>injected</system-reminder>", extra_field=1),
        _user("tool result envelope", ts="2026-07-07T10:00:02.000Z",
              isMeta=True),
        _assistant(tools=[("Bash", {"command": "ls"}),
                          ("Agent", {"subagent_type": "Explore"}),
                          ("Skill", {"skill": "verify"}),
                          ("mcp__linear__create_issue", {})]),
        {"type": "file-history-snapshot", "snapshot": {}},
    ])
    if with_subagent:
        sub = proj / sid / "subagents" / "agent-abc123.jsonl"
        _write_jsonl(sub, [
            _user("dispatch brief written by Claude"),   # not a human prompt
            _assistant(model="claude-haiku-4-5-20251001", output=40,
                       tools=[("Read", {"file_path": "/x"})]),
        ])
        (sub.parent / "agent-abc123.meta.json").write_text(
            json.dumps({"agentType": "Explore", "toolUseId": "toolu_1"}))
    return tmp_path / "projects"


def test_parse_transcript_extracts_usage_tools_title_prompts(tmp_path):
    projects = _make_session(tmp_path, with_subagent=False)
    rec = parse_transcript(projects / "-Users-x-proj" / f"{SID}.jsonl")
    assert rec["title"] == "Fix the flux capacitor"
    assert rec["project_path"] == "/Users/x/proj"
    assert rec["usage"] == {"input": 10, "output": 100,
                            "cache_read": 500, "cache_creation": 20}
    assert rec["by_model"]["claude-sonnet-4-6"]["output"] == 100
    # Agent/Skill fold in what they invoked; MCP names kept raw
    assert rec["tool_counts"] == {"Bash": 1, "Agent:Explore": 1,
                                  "Skill:verify": 1,
                                  "mcp__linear__create_issue": 1}
    # wrapper-tag-only and isMeta user records are not prompts
    assert [p["text"] for p in rec["prompts"]] == ["please fix it"]
    assert rec["first_ts"] is not None and rec["last_ts"] >= rec["first_ts"]


def test_bad_lines_are_skipped(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text('not json\n{"type":"assistant","message":{"role":"assistant",'
                 '"model":"m","usage":{"output_tokens":5},"content":[]}}\n')
    rec = parse_transcript(p)
    assert rec["usage"]["output"] == 5


def test_session_transcripts_rolls_up_subagents(tmp_path):
    projects = _make_session(tmp_path)
    recs = session_transcripts([SID, None, "missing"], projects_dir=projects)
    assert set(recs) == {SID}
    rec = recs[SID]
    # subagent usage rolled into session totals…
    assert rec["usage"]["output"] == 140
    # …and kept separately with the agentType from .meta.json
    assert len(rec["subagents"]) == 1
    sub = rec["subagents"][0]
    assert sub["agent_type"] == "Explore"
    assert sub["usage"]["output"] == 40
    assert sub["tool_calls"] == 1
    # subagent "user" records are dispatch briefs, not prompts
    assert [p["text"] for p in rec["prompts"]] == ["please fix it"]


def test_find_session_files_missing_dir(tmp_path):
    assert find_session_files([SID], projects_dir=tmp_path / "nope") == {}


def test_load_insights_merges_meta_and_facets(tmp_path):
    ud = tmp_path / "usage-data"
    (ud / "session-meta").mkdir(parents=True)
    (ud / "facets").mkdir(parents=True)
    (ud / "session-meta" / "x.json").write_text(json.dumps(
        {"session_id": SID, "duration_minutes": 42, "lines_added": 100,
         "lines_removed": 7, "files_modified": 3, "git_commits": 2}))
    (ud / "facets" / "x.json").write_text(json.dumps(
        {"session_id": SID, "brief_summary": "Fixed it", "outcome": "success"}))
    (ud / "facets" / "other.json").write_text(json.dumps(
        {"session_id": "other", "outcome": "abandoned"}))

    ins = load_insights([SID], usage_data_dir=ud)
    assert set(ins) == {SID}
    assert ins[SID]["lines_added"] == 100
    assert ins[SID]["git_commits"] == 2
    assert ins[SID]["outcome"] == "success"


def test_load_insights_missing_dir(tmp_path):
    assert load_insights([SID], usage_data_dir=tmp_path / "nope") == {}


def test_naming_helpers():
    assert friendly_model("claude-sonnet-4-6-20250514") == "Sonnet 4.6"
    assert friendly_model(None) == "unknown"
    assert friendly_tool("mcp__linear__create_issue") == "MCP linear → create_issue"
    assert friendly_tool("Agent:Explore") == "Agent: Explore"
    assert tool_use_name({"name": "Task", "input": {"subagent_type": "Plan"}}) \
        == "Task:Plan"
    assert project_display("/tmp/whatever") == "sandbox session"
    assert project_display("/Users/x/Programming/CIA") == "Programming/CIA"
    assert clean_prompt_text(
        "<system-reminder>x</system-reminder> real  text") == "real text"
