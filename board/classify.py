"""Pure bucket/owner classification — keyword heuristics, no I/O, no LLM.

How this module works:
    Two public functions — bucket() and owner() — take a StatusBlock (or None)
    and return short string labels used by build.py to populate board.json.

    Everything is stateless: given the same inputs, you always get the same
    output. That makes the rules trivially unit-testable and easy to tune
    (wrong bucket? add/remove a keyword from the relevant constant below).

Bucket order matters:
    first match wins, most-terminal first:
        finished → QA → testing → planning → writing
    The rationale: "finished" must be detected early to avoid QA/testing
    keywords in completed projects promoting them back into active work.
    "writing" is the default catch-all — active midflight work that doesn't
    match a more specific label.

Reference: spec §7 (classification rules), CLAUDE.md (Power of Ten rule 1 —
no non-local jumps; rule 4 — functions ≤ ~60 code lines).
"""
from __future__ import annotations

# postpones evaluation of type hints so 'StatusBlock | None' works at runtime
# without importing TYPE_CHECKING — clean and forward-compatible.
import re

from .statusblock import StatusBlock  # relative import: this module lives in board/

# ---------------------------------------------------------------------------
# Regex constants — compiled once at import time, reused on every call.
# Module-level constants (Power of Ten rule 6: smallest scope — these ARE
# module-scope because they're shared by bucket() and owner(); they don't
# belong inside a function where they'd recompile each call).
# ---------------------------------------------------------------------------

# Matches terminal phase keywords — signals a project is done.
# We only search status.phase (not the full blob) to avoid false positives
# from "done" appearing in status.done for a still-active project.
_FINISHED = re.compile(r"\b(complete|completed|deployed|shipped|merged|done)\b", re.I)

# Matches QA/review work in the active portion of a status (phase + next).
# "phase [abc]" catches review-phase naming conventions like "Phase A".
# We intentionally do NOT search done or blocked here — done describes
# completed work ("QA review complete" means past QA, not in it), and blocked
# is a dependency note rather than a description of current activity.
_QA = re.compile(r"\b(qa|review|phase\s+[abc])\b", re.I)

# Matches testing/stabilization work in the active portion (phase + next).
# "stabiliz" is a partial match (covers stabilize/stabilizing/stabilization).
# Same scoping rationale as _QA above: done/blocked are excluded.
_TESTING = re.compile(r"\b(test|testing|stabiliz|verification|verify)\b", re.I)

# Matches planning/design keywords in status.phase specifically.
# We only check phase (not the full blob) to avoid "plan doc" in status.done
# wrongly classifying an active coding project as planning.
_PLANNING = re.compile(r"\b(plan|spec|design|brainstorm)\b", re.I)

# Phrases that mean the ball is in the human's court (they must act before I can).
# Searched in both status.blocked and status.next. Name-AGNOSTIC by design: instead
# of matching a specific person's name, we match "the human acts next" cues that work
# for any user — awaiting/you/your plus the action verbs a human owner does (confirm,
# decide, provide, approve, merge, review) and the contexts only a human can supply
# (manual, hardware, test on).
# Bare "user\b" is deliberately NOT in the list: it matched "user-facing copy",
# "user flow", etc., wrongly flipping owner to 'you' for generic UX tasks. We use the
# action-adjacent "awaiting user" / "user will" forms (handled by "awaiting" and the
# verbs) rather than the bare word.
# The alternation is ordered longest-first to help regex engines avoid backtracking
# on partial matches.
_USER_ACTION = re.compile(
    r"\b(will provide|awaiting|test on|provide|confirm|decide"
    r"|manual|hardware|approve|merge|review|you|your)\b",
    re.I,
)

# Canonical bucket names in display order (planning → writing → QA → testing → finished).
# Not used for dispatch (bucket() uses explicit if-chains with first-match order);
# this constant is here as a reference for callers that need the full set.
BUCKETS = ("planning", "writing", "QA", "testing", "finished")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_nothing(s: str) -> bool:
    """Return True if a field value is semantically empty/absent.

    Matches the common Status-block conventions for 'no value':
    empty string, "nothing", "none", "n/a".
    Receives: s — the field value string, already stripped.
    Used by bucket() and owner() to check blocked and next fields.
    """
    return s.strip().lower() in ("", "nothing", "none", "n/a")


def _next_clear(s: str) -> bool:
    """Return True if status.next represents a cleared/complete state.

    A project is only finished if there's nothing left to do:
    either next is empty/nothing, or explicitly says work is closed.
    Receives: s — the 'next' field value from a StatusBlock.
    """
    t = s.strip().lower()
    return _is_nothing(s) or "closed" in t


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def bucket(status: StatusBlock | None, recent_commit_days: int | None) -> str:
    """Classify a project into one of five buckets.

    Receives:
        status            — parsed StatusBlock, or None if the project has no
                            ## Status block in any plan doc.
        recent_commit_days — days since the last git commit, or None if the
                            project has no git repo (or no commits yet).

    Returns:
        One of: "finished" | "QA" | "testing" | "planning" | "writing"
        First match wins (finished is checked first to avoid false promotions).

    Why first-match-wins matters:
        A project whose phase says "Complete — shipped" but whose next says
        "run QA review" would wrongly land in QA if we checked QA first.
        Terminal state takes priority over in-progress signals.
    """
    # No StatusBlock at all: fall back to git activity as the only signal.
    # Recent commit (≤ 14 days) → active work is happening → "writing".
    # No commits or old commits → probably in planning or abandoned → "planning".
    if status is None:
        # recent_commit_days <= 14 means active; None means no repo (treat as inactive).
        return "writing" if (recent_commit_days is not None and recent_commit_days <= 14) \
            else "planning"

    # Build a search string from only the ACTIVE fields (phase + next).
    # done describes completed work — a keyword there (e.g. "QA review complete",
    # "ran tests") signals the project is PAST that stage, not currently in it.
    # blocked is a dependency note, not a description of current work.
    # _FINISHED and _PLANNING already scope to status.phase specifically;
    # _QA and _TESTING use this active string for the same reason.
    active = f"{status.phase} {status.next}"

    # 1. finished — all three gates must pass:
    #    a) a terminal keyword appears in phase (not just anywhere — "done" in
    #       status.done would always match an active project)
    #    b) blocked is clear (nothing blocking)
    #    c) next is empty or closed (no outstanding work)
    if _FINISHED.search(status.phase) and _is_nothing(status.blocked) and _next_clear(status.next):
        return "finished"

    # 2. QA — any QA/review keyword in phase or next (active work only).
    if _QA.search(active):
        return "QA"

    # 3. testing — any testing/stabilization keyword in phase or next.
    if _TESTING.search(active):
        return "testing"

    # 4. planning — planning/design keyword in phase AND no recent commits.
    #    The commit-age guard prevents a project that's actively being coded
    #    from being labelled "planning" just because its phase name says "design".
    #    None (no repo) is treated the same as > 14 days (inactive).
    if _PLANNING.search(status.phase) and (recent_commit_days is None or recent_commit_days > 14):
        return "planning"

    # 5. writing — default: active midflight work that didn't match above.
    return "writing"


def owner(status: StatusBlock | None, bucket_name: str) -> str:
    """Return whose move is next: 'claude' | 'you' | 'none'.

    Receives:
        status      — parsed StatusBlock, or None.
        bucket_name — the bucket string returned by bucket() for this project.
                      Passed in rather than re-derived so owner() stays pure
                      and avoids duplicating the bucket logic.

    Returns:
        'claude' — the next action is mine (implement, write, etc.)
        'you'    — the next action requires the user (provide files, decide, test
                   on hardware, approve/merge, etc.)
        'none'   — no active owner: project is finished, or next is empty/quiescent.

    Decision order (first match wins):
        1. No status block → 'claude' (I need to write/update the Status block).
        2. Finished bucket → 'none' (work is complete).
        3. Blocked field contains a user-action phrase → 'you'.
        4. Next field contains a user-action phrase → 'you'.
        5. Next is non-empty and no user-action phrase → 'claude'.
        6. Next is empty → 'none' (quiescent; nobody's active move).

    Reference: spec §7.2.
    """
    # A missing Status block is always my action — I should add/update it.
    if status is None:
        return "claude"

    # Finished projects have no active owner.
    if bucket_name == "finished":
        return "none"

    # If blocked is non-trivial and calls for the user's input, it's their turn.
    # We check _is_nothing first to avoid searching "nothing" for user-action patterns
    # (minor efficiency win, but also makes the intent explicit).
    if not _is_nothing(status.blocked) and _USER_ACTION.search(status.blocked):
        return "you"

    # If the next action requires the user (e.g. "awaiting files from you"),
    # it's their move regardless of whether blocked is set.
    if _USER_ACTION.search(status.next):
        return "you"

    # Next is non-empty and no user-action trigger → it's my action.
    # Next is empty → project is quiescent, nobody's active move.
    return "claude" if status.next.strip() else "none"
