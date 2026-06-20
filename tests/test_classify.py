# tests/test_classify.py
# Tests for board/classify.py — bucket() and owner() pure classification logic.
# Each test pins one rule or branch so a wrong keyword/regex fix is immediately
# visible. The helper sb() keeps tests short without hiding the field being tested.

from board.classify import bucket
from board.statusblock import StatusBlock


def sb(phase: str = "", done: str = "", nxt: str = "", blocked: str = "nothing") -> StatusBlock:
    """Build a minimal StatusBlock for testing.

    Most tests only care about one or two fields; the rest default to empty/nothing.
    'nxt' aliases 'next' because 'next' is a Python builtin — safe to shadow but
    the alias keeps the call sites more readable.
    """
    return StatusBlock(phase=phase, done=done, next=nxt, blocked=blocked, updated=None, raw="")


# ---------------------------------------------------------------------------
# Task 2: bucket() tests
# ---------------------------------------------------------------------------


def test_finished_requires_terminal_phase_and_clear() -> None:
    # "deployed" in phase triggers _FINISHED; blocked is "nothing"; next is empty.
    # All three gates must pass for "finished".
    assert bucket(sb(phase="Complete — deployed", nxt="", blocked="nothing"), None) == "finished"


def test_finished_not_triggered_if_next_open() -> None:
    # Even though phase says "Complete", an open next item keeps it out of finished.
    # Verifies that _next_clear() is doing its job.
    assert bucket(sb(phase="Complete", nxt="add feature X"), None) != "finished"


def test_qa_from_review_keyword() -> None:
    # "QA" appears in next → blob contains it → _QA pattern fires before testing/planning.
    assert bucket(sb(phase="Phase 3", nxt="run QA review"), None) == "QA"


def test_testing_keyword() -> None:
    # "stabiliz" (partial) in next matches _TESTING; QA keywords absent so QA doesn't win.
    assert bucket(sb(phase="Phase 4", nxt="stabilize test suite"), None) == "testing"


def test_planning_when_spec_phase_no_commits() -> None:
    # "Design" in phase matches _PLANNING; recent_commit_days=None (no git) satisfies
    # the commit-age guard (None is treated as > 14 days = inactive).
    assert (
        bucket(sb(phase="Design (spec)", nxt="write plan"), recent_commit_days=None) == "planning"
    )


def test_writing_is_default_midflight() -> None:
    # "Phase 2 of 7" has no terminal/QA/testing/planning keywords → falls through to "writing".
    # recent_commit_days=1 means active work is happening.
    assert (
        bucket(sb(phase="Phase 2 of 7", nxt="implement parser"), recent_commit_days=1) == "writing"
    )


def test_no_status_falls_back_to_git_age() -> None:
    # No StatusBlock at all: recent commit → "writing"; no commits ever → "planning".
    assert bucket(None, recent_commit_days=2) == "writing"
    assert bucket(None, recent_commit_days=None) == "planning"


# ---------------------------------------------------------------------------
# Task 3: owner() tests  (import added here so Task-2-only runs still work)
# ---------------------------------------------------------------------------

from board.classify import owner  # noqa: E402  (below the Task-2 block intentionally)


def test_owner_blocked_on_user() -> None:
    # "user will provide" in blocked matches _USER_ACTION → ball is in the user's court.
    assert owner(sb(blocked="user will provide the data"), "writing") == "you"


def test_owner_next_is_claude_action() -> None:
    # "implement" is a Claude action; blocked is "nothing"; next is non-empty.
    assert owner(sb(nxt="implement the parser", blocked="nothing"), "writing") == "claude"


def test_owner_finished_is_none() -> None:
    # Finished projects have no active owner — the work is done.
    assert owner(sb(phase="Complete"), "finished") == "none"


def test_owner_missing_status_is_claude() -> None:
    # Missing status block = my action: go write/update a Status block.
    assert owner(None, "planning") == "claude"


def test_owner_no_next_is_none() -> None:
    # next is empty and blocked is clear → nobody's move, project is quiescent.
    assert owner(sb(nxt="", blocked="nothing"), "writing") == "none"


# ---------------------------------------------------------------------------
# Fix 2: _QA/_TESTING must NOT match keywords that only appear in done/blocked
# ---------------------------------------------------------------------------


def test_qa_keyword_in_done_does_not_fire() -> None:
    """'QA review complete' only in status.done should NOT pull bucket into QA.

    done describes COMPLETED work — a finished QA pass means the project is
    past QA, not in it. Only phase and next are searched for QA signals.
    """
    # Phase and next are generic; only done contains the QA keyword.
    s = sb(phase="Phase 2", nxt="implement parser", done="QA review complete", blocked="nothing")
    assert bucket(s, recent_commit_days=1) == "writing"


def test_finished_with_qa_keyword_in_done_stays_finished() -> None:
    """A terminal-phase project whose done mentions QA must stay 'finished'.

    This guards the finished-beats-QA precedence when the QA keyword is only
    in done (completed work), not in the active signals (phase/next).
    """
    s = sb(
        phase="Complete — shipped",
        done="ran QA review and tests",
        nxt="nothing",
        blocked="nothing",
    )
    assert bucket(s, recent_commit_days=None) == "finished"


# ---------------------------------------------------------------------------
# Fix 3: _USER_ACTION — name-agnostic "the human acts next" cues
# ---------------------------------------------------------------------------


def test_owner_user_facing_is_not_you() -> None:
    """'user-facing copy' in next must NOT match _USER_ACTION; owner should be 'claude'.

    The regex is name-agnostic and fires only on action cues (provide, decide,
    approve, merge, review, you/your, etc.), never on the bare noun 'user' — so
    a UX task like 'user-facing copy polish' stays a Claude action.
    """
    assert owner(sb(nxt="user-facing copy polish", blocked="nothing"), "writing") == "claude"


def test_owner_provide_cue_is_you() -> None:
    """A 'provide' cue in next must match _USER_ACTION and set owner to 'you'."""
    assert owner(sb(nxt="provide the source files", blocked="nothing"), "writing") == "you"


def test_owner_you_cue_is_you() -> None:
    """A direct 'you'/'your' cue in next is a user-action signal → owner 'you'."""
    assert owner(sb(nxt="awaiting your decision on the design", blocked="nothing"),
                 "writing") == "you"


def test_owner_approve_merge_cue_is_you() -> None:
    """Human-owner verbs like approve/merge/review in next → owner 'you'."""
    assert owner(sb(nxt="approve and merge the PR", blocked="nothing"), "writing") == "you"


def test_owner_hardware_cue_is_you() -> None:
    """A 'test on hardware' context only the user can supply → owner 'you'."""
    assert owner(sb(nxt="test on the physical hardware", blocked="nothing"), "writing") == "you"
