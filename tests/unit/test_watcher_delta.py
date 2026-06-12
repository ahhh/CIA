"""Unit tests for FileDelta: content snippets on Claude-data file changes."""
from __future__ import annotations

import json

import pytest

from cia.watcher import FileDelta


@pytest.fixture
def claude_dir(tmp_path):
    """A fake ~/.claude/projects/<proj>/ layout that classify_path recognises
    (it matches on path substrings, so any root works)."""
    d = tmp_path / ".claude" / "projects" / "-Users-x-proj"
    (d / "memory").mkdir(parents=True)
    return d


def _record(role: str, text: str) -> str:
    return json.dumps({"type": role, "message": {"role": role,
                       "content": [{"type": "text", "text": text}]}})


def test_jsonl_append_yields_record_previews(claude_dir):
    transcript = claude_dir / "sess.jsonl"
    transcript.write_text(_record("user", "hello") + "\n")

    delta = FileDelta()
    delta.prime(claude_dir.parents[2])   # the .claude root

    with transcript.open("a") as fh:
        fh.write(_record("assistant", "hi there, " + "x" * 300) + "\n")
    change = delta.observe(str(transcript))

    assert change["kind"] == "append"
    assert change["bytes_delta"] > 0
    assert len(change["records"]) == 1
    rec = change["records"][0]
    assert rec["type"] == "assistant"
    assert rec["preview"].startswith("hi there")
    assert len(rec["preview"]) <= 150


def test_jsonl_new_file_is_created_kind(claude_dir):
    delta = FileDelta()
    delta.prime(claude_dir.parents[2])

    transcript = claude_dir / "new.jsonl"
    transcript.write_text(_record("user", "first prompt") + "\n")
    change = delta.observe(str(transcript))

    assert change["kind"] == "created"
    assert change["records"][0]["preview"] == "first prompt"


def test_jsonl_tool_use_preview(claude_dir):
    transcript = claude_dir / "sess.jsonl"
    transcript.write_text("")
    delta = FileDelta()
    delta.observe(str(transcript))   # learn size 0

    line = json.dumps({"type": "assistant", "message": {
        "role": "assistant",
        "content": [{"type": "tool_use", "name": "Bash", "input": {}}]}})
    transcript.write_text(line + "\n")
    change = delta.observe(str(transcript))
    assert change["records"][0]["preview"] == "[tool_use: Bash]"


def test_text_file_diff(claude_dir):
    mem = claude_dir / "memory" / "MEMORY.md"
    mem.write_text("# Memory\n- old fact\n")

    delta = FileDelta()
    delta.prime(claude_dir.parents[2])

    mem.write_text("# Memory\n- old fact\n- new fact\n")
    change = delta.observe(str(mem))

    assert change["kind"] == "diff"
    assert "+- new fact" in change["snippet"]


def test_text_file_first_sight_snapshot(claude_dir):
    delta = FileDelta()
    mem = claude_dir / "memory" / "note.md"
    mem.write_text("fresh content")
    change = delta.observe(str(mem))
    assert change["kind"] == "created"
    assert change["snippet"] == "fresh content"


def test_unchanged_file_yields_none(claude_dir):
    mem = claude_dir / "memory" / "note.md"
    mem.write_text("same")
    delta = FileDelta()
    delta.observe(str(mem))
    assert delta.observe(str(mem)) is None


def test_removed_file(claude_dir):
    mem = claude_dir / "memory" / "note.md"
    mem.write_text("bye")
    delta = FileDelta()
    delta.observe(str(mem))
    mem.unlink()
    assert delta.observe(str(mem)) == {"kind": "removed"}
    # a path we never saw simply yields None
    assert delta.observe(str(claude_dir / "memory" / "ghost.md")) is None


def test_record_previews_are_capped(claude_dir):
    transcript = claude_dir / "sess.jsonl"
    transcript.write_text("")
    delta = FileDelta()
    delta.observe(str(transcript))

    lines = "".join(_record("user", f"msg {i}") + "\n" for i in range(9))
    transcript.write_text(lines)
    change = delta.observe(str(transcript))

    assert len(change["records"]) == 6           # 5 previews + the "more" marker
    assert change["records"][-1]["more"] == 4
