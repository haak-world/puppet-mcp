"""Transcript pruning and splitting logic."""

import json
import os
import uuid
from pathlib import Path


def mechanical_prune(
    entries: list[dict],
    max_tool_output: int = 2000,
    keep_resumable: bool = False,
) -> list[dict]:
    """Rule-based pruning of session JSONL entries.

    - Strips metadata entries (agent-setting, permission-mode, etc.)
    - Collapses sequential Read tool calls into summaries
    - Truncates thinking blocks >500 chars
    - Truncates tool outputs >max_tool_output chars
    - Trims large system messages >2000 chars

    If keep_resumable=True, preserves agent-setting, permission-mode,
    and last-prompt entries so the pruned file remains resumable.
    """
    prunable_types = {
        "queue-operation", "file-history-snapshot", "attachment",
    }
    if not keep_resumable:
        prunable_types |= {"agent-setting", "permission-mode", "last-prompt"}

    read_tool_ids: set[str] = set()
    run_start = None
    runs: list[tuple[int, int, list[str]]] = []
    current_run_paths: list[str] = []

    for i, entry in enumerate(entries):
        msg = entry.get("message", {})
        if not isinstance(msg, dict):
            if current_run_paths and len(current_run_paths) >= 2:
                runs.append((run_start, i - 1, current_run_paths[:]))
            current_run_paths = []
            run_start = None
            continue

        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "assistant" and isinstance(content, list):
            tool_uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
            non_tool = [b for b in content if isinstance(b, dict) and b.get("type") not in ("tool_use", "thinking")]
            all_reads = tool_uses and all(b.get("name") == "Read" for b in tool_uses) and not non_tool
            if all_reads:
                if not current_run_paths:
                    run_start = i
                for b in tool_uses:
                    path = b.get("input", {}).get("file_path", "?")
                    short = path.split("/")[-1] if "/" in path else path
                    current_run_paths.append(short)
                    read_tool_ids.add(b.get("id", ""))
            else:
                if current_run_paths and len(current_run_paths) >= 2:
                    runs.append((run_start, i - 1, current_run_paths[:]))
                current_run_paths = []
                run_start = None
        elif role == "user" and isinstance(content, list):
            has_read_results = any(
                isinstance(b, dict) and b.get("type") == "tool_result"
                and b.get("tool_use_id", "") in read_tool_ids
                for b in content
            )
            if not has_read_results and current_run_paths:
                if len(current_run_paths) >= 2:
                    runs.append((run_start, i - 1, current_run_paths[:]))
                current_run_paths = []
                run_start = None
        else:
            if current_run_paths and len(current_run_paths) >= 2:
                runs.append((run_start, i - 1, current_run_paths[:]))
            current_run_paths = []
            run_start = None

    if current_run_paths and len(current_run_paths) >= 2:
        runs.append((run_start, len(entries) - 1, current_run_paths[:]))

    collapsed_indices: set[int] = set()
    collapse_summaries: dict[int, str] = {}
    for start, end, paths in runs:
        for j in range(start, end + 1):
            collapsed_indices.add(j)
        collapse_summaries[start] = f"Read {len(paths)} files: {', '.join(paths)}"

    pruned = []
    for i, entry in enumerate(entries):
        entry_type = entry.get("type", "")
        if entry_type in prunable_types:
            continue
        if i in collapsed_indices:
            if i in collapse_summaries:
                pruned.append({"type": "summary", "note": collapse_summaries[i]})
            continue

        msg = entry.get("message", {})
        if not isinstance(msg, dict):
            pruned.append(entry)
            continue

        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "assistant" and isinstance(content, list):
            new_blocks = []
            for block in content:
                if not isinstance(block, dict):
                    new_blocks.append(block)
                    continue
                if block.get("type") == "thinking":
                    txt = block.get("thinking", "")
                    if len(txt) > 500:
                        block = {**block, "thinking": (
                            txt[:200] + f"\n[...{len(txt) - 400} chars pruned...]\n" + txt[-200:]
                        )}
                new_blocks.append(block)
            entry = {**entry, "message": {**msg, "content": new_blocks}}
            pruned.append(entry)
        elif role == "user" and isinstance(content, list):
            new_blocks = []
            for block in content:
                if not isinstance(block, dict):
                    new_blocks.append(block)
                    continue
                if block.get("type") == "tool_result":
                    result_str = str(block.get("content", ""))
                    if len(result_str) > max_tool_output:
                        block = {**block, "content": (
                            f"[Tool output: {len(result_str)} chars — pruned. "
                            f"First 200: {result_str[:200]}]"
                        )}
                new_blocks.append(block)
            entry = {**entry, "message": {**msg, "content": new_blocks}}
            pruned.append(entry)
        elif role == "user" and isinstance(content, str):
            pruned.append(entry)
        elif entry_type == "system":
            if isinstance(content, str) and len(content) > 2000:
                entry = {**entry, "message": {**msg, "content": (
                    content[:500] + f"\n[...{len(content) - 1000} chars pruned...]\n" + content[-500:]
                )}}
            pruned.append(entry)
        else:
            pruned.append(entry)

    return pruned


# ── session splitting ────────────────────────────────────────────────


def extract_rounds(entries: list[dict]) -> list[dict]:
    """Group JSONL entries into conversation rounds.

    A round starts with a user message and includes all subsequent
    assistant messages, tool_results, system messages, etc. until the
    next user text message. Returns list of {index, user_text, entries}.
    """
    rounds: list[dict] = []
    current: dict | None = None

    for entry in entries:
        msg = entry.get("message", {})
        if not isinstance(msg, dict):
            if current:
                current["entries"].append(entry)
            continue

        role = msg.get("role", "")
        content = msg.get("content", "")

        # New round starts on a user text message (not tool_result)
        is_user_text = role == "user" and isinstance(content, str)
        is_user_tool = role == "user" and isinstance(content, list)

        if is_user_text:
            if current:
                rounds.append(current)
            current = {
                "index": len(rounds),
                "user_text": content[:200],
                "entries": [entry],
            }
        elif current:
            current["entries"].append(entry)

    if current:
        rounds.append(current)

    return rounds


def _round_summary(round_data: dict) -> str:
    """One-line summary of a round for the classifier."""
    user = round_data["user_text"].replace("\n", " ")[:150]
    n_entries = len(round_data["entries"])
    return f"Round {round_data['index']}: [{n_entries} msgs] {user}"


def _make_client():
    """Create an Anthropic client. Prefers Vertex AI; falls back to API key."""
    from anthropic import AnthropicVertex

    project_id = os.environ.get(
        "ANTHROPIC_VERTEX_PROJECT_ID",
        os.environ.get("VERTEX_PROJECT_ID", "cr-mainen"),
    )
    region = os.environ.get(
        "VERTEX_REGION",
        os.environ.get("CLOUD_ML_REGION", "europe-west1"),
    )
    return AnthropicVertex(project_id=project_id, region=region)


_BATCH_SIZE = 80  # max rounds per classifier call


def _parse_classifier_json(text: str) -> dict:
    """Best-effort parse of classifier JSON, handling truncation and fences."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Truncated response — try to salvage partial classifications.
    # Find the classifications dict and parse individual "idx": "topic" pairs.
    import re
    pairs = re.findall(r'"(\d+)"\s*:\s*"([^"]+)"', text)
    if pairs:
        classifications = {k: v for k, v in pairs}
        suggestions = []
        m = re.search(r'"suggestions"\s*:\s*\[([^\]]*)', text)
        if m:
            suggestions = re.findall(r'"([^"]+)"', m.group(1))
        return {"classifications": classifications, "suggestions": suggestions}

    return {"classifications": {}, "suggestions": []}


def _classify_batch(
    rounds: list[dict],
    topics: list[str],
    client,
    model: str,
) -> dict:
    """Classify a single batch of rounds. Returns raw result dict."""
    summaries = "\n".join(_round_summary(r) for r in rounds)
    topic_list = ", ".join(f'"{t}"' for t in topics)

    # Scale max_tokens to the number of rounds — each classification
    # is ~15 tokens ('"123": "topic-name",\n').
    max_tokens = min(max(len(rounds) * 20 + 200, 1024), 8192)

    prompt = f"""Classify each conversation round into one of these topics: {topic_list}

If a round doesn't fit any topic, classify it as "other".
If you notice coherent topics that weren't listed, suggest them.

Rounds:
{summaries}

Respond with JSON only:
{{"classifications": {{"0": "topic", "1": "topic", ...}}, "suggestions": ["topic1", ...]}}"""

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return _parse_classifier_json(response.content[0].text)


def classify_rounds(
    rounds: list[dict],
    topics: list[str],
    model: str = "claude-haiku-4-5",
) -> dict:
    """Use Claude to classify each round into one of the given topics.

    Returns {round_index: topic, ..., "suggestions": [extra topics]}.
    Batches large sessions to avoid output truncation.
    Uses Vertex AI (env: ANTHROPIC_VERTEX_PROJECT_ID, VERTEX_REGION).
    """
    client = _make_client()

    if len(rounds) <= _BATCH_SIZE:
        return _classify_batch(rounds, topics, client, model)

    # Batch: split rounds into chunks, merge results
    all_classifications: dict[str, str] = {}
    all_suggestions: list[str] = []

    for start in range(0, len(rounds), _BATCH_SIZE):
        batch = rounds[start : start + _BATCH_SIZE]
        result = _classify_batch(batch, topics, client, model)
        for idx_str, topic in result.get("classifications", {}).items():
            all_classifications[idx_str] = topic
        for s in result.get("suggestions", []):
            if s not in all_suggestions:
                all_suggestions.append(s)

    return {"classifications": all_classifications, "suggestions": all_suggestions}


def build_split_session(
    original_session_id: str,
    header_entries: list[dict],
    topic: str,
    topic_rounds: list[dict],
    all_topics: list[str],
    agent: str = "",
) -> tuple[str, list[dict]]:
    """Assemble a resumable JSONL for one topic.

    Returns (new_session_id, entries).
    """
    new_sid = str(uuid.uuid4())

    # Header: agent-setting + permission-mode with new session ID
    out: list[dict] = []
    for h in header_entries:
        out.append({**h, "sessionId": new_sid})

    # Borrow metadata from the first real user entry for synthetic messages
    meta: dict = {}
    for rnd in topic_rounds:
        for e in rnd["entries"]:
            if e.get("type") == "user" and e.get("cwd"):
                meta = {k: e[k] for k in ("cwd", "entrypoint", "gitBranch", "version", "userType")
                        if k in e}
                break
        if meta:
            break

    ts = __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ).isoformat()

    # Synthetic context message
    context_text = (
        f"This session was split from {original_session_id}.\n"
        f"Topic: {topic}\n"
        f"Other topics in the original session: {', '.join(t for t in all_topics if t != topic)}\n"
        f"This session contains {len(topic_rounds)} rounds related to '{topic}'."
    )
    context_uuid = str(uuid.uuid4())
    out.append({
        "type": "user",
        "uuid": context_uuid,
        "parentUuid": "",
        "isSidechain": False,
        "message": {"role": "user", "content": context_text},
        "sessionId": new_sid,
        "timestamp": ts,
        "promptId": str(uuid.uuid4()),
        **meta,
    })

    # Synthetic assistant ack
    ack_uuid = str(uuid.uuid4())
    out.append({
        "type": "assistant",
        "uuid": ack_uuid,
        "parentUuid": context_uuid,
        "isSidechain": False,
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": f"Understood. Continuing work on: {topic}"}],
        },
        "sessionId": new_sid,
        "timestamp": ts,
        **meta,
    })

    # Rewrite rounds with new session ID and linear uuid chain
    prev_uuid = ack_uuid
    for rnd in topic_rounds:
        for entry in rnd["entries"]:
            new_uuid = str(uuid.uuid4())
            rewritten = {**entry, "uuid": new_uuid, "parentUuid": prev_uuid, "sessionId": new_sid}
            out.append(rewritten)
            prev_uuid = new_uuid

    # last-prompt — include the last user text for display in resume picker
    last_user_text = ""
    for rnd in reversed(topic_rounds):
        if rnd.get("user_text"):
            last_user_text = rnd["user_text"]
            break
    out.append({
        "type": "last-prompt",
        "leafUuid": prev_uuid,
        "lastPrompt": last_user_text,
        "sessionId": new_sid,
    })

    return new_sid, out


def split_session(
    jsonl_path: Path,
    topics: list[str],
    model: str = "claude-haiku-4-5",
) -> dict:
    """Split a session JSONL into topic-focused sessions.

    Pipeline:
    1. Parse JSONL into entries
    2. Extract header (agent-setting, permission-mode) and rounds
    3. Mechanically prune rounds for classifier (strip tool output clutter)
    4. Classify each round via Vertex AI (Haiku)
    5. Assemble per-topic JSONL files
    6. Write to disk alongside the original

    Uses Vertex AI (env: ANTHROPIC_VERTEX_PROJECT_ID, VERTEX_REGION).
    Returns {topic: {session_id, path, rounds}, ..., suggestions: [...]}.
    """
    entries = []
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue

    original_sid = ""
    agent = ""
    header_entries = []
    for e in entries:
        if e.get("type") == "agent-setting":
            header_entries.append(e)
            original_sid = e.get("sessionId", original_sid)
            agent = e.get("agentSetting", agent)
        elif e.get("type") == "permission-mode":
            header_entries.append(e)
    if not original_sid:
        original_sid = jsonl_path.stem

    # Extract rounds from the full entries
    rounds = extract_rounds(entries)
    if not rounds:
        return {"error": "No conversation rounds found."}

    # Prune rounds for classifier (so it sees clean content, not tool noise)
    pruned = mechanical_prune(entries)
    pruned_rounds = extract_rounds(pruned)

    # Classify using pruned summaries but map back to original rounds
    result = classify_rounds(pruned_rounds, topics, model)
    classifications = result.get("classifications", {})
    suggestions = result.get("suggestions", [])

    # Group rounds by topic
    topic_groups: dict[str, list[dict]] = {t: [] for t in topics}
    topic_groups["other"] = []
    for idx_str, topic in classifications.items():
        idx = int(idx_str)
        if idx < len(rounds):
            bucket = topic if topic in topic_groups else "other"
            topic_groups[bucket].append(rounds[idx])

    # Build and write split sessions
    output_dir = jsonl_path.parent
    results: dict = {}
    all_topics = [t for t in topic_groups if topic_groups[t]]

    for topic, topic_rounds in topic_groups.items():
        if not topic_rounds:
            continue

        new_sid, split_entries = build_split_session(
            original_sid, header_entries, topic, topic_rounds, all_topics, agent,
        )

        # Write as NEW_SID.jsonl — claude --resume requires filename == sessionId
        out_path = output_dir / f"{new_sid}.jsonl"
        with open(out_path, "w") as f:
            for entry in split_entries:
                f.write(json.dumps(entry) + "\n")

        # Human-readable symlink: ORIGINAL.split.TOPIC.jsonl -> NEW_SID.jsonl
        safe_topic = topic.replace(" ", "-").replace("/", "-").lower()
        link_path = output_dir / f"{original_sid}.split.{safe_topic}.jsonl"
        if link_path.exists() or link_path.is_symlink():
            link_path.unlink()
        link_path.symlink_to(out_path.name)

        results[topic] = {
            "session_id": new_sid,
            "path": str(out_path),
            "rounds": len(topic_rounds),
        }

    if suggestions:
        results["_suggestions"] = suggestions

    return results


# ── session linting ─────────────────────────────────────────────────

# Known entry types per session-format.md spec
KNOWN_ENTRY_TYPES = {
    "agent-setting", "permission-mode", "user", "assistant",
    "system", "attachment", "file-history-snapshot",
    "queue-operation", "last-prompt", "custom-title",
}

# UUID-bearing entry types (participate in the conversation tree)
UUID_ENTRY_TYPES = {"user", "assistant", "system", "attachment"}

# Required fields per entry type (from empirical spec)
REQUIRED_FIELDS: dict[str, list[str]] = {
    "agent-setting": ["type", "sessionId", "agentSetting"],
    "permission-mode": ["type", "sessionId", "permissionMode"],
    "user": ["type", "uuid", "sessionId", "message", "timestamp", "isSidechain",
             "cwd", "entrypoint", "gitBranch", "version", "userType", "promptId"],
    "assistant": ["type", "uuid", "parentUuid", "sessionId", "message",
                  "timestamp", "isSidechain", "cwd", "entrypoint", "gitBranch",
                  "version", "userType"],
    "system": ["type", "subtype", "uuid", "sessionId", "timestamp",
               "isSidechain", "cwd", "entrypoint", "gitBranch", "version", "userType"],
    "attachment": ["type", "uuid", "sessionId", "attachment", "timestamp",
                   "isSidechain", "cwd", "entrypoint", "gitBranch", "version", "userType"],
    "file-history-snapshot": ["type", "messageId", "isSnapshotUpdate", "snapshot"],
    "queue-operation": ["type", "sessionId", "operation", "timestamp"],
    "last-prompt": ["type", "sessionId"],
}

# Minimal required fields — the subset that causes functional breakage if missing
CRITICAL_FIELDS: dict[str, list[str]] = {
    "agent-setting": ["sessionId", "agentSetting"],
    "permission-mode": ["sessionId", "permissionMode"],
    "user": ["uuid", "message", "sessionId"],
    "assistant": ["uuid", "parentUuid", "message", "sessionId"],
    "system": ["uuid", "sessionId", "subtype"],
    "attachment": ["uuid", "sessionId", "attachment"],
    "last-prompt": ["sessionId"],
}

KNOWN_SYSTEM_SUBTYPES = {
    "turn_duration", "scheduled_task_fire", "away_summary",
    "local_command", "api_error",
}

KNOWN_QUEUE_OPERATIONS = {"enqueue", "dequeue", "remove", "popAll"}

KNOWN_CONTENT_BLOCK_TYPES = {"text", "thinking", "tool_use", "tool_result", "image"}

KNOWN_STOP_REASONS = {"end_turn", "tool_use", "stop_sequence", None}


def lint_session(jsonl_path: Path) -> list[dict]:
    """Validate a session JSONL file against the session-format.md spec.

    Seven check categories:
      1. json       — corrupt JSON lines (with line number and context)
      2. type       — unknown or missing entry type
      3. field      — missing required fields per entry type
      4. parent-chain — parentUuid references a uuid not yet seen
      5. tool-orphan  — tool_result references unknown tool_use_id
      6. resume     — resumability: agent-setting first, last-prompt present,
                      valid leaf UUID, lastPrompt text present
      7. structure  — message content integrity, unknown subtypes, role mismatches

    Returns a list of {severity, line, check, message} dicts.
    Severities: "error" (breaks functionality), "warn" (suspicious),
    "info" (noteworthy but harmless).
    Empty list means the session is clean.
    """
    issues: list[dict] = []

    def _issue(severity: str, line: int, check: str, message: str):
        issues.append({"severity": severity, "line": line, "check": check, "message": message})

    # ── pass 1: parse lines ──────────────────────────────────────────

    entries: list[tuple[int, dict]] = []   # (line_number, parsed)
    total_lines = 0
    empty_lines = 0

    try:
        with open(jsonl_path) as f:
            for i, raw_line in enumerate(f, 1):
                total_lines = i
                raw = raw_line.rstrip("\n")
                if not raw:
                    empty_lines += 1
                    continue
                try:
                    entries.append((i, json.loads(raw)))
                except json.JSONDecodeError as e:
                    # Extract context for the corrupt line
                    preview = raw[:120] + "…" if len(raw) > 120 else raw
                    _issue("error", i, "json",
                           f"Corrupt JSON ({len(raw)} chars): {e} | preview: {preview}")
    except OSError as e:
        _issue("error", 0, "io", f"Cannot read file: {e}")
        return issues

    if not entries:
        _issue("error", 0, "empty", "File has no valid entries")
        return issues

    # ── pass 2: structural checks ────────────────────────────────────

    # Collect ALL uuids first (two-pass) — entries sometimes reference
    # UUIDs that appear later in the file (e.g., system(scheduled_task_fire)
    # can reference a forward assistant UUID).
    all_uuids: set[str] = set()
    for _, entry in entries:
        uid = entry.get("uuid")
        if uid:
            all_uuids.add(uid)

    tool_use_ids: set[str] = set()
    tool_use_lines: dict[str, int] = {}  # tool_use_id -> line of the tool_use
    tool_result_ids: set[str] = set()
    session_ids: set[str] = set()
    agent_names: set[str] = set()
    last_prompt_entries: list[tuple[int, dict]] = []
    type_counts: dict[str, int] = {}
    user_msg_count = 0
    assistant_msg_count = 0
    seen_uuids: set[str] = set()  # track duplicates

    for lineno, entry in entries:
        etype = entry.get("type")

        # Check type field exists and is known
        if etype is None:
            _issue("error", lineno, "type", "Entry missing 'type' field")
            continue

        if etype not in KNOWN_ENTRY_TYPES:
            # Allow custom types from compact (e.g., "compact-summary", "summary")
            if etype not in ("compact-summary", "summary"):
                _issue("warn", lineno, "type", f"Unknown entry type: '{etype}'")

        type_counts[etype] = type_counts.get(etype, 0) + 1

        # Check critical required fields (error severity)
        if etype in CRITICAL_FIELDS:
            for field in CRITICAL_FIELDS[etype]:
                if field not in entry:
                    _issue("error", lineno, "field",
                           f"{etype} missing critical field '{field}'")

        # Check full required fields (warn severity for non-critical)
        if etype in REQUIRED_FIELDS:
            for field in REQUIRED_FIELDS[etype]:
                if field not in entry and field not in CRITICAL_FIELDS.get(etype, []):
                    _issue("warn", lineno, "field",
                           f"{etype} missing field '{field}'")

        # Track session IDs
        sid = entry.get("sessionId", "")
        if sid:
            session_ids.add(sid)

        # Track agent names
        if etype == "agent-setting":
            agent_names.add(entry.get("agentSetting", ""))

        # ── UUID chain validation ────────────────────────────────────
        uid = entry.get("uuid")
        puid = entry.get("parentUuid")

        if etype in UUID_ENTRY_TYPES:
            if uid:
                if uid in seen_uuids:
                    _issue("warn", lineno, "parent-chain",
                           f"Duplicate uuid {uid[:12]}…")
                seen_uuids.add(uid)
            # parentUuid can be null for first user message and some attachments
            # Check against all_uuids (allows forward references)
            if puid and puid not in all_uuids:
                # Warn, not error — compacted files have broken chains from
                # pruned entries, and resume still works with orphaned parents.
                _issue("warn", lineno, "parent-chain",
                       f"{etype} parentUuid {puid[:12]}… not found anywhere in file")

        # ── message content validation ───────────────────────────────
        msg = entry.get("message", {})
        if isinstance(msg, dict) and etype in ("user", "assistant"):
            role = msg.get("role")
            content = msg.get("content")

            # Role must match entry type
            if etype == "user":
                user_msg_count += 1
                if role != "user":
                    _issue("warn", lineno, "structure",
                           f"user entry has role='{role}', expected 'user'")
            elif etype == "assistant":
                assistant_msg_count += 1
                if role != "assistant":
                    _issue("warn", lineno, "structure",
                           f"assistant entry has role='{role}', expected 'assistant'")

                # Check stop_reason
                stop = msg.get("stop_reason")
                if stop not in KNOWN_STOP_REASONS:
                    _issue("info", lineno, "structure",
                           f"Unknown stop_reason: '{stop}'")

            # Validate content blocks
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")

                    if btype and btype not in KNOWN_CONTENT_BLOCK_TYPES:
                        _issue("info", lineno, "structure",
                               f"Unknown content block type: '{btype}'")

                    # Track tool_use / tool_result pairs
                    if btype == "tool_use":
                        tid = block.get("id", "")
                        if tid:
                            tool_use_ids.add(tid)
                            tool_use_lines[tid] = lineno
                        if not block.get("name"):
                            _issue("warn", lineno, "structure",
                                   "tool_use block missing 'name'")
                        if "input" not in block:
                            _issue("warn", lineno, "structure",
                                   f"tool_use '{block.get('name', '?')}' missing 'input'")

                    elif btype == "tool_result":
                        tuid = block.get("tool_use_id", "")
                        if tuid:
                            tool_result_ids.add(tuid)
                            if tuid not in tool_use_ids:
                                _issue("warn", lineno, "tool-orphan",
                                       f"tool_result references unknown tool_use_id {tuid[:16]}…")
                        else:
                            _issue("warn", lineno, "tool-orphan",
                                   "tool_result block missing 'tool_use_id'")

                    elif btype == "thinking":
                        if "thinking" not in block:
                            _issue("warn", lineno, "structure",
                                   "thinking block missing 'thinking' field")

            elif isinstance(content, str):
                # User messages can have string content (prompts)
                if etype == "assistant":
                    _issue("warn", lineno, "structure",
                           "assistant message has string content (expected list of blocks)")

        # ── system subtype validation ────────────────────────────────
        if etype == "system":
            subtype = entry.get("subtype", "")
            if subtype and subtype not in KNOWN_SYSTEM_SUBTYPES:
                _issue("info", lineno, "structure",
                       f"Unknown system subtype: '{subtype}'")

        # ── queue-operation validation ───────────────────────────────
        if etype == "queue-operation":
            op = entry.get("operation", "")
            if op and op not in KNOWN_QUEUE_OPERATIONS:
                _issue("info", lineno, "structure",
                       f"Unknown queue operation: '{op}'")

        # ── last-prompt tracking ─────────────────────────────────────
        if etype == "last-prompt":
            last_prompt_entries.append((lineno, entry))

    # ── pass 3: cross-entry checks ──────────────────────────────────

    # Check for unmatched tool_use (tool called but no result delivered)
    unmatched_tools = tool_use_ids - tool_result_ids
    if len(unmatched_tools) > 10:
        _issue("info", 0, "tool-orphan",
               f"{len(unmatched_tools)} tool_use blocks with no matching tool_result "
               f"(may be normal for interrupted sessions)")
    elif unmatched_tools:
        for tid in list(unmatched_tools)[:5]:
            ln = tool_use_lines.get(tid, 0)
            _issue("info", ln, "tool-orphan",
                   f"tool_use {tid[:16]}… has no matching tool_result")

    # ── pass 4: resumability checks ─────────────────────────────────

    first_type = entries[0][1].get("type", "")
    if first_type != "agent-setting":
        # Sessions without --agent start with queue-operation (no agent-setting).
        # custom-title can also appear first. These are valid but not agent-resumable.
        has_agent_setting = any(e.get("type") == "agent-setting" for _, e in entries)
        if has_agent_setting:
            _issue("warn", entries[0][0], "resume",
                   f"First entry is '{first_type}', expected 'agent-setting' "
                   f"(agent-setting exists later — may affect resume)")
        else:
            _issue("info", entries[0][0], "resume",
                   f"No agent-setting entry — session has no agent identity "
                   f"(bare claude, not --agent)")

    if not last_prompt_entries:
        _issue("warn", 0, "resume",
               "No last-prompt entry — session is not resumable")
    else:
        last_lp_line, last_lp = last_prompt_entries[-1]

        # Check leafUuid validity
        leaf = last_lp.get("leafUuid")
        if leaf:
            if leaf not in all_uuids:
                _issue("error", last_lp_line, "resume",
                       f"last-prompt leafUuid {leaf[:12]}… not found in entry UUIDs — "
                       f"resume will fail")
        else:
            _issue("warn", last_lp_line, "resume",
                   "Final last-prompt has no leafUuid — resume may fail")

        # Check lastPrompt text
        if "lastPrompt" not in last_lp:
            _issue("info", last_lp_line, "resume",
                   "Final last-prompt has no lastPrompt text")

    # ── pass 5: summary stats ───────────────────────────────────────

    agent_list = ", ".join(sorted(agent_names - {""})) or "none"
    _issue("info", 0, "stats",
           f"{total_lines} lines, {len(entries)} entries, "
           f"{empty_lines} empty, {user_msg_count} user msgs, "
           f"{assistant_msg_count} assistant msgs, "
           f"{len(session_ids)} session IDs, "
           f"agents: {agent_list}")

    return issues


# ── CLI entrypoint ──────────────────────────────────────────────────

_UUID_RE = __import__("re").compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    __import__("re").I,
)

CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "projects"


def _resolve_jsonl(target: str) -> Path | None:
    """Resolve a session ID, file path, or tmux name to a JSONL path."""
    # Direct file path
    p = Path(target)
    if p.exists() and p.suffix == ".jsonl":
        return p

    # Session ID — search all project dirs
    if _UUID_RE.match(target):
        for jsonl in CLAUDE_SESSIONS_DIR.rglob(f"{target}.jsonl"):
            return jsonl

    # Try as tmux session name via session map
    try:
        from .session import SessionMap, find_session_jsonl
        return find_session_jsonl(target, SessionMap())
    except Exception:
        pass

    return None


def lint_cli():
    """CLI entrypoint: puppet-lint <session-id | path | tmux-name> [...]"""
    import sys

    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("Usage: puppet-lint <session-id | path.jsonl | tmux-name> [...]")
        print("       puppet-lint --batch <dir>  # lint all .jsonl in directory")
        print()
        print("Validates Claude Code session JSONL files against the spec.")
        print("See docs/session-format.md for the format specification.")
        sys.exit(0)

    # Batch mode
    if args[0] == "--batch":
        directory = Path(args[1]) if len(args) > 1 else CLAUDE_SESSIONS_DIR
        if not directory.is_dir():
            print(f"Error: {directory} is not a directory", file=sys.stderr)
            sys.exit(1)
        total = 0
        broken = 0
        for jsonl in sorted(directory.glob("*.jsonl")):
            total += 1
            issues = lint_session(jsonl)
            errors = [i for i in issues if i["severity"] == "error"]
            if errors:
                broken += 1
                print(f"FAIL {jsonl.name}: {len(errors)} errors")
                for e in errors:
                    ln = f"L{e['line']}" if e["line"] else "   "
                    print(f"  {ln:>6s}  [{e['check']}] {e['message'][:100]}")
        print(f"\n{total} files, {total - broken} pass, {broken} fail")
        sys.exit(1 if broken else 0)

    # Normal mode: lint each argument
    exit_code = 0
    for target in args:
        jsonl_path = _resolve_jsonl(target)
        if jsonl_path is None:
            print(f"Error: cannot resolve '{target}' to a session JSONL file", file=sys.stderr)
            exit_code = 1
            continue

        issues = lint_session(jsonl_path)
        errors = [i for i in issues if i["severity"] == "error"]
        warns = [i for i in issues if i["severity"] == "warn"]
        infos = [i for i in issues if i["severity"] == "info"]

        if not errors and not warns:
            # Clean — show stats only
            stats = [i for i in issues if i["check"] == "stats"]
            stat_msg = stats[0]["message"] if stats else ""
            print(f"PASS {jsonl_path.name}  ({stat_msg})")
        else:
            print(f"\n{'FAIL' if errors else 'WARN'} {jsonl_path.name}")
            for i in issues:
                sev = i["severity"].upper()
                ln = f"L{i['line']}" if i["line"] else "   "
                print(f"  {sev:5s} {ln:>6s}  [{i['check']}] {i['message'][:120]}")
            print(f"\n  {len(errors)} errors, {len(warns)} warnings, {len(infos)} info")
            if errors:
                exit_code = 1

    sys.exit(exit_code)
