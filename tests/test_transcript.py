# tests/test_transcript.py
"""Tests for board/transcript.py — turn extraction, noise filtering, command-session detection.

All hermetic: synthetic .jsonl session files in tmp dirs.
"""
import json
import os
from pathlib import Path

from board import transcript


def _rec(role: str, text: str) -> dict:
    """A user/assistant transcript record carrying a single text block."""
    return {"type": role, "message": {"role": role, "content": [{"type": "text", "text": text}]}}


def _write(path: Path, records: list) -> None:
    """Write a list of records as one-JSON-object-per-line (.jsonl)."""
    path.write_text("\n".join(json.dumps(r) for r in records))


def test_is_noise_detects_injected_boilerplate() -> None:
    """Compaction/hook/system-reminder content is recognised as noise, real messages aren't."""
    assert transcript._is_noise("This session is being continued from a previous conversation")
    assert transcript._is_noise("Stop hook feedback: MEMORY SAVE CHECKPOINT")
    assert transcript._is_noise("<system-reminder> background context")
    assert not transcript._is_noise("Please fix the parser bug")


def test_recent_turns_keeps_real_text_drops_noise_and_tools(tmp_path: Path) -> None:
    """recent_turns extracts user+assistant text, drops thinking/tool blocks and noise turns."""
    f = tmp_path / "s.jsonl"
    _write(f, [
        _rec("user", "real question"),
        {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "hidden reasoning"},
            {"type": "text", "text": "real answer"},
        ]}},
        _rec("user", "Stop hook feedback: checkpoint"),   # noise -> dropped
        # a user record whose only block is a tool_result (no text) -> contributes nothing
        {"type": "user", "message": {"role": "user",
                                     "content": [{"type": "tool_result", "content": "x"}]}},
    ])
    assert transcript.recent_turns(f) == [("user", "real question"), ("assistant", "real answer")]


def test_is_command_session_detects_review_spawn(tmp_path: Path) -> None:
    """A /security-review style session is flagged; a real work session is not."""
    review = tmp_path / "rev.jsonl"
    _write(review, [_rec("user", "Review this change for security vulnerabilities.")])
    assert transcript.is_command_session(review) is True

    work = tmp_path / "work.jsonl"
    _write(work, [_rec("user", "Let's build the parser module")])
    assert transcript.is_command_session(work) is False


def test_session_files_newest_first(tmp_path: Path) -> None:
    """session_files returns a project's own-dir sessions newest-first; empty when none."""
    proj = tmp_path / "Claude" / "demo"
    proj.mkdir(parents=True)
    sroot = tmp_path / "projects"
    sdir = sroot / str(proj).replace("/", "-")
    sdir.mkdir(parents=True)
    old, new = sdir / "old.jsonl", sdir / "new.jsonl"
    _write(old, [_rec("user", "first")])
    _write(new, [_rec("user", "second")])
    os.utime(old, (100, 100))
    os.utime(new, (200, 200))
    files = transcript.session_files(proj, sroot)
    assert [f.name for f in files] == ["new.jsonl", "old.jsonl"]
    assert transcript.session_files(tmp_path / "Claude" / "nope", sroot) == []


def test_pick_session_skips_newer_command_session(tmp_path: Path) -> None:
    """pick_session prefers an older real-work session over a NEWER /security-review spawn."""
    proj = tmp_path / "Claude" / "demo"
    proj.mkdir(parents=True)
    sroot = tmp_path / "projects"
    sdir = sroot / str(proj).replace("/", "-")
    sdir.mkdir(parents=True)
    work, review = sdir / "work.jsonl", sdir / "review.jsonl"
    _write(work, [_rec("user", "build the thing")])
    _write(review, [_rec("user", "Review this change for security vulnerabilities.")])
    os.utime(work, (100, 100))     # older real work
    os.utime(review, (200, 200))   # newer, but a command session
    assert transcript.pick_session(proj, sroot) == work
    assert transcript.pick_session(tmp_path / "Claude" / "none", sroot) is None
