# Development Guide

## Setup

```bash
git clone https://github.com/yourname/cia
cd cia
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Running tests

```bash
pytest                      # all 54 tests
pytest tests/unit/          # unit tests only (no daemon, ~0.1s)
pytest tests/integration/   # integration tests (spins up real daemon)
pytest -v -k test_sse        # filter by name
pytest --tb=short            # shorter tracebacks
```

All tests are fully self-contained. Integration tests start and stop their own daemon instances. No network access required.

**Note on socket path length:** macOS limits Unix socket paths to 104 bytes. Integration tests always use `/tmp/cia_<hex>.sock` rather than pytest's `tmp_path` to avoid this limit.

## Adding a new event phase

1. Add the phase to `Phase` enum in `cia/schema.py`
2. Emit it from the appropriate component:
   - API events → `cia/sse_parser.py`
   - Tool events → `cia/hook_receiver.py` (adjust `_ROUTE_PHASE`)
   - File events → `cia/watcher.py`
3. Update `docs/event-schema.md` with the new phase description
4. Add unit tests for the new emission path

## Project structure

```
cia/           Package source
tests/
  unit/        Fast, no I/O: schema, store, SSE parser
  integration/ Real asyncio servers: daemon, hook receiver
  integration/fake_anthropic.py  — canned SSE server for proxy tests
docs/          Markdown documentation
pyproject.toml Build config, dependencies, pytest config
```

## Dependency philosophy

Runtime dependencies are kept minimal:
- `mitmproxy` — HTTPS interception (large but unavoidable)
- `aiosqlite` — async SQLite
- `click` — CLI
- `rich` — terminal output

No HTTP framework (FastAPI, aiohttp, Flask) is used at runtime. The hook receiver and Unix socket server are hand-rolled asyncio code.

## Release checklist

- [ ] All tests pass: `pytest -q`
- [ ] Version bumped in `pyproject.toml` and `cia/__init__.py`
- [ ] `CHANGELOG.md` updated
- [ ] `docs/event-schema.md` reflects any new/changed phases
- [ ] `git tag v0.x.y && git push --tags`
