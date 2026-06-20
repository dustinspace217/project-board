# tests/test_attribution.py
"""Tests for board/attribution.py — session-to-project attribution + cached index.

All hermetic: synthetic transcript text and tmp session dirs, no real ~/.claude.
The projects root is a synthetic tmp path (or a fixed "/home/your-user/Claude"
for the pure-string helpers) so nothing depends on a specific machine's home.
"""
import re
from pathlib import Path

from board import attribution

# A generic, machine-independent projects root used by the pure-helper tests.
# Its NAME ("Claude") is what the segment regex keys on; its full path is what
# the session-dir prefix is derived from.
PROJECTS_ROOT = Path("/home/your-user/Claude")

# A per-root segment regex, built the same way build_index() builds it, so the
# _primary_project tests below exercise the real pattern for PROJECTS_ROOT.
SEG_RE = attribution._segment_re(PROJECTS_ROOT)

VALID = {"project-alpha", "project-beta", "my-project"}


def test_segment_re_captures_segment_after_root_name() -> None:
    """The derived regex captures the path segment right after the root name."""
    assert SEG_RE.findall("/Claude/project-alpha/tools/x.py") == ["project-alpha"]


def test_dir_prefix_derived_from_root_path() -> None:
    """The session-dir prefix is the root path with '/' replaced by '-'."""
    assert attribution._dir_prefix(PROJECTS_ROOT) == "-home-your-user-Claude"


def test_primary_project_picks_most_mentioned() -> None:
    """The project with the most /<root-name>/<name> mentions wins."""
    text = "/Claude/project-alpha/x /Claude/project-alpha/y /Claude/project-beta/z"
    primary, n = attribution._primary_project(text, VALID, SEG_RE)
    assert primary == "project-alpha"
    assert n == 2


def test_primary_project_folderless_top_returns_none() -> None:
    """A folder-LESS project dominating must NOT leak into the next-highest folder.

    Here "single-file-plan" has no folder (not in VALID), so when it's the top
    segment we return None rather than attributing the session to project-beta.
    """
    text = ("/Claude/single-file-plan/a /Claude/single-file-plan/b "
            "/Claude/single-file-plan/c /Claude/project-beta/z")
    primary, _ = attribution._primary_project(text, VALID, SEG_RE)
    assert primary is None


def test_primary_project_no_mentions() -> None:
    """Text with no /<root-name> paths attributes to nothing."""
    assert attribution._primary_project("no project paths here", VALID, SEG_RE) == (None, 0)


def test_most_recent_session_picks_newest_for_primary() -> None:
    """Among sessions primarily about a project, the newest (by mtime) is chosen."""
    index: attribution.SessionIndex = {
        "/s/a.jsonl": {"primary": "my-project", "mtime": 100.0, "count": 5},
        "/s/b.jsonl": {"primary": "my-project", "mtime": 200.0, "count": 3},
        "/s/c.jsonl": {"primary": "project-alpha", "mtime": 300.0, "count": 9},
    }
    assert attribution.most_recent_session("my-project", index) == Path("/s/b.jsonl")
    assert attribution.most_recent_session("project-alpha", index) == Path("/s/c.jsonl")
    # A project no session is primarily about -> None (caller falls back to heuristic).
    assert attribution.most_recent_session("project-beta", index) is None


def test_build_index_attributes_and_is_incremental(tmp_path: Path) -> None:
    """build_index attributes a session, and reuses cached entries when mtime is unchanged.

    The projects root is a tmp path whose NAME is "Claude", so the in-transcript
    "/Claude/<project>" mentions match the derived segment regex, and the session
    dir is named from that root's full path (with '/' -> '-').
    """
    projects_root = tmp_path / "Claude"
    projects_root.mkdir()
    sroot = tmp_path / "projects"
    # Session-dir name = the root path with '/' replaced by '-' (Claude Code's encoding).
    d = sroot / str(projects_root).replace("/", "-")
    d.mkdir(parents=True)
    f = d / "sess.jsonl"
    f.write_text("work on /Claude/my-project/x and more /Claude/my-project/y")

    idx = attribution.build_index(sroot, projects_root, {"my-project"})
    assert idx[str(f)]["primary"] == "my-project"

    # Incremental: an unchanged file's cached attribution is reused (same object, not re-read).
    idx2 = attribution.build_index(sroot, projects_root, {"my-project"}, prev_index=idx)
    assert idx2[str(f)] is idx[str(f)]


def test_segment_re_escapes_special_chars_in_root_name() -> None:
    """A root name containing regex-special chars is escaped, not interpreted.

    Guards against a root like "my.project" being treated as "my<any-char>project".
    """
    root = Path("/home/your-user/my.project")
    seg = attribution._segment_re(root)
    # The literal "/my.project/alpha" matches; a "/myXproject/alpha" must NOT.
    assert seg.findall("/my.project/alpha/file") == ["alpha"]
    assert seg.findall("/myXproject/alpha/file") == []
    # Sanity: the pattern uses the escaped name.
    assert re.escape("my.project") in seg.pattern
