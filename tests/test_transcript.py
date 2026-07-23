# tests/test_transcript.py
"""Tests for board/transcript.py — turn extraction, noise filtering, command-session detection.

All hermetic: synthetic .jsonl session files in tmp dirs.
"""
import json
import os
from pathlib import Path

from board import transcript


def _rec(role: str, text: str) -> dict[str, object]:
    """A user/assistant transcript record carrying a single text block."""
    return {"type": role, "message": {"role": role, "content": [{"type": "text", "text": text}]}}


def _write(path: Path, records: list[dict[str, object]]) -> None:
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


def test_trampoline_husk_is_a_command_session(tmp_path: Path) -> None:
    """A session whose only user records are injected wrappers (a caveat blob + a
    /resume local command) has no real user text -> command session. Without this, a
    3-minute hop back to another session ranks as a project's best session."""
    husk = tmp_path / "husk.jsonl"
    _write(husk, [
        _rec("user", "<local-command-caveat>Caveat: The messages below were generated "
                     "by the user while running local commands.</local-command-caveat>"),
        _rec("user", "<command-name>/resume</command-name> <command-args>claude "
                     "--resume some-other-session</command-args>"),
    ])
    assert transcript.is_command_session(husk) is True


def test_pick_session_prefers_real_work_over_husk(tmp_path: Path) -> None:
    """A NEWER trampoline husk must not outrank an older genuine work session."""
    proj = tmp_path / "Claude" / "demo"
    sdir = tmp_path / "sessions" / str(proj).replace("/", "-")
    sdir.mkdir(parents=True)
    work, husk = sdir / "work.jsonl", sdir / "husk.jsonl"
    _write(work, [_rec("user", "let's fix the widget layout")])
    _write(husk, [
        _rec("user", "<local-command-caveat>Caveat: The messages below were generated "
                     "by the user while running local commands.</local-command-caveat>"),
        _rec("user", "<command-name>/resume</command-name>"),
    ])
    os.utime(work, (100, 100))   # older real work
    os.utime(husk, (200, 200))   # newer husk
    assert transcript.pick_session(proj, tmp_path / "sessions") == work


def test_slash_start_with_real_work_is_not_a_husk(tmp_path: Path) -> None:
    """Precision guard: a session that STARTS with a local command but continues with
    real typed text is work, not a husk — the noise-skip finds the real first prompt."""
    proj = tmp_path / "Claude" / "demo"
    sdir = tmp_path / "sessions" / str(proj).replace("/", "-")
    sdir.mkdir(parents=True)
    s = sdir / "s.jsonl"
    _write(s, [
        _rec("user", "<command-name>/model</command-name>"),
        _rec("user", "now help me refactor the parser"),
    ])
    assert transcript.is_command_session(s) is False
    assert transcript.pick_session(proj, tmp_path / "sessions") == s


def test_real_prompt_beyond_256kb_preamble_is_found(tmp_path: Path) -> None:
    """Root sessions open with hundreds of KB of injected context before the user's
    first prompt. A fixed 256KB head budget read right past it and false-flagged most
    REAL sessions as husks; the streaming read must find a prompt sitting beyond
    256KB of noise."""
    s = tmp_path / "s.jsonl"
    big_noise = _rec("user", "<system-reminder>" + ("x" * 300_000))
    real = _rec("user", "let's get to work on the parser")
    _write(s, [big_noise, real])
    assert transcript.is_command_session(s) is False


def test_all_husk_directory_falls_back_to_newest(tmp_path: Path) -> None:
    """When EVERY session in a project's dir is a husk/command session, pick_session
    still returns the newest overall (files[0]) rather than None — the empty->husk
    rule must not make a project session-less when husks are all it has."""
    proj = tmp_path / "Claude" / "demo"
    sdir = tmp_path / "sessions" / str(proj).replace("/", "-")
    sdir.mkdir(parents=True)
    h1, h2 = sdir / "h1.jsonl", sdir / "h2.jsonl"
    for h in (h1, h2):
        _write(h, [_rec("user", "<command-name>/resume</command-name>")])
    os.utime(h1, (100, 100))
    os.utime(h2, (200, 200))
    assert transcript.pick_session(proj, tmp_path / "sessions") == h2


def test_empty_first_text_block_does_not_hide_real_prompt(tmp_path: Path) -> None:
    """A user record whose content list leads with an EMPTY text block followed by a
    real one must read as real text (mirrors recent_turns' semantics) — breaking on
    the first block made such a session look like a husk."""
    s = tmp_path / "s.jsonl"
    rec = {"type": "user", "message": {"role": "user", "content": [
        {"type": "text", "text": ""},
        {"type": "text", "text": "let's build the parser"},
    ]}}
    s.write_text(json.dumps(rec))
    assert transcript.is_command_session(s) is False


def test_recent_turns_unreadable_file_degrades_to_empty(tmp_path: Path) -> None:
    """An unreadable 'transcript' must cost one card, not the whole scan: an unguarded
    OSError here previously aborted board.json for every project. A DIRECTORY named
    like a session file makes open() raise IsADirectoryError (an OSError) hermetically —
    no permission tricks needed."""
    bogus = tmp_path / "sess.jsonl"
    bogus.mkdir()
    assert transcript.recent_turns(bogus) == []
