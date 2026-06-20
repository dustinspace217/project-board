"""Extract the recent, human-readable turns of a project's latest SUBSTANTIVE Claude
Code session — the input the local model interprets into status/owner/next/blocked.

'Substantive' = NOT a tangential slash-command session (e.g. /security-review,
/code-review). Those spawn their own session whose content is a review, not the
project's actual work-state — which is why the spike read project-alpha as 'Claude is
reviewing code' when it's really 'done, awaiting the user's confirmation'. We skip them.
"""
from __future__ import annotations

import json
from pathlib import Path

# Bounded reads: a single autonomous session can grow to hundreds of MB (huge tool
# outputs). We only ever need the HEAD (first user message) or the TAIL (recent turns),
# so we read a capped slice rather than the whole file — Power-of-Ten rule 3 (bound
# memory). These caps comfortably cover the few lines/turns we actually use.
_HEAD_BYTES = 256 * 1024     # first user message is near the top
_TAIL_BYTES = 1024 * 1024    # last ~14 text turns live well within the final 1 MB


def _read_head(path: Path, max_bytes: int = _HEAD_BYTES) -> str:
    """Decode the first max_bytes of a file (UTF-8, lossy). Bounds memory on huge files."""
    with path.open("rb") as fh:
        return fh.read(max_bytes).decode("utf-8", errors="replace")


def _read_tail(path: Path, max_bytes: int = _TAIL_BYTES) -> str:
    """Decode the last max_bytes of a file (UTF-8, lossy). The first line may be a partial
    fragment (cut mid-line) — json.loads fails on it and the caller skips it, which is fine."""
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > max_bytes:
            fh.seek(size - max_bytes)
        return fh.read().decode("utf-8", errors="replace")


# First-user-message prefixes that mark a session as a one-off command, not project work.
# Matched case-insensitively against the START of the session's first user text.
_COMMAND_PREFIXES = (
    "review this change",
    "review the current diff",
    "complete a security review",
    "code review a pull request",
    "/security-review",
    "/code-review",
    "/review",
)


def session_files(project_path: Path, sessions_root: Path) -> list[Path]:
    """All session .jsonl files for a project, NEWEST FIRST. Empty list if none.

    The session dir name is the project's absolute path with '/'->'-' (the same
    encoding as signals.encode_session_dir)."""
    encoded = str(project_path).replace("/", "-")
    d = sessions_root / encoded
    if not d.is_dir():
        return []
    return sorted(d.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)


def _first_user_text(jsonl_path: Path, scan_lines: int = 80) -> str:
    """First human user message text in a session (for command-session detection).
    Reads only the head — the first user message is near the top."""
    for ln in _read_head(jsonl_path).splitlines()[:scan_lines]:
        try:
            o = json.loads(ln)
        except Exception:
            continue
        if o.get("type") != "user":
            continue
        c = (o.get("message") or {}).get("content")
        if isinstance(c, str):
            return c.strip()
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "text":
                    return str(b.get("text", "")).strip()
    return ""


def is_command_session(jsonl_path: Path) -> bool:
    """True if this session looks like a one-off slash-command (review etc.), not work."""
    first = _first_user_text(jsonl_path).lower()
    return any(first.startswith(p) for p in _COMMAND_PREFIXES)


def pick_session(project_path: Path, sessions_root: Path) -> Path | None:
    """The latest SUBSTANTIVE session for a project; None if it has no session at all.
    Prefers the newest non-command session; falls back to the newest overall if every
    session reads as a command session. Loop bounded by the session-file count."""
    files = session_files(project_path, sessions_root)
    if not files:
        return None
    for f in files:
        if not is_command_session(f):
            return f
    return files[0]  # all are command-sessions -> newest is the best we have


# Injected, non-user-authored user-record content that should NOT be read as conversation:
# compaction-continuation boilerplate, Stop-hook/checkpoint feedback, slash-command
# wrappers, and bare system-reminders. These pollute the model's read of project state.
_NOISE_MARKERS = (
    "this session is being continued from a previous conversation",
    "stop hook feedback",
    "memory save checkpoint",
    "caveat: the messages below were generated",
    "<system-reminder>",
    "<command-name>",
    "<local-command",
    "this is an automated background-task event",
)


def _is_noise(text: str) -> bool:
    """True if a user turn is injected boilerplate (hooks/compaction/reminders), not a
    real user message. Checks the head so a marker anywhere up front counts."""
    head = text.lower().lstrip()[:120]
    return any(m in head for m in _NOISE_MARKERS)


def recent_turns(jsonl_path: Path, max_turns: int = 14, tail: int = 2500) -> list[tuple[str, str]]:
    """Last `max_turns` human-readable (role, text) turns — user+assistant `text` blocks,
    skipping thinking / tool-call / tool-result noise AND injected user boilerplate
    (compaction summaries, hook feedback, system-reminders). Reads only the file tail."""
    turns: list[tuple[str, str]] = []
    for ln in _read_tail(jsonl_path).splitlines()[-tail:]:
        try:
            o = json.loads(ln)
        except Exception:
            continue
        # The record `type` is the role (user/assistant) — use it directly rather than
        # o["message"]["role"], which crashes when a record has type but message: null.
        role = o.get("type")
        if role not in ("user", "assistant"):
            continue
        msg = o.get("message")
        c = msg.get("content") if isinstance(msg, dict) else None
        texts: list[str] = []
        if isinstance(c, str):
            texts = [c]
        elif isinstance(c, list):
            texts = [b.get("text", "") for b in c
                     if isinstance(b, dict) and b.get("type") == "text"]
        t = " ".join(x.strip() for x in texts if x and x.strip())
        if not t:
            continue
        if role == "user" and _is_noise(t):   # drop injected boilerplate, keep real messages
            continue
        turns.append((role, t[:800]))
    return turns[-max_turns:]


def format_turns(turns: list[tuple[str, str]]) -> str:
    """Render turns for the LLM prompt (oldest to newest)."""
    return "\n".join(f"[{role}] {txt}" for role, txt in turns)
