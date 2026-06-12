"""
Wraps fswatch (must be installed: brew install fswatch) to emit
FILE_CHANGE events for a watched directory.

For paths that are part of Claude's own data (transcripts, memory, todos —
anything ``classify_path`` recognises) a ``FileDelta`` tracker keeps
per-file state so each event can carry *what changed*: appended transcript
records get parsed into compact previews, and small text files (memory,
settings) get a capped unified diff against their previous content.
"""
from __future__ import annotations

import asyncio
import difflib
import json
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


class FileDelta:
    """Tracks file sizes (and small-file contents) between change events so
    we can report what was appended / how the content changed."""

    def __init__(self) -> None:
        self._sizes: dict[str, int] = {}
        self._texts: dict[str, str] = {}

    # ------------------------------------------------------------------ #
    # Priming                                                              #
    # ------------------------------------------------------------------ #

    def prime(self, root: Path) -> None:
        """Snapshot the Claude-data files already under ``root`` so the
        first change after startup yields a real delta, not a blind
        'snapshot'."""
        try:
            paths = [p for p in root.rglob("*") if p.is_file()]
        except OSError:
            return
        for p in paths:
            sp = str(p)
            if classify_path(sp) is None:
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            self._sizes[sp] = size
            if not sp.endswith(".jsonl") and size <= _MAX_SNAPSHOT:
                text = _read_text(p)
                if text is not None:
                    self._texts[sp] = text

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
            return {"kind": "created", "snippet": text[:_MAX_SNIPPET]}
        if prev_text == text:
            return None
        return {"kind": "diff", "bytes_delta": size - (prev_size or 0),
                "snippet": _unified_snippet(prev_text, text)}


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
    ) -> None:
        self._dir = watch_dir
        self._emit = emit
        self._latency = latency
        self._proc: asyncio.subprocess.Process | None = None
        self._delta = FileDelta()

    async def start(self) -> None:
        self._delta.prime(self._dir)
        cmd = [
            "fswatch",
            "--recursive",
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
                if path:
                    meta = {"path": path, "watch_dir": str(self._dir)}
                    category = classify_path(path)
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
