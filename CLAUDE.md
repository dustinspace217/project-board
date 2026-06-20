# project-board — contributor notes

A self-maintaining Kanban board of your Claude Code projects. A pure-stdlib Python
scanner writes `board.json`; a KDE Plasma 6 plasmoid reads and renders it.

## Layout
- `scan.py` — entry point; scans the project root and writes `board.json` atomically.
- `board/` — the scanner package (enumeration, attribution, classification, signals).
- `plasmoid/org.projectboard/` — the Plasma 6 widget.
- `scripts/` — `install.sh` (one-time setup) and `reload-widget.sh` (reload after QML edits).
- `systemd/` — a `--user` timer that re-runs the scan every 15 minutes.
- `tests/` — hermetic pytest suite (no Ollama or GPU needed; runs with `allow_llm=False`).

## Conventions
- Python: tabs are not used here — 4-space indentation, type hints, `from __future__ import annotations`.
- Keep functions small and loops bounded; comment the "why" behind non-obvious choices.
- The scanner must stay side-effect-free except for the single atomic `board.json` write.

## Running
- Scan once: `python3 scan.py` (live classification needs Ollama + `qwen2.5:7b`).
- Tests: `python3 -m pytest -q` (hermetic).
- Lint/type-check: `ruff check .` and `mypy .`.

See `README.md` for the full description, requirements, and install steps.
