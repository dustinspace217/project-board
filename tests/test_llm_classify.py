# tests/test_llm_classify.py
"""Tests for board/llm_classify.py — the Ollama call + output validation.

Hermetic: urllib.request.urlopen is monkeypatched, so no Ollama is needed. We exercise
the envelope double-parse, the bucket/owner validation, truncation, and the error paths.
"""
import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping

import pytest

from board import llm_classify


class _FakeResp:
    """Minimal stand-in for the urlopen context manager: .read() + with-statement."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _urlopen_returning(model_json: Mapping[str, object]) -> Callable[..., _FakeResp]:
    """A fake urlopen whose body is the Ollama envelope {"response": "<json text>"}."""
    body = json.dumps({"response": json.dumps(model_json)}).encode()

    def _open(_req: object, timeout: float | None = None) -> _FakeResp:
        return _FakeResp(body)

    return _open


def test_classify_valid_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """A well-formed model response is parsed straight through."""
    out = {"bucket": "testing", "owner": "you", "next": "confirm", "blocked": "nothing"}
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_returning(out))
    assert llm_classify.classify("some turns") == out


def test_classify_invalid_bucket_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """An out-of-vocabulary bucket is rejected (caller falls back), never trusted."""
    out = {"bucket": "banana", "owner": "you", "next": "x", "blocked": "y"}
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_returning(out))
    assert llm_classify.classify("turns") is None


def test_classify_invalid_owner_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """An out-of-vocabulary owner is rejected."""
    out = {"bucket": "writing", "owner": "everyone", "next": "x", "blocked": "y"}
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_returning(out))
    assert llm_classify.classify("turns") is None


def test_classify_network_error_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transport error (Ollama down) degrades to None, not a crash."""
    def _boom(_req: object, timeout: float | None = None) -> _FakeResp:
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    assert llm_classify.classify("turns") is None


def test_classify_empty_input_skips_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """No turns and no status block -> None WITHOUT hitting the network."""
    def _must_not_call(_req: object, timeout: float | None = None) -> _FakeResp:
        raise AssertionError("urlopen must not be called for empty input")
    monkeypatch.setattr(urllib.request, "urlopen", _must_not_call)
    assert llm_classify.classify("", None) is None


def test_classify_truncates_next_and_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """next/blocked are coerced to str and capped at 200 chars (defends board.json)."""
    out = {"bucket": "writing", "owner": "claude", "next": "x" * 300, "blocked": "y" * 300}
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_returning(out))
    r = llm_classify.classify("turns")
    assert r is not None
    assert len(r["next"]) == 200
    assert len(r["blocked"]) == 200
