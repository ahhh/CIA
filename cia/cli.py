"""
CIA command-line interface.

  cia start         — start daemon in background (forks)
  cia stop          — stop daemon
  cia status        — show event counts
  cia export        — dump events as JSONL or CSV
  cia tail          — live event feed
  cia install-hooks — add hook scripts to .claude/settings.json
  cia uninstall-hooks
  cia trust-cert    — print instructions for trusting mitmproxy CA
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sys
import time
from pathlib import Path

import click
from rich.console import Console
from rich.markup import escape
from rich.table import Table

CIA_DIR = Path.home() / ".cia"
SOCKET_PATH = CIA_DIR / "cia.sock"
PID_FILE = CIA_DIR / "cia.pid"
LOG_FILE = CIA_DIR / "cia.log"

console = Console()


# ------------------------------------------------------------------ #
# Socket helpers                                                       #
# ------------------------------------------------------------------ #

def _clear_stale_socket() -> None:
    """Remove socket/pid files left behind by a daemon that died uncleanly."""
    SOCKET_PATH.unlink(missing_ok=True)
    PID_FILE.unlink(missing_ok=True)


def _send(cmd: dict) -> dict:
    if not SOCKET_PATH.exists():
        console.print("[red]CIA daemon not running. Run: cia start[/red]")
        sys.exit(1)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        try:
            sock.connect(str(SOCKET_PATH))
        except (ConnectionRefusedError, FileNotFoundError):
            # Socket file exists but nobody is listening — stale leftover.
            _clear_stale_socket()
            console.print("[red]CIA daemon not running (cleared stale socket). "
                          "Run: cia start[/red]")
            sys.exit(1)
        sock.sendall(json.dumps(cmd).encode() + b"\n")
        buf = b""
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            if buf.endswith(b"\n"):
                break
        return json.loads(buf.decode())
    finally:
        sock.close()


def _require_ok(result: dict) -> dict:
    if not result.get("ok"):
        console.print(f"[red]Error: {result.get('error', 'unknown')}[/red]")
        sys.exit(1)
    return result


# ------------------------------------------------------------------ #
# CLI group                                                            #
# ------------------------------------------------------------------ #

@click.group()
def main():
    """CIA — Claude Instrumentation & Analysis"""


# ------------------------------------------------------------------ #
# start                                                                #
# ------------------------------------------------------------------ #

@main.command()
@click.option("--proxy-port", default=8080, show_default=True, help="mitmproxy listen port")
@click.option("--hook-port",  default=7171, show_default=True, help="Hook receiver port")
@click.option("--otlp-port",  default=4318, show_default=True, help="OTLP telemetry receiver port (0 to disable)")
@click.option("--db",         default=str(CIA_DIR / "cia.db"), show_default=True, help="SQLite path")
@click.option("--jsonl",      default=str(CIA_DIR / "events.jsonl"), show_default=True, help="JSONL mirror path")
@click.option("--watch-dir",  "watch_dirs", multiple=True, type=click.Path(), help="Dirs to watch (repeatable)")
@click.option("--watch-project/--no-watch-project", default=True, show_default=True,
              help="Watch the current directory for file edits (file_change category 'project')")
@click.option("--watch-claude/--no-watch-claude", default=True, show_default=True,
              help="Also watch Claude Code's own memory/session/transcript data for this project")
@click.option("--foreground", is_flag=True, help="Run in foreground (no fork)")
def start(proxy_port, hook_port, otlp_port, db, jsonl, watch_dirs, watch_project,
          watch_claude, foreground):
    """Start the CIA monitoring daemon."""
    if SOCKET_PATH.exists():
        console.print("[yellow]CIA daemon appears to already be running. "
                      "Use 'cia stop' first.[/yellow]")
        sys.exit(1)

    # Explicit --watch-dir dirs are treated as source trees too.
    resolved_watch: list = [(Path(d), "project") for d in watch_dirs]
    if watch_project:
        cwd = Path.cwd()
        if cwd == Path.home():
            console.print("[yellow]  Project dir: not watching $HOME "
                          "(start from a project directory, or pass --watch-dir)[/yellow]")
        elif not any(d == cwd for d, _ in resolved_watch):
            resolved_watch.append((cwd, "project"))
            console.print(f"  Project dir: [cyan]watching {cwd}[/cyan]")
    if watch_claude:
        from cia.claude_paths import claude_watch_dirs
        claude_dirs = claude_watch_dirs(Path.cwd())
        resolved_watch.extend((d, None) for d in claude_dirs)
        if claude_dirs:
            console.print(f"  Claude data: [cyan]watching {len(claude_dirs)} "
                          f"dir(s)[/cyan] [dim]({', '.join(d.name for d in claude_dirs)})[/dim]")

    kwargs = dict(
        db_path=Path(db),
        jsonl_path=Path(jsonl),
        proxy_port=proxy_port,
        hook_port=hook_port,
        otlp_port=otlp_port,
        watch_dirs=resolved_watch,
    )

    if foreground:
        _run_daemon(kwargs)
        return

    # Fork to background
    pid = os.fork()
    if pid > 0:
        # Parent — wait briefly for socket to appear, then print instructions
        for _ in range(20):
            time.sleep(0.25)
            if SOCKET_PATH.exists():
                break
        console.print(f"[green]CIA daemon started (pid {pid})[/green]")
        console.print(f"  Proxy  : [cyan]HTTPS_PROXY=http://127.0.0.1:{proxy_port}[/cyan]")
        console.print(f"  Cert   : [cyan]NODE_EXTRA_CA_CERTS=~/.mitmproxy/mitmproxy-ca-cert.pem[/cyan]")
        console.print(f"  Hooks  : [cyan]cia install-hooks[/cyan]")
        return

    # Child process
    os.setsid()
    CIA_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    sys.stdin.close()
    log_fh = open(LOG_FILE, "ab", 0)
    os.dup2(log_fh.fileno(), sys.stdout.fileno())
    os.dup2(log_fh.fileno(), sys.stderr.fileno())

    _run_daemon(kwargs)


def _run_daemon(kwargs: dict) -> None:
    from cia.daemon import run_daemon
    asyncio.run(run_daemon(**kwargs))


# ------------------------------------------------------------------ #
# run                                                                  #
# ------------------------------------------------------------------ #

def _build_run_env(proxy_port: int = 8080, otlp_port: int = 4318,
                   cert: Path | None = None, detail: bool = False,
                   trace: bool = False) -> dict:
    """Env vars that route a child process through CIA's collectors."""
    cert = cert or Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"
    env = {
        # Route HTTPS through the mitmproxy collector
        "HTTPS_PROXY": f"http://127.0.0.1:{proxy_port}",
        "NODE_EXTRA_CA_CERTS": str(cert),
        # Claude Code native telemetry → CIA's OTLP receiver
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{otlp_port}",
        # Local export is cheap — tight intervals shrink the lag between
        # work happening and its metrics landing (turn attribution error).
        "OTEL_METRIC_EXPORT_INTERVAL": "2000",
        "OTEL_LOGS_EXPORT_INTERVAL": "2000",
        # Ask for delta counters so per-export increments arrive as-is
        # instead of being reconstructed from cumulative totals.
        "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE": "delta",
    }
    if detail:
        # Opt-in: full tool parameters/errors, real MCP server names, hook
        # matchers, refusal categories.  Off by default — can be sensitive.
        env["OTEL_LOG_TOOL_DETAILS"] = "1"
    if trace:
        # Opt-in: Claude Code's beta span tracing → /v1/traces (otel_span
        # events), its own internal timing structure.
        env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"] = "1"
        env["OTEL_TRACES_EXPORTER"] = "otlp"
        env["OTEL_TRACES_EXPORT_INTERVAL"] = "2000"
    return env


@main.command(context_settings={"ignore_unknown_options": True})
@click.option("--proxy-port", default=8080, show_default=True)
@click.option("--otlp-port",  default=4318, show_default=True)
@click.option("--detail", is_flag=True,
              help="Include tool parameters, full error messages and real "
                   "MCP/hook names in native telemetry (OTEL_LOG_TOOL_DETAILS)")
@click.option("--trace", is_flag=True,
              help="Enable Claude Code's beta span tracing (otel_span events)")
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
def run(proxy_port, otlp_port, detail, trace, command):
    """Launch a command (default: claude) fully wired into CIA.

    Sets HTTPS_PROXY + the mitmproxy CA cert so API traffic is captured,
    and enables Claude Code's native OpenTelemetry export pointed at
    CIA's OTLP receiver.  Example:

        cia run claude
        cia run -- claude --continue
        cia run --detail --trace claude
    """
    argv = list(command) or ["claude"]
    cert = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"

    if not SOCKET_PATH.exists():
        console.print("[yellow]Warning: CIA daemon not running — "
                      "events will not be captured. Run: cia start[/yellow]")
    if not cert.exists():
        console.print(f"[yellow]Warning: {cert} not found — run 'cia start' "
                      "once to generate it, and 'cia trust-cert'.[/yellow]")

    env = dict(os.environ)
    env.update(_build_run_env(proxy_port, otlp_port, cert, detail, trace))
    flags = "".join(f", {f}" for f, on in
                    (("detail", detail), ("trace", trace)) if on)
    console.print(f"[green]cia run:[/green] [cyan]{' '.join(argv)}[/cyan] "
                  f"[dim](proxy :{proxy_port}, otlp :{otlp_port}{flags})[/dim]")
    try:
        os.execvpe(argv[0], argv, env)
    except FileNotFoundError:
        console.print(f"[red]Command not found: {argv[0]}[/red]")
        sys.exit(127)


# ------------------------------------------------------------------ #
# stop                                                                 #
# ------------------------------------------------------------------ #

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by someone else.
        return True
    return True


def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _stop_via_pid() -> bool:
    """Fall back to signalling the daemon by PID. Returns True if a live
    process was found and terminated."""
    pid = _read_pid()
    if pid is None or not _pid_alive(pid):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    # Wait for graceful exit, then escalate to SIGKILL.
    for _ in range(30):
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True


@main.command()
def stop():
    """Stop the CIA daemon."""
    asked_socket = False
    # Try the clean path: ask the daemon over its socket.
    if SOCKET_PATH.exists():
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(str(SOCKET_PATH))
            sock.sendall(json.dumps({"cmd": "stop"}).encode() + b"\n")
            asked_socket = True
        except (ConnectionRefusedError, FileNotFoundError):
            pass  # Stale socket — fall through to PID-based stop.
        finally:
            sock.close()

    if asked_socket:
        # Wait for the daemon to tear down its own socket.
        for _ in range(20):
            if not SOCKET_PATH.exists():
                break
            time.sleep(0.1)

    # If the socket is still around (or the daemon never answered), make sure
    # the process is actually gone.
    killed = False
    if not asked_socket or SOCKET_PATH.exists():
        killed = _stop_via_pid()

    _clear_stale_socket()

    if asked_socket or killed:
        console.print("[green]CIA daemon stopped.[/green]")
    else:
        console.print("[yellow]CIA daemon was not running "
                      "(cleaned up stale files).[/yellow]")


# ------------------------------------------------------------------ #
# status                                                               #
# ------------------------------------------------------------------ #

@main.command()
def status():
    """Show daemon status and event counts."""
    result = _require_ok(_send({"cmd": "status"}))
    t = Table(show_header=True, header_style="bold cyan")
    t.add_column("Status")
    t.add_column("Events", justify="right")
    t.add_column("Sessions", justify="right")
    sessions = result.get("sessions", [])
    t.add_row(
        "[green]running[/green]" if result.get("running") else "[red]stopped[/red]",
        str(result.get("events", 0)),
        str(len(sessions)),
    )
    console.print(t)
    if sessions:
        console.print("Session IDs:")
        for s in sessions:
            console.print(f"  {s}")


# ------------------------------------------------------------------ #
# export                                                               #
# ------------------------------------------------------------------ #

@main.command()
@click.option("--format", "fmt", type=click.Choice(["jsonl", "csv"]), default="jsonl", show_default=True)
@click.option("--session", default=None, help="Filter by session_id")
@click.option("--since",   default=None, type=float, help="Unix timestamp lower bound")
@click.option("--until",   default=None, type=float, help="Unix timestamp upper bound")
@click.option("-o", "--output", default=None, type=click.Path(), help="Output file (default: stdout)")
def export(fmt, session, since, until, output):
    """Export recorded events as JSONL or CSV."""
    cmd: dict = {"cmd": "export", "format": fmt}
    if session:
        cmd["session_id"] = session
    if since is not None:
        cmd["since"] = since
    if until is not None:
        cmd["until"] = until

    result = _require_ok(_send(cmd))
    data: str = result.get("data", "")

    if output:
        Path(output).write_text(data)
        lines = data.strip().splitlines()
        console.print(f"Exported {len(lines)} records to {output}")
    else:
        print(data, end="")


# ------------------------------------------------------------------ #
# tail                                                                 #
# ------------------------------------------------------------------ #

@main.command()
@click.option("--interval", default=1.0, show_default=True, help="Poll interval (seconds)")
def tail(interval):
    """Stream live events to the terminal (polls the daemon).

    Pages by store insert order (seq), not timestamp: sources like the OTLP
    receiver and the proxy commit events late with earlier timestamps, and a
    timestamp cursor would skip those forever.
    """
    last_ts: float = time.time() - 1.0   # first poll / legacy-daemon fallback
    last_seq: int | None = None
    console.print("[cyan]Tailing CIA events (Ctrl-C to stop)...[/cyan]")
    try:
        while True:
            try:
                cmd = {"cmd": "export", "format": "jsonl"}
                if last_seq is not None:
                    cmd["since_seq"] = last_seq
                else:
                    cmd["since"] = last_ts
                result = _send(cmd)
                if result.get("ok"):
                    data = result.get("data", "").strip()
                    if data:
                        for line in data.splitlines():
                            try:
                                evt = json.loads(line)
                                _print_event(evt)
                                seq = evt.get("seq")
                                if seq is not None:
                                    last_seq = max(last_seq or 0, seq)
                                ts = evt.get("ts", last_ts)
                                if ts > last_ts:
                                    last_ts = ts + 0.0001
                            except Exception:
                                pass
                    if last_seq is None and result.get("max_seq") is not None:
                        # Nothing (with a seq) matched the initial ts window;
                        # seed the cursor from the store's current tip.
                        last_seq = result["max_seq"]
            except Exception:
                console.print("[yellow]Daemon not reachable, retrying...[/yellow]")
            time.sleep(interval)
    except KeyboardInterrupt:
        pass


def _print_event(evt: dict) -> None:
    phase = evt.get("phase", "?")
    ts = evt.get("ts", 0)
    ts_str = time.strftime("%H:%M:%S", time.localtime(ts))
    ms = f"{(ts % 1)*1000:03.0f}"
    dur = f"  [{evt['duration_ms']:.0f}ms]" if evt.get("duration_ms") is not None else ""
    tool = f"  tool={evt['tool']}" if evt.get("tool") else ""
    model = f"  model={evt['model']}" if evt.get("model") else ""
    colour = "red" if evt.get("error") else _phase_colour(phase)
    extra = _event_extra(evt)
    console.print(
        f"[dim]{ts_str}.{ms}[/dim]  [{colour}]{phase:<28}[/{colour}]{dur}{tool}{model}{extra}"
    )


def _event_extra(evt: dict) -> str:
    """Render the rich token / latency fields when present."""
    parts: list[str] = []
    ti, to = evt.get("tokens_input"), evt.get("tokens_output")
    if ti:
        parts.append(f"in={ti}")
    if to:
        parts.append(f"out={to}")

    meta = evt.get("meta") or {}
    phase = evt.get("phase", "")
    if phase == "user_prompt" and meta.get("prompt"):
        prompt = meta["prompt"].replace("\n", " ")
        parts.append(f"prompt={prompt[:100]!r}")
    if phase == "session_start" and meta.get("source"):
        parts.append(f"source={meta['source']}")
    if phase == "session_end" and meta.get("reason"):
        parts.append(f"reason={meta['reason']}")
    if phase == "context_compact" and meta.get("trigger"):
        parts.append(f"trigger={meta['trigger']}")
    if phase == "notification" and meta.get("message"):
        parts.append(f"msg={meta['message'][:80]!r}")
    if phase in ("tool_call_start", "tool_call_end", "tool_call_error"):
        if meta.get("path"):
            parts.append(f"path={meta['path']}")
        elif meta.get("command"):
            parts.append(f"$ {' '.join(meta['command'].split())[:60]}")
        elif meta.get("pattern"):
            parts.append(f"/{meta['pattern']}/")
        elif meta.get("target"):
            parts.append(str(meta["target"])[:60])
    if phase == "network_request":
        parts.append(f"{meta.get('method', '?')} "
                     f"{meta.get('host', '?')}{_strip_query(meta.get('path', ''))}")
        if meta.get("status") is not None:
            parts.append(str(meta["status"]))
        if meta.get("category"):
            parts.append(f"[{meta['category']}]")
        if meta.get("purpose"):
            parts.append(f"— {meta['purpose']}")
    if phase == "file_change":
        if meta.get("category"):
            parts.append(f"[{meta['category']}]")
        fp = meta.get("path", "")
        parts.append(fp if len(fp) <= 80 else "…" + fp[-79:])
        parts.extend(_change_summary(meta.get("change") or {}))
    if phase == "api_progress":
        parts.append(f"{meta.get('state')} ~{meta.get('est_output_tokens')}tok")
    if phase == "otel_metric":
        val = meta.get("value")
        val = f"{val:.4f}" if isinstance(val, float) else val
        parts.append(f"{meta.get('name')}={val}")
        for k in ("type", "model"):
            if (meta.get("attributes") or {}).get(k):
                parts.append(f"{k}={meta['attributes'][k]}")
    if phase == "otel_event":
        parts.append(str(meta.get("name")))
        attrs = meta.get("attributes") or {}
        for key in _OTEL_TAIL_FIELDS.get(meta.get("name"), ()):
            if attrs.get(key) is not None:
                val = attrs[key]
                val = f"{val:.4f}" if isinstance(val, float) else val
                parts.append(f"{key.split('.')[-1]}={val}")
    if phase == "otel_span":
        parts.append(str(meta.get("name")))
        if meta.get("parent_span_id"):
            parts.append(f"parent={str(meta['parent_span_id'])[:8]}")
    tr = meta.get("tool_result") or {}
    if tr:
        if tr.get("is_error"):
            parts.append("ERR")
        if tr.get("output_bytes") is not None:
            parts.append(f"{tr['output_bytes']}b")
    lat = meta.get("latency") or {}
    if lat.get("ttft_ms") is not None:
        parts.append(f"ttft={lat['ttft_ms']:.0f}ms")
    if lat.get("thinking_ms") is not None:
        parts.append(f"think={lat['thinking_ms']:.0f}ms")
    if phase == "api_thinking_end":
        if meta.get("est_thinking_tokens"):
            parts.append(f"~{meta['est_thinking_tokens']}tok")
        if meta.get("interrupted"):
            parts.append("CUT")
        elif meta.get("signed") is False:
            parts.append("unsigned")
    if phase == "api_generation_start" and meta.get("thinking_to_tool_ms") is not None:
        parts.append(f"dec={meta['thinking_to_tool_ms']:.0f}ms")
    think = meta.get("thinking") or {}
    if think:
        if think.get("est_thinking_tokens"):
            frac = think.get("thinking_output_frac")
            suffix = f" ({frac:.0%} of out)" if isinstance(frac, (int, float)) else ""
            parts.append(f"think~{think['est_thinking_tokens']}tok{suffix}")
        if think.get("interrupted"):
            parts.append("THINK-CUT")
        if think.get("thinking_requested") and not think.get("thinking_fired"):
            parts.append("no-think")
        if think.get("budget_utilization") is not None:
            parts.append(f"budget={think['budget_utilization']:.0%}")
        elif think.get("requested_effort"):
            parts.append(f"effort={think['requested_effort']}")
    if lat.get("output_tokens_per_sec") is not None:
        parts.append(f"{lat['output_tokens_per_sec']:.0f}tok/s")
    if meta.get("cache_read_input_tokens"):
        parts.append(f"cache_r={meta['cache_read_input_tokens']}")
    usage = meta.get("usage") or {}
    if usage.get("cache_read_input_tokens"):
        parts.append(f"cache_r={usage['cache_read_input_tokens']}")
    if meta.get("stop_reason"):
        parts.append(f"stop={meta['stop_reason']}")

    # escape: parts are data and may contain [...] that Rich would eat as markup
    return ("  [dim]" + escape(" ".join(parts)) + "[/dim]") if parts else ""


# Per-event-name attribute picks for the tail line — the fields that make a
# native telemetry event readable at a glance.
_OTEL_TAIL_FIELDS = {
    "api_request": ("model", "query_source", "duration_ms", "cost_usd",
                    "input_tokens", "output_tokens", "request_id"),
    "api_error": ("model", "status_code", "attempt", "error"),
    "api_retries_exhausted": ("status_code", "total_attempts",
                              "total_retry_duration_ms"),
    "api_refusal": ("model", "category", "server_fallback_hop"),
    "tool_decision": ("tool_name", "decision", "source"),
    "tool_result": ("tool_name", "success", "duration_ms", "error_type"),
    "permission_mode_changed": ("from_mode", "to_mode", "trigger"),
    "mcp_server_connection": ("server_name", "status", "transport_type",
                              "duration_ms", "error_code"),
    "compaction": ("trigger", "success", "duration_ms", "pre_tokens",
                   "post_tokens"),
    "skill_activated": ("skill.name", "invocation_trigger"),
    "at_mention": ("mention_type", "success"),
    "internal_error": ("error_name", "error_code"),
    "auth": ("action", "success", "auth_method"),
    "hook_execution_complete": ("hook_name", "total_duration_ms",
                                "num_blocking"),
}


def _strip_query(path: str) -> str:
    return path.split("?", 1)[0]


def _change_summary(change: dict) -> list[str]:
    """One-line rendering of a file_change content delta."""
    if not change:
        return []
    parts = [f"({change['kind']})"]
    if change.get("bytes_delta"):
        delta = change["bytes_delta"]
        parts.append(f"{'+' if delta > 0 else ''}{delta}b")
    for rec in (change.get("records") or [])[:3]:
        if rec.get("more"):
            parts.append(f"…+{rec['more']} more")
            continue
        label = "/".join(dict.fromkeys(filter(None, (rec.get("type"), rec.get("role")))))
        text = (rec.get("preview") or "").replace("\n", " ")[:60]
        parts.append(f"<{label}> {text!r}" if text else f"<{label}>")
    if change.get("snippet") and not change.get("records"):
        snippet = change["snippet"].replace("\n", " ⏎ ")
        parts.append(snippet[:120])
    return parts


def _phase_colour(phase: str) -> str:
    if "error" in phase:
        return "red"
    if "progress" in phase:
        return "dim cyan"
    if "otel" in phase:
        return "bright_green"
    if "network" in phase:
        return "bright_blue"
    if "thinking" in phase:
        return "magenta"
    if "tokenizer" in phase:
        return "bright_cyan"
    if "api" in phase:
        return "cyan"
    if "tool" in phase:
        return "yellow"
    if "file" in phase:
        return "blue"
    if "prompt" in phase:
        return "green"
    if "notification" in phase:
        return "yellow"
    if "compact" in phase:
        return "magenta"
    if "session" in phase or "turn" in phase or "subagent" in phase:
        return "blue"
    return "white"


# ------------------------------------------------------------------ #
# report                                                               #
# ------------------------------------------------------------------ #

@main.command()
@click.option("--session", default=None, help="Filter by session_id")
@click.option("--since",   default=None, type=float, help="Unix timestamp lower bound")
@click.option("--input", "input_file", default=None, type=click.Path(exists=True),
              help="Read events from a JSONL file instead of the daemon")
@click.option("--json", "as_json", is_flag=True, help="Emit raw JSON instead of tables")
@click.option("--backup", "backup_dir", is_flag=False, flag_value="__default__",
              default=None, type=click.Path(),
              help="Snapshot all report data (SQLite + JSONL) to a directory "
                   "(default: ~/.cia/backups/<timestamp>/) and exit")
@click.option("--reset", is_flag=True,
              help="Delete all recorded events, resetting the report to empty")
@click.option("--yes", "-y", is_flag=True, help="Skip the --reset confirmation prompt")
@click.option("--no-transcripts", is_flag=True,
              help="Skip reading Claude Code's on-disk session transcripts "
                   "and /insights usage-data")
def report(session, since, input_file, as_json, backup_dir, reset, yes,
           no_transcripts):
    """Derived performance report: turns, tools, human latency, compactions, rework.

    With --backup, snapshot the underlying event store instead of reporting.
    With --reset, wipe it. Combine them to back up before resetting.
    """
    from cia.analytics import full_report

    if backup_dir is not None:
        _report_backup(backup_dir)
    if reset:
        _report_reset(yes)
    if backup_dir is not None or reset:
        return

    events = _load_events(input_file, since)
    if session:
        # Keep session-less proxy events; turn anatomy matches them by time.
        events = [e for e in events if e.session_id in (None, session)]
    if not events:
        console.print("[yellow]No events found.[/yellow]")
        return

    data = full_report(events, use_transcripts=not no_transcripts)
    if as_json:
        print(json.dumps(data, indent=2))
        return

    _render_sessions(data["sessions"])
    _render_turns(data["turns"])
    _render_tools(data["tools"])
    _render_chains(data["chains"])
    _render_human(data["human"])
    _render_compactions(data["compactions"])
    _render_rework(data["rework"])
    _render_cache(data["cache"])
    _render_thinking(data["thinking"])
    _render_context(data["context"])
    _render_cost(data["cost"])
    _render_throughput(data["throughput"])
    _render_network(data["network"])
    _render_otel(data["otel"])
    _render_transcripts(data["transcripts"])


def _report_backup(backup_dir: str) -> None:
    """Snapshot the daemon's event store (SQLite + JSONL) to a directory."""
    if backup_dir == "__default__":
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = CIA_DIR / "backups" / stamp
    else:
        dest = Path(backup_dir)
    dest = dest.expanduser().resolve()

    result = _require_ok(_send({"cmd": "backup", "dir": str(dest)}))
    console.print(f"[green]Backed up {result.get('events', 0)} events → "
                  f"{result.get('dir', dest)}[/green]")
    console.print(f"  SQLite : [cyan]{result.get('db', '-')}[/cyan]")
    if result.get("jsonl"):
        console.print(f"  JSONL  : [cyan]{result['jsonl']}[/cyan]")


def _report_reset(assume_yes: bool) -> None:
    """Wipe all recorded events so the report starts from empty."""
    count = _require_ok(_send({"cmd": "status"})).get("events", 0)
    if not assume_yes and not click.confirm(
        f"Delete all {count} recorded event(s)? This cannot be undone "
        f"(use 'cia report --backup' first to keep a copy)"
    ):
        console.print("[yellow]Reset cancelled.[/yellow]")
        return
    _require_ok(_send({"cmd": "clear"}))
    console.print(f"[green]Report reset — {count} event(s) cleared.[/green]")


def _load_events(input_file, since):
    from cia.schema import Event
    if input_file:
        lines = Path(input_file).read_text().splitlines()
    else:
        cmd = {"cmd": "export", "format": "jsonl"}
        if since is not None:
            cmd["since"] = since
        lines = _require_ok(_send(cmd)).get("data", "").splitlines()
    events = []
    for line in lines:
        try:
            events.append(Event.from_dict(json.loads(line)))
        except Exception:
            continue
    if since is not None:
        events = [e for e in events if e.ts >= since]
    events.sort(key=lambda e: e.ts)
    return events


def _fmt_s(ms) -> str:
    return f"{ms/1000:.1f}" if ms is not None else "-"


def _render_sessions(stories: list) -> None:
    t = Table(title="Session stories", show_header=True, header_style="bold cyan")
    for col in ("session", "started", "dur", "turns", "api", "tok in/out",
                "think s", "tools", "wait s", "coverage"):
        t.add_column(col, justify="right" if col not in ("session", "started", "coverage") else "left")
    t.add_column("title", no_wrap=True, overflow="ellipsis", max_width=26)
    for s in stories:
        mins, secs = divmod(int(s["duration_s"]), 60)
        cov = "".join(
            f"[green]{k[0].upper()}[/green]" if v else f"[red]{k[0].upper()}[/red]"
            for k, v in s["coverage"].items()
        )
        turns = str(s["turns"]) + (f"+{s['incomplete_turns']}*" if s["incomplete_turns"] else "")
        tok_mark = "†" if s.get("token_source") == "transcript" else ""
        title = escape(s.get("title") or "")
        if s.get("project"):
            proj = f"[dim]({escape(s['project'])})[/dim]"
            title = f"{title} {proj}" if title else proj
        t.add_row(
            s["session_id"][:8],
            time.strftime("%m-%d %H:%M", time.localtime(s["start_ts"])),
            f"{mins}m{secs:02d}s",
            turns, str(s["api_calls"]),
            f"{s['tokens_input']}/{s['tokens_output']}{tok_mark}",
            f"{s['thinking_ms']/1000:.1f}",
            f"{s['tool_calls']}" + (f" ({s['tool_errors']}E)" if s["tool_errors"] else ""),
            f"{s['permission_wait_s'] + s['think_time_s']:.0f}",
            cov,
            title,
        )
    console.print(t)
    console.print("[dim]coverage: H=hooks P=proxy F=fswatch T=transcript "
                  "([green]green[/green]=data present, [red]red[/red]=missing); "
                  "* = turn still open at capture end; "
                  "† = tokens from the on-disk transcript (session not proxied)[/dim]")
    seen = set()
    for s in stories:
        for gap in s["gaps"]:
            key = (s["session_id"][:8], gap.split("—")[0])
            if key in seen:
                continue
            seen.add(key)
            console.print(f"  [yellow]{s['session_id'][:8]}[/yellow]: [dim]{gap}[/dim]")


def _render_turns(turns: list) -> None:
    t = Table(title="Turn anatomy — where the wall-clock went (seconds)",
              show_header=True, header_style="bold cyan")
    for col in ("start", "wall", "api", "think", "tools", "wait", "other",
                "tok", "edits"):
        t.add_column(col, justify="right" if col != "start" else "left")
    t.add_column("prompt", no_wrap=True, overflow="ellipsis", max_width=28)
    for turn in turns:
        t.add_row(
            time.strftime("%H:%M:%S", time.localtime(turn["start_ts"]))
            + ("" if turn["complete"] else "*"),
            _fmt_s(turn["wall_ms"]), _fmt_s(turn["api_ms"]),
            _fmt_s(turn["thinking_ms"]),
            f"{_fmt_s(turn['tool_ms'])} ({turn['tool_calls']})",
            _fmt_s(turn["permission_wait_ms"]), _fmt_s(turn["other_ms"]),
            str(turn["tokens_output"]), str(turn["edits"]),
            turn["prompt"],
        )
    console.print(t)
    console.print("[dim]wait = permission prompts; other = mostly user "
                  "interaction outside permission prompts. Full detail: "
                  "cia report --json[/dim]")


def _render_tools(tools: list) -> None:
    t = Table(title="Tool performance profiles",
              show_header=True, header_style="bold cyan")
    for col in ("tool", "calls", "err%", "p50 ms", "p90 ms", "p99 ms",
                "max ms", "total s", "avg out"):
        t.add_column(col, justify="right" if col != "tool" else "left")
    for p in tools:
        t.add_row(
            p["tool"], str(p["calls"]),
            f"{p['error_rate']*100:.0f}",
            f"{p['p50_ms']:.0f}", f"{p['p90_ms']:.0f}", f"{p['p99_ms']:.0f}",
            f"{p['max_ms']:.0f}", f"{p['total_ms']/1000:.1f}",
            f"{p['avg_output_bytes']/1024:.1f}K" if p["avg_output_bytes"] else "-",
        )
    console.print(t)


def _render_human(human: dict) -> None:
    t = Table(title="Human latency — time waiting on the user",
              show_header=True, header_style="bold cyan")
    for col in ("kind", "count", "total s", "mean s", "p50 s", "max s"):
        t.add_column(col, justify="right" if col != "kind" else "left")
    labels = {"permission": "permission waits", "input": "input waits",
              "think": "think time (turn gap)"}
    for key, label in labels.items():
        s = human["summary"][key]
        t.add_row(
            label, str(s["count"]),
            f"{s['total_s']:.1f}" if s["count"] else "-",
            f"{s['mean_s']:.1f}" if s["count"] else "-",
            f"{s['p50_s']:.1f}" if s["count"] else "-",
            f"{s['max_s']:.1f}" if s["count"] else "-",
        )
    console.print(t)
    active = human.get("native_active_time")
    if active:
        console.print(f"[dim]Claude Code's own active-time split: "
                      f"user {active['user_s']:.0f}s at the keyboard, "
                      f"cli {active['cli_s']:.0f}s working[/dim]")


def _render_compactions(compactions: list) -> None:
    if not compactions:
        return
    t = Table(title="Compaction cost", show_header=True, header_style="bold cyan")
    for col in ("time", "trigger", "ctx before", "ctx after", "reclaimed",
                "recovery s", "native"):
        t.add_column(col, justify="right" if "ctx" in col or col == "reclaimed" else "left")
    for c in compactions:
        native = c.get("native")
        if native:
            native_str = ("[red]FAILED[/red]" if not native["success"]
                          else f"{native['duration_ms']/1000:.1f}s"
                          if native["duration_ms"] is not None else "ok")
        else:
            native_str = "-"
        t.add_row(
            time.strftime("%H:%M:%S", time.localtime(c["ts"])),
            c["trigger"] or "-",
            _fmt_tok(c["context_before"]), _fmt_tok(c["context_after"]),
            _fmt_tok(c["reclaimed_tokens"]),
            f"{c['recovery_s']:.1f}" if c["recovery_s"] is not None else "-",
            native_str,
        )
    console.print(t)
    if any(c.get("native") for c in compactions):
        console.print("[dim]native = Claude Code's own compaction event "
                      "(exact pre/post tokens, duration); rows without a "
                      "PreCompact hook come from native telemetry alone[/dim]")


def _fmt_tok(v) -> str:
    return f"{int(v):,}" if v is not None else "-"


def _render_rework(files: list) -> None:
    flagged_or_busy = [f for f in files if f["flagged"] or f["edits"] >= 2]
    if not flagged_or_busy:
        return
    t = Table(title="Rework — files edited repeatedly",
              show_header=True, header_style="bold cyan")
    for col in ("file", "edits", "max/turn", "fs events", "thrash?"):
        t.add_column(col, justify="right" if col != "file" else "left")
    for f in flagged_or_busy:
        t.add_row(
            f["file"], str(f["edits"]), str(f["max_edits_one_turn"]),
            str(f["file_changes"] or "-"),
            "[red]YES[/red]" if f["flagged"] else "",
        )
    console.print(t)


def _render_chains(chains: dict) -> None:
    st = chains["search_thrash"]
    er = chains["error_recovery"]
    if not chains["transitions"]:
        return
    console.print("[bold cyan]Tool chains[/bold cyan]")
    top = ", ".join(f"{t['from']}→{t['to']} ×{t['count']}"
                    for t in chains["transitions"][:5])
    console.print(f"  transitions: [dim]{escape(top)}[/dim]")
    if st["searches"] or st["reads"]:
        ratio = (f"{st['search_to_read_ratio']:.1f}"
                 if st["search_to_read_ratio"] is not None else "-")
        console.print(
            f"  search thrash: {st['searches']} searches / {st['reads']} reads "
            f"(ratio {ratio}), {len(st['thrash_turns'])} turn(s) with 3+ "
            f"searches before the first Read")
    if er["errors"]:
        line = f"  error recovery: {er['recovered']}/{er['errors']} recovered"
        if er["recovery_calls_p50"] is not None:
            line += (f", p50 {er['recovery_calls_p50']:.0f} call(s) / "
                     f"{er['recovery_ms_p50']/1000:.1f}s to next success")
        if er["unrecovered"]:
            line += f", [red]{er['unrecovered']} never recovered[/red]"
        console.print(line)
    if chains["retry_loops"]:
        t = Table(title="Retry loops — same tool, same target, back to back",
                  show_header=True, header_style="bold cyan")
        for col in ("time", "tool", "target", "repeats", "errors"):
            t.add_column(col, justify="right" if col in ("repeats", "errors") else "left")
        for loop in chains["retry_loops"][:10]:
            t.add_row(
                time.strftime("%H:%M:%S", time.localtime(loop["first_ts"])),
                loop["tool"], (loop["target"] or "")[:50],
                str(loop["repeats"]), str(loop["errors"]),
            )
        console.print(t)


def _render_cache(cache: dict) -> None:
    if not cache["requests"]:
        return
    tok = cache["tokens"]
    warm, cold = cache["ttfb_ms"]["warm"], cache["ttfb_ms"]["cold"]
    t = Table(title="Cache economics", show_header=True, header_style="bold cyan")
    for col in ("requests", "warm", "hit rate", "read ratio",
                "ttfb warm p50", "ttfb cold p50", "ttl expiries", "retokenized"):
        t.add_column(col, justify="right")
    t.add_row(
        str(cache["requests"]), str(cache["warm_requests"]),
        f"{cache['hit_rate']*100:.0f}%" if cache["hit_rate"] is not None else "-",
        f"{tok['read_ratio']*100:.0f}%" if tok["read_ratio"] is not None else "-",
        f"{warm['p50']:.0f}ms" if warm["p50"] is not None else "-",
        f"{cold['p50']:.0f}ms" if cold["p50"] is not None else "-",
        str(cache["ttl"]["expiries"]),
        f"{cache['ttl']['retokenized_tokens']:,}" if cache["ttl"]["retokenized_tokens"] else "-",
    )
    console.print(t)
    if cache["busts"]:
        bt = Table(title="Cache busts — prompt-cache prefix rebuilt",
                   show_header=True, header_style="bold cyan")
        for col in ("time", "cause", "idle s", "cached before",
                    "read at bust", "retokenized"):
            bt.add_column(col, justify="right" if col != "cause" else "left")
        for b in cache["busts"]:
            bt.add_row(
                time.strftime("%H:%M:%S", time.localtime(b["ts"])),
                b["cause"], f"{b['idle_s']:.0f}",
                f"{b['cached_before']:,}", f"{b['cache_read']:,}",
                f"{b['retokenized_tokens']:,}",
            )
        console.print(bt)
    console.print("[dim]warm = cache covered ≥50% of context; read ratio = "
                  "cache-read tokens / total context tokens; ttl = idle gap "
                  "exceeded the 5-minute prompt-cache window[/dim]")


def _render_thinking(th: dict) -> None:
    if not th["responses"] and not th["decisiveness_ms"]:
        return
    console.print("[bold cyan]Thinking calibration[/bold cyan]")
    if th["thinking_requested"]:
        fr = f"{th['fire_rate']*100:.0f}%" if th["fire_rate"] is not None else "-"
        console.print(f"  adaptive thinking: requested on "
                      f"{th['thinking_requested']} requests, fired on "
                      f"{th['thinking_fired']} ({fr})")
    b = th["budget"]
    if b["samples"]:
        console.print(f"  budget utilization: p50 {b['utilization_p50']*100:.0f}%, "
                      f"max {b['utilization_max']*100:.0f}% "
                      f"({b['interrupted']} thinking block(s) interrupted)")
    for model, d in th["decisiveness_ms"].items():
        console.print(f"  decisiveness {model}: p50 {d['p50']:.0f}ms, "
                      f"p90 {d['p90']:.0f}ms thinking→tool ({d['count']} gaps)")
    if len(th["by_effort"]) > 1:
        t = Table(title="Thinking by requested effort",
                  show_header=True, header_style="bold cyan")
        for col in ("effort", "requests", "fired", "fire rate", "mean think s"):
            t.add_column(col, justify="right" if col != "effort" else "left")
        for k, g in sorted(th["by_effort"].items()):
            t.add_row(
                k, str(g["requests"]), str(g["fired"]),
                f"{g['fire_rate']*100:.0f}%",
                f"{g['mean_thinking_ms']/1000:.1f}" if g["mean_thinking_ms"] else "-",
            )
        console.print(t)
    split = th["turn_split"]
    if split and split["high_thinking"]["turns"] and split["low_thinking"]["turns"]:
        hi, lo = split["high_thinking"], split["low_thinking"]
        console.print(
            f"  turns above median thinking ({split['median_thinking_ms']/1000:.1f}s): "
            f"{hi['mean_tool_errors']:.2f} tool errors / "
            f"{hi['mean_repeat_edit_files']:.2f} re-edited files per turn, "
            f"vs {lo['mean_tool_errors']:.2f} / {lo['mean_repeat_edit_files']:.2f} below")


def _render_context(cp: dict) -> None:
    rows = [r for r in cp["turns"] if r["context_tokens"] is not None]
    if not rows:
        return
    t = Table(title="Context pressure — growth per turn",
              show_header=True, header_style="bold cyan")
    for col in ("start", "context", "Δ tokens", "tool out", "top tool", "compacted"):
        t.add_column(col, justify="right" if col not in ("start", "top tool") else "left")
    for r in rows:
        t.add_row(
            time.strftime("%H:%M:%S", time.localtime(r["start_ts"])),
            f"{r['context_tokens']:,}",
            f"{r['context_delta']:+,}" if r["context_delta"] is not None else "-",
            f"{r['tool_output_bytes']/1024:.0f}K" if r["tool_output_bytes"] else "-",
            r["top_tool"] or "-",
            "yes" if r["compacted"] else "",
        )
    console.print(t)
    bits = []
    if cp["growth_per_turn_p50"]:
        bits.append(f"median growth {cp['growth_per_turn_p50']:,.0f} tok/turn")
    if cp["projected_turns_to_compaction"]:
        proj = ", ".join(f"{(sid or '?')[:8]}: ~{n:g} turns"
                         for sid, n in cp["projected_turns_to_compaction"].items())
        bits.append(f"projected to compaction ({cp['compaction_threshold']:,} tok): {proj}")
    if cp["bloat_by_tool"]:
        top = cp["bloat_by_tool"][0]
        bits.append(f"biggest context feeder: {top['tool']} "
                    f"({top['output_bytes']/1024:.0f}K of tool output)")
    if bits:
        console.print(f"[dim]{escape('; '.join(bits))}[/dim]")


def _render_cost(cost: dict) -> None:
    if not cost.get("available"):
        return

    def usd(v) -> str:
        return f"${v:.4f}" if v is not None else "-"

    t = Table(title="Cost attribution (native telemetry)",
              show_header=True, header_style="bold cyan")
    for col in ("total", "rework", "per commit", "per line added", "unattributed"):
        t.add_column(col, justify="right")
    t.add_row(usd(cost["total_cost_usd"]), usd(cost["rework_cost_usd"]),
              usd(cost["cost_per_commit_usd"]),
              usd(cost["cost_per_line_added_usd"]), usd(cost["unattributed_usd"]))
    console.print(t)

    turns = [r for r in cost["turns"] if r["cost_usd"]]
    if turns:
        tt = Table(title="Cost per turn", show_header=True, header_style="bold cyan")
        for col in ("start", "cost", "rework"):
            tt.add_column(col, justify="right" if col == "cost" else "left")
        tt.add_column("prompt", no_wrap=True, overflow="ellipsis", max_width=40)
        for r in turns:
            tt.add_row(
                time.strftime("%H:%M:%S", time.localtime(r["start_ts"])),
                f"${r['cost_usd']:.4f}",
                "[red]yes[/red]" if r["rework"] else "",
                r["prompt"],
            )
        console.print(tt)
    console.print("[dim]costs from Claude Code's own telemetry, attributed to "
                  "the most recent turn started before each metric export "
                  "(2s granularity under cia run)[/dim]")


def _render_throughput(tp: dict) -> None:
    if not tp["requests"]:
        return
    t = Table(title="Throughput by model", show_header=True, header_style="bold cyan")
    for col in ("model", "req", "tok/s p50", "tok/s p90",
                "ttfb p50", "ttfb p90", "ttft p50"):
        t.add_column(col, justify="right" if col != "model" else "left")

    def fmt(d: dict, key: str, suffix: str = "") -> str:
        return f"{d[key]:.0f}{suffix}" if d[key] is not None else "-"

    for m, s in tp["by_model"].items():
        t.add_row(
            m, str(s["requests"]),
            fmt(s["tok_per_sec"], "p50"), fmt(s["tok_per_sec"], "p90"),
            fmt(s["ttfb_ms"], "p50", "ms"), fmt(s["ttfb_ms"], "p90", "ms"),
            fmt(s["ttft_ms"], "p50", "ms"),
        )
    console.print(t)
    sag = tp["sag"]
    if sag and sag["late_to_early_ratio"] is not None:
        console.print(f"[dim]in-response speed: {sag['early_tok_per_sec']:.0f} tok/s "
                      f"early → {sag['late_tok_per_sec']:.0f} tok/s late "
                      f"({sag['late_to_early_ratio']*100:.0f}% of early pace, "
                      f"{sag['flows']} long response(s))[/dim]")
    if len(tp["by_hour"]) > 1:
        hours = "  ".join(f"{h:02d}h:{s['tok_per_sec_p50']:.0f}"
                          for h, s in tp["by_hour"].items())
        console.print(f"[dim]tok/s p50 by hour: {hours}[/dim]")
    slow = tp["slow_requests"]
    if slow:
        worst = slow[0]
        console.print(f"[dim]slowest TTFB: {worst['ttfb_ms']:.0f}ms at "
                      f"{time.strftime('%H:%M:%S', time.localtime(worst['ts']))} "
                      f"({worst['model']})[/dim]")


def _render_network(net: dict) -> None:
    if not net["by_category"]:
        return
    t = Table(title="Network overhead — non-inference traffic",
              show_header=True, header_style="bold cyan")
    for col in ("category", "req", "err", "total ms", "KB", "top hosts"):
        t.add_column(col, justify="right" if col not in ("category", "top hosts") else "left")
    for c in net["by_category"]:
        t.add_row(
            c["category"], str(c["requests"]),
            str(c["errors"]) if c["errors"] else "-",
            f"{c['total_ms']:.0f}",
            f"{c['total_bytes']/1024:.1f}",
            ", ".join(c["top_hosts"]),
        )
    console.print(t)
    tot = net["totals"]
    if tot["overhead_time_frac"] is not None:
        console.print(f"[dim]overhead: {tot['overhead_ms']/1000:.1f}s across "
                      f"{tot['overhead_requests']} request(s) = "
                      f"{tot['overhead_time_frac']*100:.1f}% of network time "
                      f"(inference {tot['inference_ms']/1000:.1f}s)[/dim]")
    for fail in net["failures"][:10]:
        during = " [red]during API call[/red]" if fail["during_api_call"] else ""
        console.print(f"  [red]{fail['status'] or 'ERR'}[/red] "
                      f"{escape(fail['host'] + _strip_query(fail['path'] or ''))} "
                      f"[dim][{fail['category']}][/dim]{during}")


def _render_otel(otel: dict) -> None:
    """Sections derived from Claude Code's own telemetry (cia run only)."""
    if not otel["available"]:
        return

    perm = otel["permissions"]
    if perm["decisions"]:
        console.print("[bold cyan]Permission economics (native telemetry)[/bold cyan]")
        auto = (f"{perm['auto_rate']*100:.0f}%"
                if perm["auto_rate"] is not None else "-")
        console.print(
            f"  {perm['decisions']} decision(s): {perm['accepts']} accepted / "
            f"{perm['rejects']} rejected; {perm['auto_approved']} auto-approved "
            f"({auto}) by config/hooks")
        srcs = ", ".join(f"{k} ×{v}" for k, v in
                         sorted(perm["by_source"].items(), key=lambda kv: -kv[1]))
        console.print(f"  by source: [dim]{escape(srcs)}[/dim]")
        rejected = {k: v for k, v in perm["by_tool"].items() if v["rejects"]}
        if rejected:
            rej = ", ".join(f"{k} ×{v['rejects']}" for k, v in
                            sorted(rejected.items(), key=lambda kv: -kv[1]["rejects"]))
            console.print(f"  rejections by tool: [red]{escape(rej)}[/red]")

    rel = otel["api_reliability"]
    if rel["available"]:
        console.print("[bold cyan]API reliability (native telemetry)[/bold cyan]")
        if rel["errors"]:
            statuses = ", ".join(f"{k} ×{v}" for k, v in
                                 sorted(rel["errors_by_status"].items()))
            console.print(f"  {rel['errors']} API error(s) "
                          f"({escape(statuses)}), {rel['error_ms']/1000:.1f}s "
                          f"spent in failed attempts")
        for x in rel["retries_exhausted"]:
            attempts = int(x["total_attempts"] or 0)
            console.print(f"  [red]retries exhausted[/red] at "
                          f"{time.strftime('%H:%M:%S', time.localtime(x['ts']))}: "
                          f"{attempts} attempt(s), "
                          f"{(x['retry_ms'] or 0)/1000:.1f}s lost "
                          f"({x['model'] or '?'})")
        for r in rel["refusals"]:
            cat = f" category={r['category']}" if r.get("category") else ""
            console.print(f"  [red]refusal[/red] at "
                          f"{time.strftime('%H:%M:%S', time.localtime(r['ts']))} "
                          f"({r['model'] or '?'}){escape(cat)}")

    sub = otel["subsystems"]
    if sub["available"]:
        t = Table(title="Cost by subsystem (native telemetry)",
                  show_header=True, header_style="bold cyan")
        for col in ("query source", "req", "cost", "tok in/out", "cache read", "api s"):
            t.add_column(col, justify="right" if col != "query source" else "left")
        for qs, g in sorted(otel["subsystems"]["by_query_source"].items(),
                            key=lambda kv: -kv[1]["cost_usd"]):
            t.add_row(
                qs, str(g["requests"]), f"${g['cost_usd']:.4f}",
                f"{g['input_tokens']:,}/{g['output_tokens']:,}",
                f"{g['cache_read_tokens']:,}", f"{g['api_ms']/1000:.1f}",
            )
        console.print(t)
        for key, groups in sub["attribution"].items():
            line = ", ".join(f"{name}: ${g['cost_usd']:.4f} ({g['requests']} req)"
                             for name, g in sorted(groups.items(),
                                                   key=lambda kv: -kv[1]["cost_usd"]))
            console.print(f"  by {key}: [dim]{escape(line)}[/dim]")

    hooks = otel["hooks"]
    if hooks["hooks"]:
        t = Table(title="Hook overhead (native telemetry)",
                  show_header=True, header_style="bold cyan")
        for col in ("hook", "runs", "total s", "p50 ms", "max ms", "blocking", "err"):
            t.add_column(col, justify="right" if col != "hook" else "left")
        for h in hooks["hooks"]:
            t.add_row(
                h["hook"], str(h["runs"]), f"{h['total_ms']/1000:.1f}",
                f"{h['p50_ms']:.0f}" if h["p50_ms"] is not None else "-",
                f"{h['max_ms']:.0f}" if h["max_ms"] is not None else "-",
                str(h["blocking"]) if h["blocking"] else "-",
                str(h["errors"]) if h["errors"] else "-",
            )
        console.print(t)
        console.print("[dim]includes CIA's own instrumentation hooks — this "
                      "is what observing the session costs it[/dim]")

    mcp = otel["mcp"]
    if mcp["attempts"]:
        statuses = ", ".join(f"{k} ×{v}" for k, v in sorted(mcp["by_status"].items()))
        c = mcp["connect_ms"]
        timing = (f"; connect p50 {c['p50']:.0f}ms, max {c['max']:.0f}ms"
                  if c["count"] else "")
        console.print(f"[bold cyan]MCP connections (native telemetry)[/bold cyan]")
        console.print(f"  {escape(statuses)}{timing}")
        for f in mcp["failures"][:5]:
            server = f["server"] or "server name redacted — rerun with cia run --detail"
            console.print(f"  [red]failed[/red]: {escape(str(server))} "
                          f"[dim]({f['transport'] or '?'}, "
                          f"code {f['error_code'] or '?'})[/dim]")

    err = otel["errors"]
    if err["internal_errors"] or err["auth_failures"]:
        console.print("[bold cyan]Client health (native telemetry)[/bold cyan]")
        for name, n in sorted(err["internal_errors"].items(), key=lambda kv: -kv[1]):
            console.print(f"  internal error {escape(name)} ×{n}")
        if err["auth_failures"]:
            console.print(f"  [red]{err['auth_failures']} auth failure(s)[/red]")

    if otel["session_starts"]:
        line = ", ".join(f"{k}: {v}" for k, v in otel["session_starts"].items())
        console.print(f"[dim]session starts by type: {escape(line)}[/dim]")


def _render_transcripts(tr: dict) -> None:
    """Sections derived from on-disk transcripts + /insights usage-data."""
    if not tr["available"]:
        return

    subs = tr["subagent_economics"]
    if subs:
        t = Table(title="Subagent economics (transcripts)",
                  show_header=True, header_style="bold cyan")
        for col in ("agent type", "runs", "tok out", "tok in", "cache read",
                    "tool calls"):
            t.add_column(col, justify="right" if col != "agent type" else "left")
        for name, g in sorted(subs.items(),
                              key=lambda kv: -kv[1]["output_tokens"]):
            t.add_row(
                name, str(g["runs"]), f"{g['output_tokens']:,}",
                f"{g['input_tokens']:,}", f"{g['cache_read_tokens']:,}",
                str(g["tool_calls"]),
            )
        console.print(t)

    delivered = [(sid, s) for sid, s in tr["sessions"].items()
                 if s.get("insights")]
    for sid, s in delivered:
        ins = s["insights"]
        bits = []
        if ins.get("lines_added") is not None or ins.get("lines_removed") is not None:
            bits.append(f"+{ins.get('lines_added') or 0}/"
                        f"-{ins.get('lines_removed') or 0} lines")
        if ins.get("files_modified"):
            bits.append(f"{ins['files_modified']} file(s)")
        if ins.get("git_commits"):
            bits.append(f"{ins['git_commits']} commit(s)")
        if ins.get("outcome"):
            bits.append(f"outcome: {ins['outcome']}")
        if bits:
            console.print(f"  [cyan]{sid[:8]}[/cyan] delivered "
                          f"[dim]{escape(', '.join(str(b) for b in bits))}[/dim]")

    disagreements = [
        (sid, s["agreement"]) for sid, s in tr["sessions"].items()
        if (s["agreement"]["disagreement_frac"] or 0) > 0.05
    ]
    if disagreements:
        console.print("[bold cyan]Source agreement — output tokens[/bold cyan]")
        for sid, a in disagreements:
            src = ", ".join(f"{k}={int(v):,}" for k, v in
                            a["output_tokens"].items() if v)
            console.print(f"  [yellow]{sid[:8]}[/yellow] sources disagree by "
                          f"{a['disagreement_frac']*100:.0f}%: "
                          f"[dim]{escape(src)}[/dim]")
        console.print("[dim]transcript = usage saved in the session transcript "
                      "(ground truth for what Claude Code was billed); "
                      "divergence usually means partial proxy/telemetry "
                      "coverage of the session[/dim]")


# ------------------------------------------------------------------ #
# install-hooks / uninstall-hooks                                      #
# ------------------------------------------------------------------ #

@main.command("install-hooks")
@click.option("--global", "is_global", is_flag=True, help="Install to ~/.claude/settings.json")
def install_hooks(is_global):
    """Install Claude Code hook scripts."""
    from cia.hooks import install_hooks as _install
    scope = "global" if is_global else "project"
    path = _install(scope)
    console.print(f"[green]Hooks installed → {path}[/green]")


@main.command("uninstall-hooks")
@click.option("--global", "is_global", is_flag=True, help="Remove from ~/.claude/settings.json")
def uninstall_hooks(is_global):
    """Remove CIA hook scripts."""
    from cia.hooks import uninstall_hooks as _uninstall
    scope = "global" if is_global else "project"
    path = _uninstall(scope)
    console.print(f"[green]Hooks removed from {path}[/green]")


# ------------------------------------------------------------------ #
# trust-cert                                                           #
# ------------------------------------------------------------------ #

@main.command("trust-cert")
def trust_cert():
    """Print instructions for trusting the mitmproxy CA certificate."""
    cert = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"
    console.print("[bold]To route Claude Code through CIA's proxy:[/bold]\n")
    console.print("  [cyan]1. Run 'cia start' once to generate the mitmproxy CA.[/cyan]")
    console.print(f"\n  [cyan]2. Trust the cert (macOS keychain):[/cyan]")
    console.print(f"       sudo security add-trusted-cert -d -r trustRoot \\")
    console.print(f"         -k /Library/Keychains/System.keychain {cert}\n")
    console.print("  [cyan]3. Start Claude with:[/cyan]")
    console.print("       HTTPS_PROXY=http://127.0.0.1:8080 \\")
    console.print(f"       NODE_EXTRA_CA_CERTS={cert} \\")
    console.print("       claude\n")
    console.print("  [dim]Or add these to your shell profile so they apply automatically.[/dim]")
