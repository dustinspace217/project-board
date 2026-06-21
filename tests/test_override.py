# tests/test_override.py
"""Tests for board/override.py + build_card honoring a .board-status pin (hermetic)."""
import datetime as dt
from pathlib import Path

import pytest

from board import llm_classify
from board.build import build_card
from board.override import read_override


def test_read_override_absent(tmp_path: Path) -> None:
    """No .board-status -> empty override (auto-classification)."""
    assert read_override(tmp_path) == {}


def test_read_override_parses_fields(tmp_path: Path) -> None:
    """Recognized keys are parsed; comments and blank lines ignored."""
    (tmp_path / ".board-status").write_text(
        "# a pin\n\nbucket: finished\nowner: none\nnext: nothing\n")
    assert read_override(tmp_path) == {"bucket": "finished", "owner": "none", "next": "nothing"}


def test_read_override_drops_invalid(tmp_path: Path) -> None:
    """An invalid bucket/owner is dropped (a typo must not pin a nonsense state)."""
    (tmp_path / ".board-status").write_text("bucket: banana\nowner: everyone\nnext: real step\n")
    assert read_override(tmp_path) == {"next": "real step"}


def test_build_card_honors_pin(tmp_path: Path) -> None:
    """A pinned bucket wins over classification, defaults finished's owner, labels 'pinned'."""
    proj = tmp_path / "demo"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("x")
    (proj / ".board-status").write_text("bucket: finished\n")
    card = build_card(proj, tmp_path / "sessions", {}, {},
                      dt.date(2026, 6, 19), 5, 14, allow_llm=False)
    assert card is not None
    assert card["bucket"] == "finished"
    assert card["owner"] == "none"
    assert card["classified_by"] == "pinned"


def test_build_card_pin_skips_llm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pinned bucket means the LLM is never called — you've taken manual control."""
    proj = tmp_path / "demo"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("x")
    (proj / ".board-status").write_text("bucket: writing\nnext: keep going\n")

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("the LLM must not be called for a pinned project")

    monkeypatch.setattr(llm_classify, "classify", boom)
    card = build_card(proj, tmp_path / "sessions", {}, {},
                      dt.date(2026, 6, 19), 5, 14, allow_llm=True)
    assert card is not None
    assert card["bucket"] == "writing"
    assert card["next"] == "keep going"
    assert card["classified_by"] == "pinned"


def test_build_card_pins_non_bucket_field(tmp_path: Path) -> None:
    """A pin that sets a field WITHOUT a bucket (e.g. just owner) is applied on top of the
    classification — partial pins must not be silently ignored (QA: code-reviewer #4)."""
    proj = tmp_path / "demo"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("x")
    (proj / ".board-status").write_text("owner: you\nnext: my call\n")
    # No bucket pinned -> the heuristic decides the bucket, but owner/next come from the pin.
    card = build_card(proj, tmp_path / "sessions", {}, {},
                      dt.date(2026, 6, 19), 5, 14, allow_llm=False)
    assert card is not None
    assert card["owner"] == "you"
    assert card["next"] == "my call"
    assert card["classified_by"] == "pinned"
