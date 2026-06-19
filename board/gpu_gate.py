"""Detect whether the GPU is busy (gaming / GPU-heavy apps / any heavy CUDA work), so the
scanner can SKIP its local-LLM classification and not contend with what the user is doing
on the GPU.

Signals (both reliable): nvidia-smi GPU utilization + free-VRAM headroom. We deliberately
do NOT use GameMode process detection — `gamemoded` reports 'active' whenever the daemon
is running, even when no game is engaged, so it's a false-positive machine. Utilization
goes high (>50%) during a game or a GPU-heavy app run and sits near idle (~4%) otherwise.
"""
from __future__ import annotations

import subprocess
import time


def _sample_gpu() -> tuple[int, int, int] | None | str:
    """One nvidia-smi reading. Returns:
      (util_percent, mem_used_MiB, mem_total_MiB) — a good reading;
      None  — nvidia-smi is ABSENT (no NVIDIA GPU / CPU-only box): nothing to contend with;
      "error" — nvidia-smi is PRESENT but the reading failed (timeout, nonzero exit, driver
                reload, unparseable output). The distinction drives fail-OPEN vs fail-CLOSED.
    """
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        )
    except FileNotFoundError:
        return None                       # no nvidia-smi -> no NVIDIA GPU to contend with
    except (subprocess.SubprocessError, OSError):
        return "error"                    # present but failed this time
    rows = proc.stdout.strip().splitlines()
    if not rows:
        return "error"
    try:
        util, used, total = (int(x.strip()) for x in rows[0].split(","))
    except ValueError:
        return "error"                    # unexpected output shape
    return util, used, total


def gpu_is_busy(util_threshold: int = 35, min_free_mib: int = 6000, samples: int = 3) -> bool:
    """True if the GPU looks busy enough that loading+running the model would contend.

    busy == sustained utilization >= util_threshold (peak across `samples` reads),
            OR free VRAM < min_free_mib (not enough headroom to load the ~5 GB model
            alongside whatever is already resident — e.g. a game's textures).
    Failure directions differ on purpose: nvidia-smi ABSENT -> False (a CPU-only box can't
    contend, so run freely); nvidia-smi PRESENT-but-unreadable -> True (fail CLOSED — on a
    real NVIDIA rig a transient read failure shouldn't license hammering a possibly-busy GPU;
    skipping one scan and carrying forward is cheap). The sample loop is bounded by `samples`.
    """
    peak_util = 0
    last_mem: tuple[int, int] | None = None
    for i in range(max(1, samples)):
        s = _sample_gpu()
        if s is None:
            return False        # absent GPU -> nothing to contend with
        if isinstance(s, str):
            return True         # present but unreadable -> fail closed
        util, used, total = s
        peak_util = max(peak_util, util)
        last_mem = (used, total)
        if i < samples - 1:
            time.sleep(0.3)  # space the samples so a game's per-frame dips don't read as idle
    if last_mem is not None and (last_mem[1] - last_mem[0]) < min_free_mib:
        return True
    return peak_util >= util_threshold
