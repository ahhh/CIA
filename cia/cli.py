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
@click.option("--db",         default=str(CIA_DIR / "cia.db"), show_default=True, help="SQLite path")
@click.option("--jsonl",      default=str(CIA_DIR / "events.jsonl"), show_default=True, help="JSONL mirror path")
@click.option("--watch-dir",  "watch_dirs", multiple=True, type=click.Path(), help="Dirs to watch (repeatable)")
@click.option("--foreground", is_flag=True, help="Run in foreground (no fork)")
def start(proxy_port, hook_port, db, jsonl, watch_dirs, foreground):
    """Start the CIA monitoring daemon."""
    if SOCKET_PATH.exists():
        console.print("[yellow]CIA daemon appears to already be running. "
                      "Use 'cia stop' first.[/yellow]")
        sys.exit(1)

    kwargs = dict(
        db_path=Path(db),
        jsonl_path=Path(jsonl),
        proxy_port=proxy_port,
        hook_port=hook_port,
        watch_dirs=[Path(d) for d in watch_dirs],
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
    colour = _phase_colour(phase)
    console.print(f"[dim]{ts_str}.{ms}[/dim]  [{colour}]{phase:<28}[/{colour}]{dur}{tool}{model}")


def _phase_colour(phase: str) -> str:
    if "error" in phase:
        return "red"
    if "thinking" in phase:
        return "magenta"
    if "api" in phase:
        return "cyan"
    if "tool" in phase:
        return "yellow"
    if "file" in phase:
        return "blue"
    return "white"


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
