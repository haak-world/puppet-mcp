"""Interactive terminal UIs for puppet: watch with selection, freezer browser."""

import curses
import os
import time
from collections import defaultdict

from .session import SessionMap
from .tmux import (
    capture_pane,
    classify_activity,
    create_session,
    detect_context_window,
    extract_permission_content,
    kill_session,
    list_sessions,
    parse_status_bar,
    run_tmux,
    send_key,
    send_keys,
    session_exists,
)

SPINNER = "⣾⣽⣻⢿⡿⣟⣯⣷"
VBAR = " ▁▂▃▄▅▆▇█"
CHART_WIDTH = 20


def _bar(pct: int, width: int = 8) -> str:
    clamped = min(pct, 100)
    filled = int(clamped / 100 * width)
    return "▓" * filled + "░" * (width - filled)


def _activity_chart(deltas: list[int]) -> str:
    """Scrolling activity chart. Always CHART_WIDTH chars."""
    vals = deltas[-CHART_WIDTH:]
    peak = max((v for v in vals if v > 0), default=0)
    chars = []
    for v in vals:
        if v <= 0 or peak == 0:
            chars.append(" ")
        else:
            level = min(8, max(1, int(v / peak * 8)))
            chars.append(VBAR[level])
    pad = CHART_WIDTH - len(chars)
    return " " * pad + "".join(chars)


def _pct_color_pair(pct: int) -> int:
    """Return curses color pair number for a context percentage."""
    if pct >= 85:
        return 3  # red
    if pct >= 70:
        return 2  # yellow
    return 1  # green


def _gather_sessions(
    delta_history: dict[str, list[int]],
    token_history: dict[str, list[int]],
    smap: SessionMap | None = None,
) -> list[dict]:
    """Gather all tmux sessions with status info and update histories."""
    sessions = []
    smap_data = smap.load() if smap else {}
    for name in list_sessions():
        pane = capture_pane(name, 15)
        bar = parse_status_bar(pane)
        activity = classify_activity(pane, tmux_name=name)
        tokens = bar.get("tokens") or 0
        # Agent: status bar first, then session map fallback
        agent = bar.get("agent") or (smap_data.get(name, {}).get("agent")) or "?"
        ctx = bar.get("context_window") or detect_context_window(name, tokens=tokens)
        pct = int(tokens / ctx * 100) if tokens and ctx else 0
        ctx_label = "1M" if ctx >= 1_000_000 else ".2M"

        # Track deltas
        prev = token_history[name][-1] if token_history[name] else 0
        delta = max(0, tokens - prev) if prev else 0
        token_history[name].append(tokens)
        if len(token_history[name]) > CHART_WIDTH + 2:
            token_history[name] = token_history[name][-(CHART_WIDTH + 2):]
        delta_history[name].append(delta)
        if len(delta_history[name]) > CHART_WIDTH + 2:
            delta_history[name] = delta_history[name][-(CHART_WIDTH + 2):]

        # Permission detail
        blocked_detail = ""
        if activity == "blocked":
            info = extract_permission_content(pane)
            if info:
                blocked_detail = f"{info['tool']}({info['detail'][:35]})"

        sessions.append({
            "name": name,
            "agent": agent,
            "activity": activity,
            "tokens": tokens,
            "pct": pct,
            "ctx_label": ctx_label,
            "blocked_detail": blocked_detail,
        })
    return sessions


def _addstr_colored(stdscr, y: int, x: int, text: str, color_pair: int, extra_attr: int = 0):
    """Safe addstr with color."""
    try:
        attr = curses.color_pair(color_pair) | extra_attr if curses.has_colors() else extra_attr
        stdscr.addstr(y, x, text, attr)
    except curses.error:
        pass


def _draw_session_row(
    stdscr, y: int, w: int, s: dict, frame: int,
    is_selected: bool, delta_history: dict,
):
    """Draw one session row with colored bar and activity chart."""
    name = s["name"][:18].ljust(18)
    agent = s["agent"][:6].ljust(6)
    pct = s["pct"]
    activity = s["activity"]
    ctx_label = s["ctx_label"]

    # Icon
    if activity == "active":
        icon = SPINNER[frame % len(SPINNER)]
        icon_color = 4  # cyan
    elif activity == "blocked":
        icon = "▌"
        icon_color = 3  # red
    elif activity == "frozen":
        icon = "❄"
        icon_color = 4  # cyan
    elif activity == "exited":
        icon = "⏎"
        icon_color = 2  # yellow
    elif activity == "idle":
        icon = "·"
        icon_color = 0
    elif activity == "dead":
        icon = "x"
        icon_color = 3  # red
    else:
        icon = " "
        icon_color = 0

    # Selection background
    sel_attr = curses.A_REVERSE if is_selected else 0

    # Draw: " icon name agent  "
    _addstr_colored(stdscr, y, 0, " ", 0, sel_attr)
    _addstr_colored(stdscr, y, 1, icon, icon_color, sel_attr)
    _addstr_colored(stdscr, y, 3, name, 0, sel_attr)
    _addstr_colored(stdscr, y, 22, agent, 0, sel_attr | curses.A_DIM)

    # Progress bar — colored by pct
    bar_str = _bar(pct)
    bar_color = _pct_color_pair(pct)
    _addstr_colored(stdscr, y, 30, bar_str, bar_color, sel_attr)

    # Context label
    _addstr_colored(stdscr, y, 39, f"{ctx_label:>3s}", 0, sel_attr | curses.A_DIM)

    # Separator
    _addstr_colored(stdscr, y, 43, "│", 0, sel_attr | curses.A_DIM)

    # Activity chart — colored by activity
    chart = _activity_chart(delta_history.get(s["name"], []))
    chart_color = bar_color if activity == "active" else 0
    chart_attr = sel_attr | (curses.A_DIM if activity != "active" else 0)
    _addstr_colored(stdscr, y, 44, chart, chart_color, chart_attr)

    # Right border
    _addstr_colored(stdscr, y, 44 + CHART_WIDTH, "│", 0, sel_attr | curses.A_DIM)

    # Fill rest with selection color if selected
    rest = w - 44 - CHART_WIDTH - 1
    if rest > 0 and is_selected:
        _addstr_colored(stdscr, y, 45 + CHART_WIDTH, " " * min(rest, 20), 0, sel_attr)


def _watch_interactive(stdscr, interval: int = 5):
    """Interactive watch with arrow key selection and activity charts."""
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(300)  # 300ms for spinner

    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.init_pair(3, curses.COLOR_RED, -1)
        curses.init_pair(4, curses.COLOR_CYAN, -1)
        curses.init_pair(5, curses.COLOR_WHITE, curses.COLOR_BLUE)

    selected = 0
    frame = 0
    sessions = []
    last_poll = 0.0
    smap = SessionMap()
    delta_history: dict[str, list[int]] = defaultdict(list)
    token_history: dict[str, list[int]] = defaultdict(list)
    message = ""
    message_until = 0.0

    while True:
        now = time.time()

        if now - last_poll >= interval:
            sessions = _gather_sessions(delta_history, token_history, smap)
            # Add frozen sessions
            for name, entry in sorted(smap.frozen().items()):
                if not any(s["name"] == name for s in sessions):
                    sessions.append({
                        "name": name,
                        "agent": entry.get("agent", "?"),
                        "activity": "frozen",
                        "tokens": 0,
                        "pct": 0,
                        "ctx_label": "",
                        "blocked_detail": "",
                    })
            last_poll = now

        if not sessions:
            stdscr.erase()
            stdscr.addstr(1, 2, "No sessions. Press 'q' to quit.")
            stdscr.refresh()
            key = stdscr.getch()
            if key == ord('q'):
                return
            continue

        selected = max(0, min(selected, len(sessions) - 1))
        h, w = stdscr.getmaxyx()

        stdscr.erase()

        # Header
        ts = time.strftime("%H:%M:%S")
        header = f" puppet watch ─── {ts}"
        stdscr.addstr(0, 0, header, curses.A_BOLD)
        controls = "  ↑↓ select · Enter attach · f freeze · a accept · q quit"
        _addstr_colored(stdscr, 0, len(header), controls[:w - len(header) - 1], 0, curses.A_DIM)

        # Column header
        col_hdr = f"   {'name':<18s} {'agent':<6s}   {'context':>8s}    │{'activity':^{CHART_WIDTH}s}│"
        _addstr_colored(stdscr, 1, 0, col_hdr[:w - 1], 0, curses.A_DIM)

        # Session rows
        for i, s in enumerate(sessions):
            row_y = i + 2
            if row_y >= h - 2:
                break
            _draw_session_row(stdscr, row_y, w, s, frame, i == selected, delta_history)

            # Blocked detail on next line
            if s["activity"] == "blocked" and s["blocked_detail"]:
                row_y2 = row_y + 1
                if row_y2 < h - 2:
                    _addstr_colored(stdscr, row_y2, 3, f"BLOCKED: {s['blocked_detail']}", 3)

        # Message bar
        if message and now < message_until:
            _addstr_colored(stdscr, h - 2, 2, message[:w - 4], 4, curses.A_BOLD)

        # Footer
        n_active = sum(1 for s in sessions if s["activity"] == "active")
        n_blocked = sum(1 for s in sessions if s["activity"] == "blocked")
        n_frozen = sum(1 for s in sessions if s["activity"] == "frozen")
        parts = [f"{len(sessions)} sessions"]
        if n_active:
            parts.append(f"{n_active} active")
        if n_blocked:
            parts.append(f"{n_blocked} blocked")
        if n_frozen:
            parts.append(f"{n_frozen} frozen")
        footer = " · ".join(parts)
        _addstr_colored(stdscr, h - 1, 1, footer[:w - 2], 0, curses.A_DIM)

        stdscr.refresh()
        frame += 1

        # Input
        key = stdscr.getch()
        if key == curses.KEY_UP:
            selected = max(0, selected - 1)
        elif key == curses.KEY_DOWN:
            selected = min(len(sessions) - 1, selected + 1)
        elif key in (ord('\n'), curses.KEY_ENTER, 10, 13):
            s = sessions[selected]
            if s["activity"] == "frozen":
                # Thaw: create new tmux session and resume
                entry = smap.get(s["name"])
                if entry:
                    cwd = entry.get("cwd", os.getcwd())
                    create_session(s["name"])
                    time.sleep(0.5)
                    send_keys(s["name"], f"cd {cwd} && claude --resume {entry['session_id']}")
                    smap.thaw(s["name"])
                # Open in a new tmux window — watch keeps running
                run_tmux(["new-window", "-n", s["name"], f"tmux attach -t {s['name']}"])
                message = f"Opened '{s['name']}' in new window"
                message_until = now + 3
                last_poll = 0
            elif s["activity"] == "exited":
                # Claude exited — resume inside the existing shell
                entry = smap.get(s["name"])
                if entry and entry.get("session_id"):
                    send_keys(s["name"], f"claude --resume {entry['session_id']}")
                else:
                    send_keys(s["name"], "claude --resume")
                run_tmux(["new-window", "-n", s["name"], f"tmux attach -t {s['name']}"])
                message = f"Resuming '{s['name']}' in new window"
                message_until = now + 3
                last_poll = 0
            elif session_exists(s["name"]):
                run_tmux(["new-window", "-n", s["name"], f"tmux attach -t {s['name']}"])
                message = f"Opened '{s['name']}' in new window"
                message_until = now + 3
        elif key == ord('f'):
            s = sessions[selected]
            if s["activity"] not in ("frozen", "dead"):
                kill_session(s["name"])
                smap.freeze(s["name"])
                message = f"Frozen '{s['name']}'"
                message_until = now + 3
                last_poll = 0
        elif key == ord('a'):
            s = sessions[selected]
            if s["activity"] == "blocked":
                send_key(s["name"], "Enter")
                message = f"Accepted prompt in '{s['name']}'"
                message_until = now + 3
                last_poll = 0
        elif key == ord('q') or key == 27:
            return


def _freezer_interactive(stdscr):
    """Browse and manage frozen sessions."""
    curses.curs_set(0)
    stdscr.nodelay(False)

    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(3, curses.COLOR_RED, -1)

    smap = SessionMap()
    selected = 0

    while True:
        frozen = sorted(smap.frozen().items())
        h, w = stdscr.getmaxyx()
        stdscr.erase()

        header = " puppet freezer ─── ↑↓ select · Enter thaw+attach · d delete · q quit"
        stdscr.addstr(0, 0, header[:w - 1], curses.A_BOLD)

        if not frozen:
            stdscr.addstr(2, 2, "No frozen sessions.", curses.A_DIM)
            stdscr.addstr(3, 2, "Freeze a session with: puppet freeze NAME")
            _addstr_colored(stdscr, h - 1, 1, "Press q to quit", 0, curses.A_DIM)
            stdscr.refresh()
            key = stdscr.getch()
            if key == ord('q') or key == 27:
                return
            continue

        selected = max(0, min(selected, len(frozen) - 1))

        col_hdr = f"   {'name':<20s} {'agent':<8s} {'frozen':<12s} session ID"
        stdscr.addstr(1, 0, col_hdr[:w - 1], curses.A_DIM)

        for i, (name, entry) in enumerate(frozen):
            if i + 2 >= h - 2:
                break
            agent = entry.get("agent", "?")[:8].ljust(8)
            frozen_at = entry.get("frozen_at", "")[:10]
            sid = entry.get("session_id", "")[:20]
            row = f" ❄ {name:<20s} {agent} {frozen_at:<12s} {sid}"

            attr = curses.A_REVERSE if i == selected else curses.A_NORMAL
            try:
                stdscr.addstr(i + 2, 0, row[:w - 1].ljust(w - 1), attr)
            except curses.error:
                pass

        _addstr_colored(stdscr, h - 1, 1, f"{len(frozen)} frozen sessions", 0, curses.A_DIM)
        stdscr.refresh()

        key = stdscr.getch()
        if key == curses.KEY_UP:
            selected = max(0, selected - 1)
        elif key == curses.KEY_DOWN:
            selected = min(len(frozen) - 1, selected + 1)
        elif key in (ord('\n'), curses.KEY_ENTER, 10, 13):
            name, entry = frozen[selected]
            sid = entry.get("session_id", "")
            cwd = entry.get("cwd", os.getcwd())
            if sid:
                create_session(name, f"cd {cwd} && claude --resume {sid}")
                smap.thaw(name)
                time.sleep(1)
                curses.endwin()
                os.execlp("tmux", "tmux", "attach", "-t", name)
        elif key == ord('d'):
            name, _ = frozen[selected]
            smap.remove(name)
        elif key == ord('q') or key == 27:
            return


def watch_interactive(interval: int = 5):
    curses.wrapper(lambda stdscr: _watch_interactive(stdscr, interval))


def freezer():
    curses.wrapper(_freezer_interactive)
