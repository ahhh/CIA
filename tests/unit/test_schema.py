import json
import pytest
from cia.schema import Event, Phase, _extract_hook_error


class TestPhase:
    def test_all_phases_are_strings(self):
        for p in Phase:
            assert isinstance(p.value, str)

    def test_roundtrip(self):
        for p in Phase:
            assert Phase(p.value) is p


class TestEventSerialization:
    def test_to_dict_phase_is_string(self):
        e = Event(phase=Phase.API_REQUEST_START)
        d = e.to_dict()
        assert d["phase"] == "api_request_start"
        assert isinstance(d["phase"], str)

    def test_to_json_valid(self):
        e = Event(phase=Phase.TOOL_CALL_START, tool="Bash")
        raw = e.to_json()
        parsed = json.loads(raw)
        assert parsed["phase"] == "tool_call_start"
        assert parsed["tool"] == "Bash"

    def test_from_dict_roundtrip(self):
        e = Event(
            phase=Phase.API_RESPONSE_END,
            session_id="ses_abc",
            model="claude-sonnet-4-6",
            tokens_input=100,
            tokens_output=50,
            duration_ms=1234.5,
        )
        d = e.to_dict()
        e2 = Event.from_dict(d)
        assert e2.phase is Phase.API_RESPONSE_END
        assert e2.session_id == "ses_abc"
        assert e2.model == "claude-sonnet-4-6"
        assert e2.duration_ms == 1234.5

    def test_default_id_is_unique(self):
        ids = {Event(phase=Phase.FILE_CHANGE).id for _ in range(100)}
        assert len(ids) == 100

    def test_default_ts_is_recent(self):
        import time
        before = time.time()
        e = Event(phase=Phase.SESSION_START)
        after = time.time()
        assert before <= e.ts <= after


class TestFromHookPayload:
    def test_pre_tool_use(self):
        payload = {
            "session_id": "ses_123",
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la"},
            "hook_event_name": "PreToolUse",
        }
        e = Event.from_hook_payload(Phase.TOOL_CALL_START, payload)
        assert e.phase is Phase.TOOL_CALL_START
        assert e.session_id == "ses_123"
        assert e.tool == "Bash"
        assert e.tool_input == {"command": "ls -la"}
        assert e.error is None

    def test_post_tool_use_with_error(self):
        payload = {
            "session_id": "ses_456",
            "tool_name": "Bash",
            "tool_input": {"command": "bad_cmd"},
            "tool_response": {
                "is_error": True,
                "content": [{"type": "text", "text": "command not found"}],
            },
            "hook_event_name": "PostToolUse",
        }
        e = Event.from_hook_payload(Phase.TOOL_CALL_END, payload)
        assert e.error == "command not found"

    def test_post_tool_use_success(self):
        payload = {
            "session_id": "ses_789",
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/test"},
            "tool_response": {"is_error": False, "content": "file content"},
        }
        e = Event.from_hook_payload(Phase.TOOL_CALL_END, payload)
        assert e.error is None

    def test_stop_hook(self):
        payload = {"session_id": "ses_abc", "hook_event_name": "Stop"}
        e = Event.from_hook_payload(Phase.SESSION_END, payload)
        assert e.phase is Phase.SESSION_END
        assert e.session_id == "ses_abc"
