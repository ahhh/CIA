import time
import pytest
from cia.schema import Event, Phase
from cia.sse_parser import SSEParser


def _parser(session_id=None) -> tuple[SSEParser, list[Event]]:
    events: list[Event] = []
    p = SSEParser("flow_test", events.append, session_id=session_id)
    return p, events


def _sse(*event_dicts) -> bytes:
    """Build a multi-event SSE byte stream."""
    import json
    chunks = []
    for d in event_dicts:
        event_type = d.pop("_event", None)
        line = f"data: {json.dumps(d)}\n\n"
        if event_type:
            line = f"event: {event_type}\n" + line
        chunks.append(line.encode())
    return b"".join(chunks)


# ----- Canonical Anthropic SSE payloads ----------------------------------------

def _message_start(model="claude-sonnet-4-6", input_tokens=100):
    return {
        "_event": "message_start",
        "type": "message_start",
        "message": {
            "id": "msg_abc",
            "type": "message",
            "role": "assistant",
            "model": model,
            "usage": {"input_tokens": input_tokens},
        },
    }

def _block_start(idx, btype):
    return {
        "_event": "content_block_start",
        "type": "content_block_start",
        "index": idx,
        "content_block": {"type": btype},
    }

def _block_stop(idx):
    return {
        "_event": "content_block_stop",
        "type": "content_block_stop",
        "index": idx,
    }

def _message_delta(output_tokens=50):
    return {
        "_event": "message_delta",
        "type": "message_delta",
        "delta": {"stop_reason": "end_turn"},
        "usage": {"output_tokens": output_tokens},
    }

def _message_stop():
    return {"_event": "message_stop", "type": "message_stop"}


# ----- Tests -------------------------------------------------------------------

class TestBasicFlow:
    def test_no_thinking(self):
        p, events = _parser(session_id="s1")
        data = _sse(
            _message_start(),
            _block_start(0, "text"),
            _block_stop(0),
            _message_delta(),
            _message_stop(),
        )
        p.feed(data)

        phases = [e.phase for e in events]
        assert Phase.API_REQUEST_START    in phases
        assert Phase.API_GENERATION_START in phases
        assert Phase.API_RESPONSE_END     in phases
        assert Phase.API_THINKING_START   not in phases

    def test_with_thinking(self):
        p, events = _parser()
        data = _sse(
            _message_start(),
            _block_start(0, "thinking"),
            _block_stop(0),
            _block_start(1, "text"),
            _block_stop(1),
            _message_delta(),
            _message_stop(),
        )
        p.feed(data)

        phases = [e.phase for e in events]
        assert Phase.API_THINKING_START   in phases
        assert Phase.API_THINKING_END     in phases
        assert Phase.API_GENERATION_START in phases
        assert Phase.API_RESPONSE_END     in phases

    def test_session_id_propagated(self):
        p, events = _parser(session_id="my_session")
        p.feed(_sse(_message_start(), _message_stop()))
        for e in events:
            assert e.session_id == "my_session"

    def test_model_propagated(self):
        p, events = _parser()
        p.feed(_sse(_message_start(model="claude-opus-4-7"), _message_stop()))
        api_start = next(e for e in events if e.phase is Phase.API_REQUEST_START)
        assert api_start.model == "claude-opus-4-7"

    def test_token_counts(self):
        p, events = _parser()
        p.feed(_sse(
            _message_start(input_tokens=200),
            _message_delta(output_tokens=75),
            _message_stop(),
        ))
        end_evt = next(e for e in events if e.phase is Phase.API_RESPONSE_END)
        assert end_evt.tokens_input == 200
        assert end_evt.tokens_output == 75


class TestChunkedFeeding:
    def test_split_across_chunks(self):
        """SSE event split across two feed() calls."""
        p, events = _parser()
        full = _sse(_message_start(), _message_stop())
        mid = len(full) // 2
        p.feed(full[:mid])
        p.feed(full[mid:])
        phases = {e.phase for e in events}
        assert Phase.API_REQUEST_START in phases
        assert Phase.API_RESPONSE_END  in phases

    def test_one_byte_at_a_time(self):
        p, events = _parser()
        data = _sse(_message_start(), _message_stop())
        for byte in data:
            p.feed(bytes([byte]))
        phases = {e.phase for e in events}
        assert Phase.API_REQUEST_START in phases


class TestTiming:
    def test_duration_set_on_response_end(self):
        p, events = _parser()
        p.set_request_start(time.time() - 1.0)
        p.feed(_sse(_message_start(), _message_stop()))
        end_evt = next(e for e in events if e.phase is Phase.API_RESPONSE_END)
        assert end_evt.duration_ms is not None
        assert end_evt.duration_ms >= 900  # at least ~1 second

    def test_thinking_duration_set(self):
        p, events = _parser()
        p.feed(_sse(
            _message_start(),
            _block_start(0, "thinking"),
            _block_stop(0),
            _message_stop(),
        ))
        thinking_end = next(e for e in events if e.phase is Phase.API_THINKING_END)
        assert thinking_end.duration_ms is not None
        assert thinking_end.duration_ms >= 0


class TestEdgeCases:
    def test_empty_feed(self):
        p, events = _parser()
        p.feed(b"")
        assert events == []

    def test_garbage_data_ignored(self):
        p, events = _parser()
        p.feed(b"not valid json\n\n")
        assert events == []

    def test_done_sentinel_ignored(self):
        p, events = _parser()
        p.feed(b"data: [DONE]\n\n")
        assert events == []

    def test_flush_partial_buffer(self):
        import json
        p, events = _parser()
        # Feed a complete event without trailing \n\n
        data = b'data: {"type": "message_stop"}\n'
        p.feed(data)
        assert not events  # not processed yet
        p.flush()
        # message_stop with no prior message_start emits response_end
        phases = {e.phase for e in events}
        assert Phase.API_RESPONSE_END in phases


class TestProgress:
    def _delta(self, idx, dtype, key, text):
        return {
            "_event": "content_block_delta",
            "type": "content_block_delta",
            "index": idx,
            "delta": {"type": dtype, key: text},
        }

    def test_progress_emitted_during_thinking(self):
        p, events = _parser()
        p.progress_interval_s = 0.0   # emit on every feed while streaming
        p.set_request_start(time.time())
        p.feed(_sse(_message_start(), _block_start(0, "thinking")))
        p.feed(_sse(self._delta(0, "thinking_delta", "thinking", "x" * 400)))

        progress = [e for e in events if e.phase == Phase.API_PROGRESS]
        assert progress, "expected api_progress while stream is live"
        last = progress[-1]
        assert last.meta["state"] == "thinking"
        assert last.meta["output_chars"] == 400
        assert last.meta["est_output_tokens"] == 100
        assert last.duration_ms is not None

    def test_progress_state_responding_and_stops_after_message_stop(self):
        p, events = _parser()
        p.progress_interval_s = 0.0
        p.feed(_sse(_message_start(), _block_start(0, "text")))
        p.feed(_sse(self._delta(0, "text_delta", "text", "hello world")))
        responding = [e for e in events if e.phase == Phase.API_PROGRESS]
        assert responding[-1].meta["state"] == "responding"

        p.feed(_sse(_block_stop(0), _message_delta(), _message_stop()))
        n = len([e for e in events if e.phase == Phase.API_PROGRESS])
        p.feed(b"")   # further feeds emit nothing once stopped
        assert len([e for e in events if e.phase == Phase.API_PROGRESS]) == n

    def test_no_progress_by_default_for_fast_streams(self):
        p, events = _parser()   # default 5s interval
        p.feed(_sse(
            _message_start(), _block_start(0, "text"),
            self._delta(0, "text_delta", "text", "hi"),
            _block_stop(0), _message_delta(), _message_stop(),
        ))
        assert not [e for e in events if e.phase == Phase.API_PROGRESS]


class TestThinkingTokens:
    def _delta(self, idx, dtype, key, text):
        return {
            "_event": "content_block_delta",
            "type": "content_block_delta",
            "index": idx,
            "delta": {"type": dtype, key: text},
        }

    def test_thinking_end_estimates_tokens(self):
        p, events = _parser()
        p.set_request_start(time.time())
        p.feed(_sse(_message_start(), _block_start(0, "thinking")))
        p.feed(_sse(self._delta(0, "thinking_delta", "thinking", "x" * 400)))
        p.feed(_sse(_block_stop(0)))

        end = next(e for e in events if e.phase is Phase.API_THINKING_END)
        assert end.thinking_tokens == 100          # 400 chars // 4
        assert end.meta["thinking_chars"] == 400
        assert end.meta["est_thinking_tokens"] == 100

    def test_response_end_thinking_summary(self):
        p, events = _parser()
        p.set_request_start(time.time())
        p.feed(_sse(_message_start(), _block_start(0, "thinking")))
        p.feed(_sse(self._delta(0, "thinking_delta", "thinking", "y" * 800)))
        p.feed(_sse(_block_stop(0), _block_start(1, "text")))
        p.feed(_sse(self._delta(1, "text_delta", "text", "answer")))
        p.feed(_sse(_block_stop(1), _message_delta(output_tokens=200), _message_stop()))

        end = next(e for e in events if e.phase is Phase.API_RESPONSE_END)
        think = end.meta["thinking"]
        assert think["blocks"] == 1
        assert think["est_thinking_tokens"] == 200    # 800 // 4
        assert think["thinking_output_frac"] == 1.0   # 200 est / 200 output
        assert end.thinking_tokens == 200
