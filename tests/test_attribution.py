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
    primary, n, mentions = attribution._primary_project(text, VALID, SEG_RE)
    assert primary == "project-alpha"
    assert n == 2
    # Secondary valid mentions are recorded with their counts (the tier-2 signal).
    assert mentions == {"project-alpha": 2, "project-beta": 1}


def test_primary_project_folderless_top_returns_none() -> None:
    """A folder-LESS project dominating must NOT leak into the next-highest folder.

    Here "single-file-plan" has no folder (not in VALID), so when it's the top
    segment we return None rather than attributing the session to project-beta.
    """
    text = ("/Claude/single-file-plan/a /Claude/single-file-plan/b "
            "/Claude/single-file-plan/c /Claude/project-beta/z")
    primary, _, mentions = attribution._primary_project(text, VALID, SEG_RE)
    assert primary is None
    # The folder-less name is excluded from mentions too — only VALID projects recorded.
    assert mentions == {"project-beta": 1}


def test_primary_project_no_mentions() -> None:
    """Text with no /<root-name> paths attributes to nothing."""
    assert attribution._primary_project("no project paths here", VALID, SEG_RE) == (None, 0, {})


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


def test_most_recent_session_aliases_credit_parent() -> None:
    """A session attributed to a worktree shard credits the PARENT when the aliases map
    (enumerate.worktree_parents) is passed — the newest session across the whole group
    wins, so the parent card reflects real latest activity."""
    idx: attribution.SessionIndex = {
        "/s/wt.jsonl": {"primary": "proj-fix", "mtime": 200.0, "count": 5},
        "/s/main.jsonl": {"primary": "proj", "mtime": 100.0, "count": 9},
    }
    got = attribution.most_recent_session("proj", idx, {"proj-fix": "proj"})
    assert got == Path("/s/wt.jsonl")
    # Without the alias map, the shard's session does NOT credit the parent.
    assert attribution.most_recent_session("proj", idx) == Path("/s/main.jsonl")


def test_parent_credited_from_worktree_sessions_only() -> None:
    """A parent with NO directly-attributed session still gets the newest of its
    worktrees' sessions — the all-work-happens-in-worktrees case (multiple shards,
    newest across the whole group wins)."""
    idx: attribution.SessionIndex = {
        "/s/fix1.jsonl": {"primary": "proj-fix1", "mtime": 100.0, "count": 4},
        "/s/fix2.jsonl": {"primary": "proj-fix2", "mtime": 200.0, "count": 7},
    }
    aliases = {"proj-fix1": "proj", "proj-fix2": "proj"}
    got = attribution.most_recent_session("proj", idx, aliases)
    assert got == Path("/s/fix2.jsonl")


def test_tier2_substantial_secondary_recalled() -> None:
    """A project with NO primary session anywhere recalls the newest session where it
    is a substantial secondary (>= _MENTION_FLOOR mentions) — e.g. a project whose
    work all happened inside its build-harness sibling's sessions. A below-floor
    name-drop never qualifies."""
    idx: attribution.SessionIndex = {
        "/s/build.jsonl": {"primary": "project-alpha", "mtime": 100.0, "count": 60,
                           "mentions": {"project-alpha": 60, "my-project": 40}},
        "/s/drop.jsonl": {"primary": "project-beta", "mtime": 200.0, "count": 9,
                          "mentions": {"project-beta": 9, "my-project": 2}},
    }
    # 40 mentions in build.jsonl clears the floor; the NEWER drop.jsonl has only a
    # 2-mention name-drop, which must not outrank it.
    assert attribution.most_recent_session("my-project", idx) == Path("/s/build.jsonl")
    # A project mentioned nowhere at all still gets None (open-here fallback upstream).
    assert attribution.most_recent_session("unmentioned", idx) is None


def test_tier1_primary_beats_newer_tier2_mention() -> None:
    """Tier 2 fires ONLY when no primary session exists: an older session primarily
    about X outranks a newer session that merely mentions X substantially."""
    idx: attribution.SessionIndex = {
        "/s/own.jsonl": {"primary": "my-project", "mtime": 100.0, "count": 20,
                         "mentions": {"my-project": 20}},
        "/s/other.jsonl": {"primary": "project-alpha", "mtime": 200.0, "count": 50,
                           "mentions": {"project-alpha": 50, "my-project": 10}},
    }
    assert attribution.most_recent_session("my-project", idx) == Path("/s/own.jsonl")


def test_tier2_alias_counts_are_summed() -> None:
    """Mentions scattered across a parent and its folded worktrees sum toward the
    floor: 2 + 2 alias-folded mentions = 4 >= floor, though neither alone qualifies."""
    idx: attribution.SessionIndex = {
        "/s/mix.jsonl": {"primary": "project-alpha", "mtime": 100.0, "count": 30,
                         "mentions": {"project-alpha": 30, "proj": 2, "proj-fix": 2}},
    }
    aliases = {"proj-fix": "proj"}
    assert attribution.most_recent_session("proj", idx, aliases) == Path("/s/mix.jsonl")
    assert attribution.most_recent_session("proj", idx) is None  # unsummed: 2 < floor


def test_tier2_newest_of_competing_qualifiers_wins() -> None:
    """When MULTIPLE sessions clear both tier-2 floors, recency decides — regardless
    of which has more mentions and of dict insertion order."""
    older = {"primary": "project-alpha", "mtime": 100.0, "count": 60,
             "mentions": {"project-alpha": 60, "my-project": 40}}
    newer = {"primary": "project-beta", "mtime": 200.0, "count": 50,
             "mentions": {"project-beta": 50, "my-project": 10}}
    idx: attribution.SessionIndex = {"/s/older.jsonl": older, "/s/newer.jsonl": newer}
    assert attribution.most_recent_session("my-project", idx) == Path("/s/newer.jsonl")
    # Flip the mtimes: the other file must win — proves recency, not order/strength.
    older["mtime"], newer["mtime"] = 300.0, 100.0
    assert attribution.most_recent_session("my-project", idx) == Path("/s/older.jsonl")


def test_tier2_share_floor_blocks_megasession_hijack() -> None:
    """A board-dump mega-session (huge dominant count) that name-drops a project a
    few times must NOT become its tier-2 donor: the mention count clears the absolute
    floor but fails the SHARE floor. Without this, the newest board-work session
    would hijack many unrelated cards."""
    idx: attribution.SessionIndex = {
        "/s/board.jsonl": {"primary": "project-alpha", "mtime": 500.0, "count": 1000,
                           "mentions": {"project-alpha": 1000, "my-project": 5}},
        "/s/build.jsonl": {"primary": "project-beta", "mtime": 100.0, "count": 60,
                           "mentions": {"project-beta": 60, "my-project": 40}},
    }
    # The mega-session is NEWER but fails the share test; the genuine donor wins.
    assert attribution.most_recent_session("my-project", idx) == Path("/s/build.jsonl")


def test_mentions_cap_keeps_top_five_valid() -> None:
    """_MENTIONS_CAP pins the accepted memory bound: only the 5 highest-count VALID
    projects are recorded, so a 6th-ranked project cannot be recalled via tier 2 from
    this session (documented residual — in many-project dump sessions that loss is
    the point; genuine donors rank their subjects high)."""
    valid = {f"p{i}" for i in range(6)}
    text = " ".join(f"/Claude/p{i}/f" * (10 - i) for i in range(6))
    _, _, mentions = attribution._primary_project(text, valid, SEG_RE)
    assert len(mentions) == 5
    assert "p5" not in mentions          # the lowest-count valid name is the one dropped
    assert mentions["p0"] == 10


def test_build_index_selfheals_entry_missing_mentions(tmp_path: Path) -> None:
    """A cached entry WITHOUT the tier-2 'mentions' field (written by an older
    version) is re-read despite a matching mtime — lazy self-heal, no forced
    full rebuild."""
    projects_root = tmp_path / "Claude"
    projects_root.mkdir()
    sroot = tmp_path / "projects"
    d = sroot / str(projects_root).replace("/", "-")
    d.mkdir(parents=True)
    f = d / "sess.jsonl"
    f.write_text("work on /Claude/my-project/x /Claude/my-project/y /Claude/my-project/z")
    mt = f.stat().st_mtime
    stale_prev: attribution.SessionIndex = {
        str(f): {"primary": "my-project", "mtime": mt, "count": 3}  # no "mentions"
    }
    idx = attribution.build_index(sroot, projects_root, {"my-project"}, prev_index=stale_prev)
    assert idx[str(f)] is not stale_prev[str(f)]          # re-read, not reused
    assert idx[str(f)]["mentions"] == {"my-project": 3}   # healed with tier-2 data
