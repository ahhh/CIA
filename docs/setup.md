# Setup Guide

## Prerequisites

- macOS (Phase 1; Linux support planned for Phase 2)
- Python 3.11+
- `fswatch` for file I/O monitoring: `brew install fswatch`
- `curl` for hook scripts (pre-installed on macOS)

## Install

```bash
git clone https://github.com/yourname/cia
cd cia
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

Verify: `cia --help`

## Proxy setup (mitmproxy CA certificate)

CIA intercepts Claude Code's HTTPS calls to `api.anthropic.com` via mitmproxy.
Claude Code is a Node.js app and won't trust the proxy by default.
One-time setup:

**Step 1 — Generate the CA** (happens automatically on first `cia start`):

```bash
cia start --foreground   # let it start, then Ctrl-C
# ~/.mitmproxy/mitmproxy-ca-cert.pem now exists
```

**Step 2 — Trust the CA on macOS:**

```bash
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain \
  ~/.mitmproxy/mitmproxy-ca-cert.pem
```

**Step 3 — Tell Node.js to use it:**

Add to your shell profile (`~/.zshrc` or `~/.bash_profile`):

```bash
export HTTPS_PROXY=http://127.0.0.1:8080
export NODE_EXTRA_CA_CERTS="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"
```

Then reload: `source ~/.zshrc`

From this point on, any Claude Code session started in that shell will be monitored automatically (as long as `cia start` was run first).

The `cia trust-cert` command prints these instructions for reference.

## Hook setup

Hooks tell CIA exactly when each tool call starts and ends. Install once per project:

```bash
cd /your/project
cia install-hooks           # writes to ./.claude/settings.json
```

Or globally (monitors all projects):

```bash
cia install-hooks --global  # writes to ~/.claude/settings.json
```

This writes one shell script per hook to `~/.cia/hooks/` and registers them
as `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
`Notification`, `PreCompact`, `SubagentStop`, `Stop`, and `SessionEnd` hooks.
Each script runs `curl` to POST the hook payload to
`http://127.0.0.1:7171/hook/<type>`. Note that `Stop` marks the end of each
assistant *turn*; `SessionEnd` marks actual session termination.

If CIA is not running, the hooks exit silently (`|| true`) so they never
block Claude.

To undo: `cia uninstall-hooks [--global]`

## Running a monitored session

```bash
cia start                   # starts daemon in background
claude                      # (with HTTPS_PROXY set from your shell profile)
```

Watch events as they happen:

```bash
cia tail
```

## Exporting data

```bash
cia export --format jsonl > run1.jsonl    # all events
cia export --format jsonl --session abc-123 > session.jsonl  # one session
cia export --format csv > run1.csv        # CSV for spreadsheets
```

The daemon accumulates events across runs in `~/.cia/cia.db`. To reset:

```bash
echo '{"cmd":"clear"}' | nc -U ~/.cia/cia.sock
```

## Stopping

```bash
cia stop
```

The daemon flushes any queued events to the store before exiting.

## File layout

```
~/.cia/
  cia.db          — SQLite event store (persists across runs)
  events.jsonl    — append-only JSONL mirror
  cia.sock        — Unix domain socket (present only while running)
  cia.pid         — daemon PID file
  cia.log         — daemon stdout/stderr
  hooks/
    cia_pre_tool.sh
    cia_post_tool.sh
    cia_stop.sh
~/.mitmproxy/
  mitmproxy-ca-cert.pem   — CA cert to trust
```
