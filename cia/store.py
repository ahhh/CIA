from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import AsyncIterator, Optional

import aiosqlite

from cia.schema import Event, Phase

_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS events (
    id              TEXT PRIMARY KEY,
    ts              REAL NOT NULL,
    session_id      TEXT,
    pid             INTEGER,
    phase           TEXT NOT NULL,
    duration_ms     REAL,
    tool            TEXT,
    tool_input      TEXT,
    model           TEXT,
    tokens_input    INTEGER,
    tokens_output   INTEGER,
    thinking_tokens INTEGER,
    error           TEXT,
    meta            TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts      ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_phase   ON events(phase);
"""

_INSERT_SQL = """
INSERT OR IGNORE INTO events
    (id, ts, session_id, pid, phase, duration_ms, tool, tool_input,
     model, tokens_input, tokens_output, thinking_tokens, error, meta)
VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


class Store:
    def __init__(
        self,
        db_path: Path,
        jsonl_path: Optional[Path] = None,
    ) -> None:
        self.db_path = db_path
        self.jsonl_path = jsonl_path
        self._db: Optional[aiosqlite.Connection] = None
        self._jsonl_fh = None

    # ------------------------------------------------------------------ #
    # Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    async def open(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.executescript(_CREATE_DDL)
        await self._db.commit()
        if self.jsonl_path:
            self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
            self._jsonl_fh = open(self.jsonl_path, "a", buffering=1)

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None
        if self._jsonl_fh:
            self._jsonl_fh.close()
            self._jsonl_fh = None

    # ------------------------------------------------------------------ #
    # Write                                                                #
    # ------------------------------------------------------------------ #

    async def add(self, event: Event) -> None:
        d = event.to_dict()
        await self._db.execute(
            _INSERT_SQL,
            (
                d["id"], d["ts"], d["session_id"], d["pid"], d["phase"],
                d["duration_ms"], d["tool"],
                json.dumps(d["tool_input"]) if d["tool_input"] is not None else None,
                d["model"], d["tokens_input"], d["tokens_output"],
                d["thinking_tokens"], d["error"],
                json.dumps(d["meta"]) if d["meta"] else None,
            ),
        )
        await self._db.commit()
        if self._jsonl_fh:
            self._jsonl_fh.write(event.to_json() + "\n")

    async def clear(self) -> None:
        await self._db.execute("DELETE FROM events")
        await self._db.commit()

    # ------------------------------------------------------------------ #
    # Read                                                                 #
    # ------------------------------------------------------------------ #

    async def count(self) -> int:
        async with self._db.execute("SELECT COUNT(*) FROM events") as cur:
            row = await cur.fetchone()
            return row[0]

    async def sessions(self) -> list[str]:
        async with self._db.execute(
            "SELECT DISTINCT session_id FROM events WHERE session_id IS NOT NULL ORDER BY session_id"
        ) as cur:
            return [r[0] for r in await cur.fetchall()]

    async def query(
        self,
        session_id: Optional[str] = None,
        phase: Optional[Phase] = None,
        since: Optional[float] = None,
        until: Optional[float] = None,
        limit: int = 10_000,
    ) -> list[Event]:
        clauses: list[str] = []
        params: list = []
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if phase:
            clauses.append("phase = ?")
            params.append(phase.value)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        if until is not None:
            clauses.append("ts <= ?")
            params.append(until)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)

        async with self._db.execute(
            f"SELECT id,ts,session_id,pid,phase,duration_ms,tool,tool_input,"
            f"model,tokens_input,tokens_output,thinking_tokens,error,meta "
            f"FROM events {where} ORDER BY ts ASC LIMIT ?",
            params,
        ) as cur:
            rows = await cur.fetchall()

        return [_row_to_event(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Export                                                               #
    # ------------------------------------------------------------------ #

    async def export_jsonl(self, **kwargs) -> str:
        events = await self.query(**kwargs)
        return "\n".join(e.to_json() for e in events) + ("\n" if events else "")

    async def export_csv(self, **kwargs) -> str:
        events = await self.query(**kwargs)
        buf = io.StringIO()
        if not events:
            return ""
        dicts = [e.to_dict() for e in events]
        writer = csv.DictWriter(buf, fieldnames=list(dicts[0].keys()))
        writer.writeheader()
        writer.writerows(dicts)
        return buf.getvalue()


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #

def _row_to_event(row) -> Event:
    return Event.from_dict(
        {
            "id":              row[0],
            "ts":              row[1],
            "session_id":      row[2],
            "pid":             row[3],
            "phase":           row[4],
            "duration_ms":     row[5],
            "tool":            row[6],
            "tool_input":      json.loads(row[7]) if row[7] else None,
            "model":           row[8],
            "tokens_input":    row[9],
            "tokens_output":   row[10],
            "thinking_tokens": row[11],
            "error":           row[12],
            "meta":            json.loads(row[13]) if row[13] else {},
        }
    )
