#!/usr/bin/env python3
"""Scan ~/Claude projects → board.json. Pure stdlib; no daemon required.

Entry point: run `python3 scan.py` from anywhere. Typical run time: 1–2 s.

Default paths (all configurable by calling build_board_json() directly):
    root        ~/Claude                             (project directories)
    sessions    ~/.claude/projects                   (Claude session logs)
    out         ~/.local/share/project-board/board.json

Atomic write:
    The output file is written to a .tmp sibling first, then renamed over the
    real path. On POSIX, rename() is atomic at the filesystem level — the
    plasmoid that reads board.json can never observe a half-written file.
    (Why atomic: the plasmoid polls or watches the file; a read that lands
    mid-write would produce invalid JSON and crash the QML parser.)

Carry-forward:
    If board.json already exists, it is read and its cards are indexed by name.
    finished_at from the prior card is the only field carried forward — all
    other fields are recomputed from disk on every scan. This keeps the scanner
    stateless except for that one date (spec §8).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

from board import attribution
from board.build import build_card, build_file_card
from board.enumerate import find_file_projects, find_projects
from board.gpu_gate import gpu_is_busy

# Schema version written into the 'meta' block. Increment when the card
# schema changes in a backwards-incompatible way so consumers can gate on it.
SCAN_VERSION = 1

# Bucket display order for sorting cards: active-work buckets come first so
# the most actionable projects appear at the top of the board.
# Index 0 = planning (needs direction), 4 = finished (done, least urgent).
_BUCKET_ORDER = ("planning", "writing", "QA", "testing", "finished")


def build_board_json(
    claude_root: Path,
    sessions_root: Path,
    index: attribution.SessionIndex,
    prev: dict[str, object] | None,
    today: dt.date,
    dropoff_days: int,
    stale_days: int,
    allow_llm: bool = True,
) -> dict[str, object]:
    """Scan all projects under claude_root and return a board dict.

    This function has no side effects (no file I/O). The caller (main) is
    responsible for writing the result to disk atomically AND for building the
    attribution `index` that maps each session transcript to its primary project.

    Receives:
        claude_root   — path to ~/Claude (or a tmp root in tests).
        sessions_root — path to ~/.claude/projects (session log directories).
        index         — attribution index from attribution.build_index() (which
                        session is primarily about which project). Built by main().
        prev          — the parsed contents of the previous board.json, or None
                        on the first run. Used only to carry finished_at forward.
        today         — the date to use as 'now' (passed in for testability).
        dropoff_days  — days after which a finished card is dropped from output.
        stale_days    — days of inactivity after which a card is marked stale.
    Returns:
        A JSON-serialisable dict with 'meta' and 'cards' keys (spec §6 schema).

    Loop note: the project loop is bounded by find_projects()'s return value,
    which is itself bounded by the number of immediate subdirs of claude_root
    (typically O(10–30) entries for ~/Claude).
    """
    # Index the previous board's cards by project name so we can look up
    # finished_at for each project in O(1). prev may be None (first run) or
    # a dict with a 'cards' key whose value is a list of card dicts.
    prev_cards_raw = (prev or {}).get("cards", [])
    # Build the lookup: name → prior card dict. Type guard: we know each element
    # from board.json is a dict[str, object] because we wrote it that way.
    prev_by_name: dict[str, dict[str, object]] = {}
    if isinstance(prev_cards_raw, list):
        for item in prev_cards_raw:
            if isinstance(item, dict) and "name" in item:
                # Cast is safe: JSON dicts always have str keys; values are object.
                card_dict: dict[str, object] = {str(k): v for k, v in item.items()}
                name = card_dict.get("name")
                if isinstance(name, str):
                    prev_by_name[name] = card_dict

    # Build one card per directory project. find_projects() returns a sorted list, so the
    # loop order is deterministic. build_card() flags aged-off finished projects `dropped`
    # rather than removing them (the "Show all" toggle reveals them).
    cards: list[dict[str, object]] = []
    for proj in find_projects(claude_root):  # bounded by project count
        card = build_card(
            proj,
            sessions_root,
            index,
            prev_by_name.get(proj.name, {}),
            today,
            dropoff_days,
            stale_days,
            allow_llm,
        )
        if card is not None:
            cards.append(card)

    # Loose root-level plan files (e.g. my-plan.md) are projects-in-a-file with no
    # directory — surface them as lightweight cards so they aren't invisible.
    for f in find_file_projects(claude_root):  # bounded by root entry count
        cards.append(build_file_card(f, today, stale_days, allow_llm))

    # Sort cards: planning first (most needs attention), finished last.
    # Within each bucket, sort by last_touched_iso ascending (oldest first),
    # so long-quiet projects appear before recently-touched ones and stand out.
    cards.sort(
        key=lambda c: (
            _BUCKET_ORDER.index(str(c["bucket"])),
            str(c.get("last_touched_iso", "")),
        )
    )

    return {
        "meta": {
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "scan_version": SCAN_VERSION,
            "dropoff_days": dropoff_days,
            "stale_days": stale_days,
        },
        "cards": cards,
    }


def main() -> None:
    """Read the prior board.json (if any), scan projects, write atomically."""
    home = Path.home()
    out_path = home / ".local" / "share" / "project-board" / "board.json"
    # Ensure the output directory exists (first run on a fresh machine).
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Load the previous board.json for the carry-forward step.
    # If it doesn't exist (first run) or is unparseable, we start fresh.
    prev: dict[str, object] | None = None
    if out_path.exists():
        try:
            raw = json.loads(out_path.read_text(encoding="utf-8"))
            # Sanity-check: must be a dict to be a valid board.json.
            prev = raw if isinstance(raw, dict) else None
        except (json.JSONDecodeError, OSError):
            # Corrupt or unreadable prior file — proceed without carry-forward.
            # Warn on stderr so the user knows finished_at history was reset:
            # the drop-off clock restarts, meaning previously-finished projects
            # may briefly reappear on the board before being dropped again.
            print(
                "project-board: prior board.json unreadable"
                " — finished_at history reset, drop-off clocks restart",
                file=sys.stderr,
            )
            prev = None

    # Projects root: where the scanned project directories live. Defaults to ~/Claude
    # but is overridable via PROJECT_BOARD_ROOT so the tool works for any user / layout
    # (the attribution module derives its session-dir prefix and path regex from this).
    claude_root = Path(os.environ.get("PROJECT_BOARD_ROOT", str(home / "Claude")))
    sessions_root = home / ".claude" / "projects"

    # Attribution index: which project each session transcript is primarily about.
    # Cached to disk so each scan only re-reads sessions that CHANGED (incremental),
    # rather than re-reading the whole ~700-session pile every 15 minutes.
    index_path = out_path.parent / "session_index.json"
    prev_index: attribution.SessionIndex = {}
    if index_path.exists():
        try:
            loaded = json.loads(index_path.read_text(encoding="utf-8"))
            prev_index = loaded if isinstance(loaded, dict) else {}
        except (json.JSONDecodeError, OSError):
            prev_index = {}  # corrupt index -> rebuild from scratch (just slower this once)
    valid_projects = {p.name for p in find_projects(claude_root)}
    index = attribution.build_index(sessions_root, claude_root, valid_projects, prev_index)

    # Check the GPU ONCE, before loading the model: is the GPU busy (e.g. a game or
    # another GPU-heavy app running)?
    # If so, skip every LLM call this scan (cards carry forward) so we don't contend for
    # the GPU. Checking once (not per-project) means the model's OWN usage during the scan
    # can't trip the gate — which was making most cards fall back to "gated".
    allow_llm = not gpu_is_busy()
    if not allow_llm:
        print("project-board: GPU busy — skipping LLM classification this scan "
              "(cards carry forward)", file=sys.stderr)

    board = build_board_json(
        claude_root,
        sessions_root,
        index,
        prev,
        dt.date.today(),
        dropoff_days=5,
        stale_days=14,
        allow_llm=allow_llm,
    )

    # Atomic write: write to a .tmp sibling, then rename over the real path.
    # On POSIX, os.rename() (which Path.replace() calls) is atomic — the
    # plasmoid never reads a partial file. The .tmp extension signals "in
    # progress" to any monitoring tool that notices it briefly.
    # chmod 0o600: board.json carries transcript-derived text (next/last_done) which a
    # model could echo a secret into — keep it owner-only, not the default world-readable 644.
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(board, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(out_path)

    # Persist the attribution index for the next run's incremental rebuild — same atomic
    # tmp+replace discipline as board.json (a torn index would force a slow full rebuild).
    idx_tmp = index_path.with_suffix(".json.tmp")
    idx_tmp.write_text(json.dumps(index), encoding="utf-8")
    idx_tmp.chmod(0o600)
    idx_tmp.replace(index_path)

    n = len(board["cards"]) if isinstance(board["cards"], list) else 0
    print(f"board.json written: {n} cards → {out_path}")


if __name__ == "__main__":
    main()
