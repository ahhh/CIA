"""Dynamic watchers: when a hook reports Claude writing a file outside every
watched root, the daemon starts watching that file's directory."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cia.daemon import _MAX_DYNAMIC_WATCHERS, Daemon
from cia.schema import Event, Phase


@pytest.fixture
async def daemon(tmp_path):
    """A Daemon that is never run() — _maybe_watch_created_path only needs a
    live event loop (create_task) and the watch-root bookkeeping."""
    d = Daemon(
        db_path=tmp_path / "t.db",
        jsonl_path=None,
        proxy_port=0,
        otlp_port=0,
        watch_dirs=[(tmp_path / "project", "project")],
    )
    d._watch_roots = {(tmp_path / "project").resolve()}
    yield d
    for t in d._tasks:
        t.cancel()
    await asyncio.gather(*d._tasks, return_exceptions=True)


def _write_end(path: Path, tool: str = "Write") -> Event:
    return Event(phase=Phase.TOOL_CALL_END, tool=tool, meta={"path": str(path)})


async def test_watches_dir_of_created_file(daemon, tmp_path):
    outside = tmp_path / "scratch"
    outside.mkdir()
    daemon._maybe_watch_created_path(_write_end(outside / "notes.md"))

    assert outside.resolve() in daemon._watch_roots
    assert len(daemon._tasks) == 1


async def test_same_dir_only_watched_once(daemon, tmp_path):
    outside = tmp_path / "scratch"
    outside.mkdir()
    daemon._maybe_watch_created_path(_write_end(outside / "a.md"))
    daemon._maybe_watch_created_path(_write_end(outside / "b.md", tool="Edit"))
    assert len(daemon._tasks) == 1


async def test_paths_under_existing_roots_are_skipped(daemon, tmp_path):
    inside = tmp_path / "project" / "src"
    inside.mkdir(parents=True)
    daemon._maybe_watch_created_path(_write_end(inside / "main.py"))
    assert daemon._tasks == []


async def test_non_write_tools_are_skipped(daemon, tmp_path):
    outside = tmp_path / "scratch"
    outside.mkdir()
    daemon._maybe_watch_created_path(_write_end(outside / "x", tool="Read"))
    daemon._maybe_watch_created_path(
        Event(phase=Phase.TOOL_CALL_START, tool="Write",
              meta={"path": str(outside / "x")}))
    assert daemon._tasks == []


async def test_ignored_and_missing_dirs_are_skipped(daemon, tmp_path):
    daemon._maybe_watch_created_path(_write_end(tmp_path / "gone" / "x.md"))
    git = tmp_path / ".git"
    git.mkdir()
    daemon._maybe_watch_created_path(_write_end(git / "hook.sh"))
    daemon._maybe_watch_created_path(_write_end(Path.home() / "stray.txt"))
    assert daemon._tasks == []


async def test_dynamic_watcher_cap(daemon, tmp_path):
    for i in range(_MAX_DYNAMIC_WATCHERS + 5):
        d = tmp_path / f"dir{i}"
        d.mkdir()
        daemon._maybe_watch_created_path(_write_end(d / "f.txt"))
    assert len(daemon._tasks) == _MAX_DYNAMIC_WATCHERS
