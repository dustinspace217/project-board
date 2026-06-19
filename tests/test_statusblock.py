# tests/test_statusblock.py
# Tests for board.statusblock.parse_status_block.
# Three cases from the plan: happy-path field parsing, multiline continuation,
# and the None-on-missing-block guard.
import datetime as dt

from board.statusblock import parse_status_block

# A realistic plan-doc excerpt that exercises every field plus a multiline 'Done'.
# The blank line after 'Blocked' terminates the block; '## Next section' starts a
# new heading, also confirming the parser stops before it.
SAMPLE = """# Plan
## Status (updated 2026-06-10)
Phase: Complete — pushed to production
Done: page shipped; QA review complete (#92,
synthesis #99, issues #93-#98)
Next: open issues #94-#98
Blocked: nothing

## Next section
body
"""


def test_parses_fields_and_date() -> None:
    """All four named fields and the date are extracted from a well-formed block."""
    s = parse_status_block(SAMPLE)
    assert s is not None
    assert s.phase == "Complete — pushed to production"
    assert s.next == "open issues #94-#98"
    assert s.blocked == "nothing"
    assert s.updated == dt.date(2026, 6, 10)


def test_multiline_field_is_joined() -> None:
    """A 'Done' value that wraps to a second line is joined into a single string."""
    s = parse_status_block(SAMPLE)
    assert s is not None
    # Both the first-line content and the continuation are present.
    assert "page shipped" in s.done and "synthesis #99" in s.done


def test_no_status_block_returns_none() -> None:
    """Files with no '## Status' heading return None so callers can flag needs_status."""
    assert parse_status_block("# Plan\njust prose\n") is None


def test_invalid_date_does_not_crash() -> None:
    """A date that matches the YYYY-MM-DD shape but isn't a real calendar date
    (e.g. month=13) must degrade to updated=None rather than raising ValueError
    and aborting the scan.  The rest of the StatusBlock must still be populated."""
    text = (
        "## Status (updated 2026-13-45)\n"
        "Phase: Phase 2\n"
        "Done: some work\n"
        "Next: next step\n"
        "Blocked: nothing\n"
    )
    s = parse_status_block(text)
    assert s is not None, "parse_status_block raised or returned None for invalid date"
    assert s.updated is None, "invalid date should degrade to None, not raise"
    assert s.phase == "Phase 2"  # other fields must still be parsed


def test_multiline_no_embedded_newline() -> None:
    """A multiline Done field joined with spaces must contain no embedded newline,
    and the continuation text must be present in the result."""
    text = (
        "## Status (updated 2026-06-10)\n"
        "Phase: Phase 3\n"
        "Done: first line of done\n"
        "  second line continuation\n"
        "Next: next step\n"
        "Blocked: nothing\n"
    )
    s = parse_status_block(text)
    assert s is not None
    # The continuation content must appear in the joined field.
    assert "second line continuation" in s.done
    # The join must use spaces, not newlines — a newline here would break
    # downstream single-line displays and keyword searching.
    assert "\n" not in s.done
