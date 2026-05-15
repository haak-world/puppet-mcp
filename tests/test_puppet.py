"""Test suite for puppet-mcp. Uses real tmux for integration tests."""

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from puppet_mcp.compact import mechanical_prune
from puppet_mcp.session import SessionMap, discover_all_sessions, find_session_jsonl
from puppet_mcp.tmux import (
    capture_pane,
    is_idle,
    kill_session,
    send_keys,
    session_exists,
)


# ── test_send_keys_enter_separate ─────────────────────────────────────

def test_send_keys_enter_separate():
    """Verify send_keys sends -l text then Enter as TWO subprocess calls."""
    calls = []

    def mock_run(cmd, **kwargs):
        calls.append(cmd)
        mock = MagicMock()
        mock.returncode = 0
        mock.stdout = ""
        mock.stderr = ""
        return mock

    with patch("puppet_mcp.tmux.subprocess.run", side_effect=mock_run):
        send_keys("test-sess", "hello world")

    assert len(calls) == 2
    assert calls[0] == ["tmux", "send-keys", "-t", "test-sess", "-l", "hello world"]
    assert calls[1] == ["tmux", "send-keys", "-t", "test-sess", "Enter"]


# ── test_session_lifecycle ────────────────────────────────────────────

def test_session_lifecycle(puppet_session):
    """Create session, verify exists, kill, verify gone."""
    assert session_exists(puppet_session)
    result = kill_session(puppet_session)
    assert result.returncode == 0
    time.sleep(0.3)
    assert not session_exists(puppet_session)


# ── test_send_and_read ────────────────────────────────────────────────

def test_send_and_read(puppet_session):
    """Send 'echo hello' to bash, read pane, verify 'hello' appears."""
    send_keys(puppet_session, "echo hello")
    time.sleep(1)
    output = capture_pane(puppet_session, 10)
    assert "hello" in output


# ── test_session_map_crud ─────────────────────────────────────────────

def test_session_map_crud(data_dir):
    """Write/read/remove from session map file."""
    sm = SessionMap(path=data_dir / "sessions.json")

    sm.record("worker-1", "abc-123", agent="reed", cwd="/tmp")
    entry = sm.get("worker-1")
    assert entry is not None
    assert entry["session_id"] == "abc-123"
    assert entry["agent"] == "reed"

    sm.remove("worker-1")
    assert sm.get("worker-1") is None


# ── test_message_logging ─────────────────────────────────────────────

def test_message_logging(puppet_session, data_dir, monkeypatch):
    """Send message, verify log file contains it."""
    monkeypatch.setenv("PUPPET_DATA_DIR", str(data_dir))

    # Import after env is set so _data_dir picks it up
    from puppet_mcp import server
    server._session_map = SessionMap(path=data_dir / "sessions.json")

    result = server.puppet_send(puppet_session, "status check", from_agent="cora")
    assert "Sent to" in result

    log = data_dir / "puppet-messages.log"
    assert log.exists()
    content = log.read_text()
    assert "cora" in content
    assert "status check" in content


# ── test_kill_warns_if_attached ───────────────────────────────────────

def test_kill_warns_if_attached(puppet_session):
    """Mock has_attached_client to return True, verify puppet_manage kill warns."""
    from puppet_mcp import server

    with patch("puppet_mcp.server.has_attached_client", return_value=True):
        result = server.puppet_manage(puppet_session, action="kill", force=False)
    assert "Warning" in result or "attached" in result
    assert session_exists(puppet_session)


# ── test_status_parses_tokens ─────────────────────────────────────────

def test_status_parses_tokens():
    """Mock capture_pane to return text with token count, verify parse."""
    from puppet_mcp.tmux import parse_status_bar

    fake_pane = (
        "Some output here\n"
        "More text\n"
        "─────── reed ───────\n"
        "12,345 tokens\n"
    )
    info = parse_status_bar(fake_pane)
    assert info["tokens"] == 12345
    assert info["agent"] == "reed"


# ── test_is_idle_detection ────────────────────────────────────────────

@pytest.mark.parametrize("last_line,expected", [
    ("❯ ", True),
    ("❯", True),
    ("haak ❯ ", True),
    ("user@host $  ", False),  # rstrip removes trailing spaces; endswith("$ ") fails
    ("some-prompt > text", False),
    ("Running task...", False),
    ("Processing files", False),
    ("", False),
])
def test_is_idle_detection(last_line, expected):
    """Test _is_idle with various prompt patterns."""
    pane = f"some previous output\n{last_line}"
    assert is_idle(pane) == expected


# ── test_mechanical_prune ─────────────────────────────────────────────

def test_mechanical_prune():
    """Test compact with sample JSONL entries, verify reduction."""
    entries = [
        {"type": "agent-setting", "message": {"role": "system", "content": "setup"}},
        {"type": "permission-mode", "message": {"role": "system", "content": "auto"}},
        {"type": "message", "message": {"role": "user", "content": "hello"}},
        {"type": "message", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "r1", "name": "Read", "input": {"file_path": "/a/b/foo.py"}},
        ]}},
        {"type": "message", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "r1", "content": "file contents here"},
        ]}},
        {"type": "message", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": "r2", "name": "Read", "input": {"file_path": "/a/b/bar.py"}},
        ]}},
        {"type": "message", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "r2", "content": "more file contents"},
        ]}},
        {"type": "message", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "I analyzed the files."},
        ]}},
        {"type": "message", "message": {"role": "user", "content": "thanks"}},
    ]

    pruned = mechanical_prune(entries)

    # agent-setting and permission-mode should be stripped
    types = [e.get("type") for e in pruned]
    assert "agent-setting" not in types
    assert "permission-mode" not in types

    # Sequential reads should be collapsed
    summaries = [e for e in pruned if e.get("type") == "summary"]
    assert len(summaries) >= 1
    assert "Read 2 files" in summaries[0]["note"]

    # Total should be less than original
    assert len(pruned) < len(entries)


# ── test_find_sessions ────────────────────────────────────────────────

def test_find_sessions(tmp_path):
    """Mock ~/.claude/sessions/ directory, verify discover_all_sessions finds them."""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    session_data = {
        "sessionId": "abc-def-123",
        "agent": "reed",
        "cwd": "/tmp/project",
        "model": "claude-opus-4-6",
    }
    (sessions_dir / "99999.json").write_text(json.dumps(session_data))

    with (
        patch("puppet_mcp.session.CLAUDE_SESSION_STORE", sessions_dir),
        patch("puppet_mcp.session.CLAUDE_SESSIONS_DIR", tmp_path / "projects"),
        patch("subprocess.run") as mock_run,
    ):
        mock_result = MagicMock()
        mock_result.returncode = 1  # process not running
        mock_run.return_value = mock_result

        results = discover_all_sessions(hours=24)

    assert len(results) >= 1
    found = [r for r in results if r["session_id"] == "abc-def-123"]
    assert len(found) == 1
    assert found[0]["agent"] == "reed"
    assert found[0]["is_running"] is False


# ── test_find_session_jsonl ──────────────────────────────────────────

def test_find_session_jsonl_by_uuid(tmp_path):
    """find_session_jsonl resolves a raw UUID to its JSONL file."""
    projects_dir = tmp_path / "projects"
    project_sub = projects_dir / "-Users-zach-Projects-haak"
    project_sub.mkdir(parents=True)

    sid = "be323ee9-bbb2-4b9e-b9be-41a45529684c"
    jsonl = project_sub / f"{sid}.jsonl"
    jsonl.write_text('{"type":"agent-setting"}\n')

    sm = SessionMap(path=tmp_path / "sessions.json")

    with patch("puppet_mcp.session.CLAUDE_SESSIONS_DIR", projects_dir):
        result = find_session_jsonl(sid, sm)

    assert result is not None
    assert result == jsonl


def test_find_session_jsonl_by_tmux_name(tmp_path):
    """find_session_jsonl resolves a tmux name via session map."""
    projects_dir = tmp_path / "projects"
    project_sub = projects_dir / "-Users-zach-Projects-haak"
    project_sub.mkdir(parents=True)

    sid = "abc-def-ghi-jkl-123456789012"
    jsonl = project_sub / f"{sid}.jsonl"
    jsonl.write_text('{"type":"agent-setting"}\n')

    sm = SessionMap(path=tmp_path / "sessions.json")
    sm.record("worker-reed", sid, agent="reed")

    with patch("puppet_mcp.session.CLAUDE_SESSIONS_DIR", projects_dir):
        result = find_session_jsonl("worker-reed", sm)

    assert result is not None
    assert result == jsonl


def test_find_session_jsonl_not_found(tmp_path):
    """find_session_jsonl returns None for a UUID with no matching file."""
    projects_dir = tmp_path / "projects"
    projects_dir.mkdir()

    sm = SessionMap(path=tmp_path / "sessions.json")

    with patch("puppet_mcp.session.CLAUDE_SESSIONS_DIR", projects_dir):
        result = find_session_jsonl("deadbeef-dead-beef-dead-beefdeadbeef", sm)

    assert result is None
