"""puppet worker MCP server — restricted tool set for non-orchestrator agents.

Tools (5):
  puppet_send     — all input actions (text, enter, escape, ctrl-c, slash)
  puppet_status   — read-only session status (no heartbeat logging)
  puppet_read     — raw pane output
  puppet_find     — search all sessions by metadata/content
  puppet_sentinel — sentinel events (read-only: poll + status only)

Workers can communicate, observe, and search but cannot launch, kill,
manage lifecycle, or perform cross-session accept_all.
"""

from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

import json
import os

from . import data_dir
from .sentinel import (
    sentinel_status,
    poll_subscriber,
)
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
      "report" — completion report for a puppet_assign task.

    Args:
        name: tmux session name (for report: the assigner to report TO)
        text: text to send (required for "text", "slash", and "report")
        action: one of "text", "enter", "escape", "ctrl-c", "slash", "report"
        from_agent: caller identity (for report: who is reporting)
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

    if action == "report":
        if not from_agent:
            return "Error: from_agent required for report."
        # Deliver report to assigner's session
        if session_exists(name):
            send_keys(name, f"[REPORT from {from_agent}]: {text}")
        # Update assignments file
        af = data_dir() / "assignments.json"
        if af.exists():
            try:
                assignments = json.loads(af.read_text())
                for a in assignments.values():
                    if a.get("session") == from_agent and a.get("from") == name and a.get("status") == "pending":
                        a["status"] = "completed"
                        a["report"] = text[:500]
                        a["completed_at"] = datetime.now(timezone.utc).isoformat()
                af.write_text(json.dumps(assignments, indent=2) + "\n")
            except (json.JSONDecodeError, OSError):
                pass
        log = _message_log()
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with open(log, "a") as f:
            f.write(f"{ts} REPORT {from_agent}→{name}: {text[:200]}\n")
        return f"Report delivered to '{name}'."

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


# ── puppet_sentinel (read-only) ────────────────────────────────────────

def _resolve_caller_name() -> str:
    """Auto-resolve caller name: tmux session → settings.json agent → 'default'."""
    # Try tmux pane ancestry
    try:
        from .tmux import run_tmux as _run_tmux
        import subprocess
        result = _run_tmux(["list-panes", "-a", "-F", "#{pane_pid} #{session_name}"])
        if result.returncode == 0 and result.stdout.strip():
            pane_map = {}
            for line in result.stdout.strip().split("\n"):
                parts = line.strip().split(None, 1)
                if len(parts) == 2:
                    pane_map[parts[0]] = parts[1]
            pid = os.getpid()
            visited = set()
            while pid and pid > 1 and pid not in visited:
                visited.add(pid)
                if str(pid) in pane_map:
                    return pane_map[str(pid)]
                try:
                    r = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
                    pid = int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else 0
                except (ValueError, subprocess.TimeoutExpired, OSError):
                    break
    except Exception:
        pass
    proj = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if proj:
        from pathlib import Path
        settings = Path(proj) / ".claude" / "settings.json"
        if settings.exists():
            try:
                agent = json.loads(settings.read_text()).get("agent", "")
                if agent:
                    return agent
            except (json.JSONDecodeError, OSError):
                pass
    return "default"


@mcp.tool(structured_output=False)
def puppet_sentinel(action: str = "poll", name: str = "") -> str:
    """Sentinel events (read-only). Workers can poll their own events and check status.

    Args:
        action: "poll" (default) or "status"
        name: subscriber name (auto-resolved if empty)
    """
    if action not in ("poll", "status"):
        return f"Workers can only poll or check status, not {action}."

    if action == "status":
        info = sentinel_status(name or "")
        lines = [f"running: {info.get('running', False)}"]
        if info.get("pid"):
            lines.append(f"pid: {info['pid']}")
        subs = info.get("subscribers", {})
        if subs:
            lines.append(f"subscribers ({len(subs)}):")
            for sub_name, sub_info in subs.items():
                depth = sub_info.get("queue_depth", 0)
                lines.append(f"  {sub_name}: {depth} queued, interests={sub_info.get('interests', '?')}")
        return "\n".join(lines)

    # poll
    name = name or _resolve_caller_name()
    events = poll_subscriber(name)
    if not events:
        return ""
    lines = []
    for ev in events:
        ts = ev.get("time", "")
        if ts:
            ts = ts.split("T")[-1][:8]
        etype = ev.get("type", "?")
        session = ev.get("session", "?")
        detail = ev.get("detail", "")
        lines.append(f"{ts} {etype} {session} — {detail}")
    return "\n".join(lines)


# ── main ────────────────────────────────────────────────────────────────

def main():
    mcp.run()


if __name__ == "__main__":
    main()
