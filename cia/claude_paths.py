"""
Resolve and classify Claude Code's on-disk data directories.

Claude Code keeps its per-project session transcripts and memory under
``~/.claude/projects/<encoded-project-path>/`` (the transcript ``.jsonl``
files plus a ``memory/`` subdirectory), and some global state under
``~/.claude/todos`` / ``~/.claude/tasks``.  CIA watches these so that the
writes Claude makes to its *own* memory and session state become observable
events — not just edits to the user's source tree.
"""
from __future__ import annotations

import re
from pathlib import Path

CLAUDE_HOME = Path.home() / ".claude"


def encode_project_dir(project_dir: Path) -> str:
    """Mirror Claude Code's project-dir encoding: absolute path with every
    non-alphanumeric character replaced by ``-`` (e.g. ``/Users/db/Programming/CIA``
    → ``-Users-db-Programming-CIA``)."""
    abspath = str(project_dir.resolve())
    return re.sub(r"[^A-Za-z0-9]", "-", abspath)


def project_data_dir(project_dir: Path) -> Path:
    """The ``~/.claude/projects/<hash>`` dir holding transcripts + memory/."""
    return CLAUDE_HOME / "projects" / encode_project_dir(project_dir)


def claude_watch_dirs(project_dir: Path) -> list[Path]:
    """Claude data dirs worth watching for the given project, filtered to
    those that currently exist (fswatch errors on missing paths)."""
    candidates = [
        project_data_dir(project_dir),   # transcripts + memory/ subdir
        CLAUDE_HOME / "todos",
        CLAUDE_HOME / "tasks",
    ]
    return [d for d in candidates if d.exists()]


def classify_path(path: str) -> str | None:
    """Bucket a changed path into a Claude-data category, or ``None`` if the
    path is not part of Claude's own state. Used to tag ``file_change`` events
    so memory/transcript writes can be distinguished from source-tree edits."""
    p = path.replace("\\", "/")
    if "/memory/" in p or p.endswith("/MEMORY.md"):
        return "memory"
    if "/.claude/projects/" in p and p.endswith(".jsonl"):
        return "transcript"
    if "/.claude/todos/" in p or "/.claude/tasks/" in p:
        return "todo"
    if p.endswith("/settings.json") or p.endswith("/settings.local.json"):
        return "settings"
    if "/.claude/projects/" in p:
        return "session"
    return None
