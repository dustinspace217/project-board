"""Parse a plan-doc '## Status' block into structured fields.

Convention (workspace CLAUDE.md):
    ## Status (updated YYYY-MM-DD)
    Phase: ...
    Done: ...            (may wrap onto continuation lines)
    Next: ...
    Blocked: ...

Returns None when no block is present so callers can flag needs_status.
This module has no I/O — it operates on text already loaded by the caller,
keeping it fast and trivially testable.
"""
from __future__ import annotations

# 'annotations' postpones evaluation of type hints, so 'StatusBlock | None'
# works as a return type even on Python 3.9 (we target 3.12, but it's good practice).
import datetime as dt
import re
from dataclasses import dataclass

# _HEADER matches the opening '## Status' line of a Status block.
# We keep this simple — just detect the heading — rather than trying to capture
# the date in the same pattern. A regex like '.*?(?:updated ...)?' suffers a
# backtracking trap: the non-greedy '.*?' matches zero chars and the optional
# group is satisfied vacuously, so the date is never captured.
_HEADER = re.compile(r"^##\s+Status\b", re.IGNORECASE)

# _DATE is applied separately to the same header line to extract the ISO date,
# e.g. "## Status (updated 2026-06-10)". Two-step beats one-regex here:
# the intent is clearer and the backtracking trap is gone.
_DATE = re.compile(r"updated\s+(\d{4}-\d{2}-\d{2})", re.IGNORECASE)

# _FIELD matches a named key–value line such as "Phase: Complete — shipped".
# Group 1 = field name (Phase/Done/Next/Blocked), group 2 = value after the colon.
_FIELD = re.compile(r"^(Phase|Done|Next|Blocked)\s*:\s*(.*)$", re.IGNORECASE)


@dataclass(frozen=True)
class StatusBlock:
    """Structured representation of a single ## Status block.

    All string fields are stripped of leading/trailing whitespace.
    Multiline fields (most commonly 'done') are joined with a single space.

    Attributes:
        phase:   The 'Phase:' line value — current phase name or completion state.
        done:    The 'Done:' line value — summary of recently completed work.
        next:    The 'Next:' line value — the immediate next action.
        blocked: The 'Blocked:' line value — blocking dependencies, or 'nothing'.
        updated: Date from the '(updated YYYY-MM-DD)' suffix, or None if absent.
        raw:     The raw text of the block including the header line, for debugging.
    """

    phase: str
    done: str
    next: str
    blocked: str
    updated: dt.date | None
    raw: str


def parse_status_block(text: str) -> StatusBlock | None:
    """Return the FIRST '## Status' block found in `text`, or None if absent.

    Receives:
        text — full text of a plan doc, read by the caller from disk.

    Returns:
        A StatusBlock dataclass, or None when no Status heading exists.

    Design notes:
    - The outer loop is bounded by the line count of `text` (Power of Ten rule 2).
    - The inner loop likewise terminates when it hits a blank line or the next
      heading ('## '), which is the conventional end of a Status block.
    - We return on the FIRST match; plan docs have at most one Status block,
      but returning early keeps complexity O(n) with a small constant.
    """
    lines = text.splitlines()

    # Outer loop: scan forward for the Status heading.
    # Bounded above by len(lines).
    for i, line in enumerate(lines):
        if not _HEADER.match(line.strip()):
            continue

        # Parse the optional date from the header line (e.g. '(updated 2026-06-10)').
        # _DATE.search is used here rather than a combined regex — see _DATE note above.
        dm = _DATE.search(line)
        if dm:
            try:
                updated = dt.date.fromisoformat(dm.group(1))
            except ValueError:
                # Shape matched (\d{4}-\d{2}-\d{2}) but it's not a real calendar date
                # (e.g. 2026-13-45 typo). Degrade this one field to None rather than
                # raising and aborting the entire scan — matches the module's
                # degrade-not-crash posture.
                updated = None
        else:
            updated = None

        # Accumulate the four known fields into a dict keyed by lowercase name.
        # Using a dict instead of four separate variables lets us handle the
        # 'current field' pointer cleanly without branching on every line.
        fields: dict[str, str] = {"phase": "", "done": "", "next": "", "blocked": ""}
        block = [line]
        current: str | None = None  # which field we're currently appending to

        # Inner loop: consume lines until the block ends.
        # Bounded above by len(lines) - i - 1.
        for follow in lines[i + 1 :]:
            # A blank line or a new '## ' heading ends the Status block.
            if follow.strip() == "" or follow.startswith("## "):
                break
            block.append(follow)
            fm = _FIELD.match(follow.strip())
            if fm:
                # New named field — record it and update the 'current' pointer.
                current = fm.group(1).lower()
                fields[current] = fm.group(2).strip()
            elif current:
                # Continuation line: append to whichever field we're in.
                # A single-space join preserves readability without adding newlines.
                fields[current] = (fields[current] + " " + follow.strip()).strip()

        return StatusBlock(updated=updated, raw="\n".join(block), **fields)

    return None
