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
UNIT="$HOME/.config/systemd/user/project-board-scan.service"

# Bake this repo's ACTUAL path into the service unit. The shipped unit defaults its
# paths to ~/Claude/project-board, which is only correct if you cloned there; without
# this rewrite a clone elsewhere would point the timer at a missing scan.py. The
# substitution is anchored to the WorkingDirectory/ExecStart lines (BRE capture groups)
# so it rewrites ONLY the path values, not the comments that mention the same default
# path; '#' delimiter keeps the slash-heavy $REPO safe.
sed -e "s#^\(WorkingDirectory=\)%h/Claude/project-board#\1${REPO}#" \
    -e "s#^\(ExecStart=/usr/bin/python3 \)%h/Claude/project-board#\1${REPO}#" \
    "$REPO/systemd/project-board-scan.service" > "$UNIT"

# systemd --user units do NOT inherit your interactive shell env, so to point the 15-min
# timer (not just a one-off `PROJECT_BOARD_ROOT=… scan.py`) at a non-default projects
# root, set PROJECT_BOARD_ROOT when running this installer and we persist it into the
# unit. Reject a non-existent root (a typo would otherwise scan nothing forever), and
# QUOTE the value so a path with spaces survives systemd's Environment= parser (which
# splits unquoted values on whitespace).
if [[ -n "${PROJECT_BOARD_ROOT:-}" ]]; then
    if [[ ! -d "$PROJECT_BOARD_ROOT" ]]; then
        echo "  ! PROJECT_BOARD_ROOT='$PROJECT_BOARD_ROOT' is not a directory — refusing to bake a dead path into the timer." >&2
        exit 1
    fi
    sed -i "/^\[Service\]/a Environment=\"PROJECT_BOARD_ROOT=${PROJECT_BOARD_ROOT}\"" "$UNIT"
fi
cp "$REPO/systemd/project-board-scan.timer" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload

# Prove the generated unit actually RUNS before enabling the timer. daemon-reload does
# NOT fail on a malformed unit, and the oneshot service wouldn't run until the first
# 15-min tick — so a unit broken by an odd clone path (e.g. a space in it) would fail
# SILENTLY every tick while this installer printed all-green. `systemctl start` on a
# oneshot runs it synchronously and returns the service's result, turning that into a
# loud install-time error. (This run also seeds board.json through the real timer env.)
if ! systemctl --user start project-board-scan.service; then
    echo "  ! the scanner failed when run through systemd — NOT enabling a silently-broken timer:" >&2
    systemctl --user status project-board-scan.service --no-pager -n 20 >&2 || true
    exit 1
fi
systemctl --user enable --now project-board-scan.timer
echo "  ✓ timer enabled and verified (project-board-scan.timer)"

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
# into grep, which some grep wrappers mishandle on piped stdin.
if [[ "$(kpackagetool6 --type Plasma/Applet --list 2>/dev/null)" == *org.projectboard* ]]; then
    kpackagetool6 --type Plasma/Applet --upgrade "$REPO/plasmoid/org.projectboard"
    echo "  ✓ plasmoid upgraded"
else
    kpackagetool6 --type Plasma/Applet --install "$REPO/plasmoid/org.projectboard"
    echo "  ✓ plasmoid installed"
fi

echo
echo "Done. Add it to your desktop: right-click the desktop → Add Widgets → search 'Project Board'."
