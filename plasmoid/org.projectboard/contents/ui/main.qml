// Project Board Plasmoid — Plasma 6 desktop widget that renders board.json
// as a 5-column Kanban view. The scanner (scan.py) writes the JSON; this is
// a thin read-only display layer with no logic of its own.
//
// PLASMA 6 IDIOMS IN USE:
//   - Root element is PlasmoidItem (not a bare Item + Plasmoid attached object)
//   - fullRepresentation is a PLAIN property, not Plasmoid.fullRepresentation
//   - Neither fullRepresentation nor compactRepresentation is wrapped in Component{}
//   - pragma ComponentBehavior: Bound lets Repeater delegates reference outer ids
//     via `required property` without "unqualified access" QML warnings (Qt6 idiom)
//
// FILE WATCHING:
//   A clean FileWatcher QML type is NOT available in this Plasma 6 / Fedora 44 env.
//   Verified by scanning all installed *.qmltypes under /usr/lib64/qt6/qml — no
//   FileWatcher or equivalent public type found in QtCore, org.kde.coreaddons,
//   or Qt.labs.folderlistmodel. Plan Task 8 explicitly permits a Timer fallback.
//   Decision: 60-second polling Timer. Low overhead, simple, no missing import.
//
// CLIPBOARD (copy resume_cmd on card click):
//   QML has no built-in Clipboard object. The standard trick is to use a hidden
//   TextEdit: set its text, call selectAll(), then copy() — copy() reads the
//   TextEdit's selection and puts it on the system clipboard via Qt's clipboard
//   bridge. This works identically on Wayland (via wl_data_device) and X11.
//   The TextEdit must be visible:false but NOT width:0/height:0 or copy() silently
//   fails on some Qt builds — anchoring it to 1x1 px works around that.

pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid       // PlasmoidItem — the Plasma 6 applet root type
import org.kde.kirigami as Kirigami  // Kirigami.Theme (system colors), Units (spacing)
import org.kde.plasma.plasma5support as P5Support  // DataSource (executable engine)
import QtQuick.Controls as QQC  // ScrollView / ScrollBar — per-column scrolling

PlasmoidItem {
    id: root

    // Tell Plasma to always show the full representation (the 5-column board).
    // Without this, Plasma defaults to compact representation (a 68x68 icon) on
    // the desktop and requires the user to click to expand it. A Kanban board
    // is useless as an icon — it needs to be ambient and always visible.
    // `preferredRepresentation` is a property on PlasmoidItem (Plasma 6); setting
    // it to `fullRepresentation` makes the board show inline on the desktop.
    preferredRepresentation: fullRepresentation

    // ---------------------------------------------------------------------------
    // DATA MODEL
    // ---------------------------------------------------------------------------

    // `cards` holds the parsed array from board.json's "cards" field.
    // Starts empty; populated by the DataSource below (cat board.json) every 60s.
    property var cards: []

    // `boardError` is a human-readable message when the file is missing or broken.
    // Empty string = no error.
    property string boardError: ""

    // When false (default), cards flagged `dropped` (finished projects aged past the
    // drop-off window) are hidden so the board stays focused on active work. The "Show all"
    // checkbox flips this true to reveal them — nothing is ever lost, just hidden.
    property bool showAll: false

    // A card is visible in the board when it's not dropped, or when "Show all" is on.
    function cardVisible(card) {
        return root.showAll || card.dropped !== true
    }

    // ---------------------------------------------------------------------------
    // DRAG-TO-RECLASSIFY  (drag a card to another column to pin its bucket)
    // ---------------------------------------------------------------------------
    // Cards live inside per-column layouts, so we don't move the card itself (the layout
    // would fight us, and a moved card gets clipped by its column's ScrollView). Instead a
    // top-level proxy follows the cursor while dragging, the column under the cursor is
    // tracked LIVE (and highlighted) as you drag, and on release we pin the tracked column's
    // bucket by writing the project's .board-status (the same mechanism as a hand-edit).
    property bool dragging: false
    property var  dragCard: null                 // the card object being dragged
    property point dragScenePos: Qt.point(0, 0)  // cursor position in SCENE coordinates

    // The column index (0..4) the cursor is over during a drag, or -1 when outside the
    // board. Updated live as the drag moves and APPLIED on release — this is the drop
    // target, captured while the gesture's coordinates are still valid (see columnIndexAt
    // for why we can't read the pointer position after release). The targeted column
    // highlights its header so you can see where the card will land — the "snap to column"
    // feedback, so you no longer have to drop on an exact spot.
    property int dropIndex: -1

    // Session-only drag overrides (project name -> bucket), applied on top of board.json so a
    // dropped card stays put instantly and doesn't snap back on the next 60 s file re-read,
    // until the scanner picks up the .board-status we wrote. localPinsRev is bumped on every
    // change because mutating a var-map doesn't fire QML change notifications on its own.
    property var localPins: ({})
    property int localPinsRev: 0

    // The bucket a card should display: a live drag override if present, else board.json's.
    function effectiveBucket(card) {
        var p = root.localPins[card.name]
        return p !== undefined ? p : card.bucket
    }

    // Once the scanner has applied a pin (the card reports classified_by "pinned"), board.json
    // is authoritative — drop the transient local override so it can't keep masking a later
    // change (e.g. deleting .board-status to un-pin). Called on every fresh board.json so
    // localPins is a true bridge, not a permanent shadow layer over the scanner's state.
    function reconcileLocalPins() {
        var pins = root.localPins
        var changed = false
        for (var i = 0; i < root.cards.length; i++) {
            var c = root.cards[i]
            if (c.classified_by === "pinned" && pins[c.name] !== undefined) {
                delete pins[c.name]
                changed = true
            }
        }
        if (changed) {
            root.localPins = pins
            root.localPinsRev++
        }
    }

    // Shell-quote a string for a single-quoted argument: wrap in '...' and turn each embedded
    // ' into '\'' (close-quote, backslash-escaped quote, reopen-quote). Plain single-quoting
    // handles spaces but NOT an apostrophe — a directory like "won't-fix" would otherwise
    // break the redirect and write .board-status to the wrong place.
    function shquote(s) {
        return "'" + String(s).replace(/'/g, "'\\''") + "'"
    }

    // Pin a project to `bucket` by writing its .board-status (survives scans/reboots) and
    // recording a local override for instant feedback. File-projects have no directory, so
    // they can't be pinned this way — no-op.
    function setBucket(card, bucket) {
        if (!card || card.is_file === true) return
        if (root.effectiveBucket(card) === bucket) return
        // bucket is one of five clamped keys (safe to inline); the path is shell-escaped
        // against spaces AND apostrophes. pendingName lets pinWriter roll back the optimistic
        // override below if the write actually fails (it checks the exit code).
        pinWriter.pendingName = card.name
        pinWriter.connectSource("printf 'bucket: " + bucket + "\\n' > "
                                + root.shquote(card.path + "/.board-status"))
        var pins = root.localPins
        pins[card.name] = bucket
        root.localPins = pins
        root.localPinsRev++
    }

    // Set by the board RowLayout once it's constructed, so root-level functions can reach
    // its geometry without an unqualified cross-scope id reference.
    property var boardRowItem: null

    // Hit-test a scene position to a column index (0..4), or -1 when it's outside the
    // board. Called LIVE during the drag (from the DragHandler's centroid updates), NOT
    // after release. The previous version read the pointer position in onActiveChanged
    // AFTER the drag ended — but Qt zeroes a pointer handler's centroid when its grab
    // releases, so that post-release read mapped to (0,0), fell outside the board, hit the
    // bounds guard below, and silently cancelled every drop: the card never changed
    // columns. Tracking the target column while the gesture is still live (coordinates
    // valid) and applying it on release fixes the no-transit bug.
    function columnIndexAt(scenePos) {
        var br = root.boardRowItem
        if (!br) return -1
        var local = br.mapFromItem(null, scenePos.x, scenePos.y)
        if (local.x < 0 || local.x > br.width || local.y < 0 || local.y > br.height)
            return -1   // cursor outside the board — no valid target column
        // Five equal columns with `spacing` gaps between them. Divide by (column + gap), not
        // br.width/5, so a drop near a boundary lands in the right column instead of drifting
        // into its neighbour (the naive width/5 ignored the 4 inter-column gaps).
        var sp = Kirigami.Units.smallSpacing
        var colW = (br.width - 4 * sp) / 5
        var idx = Math.floor(local.x / (colW + sp))
        return Math.max(0, Math.min(4, idx))
    }

    // Runs the one-shot `printf > .board-status` write. If the write FAILS (read-only dir,
    // permission, etc.), roll back the optimistic localPins override so the board doesn't keep
    // showing a pin that never persisted — otherwise the card would move now and silently snap
    // back on the next scan, the classic "I dragged it, it later reverted" surprise.
    P5Support.DataSource {
        id: pinWriter
        engine: "executable"
        property string pendingName: ""   // project whose pin this write is for
        onNewData: function(source, data) {
            if (data["exit code"] !== 0 && pinWriter.pendingName !== "") {
                var pins = root.localPins
                delete pins[pinWriter.pendingName]
                root.localPins = pins
                root.localPinsRev++
                console.warn("project-board: failed to write .board-status for "
                             + pinWriter.pendingName + " (exit " + data["exit code"] + ")")
            }
            pinWriter.pendingName = ""
            pinWriter.disconnectSource(source)
        }
    }

    // The well-known output path written by scan.py (matches spec §6).
    // This is a personal single-user tool so hardcoding the absolute path is fine.
    // The canonical path is defined in scan.py main() and never changes.
    //
    // INSTALLER: replace "/home/your-user" below with your own home directory
    // (the absolute path to where scan.py writes board.json). QML cannot expand
    // "~" or "$HOME", so this must be a literal absolute path.
    //
    // NOTE: `import QtCore` + StandardPaths.writableLocation() is the idiomatic
    // approach, but plasmashell caches the compiled QML component type in-process
    // and does not reload it on kpackagetool6 --upgrade without a plasmashell
    // restart. StandardPaths is not a global in the QML context without that
    // import, so we use the hardcoded path to avoid the import dependency.
    // If this widget ever needs to be portable, re-add import QtCore and use:
    //   StandardPaths.writableLocation(StandardPaths.GenericDataLocation)
    //   + "/project-board/board.json"
    readonly property string boardPath:
        "/home/your-user/.local/share/project-board/board.json"

    // ---------------------------------------------------------------------------
    // DATA LOADING  (Plasma DataSource "executable" engine — cat the file)
    // ---------------------------------------------------------------------------
    // WHY NOT XMLHttpRequest: Qt 6 disables file:// reads in QML's XMLHttpRequest
    // by default (it needs the env var QML_XHR_ALLOW_FILE_READ=1, which plasmashell
    // does NOT set). The request is silently refused, so the board never populated
    // ("No board yet" with no error). The Plasma "executable" DataSource has no such
    // restriction: it runs a shell command (`cat board.json`) and hands us stdout,
    // re-running every `interval` ms — one mechanism replacing the old XHR + Timer.

    // parseBoard(): takes the raw board.json text and updates cards / boardError.
    // Sets root.cards to the parsed array on success; on any failure clears cards
    // and sets a human-readable root.boardError.
    function parseBoard(text) {
        if (!text || text.length === 0) {
            root.cards = []
            root.boardError = ""   // empty → the overlay shows the "No board yet" hint
            return
        }
        try {
            var doc = JSON.parse(text)
            // Validate the top-level shape — board.json must have a `cards` array
            if (!doc || !Array.isArray(doc.cards)) {
                root.cards = []
                root.boardError = "board.json has unexpected structure — run scan.py"
                return
            }
            root.cards = doc.cards
            root.boardError = ""   // clear any previous error
            root.reconcileLocalPins()
        } catch (e) {
            root.cards = []
            root.boardError = "board.json is not valid JSON: " + e.message
        }
    }

    P5Support.DataSource {
        id: boardSource
        engine: "executable"
        // Single-quote the path so a future path containing spaces still works.
        connectedSources: ["cat '" + root.boardPath + "'"]
        interval: 60000   // re-read every 60 s (scan.py rewrites at most every 15 min)

        // Fires once on connect (immediate first load) and every `interval` after.
        // `data` is a map with "stdout", "stderr", "exit code". A non-zero exit means
        // cat failed (file missing/unreadable) → show the "No board yet" hint.
        onNewData: function(source, data) {
            var ec = data["exit code"]
            if (ec !== 0) {
                root.cards = []
                root.boardError = ""   // missing file → overlay shows "No board yet"
                return
            }
            root.parseBoard(data["stdout"])
        }
    }

    // ---------------------------------------------------------------------------
    // CLIPBOARD HELPER  (hidden TextEdit trick)
    // ---------------------------------------------------------------------------
    // See the file-top comment for why TextEdit is used instead of a Clipboard object.
    // visible:false hides it from the user; the 1x1 size keeps Qt happy with copy().
    TextEdit {
        id: clipHelper
        visible: false
        width: 1
        height: 1

        // Called by card's MouseArea when the user clicks a card that has a resume_cmd.
        // Sets the text, selects all of it, then copies the selection to the clipboard.
        function copyText(text) {
            clipHelper.text = text
            clipHelper.selectAll()
            clipHelper.copy()
        }
    }

    // ---------------------------------------------------------------------------
    // BUCKET DEFINITIONS  (display metadata only — classification is done by scan.py)
    // ---------------------------------------------------------------------------
    // Each entry: title shown in the column header, accent color for the header bar.
    // The `key` must exactly match the `bucket` values written by scan.py (spec §6).
    readonly property var buckets: [
        { key: "planning",  title: "Planning",  accent: "#5f8dd3" },
        { key: "writing",   title: "Writing",   accent: "#36c5a0" },
        { key: "QA",        title: "QA",        accent: "#c678dd" },
        { key: "testing",   title: "Testing",   accent: "#e0a93b" },
        { key: "finished",  title: "Finished",  accent: "#7f8c98" }
    ]

    // ---------------------------------------------------------------------------
    // VIEW
    // ---------------------------------------------------------------------------
    // fullRepresentation = what's shown when the widget is on the desktop.
    // On a desktop applet this is the ONLY representation (there is no popup).
    // It's a plain property value (not Plasmoid.fullRepresentation — that's Plasma 5).

    fullRepresentation: Item {
        id: fullRep
        // These are hints to Plasma for initial sizing; the user can resize the widget.
        implicitWidth:  1000
        implicitHeight: 400

        // ------------------------------------------------------------------
        // ERROR / EMPTY STATE
        // If board.json is missing or broken, show a friendly message instead of
        // an empty or broken card grid. The Repeater below shows nothing when
        // cards is [], and this Item overlays a message on top.
        // ------------------------------------------------------------------
        Item {
            anchors.fill: parent
            visible: root.boardError !== "" || root.cards.length === 0

            Kirigami.Heading {
                anchors.centerIn: parent
                level: 3
                // Show the specific error if we have one, otherwise a "not scanned yet" hint
                text: root.boardError !== ""
                    ? root.boardError
                    : "No board yet — run scan.py"
                color: Kirigami.Theme.disabledTextColor
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
                width: parent.width * 0.8
            }
        }

        // ------------------------------------------------------------------
        // FIVE-COLUMN KANBAN LAYOUT
        // Each column is one bucket. Inside each column, a nested Repeater
        // iterates ALL cards but collapses (visible:false, implicitHeight:0)
        // the ones that belong to a different bucket. This avoids splitting
        // the flat `cards` array into five sub-arrays in JS (extra allocation)
        // and keeps the Repeater model simple (a single flat array).
        // ------------------------------------------------------------------
        // "Show all" toggle — floats in the top-right strip. The board below leaves room
        // for it via anchors.topMargin so they never overlap. Checking it reveals cards
        // flagged `dropped` (finished projects aged off the board).
        QQC.CheckBox {
            id: showAllBox
            anchors.top: parent.top
            anchors.right: parent.right
            anchors.rightMargin: Kirigami.Units.smallSpacing
            visible: root.cards.length > 0
            text: "Show all"
            checked: root.showAll
            onToggled: root.showAll = checked
            QQC.ToolTip.text: "Also show finished projects that have dropped off the board"
            QQC.ToolTip.visible: hovered
            QQC.ToolTip.delay: 400
        }

        // Floating drag proxy — follows the cursor while a card is dragged to a new column.
        // Top of the z-order so it shows above the board; purely visual (the reclassify
        // happens on release, applying the live-tracked dropIndex).
        Rectangle {
            id: dragProxy
            z: 9999
            visible: root.dragging
            property point sp: root.dragging
                ? fullRep.mapFromItem(null, root.dragScenePos.x, root.dragScenePos.y)
                : Qt.point(0, 0)
            x: sp.x - width / 2
            y: sp.y - height / 2
            width: 160
            height: 30
            radius: 5
            color: Kirigami.Theme.highlightColor
            opacity: 0.92
            Text {
                anchors.fill: parent
                anchors.margins: 6
                verticalAlignment: Text.AlignVCenter
                horizontalAlignment: Text.AlignHCenter
                elide: Text.ElideRight
                text: root.dragCard ? ("⠿ " + root.dragCard.name) : ""
                color: Kirigami.Theme.highlightedTextColor
            }
        }

        RowLayout {
            id: boardRow
            // Expose this layout to root so columnIndexAt() can hit-test columns against its geometry.
            Component.onCompleted: root.boardRowItem = boardRow
            anchors.fill: parent
            anchors.margins: Kirigami.Units.smallSpacing
            // Leave a strip at the top for the "Show all" checkbox so it doesn't cover cards.
            anchors.topMargin: showAllBox.height + Kirigami.Units.smallSpacing
            spacing: Kirigami.Units.smallSpacing
            // Hide the whole board until we have data; the error message above takes over
            visible: root.cards.length > 0

            // One column per bucket entry in the `buckets` array above
            Repeater {
                model: root.buckets   // 5 items — one per bucket

                delegate: ColumnLayout {
                    id: column

                    // `required property` + ComponentBehavior:Bound = safe delegate
                    // access without "unqualified access" warnings.
                    // `index`     = 0..4 (position in the buckets array)
                    // `modelData` = the bucket object { key, title, accent }
                    required property int index
                    required property var modelData

                    Layout.fillWidth: true
                    // Equal preferred width on every column → fillWidth splits the widget
                    // into 5 EQUAL columns, so empty buckets (Testing/Finished) keep their
                    // full width and don't collapse (collapsing was clipping their headers).
                    Layout.preferredWidth: 1
                    Layout.fillHeight: true
                    Layout.alignment: Qt.AlignTop
                    spacing: Kirigami.Units.smallSpacing

                    // ---- Column header bar ----
                    // Tinted rectangle with the bucket's accent color so each column is
                    // immediately identifiable at a glance.
                    Rectangle {
                        Layout.fillWidth: true
                        implicitHeight: colHeader.implicitHeight + Kirigami.Units.smallSpacing * 2
                        radius: 4
                        color: column.modelData.accent
                        // Highlight this column's header while a drag is hovering it, so the
                        // target column is obvious before you release (the "snap to column"
                        // cue). dropIndex is the live-tracked column under the cursor.
                        border.width: (root.dragging && root.dropIndex === column.index) ? 3 : 0
                        border.color: Kirigami.Theme.highlightColor

                        Kirigami.Heading {
                            id: colHeader
                            // Fill + center + elide so a narrow column never lets the title
                            // spill outside the header bar (was overflowing on empty columns).
                            anchors.fill: parent
                            anchors.leftMargin: Kirigami.Units.smallSpacing
                            anchors.rightMargin: Kirigami.Units.smallSpacing
                            horizontalAlignment: Text.AlignHCenter
                            verticalAlignment: Text.AlignVCenter
                            elide: Text.ElideRight
                            level: 5
                            // Column title + count of cards currently in this bucket
                            text: {
                                root.localPinsRev   // re-eval the count when a drag-drop pins
                                var key = column.modelData.key
                                var n = 0
                                for (var i = 0; i < root.cards.length; i++) {
                                    if (root.effectiveBucket(root.cards[i]) === key
                                            && root.cardVisible(root.cards[i])) n++
                                }
                                return column.modelData.title + " (" + n + ")"
                            }
                            // Dark header text: white on the light/saturated accents fails
                            // WCAG contrast (amber & teal worst, ~2.1:1); dark clears AA on all
                            // five accents AND stays legible under a light system theme.
                            color: "#1a1a1a"
                        }
                    }

                    // ---- Cards in this column (per-column scroll) ----
                    // A ScrollView gives each column its OWN vertical scrollbar, so a
                    // column with more cards than fit (e.g. Writing) scrolls instead of
                    // overflowing the widget bottom. clip:true keeps cards in the viewport.
                    QQC.ScrollView {
                        id: colScroll
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        QQC.ScrollBar.horizontal.policy: QQC.ScrollBar.AlwaysOff  // vertical only

                        // Scroll content: width tracks the viewport (minus the scrollbar)
                        // so cards fill the column and never trigger horizontal scroll; its
                        // implicit height (sum of visible cards) is what the view scrolls.
                        ColumnLayout {
                            width: colScroll.availableWidth
                            spacing: Kirigami.Units.smallSpacing

                            // Iterates the FULL cards array; cards for other buckets
                            // collapse to zero height and are invisible.
                            Repeater {
                                model: root.cards   // flat array from board.json

                        delegate: Rectangle {
                            id: card

                            required property var modelData   // one card object from board.json
                            required property int index       // position in cards array (unused, satisfies the requirement)

                            // Transient flag: true for ~1.5 s after a click copies the resume
                            // command, so the resume line can flash "Copied!" as click feedback.
                            property bool justCopied: false

                            // Drag this card to another column to pin its bucket (writes the
                            // project's .board-status). Only the VISIBLE copy is draggable, and
                            // file-projects (no directory) can't be pinned this way.
                            DragHandler {
                                id: cardDrag
                                enabled: card.belongsHere && card.modelData.is_file !== true
                                // target: null = track the gesture but move NOTHING. Without
                                // it, target defaults to this handler's parent (the card), so
                                // the DragHandler physically dragged the real card out of its
                                // column and the column's ScrollView (clip:true) hid the part
                                // that left the viewport — the "card becomes less visible /
                                // janky placement" symptom. The floating dragProxy is the only
                                // thing that follows the cursor; the card stays put and just
                                // changes columns on drop.
                                target: null
                                onActiveChanged: {
                                    if (cardDrag.active) {
                                        root.dragging = true
                                        root.dragCard = card.modelData
                                        root.dropIndex = -1
                                    } else {
                                        // Apply the column tracked DURING the drag (valid
                                        // coordinates), not a post-release pointer read.
                                        if (root.dropIndex >= 0)
                                            root.setBucket(root.dragCard,
                                                           root.buckets[root.dropIndex].key)
                                        root.dragging = false
                                        root.dragCard = null
                                        root.dropIndex = -1
                                    }
                                }
                                onCentroidChanged: {
                                    if (cardDrag.active) {
                                        root.dragScenePos = cardDrag.centroid.scenePosition
                                        root.dropIndex =
                                            root.columnIndexAt(cardDrag.centroid.scenePosition)
                                    }
                                }
                            }

                            // Only cards whose effective bucket matches this column are shown.
                            // effectiveBucket() applies a live drag override (localPins) on top
                            // of board.json; the `localPinsRev` read re-evaluates this on a drop.
                            // Collapsing (visible:false + implicitHeight:0) rather than removing
                            // keeps the delegate count constant and avoids model churn.
                            readonly property bool belongsHere: {
                                root.localPinsRev   // dependency: re-eval when a drag-drop pins
                                return root.effectiveBucket(card.modelData) === column.modelData.key
                                       && root.cardVisible(card.modelData)
                            }

                            visible: card.belongsHere
                            Layout.fillWidth: true
                            // When collapsed, take up zero layout space so the column isn't padded
                            implicitHeight: card.belongsHere
                                ? cardBody.implicitHeight + Kirigami.Units.smallSpacing * 2
                                : 0

                            radius: 5
                            color: Kirigami.Theme.backgroundColor

                            // STALENESS HIGHLIGHT — one of the two permitted freshness signals
                            // (spec architect flag). An amber left-border visually flags cards
                            // where stale=true (project has gone quiet beyond 14 days).
                            // The second signal is last_touched_human text (shown in the card body).
                            // status_block_age_days is deliberately NOT shown — it would be a
                            // redundant third freshness signal.
                            // Every card keeps the thin grey edge; the amber left-strip below
                            // is the stale signal. (This previously read `stale ? 0 : 1`, which
                            // wrongly REMOVED the outline from stale cards.)
                            border.width: 1
                            border.color: Qt.rgba(0.5, 0.5, 0.5, 0.35)

                            // Amber left-border strip for stale cards
                            Rectangle {
                                visible: card.modelData.stale === true
                                width: 4
                                height: parent.height
                                radius: 5
                                color: "#e0a93b"   // amber — "gone quiet" warning
                            }

                            // ---- Card body ----
                            ColumnLayout {
                                id: cardBody
                                // Inset from the card edges; left side gets extra room for the
                                // stale border strip (4 px strip + a gap)
                                x: (card.modelData.stale === true ? 6 : Kirigami.Units.smallSpacing)
                                y: Kirigami.Units.smallSpacing
                                width: parent.width - x - Kirigami.Units.smallSpacing
                                spacing: 2

                                // Project name — the primary identifier. A badge prefix
                                // surfaces the two states worth knowing at a glance: 📌 = you
                                // pinned this (manual control: a .board-status or a live drag),
                                // ⚠ = stale, i.e. the LLM was tried and FAILED so this is an old
                                // classification (carried/gated/heuristic are healthy, no badge).
                                Kirigami.Heading {
                                    level: 6
                                    Layout.fillWidth: true
                                    elide: Text.ElideRight
                                    text: {
                                        root.localPinsRev   // re-eval when a drag pins
                                        var nm = card.modelData.name
                                        var pinned = card.modelData.classified_by === "pinned"
                                                     || root.localPins[nm] !== undefined
                                        var badge = pinned ? "📌 "
                                                  : card.modelData.classified_by === "stale" ? "⚠ "
                                                  : ""
                                        return badge + nm
                                    }
                                }

                                // Last completed work item (✓)
                                CardText {
                                    textValue: card.modelData.last_done
                                        ? ("✓ " + card.modelData.last_done)
                                        : "✓ —"
                                }

                                // How long since the project was touched (⏱)
                                // This is the FIRST freshness signal (plain text).
                                CardText {
                                    textValue: "⏱ " + (card.modelData.last_touched_human || "unknown")
                                }

                                // Next step (→) — what's pending, from the Status block's Next field.
                                // Wrapped to 2 lines; the full text is revealed on hover. Hidden when
                                // there's no Next (e.g. a project with no Status block).
                                Text {
                                    id: nextText
                                    visible: card.modelData.next !== undefined
                                             && card.modelData.next !== null
                                             && card.modelData.next !== ""
                                    text: "→ " + (card.modelData.next || "")
                                    Layout.fillWidth: true
                                    color: Kirigami.Theme.textColor
                                    font: Kirigami.Theme.smallFont
                                    wrapMode: Text.WordWrap
                                    maximumLineCount: 2          // wrap to at most 2 lines, then elide
                                    elide: Text.ElideRight
                                    HoverHandler { id: nextHover }
                                    QQC.ToolTip {
                                        parent: nextText
                                        visible: nextHover.hovered && nextText.truncated
                                        text: card.modelData.next || ""
                                        delay: 400
                                    }
                                }

                                // Owner chip — whose move is next
                                // 🔵 blue  = claude's turn (next action is for Claude)
                                // 🟡 amber = your turn    (next action requires you)
                                // ⬜ grey  = none / done  (no pending action)
                                Rectangle {
                                    radius: 3
                                    implicitHeight: ownerLabel.implicitHeight + 4
                                    implicitWidth:  ownerLabel.implicitWidth  + 10
                                    color: card.modelData.owner === "claude" ? "#6c8cff"
                                         : card.modelData.owner === "you"    ? "#e0a93b"
                                         :                                     "#7f8c98"

                                    Text {
                                        id: ownerLabel
                                        anchors.centerIn: parent
                                        text: card.modelData.owner === "claude" ? "claude"
                                            : card.modelData.owner === "you"    ? "you"
                                            :                                     "done"
                                        // Dark chip text — white on these light fills (the amber
                                        // 'you' chip worst at ~2.1:1) fails contrast; dark clears AA.
                                        color: "#1a1a1a"
                                        font: Kirigami.Theme.smallFont
                                    }
                                }

                                // Resume line — shows the session id (the "conversation name" you'd
                                // resume); hover for the full `cd … && claude --resume …` command;
                                // click to copy it, with a brief "Copied!" flash as confirmation.
                                Text {
                                    id: resumeText
                                    visible: card.modelData.resume_cmd !== null
                                             && card.modelData.resume_cmd !== undefined
                                             && card.modelData.resume_cmd !== ""
                                    text: card.justCopied
                                          ? "✓ Copied resume command"
                                          : "▶ resume: " + (card.modelData.resume_session_id || "?")
                                    Layout.fillWidth: true
                                    elide: Text.ElideRight
                                    color: card.justCopied ? "#36c5a0" : Kirigami.Theme.textColor
                                    font: Kirigami.Theme.smallFont
                                    HoverHandler { id: resumeHover }
                                    QQC.ToolTip {
                                        parent: resumeText
                                        visible: resumeHover.hovered && !card.justCopied
                                        text: card.modelData.resume_cmd || ""
                                        delay: 300
                                    }
                                    MouseArea {
                                        anchors.fill: parent
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: {
                                            clipHelper.copyText(card.modelData.resume_cmd)
                                            card.justCopied = true
                                            copiedTimer.restart()
                                        }
                                    }
                                }
                            }

                            // Click anywhere on the card copies resume_cmd (convenience).
                            // The inner MouseArea on the ▶ line handles clicks there; this outer
                            // one covers the rest of the card. Both copy the same string, so even
                            // if both ever fired the result would be identical (harmless).
                            MouseArea {
                                anchors.fill: parent
                                // Don't steal hover/click from child MouseAreas
                                propagateComposedEvents: true
                                cursorShape: (card.modelData.resume_cmd !== null
                                              && card.modelData.resume_cmd !== undefined
                                              && card.modelData.resume_cmd !== "")
                                             ? Qt.PointingHandCursor
                                             : Qt.ArrowCursor
                                onClicked: function(mouse) {
                                    if (card.modelData.resume_cmd) {
                                        clipHelper.copyText(card.modelData.resume_cmd)
                                        card.justCopied = true
                                        copiedTimer.restart()
                                    }
                                    mouse.accepted = false   // let child MouseAreas also see the click
                                }
                            }

                            // Resets the "Copied!" flash after a moment. At card scope so both the
                            // resume-line click and this whole-card click can trigger it.
                            Timer { id: copiedTimer; interval: 1500; onTriggered: card.justCopied = false }
                        }
                            }
                        }
                    }
                }
            }
        }
    }

    // ---------------------------------------------------------------------------
    // INLINE REUSABLE COMPONENT — single card text row
    // ---------------------------------------------------------------------------
    // Declared here (inside PlasmoidItem) as a `component` block.
    // Under ComponentBehavior:Bound, inline components declared at the root level
    // are accessible from any delegate below — they share the scope.
    // Avoids repeating elide/font/color/fillWidth on every card text line.
    component CardText: Text {
        id: cardText
        // The text to display — set by the caller (e.g., CardText { textValue: "✓ ..." })
        property string textValue

        text: textValue
        elide: Text.ElideRight       // cut off with "…" rather than overflow the card
        Layout.fillWidth: true       // stretches to the card's available width
        color: Kirigami.Theme.textColor
        font: Kirigami.Theme.smallFont
        wrapMode: Text.NoWrap        // single line per field; elide handles overflow

        // When this line is truncated (elided by width), reveal the FULL text on hover
        // so nothing is permanently hidden by the card width. `truncated` is true ONLY
        // when actually elided, so the tooltip never fires on short lines.
        HoverHandler { id: cardTextHover }
        QQC.ToolTip {
            parent: cardText
            visible: cardTextHover.hovered && cardText.truncated
            text: cardText.textValue
            delay: 400
        }
    }
}
