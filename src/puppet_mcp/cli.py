"""puppet — CLI for managing Claude Code sessions via tmux.

Click-based replacement for the bash puppet script. Imports from
puppet_mcp modules directly rather than shelling out.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import click

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
    has_claude_process,
    is_idle,
    kill_session,
    list_sessions,
    parse_status_bar,
    run_tmux,
    send_key,
    send_keys,
    session_exists,
)


def _message_log() -> Path:
    return data_dir() / "puppet-messages.log"


def _session_map() -> SessionMap:
    return SessionMap()


def _log_message(text: str):
    log = _message_log()
    log.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(log, "a") as f:
        f.write(f"{ts} {text}\n")


def _try_register_session(name: str, smap: SessionMap, parent: str = "", role: str = "") -> str | None:
    """Poll PID chain to discover and register session ID after launch."""
    for _ in range(5):
        info = get_claude_session_info(name)
        if info:
            smap.record(name, info["session_id"], info.get("agent", ""), info.get("cwd", ""), parent=parent, role=role)
            curl_console = console_url()
            if curl_console:
                import subprocess
                try:
                    subprocess.run([
                        "curl", "-sf", "-X", "PUT",
                        f"{curl_console}/api/sessions/{info['session_id']}/name",
                        "-H", "Content-Type: application/json",
                        "-d", json.dumps({"name": name}),
                    ], capture_output=True, timeout=5)
                except Exception:
                    pass
            return info["session_id"]
        time.sleep(3)
    return None


def _set_terminal_title(name: str):
    """Set terminal title via escape sequence."""
    sys.stdout.write(f"\033]0;puppet: {name}\007")
    sys.stdout.flush()


def _attach(name: str):
    """Set terminal title and exec tmux attach (replaces process)."""
    _set_terminal_title(name)
    os.execlp("tmux", "tmux", "attach", "-t", name)


# ── CLI group ────────────────────────────────────────────────────────

@click.group()
def cli():
    """puppet — manage Claude Code sessions via tmux."""


# ── 1. launch ────────────────────────────────────────────────────────

@cli.command()
@click.argument("name")
@click.option("--agent", "-a", default="", help="Agent name (default: same as NAME)")
@click.option("--model", "-m", default="opus", help="Model name")
@click.option("--context", default="", help="Context window, e.g. 1m")
@click.option("--role", "-r", default="worker", type=click.Choice(["worker", "orchestrator"]))
@click.option("--resume", default="", help="Resume session by ID")
@click.option("--budget", "-b", default=5.0, help="Max spend in USD")
@click.option("--cwd", "-d", default="", help="Working directory")
@click.option("--prompt", "-p", default="", help="Initial prompt")
@click.option("--new", "force_new", is_flag=True, help="Force fresh session (ignore frozen)")
@click.option("--unsafe", is_flag=True, help="Skip permission prompts")
@click.option("--detach", is_flag=True, help="Don't attach — leave running in background")
def launch(name, agent, model, context, role, resume, budget, cwd, prompt, force_new, unsafe, detach):
    """Launch, resume, or attach to a session."""
    smap = _session_map()
    agent = agent or name
    cwd = cwd or project_dir()

    # If tmux session already exists, just attach
    if session_exists(name):
        click.echo(f"Session '{name}' exists. Attaching...")
        _attach(name)
        return

    # Check for frozen session -> thaw it (unless --new)
    if not force_new and not resume:
        entry = smap.get(name)
        if entry and entry.get("status") == "frozen":
            frozen_sid = entry.get("session_id", "")
            if frozen_sid:
                click.echo(f"Thawing frozen session '{name}' (session={frozen_sid})...")
                resume = frozen_sid
                frozen_agent = entry.get("agent", "")
                if frozen_agent:
                    agent = frozen_agent

    # If no --resume, search for resumable sessions
    if not resume and agent == name:
        sessions = discover_all_sessions(hours=72)
        q = name.lower()
        hits = [
            s for s in sessions
            if q in (s.get("agent") or "").lower()
            or q in (s.get("topic") or "").lower()
            or q in (s.get("session_id") or "").lower()
            or q in (s.get("cwd") or "").lower()
        ]
        if hits:
            click.echo(f"Found existing sessions matching '{name}':")
            for i, s in enumerate(hits[:8]):
                s_agent = s.get("agent") or "?"
                sid = s.get("session_id", "?")
                ex = s.get("exchanges", "?")
                sz = s.get("size_mb", "?")
                run = "running" if s.get("is_running") else "stopped"
                topic = (s.get("topic") or "")[:60]
                click.echo(f"  {i + 1}) [{run}] {s_agent} {ex}x {sz}MB sid={sid} -- {topic}")
            click.echo("")
            click.echo("  n) Start fresh session")
            click.echo("")
            choice = click.prompt("Resume which?", default="n")
            if choice.lower() != "n":
                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(hits[:8]):
                        chosen = hits[idx]
                        resume = chosen.get("session_id", "")
                        found_agent = chosen.get("agent", "")
                        if found_agent:
                            agent = found_agent
                        click.echo(f"Resuming session {resume[:12]}...")
                except (ValueError, IndexError):
                    pass

    # Build the claude command
    if resume:
        if cwd == project_dir():
            cwd = resolve_session_cwd(resume, smap, default=project_dir())
        claude_cmd = f"cd {cwd} && claude --resume {resume}"
        click.echo(f"Launching '{name}': resuming session {resume[:12]}... (cwd: {cwd})")
    elif agent:
        model_str = f"{model}[{context}]" if context else model
        claude_cmd = f"cd {cwd} && claude --agent {agent} --model {model_str} --max-budget-usd {budget}"
        if unsafe:
            claude_cmd += " --dangerously-skip-permissions"
        click.echo(f"Launching '{name}': agent={agent}, model={model_str}, role={role}, budget=${budget}")
    else:
        click.echo("Error: provide either agent or resume.", err=True)
        raise SystemExit(1)

    # Orchestrator role gets puppet MCP config
    if role == "orchestrator":
        pkg_dir = Path(__file__).resolve().parent.parent.parent
        orch_config = pkg_dir / "puppet-orchestrator.json"
        if orch_config.exists():
            claude_cmd += f" --mcp-config {orch_config}"

    # Create tmux session
    result = create_session(name)
    if result.returncode != 0:
        click.echo(f"Error creating tmux session: {result.stderr.strip()}", err=True)
        raise SystemExit(1)
    time.sleep(0.5)
    send_keys(name, claude_cmd)

    # If resuming, register and attach
    if resume:
        smap.record(name, resume, agent, cwd, role=role)
        if not detach:
            _attach(name)
        else:
            click.echo(f"Session '{name}' running (detached). Attach with: puppet attach {name}")
        return

    click.echo("Waiting for Claude to initialize...")
    time.sleep(12)

    if prompt:
        send_keys(name, prompt)
        click.echo("Prompt sent.")

    session_id = _try_register_session(name, smap, role=role)
    if session_id:
        click.echo(f"Registered: session={session_id}")

    if not detach:
        _attach(name)
    else:
        click.echo(f"Session '{name}' running (detached). Attach with: puppet attach {name}")


# ── 2. send ──────────────────────────────────────────────────────────

@cli.command()
@click.argument("name")
@click.argument("text", nargs=-1)
@click.option("--action", default="text", type=click.Choice(["text", "enter", "escape", "ctrl-c", "slash"]))
@click.option("--from", "from_agent", default="", help="Caller identity for text action")
def send(name, text, action, from_agent):
    """Send input to a session."""
    if not session_exists(name):
        click.echo(f"Error: session '{name}' does not exist.", err=True)
        raise SystemExit(1)

    text_str = " ".join(text)

    if action == "enter":
        send_key(name, "Enter")
        click.echo(f"Accepted permission prompt in '{name}'.")
    elif action == "escape":
        send_key(name, "Escape")
        click.echo(f"Sent Escape to '{name}'.")
    elif action == "ctrl-c":
        send_key(name, "C-c")
        click.echo(f"Sent Ctrl+C to '{name}'.")
    elif action == "slash":
        if not text_str:
            click.echo("Error: text required for slash action.", err=True)
            raise SystemExit(1)
        cmd = text_str if text_str.startswith("/") else f"/{text_str}"
        send_keys(name, cmd)
        click.echo(f"Sent '{cmd}' to '{name}'.")
    else:
        if not text_str:
            click.echo("Error: text required for send.", err=True)
            raise SystemExit(1)
        if from_agent:
            send_keys(name, f"[{from_agent}→{name}]: {text_str}")
            _log_message(f"{from_agent}→{name}: {text_str}")
        else:
            send_keys(name, text_str)
        click.echo(f"Sent to '{name}'.")


# ── 3. handoff ───────────────────────────────────────────────────────

@cli.command()
@click.argument("name")
@click.argument("prompt")
@click.option("--timeout", default=120, help="Max seconds to wait")
@click.option("--from", "from_agent", default="", help="Caller identity")
def handoff(name, prompt, timeout, from_agent):
    """Send a prompt and wait for the response."""
    if not session_exists(name):
        click.echo(f"Error: session '{name}' does not exist.", err=True)
        raise SystemExit(1)

    # Wait for idle
    wait_budget = timeout // 2
    elapsed = 0
    while not is_idle(capture_pane(name, 5)) and elapsed < wait_budget:
        time.sleep(2)
        elapsed += 2
    if not is_idle(capture_pane(name, 5)):
        click.echo(f"Error: '{name}' still working after {elapsed}s.", err=True)
        raise SystemExit(1)

    before = set(content_lines(capture_pane(name, 50), 50))

    msg = f"[{from_agent}→{name}]: {prompt}" if from_agent else prompt
    send_keys(name, msg)

    time.sleep(2)
    elapsed = 2
    while elapsed < timeout:
        pane = capture_pane(name, 50)
        if is_idle(pane):
            after = content_lines(pane, 50)
            new_lines = [line for line in after if line not in before]
            if new_lines and (prompt in new_lines[0] or (from_agent and from_agent in new_lines[0])):
                new_lines = new_lines[1:]
            response = "\n".join(new_lines)
            click.echo(response if response else "(empty response)")
            return
        time.sleep(2)
        elapsed += 2

    pane = capture_pane(name, 50)
    after = content_lines(pane, 50)
    new_lines = [line for line in after if line not in before]
    if new_lines and (prompt in new_lines[0] or (from_agent and from_agent in new_lines[0])):
        new_lines = new_lines[1:]
    response = "\n".join(new_lines)
    click.echo(f"[timeout after {timeout}s — partial response]", err=True)
    click.echo(response)
    raise SystemExit(1)


# ── 4. status ────────────────────────────────────────────────────────

@cli.command()
@click.option("--full", is_flag=True, help="Full snapshot of all sessions")
@click.option("--name", default="", help="Detailed status of one session")
def status(full, name):
    """Status of sessions — diffs, full snapshot, or single-session detail."""
    from .server import puppet_status
    click.echo(puppet_status(full=full, name=name))


# ── 5. read ──────────────────────────────────────────────────────────

@cli.command()
@click.argument("name")
@click.option("--lines", "-n", default=30, help="Number of lines to capture")
def read(name, lines):
    """Read the last N lines from a session's pane."""
    if not session_exists(name):
        click.echo(f"Error: session '{name}' does not exist.", err=True)
        raise SystemExit(1)
    output = capture_pane(name, lines)
    click.echo(output if output else "(empty pane)")


# ── 6. find ──────────────────────────────────────────────────────────

@cli.command()
@click.argument("query", default="")
@click.option("--scope", default="", help="Restrict to sessions in this directory tree ('.' = cwd)")
@click.option("--grep", default="", help="Search transcript content")
@click.option("--hours", default=24, help="How far back to look (0 = no limit)")
def find(query, scope, grep, hours):
    """Search ALL Claude Code sessions."""
    sessions = discover_all_sessions(hours=hours, scope=scope, grep=grep)
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
        what = f" matching '{grep or query}'" if (grep or query) else ""
        where = f" in {scope}" if scope else ""
        click.echo(f"No sessions found{what}{where}.")
        return

    for s in sessions:
        agent = s.get("agent") or "?"
        sid = s.get("session_id", "?")
        exchanges = s.get("exchanges", "?")
        size = s.get("size_mb", "?")
        running = "running" if s.get("is_running") else "stopped"
        model = (s.get("model") or "?").replace("claude-", "")
        topic = s.get("topic", "")
        cwd_path = s.get("cwd", "")
        cwd_short = cwd_path.rstrip("/").rsplit("/", 1)[-1] if cwd_path else ""
        topic_str = f" — {topic}" if topic else ""
        cwd_str = f" [{cwd_short}]" if cwd_short else ""
        click.echo(f"[{running}] {agent} ({model}) sid={sid} {exchanges}x {size}MB{cwd_str}{topic_str}")


# ── 7. manage ────────────────────────────────────────────────────────

@cli.command()
@click.argument("name", default="")
@click.option("--action", required=True, type=click.Choice(["kill", "freeze", "restart", "compact", "split", "accept_all"]))
@click.option("--force", is_flag=True, help="Override safety checks")
@click.option("--topics", multiple=True, help="Topic labels for split action")
@click.option("--summary", default="", help="Summary note for compact action")
def manage(name, action, force, topics, summary):
    """Lifecycle operations for sessions."""
    smap = _session_map()

    if action == "accept_all":
        names = list_sessions()
        if not names:
            click.echo("No tmux sessions running.")
            return
        accepted = []
        for sname in names:
            pane = capture_pane(sname, 15)
            info = extract_permission_content(pane)
            if not info:
                continue
            _log_message(f"AUTO-ACCEPT {sname}: {info['tool']} {info['detail']}")
            send_key(sname, "Enter")
            accepted.append(f"{sname} ({info['tool']}: {info['detail'][:60]})")
        if not accepted:
            click.echo("No sessions blocked on permission prompts.")
        else:
            click.echo(f"Accepted {len(accepted)} sessions:")
            for a in accepted:
                click.echo(f"  {a}")
        return

    if not name:
        click.echo("Error: name is required for this action.", err=True)
        raise SystemExit(1)

    if action == "kill":
        if not session_exists(name):
            click.echo(f"Error: session '{name}' does not exist.", err=True)
            raise SystemExit(1)
        if not force and has_attached_client(name):
            click.echo(f"Warning: a user terminal is attached to '{name}'. Use --force to kill anyway.", err=True)
            raise SystemExit(1)
        result = kill_session(name)
        if result.returncode != 0:
            click.echo(f"Error killing '{name}': {result.stderr.strip()}", err=True)
            raise SystemExit(1)
        smap.remove(name)
        click.echo(f"Killed session '{name}'.")

    elif action == "freeze":
        if not session_exists(name):
            entry = smap.get(name)
            if entry and entry.get("status") == "frozen":
                click.echo(f"'{name}' is already frozen.")
                return
            click.echo(f"Error: session '{name}' does not exist.", err=True)
            raise SystemExit(1)
        info = resolve_session_id(name, smap)
        if not info:
            click.echo(f"Error: could not resolve session ID for '{name}'.", err=True)
            raise SystemExit(1)
        result = kill_session(name)
        if result.returncode != 0:
            click.echo(f"Error stopping '{name}': {result.stderr.strip()}", err=True)
            raise SystemExit(1)
        smap.freeze(name)
        click.echo(f"Frozen '{name}': session={info['session_id']}. Use puppet launch to thaw.")

    elif action == "restart":
        if not session_exists(name):
            click.echo(f"Error: session '{name}' does not exist.", err=True)
            raise SystemExit(1)
        if not force and has_attached_client(name):
            click.echo(f"Warning: a user terminal is attached to '{name}'. Use --force to restart anyway.", err=True)
            raise SystemExit(1)
        info = resolve_session_id(name, smap)
        if not info:
            click.echo(f"Error: could not find Claude Code session ID for '{name}'.", err=True)
            raise SystemExit(1)
        session_id = info["session_id"]
        session_cwd = info.get("cwd") or resolve_session_cwd(session_id, smap, default=project_dir())
        agent = info.get("agent", "")

        kill_result = kill_session(name)
        if kill_result.returncode != 0:
            click.echo(f"Error killing '{name}': {kill_result.stderr.strip()}", err=True)
            raise SystemExit(1)
        time.sleep(1)

        create_result = create_session(name)
        if create_result.returncode != 0:
            click.echo(f"Killed '{name}' but failed to recreate: {create_result.stderr.strip()}. Session ID: {session_id}", err=True)
            raise SystemExit(1)
        time.sleep(1)
        send_keys(name, f"cd {session_cwd} && claude --resume {session_id}")

        old_entry = smap.get(name)
        parent = old_entry.get("parent", "") if old_entry else ""
        smap.record(name, session_id, agent, session_cwd, parent=parent)

        agent_info = f", agent={agent}" if agent else ""
        click.echo(f"Restarted '{name}': session={session_id}{agent_info}.")

    elif action == "compact":
        import shutil
        jsonl_path = find_session_jsonl(name, smap)
        if jsonl_path is None:
            click.echo(f"Error: could not find session transcript for '{name}'.", err=True)
            raise SystemExit(1)

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
        click.echo(f"Original: {original_count} entries, {original_size:,} bytes")
        click.echo(f"Compacted: {len(pruned)} entries, {compact_size:,} bytes")
        click.echo(f"Reduction: {reduction:.1f}%")
        click.echo(f"Backup: {backup_path}")
        click.echo(f"File: {jsonl_path}")

    elif action == "split":
        if not topics:
            click.echo("Error: --topics required for split action.", err=True)
            raise SystemExit(1)
        jsonl_path = find_session_jsonl(name, smap)
        if jsonl_path is None:
            click.echo(f"Error: could not find session transcript for '{name}'.", err=True)
            raise SystemExit(1)
        try:
            results = split_session(jsonl_path, list(topics))
        except Exception as e:
            click.echo(f"Error splitting session: {e}", err=True)
            raise SystemExit(1)

        for topic, info in sorted(results.items()):
            if topic == "_suggestions":
                continue
            click.echo(f"  {topic}: {info['rounds']} rounds, sid={info['session_id']}")
            click.echo(f"    {info['path']}")
        suggestions = results.get("_suggestions", [])
        if suggestions:
            click.echo(f"\nSuggested: {', '.join(suggestions)}")


# ── attach ───────────────────────────────────────────────────────────

@cli.command()
@click.argument("name")
def attach(name):
    """Attach to a tmux session."""
    if not session_exists(name):
        click.echo(f"Error: session '{name}' does not exist.", err=True)
        raise SystemExit(1)
    _attach(name)


# ── watch ────────────────────────────────────────────────────────────

@cli.command()
@click.option("--interval", default=5, help="Refresh interval in seconds")
def watch(interval):
    """Interactive dashboard."""
    from .interactive import watch_interactive
    watch_interactive(interval)


# ── freezer ──────────────────────────────────────────────────────────

@cli.command()
def freezer():
    """Browse and restore frozen sessions."""
    from .interactive import freezer as freezer_ui
    freezer_ui()


# ── cost ─────────────────────────────────────────────────────────────

@cli.command()
def cost():
    """Token/cost summary across all sessions."""
    names = list_sessions()
    if not names:
        click.echo("No tmux sessions running.")
        return

    click.echo("")
    click.echo(f"{'Session':<25s} {'Tokens':>12s} {'Est. Cost':>10s}")
    click.echo(f"{'─' * 25} {'─' * 12} {'─' * 10}")

    total_tokens = 0
    total_cost_cents = 0

    for name in sorted(names):
        pane = capture_pane(name, 15)
        bar = parse_status_bar(pane)
        tokens = bar.get("tokens") or 0
        if tokens == 0:
            continue

        model = bar.get("model") or ""
        # Rates: opus ~$30/Mtok, sonnet ~$6/Mtok, haiku ~$0.5/Mtok (blended)
        rate = 30
        if "sonnet" in model:
            rate = 6
        elif "haiku" in model:
            rate = 1

        cost_cents = tokens * rate // 10000
        total_tokens += tokens
        total_cost_cents += cost_cents
        cost_str = f"${cost_cents // 100}.{cost_cents % 100:02d}"
        click.echo(f"{name:<25s} {tokens:>12,d} {cost_str:>10s}")

    click.echo(f"{'─' * 25} {'─' * 12} {'─' * 10}")
    total_cost_str = f"${total_cost_cents // 100}.{total_cost_cents % 100:02d}"
    click.echo(f"{'Total':<25s} {total_tokens:>12,d} {total_cost_str:>10s}")
    click.echo("")


# ── log ──────────────────────────────────────────────────────────────

@cli.command()
@click.argument("n", default=50, type=int)
def log(n):
    """Tail message log."""
    log_file = _message_log()
    if not log_file.exists():
        click.echo("No message log yet.")
        return
    lines = log_file.read_text().strip().split("\n")
    for line in lines[-n:]:
        click.echo(line)


# ── role ─────────────────────────────────────────────────────────────

@cli.command()
@click.argument("name")
@click.argument("new_role", type=click.Choice(["worker", "orchestrator"]))
def role(name, new_role):
    """Set session role."""
    smap = _session_map()
    data = smap.load()
    if name not in data:
        click.echo(f"Session '{name}' not in map.", err=True)
        raise SystemExit(1)
    data[name]["role"] = new_role
    smap.save(data)
    click.echo(f"Set {name} role={new_role}")


# ── adopt ────────────────────────────────────────────────────────────

@cli.command()
@click.argument("session_id_or_name")
@click.option("--tmux", "tmux_mode", is_flag=True, help="Adopt existing tmux session by name")
@click.option("--name", "alias", default="", help="Name for adopted session")
def adopt(session_id_or_name, tmux_mode, alias):
    """Adopt an existing session into puppet management."""
    smap = _session_map()

    if tmux_mode:
        name = session_id_or_name
        if not session_exists(name):
            click.echo(f"Error: tmux session '{name}' does not exist.", err=True)
            raise SystemExit(1)
        info = get_claude_session_info(name)
        if not info:
            click.echo(f"Error: no Claude Code process found in '{name}'.", err=True)
            raise SystemExit(1)
        smap.record(name, info["session_id"], info.get("agent", ""), info.get("cwd", ""))
        click.echo(f"Adopted '{name}': session={info['session_id']}, agent={info.get('agent', '?')}")
    else:
        sid = session_id_or_name
        name = alias or f"puppet-{sid[:8]}"
        # Verify JSONL exists
        from .session import _find_jsonl_by_sid
        jsonl = _find_jsonl_by_sid(sid)
        if not jsonl:
            click.echo(f"Error: no session transcript found for '{sid}'.", err=True)
            raise SystemExit(1)
        if session_exists(name):
            click.echo(f"Error: tmux session '{name}' already exists.", err=True)
            raise SystemExit(1)
        session_cwd = resolve_session_cwd(sid, smap, default=project_dir())
        result = create_session(name)
        if result.returncode != 0:
            click.echo(f"Error creating tmux session: {result.stderr.strip()}", err=True)
            raise SystemExit(1)
        time.sleep(1)
        send_keys(name, f"cd {session_cwd} && claude --resume {sid}")
        smap.record(name, sid, "", session_cwd)
        click.echo(f"Adopted session {sid} as '{name}'. Resuming... (cwd: {session_cwd})")


# ── convenience aliases ──────────────────────────────────────────────

@cli.command("accept")
@click.argument("name")
def accept_cmd(name):
    """Send Enter (accept permission prompt)."""
    if not session_exists(name):
        click.echo(f"Error: session '{name}' does not exist.", err=True)
        raise SystemExit(1)
    send_key(name, "Enter")
    click.echo(f"Accepted permission prompt in '{name}'.")


@cli.command("interrupt")
@click.argument("name")
def interrupt_cmd(name):
    """Send Escape (stop generation)."""
    if not session_exists(name):
        click.echo(f"Error: session '{name}' does not exist.", err=True)
        raise SystemExit(1)
    send_key(name, "Escape")
    click.echo(f"Sent Escape to '{name}'.")


@cli.command("cancel")
@click.argument("name")
def cancel_cmd(name):
    """Send Ctrl+C."""
    if not session_exists(name):
        click.echo(f"Error: session '{name}' does not exist.", err=True)
        raise SystemExit(1)
    send_key(name, "C-c")
    click.echo(f"Sent Ctrl+C to '{name}'.")


@cli.command("accept-all")
def accept_all_cmd():
    """Accept all blocked permission prompts across all sessions."""
    names = list_sessions()
    if not names:
        click.echo("No tmux sessions running.")
        return
    accepted = []
    for sname in names:
        pane = capture_pane(sname, 15)
        info = extract_permission_content(pane)
        if not info:
            continue
        _log_message(f"AUTO-ACCEPT {sname}: {info['tool']} {info['detail']}")
        send_key(sname, "Enter")
        accepted.append(f"{sname} ({info['tool']}: {info['detail'][:60]})")
    if not accepted:
        click.echo("No sessions blocked on permission prompts.")
    else:
        click.echo(f"Accepted {len(accepted)} sessions:")
        for a in accepted:
            click.echo(f"  {a}")


@cli.command("kill")
@click.argument("name")
@click.option("--force", is_flag=True)
def kill_cmd(name, force):
    """Kill a session permanently."""
    smap = _session_map()
    if not session_exists(name):
        click.echo(f"Error: session '{name}' does not exist.", err=True)
        raise SystemExit(1)
    if not force and has_attached_client(name):
        click.echo(f"Warning: a user terminal is attached to '{name}'. Use --force to kill anyway.", err=True)
        raise SystemExit(1)
    result = kill_session(name)
    if result.returncode != 0:
        click.echo(f"Error killing '{name}': {result.stderr.strip()}", err=True)
        raise SystemExit(1)
    smap.remove(name)
    click.echo(f"Killed session '{name}'.")


@cli.command("freeze")
@click.argument("name")
def freeze_cmd(name):
    """Freeze a session (stop but keep restorable)."""
    smap = _session_map()
    if not session_exists(name):
        entry = smap.get(name)
        if entry and entry.get("status") == "frozen":
            click.echo(f"'{name}' is already frozen.")
            return
        click.echo(f"Error: session '{name}' does not exist.", err=True)
        raise SystemExit(1)
    info = resolve_session_id(name, smap)
    if not info:
        click.echo(f"Error: could not resolve session ID for '{name}'.", err=True)
        raise SystemExit(1)
    result = kill_session(name)
    if result.returncode != 0:
        click.echo(f"Error stopping '{name}': {result.stderr.strip()}", err=True)
        raise SystemExit(1)
    smap.freeze(name)
    click.echo(f"Frozen '{name}': session={info['session_id']}. Use 'puppet launch {name}' to thaw.")


@cli.command("restart")
@click.argument("name")
@click.option("--force", is_flag=True)
def restart_cmd(name, force):
    """Kill + resume with full context."""
    smap = _session_map()
    if not session_exists(name):
        click.echo(f"Error: session '{name}' does not exist.", err=True)
        raise SystemExit(1)
    if not force and has_attached_client(name):
        click.echo(f"Warning: a user terminal is attached to '{name}'. Use --force to restart anyway.", err=True)
        raise SystemExit(1)
    info = resolve_session_id(name, smap)
    if not info:
        click.echo(f"Error: could not find Claude Code session ID for '{name}'.", err=True)
        raise SystemExit(1)
    session_id = info["session_id"]
    session_cwd = info.get("cwd") or resolve_session_cwd(session_id, smap, default=project_dir())
    agent = info.get("agent", "")

    kill_result = kill_session(name)
    if kill_result.returncode != 0:
        click.echo(f"Error killing '{name}': {kill_result.stderr.strip()}", err=True)
        raise SystemExit(1)
    time.sleep(1)

    create_result = create_session(name)
    if create_result.returncode != 0:
        click.echo(f"Killed '{name}' but failed to recreate. Session ID: {session_id}", err=True)
        raise SystemExit(1)
    time.sleep(1)
    send_keys(name, f"cd {session_cwd} && claude --resume {session_id}")

    old_entry = smap.get(name)
    parent = old_entry.get("parent", "") if old_entry else ""
    smap.record(name, session_id, agent, session_cwd, parent=parent)

    agent_info = f", agent={agent}" if agent else ""
    click.echo(f"Restarted '{name}': session={session_id}{agent_info}.")


@cli.command("compact")
@click.argument("name")
@click.argument("summary", default="")
def compact_cmd(name, summary):
    """Compact session transcript."""
    # Delegate to manage action
    ctx = click.get_current_context()
    ctx.invoke(manage, name=name, action="compact", summary=summary, force=False, topics=())


@cli.command("upgrade")
@click.argument("name")
def upgrade_cmd(name):
    """Upgrade to opus 1M context."""
    if not session_exists(name):
        click.echo(f"Error: session '{name}' does not exist.", err=True)
        raise SystemExit(1)
    click.echo(f"Upgrading '{name}' to opus 1M context...")
    cmd = "/model opus[1m]"
    send_keys(name, cmd)
    click.echo(f"Sent '{cmd}' to '{name}'.")


@cli.command("ls")
@click.pass_context
def ls_cmd(ctx):
    """List sessions with status (alias for status --full)."""
    ctx.invoke(status, full=True, name="")


if __name__ == "__main__":
    cli()
