"""puppet worker MCP server — restricted tool set for non-orchestrator agents.

Tools (4):
  puppet_send   — all input actions (text, enter, escape, ctrl-c, slash)
  puppet_status — read-only session status (no heartbeat logging)
  puppet_read   — raw pane output
  puppet_find   — search all sessions by metadata/content

Workers can communicate, observe, and search but cannot launch, kill,
manage lifecycle, or perform cross-session accept_all.
"""

from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import data_dir
from .session import discover_all_sessions
from .tmux import (
    capture_pane,
    classify_activity,
    content_lines as _content_lines,
    is_idle,
    parse_status_bar,
    run_tmux,
    send_key,
    send_keys,
    session_exists,
)

mcp = FastMCP("puppet-worker")


def _message_log() -> Path:
    return data_dir() / "puppet-messages.log"


# ── puppet_send ─────────────────────────────────────────────────────────

@mcp.tool(structured_output=False)
def puppet_send(name: str, text: str = "", action: str = "text", from_agent: str = "") -> str:
    """Send input to a session. Consolidates all input actions.

    Actions:
      "text"   — (default) send text + Enter. If from_agent is set, prefix
                 with [from_agent→name]: and log to message log.
      "enter"  — send Enter key (accept a permission prompt).
      "escape" — send Escape (interrupt current generation).
      "ctrl-c" — send Ctrl+C (cancel current operation).
      "slash"  — send text as a slash command (prepends / if missing).

    Args:
        name: tmux session name
        text: text to send (required for "text" and "slash" actions)
        action: one of "text", "enter", "escape", "ctrl-c", "slash"
        from_agent: caller identity for "text" action (e.g. "orchestrator")
    """
    if not session_exists(name):
        return f"Error: tmux session '{name}' does not exist."

    if action == "enter":
        send_key(name, "Enter")
        return f"Accepted permission prompt in '{name}'."

    if action == "escape":
        send_key(name, "Escape")
        return f"Sent Escape to '{name}'."

    if action == "ctrl-c":
        send_key(name, "C-c")
        return f"Sent Ctrl+C to '{name}'."

    if action == "slash":
        cmd = text if text.startswith("/") else f"/{text}"
        send_keys(name, cmd)
        return f"Sent '{cmd}' to '{name}'."

    # action == "text" (default)
    if from_agent:
        send_keys(name, f"[{from_agent}→{name}]: {text}")
        log = _message_log()
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(log, "a") as f:
            f.write(f"{ts} {from_agent}→{name}: {text}\n")
    else:
        send_keys(name, text)
    return f"Sent to '{name}'."


# ── puppet_status ───────────────────────────────────────────────────────

@mcp.tool(structured_output=False)
def puppet_status(full: bool = False, name: str = "") -> str:
    """Read-only status of tmux sessions.

    Modes:
      - No args: structured status of all sessions (tokens, agent, activity).
      - name="X": detailed status of one session.
      - full=True: same as no args (included for API compatibility).

    No heartbeat logging — worker is read-only.

    Args:
        full: included for API compatibility (no effect in worker)
        name: if set, report status of this single session
    """
    # Single-session mode
    if name:
        if not session_exists(name):
            return f"Error: tmux session '{name}' does not exist."
        pane = capture_pane(name, 15)
        bar = parse_status_bar(pane)
        result_tmux = run_tmux(["ls", "-f", f"#{{==:#{{{name}}},#{{session_name}}}}"])
        tmux_line = result_tmux.stdout.strip() if result_tmux.returncode == 0 else ""
        activity = classify_activity(pane, tmux_line)
        tokens = bar.get("tokens")
        agent = bar.get("agent", "?")
        tokens_str = f"{tokens:,}" if tokens else "?"
        tail = _content_lines(pane, 3)
        lines = [f"[{activity}] {name}: agent={agent} tokens={tokens_str}"]
        if tail:
            lines.append("\n".join(tail))
        return "\n".join(lines)

    # All-sessions mode
    result = run_tmux(["ls"])
    if result.returncode != 0:
        return "No tmux sessions running."

    entries = []
    for tmux_line in result.stdout.strip().split("\n"):
        if not tmux_line.strip():
            continue
        sname = tmux_line.split(":")[0].strip()
        pane = capture_pane(sname, 15)
        bar = parse_status_bar(pane)
        activity = classify_activity(pane, tmux_line)
        tokens = bar.get("tokens")
        agent = bar.get("agent", "?")
        tokens_str = f"{tokens:,}" if tokens else "?"
        tail = _content_lines(pane, 1)
        entries.append(f"[{activity}] {sname}: agent={agent} tokens={tokens_str} — {tail[0] if tail else ''}")

    return "\n".join(entries) if entries else "No sessions found."


# ── puppet_read ─────────────────────────────────────────────────────────

@mcp.tool(structured_output=False)
def puppet_read(name: str, lines: int = 30) -> str:
    """Read the last N lines from a tmux session's visible pane.

    Args:
        name: tmux session name
        lines: number of lines to capture (default 30)
    """
    if not session_exists(name):
        return f"Error: tmux session '{name}' does not exist."
    output = capture_pane(name, lines)
    return output if output else "(empty pane)"


# ── puppet_find ─────────────────────────────────────────────────────────

@mcp.tool(structured_output=False)
def puppet_find(
    query: str = "",
    scope: str = "",
    grep: str = "",
    hours: int = 24,
) -> str:
    """Search ALL Claude Code sessions by agent, content, or directory.

    Args:
        query: filter by metadata — matches agent name, cwd, model, or topic
        scope: restrict to sessions in this directory tree.
               "." = current working directory. "" = all sessions.
        grep: search transcript content for this text
        hours: how far back to look (default 24). 0 = no time limit.
    """
    sessions = discover_all_sessions(hours=hours, scope=scope, grep=grep)
    if not sessions:
        return "No sessions found."

    if query:
        q = query.lower()
        sessions = [
            s for s in sessions
            if q in (s.get("agent") or "").lower()
            or q in (s.get("cwd") or "").lower()
            or q in (s.get("model") or "").lower()
            or q in (s.get("topic") or "").lower()
        ]

    if not sessions:
        return f"No sessions matching '{query}'."

    lines = []
    for s in sessions:
        agent = s.get("agent") or "?"
        sid = s.get("session_id", "?")
        exchanges = s.get("exchanges", "?")
        size = s.get("size_mb", "?")
        running = "running" if s.get("is_running") else "stopped"
        model = (s.get("model") or "?").replace("claude-", "")
        topic = s.get("topic", "")
        cwd = s.get("cwd", "")
        cwd_short = cwd.rstrip("/").rsplit("/", 1)[-1] if cwd else ""
        topic_str = f" — {topic}" if topic else ""
        cwd_str = f" [{cwd_short}]" if cwd_short else ""
        lines.append(
            f"[{running}] {agent} ({model}) sid={sid} "
            f"{exchanges}x {size}MB{cwd_str}{topic_str}"
        )

    return "\n".join(lines)


# ── main ────────────────────────────────────────────────────────────────

def main():
    mcp.run()


if __name__ == "__main__":
    main()
