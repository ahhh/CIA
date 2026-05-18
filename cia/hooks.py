"""
Install / uninstall CIA hook scripts into .claude/settings.json.

Hook scripts are tiny shell scripts written to ~/.cia/hooks/.
They read Claude Code's JSON payload from stdin and POST it to the
CIA hook receiver (default: http://127.0.0.1:7171/hook/<type>).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

CIA_DIR = Path.home() / ".cia"
_HOOKS_DIR = CIA_DIR / "hooks"

_SCRIPT_TEMPLATE = """\
#!/bin/bash
CIA_HOOK_URL="${{CIA_HOOK_URL:-http://127.0.0.1:7171}}"
PAYLOAD=$(cat)
curl -s -f --connect-timeout 0.5 --max-time 2 \\
  -X POST "$CIA_HOOK_URL/hook/{endpoint}" \\
  -H "Content-Type: application/json" \\
  -d "$PAYLOAD" >/dev/null 2>&1 || true
"""

# Maps Claude Code hook event name → (script filename, URL endpoint)
_HOOK_MAP: dict[str, tuple[str, str]] = {
    "PreToolUse": ("cia_pre_tool.sh",  "pre"),
    "PostToolUse": ("cia_post_tool.sh", "post"),
    "Stop":       ("cia_stop.sh",      "stop"),
}


def _write_scripts() -> dict[str, Path]:
    """Write hook shell scripts to ~/.cia/hooks/ and return paths."""
    _HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for event_name, (filename, endpoint) in _HOOK_MAP.items():
        p = _HOOKS_DIR / filename
        p.write_text(_SCRIPT_TEMPLATE.format(endpoint=endpoint))
        p.chmod(0o755)
        paths[event_name] = p
    return paths


def _settings_path(scope: Literal["global", "project"]) -> Path:
    if scope == "global":
        return Path.home() / ".claude" / "settings.json"
    return Path.cwd() / ".claude" / "settings.json"


def install_hooks(scope: Literal["global", "project"] = "project") -> Path:
    script_paths = _write_scripts()
    path = _settings_path(scope)
    path.parent.mkdir(parents=True, exist_ok=True)

    settings: dict = {}
    if path.exists():
        try:
            settings = json.loads(path.read_text())
        except json.JSONDecodeError:
            pass

    hooks: dict = settings.setdefault("hooks", {})

    for event_name, script_path in script_paths.items():
        bucket: list = hooks.setdefault(event_name, [])
        cia_entry = {
            "matcher": ".*",
            "hooks": [{"type": "command", "command": str(script_path)}],
        }
        # Avoid duplicates
        if not any(str(script_path) in json.dumps(e) for e in bucket):
            bucket.append(cia_entry)

    path.write_text(json.dumps(settings, indent=2))
    return path


def uninstall_hooks(scope: Literal["global", "project"] = "project") -> Path:
    path = _settings_path(scope)
    if not path.exists():
        return path

    try:
        settings = json.loads(path.read_text())
    except json.JSONDecodeError:
        return path

    hooks: dict = settings.get("hooks", {})
    cia_script_dir = str(_HOOKS_DIR)

    for event_name in list(hooks.keys()):
        hooks[event_name] = [
            e for e in hooks[event_name]
            if cia_script_dir not in json.dumps(e)
        ]
        if not hooks[event_name]:
            del hooks[event_name]

    if not hooks:
        settings.pop("hooks", None)

    path.write_text(json.dumps(settings, indent=2))
    return path
