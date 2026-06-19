# tests/test_signals.py — test suite for board.signals (Task 4).
#
# Signals are the raw data sources for the board: session mtimes, git commit
# times, and the combined last-touched timestamp. All three functions degrade
# gracefully (return None / 0.0 / empty values) when directories are missing
# or git is absent — tested here alongside the happy path.

import os
import subprocess
from pathlib import Path

from board.signals import encode_session_dir, git_last_commit, last_touched, latest_session


def test_encode_session_dir() -> None:
    """Slashes in the absolute path must become dashes — this is the convention
    Claude uses when naming the per-project session directory under
    ~/.claude/projects/ (e.g. /home/user/projects/project-alpha →
    -home-user-projects-project-alpha)."""
    assert encode_session_dir(Path("/home/user/projects/project-alpha")) == (
        "-home-user-projects-project-alpha"
    )


def test_latest_session_picks_newest(tmp_path: Path) -> None:
    """latest_session must return the stem of the NEWEST *.jsonl by mtime.

    We create two files and forcibly set the older one to mtime=1 so the
    ordering is unambiguous even on fast filesystems where wall-clock
    resolution might otherwise tie the two writes.
    """
    sd = tmp_path / "sessions"
    sd.mkdir()
    old = sd / "aaaa.jsonl"
    old.write_text("{}")
    new = sd / "bbbb.jsonl"
    new.write_text("{}")
    os.utime(old, (1, 1))  # force 'aaaa' to epoch+1 so 'bbbb' is definitively newer
    sid, _mtime = latest_session(sd)
    assert sid == "bbbb"


def test_latest_session_missing_dir(tmp_path: Path) -> None:
    """A session dir that doesn't exist yet (normal for new projects) must
    return (None, 0.0) without raising."""
    sid, mtime = latest_session(tmp_path / "nonexistent")
    assert sid is None
    assert mtime == 0.0


def test_latest_session_empty_dir(tmp_path: Path) -> None:
    """A session dir with no *.jsonl files (e.g. only metadata) returns
    (None, 0.0) — the project has sessions dir but no actual session logs."""
    sd = tmp_path / "sessions"
    sd.mkdir()
    sid, mtime = latest_session(sd)
    assert sid is None
    assert mtime == 0.0


def test_last_touched_prefers_most_recent(tmp_path: Path) -> None:
    """last_touched returns the maximum of all available timestamps.

    session_mtime=0.0 (no session), git_mtime=None (no git) — so the dir's
    own mtime is the only nonzero candidate. We assert the return value equals
    that mtime exactly, not just "> 0", to guard against accidentally returning
    a constant or the wrong candidate.
    """
    p = tmp_path / "proj"
    p.mkdir()
    (p / "f.txt").write_text("x")
    # The dir's mtime is set by the write above; capture it after the write.
    expected = p.stat().st_mtime
    result = last_touched(p, session_mtime=0.0, git_mtime=None)
    assert result == expected


def test_last_touched_uses_max(tmp_path: Path) -> None:
    """When all three candidates are present, the largest value wins."""
    p = tmp_path / "proj"
    p.mkdir()
    # session_mtime dominates
    result = last_touched(p, session_mtime=9_999_999_999.0, git_mtime=1.0)
    assert result == 9_999_999_999.0


def test_git_last_commit_no_repo(tmp_path: Path) -> None:
    """A directory with no git repo must degrade to (None, None) without raising."""
    subject, commit_time = git_last_commit(tmp_path)
    assert subject is None
    assert commit_time is None


def test_git_last_commit_happy_path(tmp_path: Path) -> None:
    """git_last_commit returns (subject_str, float_timestamp) for a real commit.

    This is the ONLY positive-path coverage for git_last_commit — without it,
    we'd only know the function handles the no-repo case. We create a real git
    repo with one commit, then verify subject and timestamp are populated.
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    # Initialise a minimal git repo with user identity so CI doesn't need global config.
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )

    # Create a file and commit it with a known subject line.
    (repo / "README.md").write_text("hello")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "initial commit"],
        check=True,
        capture_output=True,
    )

    subject, commit_time = git_last_commit(repo)

    assert subject == "initial commit", f"unexpected subject: {subject!r}"
    assert commit_time is not None, "commit_time should not be None for a real commit"
    assert commit_time > 0, "commit_time should be a positive Unix timestamp"
