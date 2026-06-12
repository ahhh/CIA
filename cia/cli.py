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

def _send(cmd: dict) -> dict:
    if not SOCKET_PATH.exists():
        console.print("[red]CIA daemon not running. Run: cia start[/red]")
        sys.exit(1)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(SOCKET_PATH))
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
@click.option("--watch-claude/--no-watch-claude", default=True, show_default=True,
              help="Also watch Claude Code's own memory/session/transcript data for this project")
@click.option("--foreground", is_flag=True, help="Run in foreground (no fork)")
def start(proxy_port, hook_port, otlp_port, db, jsonl, watch_dirs, watch_claude, foreground):
    """Start the CIA monitoring daemon."""
    if SOCKET_PATH.exists():
        console.print("[yellow]CIA daemon appears to already be running. "
                      "Use 'cia stop' first.[/yellow]")
        sys.exit(1)

    resolved_watch = [Path(d) for d in watch_dirs]
    if watch_claude:
        from cia.claude_paths import claude_watch_dirs
        claude_dirs = claude_watch_dirs(Path.cwd())
        resolved_watch.extend(claude_dirs)
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
                   cert: Path | None = None) -> dict:
    """Env vars that route a child process through CIA's collectors."""
    cert = cert or Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"
    return {
        # Route HTTPS through the mitmproxy collector
        "HTTPS_PROXY": f"http://127.0.0.1:{proxy_port}",
        "NODE_EXTRA_CA_CERTS": str(cert),
        # Claude Code native telemetry → CIA's OTLP receiver
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/json",
        "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{otlp_port}",
        "OTEL_METRIC_EXPORT_INTERVAL": "10000",
        "OTEL_LOGS_EXPORT_INTERVAL": "5000",
    }


@main.command(context_settings={"ignore_unknown_options": True})
@click.option("--proxy-port", default=8080, show_default=True)
@click.option("--otlp-port",  default=4318, show_default=True)
@click.argument("command", nargs=-1, type=click.UNPROCESSED)
def run(proxy_port, otlp_port, command):
    """Launch a command (default: claude) fully wired into CIA.

    Sets HTTPS_PROXY + the mitmproxy CA cert so API traffic is captured,
    and enables Claude Code's native OpenTelemetry export pointed at
    CIA's OTLP receiver.  Example:

        cia run claude
        cia run -- claude --continue
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
    env.update(_build_run_env(proxy_port, otlp_port, cert))
    console.print(f"[green]cia run:[/green] [cyan]{' '.join(argv)}[/cyan] "
                  f"[dim](proxy :{proxy_port}, otlp :{otlp_port})[/dim]")
    try:
        os.execvpe(argv[0], argv, env)
    except FileNotFoundError:
        console.print(f"[red]Command not found: {argv[0]}[/red]")
        sys.exit(127)


# ------------------------------------------------------------------ #
# stop                                                                 #
# ------------------------------------------------------------------ #

@main.command()
def stop():
    """Stop the CIA daemon."""
    result = _require_ok(_send({"cmd": "stop"}))
    # Wait for socket to disappear
    for _ in range(20):
        if not SOCKET_PATH.exists():
            break
        time.sleep(0.1)
    PID_FILE.unlink(missing_ok=True)
    console.print("[green]CIA daemon stopped.[/green]")


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
    """Stream live events to the terminal (polls the daemon)."""
    last_ts: float = time.time() - 1.0
    console.print("[cyan]Tailing CIA events (Ctrl-C to stop)...[/cyan]")
    try:
        while True:
            try:
                result = _send({"cmd": "export", "format": "jsonl", "since": last_ts})
                if result.get("ok"):
                    data = result.get("data", "").strip()
                    if data:
                        for line in data.splitlines():
                            try:
                                evt = json.loads(line)
                                _print_event(evt)
                                ts = evt.get("ts", last_ts)
                                if ts > last_ts:
                                    last_ts = ts + 0.0001
                            except Exception:
                                pass
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
            parts.append(f"$ {meta['command'][:60]}")
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
def report(session, since, input_file, as_json):
    """Derived performance report: turns, tools, human latency, compactions, rework."""
    from cia.analytics import full_report

    events = _load_events(input_file, since)
    if session:
        # Keep session-less proxy events; turn anatomy matches them by time.
        events = [e for e in events if e.session_id in (None, session)]
    if not events:
        console.print("[yellow]No events found.[/yellow]")
        return

    data = full_report(events)
    if as_json:
        print(json.dumps(data, indent=2))
        return

    _render_sessions(data["sessions"])
    _render_turns(data["turns"])
    _render_tools(data["tools"])
    _render_human(data["human"])
    _render_compactions(data["compactions"])
    _render_rework(data["rework"])


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
    for s in stories:
        mins, secs = divmod(int(s["duration_s"]), 60)
        cov = "".join(
            f"[green]{k[0].upper()}[/green]" if v else f"[red]{k[0].upper()}[/red]"
            for k, v in s["coverage"].items()
        )
        turns = str(s["turns"]) + (f"+{s['incomplete_turns']}*" if s["incomplete_turns"] else "")
        t.add_row(
            s["session_id"][:8],
            time.strftime("%m-%d %H:%M", time.localtime(s["start_ts"])),
            f"{mins}m{secs:02d}s",
            turns, str(s["api_calls"]),
            f"{s['tokens_input']}/{s['tokens_output']}",
            f"{s['thinking_ms']/1000:.1f}",
            f"{s['tool_calls']}" + (f" ({s['tool_errors']}E)" if s["tool_errors"] else ""),
            f"{s['permission_wait_s'] + s['think_time_s']:.0f}",
            cov,
        )
    console.print(t)
    console.print("[dim]coverage: H=hooks P=proxy F=fswatch "
                  "([green]green[/green]=data present, [red]red[/red]=missing); "
                  "* = turn still open at capture end[/dim]")
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


def _render_compactions(compactions: list) -> None:
    if not compactions:
        return
    t = Table(title="Compaction cost", show_header=True, header_style="bold cyan")
    for col in ("time", "trigger", "ctx before", "ctx after", "reclaimed", "recovery s"):
        t.add_column(col, justify="right" if "ctx" in col or col == "reclaimed" else "left")
    for c in compactions:
        t.add_row(
            time.strftime("%H:%M:%S", time.localtime(c["ts"])),
            c["trigger"] or "-",
            str(c["context_before"] or "-"), str(c["context_after"] or "-"),
            str(c["reclaimed_tokens"] or "-"),
            f"{c['recovery_s']:.1f}" if c["recovery_s"] is not None else "-",
        )
    console.print(t)


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
