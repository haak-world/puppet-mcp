"""Session identity management — map, PID chain, discovery."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import data_dir, project_dir
from .tmux import run_tmux, capture_pane

CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "projects"
CLAUDE_SESSION_STORE = Path.home() / ".claude" / "sessions"


class SessionMap:
    """Persistent tmux-name to session-info mapping."""

    def __init__(self, path: Path | None = None):
        self.path = path or (data_dir() / "puppet-sessions.json")

    def load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def save(self, data: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2) + "\n")

    def record(self, name: str, session_id: str, agent: str = "", cwd: str = "", parent: str = "", role: str = ""):
        smap = self.load()
        entry = {
            "session_id": session_id,
            "agent": agent,
            "cwd": cwd or project_dir(),
            "launched_at": datetime.now(timezone.utc).isoformat(),
        }
        if parent:
            entry["parent"] = parent
        if role:
            entry["role"] = role
        smap[name] = entry
        self.save(smap)

    def children(self, name: str) -> list[str]:
        """Return names of sessions whose parent is this session."""
        smap = self.load()
        return [n for n, v in smap.items() if v.get("parent") == name]

    def tree(self) -> dict[str, list[str]]:
        """Return {parent: [children]} mapping. Root sessions have parent ''."""
        smap = self.load()
        result: dict[str, list[str]] = {"": []}
        for name, v in smap.items():
            parent = v.get("parent", "")
            result.setdefault(parent, []).append(name)
        return result

    def freeze(self, name: str):
        """Mark a session as frozen (stopped but restorable)."""
        smap = self.load()
        if name in smap:
            smap[name]["status"] = "frozen"
            smap[name]["frozen_at"] = datetime.now(timezone.utc).isoformat()
            self.save(smap)

    def thaw(self, name: str):
        """Remove frozen status from a session."""
        smap = self.load()
        if name in smap:
            smap[name].pop("status", None)
            smap[name].pop("frozen_at", None)
            self.save(smap)

    def frozen(self) -> dict[str, dict]:
        """Return all frozen sessions."""
        return {n: v for n, v in self.load().items() if v.get("status") == "frozen"}

    def remove(self, name: str):
        smap = self.load()
        smap.pop(name, None)
        self.save(smap)

    def get(self, name: str) -> dict | None:
        return self.load().get(name)


def get_claude_session_info(tmux_name: str) -> dict | None:
    """Get Claude Code session ID via PID chain.

    Chain: tmux session -> shell PID -> claude child PID -> session file -> ID.
    """
    result = run_tmux(["list-panes", "-t", tmux_name, "-F", "#{pane_pid}"])
    if result.returncode != 0 or not result.stdout.strip():
        return None
    shell_pid = result.stdout.strip().split("\n")[0]

    import subprocess
    child_result = subprocess.run(
        ["pgrep", "-P", shell_pid], capture_output=True, text=True, timeout=5,
    )
    if child_result.returncode != 0 or not child_result.stdout.strip():
        return None
    claude_pid = child_result.stdout.strip().split("\n")[0]

    session_file = CLAUDE_SESSION_STORE / f"{claude_pid}.json"
    if not session_file.exists():
        return None

    try:
        session_data = json.loads(session_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    session_id = session_data.get("sessionId")
    if not session_id:
        return None

    return {
        "session_id": session_id,
        "claude_pid": claude_pid,
        "agent": session_data.get("agent"),
        "cwd": session_data.get("cwd", project_dir()),
    }


def resolve_session_id(name: str, session_map: SessionMap) -> dict | None:
    """Resolve session ID from map first, fall back to PID chain."""
    entry = session_map.get(name)
    if entry and entry.get("session_id"):
        return {
            "session_id": entry["session_id"],
            "agent": entry.get("agent", ""),
            "cwd": entry.get("cwd", project_dir()),
        }
    return get_claude_session_info(name)


_UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I,
)


def _find_jsonl_by_sid(sid: str) -> Path | None:
    """Search all project dirs under ~/.claude/projects/ for a session JSONL."""
    for jsonl in CLAUDE_SESSIONS_DIR.rglob(f"{sid}.jsonl"):
        return jsonl
    return None


def find_session_jsonl(name: str, session_map: SessionMap) -> Path | None:
    """Find the JSONL transcript for a tmux session name or session ID."""
    # Try session map (name is a tmux session name)
    entry = session_map.get(name)
    if entry:
        sid = entry.get("session_id")
        if sid:
            found = _find_jsonl_by_sid(sid)
            if found:
                return found

    # If name itself looks like a UUID, try it as a session ID directly
    if _UUID_RE.match(name):
        found = _find_jsonl_by_sid(name)
        if found:
            return found

    # Try PID chain (name is a tmux session name)
    info = get_claude_session_info(name)
    if info:
        found = _find_jsonl_by_sid(info["session_id"])
        if found:
            return found

    # Fallback: scan tmux pane for UUID
    cap = run_tmux(["capture-pane", "-t", name, "-p", "-S", "-200"])
    if cap.returncode == 0:
        for line in cap.stdout.split("\n"):
            match = re.search(
                r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',
                line, re.I,
            )
            if match:
                found = _find_jsonl_by_sid(match.group(1))
                if found:
                    return found

    return None


def resolve_session_cwd(
    session_id: str,
    session_map: SessionMap,
    default: str = "",
) -> str:
    """Resolve the CWD for a session ID.

    Sessions must be resumed from the CWD they were created in —
    claude --resume searches ~/.claude/projects/<encoded-cwd>/.

    Resolution order:
      1. Session map (puppet-sessions.json)
      2. Claude process metadata (~/.claude/sessions/PID.json)
      3. JSONL path encoding (decode project dir back to a path)
      4. default (caller provides, typically project_dir())
    """
    # 1. Session map
    entry = session_map.get_by_session_id(session_id) if hasattr(session_map, 'get_by_session_id') else None
    if not entry:
        # Search all entries
        for name, v in session_map.load().items():
            if v.get("session_id") == session_id:
                entry = v
                break
    if entry:
        cwd = entry.get("cwd", "")
        if cwd and Path(cwd).is_dir():
            return cwd

    # 2. Claude process metadata
    for f in CLAUDE_SESSION_STORE.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            if data.get("sessionId") == session_id:
                cwd = data.get("cwd", "")
                if cwd and Path(cwd).is_dir():
                    return cwd
        except (json.JSONDecodeError, OSError):
            continue

    # 3. JSONL path encoding
    for p in CLAUDE_SESSIONS_DIR.rglob(f"{session_id}.jsonl"):
        project_dirname = p.parent.name
        if project_dirname == "subagents":
            project_dirname = p.parent.parent.name
        # Decode: -Users-zach-Projects-haak -> /Users/zach/Projects/haak
        # This is ambiguous when path segments contain dashes, so verify
        decoded = "/" + project_dirname.lstrip("-").replace("-", "/")
        if Path(decoded).is_dir():
            return decoded
        break

    return default or str(Path.cwd())


def _cwd_to_project_dir(cwd: str) -> str:
    """Convert a cwd path to Claude Code's project directory name.

    /Users/zach/Projects/haak -> -Users-zach-Projects-haak
    """
    return cwd.replace("/", "-")


def _project_dir_to_cwd(dirname: str) -> str:
    """Best-effort reverse of the encoding. Not perfectly reversible
    (ambiguous when path components contain dashes) but good enough
    for display."""
    return "/" + dirname.lstrip("-").replace("-", "/")


def discover_all_sessions(
    hours: int = 24,
    scope: str = "",
    grep: str = "",
) -> list[dict]:
    """Find Claude Code sessions from ~/.claude/.

    Reads ~/.claude/sessions/*.json (running process metadata) and
    matches to JSONL transcripts in ~/.claude/projects/.

    Args:
        hours: how far back to look (default 24). 0 = no time limit.
        scope: restrict to sessions whose cwd starts with this path.
               Empty = all sessions. "." = current working directory.
        grep: search session transcript content for this text.
              Searches user messages and assistant text blocks.
              Only JSONL files are searched, not pane content.
    """
    if not CLAUDE_SESSION_STORE.exists():
        return []

    import os
    import subprocess
    from datetime import timedelta

    cutoff = None
    if hours > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)

    # Resolve scope to an absolute path for matching
    scope_path = ""
    if scope == ".":
        scope_path = os.getcwd()
    elif scope:
        scope_path = os.path.abspath(os.path.expanduser(scope))

    # If scope is set, compute the project dir prefix for fast filtering
    scope_project_prefix = _cwd_to_project_dir(scope_path) if scope_path else ""

    sessions = []

    for session_file in CLAUDE_SESSION_STORE.glob("*.json"):
        try:
            data = json.loads(session_file.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        session_id = data.get("sessionId", "")
        if not session_id:
            continue

        # Scope filter: check cwd from session metadata
        session_cwd = data.get("cwd", "")
        if scope_path and not session_cwd.startswith(scope_path):
            continue

        # Check if still running
        pid = session_file.stem
        is_running = False
        try:
            result = subprocess.run(
                ["kill", "-0", pid], capture_output=True, timeout=2,
            )
            is_running = result.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            pass

        # Time filter
        try:
            mtime = datetime.fromtimestamp(session_file.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if cutoff and mtime < cutoff and not is_running:
            continue

        # Find matching JSONL — scope to the right project dir if possible
        jsonl_path = None
        jsonl_size = 0
        exchanges = 0

        if scope_project_prefix:
            # Fast path: check only the matching project directory
            for project_subdir in CLAUDE_SESSIONS_DIR.iterdir():
                if project_subdir.name.startswith(scope_project_prefix.lstrip("-")):
                    candidate = project_subdir / f"{session_id}.jsonl"
                    if candidate.exists():
                        jsonl_path = candidate
                        break
        if not jsonl_path:
            # Slow path: search all project directories
            for jsonl in CLAUDE_SESSIONS_DIR.rglob(f"{session_id}.jsonl"):
                jsonl_path = jsonl
                break

        if jsonl_path:
            try:
                jsonl_size = jsonl_path.stat().st_size
                with open(jsonl_path) as f:
                    exchanges = sum(
                        1 for line in f
                        if line.strip() and ('"role":"user"' in line or '"role": "user"' in line)
                    )
            except OSError:
                pass

        # Grep filter: search transcript content
        if grep and jsonl_path:
            grep_lower = grep.lower()
            found = False
            try:
                with open(jsonl_path) as f:
                    for line in f:
                        if grep_lower in line.lower():
                            found = True
                            break
            except OSError:
                pass
            if not found:
                continue
        elif grep and not jsonl_path:
            continue

        # Extract agent and topic from JSONL header
        agent = data.get("agent")
        topic = ""
        if jsonl_path:
            try:
                with open(jsonl_path) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        d = json.loads(line)
                        # Get agent from agentSetting if not in session JSON
                        if not agent and d.get("type") == "agent-setting":
                            agent = d.get("agentSetting", "")
                        # Get topic from first user message
                        if not topic:
                            msg = d.get("message", {})
                            if isinstance(msg, dict) and msg.get("role") == "user":
                                content = msg.get("content", "")
                                if isinstance(content, str):
                                    topic = content[:80].replace("\n", " ")
                        if agent and topic:
                            break
            except (json.JSONDecodeError, OSError):
                pass

        sessions.append({
            "session_id": session_id,
            "pid": pid,
            "is_running": is_running,
            "agent": agent or "",
            "cwd": session_cwd,
            "model": data.get("model", ""),
            "jsonl_path": str(jsonl_path) if jsonl_path else None,
            "size_mb": round(jsonl_size / (1024 * 1024), 1) if jsonl_size else 0,
            "exchanges": exchanges,
            "topic": topic,
            "mtime": mtime.isoformat(),
        })

    sessions.sort(key=lambda s: s.get("mtime", ""), reverse=True)
    return sessions
