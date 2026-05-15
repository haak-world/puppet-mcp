"""Tests for puppet_split — session surgery by topic."""

import json
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from puppet_mcp.compact import (
    build_split_session,
    classify_rounds,
    extract_rounds,
    split_session,
)


# ── fixtures ─────────────────────────────────────────────────────────


def _make_entry(role, content, entry_type="message", **extra):
    """Build a minimal JSONL entry."""
    e = {
        "type": entry_type,
        "uuid": str(uuid.uuid4()),
        "parentUuid": "",
        "sessionId": "original-session-id",
        "message": {"role": role, "content": content},
    }
    e.update(extra)
    return e


def _make_tool_use(name, tool_id, inp):
    return {"type": "tool_use", "id": tool_id, "name": name, "input": inp}


def _make_tool_result(tool_id, content):
    return {"type": "tool_result", "tool_use_id": tool_id, "content": content}


@pytest.fixture
def session_entries():
    """A realistic multi-topic session: auth work, then test writing."""
    return [
        # Header entries
        {"type": "agent-setting", "sessionId": "sid-123", "agentSetting": "reed"},
        {"type": "permission-mode", "sessionId": "sid-123", "permissionMode": "default"},
        # Round 0: auth topic
        _make_entry("user", "Let's fix the authentication bug in the login flow"),
        _make_entry("assistant", [{"type": "text", "text": "I'll look at the auth module."}]),
        _make_entry("assistant", [
            _make_tool_use("Read", "r1", {"file_path": "/src/auth.py"}),
        ]),
        _make_entry("user", [_make_tool_result("r1", "class AuthManager: ...")]),
        _make_entry("assistant", [{"type": "text", "text": "Found the bug in token validation."}]),
        # Round 1: auth topic
        _make_entry("user", "Can you also check the session middleware?"),
        _make_entry("assistant", [{"type": "text", "text": "Checking middleware now."}]),
        # Round 2: tests topic
        _make_entry("user", "Now let's write tests for the auth changes"),
        _make_entry("assistant", [{"type": "text", "text": "I'll create test_auth.py."}]),
        _make_entry("assistant", [
            _make_tool_use("Write", "w1", {"file_path": "/tests/test_auth.py", "content": "..."}),
        ]),
        _make_entry("user", [_make_tool_result("w1", "File written.")]),
        _make_entry("assistant", [{"type": "text", "text": "Tests written and passing."}]),
        # Round 3: tests topic
        _make_entry("user", "Add an integration test too"),
        _make_entry("assistant", [{"type": "text", "text": "Adding integration test."}]),
    ]


@pytest.fixture
def session_jsonl(tmp_path, session_entries):
    """Write session_entries to a JSONL file, return path."""
    p = tmp_path / "sid-123.jsonl"
    with open(p, "w") as f:
        for entry in session_entries:
            f.write(json.dumps(entry) + "\n")
    return p


# ── extract_rounds ───────────────────────────────────────────────────


def test_extract_rounds_count(session_entries):
    rounds = extract_rounds(session_entries)
    assert len(rounds) == 4


def test_extract_rounds_user_text(session_entries):
    rounds = extract_rounds(session_entries)
    assert "authentication bug" in rounds[0]["user_text"]
    assert "session middleware" in rounds[1]["user_text"]
    assert "write tests" in rounds[2]["user_text"]
    assert "integration test" in rounds[3]["user_text"]


def test_extract_rounds_entries_include_assistant(session_entries):
    rounds = extract_rounds(session_entries)
    # Round 0 starts with the user message and includes assistant replies + tool calls
    roles = [
        e.get("message", {}).get("role")
        for e in rounds[0]["entries"]
        if isinstance(e.get("message"), dict)
    ]
    assert "user" in roles
    assert "assistant" in roles


def test_extract_rounds_empty():
    rounds = extract_rounds([])
    assert rounds == []


def test_extract_rounds_no_user_messages():
    entries = [
        _make_entry("assistant", [{"type": "text", "text": "Hello"}]),
        _make_entry("assistant", [{"type": "text", "text": "World"}]),
    ]
    rounds = extract_rounds(entries)
    assert rounds == []


def test_extract_rounds_tool_results_grouped():
    """Tool results (user role, list content) stay in the current round."""
    entries = [
        _make_entry("user", "Read foo.py"),
        _make_entry("assistant", [_make_tool_use("Read", "t1", {"file_path": "foo.py"})]),
        _make_entry("user", [_make_tool_result("t1", "contents")]),
        _make_entry("assistant", [{"type": "text", "text": "Got it."}]),
        _make_entry("user", "Now do bar"),
    ]
    rounds = extract_rounds(entries)
    assert len(rounds) == 2
    # The tool_result should be in round 0, not start a new round
    assert len(rounds[0]["entries"]) == 4


# ── classify_rounds ──────────────────────────────────────────────────


def _mock_vertex_response(text):
    """Create a mock anthropic module with AnthropicVertex that returns the given text."""
    mock_module = MagicMock()
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=text)]
    mock_client = MagicMock()
    mock_client.messages.create.return_value = mock_response
    mock_module.AnthropicVertex.return_value = mock_client
    return mock_module, mock_client


def test_classify_rounds_calls_vertex():
    """Verify classify_rounds uses AnthropicVertex and parses response."""
    rounds = [
        {"index": 0, "user_text": "Fix the auth bug", "entries": [{}]},
        {"index": 1, "user_text": "Write tests for auth", "entries": [{}]},
    ]

    resp_json = json.dumps({
        "classifications": {"0": "auth", "1": "tests"},
        "suggestions": ["docs"],
    })
    mock_module, mock_client = _mock_vertex_response(resp_json)

    with patch.dict("sys.modules", {"anthropic": mock_module}):
        result = classify_rounds(rounds, ["auth", "tests"])

    assert result["classifications"]["0"] == "auth"
    assert result["classifications"]["1"] == "tests"
    assert "docs" in result["suggestions"]

    # Verify AnthropicVertex was instantiated (not Anthropic)
    mock_module.AnthropicVertex.assert_called_once()
    mock_module.Anthropic.assert_not_called()

    # Verify called with haiku model
    call_kwargs = mock_client.messages.create.call_args
    assert "haiku" in call_kwargs.kwargs["model"]


def test_classify_rounds_vertex_config(monkeypatch):
    """Verify classify_rounds reads project/region from env vars."""
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "my-project")
    monkeypatch.setenv("VERTEX_REGION", "us-east5")

    rounds = [{"index": 0, "user_text": "hello", "entries": [{}]}]
    resp_json = json.dumps({"classifications": {"0": "auth"}, "suggestions": []})
    mock_module, _ = _mock_vertex_response(resp_json)

    with patch.dict("sys.modules", {"anthropic": mock_module}):
        classify_rounds(rounds, ["auth"])

    call_kwargs = mock_module.AnthropicVertex.call_args
    assert call_kwargs.kwargs["project_id"] == "my-project"
    assert call_kwargs.kwargs["region"] == "us-east5"


def test_classify_rounds_handles_markdown_fences():
    """LLM might wrap JSON in ```json fences."""
    rounds = [{"index": 0, "user_text": "hello", "entries": [{}]}]

    fenced = '```json\n{"classifications": {"0": "auth"}, "suggestions": []}\n```'
    mock_module, _ = _mock_vertex_response(fenced)

    with patch.dict("sys.modules", {"anthropic": mock_module}):
        result = classify_rounds(rounds, ["auth"])

    assert result["classifications"]["0"] == "auth"


# ── build_split_session ──────────────────────────────────────────────


def test_build_split_session_structure():
    header = [
        {"type": "agent-setting", "sessionId": "old-sid", "agentSetting": "reed"},
        {"type": "permission-mode", "sessionId": "old-sid"},
    ]
    rounds = [
        {"index": 0, "user_text": "fix auth", "entries": [
            _make_entry("user", "fix auth"),
            _make_entry("assistant", [{"type": "text", "text": "On it."}]),
        ]},
    ]

    new_sid, entries = build_split_session(
        "old-sid", header, "auth", rounds, ["auth", "tests"], agent="reed",
    )

    # New session ID is a UUID
    assert len(new_sid) == 36
    assert new_sid != "old-sid"

    # All entries have the new session ID
    for e in entries:
        assert e.get("sessionId") == new_sid

    # Structure: header(2) + context(1) + ack(1) + round entries(2) + last-prompt(1)
    assert len(entries) == 7

    # First two are header entries
    assert entries[0]["type"] == "agent-setting"
    assert entries[1]["type"] == "permission-mode"

    # Synthetic context message mentions the split
    context_msg = entries[2]["message"]["content"]
    assert "split from old-sid" in context_msg
    assert "auth" in context_msg

    # Last entry is last-prompt
    assert entries[-1]["type"] == "last-prompt"


def test_build_split_session_uuid_chain():
    """Verify uuid/parentUuid form a linear chain."""
    header = [{"type": "agent-setting", "sessionId": "old"}]
    rounds = [
        {"index": 0, "user_text": "hello", "entries": [
            _make_entry("user", "hello"),
            _make_entry("assistant", [{"type": "text", "text": "hi"}]),
        ]},
        {"index": 1, "user_text": "bye", "entries": [
            _make_entry("user", "bye"),
        ]},
    ]

    _, entries = build_split_session("old", header, "chat", rounds, ["chat"])

    # Skip header entries (they don't have uuid chain)
    chain_entries = [e for e in entries if "uuid" in e and "parentUuid" in e]
    for i in range(1, len(chain_entries)):
        assert chain_entries[i]["parentUuid"] == chain_entries[i - 1]["uuid"]


def test_build_split_session_empty_rounds():
    header = [{"type": "agent-setting", "sessionId": "old"}]
    new_sid, entries = build_split_session("old", header, "empty", [], ["empty"])
    # Header(1) + context(1) + ack(1) + last-prompt(1) = 4
    assert len(entries) == 4


# ── split_session (integration) ──────────────────────────────────────


def _split_with_mock(session_jsonl, topics, classifications, suggestions=None):
    """Run split_session with a mocked LLM classifier."""
    resp = json.dumps({
        "classifications": classifications,
        "suggestions": suggestions or [],
    })
    mock_module, _ = _mock_vertex_response(resp)
    with patch.dict("sys.modules", {"anthropic": mock_module}):
        return split_session(session_jsonl, topics)


def test_split_session_end_to_end(session_jsonl):
    """Full pipeline with mocked LLM classifier."""
    results = _split_with_mock(
        session_jsonl, ["auth", "tests"],
        {"0": "auth", "1": "auth", "2": "tests", "3": "tests"},
        suggestions=["docs"],
    )

    # Two topic sessions produced
    assert "auth" in results
    assert "tests" in results

    # Round counts
    assert results["auth"]["rounds"] == 2
    assert results["tests"]["rounds"] == 2

    # Session IDs are valid UUIDs
    for topic in ("auth", "tests"):
        sid = results[topic]["session_id"]
        uuid.UUID(sid)  # raises if invalid

    # Files exist on disk, named as NEW_SID.jsonl
    for topic in ("auth", "tests"):
        p = Path(results[topic]["path"])
        assert p.exists()
        assert p.suffix == ".jsonl"
        # Filename must be the new session ID (for claude --resume)
        assert p.stem == results[topic]["session_id"]

    # Suggestions passed through
    assert results["_suggestions"] == ["docs"]


def test_split_session_files_are_resumable(session_jsonl):
    """Verify split JSONL files have the structure claude --resume expects."""
    results = _split_with_mock(
        session_jsonl, ["auth", "tests"],
        {"0": "auth", "1": "auth", "2": "tests", "3": "tests"},
    )

    for topic in ("auth", "tests"):
        p = Path(results[topic]["path"])
        entries = []
        with open(p) as f:
            for line in f:
                if line.strip():
                    entries.append(json.loads(line))

        # Must start with agent-setting
        assert entries[0]["type"] == "agent-setting"

        # Must end with last-prompt
        assert entries[-1]["type"] == "last-prompt"

        # last-prompt must reference the final entry's uuid
        leaf = entries[-1]["leafUuid"]
        uuids = [e.get("uuid") for e in entries if "uuid" in e]
        assert leaf in uuids

        # All entries share the same sessionId
        sids = {e.get("sessionId") for e in entries if "sessionId" in e}
        assert len(sids) == 1

        # CRITICAL: filename must equal the sessionId (claude --resume requirement)
        file_sid = p.stem
        content_sid = sids.pop()
        assert file_sid == content_sid


def test_split_session_unknown_topics_go_to_other(session_jsonl):
    """Rounds classified outside requested topics land in 'other'."""
    results = _split_with_mock(
        session_jsonl, ["auth"],
        {"0": "auth", "1": "other", "2": "other", "3": "auth"},
    )

    assert "auth" in results
    assert results["auth"]["rounds"] == 2
    assert "other" in results
    assert results["other"]["rounds"] == 2


def test_split_session_output_naming(session_jsonl):
    """Primary file is NEW_SID.jsonl; symlink is ORIGINAL.split.TOPIC.jsonl."""
    results = _split_with_mock(
        session_jsonl, ["auth", "tests"],
        {"0": "auth", "1": "auth", "2": "tests", "3": "tests"},
    )

    for topic in ("auth", "tests"):
        sid = results[topic]["session_id"]
        p = Path(results[topic]["path"])

        # Primary file: named by new session ID
        assert p.name == f"{sid}.jsonl"
        assert p.parent == session_jsonl.parent

        # Human-readable symlink exists and points to the primary file
        link = session_jsonl.parent / f"sid-123.split.{topic}.jsonl"
        assert link.is_symlink()
        assert link.resolve() == p.resolve()


# ── puppet_split MCP tool ────────────────────────────────────────────


def test_puppet_split_no_transcript():
    """puppet_manage split returns error when transcript not found."""
    from puppet_mcp.server import puppet_manage

    with patch("puppet_mcp.server.find_session_jsonl", return_value=None):
        result = puppet_manage("ghost-session", action="split", topics=["auth", "tests"])

    assert "Error" in result
    assert "transcript" in result.lower()
