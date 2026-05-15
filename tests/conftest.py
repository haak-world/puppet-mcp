"""pytest fixtures for puppet-mcp tests."""

import subprocess
import time

import pytest


@pytest.fixture
def puppet_session():
    """Create a tmux session running bash, yield the name, kill on teardown."""
    name = "puppet-test-session"
    subprocess.run(
        ["tmux", "kill-session", "-t", name],
        capture_output=True, timeout=5,
    )
    result = subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "-x", "120", "-y", "30", "bash"],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 0, f"Failed to create tmux session: {result.stderr}"
    time.sleep(0.5)
    yield name
    subprocess.run(
        ["tmux", "kill-session", "-t", name],
        capture_output=True, timeout=5,
    )


@pytest.fixture
def data_dir(tmp_path):
    """Temp directory for session map and logs."""
    return tmp_path
