"""Live terminal dashboard for puppet sessions.

Usage: python -m puppet_mcp.watch [--interval 5]
"""

import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import data_dir
from .session import SessionMap
from .tmux import (
    capture_pane,
    classify_activity,
    detect_context_window,
    extract_permission_content,
    list_sessions,
    parse_status_bar,
    run_tmux,
)

HISTORY_CAP = 20

SPINNER = "⣾⣽⣻⢿⡿⣟⣯⣷"
# Braille block characters for activity chart: 8 height levels
# Each maps to a vertical bar of that height using block elements
VBAR = " ▁▂▃▄▅▆▇█"  # index 0-8
CHART_WIDTH = 20  # default characters of history (overridable via --chart-width)


def _state_file() -> Path:
    return data_dir() / "puppet-state.json"


def _load_history() -> tuple[dict[str, list[int]], dict[str, list[int]]]:
    """Load persisted token and delta history from state file."""
    sf = _state_file()
    if not sf.exists():
        return defaultdict(list), defaultdict(list)
    try:
        state = json.loads(sf.read_text())
    except (json.JSONDecodeError, OSError):
        return defaultdict(list), defaultdict(list)
    th = defaultdict(list, {k: v for k, v in state.get("token_history", {}).items()})
    dh = defaultdict(list, {k: v for k, v in state.get("delta_history", {}).items()})
    return th, dh


def _save_history(token_history: dict[str, list[int]], delta_history: dict[str, list[int]]):
    """Persist token/delta history to state file, merging with existing state."""
    sf = _state_file()
    try:
        state = json.loads(sf.read_text()) if sf.exists() else {}
    except (json.JSONDecodeError, OSError):
        state = {}
    # Cap each session's history at HISTORY_CAP entries
    state["token_history"] = {k: v[-HISTORY_CAP:] for k, v in token_history.items() if v}
    state["delta_history"] = {k: v[-HISTORY_CAP:] for k, v in delta_history.items() if v}
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(json.dumps(state, indent=2) + "\n")


def _bar(pct: int, width: int = 8) -> str:
    clamped = min(pct, 100)
    filled = int(clamped / 100 * width)
    return "▓" * filled + "░" * (width - filled)


def _color(text: str, code: int) -> str:
    return f"\033[{code}m{text}\033[0m"


def _pct_color(pct: int, text: str) -> str:
    code = 31 if pct >= 85 else 33 if pct >= 70 else 32
    return f"\033[{code}m{text}\033[0m"


def _activity_chart(deltas: list[int]) -> str:
    """Render a scrolling activity chart from token deltas.

    Each character is one time tick. Height shows consumption rate.
    Uses block element characters ▁▂▃▄▅▆▇ for 8 levels.
    Always returns exactly CHART_WIDTH characters.
    """
    vals = deltas[-CHART_WIDTH:]
    peak = max((v for v in vals if v > 0), default=0)

    chars = []
    for v in vals:
        if v <= 0 or peak == 0:
            chars.append(" ")
        else:
            level = min(8, max(1, int(v / peak * 8)))
            chars.append(VBAR[level])

    # Always pad to exactly CHART_WIDTH
    pad = CHART_WIDTH - len(chars)
    return " " * pad + "".join(chars)


def _format_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s ago"
    elif seconds < 3600:
        return f"{int(seconds / 60)}m ago"
    return f"{int(seconds / 3600)}h ago"


_COST_PER_MTOK = {
    "opus": 30.0,
    "sonnet": 6.0,
    "haiku": 0.5,
}


def _estimate_cost(tokens: int, model: str = "") -> float:
    """Rough blended cost estimate (input + output average)."""
    m = model.lower()
    for key, rate in _COST_PER_MTOK.items():
        if key in m:
            return tokens * rate / 1_000_000
    return tokens * 30 / 1_000_000  # default to opus


def _build_session_row(
    name: str,
    frame_idx: int,
    token_history: dict[str, list[int]],
    delta_history: dict[str, list[int]],
    indent: int = 0,
) -> tuple[str, dict, int, float]:
    """Build one session row with scrolling activity chart."""
    pane = capture_pane(name, 15)
    bar = parse_status_bar(pane)
    activity = classify_activity(pane, tmux_name=name)
    tokens = bar.get("tokens") or 0
    agent = bar.get("agent") or "?"
    model = bar.get("model") or ""
    ctx = bar.get("context_window") or detect_context_window(name, tokens=tokens)
    pct = int(tokens / ctx * 100) if tokens and ctx else 0
    ctx_label = " 1M" if ctx >= 1_000_000 else ".2M"
    cost = _estimate_cost(tokens, model)

    # Track token deltas for activity chart
    prev_tokens = token_history[name][-1] if token_history[name] else 0
    delta = max(0, tokens - prev_tokens) if prev_tokens else 0
    token_history[name].append(tokens)
    if len(token_history[name]) > CHART_WIDTH + 2:
        token_history[name] = token_history[name][-(CHART_WIDTH + 2):]
    delta_history[name].append(delta)
    if len(delta_history[name]) > CHART_WIDTH + 2:
        delta_history[name] = delta_history[name][-(CHART_WIDTH + 2):]

    # Activity icon
    if activity == "active":
        icon = _color(SPINNER[frame_idx % len(SPINNER)], 36)
    elif activity == "blocked":
        icon = _color("▌", 31)
    elif activity == "idle":
        icon = "·"
    elif activity == "dead":
        icon = _color("x", 31)
    else:
        icon = " "

    # Context bar with color
    bar_str = _pct_color(pct, _bar(pct))

    # Scrolling activity chart
    chart = _activity_chart(delta_history[name])
    chart_colored = _pct_color(pct, chart) if activity == "active" else _color(chart, 90)

    # Column widths (fixed)
    NAME_W = 18
    AGENT_W = 6
    BAR_W = 8  # from _bar(pct, width=8)

    prefix = "  " * indent
    tree_marker = "└ " if indent else ""
    raw_name = f"{prefix}{tree_marker}{name}"
    display_name = raw_name[:NAME_W].ljust(NAME_W)
    display_agent = agent[:AGENT_W].ljust(AGENT_W)

    # Row: │ icon name             agent  ▓▓░░░░░░ 1M │chart               │
    row = f"│ {icon} {display_name} {display_agent} {bar_str} {ctx_label} │{chart_colored}│"

    if activity == "blocked":
        info = extract_permission_content(pane)
        if info:
            # Blocked detail line — same total width
            detail = f"{info['tool']}({info['detail'][:35]})"
            row += f"\n│   {_color('BLOCKED', 31)}: {detail}"

    state_entry = {"activity": activity, "tokens": tokens, "agent": agent}
    return row, state_entry, tokens, cost


def _recent_activity(name: str, delta_history: dict[str, list[int]], window: int = 8) -> int:
    """Sum of recent token deltas — higher = more active recently."""
    deltas = delta_history.get(name, [])
    return sum(deltas[-window:])


def _build_frame(
    frame_idx: int,
    prev_state: dict,
    token_history: dict[str, list[int]],
    delta_history: dict[str, list[int]],
    change_log: list[tuple[float, str]],
    sort_by: str = "tree",
) -> tuple[str, dict]:
    """Build one frame of the dashboard.

    sort_by: "tree" (default) = parent-child hierarchy
             "activity" = most recently active first
             "name" = alphabetical
    """
    tmux_sessions = set(list_sessions())
    if not tmux_sessions:
        return "No tmux sessions running.", {}

    if sort_by == "activity":
        # Sort by recent delta sum, descending. No tree indentation.
        ordered = [
            (n, 0) for n in sorted(
                tmux_sessions,
                key=lambda n: _recent_activity(n, delta_history),
                reverse=True,
            )
        ]
    elif sort_by == "name":
        ordered = [(n, 0) for n in sorted(tmux_sessions)]
    else:
        # Tree order from session map
        smap_data = SessionMap().load()
        parent_of = {n: v.get("parent", "") for n, v in smap_data.items()}

        def _walk(parent: str, depth: int) -> list[tuple[str, int]]:
            children = sorted(n for n in tmux_sessions if parent_of.get(n) == parent and n != parent)
            result = []
            for child in children:
                result.append((child, depth))
                result.extend(_walk(child, depth + 1))
            return result

        ordered = []
        roots = sorted(
            n for n in tmux_sessions
            if not parent_of.get(n) or parent_of[n] not in tmux_sessions
        )
        for root in roots:
            ordered.append((root, 0))
            ordered.extend(_walk(root, 1))

        covered = {name for name, _ in ordered}
        for name in sorted(tmux_sessions - covered):
            ordered.append((name, 0))

    state = {}
    rows = []
    total_tokens = 0
    total_cost = 0.0
    now = time.time()

    for name, depth in ordered:
        row, entry, tokens, cost = _build_session_row(
            name, frame_idx, token_history, delta_history, depth,
        )
        state[name] = entry
        total_tokens += tokens
        total_cost += cost
        rows.append(row)

    # Detect changes
    if prev_state:
        prev_names = set(prev_state.keys())
        curr_names = set(state.keys())
        for n in sorted(curr_names - prev_names):
            change_log.append((now, f"+ {n}: new"))
        for n in sorted(prev_names - curr_names):
            change_log.append((now, f"- {n}: gone"))
        for n in sorted(curr_names & prev_names):
            c, p = state[n], prev_state[n]
            if c["activity"] != p["activity"]:
                change_log.append((now, f"~ {n}: {p['activity']} → {c['activity']}"))

    # Prune old changes (>60s)
    change_log[:] = [(t, msg) for t, msg in change_log if now - t < 60]

    # Count states
    active = sum(1 for s in state.values() if s["activity"] == "active")
    blocked = sum(1 for s in state.values() if s["activity"] == "blocked")
    idle = sum(1 for s in state.values() if s["activity"] in ("idle", "stale"))

    ts = time.strftime("%H:%M:%S")
    cost_str = f"${total_cost:.2f}"

    out = []
    # Header with column labels
    W = 65
    hdr_pad = W - 20 - len(ts)  # "╭─ puppet watch " + pad + " HH:MM:SS ─╮"
    out.append(f"╭─ puppet watch {'─' * hdr_pad} {ts} ─╮")
    out.append(f"│   {'name':<18s} {'agent':<6s}  {'context':<12s}│{'activity':^{CHART_WIDTH}s}│")
    out.extend(rows)

    if change_log:
        out.append(f"├{'─' * (W - 2)}┤")
        for t, msg in change_log[-3:]:
            age = _format_age(now - t)
            entry = f"│ {msg} ({age})"
            out.append(entry + " " * max(0, W - 1 - len(entry)) + "│")

    # Footer
    status_parts = [f"{len(state)} sessions", cost_str]
    if active:
        status_parts.append(f"{active} active")
    if blocked:
        status_parts.append(_color(f"{blocked} blocked", 31))
    if idle:
        status_parts.append(f"{idle} idle")
    footer_content = " · ".join(status_parts)
    # footer_content has ANSI so we can't use len() for padding
    footer_clean_len = len(re.sub(r'\033\[[0-9;]*m', '', footer_content))
    fpad = W - 7 - footer_clean_len  # "╰── " (4) + content + " " (1) + "─"*fpad + "─╯" (2)
    out.append(f"╰── {footer_content} {'─' * max(0, fpad)}─╯")

    return "\n".join(out), state


def watch(interval: int = 5, sort_by: str = "tree", chart_width: int = CHART_WIDTH):
    """Run the live dashboard."""
    global CHART_WIDTH
    CHART_WIDTH = chart_width

    token_history, delta_history = _load_history()
    change_log: list[tuple[float, str]] = []

    try:
        from rich.live import Live
        from rich.text import Text
        _watch_rich(interval, token_history, delta_history, change_log, sort_by)
    except ImportError:
        _watch_ansi(interval, token_history, delta_history, change_log, sort_by)


def _spin_frame(cached_output: str, frame_idx: int) -> str:
    """Update just the spinner characters in cached output without re-polling tmux."""
    for i in range(len(SPINNER)):
        cached_output = cached_output.replace(
            _color(SPINNER[i], 36),
            _color(SPINNER[frame_idx % len(SPINNER)], 36),
        )
    return cached_output


def _watch_rich(interval, token_history, delta_history, change_log, sort_by):
    from rich.live import Live
    from rich.text import Text

    frame = 0
    prev = {}
    cached_output = ""
    last_poll = 0.0
    spin_interval = 0.3

    with Live(refresh_per_second=4, console=None) as live:
        while True:
            now = time.time()
            if now - last_poll >= interval:
                cached_output, prev = _build_frame(frame, prev, token_history, delta_history, change_log, sort_by=sort_by)
                _save_history(token_history, delta_history)
                last_poll = now
            else:
                cached_output = _spin_frame(cached_output, frame)
            live.update(Text.from_ansi(cached_output))
            frame += 1
            time.sleep(spin_interval)


def _watch_ansi(interval, token_history, delta_history, change_log, sort_by):
    frame = 0
    prev = {}
    cached_output = ""
    last_poll = 0.0
    spin_interval = 0.3

    while True:
        now = time.time()
        if now - last_poll >= interval:
            cached_output, prev = _build_frame(frame, prev, token_history, delta_history, change_log, sort_by=sort_by)
            _save_history(token_history, delta_history)
            last_poll = now
        else:
            cached_output = _spin_frame(cached_output, frame)
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.write(cached_output + "\n")
        sys.stdout.flush()
        frame += 1
        time.sleep(spin_interval)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Live puppet session dashboard")
    parser.add_argument("--interval", type=int, default=5, help="Refresh interval in seconds")
    parser.add_argument("--sort", choices=["tree", "activity", "name"], default="tree",
                        help="Sort order: tree (hierarchy), activity (most active first), name (alpha)")
    parser.add_argument("--chart-width", type=int, default=CHART_WIDTH,
                        help=f"Width of activity chart in characters (default {CHART_WIDTH})")
    args = parser.parse_args()
    watch(args.interval, sort_by=args.sort, chart_width=args.chart_width)


if __name__ == "__main__":
    main()
