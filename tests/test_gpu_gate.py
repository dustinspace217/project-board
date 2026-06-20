# tests/test_gpu_gate.py
"""Tests for board/gpu_gate.py — GPU-busy detection.

We monkeypatch _sample_gpu so the tests are hermetic (no real nvidia-smi) and never
sleep (samples=1 means the inter-sample sleep loop body never runs).
"""
import pytest

from board import gpu_gate


def test_busy_when_utilization_high(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sustained high GPU utilization (a game / a heavy compute app) reads as busy."""
    monkeypatch.setattr(gpu_gate, "_sample_gpu", lambda: (90, 4000, 16000))
    assert gpu_gate.gpu_is_busy(samples=1) is True


def test_busy_when_free_vram_low(monkeypatch: pytest.MonkeyPatch) -> None:
    """Low free VRAM (something big is resident) reads as busy even at low utilization."""
    # used=15000 of 16000 -> only 1000 free, below the 6000 MiB headroom threshold.
    monkeypatch.setattr(gpu_gate, "_sample_gpu", lambda: (5, 15000, 16000))
    assert gpu_gate.gpu_is_busy(samples=1) is True


def test_idle_is_not_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    """An idle desktop (low util, plenty of free VRAM) is safe to run the model."""
    monkeypatch.setattr(gpu_gate, "_sample_gpu", lambda: (3, 3000, 16000))
    assert gpu_gate.gpu_is_busy(samples=1) is False


def test_no_gpu_is_not_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    """nvidia-smi ABSENT (None, no NVIDIA GPU) -> not busy: a CPU box can't contend."""
    monkeypatch.setattr(gpu_gate, "_sample_gpu", lambda: None)
    assert gpu_gate.gpu_is_busy(samples=1) is False


def test_uses_peak_across_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mid-sequence utilization spike reads as busy even if the first/last reads are idle."""
    seq = iter([(5, 3000, 16000), (90, 3000, 16000), (5, 3000, 16000)])
    monkeypatch.setattr(gpu_gate, "_sample_gpu", lambda: next(seq))
    monkeypatch.setattr(gpu_gate.time, "sleep", lambda _s: None)  # don't actually sleep
    assert gpu_gate.gpu_is_busy(samples=3) is True


def test_unreadable_nvidia_smi_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """nvidia-smi PRESENT but unreadable ('error') -> busy: fail CLOSED to protect the GPU."""
    monkeypatch.setattr(gpu_gate, "_sample_gpu", lambda: "error")
    assert gpu_gate.gpu_is_busy(samples=1) is True
