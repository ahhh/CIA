# Architecture

## Overview

CIA is a passive external monitor. It never modifies Claude or injects into its process. Everything is observed from the outside using three published extension points:

1. **mitmproxy** — intercepts HTTPS to `api.anthropic.com`
2. **Claude Code hooks** — `PreToolUse` / `PostToolUse` / `Stop` events
3. **fswatch** — filesystem change notifications

## Component diagram

```
┌──────────────────────────────────────────────────────────┐
│                      cia daemon                          │
│                                                          │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │ ProxyThread  │  │ HookReceiver │  │   FsWatcher   │  │
│  │ (own thread) │  │ HTTP :7171   │  │  (fswatch sub)│  │
│  │  mitmproxy   │  │              │  │               │  │
│  └──────┬───────┘  └──────┬───────┘  └───────┬───────┘  │
│         │call_soon_ts     │emit              │emit       │
│         └─────────────────┼──────────────────┘           │
│                           ▼                              │
│                  ┌────────────────┐                      │
│                  │  Event Queue   │  (asyncio.Queue)     │
│                  └───────┬────────┘                      │
│                          │ drain loop                    │
│                          ▼                               │
│                  ┌────────────────┐                      │
│                  │     Store      │                      │
│                  │ SQLite + JSONL │                      │
│                  └───────┬────────┘                      │
│                          │                              │
│                  ┌───────▼────────┐                      │
│                  │ SocketServer   │ ← CLI / other tools  │
│                  │ ~/.cia/cia.sock│                      │
│                  └────────────────┘                      │
└──────────────────────────────────────────────────────────┘
```

## Data flow

```
Claude Code
  │
  ├─ HTTPS to api.anthropic.com
  │   └─ routed via HTTPS_PROXY=http://127.0.0.1:8080
  │       └─ ProxyThread (mitmproxy) → CIAAddon → SSEParser
  │           └─ api_request_start, api_thinking_*, api_response_end
  │
  ├─ Tool calls
  │   └─ PreToolUse/PostToolUse/Stop hooks → curl POST to :7171
  │       └─ HookReceiver → tool_call_start/end, session_end
  │
  └─ File I/O in watched dirs
      └─ fswatch subprocess stdout → FsWatcher → file_change

All events → asyncio.Queue → drain loop → Store.add()
                                               ├─ SQLite INSERT
                                               └─ JSONL append
```

## Key design decisions

### mitmproxy in a daemon thread

mitmproxy's `DumpMaster.run()` calls `asyncio.run()` internally, which creates its own event loop. Running it in a daemon thread isolates its event loop from the main asyncio loop. Events bridge back via `loop.call_soon_threadsafe()`.

### Hook receiver as plain asyncio TCP server

Claude Code hook scripts use `curl` to POST to `http://127.0.0.1:7171`. A hand-rolled asyncio TCP server handles these — no HTTP framework dependency. If CIA isn't running, the hook exits silently (`|| true`) and never blocks Claude.

### Single asyncio event loop (main thread)

All non-proxy components — HookReceiver, FsWatcher, SocketServer, drain loop, Store — share the main asyncio event loop. This means:
- No locking needed for the event queue
- No lock needed for Store (aiosqlite serialises writes internally)
- Simple fan-in: any component calls `emit(event)` → `queue.put_nowait()`

### Store: SQLite + JSONL mirror

- SQLite enables queries (by session, phase, time range)
- JSONL enables streaming export and crash-safe append
- Both are updated atomically per event in the drain loop

### Unix socket IPC (no HTTP server)

Removing FastAPI eliminates ~10 transitive dependencies and a full HTTP server. The Unix socket protocol is four commands over newline-delimited JSON. Any language can implement a client in < 20 lines. The `cia` CLI is itself just a thin socket client.

## File layout

```
cia/
  schema.py         Event dataclass, Phase enum
  store.py          SQLite + JSONL storage layer
  sse_parser.py     Anthropic SSE stream parser
  proxy.py          mitmproxy CIAAddon + ProxyThread
  hook_receiver.py  Minimal asyncio HTTP server for hooks
  watcher.py        fswatch subprocess wrapper
  daemon.py         Orchestrator, event bus
  socket_server.py  Unix socket IPC server
  hooks.py          Claude Code hook script installer
  cli.py            click CLI
```

## Phase 2 additions (planned)

- `ProcessMonitor` — polls `ps` to discover new Claude pids automatically
- Multi-session correlation — each pid gets its own `session_id` derived from `pid + start_time`
- `swarm_session_discovered` / `swarm_session_lost` phases
- `GET /sessions` summary with per-session event counts and timelines
