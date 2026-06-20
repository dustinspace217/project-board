# tests/test_scan.py
"""Tests for scan.py — the CLI entry and build_board_json() orchestrator.

build_board_json() is tested here as a pure function (no disk I/O beyond
the tmp tree). The atomic-write and main() path are exercised by the smoke
test (run manually per the plan), not by these unit tests.
"""
import datetime as dt
from pathlib import Path

from scan import build_board_json


def test_writes_valid_board(tmp_path: Path) -> None:
    """build_board_json returns a valid board dict containing a card for 'demo'."""
    claude_root = tmp_path / "Claude"
    (claude_root / "demo").mkdir(parents=True)
    (claude_root / "demo" / "CLAUDE.md").write_text("x")
    out = build_board_json(
        claude_root,
        tmp_path / "sessions",
        {},  # index (no attribution in unit tests)
        prev=None,
        today=dt.date(2026, 6, 17),
        dropoff_days=5,
        stale_days=14,
        allow_llm=False,  # hermetic: never call Ollama in unit tests
    )
    # Meta block must be present with the expected version.
    assert out["meta"]["scan_version"] == 1
    # The 'demo' project should appear in the cards list.
    assert any(c["name"] == "demo" for c in out["cards"])


def test_board_meta_fields(tmp_path: Path) -> None:
    """Meta block contains all expected keys."""
    claude_root = tmp_path / "Claude"
    claude_root.mkdir()
    out = build_board_json(
        claude_root,
        tmp_path / "sessions",
        {},  # index (no attribution in unit tests)
        prev=None,
        today=dt.date(2026, 6, 17),
        dropoff_days=5,
        stale_days=14,
        allow_llm=False,  # hermetic: never call Ollama in unit tests
    )
    meta = out["meta"]
    assert "generated_at" in meta
    assert meta["dropoff_days"] == 5
    assert meta["stale_days"] == 14


def test_prev_carry_forward(tmp_path: Path) -> None:
    """finished_at from a previous board.json is carried into the new card."""
    claude_root = tmp_path / "Claude"
    proj = claude_root / "myproj"
    (proj / "docs" / "superpowers" / "plans").mkdir(parents=True)
    (proj / "CLAUDE.md").write_text("x")
    (proj / "docs" / "superpowers" / "plans" / "p.md").write_text(
        "## Status (updated 2026-06-16)\n"
        "Phase: Complete — shipped\n"
        "Done: done\n"
        "Next: nothing\n"
        "Blocked: nothing\n"
    )
    # Simulate a prev board.json that already has finished_at set 2 days ago.
    # 2 days < dropoff_days=5, so the card should NOT be dropped.
    prev = {
        "meta": {"scan_version": 1, "generated_at": "2026-06-15T00:00:00",
                 "dropoff_days": 5, "stale_days": 14},
        "cards": [{"name": "myproj", "finished_at": "2026-06-15",
                   "bucket": "finished"}],
    }
    out = build_board_json(
        claude_root,
        tmp_path / "sessions",
        {},  # index (no attribution in unit tests)
        prev=prev,
        today=dt.date(2026, 6, 17),
        dropoff_days=5,
        stale_days=14,
        allow_llm=False,  # hermetic: never call Ollama in unit tests
    )
    cards = {c["name"]: c for c in out["cards"]}
    assert "myproj" in cards
    # finished_at should be the original date, not today.
    assert cards["myproj"]["finished_at"] == "2026-06-15"


def test_empty_root_produces_empty_cards(tmp_path: Path) -> None:
    """An empty ~/Claude root produces a board with zero cards."""
    claude_root = tmp_path / "Claude"
    claude_root.mkdir()
    out = build_board_json(
        claude_root,
        tmp_path / "sessions",
        {},  # index (no attribution in unit tests)
        prev=None,
        today=dt.date(2026, 6, 17),
        dropoff_days=5,
        stale_days=14,
        allow_llm=False,  # hermetic: never call Ollama in unit tests
    )
    assert out["cards"] == []


def test_includes_file_projects(tmp_path: Path) -> None:
    """A loose root-level .md is surfaced as an is_file card alongside directory projects."""
    claude_root = tmp_path / "Claude"
    proj = claude_root / "dirproj"
    proj.mkdir(parents=True)
    (proj / "CLAUDE.md").write_text("x")
    (claude_root / "project-alpha-plan.md").write_text("# plan")
    out = build_board_json(
        claude_root,
        tmp_path / "sessions",
        {},  # index
        prev=None,
        today=dt.date(2026, 6, 17),
        dropoff_days=5,
        stale_days=14,
        allow_llm=False,
    )
    cards = {c["name"]: c for c in out["cards"]}
    assert "dirproj" in cards
    assert cards["dirproj"]["is_file"] is False
    assert "project-alpha-plan" in cards
    assert cards["project-alpha-plan"]["is_file"] is True
