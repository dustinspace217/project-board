"""Filesystem/git signals for a project: encoded session dir, resume id,
last-touched timestamp, and days-since-last-commit.

All failures degrade to None / (None, 0.0) — never raise. A missing git repo
or session dir is a normal condition for new or early-stage projects (spec §6,
§11).

Where inputs come from:
    project_path  — an absolute Path to a ~/Claude/<project> directory,
                    supplied by enumerate.find_projects() (Task 5).
    session_dir   — the per-project folder under ~/.claude/projects/, whose
                    name is derived by encode_session_dir() below.
    sessions_root — ~/.claude/projects/ itself, passed in by build.py so this
                    module doesn't hard-code a home directory and stays testable.
"""
from __future__ import annotations

# subprocess: used only for the git call; every other operation is pathlib/os.
# We keep subprocess to one function so the I/O surface is obvious.
import subprocess
from pathlib import Path


def encode_session_dir(project_path: Path) -> str:
    """Map an absolute project path to its ~/.claude/projects/ subdirectory name.

    Claude encodes the working-directory path by replacing every '/' with '-',
    so /home/your-user/Claude/project-alpha becomes -home-your-user-Claude-project-alpha.
    Verified empirically against the real ~/.claude/projects/ layout.

    Receives:
        project_path — absolute Path to the project directory.
    Returns:
        The encoded string (starts with '-' because the leading '/' also converts).
    """
    return str(project_path).replace("/", "-")


def latest_session(session_dir: Path) -> tuple[str | None, float]:
    """Find the most-recently-modified *.jsonl in session_dir.

    Each *.jsonl represents one Claude session; the newest one is the session
    the user would resume to continue this project. The stem (filename without
    extension) is the session id passed to `claude --resume`.

    Receives:
        session_dir — full path to ~/.claude/projects/<encoded-project>/.
    Returns:
        (session_id, mtime) where session_id is the stem of the newest .jsonl,
        or (None, 0.0) if the directory is missing or contains no .jsonl files.

    Loop is bounded by the entry count of session_dir (finite filesystem entries).
    Degrades to (None, 0.0) rather than raising if the directory is absent —
    this is normal for projects that have never been opened in Claude Code.
    """
    # (None, 0.0) is the sentinel for "no session found". Using 0.0 for mtime
    # means last_touched() can safely take max() over all candidates.
    newest: tuple[str | None, float] = (None, 0.0)
    if not session_dir.is_dir():
        return newest
    for f in session_dir.glob("*.jsonl"):
        m = f.stat().st_mtime
        if m > newest[1]:
            newest = (f.stem, m)
    return newest


def git_last_commit(project_path: Path) -> tuple[str | None, float | None]:
    """Return (subject_line, unix_timestamp) of the HEAD commit, or (None, None).

    Uses --format=%ct%n%s to get commit time and subject in two lines.
    The 5-second timeout prevents a hanging git command from blocking the scan.

    Receives:
        project_path — absolute Path to the project directory.
    Returns:
        (subject, float_timestamp) on success, (None, None) if the directory is
        not a git repo or has no commits. Never raises.

    Why SubprocessError + OSError: subprocess.run() raises SubprocessError on
    CalledProcessError (non-zero exit, via check=True) and OSError if git is
    not found on PATH. Both are handled identically — degrade to (None, None).
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(project_path), "log", "-1", "--format=%ct%n%s"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    except (subprocess.SubprocessError, OSError):
        # Not a git repo, no commits, git not on PATH, or timeout — all normal.
        return (None, None)

    lines = result.stdout.splitlines()
    if len(lines) < 2:
        # Empty repo (no commits) produces empty output; guard against index error.
        return (None, None)

    # lines[0] = unix timestamp string, lines[1] = commit subject
    return (lines[1], float(lines[0]))


def last_touched(project_path: Path, session_mtime: float, git_mtime: float | None) -> float:
    """Return the most recent timestamp from all available sources.

    The three candidates are:
        1. session_mtime  — mtime of the newest Claude session log (0.0 if none).
        2. git_mtime      — unix time of the HEAD commit (None if not a git repo).
        3. project dir mtime — the OS mtime of the project directory itself,
                               which updates on any file add/delete under it.

    Taking the max gives the most conservative "last activity" estimate:
    any of the three signals can indicate recent activity even if the others
    are stale (e.g. a project with no git but active Claude sessions).

    Receives:
        project_path  — Path to the project directory (for stat().st_mtime).
        session_mtime — float mtime from latest_session(); use 0.0 if no session.
        git_mtime     — float unix time from git_last_commit()[1]; use None if no git.
    Returns:
        The maximum float timestamp across all available candidates. Always > 0
        because project_path.stat().st_mtime is always available for a real dir.
    """
    # Build candidate list dynamically so None values don't pollute the max() call.
    candidates = [session_mtime, project_path.stat().st_mtime]
    if git_mtime is not None:
        candidates.append(git_mtime)
    return max(candidates)
