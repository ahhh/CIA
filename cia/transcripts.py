"""
Report-time parsing of Claude Code's on-disk session data.

Claude Code writes a full transcript of every session to
``~/.claude/projects/<encoded-project>/<session_id>.jsonl``, with subagent
sub-transcripts under ``.../<session_id>/subagents/**.jsonl`` (each with a
sibling ``.meta.json`` naming the agent type).  Optionally, /insights
leaves per-session enrichment under ``~/.claude/usage-data/``.

These files are a third, retroactive measurement source: exact per-message
token usage and model straight from the API responses Claude Code saved,
tool-call names (including MCP / skill / subagent attribution), the
session's own AI-generated title, and delivery stats (lines of code,
commits, outcome).  ``cia report`` parses them on demand for the sessions
present in the event store — nothing here runs in the daemon, and nothing
is cached.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from cia.claude_paths import CLAUDE_HOME


# ------------------------------------------------------------------ #
# Naming helpers                                                       #
# ------------------------------------------------------------------ #

def friendly_model(model_id: Optional[str]) -> str:
    """Human-readable model name: strip the ``claude-`` prefix and any
    trailing date stamp (``claude-sonnet-4-6-20250514`` → ``Sonnet 4.6``)."""
    if not model_id:
        return "unknown"
    m = re.sub(r"^claude-", "", model_id)
    m = re.sub(r"-\d{8}$", "", m)
    parts = m.split("-")
    return f"{parts[0].capitalize()} {'.'.join(parts[1:])}".strip()


# tool_use blocks whose bare name ("Agent", "Skill") is too generic to
# aggregate on — fold the specific thing they invoked into the name.
_SPECIAL_TOOL_INPUT_KEY = {"Skill": "skill", "Agent": "subagent_type",
                           "Task": "subagent_type"}


def tool_use_name(block: dict) -> Optional[str]:
    name = block.get("name")
    if not name:
        return None
    key = _SPECIAL_TOOL_INPUT_KEY.get(name)
    if key:
        sub = (block.get("input") or {}).get(key)
        if sub:
            return f"{name}:{sub}"
    return name


def friendly_tool(name: str) -> str:
    """Render a folded tool name for humans (``mcp__srv__tool`` →
    ``MCP srv → tool``, ``Agent:Explore`` → ``Agent: Explore``)."""
    if name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 3:
            return f"MCP {parts[1]} → {parts[2]}"
        return name
    if ":" in name:
        base, _, sub = name.partition(":")
        return f"{base}: {sub}"
    return name


def is_sandbox_path(path: Optional[str]) -> bool:
    if not path:
        return False
    p = path.rstrip("/")
    return "/T/tmp." in p or p.startswith("/tmp/") or "/var/folders/" in p


def project_display(path: Optional[str]) -> str:
    """Short display form of a project path (last two segments); temp-dir
    sessions collapse to 'sandbox session'."""
    if not path:
        return "unknown"
    if is_sandbox_path(path):
        return "sandbox session"
    parts = [seg for seg in path.rstrip("/").split("/") if seg]
    return "/".join(parts[-2:]) if len(parts) >= 2 else path


# ------------------------------------------------------------------ #
# Prompt extraction                                                    #
# ------------------------------------------------------------------ #

# Claude Code wraps slash-command output and injected context in pseudo-XML
# tags inside "user" records; strip them so prompts read as typed.
_WRAPPER_TAG_RE = re.compile(
    r"<(command-message|command-name|command-args|command-contents|command|"
    r"local-command-stdout|local-command-stderr|local-command-caveat|"
    r"system-reminder|user-prompt-submit-hook)>.*?</\1>",
    re.DOTALL,
)


def clean_prompt_text(text: str) -> str:
    return " ".join(_WRAPPER_TAG_RE.sub(" ", text).split())


def is_real_user_text(content) -> bool:
    """A user record is a genuine human message when it has text content and
    is not just a tool_result envelope."""
    if isinstance(content, str):
        return content.strip() != ""
    if isinstance(content, list):
        has_tool_result = any(isinstance(b, dict) and b.get("type") == "tool_result"
                              for b in content)
        has_text = any(isinstance(b, dict) and b.get("type") == "text"
                       and (b.get("text") or "").strip() for b in content)
        return has_text and not has_tool_result
    return False


def extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content
                         if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _epoch(ts: Optional[str]) -> Optional[float]:
    """ISO-8601 transcript timestamp → epoch seconds; None on garbage."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ------------------------------------------------------------------ #
# Transcript parsing                                                   #
# ------------------------------------------------------------------ #

def find_session_files(session_ids, projects_dir: Optional[Path] = None) -> dict:
    """Locate the main transcript and subagent sub-transcripts for each
    session id: {sid: {"main": Path|None, "subagents": [Path, ...]}}."""
    projects_dir = projects_dir or (CLAUDE_HOME / "projects")
    out: dict[str, dict] = {}
    if not projects_dir.is_dir():
        return out
    for sid in session_ids:
        if not sid:
            continue
        main = next(iter(projects_dir.glob(f"*/{sid}.jsonl")), None)
        subagents = sorted(
            p for p in projects_dir.glob(f"*/{sid}/**/*.jsonl") if p.is_file())
        if main or subagents:
            out[sid] = {"main": main, "subagents": subagents}
    return out


_EMPTY_USAGE = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}


def _empty_record() -> dict:
    return {"usage": dict(_EMPTY_USAGE), "by_model": {}, "tool_counts": {},
            "prompts": [], "title": None, "project_path": None,
            "first_ts": None, "last_ts": None, "user_messages": 0,
            "assistant_messages": 0}


def parse_transcript(path: Path, top_level: bool = True) -> dict:
    """One pass over a transcript file.  Unparseable lines are skipped —
    the file may be mid-write.  ``top_level=False`` (subagent transcripts)
    suppresses prompt extraction: their "user" records are the dispatch
    briefs Claude wrote for the subagent, not human input."""
    record = _empty_record()
    usage = record["usage"]
    by_model: dict[str, dict] = record["by_model"]
    tool_counts: dict[str, int] = record["tool_counts"]
    prompts: list[dict] = record["prompts"]
    title: Optional[str] = None
    project_path: Optional[str] = None
    first_ts: Optional[float] = None
    last_ts: Optional[float] = None
    user_messages = assistant_messages = 0

    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return record

    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(d, dict):
                continue
            dtype = d.get("type")
            if dtype == "ai-title":
                title = d.get("aiTitle") or title
                continue
            if dtype not in ("user", "assistant"):
                continue

            msg = d.get("message") or {}
            ts = _epoch(d.get("timestamp"))
            if ts is not None:
                first_ts = ts if first_ts is None else min(first_ts, ts)
                last_ts = ts if last_ts is None else max(last_ts, ts)
            if d.get("cwd") and not project_path:
                project_path = d["cwd"]

            if dtype == "user":
                if top_level and not d.get("isMeta") and not d.get("isSidechain"):
                    content = msg.get("content")
                    if is_real_user_text(content):
                        text = clean_prompt_text(extract_text(content))
                        if text:
                            user_messages += 1
                            prompts.append({"ts": ts, "text": text[:600]})
                continue

            # assistant
            assistant_messages += 1
            u = msg.get("usage") or {}
            model = msg.get("model")
            m = by_model.setdefault(model or "?", dict(_EMPTY_USAGE))
            for dst, src in (("input", "input_tokens"),
                             ("output", "output_tokens"),
                             ("cache_read", "cache_read_input_tokens"),
                             ("cache_creation", "cache_creation_input_tokens")):
                v = u.get(src) or 0
                usage[dst] += v
                m[dst] += v
            for block in msg.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    name = tool_use_name(block)
                    if name:
                        tool_counts[name] = tool_counts.get(name, 0) + 1

    record.update(title=title, project_path=project_path,
                  first_ts=first_ts, last_ts=last_ts,
                  user_messages=user_messages,
                  assistant_messages=assistant_messages)
    return record


def _subagent_type(path: Path) -> str:
    """Agent type for a subagent transcript: the sibling ``.meta.json``
    carries it verbatim; fall back to the filename stem."""
    meta_path = path.parent / (path.stem + ".meta.json")
    try:
        meta = json.loads(meta_path.read_text())
        if isinstance(meta, dict) and meta.get("agentType"):
            return str(meta["agentType"])
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return path.stem


def session_transcripts(session_ids,
                        projects_dir: Optional[Path] = None) -> dict:
    """Parse main + subagent transcripts for each session id.

    Subagent usage/tool counts are kept per subagent (with the agent type
    from its ``.meta.json``) *and* rolled into the session totals, matching
    how the session actually spent tokens.
    """
    out: dict[str, dict] = {}
    for sid, files in find_session_files(session_ids, projects_dir).items():
        record = (parse_transcript(files["main"], top_level=True)
                  if files["main"] else _empty_record())
        record["subagents"] = []
        for sub_path in files["subagents"]:
            sub = parse_transcript(sub_path, top_level=False)
            for k in _EMPTY_USAGE:
                record["usage"][k] += sub["usage"][k]
            for model, mu in sub["by_model"].items():
                m = record["by_model"].setdefault(model, dict(_EMPTY_USAGE))
                for k in _EMPTY_USAGE:
                    m[k] += mu[k]
            record["subagents"].append({
                "key": sub_path.name,
                "agent_type": _subagent_type(sub_path),
                "usage": sub["usage"],
                "tool_calls": sum(sub["tool_counts"].values()),
                "tool_counts": sub["tool_counts"],
            })
        out[sid] = record
    return out


# ------------------------------------------------------------------ #
# /insights enrichment (~/.claude/usage-data)                          #
# ------------------------------------------------------------------ #

def load_insights(session_ids,
                  usage_data_dir: Optional[Path] = None) -> dict:
    """Per-session /insights enrichment, when the user has generated it:
    session-meta (duration, lines added/removed, files modified, commits)
    and facets (brief_summary, outcome).  Missing dir → {}."""
    usage_data_dir = usage_data_dir or (CLAUDE_HOME / "usage-data")
    wanted = {sid for sid in session_ids if sid}
    out: dict[str, dict] = {}
    if not usage_data_dir.is_dir() or not wanted:
        return out

    for fp in (usage_data_dir / "session-meta").glob("*.json"):
        try:
            m = json.loads(fp.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        sid = m.get("session_id")
        if sid in wanted:
            out.setdefault(sid, {}).update({
                "duration_minutes": m.get("duration_minutes"),
                "lines_added": m.get("lines_added"),
                "lines_removed": m.get("lines_removed"),
                "files_modified": m.get("files_modified"),
                "git_commits": m.get("git_commits"),
            })

    for fp in (usage_data_dir / "facets").glob("*.json"):
        try:
            fa = json.loads(fp.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        sid = fa.get("session_id")
        if sid in wanted:
            out.setdefault(sid, {}).update({
                "brief_summary": fa.get("brief_summary"),
                "outcome": fa.get("outcome"),
            })
    return out
