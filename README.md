# Project Board

A self-maintaining Kanban board of your Claude Code projects, rendered as a KDE
Plasma 6 desktop widget. A small Python scanner walks your projects directory,
figures out what state each project is in, and writes a `board.json` that the
widget displays. It runs on a timer in the background, so the board stays current
without you touching it.

Each project lands in one of five buckets:

| Bucket | Meaning |
|--------|---------|
| **planning** | Still designing or speccing; little or no code written yet. |
| **writing** | Actively implementing or fixing code. |
| **QA** | Code is written and under review. |
| **testing** | Code is done; verifying, stabilizing, or waiting on a manual test/confirmation. |
| **finished** | The whole project is shipped, merged, or abandoned — nothing more intended. |

Each card also shows whose move is next (you or the AI), the immediate next step,
anything that's blocking it, when it was last touched, and a `claude --resume`
command to jump back into the relevant session.

## How it works

The pipeline is a single Python scanner plus a Plasma widget that reads its output:

1. **Enumerate projects.** The scanner looks at the immediate subdirectories of
   your projects root (default `~/Claude`). A directory counts as a project if it
   has a `CLAUDE.md`, a `.git/` directory, or a plan doc under
   `docs/superpowers/plans/`. Drop a `.board-ignore` file into a directory to
   exclude it.

2. **Attribute sessions to projects.** Claude Code stores session transcripts
   under `~/.claude/projects`. You often work on several projects from one session
   started at your projects root, so a project's own session folder may hold only
   stray command-spawns rather than the real work. The scanner reads the
   transcripts, counts how often each `<root>/<project>` path is mentioned, and
   attributes each session to the project that dominates it. The result is cached
   to an index so each scan only re-reads transcripts that actually changed.

3. **Classify state.** For each project, the most relevant recent session turns
   (plus the project's written `## Status` block, if it has one) are handed to a
   **local** LLM via [Ollama](https://ollama.com) — `qwen2.5:7b` by default. The
   model returns the bucket, the owner of the next action, the next step, and what's
   blocked, as constrained JSON. Nothing leaves your machine.

4. **GPU gate.** Before loading the model, the scanner checks GPU utilization and
   free VRAM via `nvidia-smi`. If the GPU is busy — you're gaming, running an ML
   job, or doing heavy rendering — it skips LLM classification entirely for that
   scan and carries the previous results forward, so it never fights your other
   work for the GPU. On a machine with no NVIDIA GPU, it simply runs the model
   freely. If `nvidia-smi` is present but unreadable, it fails closed (skips the
   scan) to be safe.

5. **Heuristic fallback.** If Ollama is unavailable, the model isn't pulled, or it
   returns something invalid, the scanner falls back to a deterministic
   keyword-based classifier that reads the project's `## Status` block and git
   activity. Every card records *how* it was classified (`llm`, `carried`, `gated`,
   `stale`, or `heuristic`) so a silent model outage stays visible instead of
   producing wrong data quietly.

6. **Write `board.json`.** The result is written atomically (to a temp sibling,
   then renamed over the real file) so the widget never reads a half-written file.
   Finished projects stay on the board for a few days, then drop off. Long-quiet
   projects are flagged stale. The file is written owner-only (`0600`) because it
   contains transcript-derived text.

7. **Display.** The Plasma 6 widget polls `board.json` and renders the cards,
   grouped and sorted so the projects that need attention float to the top.

The scanner is pure Python standard library — no third-party packages, no daemon.

## Requirements

- **Python 3** (standard library only — nothing to `pip install`).
- **[Ollama](https://ollama.com)** with a small instruct model pulled. The default
  is `qwen2.5:7b` (note: the *instruct* variant, not the coder one):

  ```
  ollama pull qwen2.5:7b
  ```

  Ollama must be reachable at its default `http://localhost:11434`. Without it, the
  scanner still works via the heuristic fallback — you just get coarser
  classifications.
- **NVIDIA GPU (optional).** Only used for the GPU-busy gate via `nvidia-smi`. On a
  CPU-only or non-NVIDIA box the gate is a no-op and the scanner runs the model
  freely.
- **KDE Plasma 6** for the desktop widget. The scanner alone runs anywhere Python
  and Ollama do; only the widget needs Plasma.

## Install

Run the scanner once by hand to make sure it works and to seed the first
`board.json`:

```
python3 scan.py
```

That writes `~/.local/share/project-board/board.json` and prints how many cards it
produced.

To set up the background timer and install the widget, run the helper script (no
`sudo` — everything is per-user):

```
scripts/install.sh
```

It does three things:

1. Installs and enables a **systemd `--user` timer** that re-scans every 15
   minutes (`project-board-scan.timer`).
2. Seeds `board.json` immediately so the widget has data on first show.
3. Installs (or upgrades) the **Plasma widget** package with `kpackagetool6`.

You can also do those steps manually:

```
# Timer
cp systemd/project-board-scan.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now project-board-scan.timer

# Widget
kpackagetool6 --type Plasma/Applet --install plasmoid/org.projectboard
```

Then add the widget to your desktop or panel: right-click → **Add Widgets** →
search for **Project Board**.

## Configuration

| Setting | Default | Notes |
|---------|---------|-------|
| `PROJECT_BOARD_ROOT` (env var) | `~/Claude` | The projects directory to scan. |
| Model | `qwen2.5:7b` | Set in `board/llm_classify.py` (`MODEL`). |
| Ollama URL | `http://localhost:11434` | Set in `board/llm_classify.py`. |
| Scan interval | 15 min | `OnUnitActiveSec` in `systemd/project-board-scan.timer`. |
| Drop-off window | 5 days | How long finished projects stay on the board (`scan.py`). |
| Stale threshold | 14 days | Inactivity before a card is flagged stale (`scan.py`). |
| GPU gate | util ≥ 35% or < 6 GB free VRAM | `board/gpu_gate.py`. |

To scan a different projects directory, set the environment variable before
running:

```
PROJECT_BOARD_ROOT=/path/to/your/projects python3 scan.py
```

(If you change it, update `PROJECT_BOARD_ROOT` in the systemd service environment
too, or the timer-driven scans will keep using the default.)

## Output

`board.json` lives at `~/.local/share/project-board/board.json`. It has a `meta`
block (generation time, schema version, drop-off and stale windows) and a `cards`
array — one card per project with its bucket, owner, next step, blocker,
last-touched timestamp, resume command, and how it was classified.

## Layout

```
scan.py                  # entry point: enumerate → attribute → classify → write
board/
  enumerate.py           # find project directories under the root
  attribution.py         # map session transcripts to projects
  transcript.py          # pull recent turns out of a session
  statusblock.py         # parse a project's ## Status block
  llm_classify.py        # local Ollama classification
  classify.py            # deterministic heuristic fallback
  signals.py             # git/mtime activity signals
  gpu_gate.py            # nvidia-smi GPU-busy check
  build.py               # assemble one card per project
systemd/                 # --user service + timer
scripts/install.sh       # one-shot setup
plasmoid/org.projectboard # the KDE Plasma 6 widget
```
