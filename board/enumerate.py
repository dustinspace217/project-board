"""Enumerate ~/projects project dirs per the scope rule (spec §5).

A directory qualifies as a project if ALL of these hold:
    1. It IS a directory.
    2. Its name does NOT start with '.'.
    3. Its name is NOT 'archive'.
    4. It does NOT contain a .board-ignore file.
    5. It has AT LEAST ONE of:
       - a CLAUDE.md file
       - a .git/ subdirectory
       - any *.md file under docs/superpowers/plans/

Rules 1–4 are exclusion guards; rule 5 is the inclusion test.

Why this shape:
    We scan only immediate subdirectories (depth 1) of the root, so the loop
    is bounded by the number of direct children of ~/projects — typically O(10s)
    of entries, well within any meaningful bound (Power of Ten rule 2).
    We don't recurse; nested directories are the project's internal structure,
    not separate projects.
"""
from __future__ import annotations

from pathlib import Path

# Names that are unconditionally excluded, regardless of contents.
# 'archive' is the canonical parking lot for retired projects in this workspace.
_EXCLUDE_NAMES: frozenset[str] = frozenset({"archive"})


def _is_project(d: Path) -> bool:
    """Return True if directory d qualifies as a project under ~/projects.

    Receives:
        d — a Path to an immediate subdirectory of ~/projects (or the test root).
    Returns:
        True if d should appear in the project board; False otherwise.

    All five criteria must hold — see module docstring for the rationale.
    Short-circuit ordering: cheap fs-metadata checks (is_dir, name) come first
    so we avoid stat()-ing .board-ignore when the name already disqualifies.
    """
    # Guard 1–3: must be a directory, not a dotdir, not in the exclude list.
    if not d.is_dir() or d.name.startswith(".") or d.name in _EXCLUDE_NAMES:
        return False

    # Guard 4: an explicit opt-out marker trumps all markers below.
    # .board-ignore is analogous to .gitignore — present = "don't scan me".
    if (d / ".board-ignore").exists():
        return False

    # Inclusion check: at least one project marker must be present.
    # Three markers are accepted, any one is sufficient:
    #   a) CLAUDE.md  — present in projects that have been set up for Claude Code.
    #   b) .git/      — present in version-controlled projects (even without CLAUDE.md).
    #   c) plan docs  — present in spec-phase projects that haven't yet started coding.
    #
    # Why not require CLAUDE.md alone: new projects are created with git init
    # before they get a CLAUDE.md; plan-only projects (this repo before Task 0)
    # have docs/superpowers/plans/*.md but no CLAUDE.md or git yet.
    has_claude_md = (d / "CLAUDE.md").exists()
    has_git = (d / ".git").exists()
    # any() short-circuits after the first match; glob is bounded by plan dir contents.
    has_plans = (d / "docs" / "superpowers" / "plans").is_dir() and any(
        (d / "docs" / "superpowers" / "plans").glob("*.md")
    )
    return has_claude_md or has_git or has_plans


def find_projects(root: Path) -> list[Path]:
    """Return all project directories that are immediate children of root.

    Receives:
        root — path to the ~/projects directory (or a tmp dir in tests).
    Returns:
        Sorted list of qualifying project Paths. Sorted by name for deterministic
        output — board consumers should not depend on filesystem ordering.

    Loop is bounded by the number of direct entries under root (finite directory).
    Directories that fail _is_project() are silently skipped (not errors).
    """
    return sorted(
        (d for d in root.iterdir() if _is_project(d)),
        key=lambda p: p.name,
    )
