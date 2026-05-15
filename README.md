# puppet-mcp

MCP server for orchestrating Claude Code sessions via tmux. A parent Claude Code session launches, monitors, communicates with, and manages child sessions running as independent processes in tmux.

## Install

```bash
pip install -e .
```

Or with uv:

```bash
uv pip install -e .
```

## Quick start

Add to your Claude Code MCP config (`.mcp.json` or `~/.claude/settings.json`):

```json
{
  "mcpServers": {
    "puppet": {
      "command": "puppet-mcp",
      "args": []
    }
  }
}
```

Then from Claude Code:

```
puppet_launch(name="worker-1", agent="reed", prompt="Fix the tests")
puppet_read(name="worker-1")
puppet_status()
puppet_kill(name="worker-1")
```

## Configuration

All configuration via environment variables with sensible defaults:

| Variable | Default | Purpose |
|:---------|:--------|:--------|
| `PUPPET_DATA_DIR` | `~/.puppet-mcp/` | Where to write logs, session map, heartbeat log |
| `PUPPET_PROJECT_DIR` | Current working directory | Project directory for `cd` in launch/restart |
| `PUPPET_CONSOLE_URL` | _(empty = skip)_ | Console API base URL for session naming |

## Tools reference

### Orchestrator (17 tools — `puppet-mcp`)

| Tool | Purpose |
|:-----|:--------|
| **Launch & lifecycle** | |
| `puppet_launch` | Spawn a new session — model, context, budget, safe by default |
| `puppet_kill` | Terminate a session (warns if user attached) |
| `puppet_restart` | Kill and relaunch with `--resume` (warns if attached) |
| `puppet_compact` | Prune session JSONL transcript (backs up original first) |
| **Input** | |
| `puppet_send` | Send raw text + Enter |
| `puppet_accept` | Send Enter (accept permission prompt) |
| `puppet_interrupt` | Send Escape (stop generation) |
| `puppet_cancel` | Send Ctrl+C |
| `puppet_cli` | Send any slash command (/status, /model, /compact, etc.) |
| `puppet_upgrade` | Upgrade to opus with 1M context window |
| **Read** | |
| `puppet_read` | Capture last N lines from a session |
| `puppet_list` | List tmux sessions with status and tail output |
| `puppet_status` | Structured status: tokens, agent, activity class per session |
| `puppet_find` | Search ALL Claude Code sessions by agent/topic |
| **Communication** | |
| `puppet_message` | Inter-agent messaging with durable log |
| `puppet_ping` | Send prompt to idle agent, wait for response |
| **Monitoring** | |
| `puppet_heartbeat` | One-shot monitoring pass: status all, optionally ping idles |

### Worker (5 tools — `puppet-mcp-worker`)

| Tool | Purpose |
|:-----|:--------|
| `puppet_message` | Send messages to other sessions |
| `puppet_list` | List sessions with status |
| `puppet_read` | Read pane output |
| `puppet_status` | Structured status of all sessions |
| `puppet_find` | Search all Claude Code sessions |

Workers can communicate and observe but cannot launch, kill, compact, or control other sessions.

## CLI reference

A companion bash CLI at `infra/scripts/puppet` (HAAK-specific, not part of the published package):

```bash
puppet ls                              # list sessions with tail output
puppet status                          # structured: tokens, agent, activity class
puppet find [QUERY]                    # search ALL Claude Code sessions
puppet launch NAME AGENT PROMPT        # launch (safe mode, opus)
puppet attach NAME                     # attach to a session
puppet send NAME TEXT                  # send text + Enter
puppet read NAME [LINES]              # read pane output
puppet accept NAME                    # accept permission prompt
puppet kill NAME [--force]            # kill session
puppet restart NAME [--force]         # kill + resume
puppet compact NAME [SUMMARY]         # compact transcript
puppet message FROM TO MSG            # inter-agent message
puppet heartbeat [ACTION]             # monitoring pass (status|ping)
puppet log [N]                        # tail message log
```

## Worker mode

Two server configurations enforce access control:

- **`puppet-mcp`** (orchestrator) — all 17 tools. Only the orchestrating session loads this.
- **`puppet-mcp-worker`** — restricted to 5 read/communicate tools. Child sessions load this so they can observe and message but cannot control other sessions.

## Architecture

```
puppet-mcp/
├── server.py       # Orchestrator: all 17 tools
├── worker.py       # Worker: 5 observation/communication tools
├── tmux.py         # All tmux subprocess wrappers
├── session.py      # Session map + PID chain resolution + discovery
└── compact.py      # Transcript pruning logic
```

Key design decisions:

- **Safe by default**: `puppet_launch` does NOT use `--dangerously-skip-permissions`. The orchestrator reads each session with `puppet_read`, sees permission prompts, and calls `puppet_accept`.
- **Enter bug fix**: Every `send_keys` call sends text with `-l` (literal) flag then Enter as a SEPARATE tmux call. Never concatenated.
- **No hardcoded paths**: Uses env vars for data directory and project directory. Only `~/.claude/` is referenced directly (Claude Code's standard location).
- **Session discovery**: `puppet_find` reads `~/.claude/sessions/*.json` directly — no external script dependencies.

## Activity classification

`puppet_status` classifies each session:

| Class | Meaning |
|:------|:--------|
| **active** | Output in progress (working spinner visible) |
| **idle** | At prompt, waiting for input |
| **stale** | Idle with recap message — no recent activity |
| **dead** | tmux session exists but process is gone |

## License

MIT
