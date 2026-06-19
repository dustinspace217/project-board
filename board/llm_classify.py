"""Classify a project's state by having a LOCAL model (Ollama) interpret its recent
session turns (+ the Status block if one exists).

Uses the Ollama HTTP API with format:json (guarantees clean JSON — no CLI/ANSI junk) and
keep_alive:30s (one scan's sequential calls reuse the loaded model; it self-evicts from
VRAM shortly after). GPU-gating is NOT done here — the caller (scan.py) checks gpu_is_busy()
ONCE per scan before loading the model, so the model's own GPU usage during a scan can't
trip the gate (which was making most cards fall back)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

MODEL = "qwen2.5:7b"                       # instruct model (NOT the coder variant)
_URL = "http://localhost:11434/api/generate"
_BUCKETS = {"planning", "writing", "QA", "testing", "finished"}
_OWNERS = {"claude", "you", "none"}

# Few-shot prompt (the version that classified cleanly in the spike). The model only ever
# sees this + the project's recent turns; output is constrained to JSON by format:json.
_SYSTEM = """You read the recent turns of a coding session between the user (the human) and an
AI assistant, plus an optional written Status block, and infer the project's CURRENT state.
Reply ONLY with JSON: {"bucket","owner","next","blocked"}.

bucket = the current stage:
- planning: still designing/speccing, little or no code written
- writing: actively implementing or fixing code
- QA: code is written and under review
- testing: code is done; verifying/stabilizing, OR waiting on a manual test/confirmation
- finished: the WHOLE project is shipped/merged/abandoned with nothing more intended. A
  completed PHASE, milestone, or task within an ONGOING project is NOT finished — classify
  the remaining work instead (often planning or writing the next phase). When unsure between
  finished and not-finished, prefer NOT finished; "finished" removes the project from the board.

owner = whose NEXT action it is:
- you: the HUMAN (the user) acts next — confirm, decide, test, approve/merge, provide data, or
  resume a paused project / kick off its next phase
- claude: the AI does the next step (more implementation, review, fixing)
- none: finished or idle, nothing pending

next = <=12 words, the single immediate next step.
blocked = <=12 words, or "nothing".

Examples (your output is ONE JSON object like these):
- waiting on your windowed confirmation before release ->
  {"bucket":"testing","owner":"you","next":"confirm the look","blocked":"your confirmation"}
- the entire project is merged to main and shipped, no further phases ->
  {"bucket":"finished","owner":"none","next":"nothing","blocked":"nothing"}
- "Phase 1 complete, tests green" but the project has later phases ->
  {"bucket":"planning","owner":"you","next":"kick off the next phase","blocked":"nothing"}
- implementing the parser, tests still failing ->
  {"bucket":"writing","owner":"claude","next":"get tests green","blocked":"nothing"}
"""


def classify(turns_text: str | None, status_block: str | None = None,
             timeout: float = 180.0) -> dict[str, str] | None:
    """Return {bucket, owner, next, blocked}, or None.

    None means: no input to classify, Ollama unavailable, or the model returned something
    invalid. The caller decides the fallback; this function never guesses on failure.
    GPU-gating is NOT done here — the caller checks gpu_is_busy() ONCE per scan (before
    loading the model), so the model's own GPU usage during the scan can't trip the gate.
    """
    if not turns_text and not status_block:
        return None

    prompt = _SYSTEM
    if status_block:
        prompt += "\n\nSTATUS BLOCK (a written summary, may be stale):\n" + status_block[:2000]
    prompt += "\n\nCONVERSATION (oldest to newest):\n" + (turns_text or "(no transcript)")[:5000]

    body = json.dumps({
        "model": MODEL, "prompt": prompt, "stream": False,
        # keep_alive "30s": one scan's sequential calls reuse the loaded model (calls are
        # seconds apart), then it self-evicts from VRAM 30s after the last project — quick
        # reclaim without reloading 5GB per project. Paired with the GPU-busy gate, the
        # model is only ever resident while the user is idle on the GPU.
        "format": "json", "keep_alive": "30s", "options": {"temperature": 0},
    }).encode()
    try:
        req = urllib.request.Request(_URL, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read())
        out = json.loads(resp["response"])   # format:json => response is valid JSON text
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return None

    bucket, owner = out.get("bucket"), out.get("owner")
    if bucket not in _BUCKETS or owner not in _OWNERS:
        return None
    return {
        "bucket": bucket,
        "owner": owner,
        "next": str(out.get("next", "")).strip()[:200],
        "blocked": str(out.get("blocked", "")).strip()[:200],
    }
