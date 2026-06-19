# tests/test_enumerate.py — test suite for board.enumerate (Task 5).
#
# find_projects() scans the immediate subdirectories of ~/Claude and returns
# only those that qualify as projects. These tests verify every branch of the
# predicate: marker presence (CLAUDE.md / .git / plan docs), exclusions
# (.board-ignore / dotdirs / archive), and the no-marker fallthrough.

from pathlib import Path

from board.enumerate import find_projects


def make_proj(
    root: Path,
    name: str,
    *,
    claudemd: bool = False,
    git: bool = False,
    ignore: bool = False,
    plans: bool = False,
) -> Path:
    """Helper: create a subdirectory under root with optional project markers.

    Receives:
        root     — the tmp directory acting as ~/Claude.
        name     — subdirectory name to create.
        claudemd — if True, create a CLAUDE.md file inside.
        git      — if True, create a .git/ subdirectory inside.
        ignore   — if True, create a .board-ignore file inside (exclude marker).
        plans    — if True, create docs/superpowers/plans/p.md inside.
    Returns:
        The created directory Path.
    """
    d = root / name
    d.mkdir()
    if claudemd:
        (d / "CLAUDE.md").write_text("x")
    if git:
        (d / ".git").mkdir()
    if ignore:
        (d / ".board-ignore").write_text("")
    if plans:
        plan_dir = d / "docs" / "superpowers" / "plans"
        plan_dir.mkdir(parents=True)
        (plan_dir / "p.md").write_text("# plan")
    return d


def test_includes_dirs_with_markers(tmp_path: Path) -> None:
    """Directories with CLAUDE.md or .git must be included."""
    make_proj(tmp_path, "alpha", claudemd=True)
    make_proj(tmp_path, "beta", git=True)
    names = {p.name for p in find_projects(tmp_path)}
    assert names == {"alpha", "beta"}


def test_includes_dir_with_plan_docs(tmp_path: Path) -> None:
    """A directory with docs/superpowers/plans/*.md qualifies even without CLAUDE.md or .git.
    This covers projects that live in the spec-planning phase before they have
    a git repo or a top-level CLAUDE.md."""
    make_proj(tmp_path, "speconly", plans=True)
    names = {p.name for p in find_projects(tmp_path)}
    assert "speconly" in names


def test_excludes_archive_dotdirs_and_ignored(tmp_path: Path) -> None:
    """archive/, dot-dirs, .board-ignored dirs, and marker-less dirs are excluded."""
    make_proj(tmp_path, "archive", claudemd=True)
    make_proj(tmp_path, ".hidden", claudemd=True)
    make_proj(tmp_path, "dead", claudemd=True, ignore=True)
    make_proj(tmp_path, "live", claudemd=True)
    names = {p.name for p in find_projects(tmp_path)}
    assert names == {"live"}


def test_excludes_marker_less_dirs(tmp_path: Path) -> None:
    """A plain directory with no markers must not appear in the result."""
    (tmp_path / "justfiles").mkdir()
    assert find_projects(tmp_path) == []


def test_result_is_sorted(tmp_path: Path) -> None:
    """find_projects must return paths sorted by name for deterministic output."""
    make_proj(tmp_path, "zebra", claudemd=True)
    make_proj(tmp_path, "alpha", claudemd=True)
    names = [p.name for p in find_projects(tmp_path)]
    assert names == sorted(names)


def test_board_ignore_overrides_markers(tmp_path: Path) -> None:
    """.board-ignore must exclude even if CLAUDE.md and .git are both present."""
    make_proj(tmp_path, "ignored", claudemd=True, git=True, ignore=True)
    assert find_projects(tmp_path) == []
