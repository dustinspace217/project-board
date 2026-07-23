"""Enumerate ~/Claude project dirs per the scope rule (spec §5).

A directory qualifies as a project if ALL of these hold:
    1. It IS a directory.
    2. Its name does NOT start with '.'.
    3. Its name is NOT 'archive'.
    4. It does NOT contain a .board-ignore file.
    5. It has AT LEAST ONE project marker:
       - a CLAUDE.md file
       - a .git/ subdirectory
       - any *.md file under docs/superpowers/plans/
       - a root-level PLAN.md or README.md
       - a .board-status pin (an explicit pin IS an explicit "this is a project")
       - a source/content file at depth 0 or 1 (see _has_source)

Rules 1–4 are exclusion guards; rule 5 is the inclusion test.

Why the broad inclusion set: the original three markers missed real projects that
simply have no git repo and no CLAUDE.md yet — e.g. a from-scratch explorable that's
just index.html + src/, or an analysis dir of .py scripts. Requiring a source file or
a root plan/readme catches those while still excluding near-empty/stray dirs (an empty
dir, a single stray log or trace file). A dir you actively don't want can still be
suppressed with .board-ignore.

Why this shape:
    We scan only immediate subdirectories (depth 1) of the root, so the loop
    is bounded by the number of direct children of ~/Claude — typically O(10s)
    of entries, well within any meaningful bound (Power of Ten rule 2).
    We don't recurse; nested directories are the project's internal structure,
    not separate projects.
"""
from __future__ import annotations

from pathlib import Path

# Names that are unconditionally excluded, regardless of contents.
# 'archive' is the canonical parking lot for retired projects in this workspace.
_EXCLUDE_NAMES: frozenset[str] = frozenset({"archive"})

# File extensions that mark a directory as having real source/content (a project), as
# opposed to a stray log/trace/note. Markdown is deliberately NOT here — a lone .md is
# how near-empty dirs look; a root PLAN.md/README.md is checked separately as a marker.
_SOURCE_EXT: frozenset[str] = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".c", ".cpp", ".h", ".java",
    ".rb", ".php", ".html", ".css", ".qml", ".sh", ".lua", ".gd", ".vue", ".svelte",
})

# Cap on filesystem entries examined when sniffing for a source file, so a huge
# node_modules / SDK tree can't make the scan walk forever (Power of Ten rule 2).
_SCAN_CAP = 400


def _has_source(d: Path) -> bool:
    """True if d contains a source/content file at depth 0 or 1. Bounded: examines at most
    _SCAN_CAP entries. Most projects have a source file at the top level or one dir down
    (src/, compute/, etc.); we don't walk deeper, which keeps huge trees cheap to reject."""
    seen = 0
    subdirs: list[Path] = []
    # Iterate the generator (don't list() it) so a directory with 100k children doesn't
    # materialize the whole list before the cap applies — we stop at _SCAN_CAP entries.
    try:
        top = d.iterdir()
    except OSError:
        return False
    for entry in top:                            # depth 0
        seen += 1
        if seen > _SCAN_CAP:
            return False
        try:
            if entry.is_file() and entry.suffix in _SOURCE_EXT:
                return True
            if entry.is_dir() and not entry.name.startswith("."):
                subdirs.append(entry)            # bounded: at most _SCAN_CAP entries seen
        except OSError:
            continue
    for sub in subdirs:                          # depth 1 — total work still bounded by cap
        try:
            for entry in sub.iterdir():
                seen += 1
                if seen > _SCAN_CAP:
                    return False
                if entry.is_file() and entry.suffix in _SOURCE_EXT:
                    return True
        except OSError:
            continue
    return False


def _is_project(d: Path) -> bool:
    """Return True if directory d qualifies as a project under ~/Claude.

    Receives:
        d — a Path to an immediate subdirectory of ~/Claude (or the test root).
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
    # Broader markers (added so real projects without a repo/CLAUDE.md aren't missed):
    #   d) a root-level PLAN.md or README.md — plan-only or just-documented projects.
    #   e) a .board-status pin — you've explicitly declared this dir a tracked project.
    #   f) a source/content file at depth 0–1 — an explorable, a script dir, etc.
    has_root_doc = (d / "PLAN.md").exists() or (d / "README.md").exists()
    has_pin = (d / ".board-status").exists()
    return (has_claude_md or has_git or has_plans
            or has_root_doc or has_pin or _has_source(d))


def find_projects(root: Path) -> list[Path]:
    """Return all project directories that are immediate children of root.

    Receives:
        root — path to the ~/Claude directory (or a tmp dir in tests).
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


def attribution_names(root: Path) -> set[str]:
    """Project NAMES for session attribution: every boarded project PLUS every
    .board-ignore'd directory.

    Why retired dirs stay in this set: attribution stores each session's primary by
    NAME, and tier-3 family recall keys on that name (a project recalling a session
    primarily about its retired build harness). Retiring a dir with .board-ignore
    removes its CARD — but if it also left this set, re-indexed sessions would
    attribute to None and the family link would silently die. A retired project
    keeps its attribution identity; it just isn't displayed. Loop bounded by the
    root's entry count."""
    names = {p.name for p in find_projects(root)}
    for d in root.iterdir():
        try:
            if d.is_dir() and (d / ".board-ignore").exists():
                names.add(d.name)
        except OSError:
            continue
    return names


def worktree_parent(d: Path) -> str | None:
    """If d is a LINKED GIT WORKTREE of a sibling project, return the parent project's
    directory name; else None.

    How detection works: in a linked worktree, `.git` is a FILE (not a directory)
    holding a pointer line `gitdir: <parent-checkout>/.git/worktrees/<name>`. We
    anchor on that exact git-written tail (…/.git/worktrees/<name>) rather than
    searching for ".git" anywhere in the path, so an unlucky directory name can't
    fake a match. Fold only when the parent checkout is a SIBLING under the same
    projects root — a worktree of a repo living elsewhere has no parent card on
    this board to fold into, so it stands alone.

    Receives: d — an immediate subdirectory of the projects root.
    Returns:  the parent project's directory name, or None (not a worktree / parent
              not a sibling / unreadable pointer — all mean "treat as its own project").
    """
    gitfile = d / ".git"
    try:
        if not gitfile.is_file():
            return None     # .git is a directory (main checkout) or absent — not a worktree
        text = gitfile.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for raw in text.splitlines():   # bounded: a worktree pointer file is 1-2 lines
        line = raw.strip()
        if not line.startswith("gitdir:"):
            continue
        target = Path(line.partition(":")[2].strip())
        if not target.is_absolute():
            # git can write the pointer relative to the worktree (portable/moved setups)
            target = d / target
        # Resolve BOTH sides so the sibling comparison happens in one canonical space.
        # Comparing a resolved target against an unresolved d.parent would mix
        # normalizations: under a symlinked projects root a genuine worktree would
        # spuriously fail the sibling check, and a symlinked "parent" could fold a
        # card into a name that isn't on the board (QA finding, 2026-07-01).
        target = target.resolve()
        d_resolved = d.resolve()
        parts = target.parts
        # Anchor on the canonical tail git writes: <parent>/.git/worktrees/<name>
        if len(parts) < 4 or parts[-2] != "worktrees" or parts[-3] != ".git":
            return None
        parent_dir = Path(*parts[:-3])
        if (parent_dir.parent == d_resolved.parent and parent_dir != d_resolved
                and parent_dir.is_dir()):
            return parent_dir.name
        return None
    return None


def worktree_parents(projects: list[Path]) -> dict[str, str]:
    """Map worktree-project NAME -> parent-project NAME for every linked worktree in
    `projects` (from find_projects). scan.py uses this to (a) skip emitting a shard card
    for the worktree and (b) credit the worktree's attributed sessions to the parent, so
    one project stops appearing as N cards. Loop bounded by len(projects)."""
    out: dict[str, str] = {}
    for p in projects:
        parent = worktree_parent(p)
        if parent is not None:
            out[p.name] = parent
    return out


# Loose root-level .md FILES that are projects-in-a-file (a plan/spec not yet in its own
# directory) rather than notes/logs/config. We exclude the workspace config and anything
# that reads as a log or a review; everything else (plans, setup write-ups) is a project.
_FILE_PROJECT_DENY_NAMES: frozenset[str] = frozenset({"CLAUDE.md", "README.md"})
_FILE_PROJECT_DENY_SUBSTRINGS: tuple[str, ...] = ("log", "review", "notes")


def find_file_projects(root: Path) -> list[Path]:
    """Return root-level .md FILES that represent loose projects (a plan living as a single
    file at ~/Claude root, not yet in its own directory — e.g. a-plan.md).

    A file qualifies if it's a top-level *.md that is NOT CLAUDE.md/README.md and whose
    name doesn't read as a log/review/notes file. Loop bounded by the root's entry count.
    """
    out: list[Path] = []
    for f in root.iterdir():
        if not f.is_file() or f.suffix != ".md" or f.name in _FILE_PROJECT_DENY_NAMES:
            continue
        low = f.name.lower()
        if any(s in low for s in _FILE_PROJECT_DENY_SUBSTRINGS):
            continue
        out.append(f)
    return sorted(out, key=lambda p: p.name)
