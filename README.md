# CIA — Claude Instrumentation & Analysis

External, passive monitor for Claude Code sessions. Observes API call latency, thinking phases, tool call timing, and file I/O without modifying Claude.

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

## Powerful debugging

CIA turns an otherwise opaque Claude Code session into something you can step through, time, and explain. Every signal below is captured passively — nothing about Claude's behavior changes while you watch.

**Live event feed with full forensic annotations.** `cia tail` streams every event the moment it happens, each line annotated with the numbers that actually matter for debugging — TTFB, thinking tokens, cache hits, the thinking→tool decision gap, network status codes, file deltas:

```text
14:22:01.840  user_prompt                            prompt='refactor the auth module'
14:22:02.110  api_request_start             model=claude-sonnet-4-6
14:22:02.470  api_thinking_end       [2310ms]  model=claude-sonnet-4-6  ~1240tok
14:22:04.980  api_response_end       [6870ms]  model=claude-sonnet-4-6  in=48210 out=1530 \
                                     think~1240tok (38% of out) budget=72% 84tok/s \
                                     cache_r=46000 stop=tool_use ttft=512ms
14:22:05.020  tool_call_start                  tool=Edit  path=/src/auth.py
14:22:05.430  tool_call_end          [412ms]   tool=Edit  path=/src/auth.py 1024b
14:22:05.450  file_change                      [memory] …/auth.py (modified) +84b
14:22:06.120  network_request                  GET statsig.anthropic.com/... 200 [telemetry] — feature flag check
```

**Why each request happened — not just that it happened.** Every non-inference call Claude Code makes (boot connectivity probe, managed-settings fetch, Statsig flags, Sentry crash reports, update checks) is tagged with a category *and a plain-English reason*, so a stall during a turn is traceable to the exact background request that caused it.

**See what Claude wrote to its own files.** File changes in `~/.claude` are captured as content deltas: appended transcript records parsed into previews, memory/settings edits rendered as capped unified diffs. You can watch Claude edit its own memory in real time.

**Cache and thinking forensics.** Each cache rebuild is attributed to a cause — a compaction, a 5-minute TTL expiry (with the idle gap that triggered it), or a prompt change — and priced in retokenized input. Each thinking block records whether it finished cleanly or was cut off by `max_tokens`, and how decisively Claude moved from thinking to its next tool call.

**A full derived report.** When the session ends, `cia report` reconstructs where the wall-clock went — see [Derived analytics](#derived-analytics--cia-report) below.

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
| Claude Code native telemetry: cost, tokens, lines of code, commits, active time, plus every telemetry event — tool permission decisions, API errors/retries/refusals, hook executions, MCP connections, compactions, permission-mode changes (`otel_metric` / `otel_event`) | Built-in OTLP receiver on :4318 |
| Claude Code's beta tracing spans — its own internal timing structure (`otel_span`) | OTLP receiver, `cia run --trace` |
| On-disk session transcripts (exact per-message token usage & models, tool/skill/agent names, session titles, subagent sub-transcripts) and /insights usage-data (outcome, lines of code, commits) | Read at report time from `~/.claude/projects` / `~/.claude/usage-data` — retroactive, works even for sessions CIA never watched live (`--no-transcripts` to skip) |
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
| Session stories | Per-session rollup (turns, tokens, thinking, tool calls, human wait) with the session's own title/project/outcome from its transcript, plus coverage diagnostics that say *why* a field is blank (e.g. session not proxied). Unproxied sessions get their token/model columns filled from the transcript (marked †) |
| Turn anatomy | Each turn's wall-clock split into API time, thinking, tool execution, permission waits, and everything else. Turns still open at capture end are kept and marked `*` |
| Tool profiles | Per-tool duration percentiles (p50/p90/p99), error rates, output sizes |
| Human latency | Time spent waiting on permission prompts and user input vs Claude actually working |
| Compaction cost | Context tokens reclaimed by each compaction |
| Rework | Files edited repeatedly in a single turn (thrash signal) |
| Cache economics | Prompt-cache hit rate, warm vs cold TTFB (what cache warmth is worth in latency), and bust forensics — each cache rebuild attributed to a compaction, a 5-minute TTL expiry (with the idle gap that caused it), or a prompt change, plus the retokenized input it cost |
| Thinking calibration | Adaptive-thinking fire rate (requested vs fired), budget utilization, thinking→tool decisiveness percentiles per model, and turns above vs below median thinking compared on downstream tool errors / re-edits |
| Context pressure | Context growth per turn, which tools feed the context fastest, and projected turns until the next compaction |
| Tool chains | Tool→tool transition patterns, retry loops (same tool + same target back to back), search thrash (Grep/Glob churn before the first Read), and time/calls to recover after tool errors |
| Cost attribution | Claude Code's native cost/token/LoC telemetry joined onto turns: cost per turn, cost of rework turns, cost per commit and per line added |
| Throughput | tok/s and TTFB/TTFT percentiles per model, hour-of-day variance, slowest requests, and in-response speed sag computed from `api_progress` ticks |
| Network overhead | Non-inference traffic share by category (Statsig, Sentry, update checks…), failure rates, and whether failures landed while an inference call was in flight |
| Permission economics | Tool permission decisions from native telemetry: accept/reject counts, how many were free (auto-approved by config/hooks) vs. interruptions, rejections by tool |
| API reliability | Native `api_error` / `api_retries_exhausted` / `api_refusal` telemetry: errors by status code, time lost to retries, refusals (with category under `--detail`) |
| Cost by subsystem | Native per-request cost/token telemetry split by `query_source` (main thread vs subagent vs compaction) and attributed to agents, skills, MCP servers and plugins |
| Hook overhead | What each hook costs the session (runs, total/p50/max ms, blocking count) — including CIA's own instrumentation hooks, i.e. the cost of observing |
| MCP connections & client health | MCP server connect times and failures; Claude Code internal errors and auth failures; session starts by type (fresh/resume/continue) |
| Subagent economics | Token/tool spend per subagent type, from the subagent sub-transcripts each session leaves on disk |
| Delivery & source agreement | /insights delivery stats (lines added/removed, files, commits, outcome) per session, and a three-way output-token cross-check — transcript vs proxy vs native telemetry — that flags sessions where the sources disagree by >5% |

`api_request_start` events also carry the request anatomy (system prompt size, message count, tool definition size, thinking type/budget, effort) in `meta.request`.

### Managing report data

The report is computed over everything in the event store, which accumulates across runs. Two flags manage that store directly:

```bash
# Snapshot all report data (SQLite + JSONL) before a risky run.
# Default destination is ~/.cia/backups/<timestamp>/.
cia report --backup
cia report --backup ./before-refactor      # or an explicit directory

# Wipe the store so the next report starts from empty (prompts to confirm).
cia report --reset
cia report --reset --yes                    # skip the confirmation

# Back up first, then reset — keep a copy of what you're clearing.
cia report --backup --reset
```

The backup uses SQLite's online backup API, so it is safe to run while the daemon is recording. Both flags talk to the running daemon over the IPC socket.

### Thinking instrumentation

Each `api_thinking_end` records estimated thinking tokens, whether the block was **signed** (reasoning finished cleanly) or **interrupted** (cut off, usually by `max_tokens`), and the thinking→tool gap ("decisiveness") on the following tool call. Each `api_response_end` adds a `meta.thinking` summary correlating what the request asked for (`effort` / thinking type / budget) against what actually fired — including `thinking_requested` vs `thinking_fired` (the adaptive-thinking decision) and `budget_utilization`. See [docs/event-schema.md](docs/event-schema.md#thinking-instrumentation).

To also persist a **truncated sample of the reasoning text** (off by default — it can be large and sensitive), start the daemon with `CIA_CAPTURE_THINKING=1` (bound the sample with `CIA_THINKING_SAMPLE_CHARS`, default 2000):

```bash
CIA_CAPTURE_THINKING=1 cia start
```

Session attribution: proxy events carry no session ID of their own, but when native telemetry is captured (`cia run`) CIA joins them to sessions **exactly** via the Anthropic `request-id` — recorded by the proxy from the response header and by Claude Code's `api_request` telemetry alongside `session.id`. Without that join (telemetry off), turn anatomy falls back to time-window matching — exact for single-session captures, approximate when multiple proxied sessions run concurrently.

## CLI reference

```
cia start [--proxy-port 8080] [--hook-port 7171] [--otlp-port 4318]
          [--db PATH] [--jsonl PATH] [--watch-dir DIR] [--foreground]
cia run [--proxy-port 8080] [--otlp-port 4318] [--detail] [--trace]
        [COMMAND...]                                          # default: claude
        # --detail: unredact tool params / errors / MCP names in native
        #           telemetry (OTEL_LOG_TOOL_DETAILS=1)
        # --trace : Claude Code's beta span tracing → otel_span events
cia stop
cia status
cia export [--format jsonl|csv] [--session ID] [--since EPOCH] [-o FILE]
cia report [--session ID] [--since EPOCH] [--input FILE.jsonl] [--json]
           [--backup [DIR]] [--reset] [--yes] [--no-transcripts]
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
echo '{"cmd":"backup","dir":"/tmp/cia-snap"}' | nc -U ~/.cia/cia.sock
echo '{"cmd":"stop"}' | nc -U ~/.cia/cia.sock
```

See [docs/socket-api.md](docs/socket-api.md) for the full command reference.

## Development

```bash
pip install -e ".[dev]"
pytest          # 54 tests, no network required
```

See [docs/development.md](docs/development.md).
</content>
</invoke>
