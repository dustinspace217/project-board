# project-board

A self-maintaining Kanban board of your local projects. A scanner (Python,
standard library only) walks a projects root, writes `board.json`, and a Plasma 6
desktop plasmoid (~20 MB) renders it. No server or daemon: a `systemd --user`
timer runs the scan every 15 minutes.

## How it works
For each project directory, the scanner determines a Kanban bucket (e.g. todo /
in-progress / blocked / done), an owner, the next action, and any blocker. Two
classification paths exist:

- **LLM path** — points a local LLM (via Ollama) at recent session transcripts
  and asks it to classify bucket/owner/next/blocked. It is GPU-gated
  (`board/gpu_gate.py`, checked once per scan) so it stays out of the way when the
  GPU is busy, and re-classifies a project only when its session has new activity.
- **Deterministic fallback** (`board/classify.py`) — a keyword heuristic used when
  there is no transcript to read or the LLM path is disabled. The test suite runs
  entirely on this path, so no model is required to run the tests.

## Code layout
- `scan.py` — entry point; walks the projects root and writes `board.json`.
- `board/` — the package: scanning, attribution, classification, and JSON build.
- `plasmoid/org.projectboard/` — the Plasma 6 widget that reads `board.json`.
- `systemd/` — the `--user` service + timer that run the scan periodically.
- `scripts/install.sh` — one-time per-user setup (no sudo).
- `tests/` — pytest suite; runs hermetically with the LLM path disabled.

## Running
- Scanner: `python3 scan.py` (live LLM classification needs Ollama + a local model).
- Tests: `python3 -m pytest -q` (no Ollama needed).
