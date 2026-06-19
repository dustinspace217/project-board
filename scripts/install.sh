#!/usr/bin/env bash
# install.sh — one-time setup for the Project Board on this machine.
# No sudo: everything runs at the systemd --user / per-user level.
#
#   1. Installs the systemd --user timer that refreshes board.json every 15 min.
#   2. Seeds board.json immediately so the widget has data on first show.
#   3. Installs (or upgrades) the Plasma plasmoid package.
#
# After running, add the widget from the desktop's "Add Widgets" panel.
set -euo pipefail

# Resolve the repo root from this script's location (scripts/ -> repo root).
REPO="$(cd "$(dirname "$0")/.." && pwd)"

# --- 1. systemd --user timer ---
mkdir -p "$HOME/.config/systemd/user" "$HOME/.local/share/project-board"
cp "$REPO/systemd/project-board-scan.service" "$HOME/.config/systemd/user/"
cp "$REPO/systemd/project-board-scan.timer"   "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable --now project-board-scan.timer
echo "  ✓ timer enabled (project-board-scan.timer)"

# --- 2. Seed board.json now (so the widget isn't empty before the first timer tick) ---
# An `if` condition is exempt from `set -e`, so a transient scan failure here does
# NOT abort the install (the plasmoid still gets installed; the timer will retry).
if python3 "$REPO/scan.py"; then
    echo "  ✓ board.json seeded"
else
    echo "  ! seed scan failed — installing anyway; the timer will retry every 15 min"
fi

# --- 3. Plasma plasmoid (upgrade if already installed, else install) ---
# Branch on whether the package is present so a GENUINE upgrade error surfaces
# instead of being masked as "not installed". The [[ == *..* ]] test avoids piping
# into grep (some grep builds are wrapper-shimmed and mishandle piped stdin).
if [[ "$(kpackagetool6 --type Plasma/Applet --list 2>/dev/null)" == *org.projectboard* ]]; then
    kpackagetool6 --type Plasma/Applet --upgrade "$REPO/plasmoid/org.projectboard"
    echo "  ✓ plasmoid upgraded"
else
    kpackagetool6 --type Plasma/Applet --install "$REPO/plasmoid/org.projectboard"
    echo "  ✓ plasmoid installed"
fi

echo
echo "Done. Add it to your desktop: right-click the desktop → Add Widgets → search 'Project Board'."
