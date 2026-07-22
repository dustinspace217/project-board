# tests/test_build.py
"""Tests for board/build.py — helpers and orchestrator.

Three pure-function helpers are tested in isolation first (Steps 1–4), then
build_card() is exercised against a real tmp directory structure (Step 5).
All tests use fixed dates so results are deterministic.

All imports are at the top so ruff (E402) stays clean. The module-level
constant T and T_epoch() helper are defined before the test functions that
use them, but after the imports — that order is required by ruff's E402 rule.
"""
import datetime as dt
import json
import os
import time
from pathlib import Path

import pytest

from board import build, llm_classify
from board.build import (
    _session_cwd,
    _status_age,
    apply_dropoff,
    build_card,
    compute_finished_at,
    is_stale,
)
from board.statusblock import StatusBlock

# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

# T is the anchor "today" used throughout the helper tests.
T = dt.date(2026, 6, 17)


def T_epoch() -> float:
    """Return the Unix timestamp for the anchor date T (midnight, local time).

    Used by test_stale_after_threshold to build an epoch value that is a
    known number of seconds before T. time.mktime converts a local-time struct
    to a Unix timestamp — matches is_stale()'s internal conversion, so the
    comparison is apples-to-apples.
    """
    return time.mktime(dt.datetime(2026, 6, 17).timetuple())


# ---------------------------------------------------------------------------
# compute_finished_at
# ---------------------------------------------------------------------------


def test_carry_finished_at_set_on_first_finish() -> None:
    """First time a project hits 'finished', finished_at is set to today's ISO date."""
    assert compute_finished_at("finished", prev_finished_at=None, today=T) == T.isoformat()


def test_carry_finished_at_preserved() -> None:
    """Once finished_at is set, subsequent scans carry it forward unchanged."""
    assert compute_finished_at("finished", "2026-06-10", today=T) == "2026-06-10"


def test_finished_at_cleared_on_reopen() -> None:
    """If a project moves out of 'finished', finished_at is cleared to None."""
    assert compute_finished_at("writing", "2026-06-10", today=T) is None


# ---------------------------------------------------------------------------
# apply_dropoff
# ---------------------------------------------------------------------------


def test_dropoff_excludes_old_finished() -> None:
    """A card finished more than dropoff_days ago is dropped (True); recent ones are kept."""
    # 2026-06-01 is 16 days before 2026-06-17 → exceeds dropoff_days=5 → drop.
    assert apply_dropoff("finished", "2026-06-01", today=T, dropoff_days=5) is True
    # 2026-06-15 is 2 days before 2026-06-17 → within dropoff_days=5 → keep.
    assert apply_dropoff("finished", "2026-06-15", today=T, dropoff_days=5) is False


# ---------------------------------------------------------------------------
# is_stale
# ---------------------------------------------------------------------------


def test_stale_after_threshold() -> None:
    """A project not touched in more than stale_days×86400 seconds is stale."""
    fourteen_days = 14 * 86400
    # Subtract 14 days + 1 second from T's epoch → just past the threshold.
    assert is_stale(
        last_touched_epoch=T_epoch() - fourteen_days - 1,
        today=T,
        stale_days=14,
    ) is True


# ---------------------------------------------------------------------------
# build_card() — integration over a tmp directory tree
# ---------------------------------------------------------------------------


def test_build_card_finished_project(tmp_path: Path) -> None:
    """build_card returns a card with bucket='finished' and owner='none' for a
    project whose plan doc Status block says Complete."""
    proj = tmp_path / "demo"
    (proj / "docs" / "superpowers" / "plans").mkdir(parents=True)
    (proj / "CLAUDE.md").write_text("x")
    (proj / "docs" / "superpowers" / "plans" / "p.md").write_text(
        "## Status (updated 2026-06-16)\n"
        "Phase: Complete — shipped\n"
        "Done: it shipped\n"
        "Next: nothing\n"
        "Blocked: nothing\n"
    )
    # sessions_root doesn't need to exist — latest_session() degrades gracefully.
    card = build_card(
        proj,
        tmp_path / "sessions",
        {},  # index (no attribution in unit tests)
        {},  # prev
        dt.date(2026, 6, 17),
        dropoff_days=5,
        stale_days=14,
        allow_llm=False,  # hermetic: exercise the deterministic heuristic, never call Ollama
    )
    # Card must not be None — a 1-day-old finished project is within the 5-day window.
    assert card is not None
    assert card["bucket"] == "finished"
    assert card["owner"] == "none"
    assert card["needs_status"] is False


def test_build_card_no_status(tmp_path: Path) -> None:
    """A project with a CLAUDE.md but no Status block gets needs_status=True
    and owner='claude' (my action: add the Status block)."""
    proj = tmp_path / "no_status"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("# no status block here\n")
    card = build_card(
        proj,
        tmp_path / "sessions",
        {},  # index (no attribution in unit tests)
        {},  # prev
        dt.date(2026, 6, 17),
        dropoff_days=5,
        stale_days=14,
        allow_llm=False,  # hermetic: exercise the deterministic heuristic, never call Ollama
    )
    assert card is not None
    assert card["needs_status"] is True
    assert card["owner"] == "claude"


def test_build_card_dropped_after_dropoff(tmp_path: Path) -> None:
    """Once a finished project exceeds the drop-off window it is FLAGGED dropped (hidden by
    default, shown via the toggle) — not removed from the board's knowledge."""
    proj = tmp_path / "old_finished"
    (proj / "docs" / "superpowers" / "plans").mkdir(parents=True)
    (proj / "CLAUDE.md").write_text("x")
    (proj / "docs" / "superpowers" / "plans" / "p.md").write_text(
        "## Status (updated 2026-06-01)\n"
        "Phase: Complete — shipped\n"
        "Done: shipped long ago\n"
        "Next: nothing\n"
        "Blocked: nothing\n"
    )
    # prev_finished_at is set 20 days before today — well outside dropoff_days=5.
    prev: dict[str, object] = {"finished_at": "2026-05-28"}
    card = build_card(
        proj,
        tmp_path / "sessions",
        {},  # index (no attribution in unit tests)
        prev,
        dt.date(2026, 6, 17),
        dropoff_days=5,
        stale_days=14,
        allow_llm=False,  # hermetic: exercise the deterministic heuristic, never call Ollama
    )
    assert card is not None
    assert card["dropped"] is True
    assert card["bucket"] == "finished"


def test_build_card_pinned_finished_does_not_drop(tmp_path: Path) -> None:
    """A PINNED finished project never gets flagged dropped, even past the window."""
    proj = tmp_path / "pinned_done"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("x")
    (proj / ".board-status").write_text("bucket: finished\n")
    prev: dict[str, object] = {"finished_at": "2026-05-28"}  # 20 days before today
    card = build_card(proj, tmp_path / "sessions", {}, prev,
                      dt.date(2026, 6, 17), 5, 14, allow_llm=False)
    assert card is not None
    assert card["dropped"] is False
    assert card["classified_by"] == "pinned"


def test_build_file_card_planning_default(tmp_path: Path) -> None:
    """A loose root .md file becomes a lightweight 'planning/you' card with allow_llm=False."""
    f = tmp_path / "project-alpha-plan.md"
    f.write_text("# project-alpha plan\n- step 1\n")
    card = build.build_file_card(f, dt.date(2026, 6, 17), 14, allow_llm=False)
    assert card["name"] == "project-alpha-plan"
    # File-projects have no session pile — they get the open-here fallback at the
    # projects root (the file's home), never a bare None (unclickable card).
    assert card["resume_cmd"] == f"cd '{tmp_path}' && claude"
    assert card["resume_session_id"] is None
    assert card["bucket"] == "planning"
    assert card["owner"] == "you"
    assert card["classified_by"] == "file"
    assert card["is_file"] is True
    assert card["dropped"] is False


def test_build_file_card_llm_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With allow_llm, the file's content is classified by the LLM (classified_by='llm')."""
    f = tmp_path / "some-plan.md"
    f.write_text("# a finished thing\nall shipped\n")

    def fake(_turns: object, status_block: object = None, **_k: object) -> dict[str, object]:
        assert status_block  # build_file_card passes the file content as the status block
        return {"bucket": "finished", "owner": "none", "next": "nothing", "blocked": "nothing"}

    monkeypatch.setattr(llm_classify, "classify", fake)
    card = build.build_file_card(f, dt.date(2026, 6, 17), 14, allow_llm=True)
    assert card["bucket"] == "finished"
    assert card["classified_by"] == "llm"
    assert card["is_file"] is True


def test_build_card_carry_forward_finished_at(tmp_path: Path) -> None:
    """finished_at from the previous scan is preserved in the new card."""
    proj = tmp_path / "carry"
    (proj / "docs" / "superpowers" / "plans").mkdir(parents=True)
    (proj / "CLAUDE.md").write_text("x")
    (proj / "docs" / "superpowers" / "plans" / "p.md").write_text(
        "## Status (updated 2026-06-16)\n"
        "Phase: Complete — shipped\n"
        "Done: done\n"
        "Next: nothing\n"
        "Blocked: nothing\n"
    )
    prev: dict[str, object] = {"finished_at": "2026-06-14"}  # 3 days ago — within 5-day window
    card = build_card(
        proj,
        tmp_path / "sessions",
        {},  # index (no attribution in unit tests)
        prev,
        dt.date(2026, 6, 17),
        dropoff_days=5,
        stale_days=14,
        allow_llm=False,  # hermetic: exercise the deterministic heuristic, never call Ollama
    )
    assert card is not None
    assert card["finished_at"] == "2026-06-14"  # carried forward, not reset to today


# ---------------------------------------------------------------------------
# Fix 4: last_touched_human must use today, not time.time()
# ---------------------------------------------------------------------------


def test_last_touched_human_consistent_with_today(tmp_path: Path) -> None:
    """build_card's last_touched_human must be derived from the fixed 'today'
    argument, not from time.time(), so the value is deterministic under a
    fixed clock and consistent with is_stale/finished_at behaviour.

    We set the project dir's mtime to exactly 3 days before T's epoch, then
    assert the card returns '3 days ago' — not some wall-clock-relative value.
    """
    proj = tmp_path / "clock_test"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("x")

    # Compute T's epoch (midnight local) so we can set mtime relative to it.
    now_epoch = time.mktime(T.timetuple())
    three_days_ago = now_epoch - 3 * 86400
    # Set the directory mtime to exactly 3 days before T.
    os.utime(proj, (three_days_ago, three_days_ago))

    card = build_card(
        proj,
        tmp_path / "sessions",
        {},  # index (no attribution in unit tests)
        {},  # prev
        T,  # fixed today
        dropoff_days=5,
        stale_days=14,
        allow_llm=False,  # hermetic: exercise the deterministic heuristic, never call Ollama
    )
    assert card is not None
    assert card["last_touched_human"] == "3 days ago"


# ---------------------------------------------------------------------------
# Fix 5: _status_age must clamp at 0 for future-dated status
# ---------------------------------------------------------------------------


def _make_sb(updated: dt.date) -> StatusBlock:
    """Return a minimal StatusBlock with the given updated date."""
    return StatusBlock(
        phase="Phase 1",
        done="",
        next="next step",
        blocked="nothing",
        updated=updated,
        raw="",
    )


def test_status_age_future_date_clamps_to_zero() -> None:
    """A Status block dated in the future (ahead of today) must return 0, not negative.

    A negative age would be misleading: it would read as "status is very fresh"
    when the author made a future-dated typo. Clamping at 0 is safe and honest.
    """
    future = T + dt.timedelta(days=5)
    assert _status_age(_make_sb(future), T) == 0


# ---------------------------------------------------------------------------
# Fix 9: drop-off boundary (age==5 kept, age==6 dropped)
# ---------------------------------------------------------------------------


def test_dropoff_boundary_kept() -> None:
    """Age == dropoff_days exactly must be KEPT (apply_dropoff uses strict >)."""
    # T is 2026-06-17; finished 5 days ago = 2026-06-12; age == dropoff_days.
    assert apply_dropoff("finished", "2026-06-12", today=T, dropoff_days=5) is False


def test_dropoff_boundary_dropped() -> None:
    """Age == dropoff_days + 1 must be DROPPED."""
    # T is 2026-06-17; finished 6 days ago = 2026-06-11; age > dropoff_days.
    assert apply_dropoff("finished", "2026-06-11", today=T, dropoff_days=5) is True


# ---------------------------------------------------------------------------
# Fix 9: staleness boundary (13.9 days → not stale)
# ---------------------------------------------------------------------------


def test_stale_just_inside_threshold() -> None:
    """A project touched 13.9 days ago must NOT be stale (threshold is exclusive)."""
    # 13.9 days = 13 days * 86400 + 0.9 * 86400 seconds before T's epoch.
    just_inside = T_epoch() - (13 * 86400 + int(0.9 * 86400))
    assert is_stale(just_inside, T, stale_days=14) is False


# ---------------------------------------------------------------------------
# build_card classification branches: llm / gated / carried / stale.
# The LLM is monkeypatched so these stay hermetic (no Ollama). This is the v2
# heart that the original suite never walked (it only hit the heuristic arm).
# ---------------------------------------------------------------------------


def _with_session(sessions_root: Path, project: Path) -> None:
    """Create a one-text-turn session in the project's OWN session dir (fresh mtime)."""
    sdir = sessions_root / str(project).replace("/", "-")
    sdir.mkdir(parents=True, exist_ok=True)
    rec = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}}
    (sdir / "s.jsonl").write_text(json.dumps(rec))


def test_build_card_llm_branch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """allow_llm + a session -> the LLM result populates the card (classified_by='llm')."""
    proj = tmp_path / "demo"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("x")
    sroot = tmp_path / "sessions"
    _with_session(sroot, proj)

    def fake(*_a: object, **_k: object) -> dict[str, object]:
        return {"bucket": "testing", "owner": "you", "next": "confirm it", "blocked": "nothing"}

    monkeypatch.setattr(llm_classify, "classify", fake)
    card = build_card(proj, sroot, {}, {}, dt.date(2026, 6, 17), 5, 14, allow_llm=True)
    assert card is not None
    assert card["classified_by"] == "llm"
    assert card["bucket"] == "testing"
    assert card["owner"] == "you"
    assert card["next"] == "confirm it"


def test_build_card_gated_branch(tmp_path: Path) -> None:
    """allow_llm=False with new activity + a prior card -> 'gated', prior fields carried."""
    proj = tmp_path / "demo"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("x")
    sroot = tmp_path / "sessions"
    _with_session(sroot, proj)
    prev = {"bucket": "writing", "owner": "claude", "next": "old step", "blocked": "nothing",
            "classified_by": "llm", "classified_at_mtime": 0.0}
    card = build_card(proj, sroot, {}, prev, dt.date(2026, 6, 17), 5, 14, allow_llm=False)
    assert card is not None
    assert card["classified_by"] == "gated"
    assert card["bucket"] == "writing"
    assert card["next"] == "old step"


def test_build_card_carried_when_unchanged(tmp_path: Path) -> None:
    """A prior LLM card with no new session activity -> 'carried' (re-classify skipped)."""
    proj = tmp_path / "demo"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("x")
    # No session -> sess_mtime 0.0 == prev_mtime -> unchanged; allow_llm True but skipped.
    prev = {"bucket": "finished", "owner": "none", "next": "nothing", "blocked": "nothing",
            "classified_by": "llm", "classified_at_mtime": 0.0}
    card = build_card(proj, tmp_path / "sessions", {}, prev,
                      dt.date(2026, 6, 17), 5, 14, allow_llm=True)
    assert card is not None
    assert card["classified_by"] == "carried"
    assert card["bucket"] == "finished"


def test_build_card_stale_when_llm_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """allow_llm + new activity but the LLM returns None -> 'stale' (outage stays visible)."""
    proj = tmp_path / "demo"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("x")
    sroot = tmp_path / "sessions"
    _with_session(sroot, proj)

    def fake_down(*_a: object, **_k: object) -> None:
        return None  # simulate Ollama being unreachable

    monkeypatch.setattr(llm_classify, "classify", fake_down)
    prev = {"bucket": "writing", "owner": "you", "next": "old", "blocked": "nothing",
            "classified_by": "llm", "classified_at_mtime": 0.0}
    card = build_card(proj, sroot, {}, prev, dt.date(2026, 6, 17), 5, 14, allow_llm=True)
    assert card is not None
    assert card["classified_by"] == "stale"
    assert card["bucket"] == "writing"


def test_no_signal_carries_without_stale_badge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NO transcript + NO Status block = nothing to classify from. That must carry the
    prior card as 'carried', NOT brand it 'stale' (stale means the LLM was tried and
    FAILED) — and classify() must not even be called: its empty-input None is what used
    to masquerade as a permanent outage (a third of the board wore false ⚠ badges)."""
    proj = tmp_path / "quiet"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("x")

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("classify() must not be called when there is no input signal")

    monkeypatch.setattr(llm_classify, "classify", boom)
    prev: dict[str, object] = {
        "bucket": "planning", "owner": "you", "next": "old next", "blocked": "nothing",
        "classified_by": "stale", "classified_at_mtime": 0.0,
    }
    card = build_card(proj, tmp_path / "sessions", {}, prev,
                      dt.date(2026, 6, 17), 5, 14, allow_llm=True)
    assert card is not None
    assert card["classified_by"] == "carried"
    assert card["bucket"] == "planning"
    assert card["next"] == "old next"


def test_status_only_classify_failure_is_still_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The no_signal boundary: a Status block ALONE is real signal, so a classify()
    failure there is a genuine outage and must still label 'stale'. prev is non-llm so
    the changed-only check can't short-circuit into 'carried'."""
    proj = tmp_path / "demo"
    (proj / "docs" / "superpowers" / "plans").mkdir(parents=True)
    (proj / "CLAUDE.md").write_text("x")
    (proj / "docs" / "superpowers" / "plans" / "p.md").write_text(
        "## Status (updated 2026-06-16)\n"
        "Phase: 1 of 2 (build)\nDone: scaffolding\nNext: wire it up\nBlocked: nothing\n"
    )

    def fake_down(*_a: object, **_k: object) -> None:
        return None  # simulate Ollama being unreachable

    monkeypatch.setattr(llm_classify, "classify", fake_down)
    prev: dict[str, object] = {
        "bucket": "writing", "owner": "you", "next": "old", "blocked": "nothing",
        "classified_by": "heuristic", "classified_at_mtime": 0.0,
    }
    card = build_card(proj, tmp_path / "sessions", {}, prev,
                      dt.date(2026, 6, 17), 5, 14, allow_llm=True)
    assert card["classified_by"] == "stale"


def test_no_signal_no_prev_falls_to_heuristic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No signal AND no prior card: nothing to carry, so the deterministic heuristic
    classifies — and needs_status flags that a Status block would help."""
    proj = tmp_path / "brandnew"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("x")

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("classify() must not be called when there is no input signal")

    monkeypatch.setattr(llm_classify, "classify", boom)
    card = build_card(proj, tmp_path / "sessions", {}, {},
                      dt.date(2026, 6, 17), 5, 14, allow_llm=True)
    assert card["classified_by"] == "heuristic"
    assert card["needs_status"] is True


def test_no_signal_while_gated_labels_gated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pins the label precedence during a GPU-busy scan: allow_llm=False wins, so a
    no-signal project reads 'gated' for that one scan (no ⚠ badge; it settles to
    'carried' on the next free scan). Deliberate — not worth a special case."""
    proj = tmp_path / "quiet"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("x")

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("classify() must not be called while gated")

    monkeypatch.setattr(llm_classify, "classify", boom)
    prev: dict[str, object] = {
        "bucket": "planning", "owner": "you", "next": "", "blocked": "",
        "classified_by": "carried", "classified_at_mtime": 0.0,
    }
    card = build_card(proj, tmp_path / "sessions", {}, prev,
                      dt.date(2026, 6, 17), 5, 14, allow_llm=False)
    assert card["classified_by"] == "gated"
    # resume_cmd is orthogonal to the label chain: a gated no-session card still
    # carries the open-here fallback (recomputed every scan, never carried).
    assert card["resume_cmd"] == f"cd '{proj}' && claude"


def test_resume_cmd_cds_to_own_session_dir(tmp_path: Path) -> None:
    """A session living in the project's OWN pile resumes from the project dir —
    `claude --resume` is cwd-scoped, so the copied command must cd there first."""
    proj = tmp_path / "Claude" / "demo"
    proj.mkdir(parents=True)
    (proj / "CLAUDE.md").write_text("x")
    sroot = tmp_path / "sessions"
    _with_session(sroot, proj)
    card = build_card(proj, sroot, {}, {}, dt.date(2026, 6, 17), 5, 14, allow_llm=False)
    assert card["resume_cmd"] == f"cd '{proj}' && claude --resume s"


def test_resume_cmd_cds_to_root_for_root_pile_session(tmp_path: Path) -> None:
    """A session attributed from the ROOT pile (where root-pile work happens) resumes
    from the projects root, not the project's dir."""
    claude_root = tmp_path / "Claude"
    proj = claude_root / "demo"
    proj.mkdir(parents=True)
    (proj / "CLAUDE.md").write_text("x")
    sroot = tmp_path / "sessions"
    pile = sroot / str(claude_root).replace("/", "-")
    pile.mkdir(parents=True)
    f = pile / "abc123.jsonl"
    f.write_text("{}")
    index = {str(f): {"primary": "demo", "mtime": f.stat().st_mtime, "count": 3}}
    card = build_card(proj, sroot, index, {}, dt.date(2026, 6, 17), 5, 14, allow_llm=False)
    assert card["resume_cmd"] == f"cd '{claude_root}' && claude --resume abc123"


def test_resume_cmd_decodes_hyphenated_project_name(tmp_path: Path) -> None:
    """The session-dir encoding is lossy for hyphens ('/'->'-'), so the cwd decode
    must check the real filesystem: -<root>-my-proj is <root>/my-proj when that dir
    exists — not a blind hyphen->slash replacement."""
    claude_root = tmp_path / "Claude"
    proj = claude_root / "my-proj"
    proj.mkdir(parents=True)
    (proj / "CLAUDE.md").write_text("x")
    sroot = tmp_path / "sessions"
    _with_session(sroot, proj)
    card = build_card(proj, sroot, {}, {}, dt.date(2026, 6, 17), 5, 14, allow_llm=False)
    assert card["resume_cmd"] == f"cd '{proj}' && claude --resume s"


def test_session_cwd_falls_back_to_root_for_nested_subdir(tmp_path: Path) -> None:
    """A session started in a NESTED subdir (root/demo/tools) encodes as -root-demo-tools;
    root/demo-tools is not a dir, so the decode takes the root fallback — the only route
    to _session_cwd's final return, and a real shape (nested-cwd sessions happen). Direct
    unit test: the helper, not the whole card."""
    root = tmp_path / "Claude"
    (root / "demo").mkdir(parents=True)
    enc = str(root).replace("/", "-")
    sess = tmp_path / "sessions" / f"{enc}-demo-tools" / "x.jsonl"
    assert _session_cwd(sess, root) == root


def test_resume_cmd_quotes_apostrophe_in_path(tmp_path: Path) -> None:
    """A path containing an apostrophe survives the shell quoting: each ' becomes the
    close-escape-reopen sequence '\\'' inside the single-quoted cd argument."""
    proj = tmp_path / "Claude" / "o'brien"
    proj.mkdir(parents=True)
    (proj / "CLAUDE.md").write_text("x")
    sroot = tmp_path / "sessions"
    _with_session(sroot, proj)
    card = build_card(proj, sroot, {}, {}, dt.date(2026, 6, 17), 5, 14, allow_llm=False)
    expected = "cd '" + str(proj).replace("'", "'\\''") + "' && claude --resume s"
    assert card["resume_cmd"] == expected


def test_tier2_session_marked_resume_shared(tmp_path: Path) -> None:
    """A card whose session came from tier-2 attribution (primary = a SIBLING project)
    carries resume_shared=True so the widget can label the borrowed session; a card
    using its OWN session (pick_session fallback here) stays resume_shared=False."""
    claude_root = tmp_path / "Claude"
    proj = claude_root / "demo"
    proj.mkdir(parents=True)
    (proj / "CLAUDE.md").write_text("x")
    pile = tmp_path / "sessions" / str(claude_root).replace("/", "-")
    pile.mkdir(parents=True)
    f = pile / "shared1.jsonl"
    f.write_text("{}")
    index = {str(f): {"primary": "sibling", "mtime": f.stat().st_mtime, "count": 60,
                      "mentions": {"sibling": 60, "demo": 40}}}
    card = build_card(proj, tmp_path / "sessions", index, {},
                      dt.date(2026, 6, 17), 5, 14, allow_llm=False)
    assert card["resume_session_id"] == "shared1"
    assert card["resume_shared"] is True

    own = claude_root / "solo"
    own.mkdir()
    (own / "CLAUDE.md").write_text("x")
    _with_session(tmp_path / "sessions", own)
    own_card = build_card(own, tmp_path / "sessions", {}, {},
                          dt.date(2026, 6, 17), 5, 14, allow_llm=False)
    assert own_card["resume_session_id"] == "s"
    assert own_card["resume_shared"] is False


def test_open_here_fallback_quotes_apostrophe(tmp_path: Path) -> None:
    """Both FALLBACK quoting sites (directory card + file card) survive an apostrophe.
    Each is a separate copy of the '\\'' escape, so each needs its own exercise — the
    resume-path apostrophe test does not cover these two."""
    proj = tmp_path / "Claude" / "o'brien"
    proj.mkdir(parents=True)
    (proj / "CLAUDE.md").write_text("x")
    card = build_card(proj, tmp_path / "sessions", {}, {},
                      dt.date(2026, 6, 17), 5, 14, allow_llm=False)
    assert card["resume_cmd"] == "cd '" + str(proj).replace("'", "'\\''") + "' && claude"

    fdir = tmp_path / "o'dir"
    fdir.mkdir()
    f = fdir / "some-plan.md"
    f.write_text("# plan")
    fcard = build.build_file_card(f, dt.date(2026, 6, 17), 14, allow_llm=False)
    assert fcard["resume_cmd"] == "cd '" + str(fdir).replace("'", "'\\''") + "' && claude"


def test_resume_cmd_open_here_fallback_without_session(tmp_path: Path) -> None:
    """No session anywhere -> the open-here fallback: cd to the PROJECT dir and start a
    NEW session (nothing to resume, but the card stays copy-useful). resume_session_id
    stays None so the widget can label it 'open here' instead of 'resume:'."""
    proj = tmp_path / "Claude" / "demo"
    proj.mkdir(parents=True)
    (proj / "CLAUDE.md").write_text("x")
    card = build_card(proj, tmp_path / "sessions", {}, {},
                      dt.date(2026, 6, 17), 5, 14, allow_llm=False)
    assert card["resume_cmd"] == f"cd '{proj}' && claude"
    assert card["resume_session_id"] is None
