from cia.schema import Event, Phase


def _ev(tool, tool_input):
    return Event.from_hook_payload(
        Phase.TOOL_CALL_START,
        {"session_id": "s1", "tool_name": tool, "tool_input": tool_input},
    )


def test_read_surfaces_path():
    assert _ev("Read", {"file_path": "/a/b.py"}).meta["path"] == "/a/b.py"


def test_write_surfaces_path():
    assert _ev("Write", {"file_path": "/a/c.py", "content": "x"}).meta["path"] == "/a/c.py"


def test_notebook_path():
    assert _ev("NotebookEdit", {"notebook_path": "/n.ipynb"}).meta["path"] == "/n.ipynb"


def test_bash_command():
    m = _ev("Bash", {"command": "ls -la"}).meta
    assert m["command"] == "ls -la"
    assert "path" not in m


def test_grep_pattern():
    assert _ev("Grep", {"pattern": "foo", "path": "/x"}).meta["pattern"] == "foo"


def test_web_target():
    assert _ev("WebFetch", {"url": "https://x.com"}).meta["target"] == "https://x.com"
