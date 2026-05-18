import asyncio
import pytest
import tempfile
from pathlib import Path

from cia.schema import Event, Phase
from cia.store import Store


@pytest.fixture
async def store(tmp_path):
    s = Store(tmp_path / "test.db", tmp_path / "events.jsonl")
    await s.open()
    yield s
    await s.close()


async def _add_events(store: Store, phases: list[Phase], session: str = "s1") -> list[Event]:
    events = []
    for i, phase in enumerate(phases):
        e = Event(phase=phase, session_id=session, ts=float(1000 + i))
        await store.add(e)
        events.append(e)
    return events


class TestLifecycle:
    async def test_open_creates_db(self, tmp_path):
        s = Store(tmp_path / "sub" / "test.db")
        await s.open()
        assert (tmp_path / "sub" / "test.db").exists()
        await s.close()

    async def test_double_add_ignored(self, store):
        e = Event(phase=Phase.SESSION_START, id="dup_id")
        await store.add(e)
        await store.add(e)
        assert await store.count() == 1


class TestAddAndQuery:
    async def test_count(self, store):
        await _add_events(store, [Phase.API_REQUEST_START, Phase.API_RESPONSE_END])
        assert await store.count() == 2

    async def test_query_all(self, store):
        events = await _add_events(store, [Phase.TOOL_CALL_START, Phase.TOOL_CALL_END])
        results = await store.query()
        assert len(results) == 2

    async def test_query_by_session(self, store):
        await _add_events(store, [Phase.SESSION_START], session="alpha")
        await _add_events(store, [Phase.SESSION_START], session="beta")
        results = await store.query(session_id="alpha")
        assert len(results) == 1
        assert results[0].session_id == "alpha"

    async def test_query_by_phase(self, store):
        await _add_events(store, [Phase.API_REQUEST_START, Phase.FILE_CHANGE])
        results = await store.query(phase=Phase.FILE_CHANGE)
        assert len(results) == 1
        assert results[0].phase is Phase.FILE_CHANGE

    async def test_query_since(self, store):
        await _add_events(store, [Phase.SESSION_START, Phase.SESSION_END])
        results = await store.query(since=1001.0)
        assert len(results) == 1
        assert results[0].ts == 1001.0

    async def test_query_ordered_by_ts(self, store):
        await _add_events(store, [Phase.API_REQUEST_START, Phase.API_RESPONSE_END])
        results = await store.query()
        assert results[0].ts < results[1].ts

    async def test_sessions(self, store):
        await _add_events(store, [Phase.SESSION_START], session="x")
        await _add_events(store, [Phase.SESSION_START], session="y")
        sessions = await store.sessions()
        assert sorted(sessions) == ["x", "y"]


class TestClear:
    async def test_clear(self, store):
        await _add_events(store, [Phase.SESSION_START])
        await store.clear()
        assert await store.count() == 0


class TestExport:
    async def test_export_jsonl(self, store):
        import json
        await _add_events(store, [Phase.API_REQUEST_START, Phase.API_RESPONSE_END])
        output = await store.export_jsonl()
        lines = [l for l in output.strip().splitlines() if l]
        assert len(lines) == 2
        parsed = json.loads(lines[0])
        assert "phase" in parsed

    async def test_export_csv(self, store):
        await _add_events(store, [Phase.TOOL_CALL_START])
        output = await store.export_csv()
        lines = output.strip().splitlines()
        assert lines[0].startswith("phase,ts")  # header
        assert len(lines) == 2              # header + 1 data row

    async def test_export_empty(self, store):
        assert await store.export_jsonl() == ""
        assert await store.export_csv() == ""

    async def test_jsonl_mirror(self, tmp_path):
        import json
        jsonl_path = tmp_path / "mirror.jsonl"
        s = Store(tmp_path / "test.db", jsonl_path)
        await s.open()
        e = Event(phase=Phase.SESSION_START, session_id="abc")
        await s.add(e)
        await s.close()
        lines = jsonl_path.read_text().strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["session_id"] == "abc"
