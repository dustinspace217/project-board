# tests/test_enumerate.py — test suite for board.enumerate (Task 5).
#
# find_projects() scans the immediate subdirectories of ~/Claude and returns
# only those that qualify as projects. These tests verify every branch of the
# predicate: marker presence (CLAUDE.md / .git / plan docs), exclusions
# (.board-ignore / dotdirs / archive), and the no-marker fallthrough.

from pathlib import Path

from board.enumerate import find_file_projects, find_projects


def make_proj(
    root: Path,
    name: str,
    *,
    claudemd: bool = False,
    git: bool = False,
    ignore: bool = False,
    plans: bool = False,
    plan_md: bool = False,
    readme: bool = False,
    board_status: bool = False,
    source: str | None = None,
    other: str | None = None,
) -> Path:
    """Helper: create a subdirectory under root with optional project markers.

    Receives:
        root         — the tmp directory acting as ~/Claude.
        name         — subdirectory name to create.
        claudemd     — if True, create a CLAUDE.md file inside.
        git          — if True, create a .git/ subdirectory inside.
        ignore       — if True, create a .board-ignore file inside (exclude marker).
        plans        — if True, create docs/superpowers/plans/p.md inside.
        plan_md      — if True, create a root-level PLAN.md.
        readme       — if True, create a root-level README.md.
        board_status — if True, create a .board-status pin file.
        source       — relative path of a SOURCE file to create (e.g. "index.html",
                       "src/app.py"); marks the dir as having real content.
        other        — relative path of a NON-source file to create (e.g. "run.log",
                       "docs/note.md"); used to test that near-empty dirs are excluded.
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
    if plan_md:
        (d / "PLAN.md").write_text("# plan")
    if readme:
        (d / "README.md").write_text("# readme")
    if board_status:
        (d / ".board-status").write_text("bucket: finished\n")
    for rel in (source, other):
        if rel:
            f = d / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("x")
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


# --- Broader markers added so real projects without a repo/CLAUDE.md aren't missed ---


def test_includes_root_plan_or_readme(tmp_path: Path) -> None:
    """A dir with just a root-level PLAN.md or README.md qualifies."""
    make_proj(tmp_path, "planonly", plan_md=True)
    make_proj(tmp_path, "readmeonly", readme=True)
    names = {p.name for p in find_projects(tmp_path)}
    assert {"planonly", "readmeonly"} <= names


def test_includes_source_file_at_root_or_one_level_down(tmp_path: Path) -> None:
    """A dir with a source file at depth 0 (index.html) or depth 1 (src/app.py) qualifies."""
    make_proj(tmp_path, "webapp", source="index.html")
    make_proj(tmp_path, "scripts", source="src/app.py")
    names = {p.name for p in find_projects(tmp_path)}
    assert {"webapp", "scripts"} <= names


def test_includes_board_status_pin(tmp_path: Path) -> None:
    """A .board-status pin is itself a marker — an explicit 'this is a project'."""
    make_proj(tmp_path, "pinned", board_status=True)
    assert "pinned" in {p.name for p in find_projects(tmp_path)}


def test_excludes_near_empty_dirs(tmp_path: Path) -> None:
    """A lone log file (the logs-only case) or a lone .md buried in a subdir (the
    nested-doc case) is NOT enough to count as a project — no source, no root doc, no pin."""
    make_proj(tmp_path, "logsonly", other="run.log")
    make_proj(tmp_path, "subdocs", other="docs/note.md")
    names = {p.name for p in find_projects(tmp_path)}
    assert "logsonly" not in names
    assert "subdocs" not in names


def test_find_file_projects_selects_loose_plans(tmp_path: Path) -> None:
    """Root-level plan .md files are file-projects (e.g. project-alpha-plan.md); CLAUDE.md /
    README.md and anything that reads as a log/review/notes file are not."""
    for name in ("project-alpha-plan.md", "project-beta-plan.md", "CLAUDE.md", "README.md",
                 "permissions-log.md", "workspace-review-2026-06.md", "scratch-notes.md",
                 "not-markdown.txt"):
        (tmp_path / name).write_text("x")
    names = {p.name for p in find_file_projects(tmp_path)}
    assert names == {"project-alpha-plan.md", "project-beta-plan.md"}


def test_source_in_dotdir_or_too_deep_does_not_count(tmp_path: Path) -> None:
    """_has_source ignores source files hidden in a dotdir (e.g. .venv/) and ones more than
    one level deep — so a dir whose only code is in .venv/ or a/b/ is not a project."""
    make_proj(tmp_path, "venvonly", source=".venv/app.py")   # source inside a dotdir
    make_proj(tmp_path, "deeponly", source="a/b/app.py")      # source two levels down
    names = {p.name for p in find_projects(tmp_path)}
    assert "venvonly" not in names
    assert "deeponly" not in names
