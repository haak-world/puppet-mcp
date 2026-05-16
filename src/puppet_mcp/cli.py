"""puppet — CLI for managing Claude Code sessions via tmux.

5 commands for humans. 8 MCP tools for agents.
"""

import json
import os
import sys
import time
from pathlib import Path

import click

from . import data_dir, project_dir
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


def _session_map() -> SessionMap:
    return SessionMap()


def _log_message(msg: str):
    log = data_dir() / "puppet-messages.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with open(log, "a") as f:
        f.write(f"{ts} {msg}\n")


def _attach(name: str):
    """Set terminal title and attach to tmux session."""
    sys.stdout.write(f"\033]0;puppet: {name}\007")
    sys.stdout.flush()
    os.execlp("tmux", "tmux", "attach", "-t", name)


@click.group()
def cli():
    """puppet — manage Claude Code sessions via tmux.

    \b
    5 commands:
      launch NAME    get into a session (create/resume/thaw/attach)
      send NAME      post to a session (fire-and-forget)
      assign NAME    send work with completion tracking (agent reports back)
      manage NAME    lifecycle (kill, freeze, restart, compact, etc.)
      watch          live interactive dashboard
    """


# ── launch ──────────────────────────────────────────────────────────

@cli.command()
@click.argument("name")
@click.option("--agent", "-a", default="", help="Agent name (default: same as NAME)")
@click.option("--model", "-m", default="opus")
@click.option("--context", default="", help="Context window, e.g. 1m")
@click.option("--role", "-r", default="worker", type=click.Choice(["worker", "orchestrator"]))
@click.option("--resume", default="", help="Resume session by ID")
@click.option("--budget", "-b", default=5.0)
@click.option("--cwd", "-d", default="", help="Working directory")
@click.option("--prompt", "-p", default="", help="Initial prompt")
@click.option("--new", "force_new", is_flag=True, help="Force fresh session (ignore frozen)")
@click.option("--unsafe", is_flag=True)
@click.option("--detach", is_flag=True, help="Don't attach after launching")
def launch(name, agent, model, context, role, resume, budget, cwd, prompt, force_new, unsafe, detach):
    """Get into a session — create, resume, thaw, or attach.

    If NAME matches an existing tmux session, attaches to it.
    If a frozen session exists for NAME, thaws and resumes it.
    Otherwise searches for resumable sessions matching NAME.
    """
    agent = agent or name
    cwd = cwd or os.getcwd()
    smap = _session_map()

    # Active tmux session → attach
    if session_exists(name):
        click.echo(f"Session '{name}' exists. Attaching...")
        _attach(name)
        return

    # Frozen → thaw
    if not force_new and not resume:
        frozen_entry = smap.get(name)
        if frozen_entry and frozen_entry.get("status") == "frozen":
            resume = frozen_entry["session_id"]
            agent = frozen_entry.get("agent") or agent
            click.echo(f"Thawing frozen session '{name}'...")

    # Search for resumable sessions
    if not force_new and not resume:
        sessions = discover_all_sessions(hours=72)
        q = name.lower()
        hits = [s for s in sessions if
                q in (s.get("agent") or "").lower()
                or q in (s.get("topic") or "").lower()
                or q in (s.get("session_id") or "").lower()]
        if hits:
            click.echo(f"Found existing sessions matching '{name}':")
            for i, s in enumerate(hits[:8]):
                ag = s.get("agent") or "?"
                sid = s.get("session_id", "")
                ex = s.get("exchanges", "?")
                sz = s.get("size_mb", "?")
                run = "running" if s.get("is_running") else "stopped"
                topic = (s.get("topic") or "")[:50]
                click.echo(f"  {i+1}) [{run}] {ag} {ex}x {sz}MB sid={sid} — {topic}")
            click.echo(f"\n  n) Start fresh session\n")
            choice = click.prompt("Resume which?", default="n")
            if choice != "n" and choice.isdigit():
                idx = int(choice) - 1
                if 0 <= idx < len(hits):
                    resume = hits[idx]["session_id"]
                    found_agent = hits[idx].get("agent")
                    if found_agent:
                        agent = found_agent

    # Build claude command
    if resume:
        session_cwd = resolve_session_cwd(resume, smap, default=cwd)
        claude_cmd = f"cd {session_cwd} && claude --resume {resume}"
        click.echo(f"Resuming session {resume[:12]}…")
    else:
        model_str = f"{model}[{context}]" if context else model
        claude_cmd = f"cd {cwd} && claude --agent {agent} --model {model_str} --max-budget-usd {budget}"
        if unsafe:
            claude_cmd += " --dangerously-skip-permissions"
        click.echo(f"Launching '{name}': agent={agent}, model={model_str}, role={role}")

    # Orchestrator gets full puppet tools
    if role == "orchestrator":
        pkg_dir = Path(__file__).resolve().parent.parent.parent
        orch_config = pkg_dir / "puppet-orchestrator.json"
        if orch_config.exists():
            claude_cmd += f" --mcp-config {orch_config}"

    # Create tmux session with shell, send claude command
    create_session(name)
    time.sleep(0.5)
    send_keys(name, claude_cmd)

    if resume:
        smap.record(name, resume, agent, cwd, role=role)
    else:
        # Wait for claude to start, register session ID
        time.sleep(12)
        if prompt:
            send_keys(name, prompt)
        for _ in range(5):
            info = get_claude_session_info(name)
            if info:
                smap.record(name, info["session_id"], info.get("agent", ""), info.get("cwd", ""), role=role)
                break
            time.sleep(3)

    if detach:
        click.echo(f"Session '{name}' running (detached).")
    else:
        _attach(name)


# ── send ────────────────────────────────────────────────────────────

@cli.command()
@click.argument("name")
@click.argument("text", default="")
@click.option("--action", type=click.Choice(["text", "enter", "escape", "ctrl-c", "slash"]), default="text",
              help="Input type")
@click.option("--from", "from_agent", default="", help="Caller identity for message prefix")
@click.option("--wait", is_flag=True, help="Wait for response (like handoff)")
@click.option("--timeout", default=120, help="Timeout in seconds when --wait is used")
def send(name, text, action, from_agent, wait, timeout):
    """Talk to a session — send text, keys, or slash commands.

    \b
    Actions:
      text     Send text + Enter (default)
      enter    Accept a permission prompt
      escape   Stop generation
      ctrl-c   Cancel operation
      slash    Send a slash command (e.g. /model sonnet)

    Use --wait to block until the agent responds (handoff mode).
    """
    if not session_exists(name):
        click.echo(f"Error: session '{name}' does not exist.", err=True)
        raise SystemExit(1)

    if action == "enter":
        send_key(name, "Enter")
        click.echo(f"Sent Enter to '{name}'.")
    elif action == "escape":
        send_key(name, "Escape")
        click.echo(f"Sent Escape to '{name}'.")
    elif action == "ctrl-c":
        send_key(name, "C-c")
        click.echo(f"Sent Ctrl+C to '{name}'.")
    elif action == "slash":
        cmd = text if text.startswith("/") else f"/{text}"
        send_keys(name, cmd)
        click.echo(f"Sent '{cmd}' to '{name}'.")
    else:
        if not text:
            click.echo("Error: text is required for action=text.", err=True)
            raise SystemExit(1)
        msg = f"[{from_agent}→{name}]: {text}" if from_agent else text
        send_keys(name, msg)
        if from_agent:
            _log_message(f"{from_agent}→{name}: {text}")

        if wait:
            # Handoff mode: wait for response
            elapsed = 0
            before = set(content_lines(capture_pane(name, 50), 50))
            time.sleep(2)
            elapsed += 2
            while elapsed < timeout:
                pane = capture_pane(name, 50)
                if is_idle(pane):
                    after = content_lines(pane, 50)
                    new = [l for l in after if l not in before]
                    if new and (text in new[0] or (from_agent and from_agent in new[0])):
                        new = new[1:]
                    click.echo("\n".join(new) if new else "(empty response)")
                    return
                time.sleep(2)
                elapsed += 2
            click.echo(f"[timeout after {timeout}s]", err=True)
            return

        click.echo(f"Sent to '{name}'.")


# ── rename ──────────────────────────────────────────────────────────

@cli.command()
@click.argument("old_name")
@click.argument("new_name")
def rename(old_name, new_name):
    """Rename a tmux session and update the session map."""
    if not session_exists(old_name):
        click.echo(f"Error: session '{old_name}' does not exist.", err=True)
        raise SystemExit(1)
    if session_exists(new_name):
        click.echo(f"Error: session '{new_name}' already exists.", err=True)
        raise SystemExit(1)
    result = run_tmux(["rename-session", "-t", old_name, new_name])
    if result.returncode != 0:
        click.echo(f"Error: {result.stderr.strip()}", err=True)
        raise SystemExit(1)
    smap = _session_map()
    entry = smap.get(old_name)
    if entry:
        smap.remove(old_name)
        smap.record(new_name, entry.get("session_id", ""), entry.get("agent", ""),
                    entry.get("cwd", ""), parent=entry.get("parent", ""), role=entry.get("role", ""))
    click.echo(f"Renamed '{old_name}' → '{new_name}'.")


# ── assign ──────────────────────────────────────────────────────────

@cli.command()
@click.argument("name")
@click.argument("task")
@click.option("--from", "from_agent", default="orchestrator", help="Who is assigning")
def assign(name, task, from_agent):
    """Assign work to a session with completion tracking.

    The agent receives the task with an instruction to report back
    when done via puppet_send(action="report"). The sentinel monitors
    for failures (died, blocked, context wall).

    \b
    Examples:
      puppet assign worker-1 "Fix the auth tests"
      puppet assign reed "Review the PR" --from sakshi
    """
    if not session_exists(name):
        click.echo(f"Error: session '{name}' does not exist.", err=True)
        raise SystemExit(1)

    # Record assignment
    af = data_dir() / "assignments.json"
    af.parent.mkdir(parents=True, exist_ok=True)
    assignments = {}
    if af.exists():
        try:
            assignments = json.loads(af.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    import time as _t
    aid = f"{name}:{int(_t.time())}"
    from datetime import datetime, timezone
    assignments[aid] = {
        "session": name,
        "from": from_agent,
        "task": task[:200],
        "assigned_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
    }
    af.write_text(json.dumps(assignments, indent=2) + "\n")

    # Send with report-back instruction
    msg = (
        f"[{from_agent}→{name}]: {task}\n\n"
        f"When you complete this task, report back by calling: "
        f'puppet_send(name="{from_agent}", text="your summary", action="report", from_agent="{name}")'
    )
    send_keys(name, msg)

    _log_message(f"ASSIGN {from_agent}→{name}: {task[:100]}")
    click.echo(f"Assigned to '{name}': {task[:80]}")


# ── manage ──────────────────────────────────────────────────────────

@cli.command()
@click.argument("name", default="")
@click.option("--action", "-a", required=True,
              type=click.Choice(["kill", "freeze", "restart", "compact", "split", "accept-all",
                                 "adopt", "role", "cost", "log"]),
              help="Operation to perform")
@click.option("--force", is_flag=True)
@click.option("--topics", multiple=True, help="Topics for split")
@click.option("--summary", default="", help="Summary for compact")
@click.option("--value", default="", help="Value for role (worker/orchestrator)")
@click.option("--lines", "-n", default=50, help="Lines for log")
def manage(name, action, force, topics, summary, value, lines):
    """Lifecycle and admin operations.

    \b
    Actions:
      kill        Kill session permanently
      freeze      Stop session, keep restorable
      restart     Kill + resume with full context
      compact     Prune session transcript (backs up original)
      split       Split session by topics (--topics T1 --topics T2)
      accept-all  Accept all blocked permission prompts
      adopt       Bring existing session under management
      role        Set session role (--value worker|orchestrator)
      cost        Show token counts and costs
      log         Show message log (--lines N)
    """
    smap = _session_map()

    if action == "kill":
        if not name:
            click.echo("Error: name required for kill.", err=True); raise SystemExit(1)
        if not session_exists(name):
            click.echo(f"Error: session '{name}' does not exist.", err=True); raise SystemExit(1)
        if not force and has_attached_client(name):
            click.echo(f"Warning: user attached to '{name}'. Use --force.", err=True); raise SystemExit(1)
        kill_session(name)
        smap.remove(name)
        click.echo(f"Killed '{name}'.")

    elif action == "freeze":
        if not name:
            click.echo("Error: name required for freeze.", err=True); raise SystemExit(1)
        if not session_exists(name):
            entry = smap.get(name)
            if entry and entry.get("status") == "frozen":
                click.echo(f"'{name}' is already frozen."); return
            click.echo(f"Error: session '{name}' does not exist.", err=True); raise SystemExit(1)
        info = resolve_session_id(name, smap)
        if not info:
            click.echo(f"Error: could not resolve session ID.", err=True); raise SystemExit(1)
        kill_session(name)
        smap.freeze(name)
        click.echo(f"Frozen '{name}'. Use 'puppet launch {name}' to thaw.")

    elif action == "restart":
        if not name:
            click.echo("Error: name required.", err=True); raise SystemExit(1)
        if not session_exists(name):
            click.echo(f"Error: session '{name}' does not exist.", err=True); raise SystemExit(1)
        if not force and has_attached_client(name):
            click.echo(f"Warning: user attached. Use --force.", err=True); raise SystemExit(1)
        info = resolve_session_id(name, smap)
        if not info:
            click.echo(f"Error: could not resolve session ID.", err=True); raise SystemExit(1)
        sid = info["session_id"]
        session_cwd = info.get("cwd") or resolve_session_cwd(sid, smap, default=os.getcwd())
        kill_session(name)
        time.sleep(1)
        create_session(name)
        time.sleep(1)
        send_keys(name, f"cd {session_cwd} && claude --resume {sid}")
        old = smap.get(name)
        smap.record(name, sid, info.get("agent", ""), session_cwd, parent=old.get("parent", "") if old else "")
        click.echo(f"Restarted '{name}': session={sid}")

    elif action == "compact":
        if not name:
            click.echo("Error: name required.", err=True); raise SystemExit(1)
        from .compact import mechanical_prune
        import shutil
        jsonl = find_session_jsonl(name, smap)
        if not jsonl:
            click.echo(f"Error: no transcript for '{name}'.", err=True); raise SystemExit(1)
        entries = []
        with open(jsonl) as f:
            for line in f:
                line = line.strip()
                if line:
                    try: entries.append(json.loads(line))
                    except json.JSONDecodeError: pass
        orig_size = jsonl.stat().st_size
        pruned = mechanical_prune(entries)
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = jsonl.with_suffix(f".pre-compact-{ts}.jsonl")
        shutil.copy2(jsonl, backup)
        with open(jsonl, "w") as f:
            for e in pruned:
                f.write(json.dumps(e) + "\n")
        new_size = jsonl.stat().st_size
        pct = (1 - new_size / orig_size) * 100 if orig_size else 0
        click.echo(f"Compacted: {len(entries)} → {len(pruned)} entries ({pct:.0f}% reduction)")

    elif action == "split":
        if not name or not topics:
            click.echo("Error: name and --topics required.", err=True); raise SystemExit(1)
        from .compact import split_session
        jsonl = find_session_jsonl(name, smap)
        if not jsonl:
            click.echo(f"Error: no transcript for '{name}'.", err=True); raise SystemExit(1)
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            click.echo("Error: ANTHROPIC_API_KEY required.", err=True); raise SystemExit(1)
        results = split_session(jsonl, list(topics), api_key)
        for topic, info in sorted(results.items()):
            if topic == "_suggestions": continue
            click.echo(f"  {topic}: {info['rounds']} rounds, sid={info['session_id']}")

    elif action == "accept-all":
        for sname in list_sessions():
            pane = capture_pane(sname, 15)
            info = extract_permission_content(pane)
            if info:
                _log_message(f"AUTO-ACCEPT {sname}: {info['tool']} {info['detail']}")
                send_key(sname, "Enter")
                click.echo(f"Accepted: {sname} ({info['tool']})")

    elif action == "adopt":
        if not name:
            click.echo("Error: session ID or --tmux name required.", err=True); raise SystemExit(1)
        if session_exists(name):
            info = get_claude_session_info(name)
            if info:
                smap.record(name, info["session_id"], info.get("agent", ""))
                click.echo(f"Adopted '{name}': session={info['session_id']}")
            else:
                click.echo(f"Error: no claude process in '{name}'.", err=True)
        else:
            click.echo(f"Creating tmux session for {name[:12]}…")
            session_cwd = resolve_session_cwd(name, smap, default=os.getcwd())
            tmux_name = value or f"puppet-{name[:8]}"
            create_session(tmux_name)
            time.sleep(1)
            send_keys(tmux_name, f"cd {session_cwd} && claude --resume {name}")
            smap.record(tmux_name, name, "", session_cwd)
            click.echo(f"Adopted as '{tmux_name}'.")

    elif action == "role":
        if not name or not value:
            click.echo("Usage: puppet manage NAME -a role --value worker|orchestrator", err=True); raise SystemExit(1)
        s = smap.load()
        if name not in s:
            click.echo(f"Error: '{name}' not in session map.", err=True); raise SystemExit(1)
        s[name]["role"] = value
        smap.save(s)
        click.echo(f"Set {name} role={value}")

    elif action == "cost":
        total_tokens = 0
        total_cost = 0
        click.echo(f"\n{'Session':<25s} {'Tokens':>12s} {'Est. Cost':>10s}")
        click.echo(f"{'─'*25} {'─'*12} {'─'*10}")
        for sname in list_sessions():
            pane = capture_pane(sname, 5)
            bar = parse_status_bar(pane)
            tokens = bar.get("tokens") or 0
            if tokens:
                cost_cents = tokens * 11 // 1000
                total_tokens += tokens
                total_cost += cost_cents
                click.echo(f"{sname:<25s} {tokens:>12,} ${cost_cents/100:>9.2f}")
        click.echo(f"{'─'*25} {'─'*12} {'─'*10}")
        click.echo(f"{'Total':<25s} {total_tokens:>12,} ${total_cost/100:>9.2f}\n")

    elif action == "log":
        log_file = data_dir() / "puppet-messages.log"
        if not log_file.exists():
            click.echo("No message log yet."); return
        text = log_file.read_text()
        for line in text.strip().split("\n")[-lines:]:
            click.echo(line)


# ── sentinel ───────────────────────────────────────────────────────

@cli.group()
def sentinel():
    """Sentinel daemon lifecycle and event subscription."""
    pass


@sentinel.command()
def start():
    """Start the sentinel daemon."""
    from .sentinel import start_sentinel
    result = start_sentinel()
    if result.get("running"):
        verb = "started" if result.get("started") else "already running"
        click.echo(f"Sentinel {verb} (pid={result.get('pid', '?')}).")
    else:
        click.echo(f"Error: sentinel failed to start (pid={result.get('pid', '?')})", err=True)
        raise SystemExit(1)


@sentinel.command()
def stop():
    """Stop the sentinel daemon."""
    from .sentinel import stop_sentinel
    result = stop_sentinel()
    if result.get("stopped"):
        click.echo("Sentinel stopped.")
    else:
        click.echo(f"Error: {result.get('error', 'not running')}", err=True)
        raise SystemExit(1)


@sentinel.command()
def restart():
    """Stop + start the sentinel daemon."""
    from .sentinel import stop_sentinel, start_sentinel
    stop_sentinel()
    result = start_sentinel()
    if result.get("running"):
        click.echo(f"Sentinel restarted (pid={result.get('pid', '?')}).")
    else:
        click.echo(f"Error: sentinel failed to start", err=True)
        raise SystemExit(1)


@sentinel.command("status")
@click.argument("name", default="")
def sentinel_status_cmd(name):
    """Show sentinel daemon health, subscribers, queue depths."""
    from .sentinel import sentinel_status
    info = sentinel_status(name)
    click.echo(f"running: {info.get('running', False)}")
    if info.get("pid"):
        click.echo(f"pid: {info['pid']}")
    if info.get("uptime"):
        click.echo(f"uptime: {info['uptime']}")
    subs = info.get("subscribers", {})
    if subs:
        click.echo(f"subscribers ({len(subs)}):")
        for sub_name, sub_info in subs.items():
            depth = sub_info.get("queue_depth", 0)
            click.echo(f"  {sub_name}: {depth} queued, interests={sub_info.get('interests', '?')}")


@sentinel.command()
@click.argument("name")
@click.option("--interests", "-i", required=True, help="Comma-separated event types")
@click.option("--cadence", "-c", default="5m", help="Poll cadence (e.g. 5m, 30s)")
def register(name, interests, cadence):
    """Subscribe NAME to sentinel events."""
    from .sentinel import register_subscriber, parse_interests, parse_cadence
    filters = parse_interests(interests)
    cadence_val = parse_cadence(cadence)
    msg = register_subscriber(name, filters, cadence_val)
    click.echo(msg)


@sentinel.command()
@click.argument("name")
def poll(name):
    """Read and clear queued events for NAME."""
    from .sentinel import poll_subscriber
    events = poll_subscriber(name)
    if not events:
        return
    for ev in events:
        ts = ev.get("time", "")
        if ts:
            ts = ts.split("T")[-1][:8]
        etype = ev.get("type", "?")
        session = ev.get("session", "?")
        detail = ev.get("detail", "")
        click.echo(f"{ts} {etype} {session} — {detail}")


@sentinel.command()
@click.argument("name")
def unregister(name):
    """Remove subscription for NAME."""
    from .sentinel import unregister_subscriber
    msg = unregister_subscriber(name)
    click.echo(msg)


# ── watch ───────────────────────────────────────────────────────────

@cli.command()
@click.option("--interval", default=5, help="Refresh interval in seconds")
def watch(interval):
    """Live interactive dashboard — arrow keys, Enter to open, f to freeze."""
    from .interactive import watch_interactive
    watch_interactive(interval)


if __name__ == "__main__":
    cli()
