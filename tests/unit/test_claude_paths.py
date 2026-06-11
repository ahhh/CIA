from pathlib import Path

from cia.claude_paths import classify_path, encode_project_dir


class TestEncode:
    def test_path_to_dashes(self):
        assert encode_project_dir(Path("/Users/db/Programming/CIA")) == \
            "-Users-db-Programming-CIA"


class TestClassify:
    def test_memory(self):
        assert classify_path(
            "/Users/db/.claude/projects/-Users-db-Programming-CIA/memory/foo.md"
        ) == "memory"

    def test_memory_index(self):
        assert classify_path("/Users/db/.claude/projects/x/MEMORY.md") == "memory"

    def test_transcript(self):
        assert classify_path(
            "/Users/db/.claude/projects/-Users-db-Programming-CIA/abc.jsonl"
        ) == "transcript"

    def test_todo(self):
        assert classify_path("/Users/db/.claude/todos/t.json") == "todo"
        assert classify_path("/Users/db/.claude/tasks/t.json") == "todo"

    def test_settings(self):
        assert classify_path("/repo/.claude/settings.json") == "settings"

    def test_unrelated(self):
        assert classify_path("/Users/db/Programming/CIA/cia/proxy.py") is None
