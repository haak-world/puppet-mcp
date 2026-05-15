"""puppet-mcp: MCP server for orchestrating Claude Code sessions via tmux."""

import os
from pathlib import Path

__version__ = "0.1.0"


def data_dir() -> Path:
    """Where puppet-mcp stores logs, state, and session map.
    Override with PUPPET_DATA_DIR env var. Default: ~/.puppet-mcp/"""
    d = Path(os.environ.get("PUPPET_DATA_DIR", "~/.puppet-mcp")).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d


def project_dir() -> str:
    """Project directory used for `cd` in launch/restart.
    Override with PUPPET_PROJECT_DIR env var. Default: cwd."""
    return os.environ.get("PUPPET_PROJECT_DIR", os.getcwd())


def console_url() -> str | None:
    """Optional console API URL for session naming.
    Override with PUPPET_CONSOLE_URL env var. Default: disabled."""
    url = os.environ.get("PUPPET_CONSOLE_URL", "")
    return url if url else None
