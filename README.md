# CIA — Claude Instrumentation & Analysis

External, passive monitor for Claude Code sessions. Observes API call latency, thinking phases, tool call timing, and file I/O without modifying Claude.

## What it captures

| Signal | How |
|---|---|
| Anthropic API request + response timing (streaming and non-streaming) | mitmproxy intercepts HTTPS |
| Tokenizer runs (`count_tokens` calls): when + duration + counted tokens | mitmproxy intercepts HTTPS |
| Every other network check-in Claude Code makes — boot connectivity probe, managed-settings fetch, account/profile sync, Statsig feature flags & telemetry, Sentry crash reports, update checks — each tagged with a category and *why* the request happened (`network_request`) | mitmproxy intercepts HTTPS (all hosts) |
| Latency breakdown (TTFB, time-to-first-token, generation, tokens/sec) | SSE stream parsing |
| Model thinking phase start/end (+ estimated thinking tokens & share of output) | SSE stream parsing |
| Text generation start/end + cache token usage | SSE stream parsing |
| User prompt submitted (+ prompt text) | Claude Code UserPromptSubmit hook |
| Session start / end (+ source / reason) | Claude Code SessionStart / SessionEnd hooks |
| Assistant turn end | Claude Code Stop hook |
| Subagent finished | Claude Code SubagentStop hook |
| Context compaction | Claude Code PreCompact hook |
| Claude waiting / permission prompts | Claude Code Notification hook |
| Tool call start/end/error (+ output size, + target path / command / pattern) | Claude Code PreToolUse/PostToolUse hooks |
| In-flight stream progress (elapsed · ~tokens · thinking/responding — the spinner's data) | SSE stream parsing (`api_progress` every 5s) |
| Claude Code native telemetry: cost, token usage, lines of code, commits (`otel_metric` / `otel_event`) | Built-in OTLP receiver on :4318 |
| File I/O in watched dirs | `fswatch` subprocess |
| Claude's own memory / session / transcript writes (categorised `file_change` events), with content deltas: appended transcript records parsed into previews, memory/settings edits as capped unified diffs (`meta.change`) | `fswatch` on `~/.claude/projects/<project>/` + `tasks` (on by default; `--no-watch-claude` to disable) |

## Output format

All events are JSONL (one JSON object per line). SQLite is kept in sync as a queryable mirror.

```jsonc
{
  "phase": "api_thinking_end",
  "ts": 1716000000.123,
  "id": "evt_a1b2c3d4e5f6g7h8",
  "session_id": "abc-123",
  "duration_ms": 4821.3,
  "model": "claude-sonnet-4-6",
  "tokens_input": 2048,
  "tokens_output": 512
}
```

See [docs/event-schema.md](docs/event-schema.md) for the full field reference.

## Derived analytics — `cia report`

`cia report` computes performance metrics from the recorded events:

| Section | What it shows |
|---|---|
| Session stories | Per-session rollup (turns, tokens, thinking, tool calls, human wait) plus coverage diagnostics that say *why* a field is blank (e.g. session not proxied) |
| Turn anatomy | Each turn's wall-clock split into API time, thinking, tool execution, permission waits, and everything else. Turns still open at capture end are kept and marked `*` |
| Tool profiles | Per-tool duration percentiles (p50/p90/p99), error rates, output sizes |
| Human latency | Time spent waiting on permission prompts and user input vs Claude actually working |
| Compaction cost | Context tokens reclaimed by each compaction |
| Rework | Files edited repeatedly in a single turn (thrash signal) |

`api_request_start` events also carry the request anatomy (system prompt size, message count, tool definition size, thinking type/budget, effort) in `meta.request`.

### Thinking instrumentation

Each `api_thinking_end` records estimated thinking tokens, whether the block was **signed** (reasoning finished cleanly) or **interrupted** (cut off, usually by `max_tokens`), and the thinking→tool gap ("decisiveness") on the following tool call. Each `api_response_end` adds a `meta.thinking` summary correlating what the request asked for (`effort` / thinking type / budget) against what actually fired — including `thinking_requested` vs `thinking_fired` (the adaptive-thinking decision) and `budget_utilization`. See [docs/event-schema.md](docs/event-schema.md#thinking-instrumentation).

To also persist a **truncated sample of the reasoning text** (off by default — it can be large and sensitive), start the daemon with `CIA_CAPTURE_THINKING=1` (bound the sample with `CIA_THINKING_SAMPLE_CHARS`, default 2000):

```bash
CIA_CAPTURE_THINKING=1 cia start
```

Note: proxy events carry no session ID, so turn anatomy attributes API events to turns by time window — exact for single-session captures, approximate when multiple proxied sessions run concurrently.

## Quick start

```bash
# Install
git clone https://github.com/yourname/cia
cd cia
python3 -m venv .venv && source .venv/bin/activate
pip install -e .

# Start daemon
cia start

# Trust mitmproxy CA cert (first time only — follow printed instructions)
cia trust-cert

# Install Claude Code hooks into current project
cia install-hooks

# Run Claude fully wired into CIA (proxy + native telemetry).
# This is the step people forget — without it you get hook events only,
# no API timing, no thinking phases, no tokenizer.
cia run claude

# Watch events live
cia tail

# Export when done
cia export --format jsonl > session.jsonl
cia stop

# Run again — events accumulate in the same DB across runs
cia start
```

## CLI reference

```
cia start [--proxy-port 8080] [--hook-port 7171] [--otlp-port 4318]
          [--db PATH] [--jsonl PATH] [--watch-dir DIR] [--foreground]
cia run [--proxy-port 8080] [--otlp-port 4318] [COMMAND...]   # default: claude
cia stop
cia status
cia export [--format jsonl|csv] [--session ID] [--since EPOCH] [-o FILE]
cia report [--session ID] [--since EPOCH] [--input FILE.jsonl] [--json]
cia tail [--interval 1.0]
cia install-hooks [--global]
cia uninstall-hooks [--global]
cia trust-cert
```

## IPC — Unix socket

The daemon exposes `~/.cia/cia.sock`. Any process can control it with newline-delimited JSON:

```bash
echo '{"cmd":"status"}' | nc -U ~/.cia/cia.sock
echo '{"cmd":"export","format":"jsonl"}' | nc -U ~/.cia/cia.sock
echo '{"cmd":"stop"}' | nc -U ~/.cia/cia.sock
```

See [docs/socket-api.md](docs/socket-api.md) for the full command reference.

## Development

```bash
pip install -e ".[dev]"
pytest          # 54 tests, no network required
```

See [docs/development.md](docs/development.md).
