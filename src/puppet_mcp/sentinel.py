#!/Users/zach/anaconda/bin/python3
"""Puppet sentinel — zero-token background monitor for tmux Claude sessions.

Polls tmux sessions on an interval, detects state transitions, and queues
events for registered subscribers via ~/.puppet-mcp/subscriptions.json.

Agents register interest via the sentinel_register MCP tool and poll
events via sentinel_poll. No tmux send-keys injection — purely file-based.

Reuses puppet_mcp.tmux for all tmux parsing — no duplicated logic.

Env vars:
  PUPPET_SENTINEL_INTERVAL  — poll interval in seconds (default 30)
  PUPPET_DATA_DIR           — state dir (default ~/.puppet-mcp)
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .tmux import (
    capture_pane,
    classify_activity,
    content_lines,
    detect_context_window,
    extract_permission_content,
    list_sessions,
    parse_status_bar,
    run_tmux,
)

INTERVAL = int(os.environ.get("PUPPET_SENTINEL_INTERVAL", "30"))
DATA_DIR = Path(os.environ.get("PUPPET_DATA_DIR", "~/.puppet-mcp")).expanduser()
STATE_FILE = DATA_DIR / "sentinel-state.json"
SUBS_FILE = DATA_DIR / "subscriptions.json"
QUEUE_DIR = DATA_DIR / "queues"

PID_FILE = DATA_DIR / "sentinel.pid"
LOG_FILE = DATA_DIR / "sentinel.log"

THRESH_WARN = 0.70
THRESH_CRIT = 0.85

# --- Interest parsing ---

_ALL_EVENTS = [
    "blocked", "unblocked", "died", "exited", "completed",
    "new_session", "context_70", "context_85", "stale", "fleet_summary",
]

_INTEREST_MAP = {
    "block": ["blocked", "unblocked"], "stuck": ["blocked"], "permission": ["blocked"],
    "context": ["context_70", "context_85"], "warn": ["context_70", "context_85"],
    "die": ["died"], "death": ["died"], "dead": ["died"],
    "exit": ["exited"],
    "complete": ["completed"], "completion": ["completed"], "finish": ["completed"], "done": ["completed"], "idle": ["completed"],
    "new": ["new_session"], "spawn": ["new_session"],
    "stale": ["stale"],
    "all": _ALL_EVENTS, "everything": _ALL_EVENTS,
    "fleet": ["fleet_summary"], "summary": ["fleet_summary"], "status": ["fleet_summary"],
}


def parse_interests(text: str) -> list[str]:
    """Map natural language to event filter list."""
    words = text.lower().replace(",", " ").split()
    result: set[str] = set()
    for w in words:
        if w in _INTEREST_MAP:
            result.update(_INTEREST_MAP[w])
        elif w.rstrip("s") in _INTEREST_MAP:
            result.update(_INTEREST_MAP[w.rstrip("s")])
        elif w in _ALL_EVENTS:
            result.add(w)
    return sorted(result) if result else _ALL_EVENTS


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def snapshot() -> dict[str, dict]:
    """Capture current state of all tmux sessions."""
    result = run_tmux(["ls"])
    if result.returncode != 0:
        return {}

    snap = {}
    for tmux_line in result.stdout.strip().split("\n"):
        if not tmux_line.strip():
            continue
        name = tmux_line.split(":")[0].strip()
        pane = capture_pane(name, 15)
        bar = parse_status_bar(pane)
        activity = classify_activity(pane, tmux_line)
        tokens = bar.get("tokens")
        agent = bar.get("agent", "?")
        ctx = None
        if tokens:
            ctx = bar.get("context_window") or detect_context_window(name)
        snap[name] = {
            "activity": activity,
            "tokens": tokens,
            "agent": agent,
            "context_window": ctx,
        }
    return snap


def context_band(tokens: int | None, ctx: int | None) -> str:
    if not tokens or not ctx:
        return "ok"
    pct = tokens / ctx
    if pct >= THRESH_CRIT:
        return "crit"
    if pct >= THRESH_WARN:
        return "warn"
    return "ok"


def _load_subscriptions() -> dict:
    if SUBS_FILE.exists():
        try:
            return json.loads(SUBS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _queue_event(subscriber: str, event: dict):
    """Append an event to a subscriber's queue file atomically."""
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    queue_file = QUEUE_DIR / f"{subscriber}.json"

    existing = []
    if queue_file.exists():
        try:
            existing = json.loads(queue_file.read_text())
        except (json.JSONDecodeError, OSError):
            existing = []

    existing.append(event)

    tmp = tempfile.NamedTemporaryFile(mode="w", dir=QUEUE_DIR, suffix=".tmp", delete=False)
    try:
        tmp.write(json.dumps(existing) + "\n")
        tmp.close()
        os.replace(tmp.name, queue_file)
    except Exception:
        tmp.close()
        Path(tmp.name).unlink(missing_ok=True)


def _dispatch_event(subs: dict, event_type: str, session: str, detail: str):
    """Queue an event to all subscribers whose filters include event_type."""
    now = datetime.now(timezone.utc).isoformat()
    event = {"time": now, "type": event_type, "session": session, "detail": detail}
    for name, sub in subs.items():
        if event_type in sub.get("filters", []):
            _queue_event(name, event)


def _generate_fleet_summary(curr: dict) -> str:
    total = len(curr)
    active = sum(1 for s in curr.values() if s["activity"] == "active")
    blocked = sum(1 for s in curr.values() if s["activity"] == "blocked")
    idle = sum(1 for s in curr.values() if s["activity"] in ("idle", "stale"))

    parts = [f"{total} sessions: {active} active"]
    if idle:
        parts.append(f"{idle} idle")
    if blocked:
        parts.append(f"{blocked} blocked")

    # Context warnings
    warns = []
    for name, s in curr.items():
        tok, ctx = s.get("tokens"), s.get("context_window")
        if tok and ctx:
            pct = int(tok / ctx * 100)
            if pct >= 70:
                warns.append(f"{name} at {pct}%")
    if warns:
        parts.append(". ".join(warns))

    return ". ".join(parts) + "."


def _parse_cadence_seconds(cadence: str) -> int | None:
    """Parse cadence string to seconds. Returns None for 'immediate'."""
    if cadence in ("immediate", "0", ""):
        return None
    c = cadence.strip().lower()
    if c.endswith("m"):
        try:
            return int(c[:-1]) * 60
        except ValueError:
            return None
    if c.endswith("s"):
        try:
            return int(c[:-1])
        except ValueError:
            return None
    try:
        return int(c)
    except ValueError:
        return None


def _check_periodic_summaries(subs: dict, curr: dict):
    """Generate fleet summaries for subscribers whose cadence has elapsed."""
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    updated = False

    for name, sub in subs.items():
        if "fleet_summary" not in sub.get("filters", []):
            continue
        interval = _parse_cadence_seconds(sub.get("cadence", "immediate"))
        if interval is None:
            continue

        last = sub.get("last_summary", "")
        try:
            last_dt = datetime.fromisoformat(last)
            elapsed = (now - last_dt).total_seconds()
        except (ValueError, TypeError):
            elapsed = interval + 1

        if elapsed >= interval:
            summary = _generate_fleet_summary(curr)
            if summary == sub.get("last_summary_text", ""):
                sub["last_summary"] = now_iso
                updated = True
                continue
            event = {"time": now_iso, "type": "fleet_summary", "session": "", "detail": summary}
            _queue_event(name, event)
            sub["last_summary"] = now_iso
            sub["last_summary_text"] = summary
            updated = True

    if updated:
        # Write back updated last_summary timestamps
        sf = SUBS_FILE
        tmp = tempfile.NamedTemporaryFile(mode="w", dir=sf.parent, suffix=".tmp", delete=False)
        try:
            tmp.write(json.dumps(subs, indent=2) + "\n")
            tmp.close()
            os.replace(tmp.name, sf)
        except Exception:
            tmp.close()
            Path(tmp.name).unlink(missing_ok=True)


# --- Lifecycle functions ---

def is_running() -> tuple[bool, int | None]:
    """Check if sentinel is running via PID file. Returns (alive, pid)."""
    if not PID_FILE.exists():
        return False, None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return True, pid
    except (ValueError, OSError):
        return False, None


def start_sentinel() -> dict:
    """Start sentinel daemon if not running. Idempotent."""
    alive, pid = is_running()
    if alive:
        return {"running": True, "pid": pid, "started": False}

    exe = shutil.which("puppet-sentinel")
    if exe:
        cmd = [exe]
    else:
        cmd = [sys.executable, "-m", "puppet_mcp.sentinel"]

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log_fh = open(LOG_FILE, "a")
    proc = subprocess.Popen(cmd, stdout=log_fh, stderr=log_fh, start_new_session=True)

    # Brief wait to verify it started
    for _ in range(10):
        time.sleep(0.3)
        alive, pid = is_running()
        if alive:
            return {"running": True, "pid": pid, "started": True}

    return {"running": False, "pid": proc.pid, "started": False}


def stop_sentinel() -> dict:
    """Stop sentinel daemon."""
    alive, pid = is_running()
    if not alive:
        PID_FILE.unlink(missing_ok=True)
        return {"stopped": True, "pid": None}
    try:
        os.kill(pid, signal.SIGTERM)
        # Wait for exit
        for _ in range(20):
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except OSError:
                break
    except OSError:
        pass
    PID_FILE.unlink(missing_ok=True)
    return {"stopped": True, "pid": pid}


def sentinel_status(name: str = "") -> dict:
    """Return sentinel health + subscriber info.
    name="" -> full fleet, name="X" -> just that subscriber.
    """
    alive, pid = is_running()
    result: dict = {"running": alive, "pid": pid}

    # Daemon state
    state = load_state()
    result["last_tick"] = state.get("timestamp")
    result["session_count"] = len(state.get("sessions", {}))

    subs = _load_subscriptions()

    if name:
        sub = subs.get(name)
        if not sub:
            result["subscriber"] = None
            return result
        queue_file = QUEUE_DIR / f"{name}.json"
        depth = 0
        if queue_file.exists():
            try:
                depth = len(json.loads(queue_file.read_text()))
            except (json.JSONDecodeError, OSError):
                pass
        result["subscriber"] = {**sub, "queue_depth": depth}
        return result

    # Full fleet
    subscribers = {}
    for sname, sub in subs.items():
        queue_file = QUEUE_DIR / f"{sname}.json"
        depth = 0
        if queue_file.exists():
            try:
                depth = len(json.loads(queue_file.read_text()))
            except (json.JSONDecodeError, OSError):
                pass
        subscribers[sname] = {**sub, "queue_depth": depth}
    result["subscribers"] = subscribers
    return result


# --- Subscription management ---

def _atomic_write_json(path: Path, data):
    """Write JSON atomically via temp + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(mode="w", dir=path.parent, suffix=".tmp", delete=False)
    try:
        tmp.write(json.dumps(data, indent=2) + "\n")
        tmp.close()
        os.replace(tmp.name, path)
    except Exception:
        tmp.close()
        Path(tmp.name).unlink(missing_ok=True)
        raise


def register_subscriber(name: str, filters: list[str], cadence: str) -> str:
    """Register a subscriber. Returns confirmation string."""
    subs = _load_subscriptions()
    subs[name] = {
        "filters": filters,
        "cadence": cadence,
        "registered": datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_json(SUBS_FILE, subs)
    return f"registered {name}: filters={filters}, cadence={cadence}"


def unregister_subscriber(name: str) -> str:
    """Remove subscriber and delete queue file."""
    subs = _load_subscriptions()
    removed = name in subs
    subs.pop(name, None)
    _atomic_write_json(SUBS_FILE, subs)
    queue_file = QUEUE_DIR / f"{name}.json"
    queue_file.unlink(missing_ok=True)
    return f"unregistered {name}" if removed else f"{name} not found"


def poll_subscriber(name: str) -> list[dict]:
    """Read and clear event queue. Returns list of events."""
    queue_file = QUEUE_DIR / f"{name}.json"
    if not queue_file.exists():
        return []
    try:
        events = json.loads(queue_file.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    if events:
        _atomic_write_json(queue_file, [])
    return events


def diff_and_notify(prev_sessions: dict, curr: dict):
    """Compare states and queue events for matching subscriptions."""
    subs = _load_subscriptions()
    if not subs:
        return

    prev_names = set(prev_sessions.keys())
    curr_names = set(curr.keys())

    for name in sorted(curr_names - prev_names):
        s = curr[name]
        _dispatch_event(subs, "new_session", name, f"agent={s['agent']}")

    for name in sorted(prev_names - curr_names):
        p = prev_sessions[name]
        _dispatch_event(subs, "died", name, f"was {p.get('activity', '?')}, agent={p.get('agent', '?')}")

    for name in sorted(curr_names & prev_names):
        c = curr[name]
        p = prev_sessions[name]
        prev_act = p.get("activity", "?")
        curr_act = c["activity"]

        if curr_act == "blocked" and prev_act != "blocked":
            # Try to extract permission prompt detail
            detail = f"was {prev_act}"
            try:
                pane = capture_pane(name, 15)
                info = extract_permission_content(pane)
                if info:
                    detail = f"permission prompt for {info['tool']}"
            except Exception:
                pass
            _dispatch_event(subs, "blocked", name, detail)

        if prev_act == "blocked" and curr_act != "blocked":
            _dispatch_event(subs, "unblocked", name, f"now {curr_act}")

        if curr_act == "idle" and prev_act == "active":
            # Lightweight event only — agents report completion themselves
            # via puppet_send(action="report"). No sentinel intervention.
            _dispatch_event(subs, "completed", name, "idle")

        if curr_act == "stale" and prev_act != "stale":
            _dispatch_event(subs, "stale", name, f"was {prev_act}")

        # Context band transitions
        prev_band = context_band(p.get("tokens"), p.get("context_window"))
        curr_band = context_band(c.get("tokens"), c.get("context_window"))

        if curr_band != prev_band:
            tok = c.get("tokens") or 0
            ctx = c.get("context_window") or 1
            pct = int(tok / ctx * 100)
            label = "1M" if ctx >= 1_000_000 else f"{ctx // 1000}K"
            if curr_band == "warn" and prev_band == "ok":
                _dispatch_event(subs, "context_70", name, f"{pct}% context ({tok:,}/{label})")
            elif curr_band == "crit":
                _dispatch_event(subs, "context_85", name, f"{pct}% context ({tok:,}/{label})")

    _check_periodic_summaries(subs, curr)


def run_loop():
    """Main polling loop — runs until killed."""
    print(f"puppet-sentinel starting: interval={INTERVAL}s mode=subscription", flush=True)
    prev = load_state()
    while True:
        try:
            curr = snapshot()
            prev_sessions = prev.get("sessions", {})
            if prev_sessions:
                diff_and_notify(prev_sessions, curr)
            prev = {"sessions": curr, "timestamp": datetime.now(timezone.utc).isoformat()}
            save_state(prev)
        except Exception as e:
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"{ts} [sentinel] error: {e}", flush=True)
        time.sleep(INTERVAL)


def main():
    """Entry point with signal handling and PID management."""
    def _handle(signum, frame):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"{ts} puppet-sentinel exiting (signal {signum})", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()) + "\n")
    try:
        run_loop()
    finally:
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()


def parse_cadence(cadence: str) -> str:
    """Public wrapper: normalize cadence string."""
    c = cadence.strip().lower()
    if c in ("", "immediate", "0"):
        return "immediate"
    return c
