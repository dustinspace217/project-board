"""Manual per-project status override.

A `.board-status` file in a project directory pins card fields the model can't infer —
e.g. "this is done" when the transcript still reads as active, or the inverse. It's the
same idea as `.board-ignore`: hand-editable, lives with the project, and is also what the
plasmoid writes when you drag a card to a different column.

Format (simple `key: value` lines, like a Status block; pin only what you want):

    bucket: finished
    # owner: you
    # next: kick off phase 2

When a `.board-status` pins a bucket, that card is authoritative — the scanner skips LLM
classification for it entirely (pinned == you've taken manual control) and labels it
`pinned` so the UI can badge it. Delete the file to return to auto-classification.
"""
from __future__ import annotations

import sys
from pathlib import Path

# Mirrors the buckets/owners the classifier and LLM use; an invalid pin is ignored rather
# than allowed to set a nonsense state.
_VALID_BUCKETS = {"planning", "writing", "QA", "testing", "finished"}
_VALID_OWNERS = {"claude", "you", "none"}
_FIELDS = ("bucket", "owner", "next", "blocked")

FILENAME = ".board-status"


def read_override(project: Path) -> dict[str, str]:
    """Parse <project>/.board-status into a dict of pinned fields, or {} if absent/empty.

    Recognized keys: bucket, owner, next, blocked. Blank lines and `#` comments are
    ignored; an invalid bucket/owner value is dropped AND announced on stderr (so a typo like
    `bucket: finsihed` doesn't silently no-op — the user, having hand-edited a pin, gets told
    why it didn't take). The loop is bounded by the file's line count.
    """
    f = project / FILENAME
    try:
        text = f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}   # no file = no pin: correct graceful degradation, not an error
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, val = line.partition(":")
        key, val = key.strip().lower(), val.strip()
        if key not in _FIELDS or not val:
            continue
        if key == "bucket" and val not in _VALID_BUCKETS:
            print(f"project-board: {f}: ignoring invalid bucket {val!r} "
                  f"(expected one of {sorted(_VALID_BUCKETS)})", file=sys.stderr)
            continue
        if key == "owner" and val not in _VALID_OWNERS:
            print(f"project-board: {f}: ignoring invalid owner {val!r} "
                  f"(expected one of {sorted(_VALID_OWNERS)})", file=sys.stderr)
            continue
        out[key] = val
    return out
