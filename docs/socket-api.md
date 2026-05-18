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
  "until": 1716100000.0
}
```

All filter fields are optional. `format` defaults to `"jsonl"`; also accepts `"csv"`.

Response:
```json
{"ok": true, "data": "...JSONL or CSV string..."}
```

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
