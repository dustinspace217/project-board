"""Attribute Claude Code session transcripts to the project they're primarily about,
by counting `<root>/<project>` path-mentions, and cache the result in an index so the
15-minute scan only re-reads sessions that changed.

WHY: the user works on most projects from the projects-ROOT session, so a project's own
session folder mostly holds tangential command-spawns (e.g. /security-review). The REAL
work session lives in the big root pile and is found by content attribution — each
session is dominated by ONE project by a large margin (verified: a dominant project had
7502 mentions vs the next at 5).

The session-dir prefix and the path-segment regex are DERIVED from the projects root
that is passed in (rather than hardcoded to one machine's home), so the scanner works
for any user. Claude Code encodes a session's working directory into the session-dir
NAME by replacing every '/' with '-' (e.g. root "/home/me/Claude" -> dir prefix
"-home-me-Claude"), which is how we recover the prefix from the root path below."""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

# Cap per-session read when building the index (a transcript can be hundreds of MB).
_MAX_READ_BYTES = 16 * 1024 * 1024

# The attribution index: session-file-path -> {"primary": str|None, "mtime": float,
# "count": int}. Inner values are typed `object` (not a TypedDict) because the index is
# round-tripped through JSON on disk, so consumers must isinstance-guard before using a
# value — which also makes a corrupt/old on-disk index safe to read.
SessionIndex = dict[str, dict[str, object]]


def _dir_prefix(projects_root: Path) -> str:
    """The session-dir name prefix for projects under projects_root.

    Claude Code encodes a session's cwd into the directory name by replacing
    every '/' with '-'. So a root of "/home/me/Claude" becomes the prefix
    "-home-me-Claude", which matches the root pile AND every per-project dir
    ("-home-me-Claude-<project>"). Derived from projects_root (passed in from
    scan.py) so the scanner isn't pinned to one machine's home path.
    """
    return str(projects_root).replace("/", "-")


def _segment_re(projects_root: Path) -> re.Pattern[str]:
    """Regex capturing the path segment right after the projects-root NAME.

    e.g. for a root whose name is "Claude", this captures "project-alpha" from
    "/Claude/project-alpha/tools/x.py". Used to find the segment a transcript
    mentions most — including folder-LESS projects, so they don't silently leak
    into the next-highest real folder. The root name is re.escape()'d because it
    could contain regex-special characters.
    """
    name = re.escape(projects_root.name)
    return re.compile(rf"/{name}/([^/\s'\"`)\]}}>,;|]+)")


def candidate_session_dirs(sessions_root: Path, projects_root: Path) -> list[Path]:
    """Every ~/.claude/projects dir that could hold a session about a projects-root project.

    Receives:
        sessions_root  — the ~/.claude/projects directory of session logs.
        projects_root  — the projects root (e.g. ~/Claude); its path determines the
                         session-dir name prefix we match (see _dir_prefix).
    """
    if not sessions_root.is_dir():
        return []
    prefix = _dir_prefix(projects_root)
    return [d for d in sessions_root.iterdir() if d.is_dir() and d.name.startswith(prefix)]


def _primary_project(text: str, valid: set[str], seg_re: re.Pattern[str]) -> tuple[str | None, int]:
    """The project a transcript is primarily about. We find the OVERALL most-mentioned
    `<root>/<segment>` and attribute the session to it ONLY if that segment is a real
    folder project. If the top segment is a folder-less project (e.g. a single-file plan)
    or a non-project path (CLAUDE.md, archive), we return None rather than leaking the
    session into the next-highest folder. Returns (primary_or_None, top_mention_count).

    Receives seg_re — the per-root segment regex from _segment_re(), passed in so the
    pattern is built once per build_index() run rather than recompiled per session."""
    counts = Counter(seg_re.findall(text))
    if not counts:
        return None, 0
    top, n = counts.most_common(1)[0]
    return (top if top in valid else None), n


def build_index(sessions_root: Path, projects_root: Path, valid_projects: set[str],
                prev_index: SessionIndex | None = None) -> SessionIndex:
    """Map each session .jsonl path -> {primary, mtime, count}. INCREMENTAL: a session
    whose mtime matches the previous index is reused without re-reading (the expensive
    part). Only new/changed sessions are read. Loops bounded by the file count.

    Receives:
        sessions_root  — ~/.claude/projects, the directory of session logs.
        projects_root  — the projects root (e.g. ~/Claude). Determines both which session
                         dirs to scan (via the name prefix) and how `/root-name/<project>`
                         path-mentions are matched in transcripts (via the segment regex).
        valid_projects — the set of real folder-project names; a top segment outside this
                         set attributes to None (see _primary_project).
        prev_index     — the previous on-disk index for the incremental mtime reuse."""
    prev = prev_index or {}
    index: SessionIndex = {}
    # Build the segment regex once per run (not per session) — the root NAME is constant
    # across every transcript we scan, so recompiling it 700× would be wasted work.
    seg_re = _segment_re(projects_root)
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
            # build_index touches ~700 of them per scan (Power-of-Ten rule 3, bound memory).
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
