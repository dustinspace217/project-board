# tests/test_attribution.py
"""Tests for board/attribution.py — session-to-project attribution + cached index.

All hermetic: synthetic transcript text and tmp session dirs, no real ~/.claude.
"""
from pathlib import Path

from board import attribution

ROOT = Path("/home/u/projects")          # a generic projects root (basename "projects")
SEG = attribution._segment_re(ROOT)      # matches "/projects/<name>"
VALID = {"project-alpha", "project-beta", "project-gamma"}


def test_primary_project_picks_most_mentioned() -> None:
    """The project with the most <root>/<name> mentions wins."""
    text = "/projects/project-alpha/x /projects/project-alpha/y /projects/project-beta/z"
    primary, n = attribution._primary_project(text, VALID, SEG)
    assert primary == "project-alpha"
    assert n == 2


def test_primary_project_folderless_top_returns_none() -> None:
    """A project with NO folder dominating must NOT leak into the next-highest real project.

    'ghost-project' isn't in VALID (no folder), so when it's the top mentioned segment we
    return None rather than attributing the session to project-beta.
    """
    text = ("/projects/ghost-project/a /projects/ghost-project/b "
            "/projects/ghost-project/c /projects/project-beta/z")
    primary, _ = attribution._primary_project(text, VALID, SEG)
    assert primary is None


def test_primary_project_no_mentions() -> None:
    """Text with no <root>/ paths attributes to nothing."""
    assert attribution._primary_project("no project paths here", VALID, SEG) == (None, 0)


def test_most_recent_session_picks_newest_for_primary() -> None:
    """Among sessions primarily about a project, the newest (by mtime) is chosen."""
    index: attribution.SessionIndex = {
        "/s/a.jsonl": {"primary": "project-gamma", "mtime": 100.0, "count": 5},
        "/s/b.jsonl": {"primary": "project-gamma", "mtime": 200.0, "count": 3},
        "/s/c.jsonl": {"primary": "project-alpha", "mtime": 300.0, "count": 9},
    }
    assert attribution.most_recent_session("project-gamma", index) == Path("/s/b.jsonl")
    assert attribution.most_recent_session("project-alpha", index) == Path("/s/c.jsonl")
    # A project no session is primarily about -> None (caller falls back to heuristic).
    assert attribution.most_recent_session("project-beta", index) is None


def test_build_index_attributes_and_is_incremental(tmp_path: Path) -> None:
    """build_index attributes a session, and reuses cached entries when mtime is unchanged."""
    root = tmp_path / "projects"                      # projects root; basename "projects"
    sroot = tmp_path / "sessions"
    d = sroot / str(root).replace("/", "-")           # the root-cwd session dir name
    d.mkdir(parents=True)
    f = d / "sess.jsonl"
    f.write_text(f"work on /{root.name}/project-gamma/x and /{root.name}/project-gamma/y")

    idx = attribution.build_index(sroot, {"project-gamma"}, root)
    assert idx[str(f)]["primary"] == "project-gamma"

    # Incremental: an unchanged file's cached attribution is reused (same object, not re-read).
    idx2 = attribution.build_index(sroot, {"project-gamma"}, root, prev_index=idx)
    assert idx2[str(f)] is idx[str(f)]
