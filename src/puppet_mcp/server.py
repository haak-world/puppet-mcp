"""puppet MCP server — full remote control for Claude Code sessions via tmux.

Tools (10):
  puppet_launch      — launch/resume/thaw/new sessions
  puppet_send        — all input: text, enter, escape, ctrl-c, slash commands
  puppet_handoff     — send prompt and wait for response (absorbs ping)
  puppet_status      — diff-based monitoring, full snapshot, single-session detail
  puppet_read        — raw pane output
  puppet_find        — search all sessions by metadata/content
  puppet_manage      — lifecycle: kill, freeze, restart, compact, split, accept_all
  sentinel_register  — subscribe to sentinel events (blocked, died, context, etc.)
  sentinel_poll      — read and clear queued sentinel events
  sentinel_unregister — remove a sentinel subscription
"""

import json
import os
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from . import console_url, data_dir, project_dir
from .compact import mechanical_prune, split_session
from .session import (
    SessionMap,
    discover_all_sessions,
    find_session_jsonl,
    get_claude_session_info,
    resolve_session_cwd,
    resolve_session_id,
)
from .tmux import (
    capture_pane,
    classify_activity,
    content_lines,
    create_session,
    detect_context_window,
    extract_permission_content,
    has_attached_client,
    is_idle,
    kill_session,
    list_sessions,
    parse_status_bar,
    run_tmux,
    send_key,
    send_keys,
    session_exists,
)

mcp = FastMCP("puppet")

_session_map = SessionMap()


def _self_context() -> dict | None:
    """Detect the calling session's own tmux session name and token count.

    Walks the process tree from this MCP server's PID upward until finding
    a process that's a child of a tmux pane. Maps that PID to a tmux session
    name, then reads the status bar for token count and context window.

    Returns {"name": "...", "tokens": N, "context_window": N} or None.
    """
    import subprocess

    # Get all tmux pane PIDs and their session names
    result = run_tmux(["list-panes", "-a", "-F", "#{pane_pid} #{session_name}"])
    if result.returncode != 0 or not result.stdout.strip():
        return None

    pane_map: dict[str, str] = {}  # pid_str -> session_name
    for line in result.stdout.strip().split("\n"):
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            pane_map[parts[0]] = parts[1]

    # Walk the process tree upward from our PID
    pid = os.getpid()
    visited = set()
    while pid and pid > 1 and pid not in visited:
        visited.add(pid)
        pid_str = str(pid)
        if pid_str in pane_map:
            session_name = pane_map[pid_str]
            pane = capture_pane(session_name, 15)
            bar = parse_status_bar(pane)
            tokens = bar.get("tokens")
            ctx = bar.get("context_window") or (detect_context_window(session_name, tokens=tokens) if tokens else None)
            return {
                "name": session_name,
                "tokens": tokens or 0,
                "context_window": ctx or 200_000,
            }
        # Get parent PID
        try:
            ppid_result = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5,
            )
            if ppid_result.returncode != 0 or not ppid_result.stdout.strip():
                break
            pid = int(ppid_result.stdout.strip())
        except (ValueError, subprocess.TimeoutExpired, OSError):
            break

    return None


def _message_log() -> Path:
    return data_dir() / "puppet-messages.log"


def _heartbeat_log() -> Path:
    return data_dir() / "puppet-heartbeat.log"


def _log_heartbeat(message: str):
    log = _heartbeat_log()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(log, "a") as f:
        f.write(f"{ts} {message}\n")


def _state_file() -> Path:
    return data_dir() / "puppet-state.json"


def _load_prev_state() -> dict:
    sf = _state_file()
    if sf.exists():
        try:
            return json.loads(sf.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_state(state: dict):
    sf = _state_file()
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps(state, indent=2) + "\n")


def _snapshot_sessions() -> dict[str, dict]:
    """Capture current state of all tmux sessions. Returns {name: info}."""
    result = run_tmux(["ls"])
    if result.returncode != 0:
        return {}

    smap = _session_map.load()
    snapshot = {}

    for tmux_line in result.stdout.strip().split("\n"):
        if not tmux_line.strip():
            continue
        name = tmux_line.split(":")[0].strip()
        pane = capture_pane(name, 15)
        bar = parse_status_bar(pane)
        activity = classify_activity(pane, tmux_line, tmux_name=name)
        tokens = bar.get("tokens")
        map_entry = smap.get(name, {})
        agent = bar.get("agent") or map_entry.get("agent") or "?"
        sid = map_entry.get("session_id", "")

        tail = content_lines(pane, 1)

        ctx = None
        if tokens:
            ctx = bar.get("context_window") or detect_context_window(name, tokens=tokens)

        snapshot[name] = {
            "activity": activity,
            "tokens": tokens,
            "agent": agent,
            "sid": sid,
            "context_window": ctx,
            "tail": tail[0] if tail else "",
        }

    return snapshot


def _diff_states(prev: dict, curr: dict) -> list[str]:
    """Compare previous and current snapshots. Return list of noteworthy changes."""
    diffs = []
    prev_names = set(prev.get("sessions", {}).keys())
    curr_names = set(curr.keys())

    for name in sorted(curr_names - prev_names):
        s = curr[name]
        diffs.append(f"+ {name}: agent={s['agent']} [{s['activity']}]")

    for name in sorted(prev_names - curr_names):
        diffs.append(f"- {name}: gone")

    for name in sorted(curr_names & prev_names):
        c = curr[name]
        p = prev["sessions"][name]

        ct = c.get("tokens") or 0
        pt = p.get("tokens") or 0
        activity_changed = c["activity"] != p.get("activity")
        tokens_changed = ct != pt and ct and pt

        if not activity_changed and not tokens_changed:
            continue

        if activity_changed:
            diffs.append(f"~ {name}: {p.get('activity')} → {c['activity']}")

        if tokens_changed:
            delta = ct - pt
            sign = "+" if delta > 0 else ""
            diffs.append(f"  {name}: {pt:,} → {ct:,} tokens ({sign}{delta:,})")

        if c.get("context_window") and ct:
            pct = ct / c["context_window"]
            prev_pct = pt / c["context_window"] if pt else 0
            if pct > 0.8 and prev_pct <= 0.8:
                diffs.append(f"⚠ {name}: {int(pct*100)}% context — upgrade to 1M")

    return diffs


def _try_register_session(name: str, parent: str = "", role: str = "") -> str | None:
    """Poll PID chain to discover and record session ID after launch."""
    for _ in range(5):
        info = get_claude_session_info(name)
        if info:
            _session_map.record(name, info["session_id"], info.get("agent", ""), info.get("cwd", ""), parent=parent, role=role)
            console = console_url()
            if console:
                try:
                    import subprocess
                    subprocess.run([
                        "curl", "-sf", "-X", "PUT",
                        f"{console}/api/sessions/{info['session_id']}/name",
                        "-H", "Content-Type: application/json",
                        "-d", json.dumps({"name": name}),
                    ], capture_output=True, timeout=5)
                except Exception:
                    pass
            return info["session_id"]
        time.sleep(3)
    return None


def _mcp_config_path() -> str | None:
    """Return the path to puppet-orchestrator.json if it exists."""
    env_path = os.environ.get("PUPPET_ORCHESTRATOR_CONFIG", "")
    if env_path:
        p = Path(env_path).expanduser()
        if p.exists():
            return str(p)

    pkg_dir = Path(__file__).resolve().parent.parent.parent
    candidate = pkg_dir / "puppet-orchestrator.json"
    if candidate.exists():
        return str(candidate)

    return None


def _sentinel_pid_file() -> Path:
    return data_dir() / "sentinel.pid"


def _ensure_sentinel():
    """Start puppet-sentinel if not already running."""
    import subprocess as _sp
    pid_file = _sentinel_pid_file()
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            return
        except (ValueError, OSError):
            pass
    sentinel = Path(__file__).resolve().parent.parent.parent.parent.parent / "scripts" / "puppet-sentinel"
    if not sentinel.exists():
        return
    proc = _sp.Popen(
        [str(sentinel)],
        stdout=open(data_dir() / "sentinel.log", "a"),
        stderr=_sp.STDOUT,
        start_new_session=True,
    )
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(proc.pid) + "\n")


# ── 1. puppet_launch (unchanged) ────────────────────────────────────────

@mcp.tool(structured_output=False)
def puppet_launch(
    name: str,
    agent: str = "",
    prompt: str = "",
    resume: str = "",
    model: str = "opus",
    max_context: str = "",
    budget: float = 5.0,
    unsafe: bool = False,
    role: str = "worker",
    parent: str = "",
    new: bool = False,
) -> str:
    """Launch, attach, or thaw a Claude Code session.

    Behavior depends on session state:
      - Active tmux session exists → return "already active" message
      - Frozen session exists → thaw (resume) it
      - No session → create fresh

    Pass new=True to force a fresh session even if a frozen one exists.
    Pass resume=SESSION_ID to resume a specific historical session.

    Args:
        name: tmux session name (e.g. "worker-reed")
        agent: agent name for new sessions (e.g. "reed", "lina")
        prompt: initial prompt for new sessions
        resume: Claude Code session ID to resume instead of starting fresh
        model: model to use (default "opus")
        max_context: context window, e.g. "1m" (default "" for standard)
        budget: max spend in USD (default 5.0)
        unsafe: if True, use --dangerously-skip-permissions (default False)
        role: "worker" (default) or "orchestrator"
        parent: tmux session name of the parent (for hierarchy tracking)
        new: if True, force fresh session (ignore frozen)
    """
    if session_exists(name):
        return f"Session '{name}' is already active. Use puppet_read to see it."

    if not new and not resume:
        frozen_entry = _session_map.get(name)
        if frozen_entry and frozen_entry.get("status") == "frozen":
            resume = frozen_entry["session_id"]
            agent = agent or frozen_entry.get("agent", "")
            role = frozen_entry.get("role", role)

    if resume:
        cwd = resolve_session_cwd(resume, _session_map, default=project_dir())
        claude_cmd = f"cd {cwd} && claude --resume {resume}"
    elif agent:
        model_str = f"{model}[{max_context}]" if max_context else model
        claude_cmd = f"cd {project_dir()} && claude --agent {agent} --model {model_str} --max-budget-usd {budget}"
        if unsafe:
            claude_cmd += " --dangerously-skip-permissions"
    else:
        return "Error: provide either agent+prompt (new session) or resume=SESSION_ID."

    if role == "orchestrator":
        orch_config = _mcp_config_path()
        if orch_config:
            claude_cmd += f" --mcp-config {orch_config}"

    result = create_session(name)
    if result.returncode != 0:
        return f"Error creating tmux session: {result.stderr.strip()}"
    time.sleep(0.5)
    send_keys(name, claude_cmd)

    time.sleep(12)

    if resume:
        original_agent = ""
        jsonl = find_session_jsonl(resume, _session_map)
        if jsonl:
            try:
                with open(jsonl) as f:
                    for line in f:
                        d = json.loads(line.strip())
                        if d.get("type") == "agent-setting":
                            original_agent = d.get("agentSetting", "")
                            break
            except (json.JSONDecodeError, OSError):
                pass

        record_agent = original_agent or agent
        _session_map.record(name, resume, record_agent, cwd, parent=parent, role=role)

        warn = ""
        if agent and original_agent and agent != original_agent:
            warn = (
                f"\n⚠ Agent mismatch: you requested '{agent}' but this session "
                f"was created with '{original_agent}'. Claude --resume uses the "
                f"original agent. Use /agent or start a new session to switch."
            )
        return f"Launched '{name}': resuming session {resume}, agent={record_agent}, cwd={cwd}, role={role}.{warn}"

    send_keys(name, prompt)
    session_id = _try_register_session(name, parent=parent, role=role)

    # Inject context self-monitoring instruction
    _CONTEXT_HYGIENE = (
        "CONTEXT HYGIENE: Monitor your token count in the status bar. "
        "When you reach 70% of your context window, pause current work, "
        "write a summary of decisions and findings to a file, then run "
        "/clear to reset. You keep your agent mandate but drop accumulated noise. "
        "Do not wait until 90% — by then compaction artifacts degrade quality."
    )
    time.sleep(2)
    send_keys(name, _CONTEXT_HYGIENE)

    if role == "orchestrator":
        _ensure_sentinel()

    sid_note = f", session={session_id}" if session_id else " (session ID pending)"
    mode = "UNSAFE (skip-permissions)" if unsafe else "safe (permission prompts enabled)"
    return (
        f"Launched '{name}': agent={agent}, model={model_str}, budget=${budget}, "
        f"mode={mode}, role={role}{sid_note}.\nPrompt sent."
    )


# ── 2. puppet_send ──────────────────────────────────────────────────────

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
                 Examples: "status", "/model sonnet", "compact".

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


# ── 3. puppet_handoff ───────────────────────────────────────────────────

@mcp.tool(structured_output=False)
def puppet_handoff(
    name: str,
    prompt: str = "",
    from_agent: str = "",
    timeout: int = 120,
) -> str:
    """Send a prompt and wait for the response. Round-trip in one call.

    If prompt is empty or a status-check pattern (e.g. "status", "ping"),
    behaves like a quick ping with a shorter default timeout (30s).

    Waits for the session to be idle (if currently working), sends the
    prompt, then polls every 2 seconds until the agent finishes and
    returns to idle. Returns only the new output.

    Args:
        name: tmux session name
        prompt: text to send. Empty or status-like = ping mode (30s timeout).
        from_agent: caller identity. Empty = no prefix (human-style).
        timeout: max seconds to wait for response (default 120)
    """
    if not session_exists(name):
        return f"Error: tmux session '{name}' does not exist."

    # Ping mode: empty prompt or status-check pattern
    ping_patterns = {"", "status", "ping", "what is your status?"}
    is_ping = prompt.strip().lower() in ping_patterns
    if is_ping:
        prompt = prompt or "What is your status? What have you produced so far? What are you blocked on?"
        from_agent = from_agent or "orchestrator"
        timeout = min(timeout, 30) if timeout == 120 else timeout

    # Wait for idle before sending (up to timeout/2)
    wait_budget = timeout // 2
    elapsed = 0
    while not is_idle(capture_pane(name, 5)) and elapsed < wait_budget:
        time.sleep(2)
        elapsed += 2
    if not is_idle(capture_pane(name, 5)):
        if is_ping:
            return f"Session '{name}' is working — cannot ping. Wait until idle."
        return f"Error: '{name}' still working after {elapsed}s — cannot send prompt."

    # Snapshot content before sending
    before = set(content_lines(capture_pane(name, 50), 50))

    # Send prompt
    msg = f"[{from_agent}→{name}]: {prompt}" if from_agent else prompt
    send_keys(name, msg)

    # Poll for completion
    time.sleep(2)
    elapsed = 2
    while elapsed < timeout:
        pane = capture_pane(name, 50)
        if is_idle(pane):
            after = content_lines(pane, 50)
            new_lines = [l for l in after if l not in before]
            if new_lines and (prompt in new_lines[0] or (from_agent and from_agent in new_lines[0])):
                new_lines = new_lines[1:]
            response = "\n".join(new_lines)
            return response if response else "(empty response)"
        time.sleep(2)
        elapsed += 2

    # Timeout
    pane = capture_pane(name, 50)
    after = content_lines(pane, 50)
    new_lines = [l for l in after if l not in before]
    if new_lines and (prompt in new_lines[0] or (from_agent and from_agent in new_lines[0])):
        new_lines = new_lines[1:]
    response = "\n".join(new_lines)
    return f"[timeout after {timeout}s — partial response]\n{response}"


# ── 4. puppet_status ────────────────────────────────────────────────────

@mcp.tool(structured_output=False)
def puppet_status(full: bool = False, name: str = "") -> str:
    """Status of sessions — diffs, full snapshot, or single-session detail.

    Modes:
      - No args: diff-based status of all sessions since last check.
        Reports new/killed sessions, activity transitions, token changes,
        context warnings. Blocked sessions always surfaced.
      - full=True: complete snapshot of all sessions regardless of changes.
      - name="X": detailed status of one session including permission
        prompt content if blocked.

    Activity classes: active, idle, stale, dead, blocked.

    Writes diffs to heartbeat log on every call for monitoring continuity.

    Args:
        full: if True, report all sessions regardless of changes
        name: if set, report detailed status of this single session
    """
    # Single-session mode
    if name:
        if not session_exists(name):
            entry = _session_map.get(name)
            if entry and entry.get("status") == "frozen":
                frozen_at = entry.get("frozen_at", "")[:10]
                return f"[frozen] {name}: agent={entry.get('agent', '?')} since {frozen_at}, session={entry.get('session_id', '?')}"
            return f"Error: tmux session '{name}' does not exist."
        pane = capture_pane(name, 15)
        bar = parse_status_bar(pane)
        result_tmux = run_tmux(["ls", "-f", f"#{{==:#{{{name}}},#{{session_name}}}}"])
        tmux_line = result_tmux.stdout.strip() if result_tmux.returncode == 0 else ""
        activity = classify_activity(pane, tmux_line, tmux_name=name)
        tokens = bar.get("tokens")
        map_entry = _session_map.get(name) or {}
        agent = bar.get("agent") or map_entry.get("agent") or "?"
        sid = map_entry.get("session_id", "")
        ctx = None
        if tokens:
            ctx = bar.get("context_window") or detect_context_window(name, tokens=tokens)
        tok_str = "?"
        if tokens and ctx:
            ctx_label = "1M" if ctx >= 1_000_000 else "200k"
            pct = int(tokens / ctx * 100)
            tok_str = f"{tokens:,}/{ctx_label} ({pct}%)"
        elif tokens:
            tok_str = f"{tokens:,}"
        tail = content_lines(pane, 3)
        lines = [f"[{activity}] {name}: agent={agent} tokens={tok_str} session={sid}"]
        if tail:
            lines.append("\n".join(tail))
        # Permission prompt detail
        if activity == "blocked":
            info = extract_permission_content(pane)
            if info:
                lines.append(f"\nPermission prompt:\n  Tool: {info['tool']}\n  Detail: {info['detail']}\n\n{info['raw']}")
        return "\n".join(lines)

    # Multi-session mode
    curr = _snapshot_sessions()
    if not curr:
        return "No tmux sessions running."

    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    prev = _load_prev_state()
    _save_state({"sessions": curr, "timestamp": ts})

    # Self-context prefix
    self_prefix = ""
    self_ctx = _self_context()
    if self_ctx:
        s_tok = self_ctx["tokens"]
        s_ctx = self_ctx["context_window"]
        s_label = "1M" if s_ctx >= 1_000_000 else "200k"
        s_pct = int(s_tok / s_ctx * 100) if s_ctx else 0
        s_tok_k = f"{s_tok // 1000}k"
        self_prefix = f"[{ts}] self: {s_tok_k}/{s_label} ({s_pct}%) | "

    # Log diffs to heartbeat log
    diffs = _diff_states(prev, curr) if prev.get("sessions") else []
    for d in diffs:
        _log_heartbeat(d)

    frozen = _session_map.frozen()

    if full or not prev.get("sessions"):
        blocked = [n for n, s in sorted(curr.items()) if s["activity"] == "blocked"]
        total = len(curr) + len(frozen)
        header = f"{total} sessions ({len(frozen)} frozen):" if frozen else f"{len(curr)} sessions:"
        if blocked:
            header += f" ⚠ {len(blocked)} BLOCKED"
        lines = [f"{self_prefix}{header}" if self_prefix else f"[{ts}] {header}"]
        for sname, s in sorted(curr.items()):
            ctx = s.get("context_window")
            tok = s.get("tokens")
            if tok and ctx:
                ctx_label = "1M" if ctx >= 1_000_000 else "200k"
                pct = int(tok / ctx * 100)
                tok_str = f"{tok:,}/{ctx_label} ({pct}%)"
            elif tok:
                tok_str = f"{tok:,}"
            else:
                tok_str = "?"
            warn = ""
            if s["activity"] == "blocked":
                warn = " ⚠ PERMISSION PROMPT"
            elif tok and ctx and tok / ctx > 0.8:
                warn = " ⚠ CONTEXT"
            lines.append(f"  [{s['activity']}] {sname}: agent={s['agent']} tokens={tok_str}{warn} — {s['tail']}")
        for sname, entry in sorted(frozen.items()):
            if sname not in curr:
                agent = entry.get("agent", "?")
                frozen_at = entry.get("frozen_at", "")[:10]
                lines.append(f"  [❄ frozen] {sname}: agent={agent} since {frozen_at}")
        return "\n".join(lines)

    # Diff mode
    blocked = [n for n, s in curr.items() if s["activity"] == "blocked"]
    if blocked and not any("blocked" in d.lower() or "PERMISSION" in d for d in diffs):
        for n in blocked:
            diffs.insert(0, f"⚠ {n}: BLOCKED ON PERMISSION PROMPT — needs puppet_send(action='enter')")

    if not diffs:
        active = sum(1 for s in curr.values() if s["activity"] == "active")
        idle = sum(1 for s in curr.values() if s["activity"] in ("idle", "stale"))
        summary = f"No changes. {active} active, {idle} idle."
        return f"{self_prefix}{summary}" if self_prefix else f"[{ts}] {summary}"

    change_str = f"{len(diffs)} change{'s' if len(diffs) != 1 else ''}:"
    header = f"{self_prefix}{change_str}" if self_prefix else f"[{ts}] {change_str}"
    return header + "\n" + "\n".join(f"  {d}" for d in diffs)


# ── 5. puppet_read ──────────────────────────────────────────────────────

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


# ── 6. puppet_find ──────────────────────────────────────────────────────

@mcp.tool(structured_output=False)
def puppet_find(
    query: str = "",
    scope: str = "",
    grep: str = "",
    hours: int = 24,
) -> str:
    """Search ALL Claude Code sessions — not just puppet-managed tmux ones.

    Reads ~/.claude/ directly to find every Claude Code session and its
    transcript. Supports filtering by metadata, directory scope, and
    content search.

    Args:
        query: filter by metadata — matches agent name, cwd, model, or topic
        scope: restrict to sessions in this directory tree.
               "." = current working directory. "" = all sessions.
        grep: search transcript content for this text (searches JSONL)
        hours: how far back to look (default 24). 0 = no time limit.
    """
    sessions = discover_all_sessions(hours=hours, scope=scope, grep=grep)
    if not sessions:
        what = f" matching '{grep or query}'" if (grep or query) else ""
        where = f" in {scope}" if scope else ""
        return f"No sessions found{what}{where}."

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


# ── 7. puppet_manage ────────────────────────────────────────────────────

@mcp.tool(structured_output=False)
def puppet_manage(
    name: str = "",
    action: str = "freeze",
    topics: list[str] | None = None,
    summary: str = "",
    force: bool = False,
) -> str:
    """Lifecycle operations for puppet sessions.

    Actions:
      "kill"       — Kill tmux session, remove from map. Requires name.
                     force=True to override attached client warning.
      "freeze"     — Stop tmux session, keep in map as frozen (restorable
                     via puppet_launch). Requires name.
      "restart"    — Kill + resume with --resume, preserving full context.
                     Requires name. force=True to override attached client.
      "compact"    — Prune session JSONL transcript (backup first).
                     Requires name. summary= optional note to prepend.
      "split"      — Split session transcript by topics into resumable
                     per-topic sessions. Requires name and topics list.
      "accept_all" — Accept all blocked permission prompts across ALL
                     sessions. name is ignored. Logs what was accepted.

    Args:
        name: tmux session name (required for all actions except accept_all)
        action: one of "kill", "freeze", "restart", "compact", "split", "accept_all"
        topics: list of topic labels for "split" action
        summary: optional summary note for "compact" action
        force: override safety checks for "kill" and "restart"
    """

    # ── accept_all ──
    if action == "accept_all":
        names = list_sessions()
        if not names:
            return "No tmux sessions running."
        accepted = []
        for sname in names:
            pane = capture_pane(sname, 15)
            info = extract_permission_content(pane)
            if not info:
                continue
            log = _message_log()
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(log, "a") as f:
                f.write(f"{ts} AUTO-ACCEPT {sname}: {info['tool']} {info['detail']}\n")
            send_key(sname, "Enter")
            accepted.append(f"{sname} ({info['tool']}: {info['detail'][:60]})")
        if not accepted:
            return "No sessions blocked on permission prompts."
        return f"Accepted {len(accepted)} sessions:\n" + "\n".join(f"  {a}" for a in accepted)

    # All remaining actions require name
    if not name:
        return "Error: name is required for this action."

    # ── kill ──
    if action == "kill":
        if not session_exists(name):
            return f"Error: tmux session '{name}' does not exist."
        if not force and has_attached_client(name):
            return f"Warning: a user terminal is attached to '{name}'. Pass force=True to kill anyway."
        result = kill_session(name)
        if result.returncode != 0:
            return f"Error killing '{name}': {result.stderr.strip()}"
        _session_map.remove(name)
        return f"Killed session '{name}'."

    # ── freeze ──
    if action == "freeze":
        if not session_exists(name):
            entry = _session_map.get(name)
            if entry and entry.get("status") == "frozen":
                return f"'{name}' is already frozen."
            return f"Error: tmux session '{name}' does not exist."
        info = resolve_session_id(name, _session_map)
        if not info:
            return f"Error: could not resolve session ID for '{name}'."
        result = kill_session(name)
        if result.returncode != 0:
            return f"Error stopping '{name}': {result.stderr.strip()}"
        _session_map.freeze(name)
        return f"Frozen '{name}': session={info['session_id']}. Use puppet_launch to thaw."

    # ── restart ──
    if action == "restart":
        if not session_exists(name):
            return f"Error: tmux session '{name}' does not exist."
        if not force and has_attached_client(name):
            return f"Warning: a user terminal is attached to '{name}'. Pass force=True to restart anyway."
        info = resolve_session_id(name, _session_map)
        if not info:
            return (
                f"Error: could not find Claude Code session ID for '{name}'. "
                f"Is claude running inside this tmux session?"
            )
        session_id = info["session_id"]
        cwd = info.get("cwd") or resolve_session_cwd(session_id, _session_map, default=project_dir())
        agent = info.get("agent", "")

        kill_result = kill_session(name)
        if kill_result.returncode != 0:
            return f"Error killing '{name}': {kill_result.stderr.strip()}"
        time.sleep(1)

        create_result = create_session(name)
        if create_result.returncode != 0:
            return (
                f"Killed '{name}' but failed to recreate: {create_result.stderr.strip()}. "
                f"Session ID for manual resume: {session_id}"
            )
        time.sleep(1)
        send_keys(name, f"cd {cwd} && claude --resume {session_id}")

        old_entry = _session_map.get(name)
        parent = old_entry.get("parent", "") if old_entry else ""
        _session_map.record(name, session_id, agent, cwd, parent=parent)

        # Gap context
        gap_lines = []
        map_entry = _session_map.get(name)
        if map_entry and map_entry.get("launched_at"):
            try:
                from datetime import datetime as dt
                stopped_at = dt.fromisoformat(map_entry["launched_at"])
                gap = datetime.now(timezone.utc) - stopped_at
                gap_lines.append(f"Session was stopped and restarted. Gap: {int(gap.total_seconds())}s.")
            except (ValueError, TypeError):
                pass
        try:
            import subprocess
            git_result = subprocess.run(
                ["git", "-C", cwd, "log", "--oneline", "-5", "--since=1 hour ago"],
                capture_output=True, text=True, timeout=5,
            )
            if git_result.returncode == 0 and git_result.stdout.strip():
                commits = git_result.stdout.strip().split("\n")
                gap_lines.append(f"{len(commits)} recent commits: {'; '.join(c[:50] for c in commits[:3])}")
        except Exception:
            pass

        if gap_lines:
            time.sleep(10)
            context_msg = "[restart-hook]: " + " | ".join(gap_lines)
            send_keys(name, context_msg)

        agent_info = f", agent={agent}" if agent else ""
        return (
            f"Restarted '{name}': session={session_id}{agent_info}. "
            f"Agent resumes with full context + fresh config."
        )

    # ── compact ──
    if action == "compact":
        jsonl_path = find_session_jsonl(name, _session_map)
        if jsonl_path is None:
            return f"Error: could not find session transcript for '{name}'."

        entries = []
        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

        original_size = jsonl_path.stat().st_size
        original_count = len(entries)
        pruned = mechanical_prune(entries, keep_resumable=True)

        if summary:
            # Insert after header entries to preserve resume order
            insert_at = 0
            for j, e in enumerate(pruned):
                if e.get("type") in ("agent-setting", "permission-mode"):
                    insert_at = j + 1
                else:
                    break
            pruned.insert(insert_at, {
                "type": "compact-summary",
                "summary": summary,
                "compacted_at": datetime.now(timezone.utc).isoformat(),
                "original_file": str(jsonl_path),
                "original_entries": original_count,
            })

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = jsonl_path.with_suffix(f".pre-compact-{timestamp}.jsonl")
        shutil.copy2(jsonl_path, backup_path)

        with open(jsonl_path, "w") as f:
            for entry in pruned:
                f.write(json.dumps(entry) + "\n")

        compact_size = jsonl_path.stat().st_size
        reduction = (1 - compact_size / original_size) * 100 if original_size > 0 else 0

        return (
            f"Compacted '{name}':\n"
            f"  Original: {original_count} entries, {original_size:,} bytes\n"
            f"  Compacted: {len(pruned)} entries, {compact_size:,} bytes\n"
            f"  Reduction: {reduction:.1f}%\n"
            f"  Backup: {backup_path}\n"
            f"  File: {jsonl_path}"
        )

    # ── split ──
    if action == "split":
        if not topics:
            return "Error: topics list required for split action."
        jsonl_path = find_session_jsonl(name, _session_map)
        if jsonl_path is None:
            return f"Error: could not find session transcript for '{name}'."
        try:
            results = split_session(jsonl_path, topics)
        except Exception as e:
            return f"Error splitting session: {e}"

        lines = [f"Split '{name}' into {len([k for k in results if not k.startswith('_')])} topic sessions:"]
        for topic, info in sorted(results.items()):
            if topic == "_suggestions":
                continue
            lines.append(
                f"  {topic}: {info['rounds']} rounds, sid={info['session_id']}\n"
                f"    {info['path']}"
            )
        suggestions = results.get("_suggestions", [])
        if suggestions:
            lines.append(f"\nSuggested additional topics: {', '.join(suggestions)}")
        return "\n".join(lines)

    return f"Error: unknown action '{action}'. Use kill, freeze, restart, compact, split, or accept_all."


# ── 8. sentinel_register ──────────────────────────────────────────────

_ALL_EVENTS = ['blocked', 'unblocked', 'died', 'exited', 'completed', 'new_session', 'context_70', 'context_85', 'stale']

_INTEREST_MAP = {
    'block': ['blocked', 'unblocked'],
    'stuck': ['blocked'],
    'permission': ['blocked'],
    'context': ['context_70', 'context_85'],
    'warn': ['context_70', 'context_85'],
    'die': ['died'],
    'death': ['died'],
    'dead': ['died'],
    'exit': ['exited'],
    'complete': ['completed'],
    'finish': ['completed'],
    'idle': ['completed'],
    'new': ['new_session'],
    'spawn': ['new_session'],
    'stale': ['stale'],
    'all': _ALL_EVENTS,
    'everything': _ALL_EVENTS,
    'fleet': ['fleet_summary'],
    'summary': ['fleet_summary'],
    'status': ['fleet_summary'],
}


def _subscriptions_file() -> Path:
    return data_dir() / "subscriptions.json"


def _queue_dir() -> Path:
    d = data_dir() / "queues"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_subscriptions() -> dict:
    sf = _subscriptions_file()
    if sf.exists():
        try:
            return json.loads(sf.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_subscriptions(subs: dict):
    import tempfile
    sf = _subscriptions_file()
    sf.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(mode="w", dir=sf.parent, suffix=".tmp", delete=False)
    try:
        tmp.write(json.dumps(subs, indent=2) + "\n")
        tmp.close()
        os.replace(tmp.name, sf)
    except Exception:
        tmp.close()
        Path(tmp.name).unlink(missing_ok=True)
        raise


def _parse_interests(text: str) -> list[str]:
    words = text.lower().replace(",", " ").replace(";", " ").split()
    filters = set()
    for word in words:
        for key, events in _INTEREST_MAP.items():
            if key in word:
                filters.update(events)
    if not filters:
        filters.update(_ALL_EVENTS)
    return sorted(filters)


def _parse_cadence(cadence: str) -> str:
    c = cadence.strip().lower()
    if c in ("", "immediate", "0"):
        return "immediate"
    return c


@mcp.tool(structured_output=False)
def sentinel_register(name: str, interests: str, cadence: str = "immediate") -> str:
    """Register for sentinel event notifications via pub/sub.

    The sentinel daemon monitors all tmux sessions and queues matching
    events for registered subscribers. Use sentinel_poll to read events.

    Args:
        name: subscriber identifier (e.g. "sakshi", "inscription-guardian")
        interests: natural language description of what to monitor.
            Keywords: block/stuck/permission, context/warn, die/death/dead,
            exit, complete/finish/idle, new/spawn, stale, fleet/summary/status,
            all/everything. Unrecognized text defaults to all events.
        cadence: "immediate" (queue on event) or duration like "1m", "5m"
            (also generate periodic fleet summary)
    """
    filters = _parse_interests(interests)
    parsed_cadence = _parse_cadence(cadence)
    now = datetime.now(timezone.utc).isoformat()

    subs = _load_subscriptions()
    subs[name] = {
        "interests": interests,
        "filters": filters,
        "cadence": parsed_cadence,
        "registered_at": now,
        "last_summary": now,
    }
    _save_subscriptions(subs)

    filter_str = ", ".join(filters)
    return f"Registered: {filter_str}. Cadence: {parsed_cadence}."


# ── 9. sentinel_poll ──────────────────────────────────────────────────

@mcp.tool(structured_output=False)
def sentinel_poll(name: str) -> str:
    """Read and clear queued sentinel events for a subscriber.

    Returns formatted events (one per line) or empty string if none.
    Events are cleared after reading.

    Args:
        name: subscriber identifier (must match a sentinel_register name)
    """
    import tempfile
    queue_file = _queue_dir() / f"{name}.json"
    if not queue_file.exists():
        return ""

    # Atomic read-and-clear: read, then truncate to empty array
    try:
        raw = queue_file.read_text()
        events = json.loads(raw) if raw.strip() else []
    except (json.JSONDecodeError, OSError):
        events = []

    if not events:
        return ""

    # Clear by atomic write of empty array
    tmp = tempfile.NamedTemporaryFile(mode="w", dir=queue_file.parent, suffix=".tmp", delete=False)
    try:
        tmp.write("[]\n")
        tmp.close()
        os.replace(tmp.name, queue_file)
    except Exception:
        tmp.close()
        Path(tmp.name).unlink(missing_ok=True)

    lines = []
    for ev in events:
        t = ev.get("time", "")
        ts = t[11:19] if len(t) >= 19 else t
        etype = ev.get("type", "?").upper()
        session = ev.get("session", "")
        detail = ev.get("detail", "")
        if session:
            lines.append(f"{ts} {etype} {session} — {detail}")
        else:
            lines.append(f"{ts} {etype} — {detail}")
    return "\n".join(lines)


# ── 10. sentinel_unregister ───────────────────────────────────────────

@mcp.tool(structured_output=False)
def sentinel_unregister(name: str) -> str:
    """Remove a sentinel subscription and delete its event queue.

    Args:
        name: subscriber identifier to remove
    """
    subs = _load_subscriptions()
    removed = name in subs
    subs.pop(name, None)
    _save_subscriptions(subs)

    queue_file = _queue_dir() / f"{name}.json"
    queue_file.unlink(missing_ok=True)

    return "Unregistered." if removed else f"No subscription found for '{name}'."


# ── main ────────────────────────────────────────────────────────────────

def main():
    mcp.run()


if __name__ == "__main__":
    main()
