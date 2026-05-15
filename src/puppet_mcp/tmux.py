"""All tmux interaction helpers."""

import re
import subprocess


def run_tmux(args: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
    """Run a tmux command, capture output."""
    return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=timeout)


def send_keys(name: str, text: str):
    """Send text literally then press Enter. Two separate tmux calls
    to guarantee Enter is never swallowed or concatenated."""
    run_tmux(["send-keys", "-t", name, "-l", text], timeout=5)
    run_tmux(["send-keys", "-t", name, "Enter"], timeout=5)


def send_key(name: str, key: str):
    """Send a single special key (Enter, Escape, C-c)."""
    run_tmux(["send-keys", "-t", name, key], timeout=5)


def session_exists(name: str) -> bool:
    """Check if a tmux session exists."""
    return run_tmux(["has-session", "-t", name]).returncode == 0


def has_attached_client(name: str) -> bool:
    """Check if a human terminal is attached to this tmux session."""
    result = run_tmux(["list-clients", "-t", name])
    return result.returncode == 0 and bool(result.stdout.strip())


def capture_pane(name: str, lines: int = 30) -> str:
    """Capture and trim pane content, stripping leading blank lines."""
    result = run_tmux(["capture-pane", "-t", name, "-p", "-S", f"-{lines}"])
    if result.returncode != 0:
        return ""
    out_lines = result.stdout.rstrip("\n").split("\n")
    while out_lines and not out_lines[0].strip():
        out_lines.pop(0)
    return "\n".join(out_lines)


def list_sessions() -> list[str]:
    """Return list of tmux session names."""
    result = run_tmux(["ls"])
    if result.returncode != 0:
        return []
    names = []
    for line in result.stdout.strip().split("\n"):
        if line.strip():
            names.append(line.split(":")[0].strip())
    return names


def kill_session(name: str) -> subprocess.CompletedProcess:
    """Kill a tmux session."""
    return run_tmux(["kill-session", "-t", name])


def create_session(name: str, command: str | None = None) -> subprocess.CompletedProcess:
    """Create a detached tmux session, optionally running a command."""
    args = ["new-session", "-d", "-s", name, "-x", "220", "-y", "50"]
    if command:
        args.append(command)
    return run_tmux(args)


def has_claude_process(name: str) -> bool:
    """Check if a claude process is running inside the tmux session."""
    result = run_tmux(["list-panes", "-t", name, "-F", "#{pane_pid}"])
    if result.returncode != 0 or not result.stdout.strip():
        return False
    shell_pid = result.stdout.strip().split("\n")[0]
    child = subprocess.run(
        ["pgrep", "-P", shell_pid, "-f", "claude"],
        capture_output=True, text=True, timeout=5,
    )
    return child.returncode == 0 and bool(child.stdout.strip())


def clean_pane(pane_text: str) -> str:
    """Strip terminal control sequences but keep unicode."""
    return re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', pane_text)


def is_idle(pane_text: str) -> bool:
    """Detect if a session is idle (showing a prompt)."""
    lines = [l for l in pane_text.split("\n") if l.strip()]
    if not lines:
        return False
    last = lines[-1].rstrip()
    return (
        last.endswith("> ") or last.endswith("$ ")
        or last.endswith("❯") or last.endswith("❯ ")
        or re.search(r'❯\s*$', last) is not None
    )


def parse_status_bar(pane_text: str) -> dict:
    """Parse Claude Code's status bar for tokens, agent name, context window, and model."""
    info: dict = {"tokens": None, "agent": None, "context_window": None, "model": None}
    cleaned = clean_pane(pane_text)
    for line in reversed(cleaned.split("\n")):
        stripped = line.strip()
        if info["tokens"] is None:
            m = re.search(r'(\d[\d,]*)\s+tokens\s*$', stripped)
            if m:
                info["tokens"] = int(m.group(1).replace(",", ""))
        if info["agent"] is None:
            m = re.search(r'[─━]+\s+(\w[\w-]*)\s+[─━]+', stripped)
            if m:
                info["agent"] = m.group(1)
            elif len(stripped) < 30 and re.match(r'^[a-z][\w-]*$', stripped):
                info["agent"] = stripped
        if info["context_window"] is None:
            m = re.search(r'(\d+)\s*([KkMm])\s*context', stripped)
            if m:
                num = int(m.group(1))
                unit = m.group(2).upper()
                info["context_window"] = num * (1_000_000 if unit == 'M' else 1_000)
        if info["model"] is None:
            m = re.search(r'(Opus|Sonnet|Haiku)\s', stripped, re.I)
            if m:
                info["model"] = m.group(1).lower()
        if all(v is not None for v in info.values()):
            break
    return info


def detect_context_window(name: str, tokens: int = 0) -> int:
    """Detect the context window size for a tmux session.

    Strategy (in order):
    1. If tokens > 195k, it's definitely 1M — a 200k session would have
       hit the wall before reaching this count.
    2. Scan pane scrollback for "1M context" (startup banner or /model output).
    3. Default to 200k.

    Args:
        name: tmux session name
        tokens: current token count (if known) for heuristic detection
    """
    if tokens > 195_000:
        return 1_000_000
    result = run_tmux(["capture-pane", "-t", name, "-p", "-S", "-2000"])
    if result.returncode != 0:
        return 200_000
    if "1M context" in result.stdout or "1m context" in result.stdout:
        return 1_000_000
    return 200_000


def _is_status_bar_line(line: str) -> bool:
    """Return True if the line is Claude Code chrome, not user content."""
    s = line.strip()
    if not s:
        return True
    # Token counter: "12345 tokens" or "191,585 tokens"
    if re.search(r'\d[\d,]*\s+tokens\s*$', s):
        return True
    # Model / context-window line: "Opus 4.6 (1M context)"
    if re.search(r'\d+\s*[KkMm]\s*context', s):
        return True
    # Agent name bar: "── haak ──" or just "haak" alone on a short line
    if re.match(r'^[─━\s]+\w[\w-]*[─━\s]+$', s):
        return True
    if len(s) < 30 and re.match(r'^[a-z][\w-]*$', s):
        return True
    # Permission/mode line
    if "shift+tab to cycle" in s or "bypass permissions" in s or "accept edits" in s:
        return True
    # Interrupt hint
    if s.startswith("esc to interrupt") or "· esc to interrupt" in s:
        return True
    # Version line: "current: 2.1.141  latest: 2.1.142"
    if re.match(r'^current:\s', s) or re.search(r'current:\s.*latest:', s):
        return True
    # Line is only box-drawing chars / whitespace
    if re.match(r'^[─━═┄┈\s]+$', s):
        return True
    return False


def content_lines(pane_text: str, n: int = 3) -> list[str]:
    """Extract the last N meaningful content lines from a pane, stripping all
    Claude Code status bar chrome."""
    cleaned = clean_pane(pane_text)
    lines = [l.strip() for l in cleaned.split("\n") if l.strip()]
    lines = [l for l in lines if not _is_status_bar_line(l)]
    return lines[-n:] if lines else []


_PERMISSION_PATTERNS = [
    re.compile(r'Do you want to proceed', re.I),
    re.compile(r'Do you want to make this edit', re.I),
    re.compile(r"Yes,? and don.?t ask again", re.I),
    re.compile(r'Allow .*\?'),
    re.compile(r'allow once', re.I),
    re.compile(r'allow for this', re.I),
    re.compile(r'deny', re.I),
    re.compile(r'Permission rule', re.I),
    re.compile(r'Esc to cancel'),
]


def extract_permission_content(pane_text: str) -> dict | None:
    """Extract structured permission prompt content.

    Returns None if no permission prompt detected.
    Returns dict with tool, detail, and raw text.
    """
    cleaned = clean_pane(pane_text)
    tail = cleaned.split("\n")[-15:]
    text = "\n".join(tail)

    if not any(p.search(text) for p in _PERMISSION_PATTERNS):
        return None

    # Parse tool name and detail from prompt content
    tool = "unknown"
    detail = ""

    # Pattern: "Bash(command here)" or "Read(path)" etc.
    m = re.search(r'(Bash|Read|Write|Edit|Glob|Grep|Agent|NotebookEdit)\(([^)]*)\)', text)
    if m:
        tool = m.group(1)
        detail = m.group(2)[:200]
    else:
        # Pattern: "Tool: Bash" / "Command: ..." in box format
        tm = re.search(r'Tool:\s*(\w+)', text)
        if tm:
            tool = tm.group(1)
        dm = re.search(r'Command:\s*(.+)', text)
        if dm:
            detail = dm.group(1).strip()[:200]

    # Clean raw text — strip blank lines
    raw = "\n".join(l for l in tail if l.strip())

    return {"tool": tool, "detail": detail, "raw": raw}


def detect_permission_prompt(pane_text: str) -> bool:
    """Detect if a session is showing a permission prompt.

    Scans the last 10 lines for Claude Code permission dialog patterns.
    Works regardless of whether the session appears idle or active.
    """
    cleaned = clean_pane(pane_text)
    # Only check the tail — permission prompts are at the bottom
    lines = cleaned.split("\n")[-10:]
    text = "\n".join(lines)
    return any(p.search(text) for p in _PERMISSION_PATTERNS)


def classify_activity(pane_text: str, tmux_line: str = "", tmux_name: str = "") -> str:
    """Classify session activity: active, idle, stale, dead, blocked, exited."""
    cleaned = clean_pane(pane_text)
    if tmux_line and "(dead)" in tmux_line:
        return "dead"

    # Permission prompt overrides everything — session is blocked
    if detect_permission_prompt(pane_text):
        return "blocked"

    working_indicators = [
        "Running", "Flambéing", "Boogieing", "Crunching", "Sautéed",
        "Worked for", "Crunched for", "thought for", "esc to interrupt",
    ]
    lines = cleaned.split("\n")
    for line in lines[-5:]:
        for indicator in working_indicators:
            if indicator in line:
                return "active"
    if is_idle(pane_text):
        for line in lines[-10:]:
            if "recap:" in line.lower() or "disable recaps" in line.lower():
                return "stale"
        return "idle"

    # No claude indicators found — check if claude is actually running.
    # If tmux session exists but no claude process, claude exited and
    # we're looking at a bare shell.
    if tmux_name:
        # Check for "tokens" in pane — if present, claude is running
        if "tokens" not in cleaned:
            try:
                if not has_claude_process(tmux_name):
                    return "exited"
            except Exception:
                pass

    return "active"
