# Event Schema

Every CIA event is a JSON object. The canonical transport is JSONL (newline-delimited JSON), one event per line.

## Fields

| Field | Type | Always present | Description |
|---|---|---|---|
| `phase` | string | yes | Event type (see Phase Taxonomy) |
| `ts` | float | yes | Unix epoch timestamp, millisecond precision |
| `id` | string | yes | Unique event ID (`evt_<16 hex chars>`) |
| `session_id` | string\|null | no | Claude Code session UUID |
| `pid` | int\|null | no | OS process ID of the Claude instance |
| `duration_ms` | float\|null | no | Duration in milliseconds (set on paired `*_end` events) |
| `tool` | string\|null | no | Tool name (tool_call events only) |
| `tool_input` | object\|null | no | Tool input dict (PreToolUse events only) |
| `model` | string\|null | no | Anthropic model ID (API events only) |
| `tokens_input` | int\|null | no | Input token count |
| `tokens_output` | int\|null | no | Output token count |
| `thinking_tokens` | int\|null | no | Estimated thinking tokens (~chars/4), set on `api_thinking_end` (per block) and `api_response_end` (whole turn) |
| `error` | string\|null | no | Error message if applicable |
| `meta` | object | yes | Extensible bag of additional data |

## Phase Taxonomy

### API lifecycle

| Phase | Emitted when | Key fields set |
|---|---|---|
| `api_request_start` | mitmproxy sees the POST to api.anthropic.com (via `message_start` SSE, or the HTTP roundtrip for non-streaming calls) | `model`, `tokens_input` |
| `api_thinking_start` | First thinking SSE block opens | `model` |
| `api_thinking_end` | Thinking SSE block closes (or is cut off at stream end) | `model`, `duration_ms`, `thinking_tokens` |
| `api_generation_start` | A text or tool_use SSE block opens | `model` |
| `api_response_end` | `message_stop` SSE event received, or non-streaming response body received | `model`, `tokens_input`, `tokens_output`, `duration_ms` |
| `api_request_error` | HTTP error response (4xx/5xx) | `error` |
| `api_progress` | Every ~5s while a stream is in flight — the spinner's data | `model`, `duration_ms` (elapsed), `meta.state` (thinking/responding/waiting), `meta.est_output_tokens` |

`duration_ms` on `api_thinking_end` = wall-clock time from thinking block start to stop.  
`duration_ms` on `api_response_end` = wall-clock time from HTTP request sent to `message_stop` received (streaming) or full response received (non-streaming).

#### Thinking instrumentation

`api_thinking_end` carries, in `meta`:

| Field | Description |
|---|---|
| `thinking_chars` / `est_thinking_tokens` | Streamed reasoning size (tokens ≈ chars/4) |
| `thinking_tokens_per_sec` | Reasoning throughput for the block |
| `signed` | Block carried a completion signature (`signature_delta`) — i.e. reasoning finished cleanly |
| `interrupted` | Block never closed before stream end (usually `stop_reason: max_tokens`); also sets the top-level `error` to `thinking_interrupted` |
| `thinking_sample` / `thinking_sample_truncated` | Truncated reasoning text — **only present when `CIA_CAPTURE_THINKING=1`** (off by default; bound with `CIA_THINKING_SAMPLE_CHARS`, default 2000) |

`api_generation_start` for a `tool_use` block that immediately follows reasoning carries `meta.thinking_to_tool_ms` — the thinking→tool ("decisiveness") gap.

`api_response_end` carries a `meta.thinking` summary for the whole turn:

| Field | Description |
|---|---|
| `blocks` | Number of thinking blocks |
| `thinking_ms` / `est_thinking_tokens` / `thinking_chars` | Totals across the turn |
| `interrupted` | Any thinking block was cut off |
| `thinking_requested` / `thinking_fired` | Whether thinking was allowed by the request vs. whether the model actually thought (the adaptive-thinking decision) |
| `requested_effort` / `requested_thinking_type` / `requested_budget_tokens` | What the request asked for (from `meta.request`) |
| `budget_utilization` | `est_thinking_tokens / requested_budget_tokens`, when a budget was set |
| `thinking_time_frac` / `thinking_output_frac` | Reasoning's share of wall-clock and of output tokens |

Non-streaming `/v1/messages` calls have no thinking/generation sub-phases; their events carry `meta.streaming: false`. The `ts` on their `api_request_start` is backdated to when the request left the client, matching the streaming behaviour.

#### Request anatomy (`meta.request` on `api_request_start`)

How the request body spends the context window:

| Field | Description |
|---|---|
| `body_chars` | Total request body size in characters |
| `system_chars` | System prompt size |
| `message_count` | Number of messages in the conversation |
| `tool_count` | Number of tool definitions offered |
| `tools_chars` | Total size of tool definitions (JSON) |
| `max_tokens` | Requested output cap |
| `thinking_type` | `thinking.type` from the request (`adaptive` / `enabled` / `disabled`) |
| `thinking_budget_tokens` | Extended-thinking budget, if enabled (legacy models) |
| `effort` | `output_config.effort` (`low`/`medium`/`high`/`xhigh`/`max`), if set |
| `stream` | Whether the request asked for SSE streaming |

### Tokenizer

Claude Code counts tokens server-side via `POST /v1/messages/count_tokens`; CIA times the roundtrip.

| Phase | Emitted when | Key fields set |
|---|---|---|
| `tokenizer_start` | count_tokens request leaves the client | `model` |
| `tokenizer_end` | count_tokens response received | `model`, `tokens_input` (the counted tokens), `duration_ms` |

### Tool calls

| Phase | Emitted when | Key fields set |
|---|---|---|
| `tool_call_start` | Claude Code `PreToolUse` hook fires | `tool`, `tool_input`, `session_id` |
| `tool_call_end` | Claude Code `PostToolUse` hook fires | `tool`, `session_id` |
| `tool_call_error` | PostToolUse with `is_error: true` in tool_response | `tool`, `error`, `session_id` |

Note: `tool_call_end` events may also set `error` when the tool returned a non-fatal error.

### File system

| Phase | Emitted when | Key fields set |
|---|---|---|
| `file_change` | `fswatch` reports a create/update/delete/rename | `meta.path`, `meta.watch_dir` |

### Claude Code native telemetry (OTLP receiver)

When launched via `cia run`, Claude Code exports its own OpenTelemetry stream to CIA's OTLP receiver (port 4318). These events carry the true `session_id` (from the `session.id` attribute).

| Phase | Emitted when | Key fields set |
|---|---|---|
| `otel_metric` | Claude Code exports a metric data point (e.g. `claude_code.token.usage`, `claude_code.cost.usage`, `claude_code.lines_of_code.count`, `claude_code.commit.count`) | `session_id`, `meta.name`, `meta.value`, `meta.unit`, `meta.attributes` |
| `otel_event` | Claude Code exports a log event (e.g. `api_request`, `api_error`, `tool_result`, `user_prompt`) | `session_id`, `meta.name`, `meta.attributes`, `meta.severity` |

### Session tracking

| Phase | Emitted when | Key fields set |
|---|---|---|
| `session_start` | First event seen from a new process/session | `session_id`, `pid` |
| `session_end` | Claude Code `Stop` hook fires or process exits | `session_id` |

## Example events

```jsonl
{"phase":"api_request_start","ts":1716000000.123,"id":"evt_a1b2c3d4e5f6g7h8","session_id":"abc-123","model":"claude-sonnet-4-6","tokens_input":2048,"meta":{"flow_id":"f1","message_id":"msg_xyz"}}
{"phase":"api_thinking_start","ts":1716000000.456,"id":"evt_b2c3d4e5f6g7h8i9","session_id":"abc-123","model":"claude-sonnet-4-6","meta":{"flow_id":"f1","block_index":0}}
{"phase":"api_thinking_end","ts":1716000005.789,"id":"evt_c3d4e5f6g7h8i9j0","session_id":"abc-123","duration_ms":3333.0,"model":"claude-sonnet-4-6","meta":{"flow_id":"f1"}}
{"phase":"api_generation_start","ts":1716000005.790,"id":"evt_d4e5f6g7h8i9j0k1","session_id":"abc-123","model":"claude-sonnet-4-6","meta":{"flow_id":"f1","block_index":1}}
{"phase":"api_response_end","ts":1716000007.012,"id":"evt_e5f6g7h8i9j0k1l2","session_id":"abc-123","duration_ms":6889.0,"model":"claude-sonnet-4-6","tokens_input":2048,"tokens_output":341,"meta":{"flow_id":"f1"}}
{"phase":"tool_call_start","ts":1716000007.100,"id":"evt_f6g7h8i9j0k1l2m3","session_id":"abc-123","tool":"Bash","tool_input":{"command":"ls -la"},"meta":{}}
{"phase":"tool_call_end","ts":1716000007.250,"id":"evt_g7h8i9j0k1l2m3n4","session_id":"abc-123","tool":"Bash","meta":{}}
```

## Ingesting with DuckDB

```sql
-- Load all events
SELECT * FROM read_ndjson_auto('session.jsonl');

-- API call durations
SELECT model, duration_ms
FROM read_ndjson_auto('session.jsonl')
WHERE phase = 'api_response_end'
ORDER BY ts;

-- Thinking time per session
SELECT session_id, SUM(duration_ms) AS total_thinking_ms
FROM read_ndjson_auto('session.jsonl')
WHERE phase = 'api_thinking_end'
GROUP BY session_id;

-- Tool call latency
SELECT tool,
       AVG(duration_ms) AS avg_ms,
       COUNT(*) AS calls
FROM (
  SELECT s.tool, (e.ts - s.ts)*1000 AS duration_ms
  FROM read_ndjson_auto('session.jsonl') s
  JOIN read_ndjson_auto('session.jsonl') e
    ON s.session_id = e.session_id
    AND s.tool = e.tool
    AND s.phase = 'tool_call_start'
    AND e.phase = 'tool_call_end'
    AND e.ts > s.ts
)
GROUP BY tool;
```
