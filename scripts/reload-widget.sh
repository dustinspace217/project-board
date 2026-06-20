#!/bin/bash
# Reload the Project Board plasmoid after editing its QML.
#
# WHY THIS IS NEEDED: plasmashell compiles and CACHES the widget's QML. The board's
# DATA (board.json) auto-refreshes every 60s on its own, but CODE changes to the widget
# (main.qml) do not take effect until you: re-upgrade the package, clear the QML cache,
# and restart plasmashell. This does all three. (Your desktop will flicker for ~2s as
# plasmashell restarts — that's expected.)
set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)/plasmoid/org.projectboard"
echo "Upgrading package..."
kpackagetool6 --type Plasma/Applet --upgrade "$DIR"
echo "Clearing QML cache..."
rm -rf "$HOME/.cache/plasmashell/qmlcache"
echo "Restarting plasmashell..."
setsid -f plasmashell --replace >/dev/null 2>&1
echo "Done — the Project Board widget reloads in a few seconds."
