"""Attribute Claude Code session transcripts to the project they're primarily about,
by counting `<root>/<project>` path-mentions, and cache the result in an index so the
periodic scan only re-reads sessions that changed.

WHY: a Claude Code user often works on many projects from ONE session started at the
projects-root directory, so a project's own session folder may hold only tangential
command-spawns (e.g. /security-review). The real work session lives in the big root-cwd
pile and is found by content attribution — each session tends to be dominated by ONE
project by a large margin.

The projects-root path (e.g. ~/Claude, or whatever PROJECT_BOARD_ROOT points at) is passed
in, so nothing about any particular machine or user is hardcoded.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

# Cap per-session read when building the index (a transcript can be hundreds of MB).
_MAX_READ_BYTES = 16 * 1024 * 1024


def _root_prefix(projects_root: Path) -> str:
    """The leading session-dir name for sessions started at/under the projects root.

    ~/.claude/projects encodes a session's cwd as the absolute path with '/' -> '-', so a
    session started in <projects_root> (or a subdir) lives in a dir whose name starts with
    str(projects_root).replace('/', '-')."""
    return str(projects_root).replace("/", "-")


def _segment_re(projects_root: Path) -> re.Pattern[str]:
    """Regex capturing the path segment right after `<root_basename>/`, e.g. for a root of
    ~/Claude it matches `/Claude/<name>`. Used to find which project a transcript mentions
    most. Built from the root's basename so it works for any projects-root location."""
    return re.compile(r"/" + re.escape(projects_root.name) + r"/([^/\s'\"`)\]}>,;|]+)")


def candidate_session_dirs(sessions_root: Path, projects_root: Path) -> list[Path]:
    """Every ~/.claude/projects dir that could hold a session about a project under the
    projects root: the root-cwd pile plus every per-project dir (names share the prefix)."""
    if not sessions_root.is_dir():
        return []
    prefix = _root_prefix(projects_root)
    return [d for d in sessions_root.iterdir() if d.is_dir() and d.name.startswith(prefix)]


def _primary_project(text: str, valid: set[str], seg_re: re.Pattern[str]) -> tuple[str | None, int]:
    """The project a transcript is primarily about. We find the OVERALL most-mentioned
    `<root>/<segment>` and attribute the session to it ONLY if that segment is a real
    project directory. If the top segment is a non-project path (a loose file, an archive
    dir, or a project with no folder), we return None rather than leaking the session into
    the next-highest real project. Returns (primary_or_None, top_mention_count)."""
    counts = Counter(seg_re.findall(text))
    if not counts:
        return None, 0
    top, n = counts.most_common(1)[0]
    return (top if top in valid else None), n


# The attribution index: session-file-path -> {"primary": str|None, "mtime": float,
# "count": int}. Inner values are typed `object` (not a TypedDict) because the index is
# round-tripped through JSON on disk, so consumers must isinstance-guard before using a
# value — which also makes a corrupt/old on-disk index safe to read.
SessionIndex = dict[str, dict[str, object]]


def build_index(sessions_root: Path, valid_projects: set[str], projects_root: Path,
                prev_index: SessionIndex | None = None) -> SessionIndex:
    """Map each session .jsonl path -> {primary, mtime, count}. INCREMENTAL: a session
    whose mtime matches the previous index is reused without re-reading (the expensive
    part). Only new/changed sessions are read. Loops bounded by the file count."""
    seg_re = _segment_re(projects_root)
    prev = prev_index or {}
    index: SessionIndex = {}
    for d in candidate_session_dirs(sessions_root, projects_root):
        for f in d.glob("*.jsonl"):
            key = str(f)
            try:
                mt = f.stat().st_mtime
            except OSError:
                continue
            cached = prev.get(key)
            if cached is not None:
                cached_mt = cached.get("mtime")
                if isinstance(cached_mt, (int, float)) and abs(cached_mt - mt) < 1.0:
                    index[key] = cached        # unchanged -> reuse cached attribution
                    continue
            # Read a CAPPED slice, not the whole file: a session can be hundreds of MB and
            # build_index touches many of them per scan (Power-of-Ten rule 3, bound memory).
            # The dominant project is mentioned throughout, so the first _MAX_READ_BYTES is a
            # sound proxy for which project a huge transcript is primarily about.
            try:
                with f.open("rb") as fh:
                    text = fh.read(_MAX_READ_BYTES).decode("utf-8", errors="replace")
            except OSError:
                continue
            primary, n = _primary_project(text, valid_projects, seg_re)
            index[key] = {"primary": primary, "mtime": mt, "count": n}
    return index


def most_recent_session(project: str, index: SessionIndex) -> Path | None:
    """The most recent session file (by mtime) whose PRIMARY project is `project`,
    or None if no session is primarily about it. Bounded by the index size."""
    best: str | None = None
    best_mt = -1.0
    for key, meta in index.items():
        mt = meta.get("mtime")
        if meta.get("primary") == project and isinstance(mt, (int, float)) and mt > best_mt:
            best, best_mt = key, float(mt)
    return Path(best) if best else None
