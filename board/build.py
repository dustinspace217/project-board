"""Assemble board.json from per-project signals.

This module is the orchestrator for the scanner. It ties together the four
lower-level modules (statusblock, classify, signals, enumerate) into one
public function — build_card() — that produces a single JSON-serialisable
card dict for one project.

State design:
    The scanner is intentionally STATELESS between runs, with one exception:
    finished_at. A project's finish date cannot be inferred from the project
    directory alone (the plan doc's Status block doesn't record when it was
    first marked Complete). So finished_at is carried forward from the previous
    board.json by build_board() in scan.py passing the prior card's dict as
    `prev`. This means the scanner only needs to persist board.json itself —
    no separate database, no hidden state files (spec §8).

Drop-off behaviour:
    Finished projects stay on the board for dropoff_days (default 5) after
    finished_at is first set, then disappear. This prevents the board filling
    up with permanently-completed work. build_card() returns None to signal
    "omit this card from output" (spec §8).

Staleness:
    A card is marked stale=True if now - last_touched > stale_days * 86400.
    This is a display hint only — stale cards are included in output.

See: spec §6 (schema), spec §8 (drop-off / staleness), CLAUDE.md (Power of Ten).
"""
from __future__ import annotations

import datetime as dt
import sys
import time
from pathlib import Path

from . import attribution, llm_classify, transcript
from .classify import bucket as classify_bucket
from .classify import owner as classify_owner
from .override import read_override
from .signals import git_last_commit, last_touched
from .statusblock import StatusBlock, parse_status_block


def _status_block_text(status: StatusBlock | None) -> str | None:
    """Format a Status block as plain text for the LLM prompt, or None if absent.
    Gives the model the human-written summary as extra context alongside the transcript."""
    if status is None:
        return None
    return (f"PHASE: {status.phase}\nDONE: {status.done}\n"
            f"NEXT: {status.next}\nBLOCKED: {status.blocked}")

# ---------------------------------------------------------------------------
# Pure helper functions — no I/O, fully unit-testable (tested in test_build.py)
# ---------------------------------------------------------------------------


def compute_finished_at(
    bucket: str,
    prev_finished_at: str | None,
    today: dt.date,
) -> str | None:
    """Return the finished_at ISO date string for a card, carrying it forward.

    This is the carry-forward logic described in spec §8:
        - If the current bucket is NOT 'finished', return None (clear it on reopen).
        - If it IS 'finished' and there's a prior date, preserve it unchanged.
        - If it IS 'finished' and no prior date, set it to today (first detection).

    Receives:
        bucket          — the bucket string for this scan (from classify.bucket()).
        prev_finished_at — the finished_at from the previous board.json card, or None.
        today           — the date of the current scan (passed in for testability;
                          avoids coupling to a real-time clock inside this helper).
    Returns:
        ISO date string (YYYY-MM-DD) if still finished; None otherwise.
    """
    if bucket != "finished":
        # Project is no longer finished (reopened, work added back) — clear the date.
        return None
    # Preserve the original finish date so the drop-off window stays stable.
    # If this is the first scan where the project hit 'finished', set it to today.
    return prev_finished_at or today.isoformat()


def apply_dropoff(
    bucket: str,
    finished_at: str | None,
    today: dt.date,
    dropoff_days: int,
) -> bool:
    """Return True if this card should be DROPPED from the output.

    A card is dropped when:
        - Its bucket is 'finished', AND
        - It has a finished_at date, AND
        - (today - finished_at).days > dropoff_days

    Non-finished cards are never dropped (return False).

    Receives:
        bucket       — the current bucket (only acts when 'finished').
        finished_at  — ISO date string from compute_finished_at(), or None.
        today        — date of the current scan.
        dropoff_days — how many days to keep a finished card visible.
    Returns:
        True = omit this card from board.json; False = include it.
    """
    if bucket != "finished" or finished_at is None:
        return False
    age = (today - dt.date.fromisoformat(finished_at)).days
    return age > dropoff_days


def is_stale(
    last_touched_epoch: float,
    today: dt.date,
    stale_days: int,
) -> bool:
    """Return True if the project has gone quiet beyond the staleness threshold.

    Staleness is defined as: now - last_touched > stale_days * 86400 seconds.
    'now' is derived from `today` (midnight local time) so that test results
    are deterministic — production callers pass dt.date.today().

    Receives:
        last_touched_epoch — Unix timestamp of the most recent project activity
                             (from signals.last_touched()).
        today              — date of the current scan (for testable 'now').
        stale_days         — how many inactive days before a card is marked stale.
    Returns:
        True if the project is stale; False if recently active.
    """
    # Convert 'today midnight' to a Unix timestamp for comparison with the epoch.
    # time.mktime interprets local time, matching how last_touched() computes mtimes.
    now = time.mktime(today.timetuple())
    return (now - last_touched_epoch) > stale_days * 86400


# ---------------------------------------------------------------------------
# Private helpers for build_card()
# ---------------------------------------------------------------------------


def _newest_status(project: Path) -> StatusBlock | None:
    """Return the Status block from the most-recently-modified plan doc, or None.

    Searches docs/superpowers/plans/*.md (if the directory exists), sorted by
    mtime descending. Returns the first Status block found. Most projects have
    exactly one plan doc, but some have multiple phases — we always want the
    freshest signal.

    Receives:
        project — absolute Path to the project directory.
    Returns:
        The most-recently-updated StatusBlock across all plan docs, or None if
        no plan docs exist or none of them contain a Status block.

    Loop is bounded by the number of .md files in the plans directory (finite).
    read_text errors='replace' prevents a badly-encoded plan doc from aborting
    the entire scan — we just get garbage characters for the bad bytes, which
    won't match any keyword.
    """
    plans_dir = project / "docs" / "superpowers" / "plans"
    if not plans_dir.is_dir():
        # No plans directory — project may be early-stage or have a different layout.
        return None

    # Sort by mtime descending so the most-recently-written doc comes first.
    # The loop is bounded by the (typically small) number of plan files.
    plans = sorted(plans_dir.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    for doc in plans:
        sb = parse_status_block(doc.read_text(encoding="utf-8", errors="replace"))
        if sb is not None:
            return sb
    return None


def _humanize(seconds: float) -> str:
    """Convert a duration in seconds to a human-readable 'time ago' string.

    Receives:
        seconds — elapsed seconds (should be non-negative; negatives read as 'today').
    Returns:
        One of: 'today', 'yesterday', 'N days ago', 'N weeks ago'.
    """
    days = int(seconds // 86400)
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 14:
        return f"{days} days ago"
    return f"{days // 7} weeks ago"


def _status_age(status: StatusBlock | None, today: dt.date) -> int | None:
    """Return how many days ago the Status block was last updated, or None.

    Returns None when there is no Status block, or when the block has no
    'updated' date (the '(updated YYYY-MM-DD)' suffix was omitted by the author).

    Receives:
        status — the StatusBlock for this project, or None.
        today  — date of the current scan.
    Returns:
        Integer days since status.updated, or None.
    """
    if status is None or status.updated is None:
        return None
    age = (today - status.updated).days
    # Clamp at 0: a future-dated status (author typo like '2026-13-45 → 2027-...')
    # would yield a negative age that reads as "very fresh". Zero is safe and honest —
    # we can't know the real age, so we report the minimum plausible value.
    return max(age, 0)


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


def build_card(
    project: Path,
    sessions_root: Path,
    index: attribution.SessionIndex,
    prev: dict[str, object],
    today: dt.date,
    dropoff_days: int,
    stale_days: int,
    allow_llm: bool = True,
) -> dict[str, object] | None:
    """Assemble one board card for a project, or return None if it should be dropped.

    This is the top-level function that brings all modules together for a single
    project directory. It is called once per project by scan.py's build_board_json().

    Where each field comes from:
        sess_path   — attribution.most_recent_session() (the project's real work session in
                      the root-cwd pile); falls back to transcript.pick_session() (own dir).
        bucket/owner/next/blocked — the local LLM (llm_classify.classify) reading sess_path's
                      recent turns + the Status block; or carried from `prev`; or, with no
                      transcript/allow_llm, the deterministic classify.bucket()/owner() heuristic.
        status      — _newest_status() reads the freshest plan doc's Status block (LLM context
                      + heuristic-fallback input).
        subject     — git_last_commit() runs `git log -1`; feeds last_done.
        finished_at — compute_finished_at() carries it forward from `prev` (the prior card).
        drop check  — apply_dropoff() returns None early if the card is past its window.
        sid         — sess_path's stem (the session UUID), for resume_cmd.
        touched     — last_touched() takes the max of session mtime, git mtime, dir mtime.
        stale       — is_stale() checks whether touched > stale_days ago.

    Receives:
        project      — absolute Path to the project directory.
        sessions_root — Path to ~/.claude/projects/ (passed in for testability).
        index        — attribution index (session -> primary project); {} disables attribution.
        prev         — the previous board.json card for this project (dict), or {} if new.
                       Carries finished_at AND the prior classification (for carry/changed-only).
        today        — date of the current scan.
        dropoff_days — how many days to keep finished cards visible.
        stale_days   — how many inactive days before marking a card stale.
        allow_llm    — False skips all LLM calls (GPU busy, or hermetic tests) -> carry/heuristic.
    Returns:
        A JSON-serialisable dict matching spec §6 schema, or None if dropped.

    Loop note: this function itself contains no loops; it delegates to helpers
    that each bound their own loops. See _newest_status(), recent_turns(),
    git_last_commit() for loop-bound documentation.
    """
    # --- Deterministic signals (cheap, no LLM) ---
    status = _newest_status(project)
    subject, git_time = git_last_commit(project)
    commit_days: int | None = None
    if git_time is not None:
        commit_days = int((time.time() - git_time) // 86400)

    # --- Find the session that's actually ABOUT this project (root pile + project dir)
    #     via the attribution index; its recent turns are the local LLM's input. ---
    sess_path = attribution.most_recent_session(project.name, index) if index else None
    if sess_path is None:
        # No root-pile attribution -> fall back to the project's OWN session dir (for
        # projects you cd into); pick_session prefers a real-work over a command session.
        sess_path = transcript.pick_session(project, sessions_root)
    sid = sess_path.stem if sess_path else None
    try:
        sess_mtime = sess_path.stat().st_mtime if sess_path else 0.0
    except OSError:
        sess_mtime = 0.0

    # --- Manual override: a `.board-status` pin (drag-drop or hand-edited) is authoritative.
    #     When a bucket is pinned we skip the LLM entirely — you've taken manual control. ---
    pinned = read_override(project)
    pinned_bucket = pinned.get("bucket")

    # --- Changed-only: spend an LLM call ONLY when there's new session activity since we
    #     last classified this project; otherwise carry the prior classification forward
    #     untouched. This is what keeps the 15-min scan cheap and low-footprint. ---
    _pm = prev.get("classified_at_mtime")
    prev_mtime = float(_pm) if isinstance(_pm, (int, float)) else 0.0
    # Re-classify when there's new session activity OR the prior card was NOT a confident
    # LLM read. Keying "unchanged" on prev being an LLM result means a gated/stale/heuristic
    # card gets re-attempted once the GPU frees, without waiting for new activity — so a
    # project never gets stuck on a fallback guess after a transient outage.
    prev_was_llm = prev.get("classified_by") == "llm"
    unchanged = prev_was_llm and sess_mtime <= prev_mtime + 1.0

    # Run the LLM only when nothing is pinned, a re-classify is needed, AND the GPU is free
    # (allow_llm, decided once per scan by the caller — so the model's OWN usage can't trip it).
    llm: dict[str, str] | None = None
    if not pinned_bucket and allow_llm and not unchanged:
        turns_text = (transcript.format_turns(transcript.recent_turns(sess_path))
                      if sess_path else "")
        llm = llm_classify.classify(turns_text, _status_block_text(status))

    # --- Resolve bucket/owner/next/blocked: pin -> live LLM -> carry prior card -> heuristic ---
    if pinned_bucket:
        # You pinned this (drag-drop or hand-edited .board-status). Pinned fields win;
        # fields the pin doesn't specify get sensible defaults for the pinned bucket.
        bucket = pinned_bucket
        if pinned_bucket == "finished":
            owner, nxt, blocked = "none", "nothing", "nothing"
        else:
            owner, nxt, blocked = "you", "", "nothing"
        owner = pinned.get("owner", owner)
        nxt = pinned.get("next", nxt)
        blocked = pinned.get("blocked", blocked)
        source = "pinned"
    elif llm is not None:
        bucket, owner = llm["bucket"], llm["owner"]
        nxt, blocked, source = llm["next"], llm["blocked"], "llm"
    elif prev.get("bucket"):
        # Carry the prior card forward, but LABEL WHY so a silent LLM outage stays visible
        # (the user's "never silently produce wrong data" bar):
        #   carried — prior LLM read, no new activity (healthy, cheap skip)
        #   gated   — GPU busy; we deliberately skipped a needed re-classify
        #   stale   — the LLM was TRIED and FAILED (Ollama down / model unpulled / bad output)
        bucket = str(prev.get("bucket"))
        owner = str(prev.get("owner", "none"))
        nxt = str(prev.get("next", ""))
        blocked = str(prev.get("blocked", ""))
        if unchanged:
            source = "carried"
        elif not allow_llm:
            source = "gated"
        else:
            source = "stale"
    else:
        bucket = classify_bucket(status, commit_days)
        owner = classify_owner(status, bucket)
        nxt = status.next if status else ""
        blocked = status.blocked if status else ""
        source = "heuristic"

    # A .board-status may pin individual fields WITHOUT a bucket (e.g. just `owner: you`).
    # The bucket-pin branch above only fires when a bucket is pinned, so apply any non-bucket
    # pins here, on top of whatever classification ran — otherwise a partial pin (which the
    # docstring invites) would be silently ignored.
    if pinned and not pinned_bucket:
        owner = pinned.get("owner", owner)
        nxt = pinned.get("next", nxt)
        blocked = pinned.get("blocked", blocked)
        source = "pinned"

    # --- finished_at + drop-off (based on the resolved bucket) ---
    # A PINNED card never drops: you placed it deliberately, so it stays where you put it
    # until you unpin. Otherwise an aged-off finished project is FLAGGED `dropped` (hidden by
    # default, revealed by the plasmoid "Show all" toggle) rather than removed from board.json
    # entirely — so nothing silently vanishes from the board's knowledge.
    prev_finished_at = prev.get("finished_at")
    finished_at = compute_finished_at(
        bucket, str(prev_finished_at) if prev_finished_at is not None else None, today)
    dropped = bool(not pinned_bucket
                   and apply_dropoff(bucket, finished_at, today, dropoff_days))

    # --- Last-touched (max of session mtime, git, dir mtime), clock unified to today ---
    touched = last_touched(project, sess_mtime, git_time)
    now_epoch = time.mktime(today.timetuple())

    # resume_cmd: resume the actual work session by id (works from any cwd — the session
    # records its own directory). Display hint only; the scanner never runs it.
    resume_cmd: str | None = f"claude --resume {sid}" if sid else None

    return {
        "name": project.name,
        "path": str(project),
        "bucket": bucket,
        "last_done": (subject or (status.done if status else "")),
        "last_touched_iso": dt.datetime.fromtimestamp(touched).isoformat(timespec="seconds"),
        "last_touched_human": _humanize(now_epoch - touched),
        "owner": owner,
        "next": nxt,
        "blocked": blocked,
        "resume_session_id": sid,
        "resume_cmd": resume_cmd,
        # How the status was determined: pinned | llm | carried | gated | stale | heuristic.
        "classified_by": source,
        # Session mtime at classification time, for next scan's changed-only check.
        "classified_at_mtime": sess_mtime,
        # True only when we had nothing better than the heuristic and no Status block.
        "needs_status": status is None and source == "heuristic",
        "finished_at": finished_at,
        "stale": is_stale(touched, today, stale_days),
        # True once a finished project has aged past the drop-off window: hidden by default,
        # shown by the plasmoid "Show all" toggle (not removed, so nothing is lost).
        "dropped": dropped,
        "is_file": False,  # a real directory project (vs a loose root-level plan file)
    }


def build_file_card(
    file: Path,
    today: dt.date,
    stale_days: int,
    allow_llm: bool = True,
) -> dict[str, object]:
    """Assemble a card for a LOOSE root-level .md 'project' file — a plan living as a single
    file, not yet in its own directory (e.g. a-plan.md).

    Lighter than build_card: there is no session transcript, no git, and no .board-status —
    the file's OWN content is the input. We hand that content to the LLM (as a status block)
    to classify; if the LLM is unavailable we default to planning/you (an unstarted plan
    awaiting your action). File-projects never drop off — delete the file when it's done.
    """
    try:
        content = file.read_text(encoding="utf-8", errors="replace")[:4000]
        mtime = file.stat().st_mtime
    except OSError as e:
        print(f"project-board: cannot read file-project {file}: {e}", file=sys.stderr)
        content, mtime = "", 0.0

    llm = llm_classify.classify("", content) if allow_llm else None
    if llm is not None:
        bucket, owner = llm["bucket"], llm["owner"]
        nxt, blocked, source = llm["next"], llm["blocked"], "llm"
    else:
        bucket, owner, nxt, blocked, source = "planning", "you", "", "nothing", "file"

    now_epoch = time.mktime(today.timetuple())
    return {
        "name": file.stem,
        "path": str(file),
        "bucket": bucket,
        "last_done": "",
        "last_touched_iso": dt.datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
        "last_touched_human": _humanize(now_epoch - mtime),
        "owner": owner,
        "next": nxt,
        "blocked": blocked,
        "resume_session_id": None,
        "resume_cmd": None,
        "classified_by": source,
        "classified_at_mtime": mtime,
        "needs_status": False,
        "finished_at": None,
        "stale": is_stale(mtime, today, stale_days),
        "dropped": False,
        "is_file": True,  # a loose root-level plan file, not a directory project
    }
