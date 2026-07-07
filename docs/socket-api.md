# Unix Socket API

The CIA daemon exposes a Unix domain socket at `~/.cia/cia.sock`.

**Protocol:** Newline-delimited JSON. Send one JSON command line, receive one JSON response line.

## Using from the shell

```bash
echo '{"cmd":"status"}' | nc -U ~/.cia/cia.sock
```

Or with Python:

```python
import socket, json

def cia(cmd: dict) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(os.path.expanduser("~/.cia/cia.sock"))
    sock.sendall(json.dumps(cmd).encode() + b"\n")
    return json.loads(sock.recv(1_048_576).decode())
```

## Commands

### `status`

```json
{"cmd": "status"}
```

Response:
```json
{
  "ok": true,
  "running": true,
  "events": 1423,
  "sessions": ["abc-123", "def-456"]
}
```

---

### `sessions`

```json
{"cmd": "sessions"}
```

Response:
```json
{"ok": true, "sessions": ["abc-123", "def-456"]}
```

---

### `export`

```json
{
  "cmd": "export",
  "format": "jsonl",
  "session_id": "abc-123",
  "since": 1716000000.0,
  "until": 1716100000.0,
  "since_seq": 4210
}
```

All filter fields are optional. `format` defaults to `"jsonl"`; also accepts `"csv"`.

`since`/`until` filter by event timestamp (ordered by `ts`). `since_seq`
instead pages by **store insert order** (SQLite rowid, exposed as `seq` on
each exported event) and returns events in commit order — use it for live
tailing, where a timestamp cursor would permanently skip events that are
committed late with earlier timestamps (OTLP batches, proxy flow completions).

Response:
```json
{"ok": true, "data": "...JSONL or CSV string...", "max_seq": 4223}
```

`max_seq` is the highest insert seq in the store at query time — seed a
`since_seq` cursor with it when the first poll returns no events.

---

### `clear`

Deletes all events from the store.

```json
{"cmd": "clear"}
```

Response:
```json
{"ok": true, "cleared": true}
```

---

### `backup`

Snapshots all report data into `dir` and returns where it landed. The SQLite
copy is taken with the online backup API, so it is consistent even while the
daemon keeps recording; the JSONL mirror is copied alongside it.

```json
{"cmd": "backup", "dir": "/path/to/dest"}
```

Response:
```json
{
  "ok": true,
  "dir": "/path/to/dest",
  "db": "/path/to/dest/cia.db",
  "jsonl": "/path/to/dest/events.jsonl",
  "events": 1423
}
```

`jsonl` is omitted when the daemon was started without a JSONL mirror. Returns
`{"ok": false, "error": "backup requires a 'dir'"}` if `dir` is missing.

Exposed on the CLI as `cia report --backup [DIR]`.

---

### `stop`

Stops the daemon gracefully.

```json
{"cmd": "stop"}
```

Response:
```json
{"ok": true, "stopped": true}
```

---

## Error responses

All commands respond with `{"ok": false, "error": "..."}` on failure.

## Integration example (Python)

```python
import socket, json, os
from pathlib import Path

SOCKET = Path.home() / ".cia" / "cia.sock"

def cia_send(cmd: dict) -> dict:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(10.0)
    try:
        sock.connect(str(SOCKET))
        sock.sendall(json.dumps(cmd).encode() + b"\n")
        buf = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
            if buf.endswith(b"\n"):
                break
        return json.loads(buf)
    finally:
        sock.close()

# Examples
status  = cia_send({"cmd": "status"})
events  = cia_send({"cmd": "export", "format": "jsonl"})
session = cia_send({"cmd": "export", "session_id": "abc-123"})
cia_send({"cmd": "stop"})
```
