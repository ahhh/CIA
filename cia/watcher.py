"""
Wraps fswatch (must be installed: brew install fswatch) to emit
FILE_CHANGE events for a watched directory.

For paths that get a category — Claude's own data (anything ``classify_path``
recognises) or anything under a watcher started with a ``default_category``
(project source trees, dirs Claude created files in) — a ``FileDelta``
tracker keeps per-file state so each event can carry *what changed*:
appended JSONL records get parsed into compact previews, and small text
files get a capped unified diff against their previous content.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import os
from pathlib import Path
from typing import Callable, Optional

from cia.claude_paths import classify_path
from cia.schema import Event, Phase

# FileDelta limits: keep events lean and reads bounded.
_MAX_SNAPSHOT = 256 * 1024   # diff-track text files up to this size
_MAX_READ = 16 * 1024        # max appended bytes read per event
_MAX_SNIPPET = 600           # chars per snippet / diff
_MAX_DIFF_LINES = 12
_MAX_RECORDS = 5             # parsed JSONL record previews per event
_PREVIEW_CHARS = 150
_MAX_PRIME_TEXT = 8 * 1024 * 1024   # total text snapshotted at prime time

# Churn that is never a signal when watching a source tree. ``.cia`` is CIA's
# own data dir — watching it would feed back (events.jsonl writes → events).
_IGNORE_DIR_PARTS = {
    ".git", ".hg", ".svn", ".venv", "venv", ".tox", "node_modules",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".cache", ".idea", "dist", "build", ".next", ".turbo", "target",
    ".gradle", ".terraform", ".direnv", ".cia",
}
_IGNORE_FILE_SUFFIXES = (".pyc", ".pyo", ".swp", ".swx", ".tmp", ".lock~")
_IGNORE_FILE_NAMES = {".DS_Store", "4913"}  # 4913: vim's write probe


def is_ignored_path(path: str) -> bool:
    """True for build/VCS/editor churn we never want file_change events for."""
    p = Path(path)
    if p.name in _IGNORE_FILE_NAMES or p.name.endswith(_IGNORE_FILE_SUFFIXES):
        return True
    if ".tmp." in p.name:   # atomic-write staging files (foo.txt.tmp.<pid>.<hash>)
        return True
    return any(part in _IGNORE_DIR_PARTS for part in p.parts)


class FileDelta:
    """Tracks file sizes (and small-file contents) between change events so
    we can report what was appended / how the content changed."""

    def __init__(self, categorize: Callable[[str], Optional[str]] = classify_path) -> None:
        self._sizes: dict[str, int] = {}
        self._texts: dict[str, str] = {}
        self._categorize = categorize

    # ------------------------------------------------------------------ #
    # Priming                                                              #
    # ------------------------------------------------------------------ #

    def prime(self, root: Path, recursive: bool = True) -> None:
        """Snapshot the tracked files already under ``root`` so the first
        change after startup yields a real delta, not a blind 'snapshot'.

        Sizes are recorded for every tracked file; text contents only up to
        a total budget (big source trees fall back to a lazy snapshot on
        first change — see ``_observe_text``)."""
        text_budget = _MAX_PRIME_TEXT
        for p in _iter_files(root, recursive):
            sp = str(p)
            if is_ignored_path(sp) or self._categorize(sp) is None:
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            self._sizes[sp] = size
            if (text_budget > 0 and not sp.endswith(".jsonl")
                    and size <= _MAX_SNAPSHOT):
                text = _read_text(p)
                if text is not None:
                    self._texts[sp] = text
                    text_budget -= len(text)

    # ------------------------------------------------------------------ #
    # Observation                                                          #
    # ------------------------------------------------------------------ #

    def observe(self, path: str) -> Optional[dict]:
        """Return a ``change`` description for a path the watcher reported,
        or None when there is nothing useful to say."""
        p = Path(path)
        if not p.exists():
            if path in self._sizes or path in self._texts:
                self._sizes.pop(path, None)
                self._texts.pop(path, None)
                return {"kind": "removed"}
            return None
        if not p.is_file():
            return None
        try:
            size = p.stat().st_size
        except OSError:
            return None

        prev_size = self._sizes.get(path)
        self._sizes[path] = size

        if path.endswith(".jsonl"):
            return self._observe_jsonl(p, path, size, prev_size)
        return self._observe_text(p, path, size, prev_size)

    def _observe_jsonl(self, p: Path, path: str, size: int,
                       prev_size: Optional[int]) -> Optional[dict]:
        """Transcripts and other JSONL logs are append-mostly: read the
        appended byte range and parse it into record previews."""
        if prev_size is not None and size == prev_size:
            return None
        if prev_size is not None and size < prev_size:
            tail = _read_range(p, max(0, size - _MAX_READ), size)
            return {"kind": "rewrite", "bytes_delta": size - prev_size,
                    "records": _preview_records(tail)}
        start = prev_size if prev_size is not None else max(0, size - _MAX_READ)
        # prime() snapshots pre-existing files, so first sight here = new file
        kind = "append" if prev_size is not None else "created"
        clipped = size - start > _MAX_READ
        if clipped:
            start = size - _MAX_READ
        text = _read_range(p, start, size)
        change: dict = {"kind": kind, "records": _preview_records(text)}
        if prev_size is not None:
            change["bytes_delta"] = size - prev_size
        if clipped:
            change["clipped"] = True
        return change

    def _observe_text(self, p: Path, path: str, size: int,
                      prev_size: Optional[int]) -> Optional[dict]:
        """Memory / settings / todo files: diff small text against the last
        snapshot; fall back to a size delta for big or binary files."""
        if size > _MAX_SNAPSHOT:
            self._texts.pop(path, None)
            if prev_size is None or size == prev_size:
                return None
            return {"kind": "modified", "bytes_delta": size - prev_size}
        text = _read_text(p)
        if text is None:   # binary / unreadable
            return None
        prev_text = self._texts.get(path)
        self._texts[path] = text
        if prev_text is None:
            if prev_size is not None:
                # Size was primed but the text snapshot was skipped (prime
                # budget): take the snapshot now so the *next* edit diffs.
                if size == prev_size:
                    return None
                return {"kind": "modified", "bytes_delta": size - prev_size}
            return {"kind": "created", "snippet": text[:_MAX_SNIPPET]}
        if prev_text == text:
            return None
        return {"kind": "diff", "bytes_delta": size - (prev_size or 0),
                "snippet": _unified_snippet(prev_text, text)}


def _iter_files(root: Path, recursive: bool):
    """Yield files under ``root``, pruning ignored directories during the
    walk (rglob can't prune, and descending into .git/.venv is the cost)."""
    try:
        if not recursive:
            yield from (p for p in root.iterdir() if p.is_file())
            return
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIR_PARTS]
            for name in filenames:
                yield Path(dirpath) / name
    except OSError:
        return


def _read_text(p: Path) -> Optional[str]:
    try:
        raw = p.read_bytes()
        if b"\x00" in raw[:1024]:
            return None
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return None


def _read_range(p: Path, start: int, end: int) -> str:
    try:
        with open(p, "rb") as fh:
            fh.seek(start)
            return fh.read(end - start).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _unified_snippet(old: str, new: str) -> str:
    lines = list(difflib.unified_diff(
        old.splitlines(), new.splitlines(), lineterm="", n=1))[2:]  # drop ---/+++
    if len(lines) > _MAX_DIFF_LINES:
        lines = lines[:_MAX_DIFF_LINES] + [f"… (+{len(lines) - _MAX_DIFF_LINES} more lines)"]
    return "\n".join(lines)[:_MAX_SNIPPET]


def _preview_records(text: str) -> list[dict]:
    """Parse appended JSONL into compact previews: record type, role and the
    first chunk of human-readable content (or tool names)."""
    out: list[dict] = []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if lines and not lines[0].lstrip().startswith("{"):
        lines = lines[1:]   # first line may be a partial record we started mid-way
    for line in lines:
        if len(out) >= _MAX_RECORDS:
            out.append({"more": len(lines) - _MAX_RECORDS})
            break
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict):
            out.append(_preview_record(rec))
    return out


def _preview_record(rec: dict) -> dict:
    preview: dict = {}
    if rec.get("type"):
        preview["type"] = rec["type"]
    msg = rec.get("message")
    if isinstance(msg, dict):
        if msg.get("role"):
            preview["role"] = msg["role"]
        text = _content_text(msg.get("content"))
        if text:
            preview["preview"] = text[:_PREVIEW_CHARS]
    elif isinstance(rec.get("summary"), str):
        preview["preview"] = rec["summary"][:_PREVIEW_CHARS]
    elif isinstance(rec.get("content"), (str, list)):
        text = _content_text(rec["content"])
        if text:
            preview["preview"] = text[:_PREVIEW_CHARS]
    return preview


def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and block.get("text"):
            parts.append(block["text"])
        elif btype == "thinking":
            parts.append("[thinking]")
        elif btype == "tool_use":
            parts.append(f"[tool_use: {block.get('name', '?')}]")
        elif btype == "tool_result":
            parts.append("[tool_result]")
        elif btype:
            parts.append(f"[{btype}]")
    return " ".join(parts)


class FsWatcher:
    def __init__(
        self,
        watch_dir: Path,
        emit: Callable[[Event], None],
        latency: float = 0.5,
        recursive: bool = True,
        default_category: Optional[str] = None,
    ) -> None:
        self._dir = watch_dir
        self._emit = emit
        self._latency = latency
        self._recursive = recursive
        self._default_category = default_category
        self._proc: asyncio.subprocess.Process | None = None
        self._delta = FileDelta(categorize=self._categorize)

    def _categorize(self, path: str) -> Optional[str]:
        """Claude-data classification wins; otherwise the watcher's default
        (e.g. 'project' for the source tree, 'artifact' for dirs Claude
        created files in)."""
        return classify_path(path) or self._default_category

    async def start(self) -> None:
        self._delta.prime(self._dir, recursive=self._recursive)
        cmd = [
            "fswatch",
            *( ["--recursive"] if self._recursive else [] ),
            f"--latency={self._latency}",
            "--event=Created",
            "--event=Updated",
            "--event=Removed",
            "--event=Renamed",
            str(self._dir),
        ]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert self._proc.stdout is not None
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                path = line.decode("utf-8", errors="replace").strip()
                if not path or is_ignored_path(path):
                    continue
                meta = {"path": path, "watch_dir": str(self._dir)}
                category = self._categorize(path)
                if category:
                    meta["category"] = category
                    meta["filename"] = Path(path).name
                    change = self._delta.observe(path)
                    if change:
                        meta["change"] = change
                self._emit(Event(phase=Phase.FILE_CHANGE, meta=meta))
        except (FileNotFoundError, asyncio.CancelledError):
            pass
        finally:
            await self.stop()

    async def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except Exception:
                pass
