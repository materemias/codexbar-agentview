# /// script
# dependencies = ["pillow"]
# ///
"""Films one take of the Plasma widget for film/compose.py: the real FullRepresentation and
CompactRepresentation from contents/ui, fed made-up accounts and sessions, in an offscreen Qt
window dressed as a Plasma desktop (wallpaper, panel, terminal windows, the popup above the tray).
A script in the window drives it with real key and mouse events and grabs the window about 15
times a second; the frames, pointer moves, key presses, captions and scene changes go to
WORK/timeline.json.

    uv run film/take_kde.py [WORK]   (default /tmp/codexbar-agentview-film-kde)
    uv run film/compose.py kde [WORK]

Needs qml6 with QtTest and Plasma/Kirigami, and localedef, like tests/render_screenshots.py, whose
fixture, display helpers and isolated Qt environment this reuses. The theme is copied from your
Plasma session. main.qml never runs, so nothing fetches usage, scans processes, focuses or launches.
"""

import json
import re
import shutil
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from multiprocessing import Pool
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))
import render_screenshots as rs  # noqa: E402

WORK = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/codexbar-agentview-film-kde").resolve()
# The desktop in logical pixels, shot at DPR device pixels per logical pixel.
SCREEN = (1280, 720)
DPR = 1.5
PANEL_H = 44
POPUP = (480, 560)
# Camera moves change every pixel and cost the GIF most of its bytes, so they stay short.
TRANSITION_MS = 1100

# Usage windows and ages count from now, so the panel's rings (which read the clock) and the popup
# agree on pace.
rs.NOW = datetime.now(timezone.utc).replace(second=0, microsecond=0)
rs.NOW_MS = int(rs.NOW.timestamp() * 1000)
NOW_MS = rs.NOW_MS
MIN = 60_000


def turns(*pairs):
    return [{"role": role, "text": text} if role != "tools" else {"role": "assistant", "kind": "tools", "text": text} for role, text in pairs]


# Per-session conversation for rs.fixture()'s eight agents, in its order. No text contains "rev",
# so the filter in the film matches titles only.
RECENT = [
    (6, turns(("user", "Does a failed refresh keep the last good result?"),
              ("assistant", "Yes. The handler swaps the cache only after the new fetch succeeds."),
              ("tools", "Ran pytest tests/test_cache.py · 6 passed"))),
    (95, turns(("user", "Tighten the spacing on the settings page."),
               ("assistant", "Labels now share one column, and the page fits at 800 pixels."))),
    (3, turns(("user", "Why do large uploads retry three times?"),
              ("assistant", "The proxy closes idle sockets after 30 seconds. Large files send chunks slower than that."))),
    (9, turns(("user", "Can the report query finish under five seconds?"),
              ("assistant", "It scans every order. An index on orders.created_at fixes it. May I add the migration?"))),
    (140, turns(("user", "Who can export the audit log?"),
                ("assistant", "Only owners. The admin role misses the export scope; the list is in docs/roles.md."))),
    (2, turns(("user", "The runner hangs on shutdown."),
              ("assistant", "A worker waits on a closed channel. I am adding a timeout to the drain loop."))),
    (4, turns(("user", "Find why webhook deliveries fail with 401."),
              ("assistant", "The signing secret rotates hourly, but queued retries reuse their first signature."),
              ("assistant", "I want to sign each retry when it is sent. Can I edit src/webhooks/retry.ts?"))),
    (12, turns(("user", "Group the log lines by request id."),
               ("assistant", "Grouping works. The worker logs still lack the id, so I am threading it through."))),
]


def film_fixture():
    data = rs.fixture()
    agents = data["agentSnapshot"]["agents"]
    for agent, (minutes, recent) in zip(agents, RECENT, strict=True):
        agent["stateChangedAt"] = NOW_MS - minutes * MIN
        agent["recent"] = recent

    def ended(n, folder, title, provider, model, closed_by, minutes_ago, recent, desktop=None):
        session_id = f"00000000-0000-4000-8000-{n:012d}"
        command = f"codex resume {session_id}" if provider == "codex" else f"{provider} --resume {session_id}"
        row = {"provider": provider, "sessionId": session_id, "cwd": f"/workspace/{folder}", "windowTitle": title,
               "state": "idle", "model": model, "host": "kitty", "closedBy": closed_by,
               "lastSeenAt": NOW_MS - minutes_ago * MIN, "resumeCommand": f"cd -- /workspace/{folder} && {command}",
               "recent": recent}
        if desktop:
            row["desktop"] = desktop
        return row

    data["agentSnapshot"]["history"] = [
        ended(101, "atlas", "Tune image cache", "claude", "claude-sonnet-4-6", "reboot", 300,
              turns(("user", "Cap the image cache at 200 MB."), ("assistant", "Capped. The oldest entries go first.")), "1"),
        ended(102, "harbor", "Draft release notes", "codex", "gpt-5.4", "reboot", 310,
              turns(("user", "Draft notes for 2.4."), ("assistant", "The draft lists the upload fixes and the new export scope.")), "2"),
        ended(103, "beacon", "Map flaky tests", "omp", "gpt-5.4", "reboot", 320,
              turns(("user", "Which tests fail at random?"), ("assistant", "Three, all waiting on the same mock clock.")), "2"),
        ended(104, "atlas", "Fix keyboard navigation", "codex", "gpt-5.4", "exit", 40,
              turns(("user", "Keep selection on the same session when the list changes."),
                    ("assistant", "Selection now follows session identity, not the row index."))),
        ended(105, "harbor", "Rename config keys", "claude", "claude-sonnet-4-6", "exit", 130,
              turns(("user", "Rename the retry keys to snake case."), ("assistant", "Renamed, with the old keys still read once."))),
        ended(106, "beacon", "Bump lint rules", "omp", "gpt-5.4", "exit", 220,
              turns(("user", "Turn on the unused-import rule."), ("assistant", "On, and the 14 offenders are fixed."))),
    ]
    return data


PLASMOID = (
    " property int formFactor: 2\n"  # PlasmaCore.Types.Horizontal: a horizontal panel
    " property var configuration: ({showAgents: true, showAgentPrompts: true, showComposite5h: false,"
    " showComposite7d: true, trayIconSize: 24, compactStyle: 0, showAgentStateDots: true, agentBlockedBadge: true,"
    " showAgentTopicInPanel: false,"
    ' trayIndicators: ["claude:primary", "codex:primary", "zai:primary", "opencodego:primary"]})\n'
)

# Terminal windows behind the popup: (desktop, x, y, w, h, icon, title, lines). Colors are names
# from TERM_COLORS; a line is [text, color, bold].
FG, DIM, ACCENT, GREEN, RED, BLUE, YELLOW = "fg", "dim", "accent", "green", "red", "blue", "yellow"
TERM_COLORS = {FG: "#d8dee9", DIM: "#7b8394", ACCENT: "#d97757", GREEN: "#a3be8c", RED: "#bf616a", BLUE: "#88c0d0", YELLOW: "#ebcb8b"}


def box(rows, width=60, color=DIM):
    out = [["╭" + "─" * (width - 2) + "╮", color, False]]
    for text, c, bold in rows:
        out.append(["│ " + text.ljust(width - 4) + " │", c, bold])
    out.append(["╰" + "─" * (width - 2) + "╯", color, False])
    return out


CODEX = [
    [">_ OpenAI Codex", FG, True],
    ["   model gpt-5.4 · directory /workspace/atlas", DIM, False],
    ["", FG, False],
    ["› Does a failed refresh keep the last good result?", FG, True],
    ["", FG, False],
    ["• Yes. The handler swaps the cache only after the new", FG, False],
    ["  fetch succeeds, so readers keep the old value.", FG, False],
    ["", FG, False],
    ["• Ran pytest tests/test_cache.py -k refresh", FG, False],
    ["  └ 6 passed in 0.41s", GREEN, False],
    ["", FG, False],
    ["• Adding a test for the timeout path now.", FG, False],
    ["", FG, False],
    ["◦ Working (32s · esc to interrupt)", DIM, False],
]
OMP = [
    [" omp · harbor · gpt-5.4", BLUE, True],
    ["", FG, False],
    [" ❯ Why do large uploads retry three times?", FG, True],
    ["", FG, False],
    [" The proxy closes idle sockets after 30 seconds, and a", FG, False],
    [" large file sends its chunks slower than that.", FG, False],
    ["", FG, False],
    [" ⏺ grep -rn keepalive src/upload", YELLOW, False],
    ["   src/upload/client.ts:18  keepAlive: false", DIM, False],
    ["", FG, False],
    [" ⠋ Turning keep-alive on…", DIM, False],
]
CLAUDE = [
    *box([["✻ Welcome to Claude Code", ACCENT, True], ["  /workspace/beacon", DIM, False]], 44, ACCENT),
    ["", FG, False],
    ["> Find why webhook deliveries fail with 401.", FG, True],
    ["", FG, False],
    ["● Read(src/webhooks/retry.ts)", FG, False],
    ["  ⎿  Read 84 lines", DIM, False],
    ["", FG, False],
    ["● The signing secret rotates every hour, but a queued retry", FG, False],
    ["  reuses the signature from its first attempt. After a", FG, False],
    ["  rotation, every retry fails with 401.", FG, False],
    ["", FG, False],
    ["● I'll sign each retry when it is sent.", FG, False],
    ["", FG, False],
    *box([
        ["Edit file", FG, True],
        ["src/webhooks/retry.ts", DIM, False],
        ["", FG, False],
        [" 41 -   headers: { signature: job.signature },", RED, False],
        [" 41 +   headers: { signature: sign(job.body) },", GREEN, False],
        ["", FG, False],
        ["Do you want to make this edit to retry.ts?", FG, False],
        ["❯ 1. Yes", BLUE, True],
        ["  2. Yes, allow all edits during this session", FG, False],
        ["  3. No, and tell Claude what to do differently", FG, False],
    ], 62, ACCENT),
]
TERMS = [
    {"desktop": 0, "x": 36, "y": 34, "w": 640, "h": 400, "icon": "kitty", "title": "Review cache invalidation", "lines": CODEX},
    {"desktop": 0, "x": 250, "y": 250, "w": 560, "h": 330, "icon": "kitty", "title": "Trace upload retries", "lines": OMP},
    {"desktop": 1, "x": 70, "y": 30, "w": 680, "h": 590, "icon": "kitty", "title": "✳ Review webhook errors", "lines": CLAUDE},
]

# Where each restored session's window opens: sessionId suffix -> (x, y). Windows open on their saved
# desktop, or on the current one (the second) when the row has none.
RESTORED_AT = {101: (420, 70), 102: (110, 80), 103: (170, 150), 104: (230, 220)}
MARK = {"claude": "●", "codex": "•", "omp": "⏺"}


def restored_terms(history):
    """A window for each session the film restores, showing its resumed conversation."""
    out = []
    for row in history:
        n = int(row["sessionId"][-12:])
        if n not in RESTORED_AT:
            continue
        lines = [[row["resumeCommand"].split(" && ")[-1][:52], DIM, False], ["", FG, False]]
        for turn in row["recent"]:
            user = turn["role"] == "user"
            wrapped = textwrap.wrap(turn["text"], 60)
            prefix = ">" if user else MARK[row["provider"]]
            for i, text in enumerate(wrapped):
                lines.append([(prefix if i == 0 else " ") + " " + text, FG, user])
            lines.append(["", FG, False])
        x, y = RESTORED_AT[n]
        out.append({"desktop": int(row.get("desktop", "2")) - 1, "x": x, "y": y, "w": 520, "h": 250, "icon": "kitty",
                    "title": row["windowTitle"], "lines": lines, "restores": row["sessionId"]})
    return out


HOST = r'''
import QtQuick
import QtQuick.Window
import QtQuick.Layouts
import QtTest
import org.kde.kirigami as Kirigami
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PC3
import "contents/ui" as Product
import "contents/ui/Usage.js" as Usage

Window {
    id: window
    width: @WIDTH@
    height: @HEIGHT@
    visible: true
    color: "black"

    readonly property double t0: Date.now()
    function now() { return Date.now() - t0 }
    // Everything compose.py needs goes to stdout as one JSON object per line.
    function note(event) {
        event.t = now()
        console.log("EVENT " + JSON.stringify(event))
    }

    // The state main.qml's PlasmoidItem holds, with its pure display helpers; nothing here fetches,
    // scans, focuses or launches.
    Item {
        id: root
        property var snapshot: (@SNAPSHOT@)
        property var agentSnapshot: (@AGENTS@)
        property double nowMs: @NOW@
        property string requestedTab: ""
        property bool expanded: false
        property bool loading: false
        property bool codexForecastEnabled: false
        property bool showRecentUsage: true
        property string lastError: ""
        property string agentsError: ""
        property string notificationError: ""
        property string historyLaunchError: ""
        property var historyLaunches: ({})
        property var teleports: ({})
        property string teleportError: ""
        onExpandedChanged: {
            root.nowMs = Date.now()
            if (!root.expanded) root.requestedTab = ""
        }
        Timer {
            interval: 1000
            running: true
            repeat: true
            onTriggered: root.nowMs = Date.now()
        }
        function desktopInfoFor(record) { return null }
        function isCurrentDesktop(label) { return label === String(desk.current + 1) }
        function teleportAgent(record) { throw new Error("The film must never move a session") }
        // Launches are staged: main.qml's cooldown bookkeeping, then the session's window opens
        // and, as the next aggregator sweep would report, its row leaves History for Agents.
        function launchHistory(record) { window.stageLaunch([record]) }
        function launchAllHistory(records) {
            window.stageLaunch(records.filter(function(r) {
                return r.closedBy === "reboot" && root.launchHost(r.host) && !!r.resumeCommand
            }))
        }
        function dismissHistory(records) { throw new Error("The film must never mutate history") }
        function refresh() { throw new Error("The film must never fetch live usage") }
        function runAggregator() { throw new Error("The film must never scan live sessions") }
        @HELPERS@
    }

    // A window's soft shadow, a blurred rounded rectangle from take_kde.py.
    component Shadow: BorderImage {
        source: "shadow.png"
        border { left: 48; top: 48; right: 48; bottom: 48 }
        anchors.fill: parent
        anchors.margins: -32
        anchors.topMargin: -28
        anchors.bottomMargin: -38
    }

    component Term: Item {
        id: term
        property var spec
        property bool active: false
        // A restored session's window opens when the film launches it, the way KWin fades one in.
        readonly property int opened: spec.restores ? (window.restoredIds[spec.restores] || 0) : 0
        property real shown: !spec.restores || opened > 0 ? 1 : 0
        Behavior on shown { NumberAnimation { duration: 260; easing.type: Easing.OutCubic } }
        visible: shown > 0
        opacity: shown
        scale: 0.92 + 0.08 * shown
        x: spec.x
        y: spec.y
        width: spec.w
        height: spec.h
        Shadow { opacity: term.active ? 1 : 0.7 }
        Rectangle {
            anchors.fill: parent
            radius: 8
            color: "#1f232b"
            border.color: term.active ? "#4c566a" : "#353b48"
        }
        Rectangle {
            id: titlebar
            anchors { left: parent.left; right: parent.right; top: parent.top; margins: 1 }
            height: 30
            radius: 7
            color: term.active ? "#3b4252" : "#2b303b"
            Rectangle { anchors { left: parent.left; right: parent.right; bottom: parent.bottom } height: 8; color: parent.color }
            Kirigami.Icon {
                source: term.spec.icon
                width: 18; height: 18
                anchors { left: parent.left; leftMargin: 8; verticalCenter: parent.verticalCenter }
            }
            Text {
                anchors.centerIn: parent
                text: term.spec.title
                color: term.active ? "#eceff4" : "#8a93a5"
                font: Kirigami.Theme.defaultFont
            }
            Row {
                anchors { right: parent.right; rightMargin: 10; verticalCenter: parent.verticalCenter }
                spacing: 8
                Repeater {
                    model: ["#4c566a", "#4c566a", term.active ? "#bf616a" : "#4c566a"]
                    Rectangle { width: 14; height: 14; radius: 7; color: modelData }
                }
            }
        }
        Column {
            anchors { left: parent.left; right: parent.right; top: titlebar.bottom; margins: 12 }
            clip: true
            Repeater {
                model: term.spec.lines
                Text {
                    text: modelData[0] || " "
                    color: modelData[1]
                    font.family: "JetBrainsMono Nerd Font"
                    font.pixelSize: 12
                    font.bold: modelData[2]
                    textFormat: Text.PlainText
                }
            }
        }
    }

    Item {
        id: desktop
        anchors.fill: parent

        Image {
            anchors.fill: parent
            source: "wallpaper.png"
            fillMode: Image.PreserveAspectCrop
            smooth: true
        }

        // The virtual desktops' windows, side by side; switching slides them like KWin does.
        Item {
            id: desk
            property int current: 0
            y: 0
            width: window.width * 2
            height: window.height - panel.height
            x: -current * window.width
            Behavior on x { NumberAnimation { duration: 420; easing.type: Easing.InOutCubic } }
            Repeater {
                model: window.terms
                Term {
                    spec: modelData
                    x: modelData.x + modelData.desktop * window.width
                    active: modelData.desktop === desk.current && index === window.activeTerm[modelData.desktop]
                    z: active ? 100 : opened
                }
            }
        }

        // The panel: launcher, pager, task manager, the widget, the system tray and the clock.
        Rectangle {
            id: panel
            anchors { left: parent.left; right: parent.right; bottom: parent.bottom }
            height: @PANEL@
            color: Qt.rgba(Kirigami.Theme.backgroundColor.r, Kirigami.Theme.backgroundColor.g, Kirigami.Theme.backgroundColor.b, 0.96)
            Rectangle { anchors { left: parent.left; right: parent.right; top: parent.top } height: 1; color: Qt.rgba(1, 1, 1, 0.08) }

            RowLayout {
                anchors { fill: parent; leftMargin: 8; rightMargin: 10 }
                spacing: 6

                Kirigami.Icon { source: "start-here-kde"; Layout.preferredWidth: 28; Layout.preferredHeight: 28 }
                Row {
                    spacing: 4
                    Layout.leftMargin: 6
                    Repeater {
                        model: 3
                        Rectangle {
                            width: 36; height: 24; radius: 3
                            color: index === desk.current ? Qt.rgba(Kirigami.Theme.highlightColor.r, Kirigami.Theme.highlightColor.g, Kirigami.Theme.highlightColor.b, 0.35) : Qt.rgba(1, 1, 1, 0.05)
                            border.color: index === desk.current ? Kirigami.Theme.highlightColor : Qt.rgba(1, 1, 1, 0.15)
                            Behavior on color { ColorAnimation { duration: 200 } }
                            Text {
                                anchors.centerIn: parent
                                text: index + 1
                                color: Kirigami.Theme.textColor
                                opacity: 0.7
                                font.pixelSize: 11
                            }
                        }
                    }
                }
                Repeater {
                    model: [{icon: "kitty", running: true}, {icon: "org.kde.dolphin", running: true}, {icon: "firefox", running: false}]
                    Item {
                        Layout.preferredWidth: 44
                        Layout.fillHeight: true
                        Layout.leftMargin: index === 0 ? 10 : 0
                        Rectangle {
                            anchors.fill: parent
                            anchors.margins: 3
                            radius: 4
                            color: Qt.rgba(1, 1, 1, index === 0 ? 0.08 : 0)
                        }
                        Kirigami.Icon { source: modelData.icon; width: 30; height: 30; anchors.centerIn: parent }
                        Rectangle {
                            visible: modelData.running
                            anchors { horizontalCenter: parent.horizontalCenter; bottom: parent.bottom; bottomMargin: 2 }
                            width: index === 0 ? 24 : 6; height: 3; radius: 1.5
                            color: Kirigami.Theme.highlightColor
                        }
                    }
                }
                Item { Layout.fillWidth: true }
                Product.CompactRepresentation {
                    id: compact
                    Layout.fillHeight: true
                    Layout.topMargin: 6
                    Layout.bottomMargin: 6
                }
                Row {
                    spacing: 10
                    Layout.leftMargin: 14
                    Repeater {
                        model: ["network-wireless-symbolic", "audio-volume-high-symbolic", "notifications-symbolic"]
                        Kirigami.Icon { source: modelData; width: 18; height: 18; isMask: true; color: Kirigami.Theme.textColor }
                    }
                }
                Column {
                    Layout.leftMargin: 12
                    Text {
                        anchors.horizontalCenter: parent.horizontalCenter
                        text: Qt.formatTime(new Date(root.nowMs), "HH:mm")
                        color: Kirigami.Theme.textColor
                        font.pixelSize: 14
                        font.weight: Font.DemiBold
                    }
                    Text {
                        anchors.horizontalCenter: parent.horizontalCenter
                        text: Qt.formatDate(new Date(root.nowMs), "ddd d MMM")
                        color: Kirigami.Theme.textColor
                        opacity: 0.7
                        font.pixelSize: 10
                    }
                }
            }
        }

        // The popup, opening above the widget the way Plasma slides it in.
        Item {
            id: dialog
            width: @POPUP_W@
            height: @POPUP_H@
            x: Math.min(window.width - width - 8, compact.mapToItem(desktop, compact.width / 2, 0).x - width / 2)
            y: panel.y - height - 8
            property real shown: root.expanded ? 1 : 0
            Behavior on shown { NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }
            opacity: shown
            visible: shown > 0
            transform: Translate { y: (1 - dialog.shown) * 20 }
            Shadow {}
            Rectangle {
                anchors.fill: parent
                radius: 8
                color: Kirigami.Theme.backgroundColor
                border.color: Qt.rgba(1, 1, 1, 0.1)
            }
            Product.FullRepresentation { id: popup; anchors.fill: parent; anchors.margins: 2 }
        }
    }

    TestCase {
        id: input
        name: "FilmInput"
        when: false
        // No test runner: key and mouse events go only to this offscreen window.
    }

    // Grabs the window as often as it can, at most about 15 times a second.
    property int shots: 0
    property bool filming: true
    function shoot() {
        var t = now()
        window.contentItem.grabToImage(function(result) {
            // BMP, since encoding PNG here would halve the frame rate; take_kde.py converts them.
            var path = "shots/" + ("0000" + window.shots).slice(-5) + ".bmp"
            if (!result.saveToFile(path)) throw new Error("Could not save " + path)
            console.log("FRAME " + JSON.stringify({t: t, path: path}))
            window.shots++
            if (window.filming) {
                shooter.interval = Math.max(1, 66 - (now() - t))
                shooter.restart()
            } else {
                Qt.quit()
            }
        })
    }
    Timer { id: shooter; onTriggered: window.shoot() }

    // Pointer: eased glides through real mouse moves, so hover effects and tooltips are real too.
    property var pointer: ({x: window.width * 0.56, y: window.height * 0.42})
    Timer {
        id: glider
        interval: 30
        repeat: true
        property var from
        property var to
        property double started
        property double ms
        onTriggered: {
            var k = Math.min(1, (window.now() - started) / ms)
            var e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2
            window.pointer = {x: from.x + (to.x - from.x) * e, y: from.y + (to.y - from.y) * e}
            input.mouseMove(window.contentItem, window.pointer.x, window.pointer.y)
            window.note({kind: "move", surface: "desktop", x: window.pointer.x, y: window.pointer.y})
            if (k >= 1) stop()
        }
    }
    function glide(item, ms) {
        var p = item.mapToItem(window.contentItem, item.width / 2, item.height / 2)
        glider.from = window.pointer
        glider.to = {x: p.x, y: p.y}
        glider.started = now()
        glider.ms = ms
        glider.restart()
        return ms
    }
    function click(item) {
        var p = item.mapToItem(window.contentItem, item.width / 2, item.height / 2)
        note({kind: "click", surface: "desktop"})
        input.mouseClick(window.contentItem, p.x, p.y)
    }
    function hidePointer() { note({kind: "hide", surface: "desktop"}) }

    function press(caps, key) {
        note({kind: "key", keys: caps})
        if (key !== undefined) input.keyClick(key)
    }
    function caption(eyebrow, headline) { note({kind: "caption", eyebrow: eyebrow, headline: headline}) }
    function cut(scene, rect, fill) {
        var event = {kind: "scene", scene: scene}
        if (rect) event.rect = rect
        if (fill) event.fill = fill
        note(event)
    }
    // compose.py's card is 1480 x 684; framings with its aspect fill it without showing the bezel.
    readonly property real cardAspect: 1480 / 684
    // The popup and the panel under it, flush with the screen's right edge.
    function popupRect() {
        var h = window.height - dialog.y + 14
        var w = h * cardAspect
        return [window.width - w, window.height - h, w, h]
    }
    // The widget in the panel, with some desktop above it.
    function trayRect() {
        var w = compact.width + 420
        var h = w / cardAspect
        var x = compact.mapToItem(window.contentItem, compact.width / 2, 0).x - w / 2 + 60
        return [Math.min(x, window.width - w), window.height - h, w, h]
    }
    function find(item, test) {
        if (test(item)) return item
        var children = item.children || []
        for (var i = 0; i < children.length; ++i) {
            var found = find(children[i], test)
            if (found) return found
        }
        return null
    }
    readonly property var terms: (@TERMS@)
    // Restored session id -> the order its window opened in.
    property var restoredIds: ({})
    property int restoreCount: 0
    // The raised window on each desktop: the newest restored one, else the last one listed there.
    readonly property var activeTerm: {
        var best = [1, 2], order = [0, 0]
        for (var i = 0; i < terms.length; i++) {
            var opened = terms[i].restores ? (restoredIds[terms[i].restores] || 0) : 0
            if (opened > order[terms[i].desktop]) {
                order[terms[i].desktop] = opened
                best[terms[i].desktop] = i
            }
        }
        return best
    }
    function stageLaunch(records) {
        var launches = Object.assign({}, root.historyLaunches)
        var ids = Object.assign({}, restoredIds)
        for (var i = 0; i < records.length; i++) {
            launches[JSON.stringify([records[i].provider, records[i].sessionId])] = Date.now()
            ids[records[i].sessionId] = ++restoreCount
        }
        root.historyLaunches = launches
        restoredIds = ids
        goLive.ids = records.map(function(r) { return r.sessionId })
        goLive.restart()
    }
    Timer {
        id: goLive
        interval: 1500
        property var ids: []
        onTriggered: {
            var s = root.agentSnapshot
            var back = s.history.filter(function(r) { return ids.indexOf(r.sessionId) >= 0 }).map(function(r) {
                var live = Object.assign({}, r, {state: "idle", stateChangedAt: Date.now()})
                delete live.closedBy
                delete live.lastSeenAt
                return live
            })
            var counts = Object.assign({}, s.counts)
            counts.idle += back.length
            counts.total += back.length
            root.agentSnapshot = {
                agents: s.agents.concat(back),
                history: s.history.filter(function(r) { return ids.indexOf(r.sessionId) < 0 }),
                counts: counts,
                ternInstalled: s.ternInstalled
            }
        }
    }
    property Item target: null

    readonly property int transitionMs: @TRANSITION@
    property int step: 0
    property var script: [
        function() {
            cut("wide")
            caption("CodexBar AgentView", "Your agents and limits, in the panel.")
            note({kind: "move", surface: "desktop", x: pointer.x, y: pointer.y})
            return 3000
        },
        function() {
            caption("At a glance", "Red means a session needs you.")
            cut("desktop", trayRect(), 1)
            return transitionMs + 1500
        },
        function() { return glide(compact, 800) + 250 },
        function() { click(compact); return 500 },
        function() {
            cut("desktop", popupRect(), 1)
            return transitionMs + 900
        },
        function() {
            caption("Usage", "No surprise limits.")
            hidePointer()
            press(["←"], Qt.Key_Left)
            return 2500
        },
        function() { caption("Usage", "Pace marks warn you early."); return 2400 },
        function() { press(["Esc"], Qt.Key_Escape); root.expanded = false; return 900 },
        function() {
            caption("Keyboard", "Never leave the keyboard.")
            press(["Super", "A"])
            root.expanded = true
            root.requestedTab = "agents"
            return 2000
        },
        function() { caption("Agents", "Find any session by typing."); press(["R"], "r"); return 300 },
        function() { press(["E"], "e"); return 300 },
        function() { press(["V"], "v"); return 1700 },
        function() { press(["↓"], Qt.Key_Down); return 1100 },
        function() {
            // With a query typed, Space is part of it; Esc clears the query and keeps the selection.
            caption("Agents", "Peek at its last messages.")
            press(["Esc"], Qt.Key_Escape)
            return 700
        },
        function() { press(["Space"], Qt.Key_Space); return 2700 },
        function() {
            // Enter would ask the system to focus the session; the film stages the jump instead.
            caption("Agents", "Jump straight to it.")
            press(["Enter"])
            root.expanded = false
            return 300
        },
        function() { desk.current = 1; cut("desktop"); return transitionMs + 1700 },
        function() {
            caption("Get it all back", "Restore all after a reboot.")
            press(["Super", "A"])
            root.expanded = true
            root.requestedTab = "agents"
            cut("desktop", popupRect(), 1)
            return transitionMs
        },
        function() { press(["→"], Qt.Key_Right); return 900 },
        function() {
            target = find(popup, function(item) {
                return item.visible && typeof item.text === "string" && item.text.indexOf("Restore all") === 0
            })
            if (!target) throw new Error("No Restore all button")
            return glide(target, 700) + 1200
        },
        function() { click(target); return 2600 },
        function() {
            caption("Get it all back", "Closed by mistake? Bring it back.")
            var row = find(popup, function(item) {
                return item.visible && item.modelData && item.modelData.windowTitle === "Fix keyboard navigation"
                    && item.canLaunch !== undefined
            })
            target = row && find(row, function(item) {
                return item.visible && item.icon && item.icon.name === "media-playback-start"
            })
            if (!target) throw new Error("No launch button on the closed session")
            return glide(target, 700) + 1000
        },
        function() { click(target); return 2600 },
        function() {
            caption("CodexBar AgentView for KDE Plasma", "Every agent, one keystroke away.")
            hidePointer()
            // The real pointer leaves the popup too, so no tooltip stays up in the last shot.
            input.mouseMove(window.contentItem, window.width * 0.3, window.height * 0.85)
            cut("wide")
            return transitionMs + 2800
        },
        function() { filming = false; return 0 }
    ]
    Timer {
        id: director
        onTriggered: {
            var wait = window.script[window.step++]()
            if (window.step < window.script.length) {
                interval = Math.max(1, wait)
                restart()
            }
        }
    }
    Timer {
        // Lets the offscreen layout settle before the take starts.
        interval: 1500
        running: true
        onTriggered: {
            window.shoot()
            director.interval = 300
            director.start()
        }
    }
}
'''


def wallpaper(path: Path):
    size = (round(SCREEN[0] * DPR), round(SCREEN[1] * DPR))
    for candidate in ("Next/contents/images_dark/5120x2880.png", "Next/contents/images/5120x2880.png"):
        source = Path("/usr/share/wallpapers") / candidate
        if source.is_file():
            Image.open(source).convert("RGB").resize(size, Image.LANCZOS).save(path)
            return
    Image.new("RGB", size, (35, 38, 52)).save(path)


def to_png(bmp: Path):
    Image.open(bmp).save(bmp.with_suffix(".png"), compress_level=3)
    bmp.unlink()


def shadow(path: Path):
    """A 9-slice window shadow: a blurred rounded rectangle, 48 pixels of falloff on each side."""
    image = Image.new("L", (160, 160), 0)
    ImageDraw.Draw(image).rounded_rectangle((32, 28, 128, 122), radius=10, fill=150)
    alpha = image.filter(ImageFilter.GaussianBlur(11))
    out = Image.new("RGBA", alpha.size, (0, 0, 0, 0))
    out.putalpha(alpha)
    out.save(path)


def main():
    # Only a folder an earlier take made (it holds SENTINEL) is cleared; anything else is refused.
    sentinel = WORK / ".codexbar-agentview-film"
    if WORK.exists():
        if not sentinel.exists():
            sys.exit(f"{WORK} exists and wasn't made by film/take_kde.py: pick a new folder")
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    sentinel.write_text("")
    qt = WORK / "qt"
    environment, _ = rs.prepare(qt, PLASMOID)
    environment.update(QT_SCALE_FACTOR=str(DPR), XDG_STATE_HOME=str(qt / "state"))
    (qt / "shots").mkdir()
    wallpaper(qt / "wallpaper.png")
    shadow(qt / "shadow.png")
    data = film_fixture()
    (WORK / "fixture.json").write_text(json.dumps(data, indent=2) + "\n")
    terms = [{**term, "lines": [[text, TERM_COLORS[color], bold] for text, color, bold in term["lines"]]}
             for term in TERMS + restored_terms(data["agentSnapshot"]["history"])]
    helpers = rs.display_helpers([*rs.HELPERS, "pacePercent", "projectedPercent", "featuredAgent"])
    host = HOST
    for key, value in {
        "@WIDTH@": SCREEN[0], "@HEIGHT@": SCREEN[1], "@PANEL@": PANEL_H, "@POPUP_W@": POPUP[0], "@POPUP_H@": POPUP[1],
        "@NOW@": NOW_MS, "@TRANSITION@": TRANSITION_MS, "@SNAPSHOT@": json.dumps(data["snapshot"]),
        "@AGENTS@": json.dumps(data["agentSnapshot"]), "@TERMS@": json.dumps(terms), "@HELPERS@": helpers,
    }.items():
        host = host.replace(key, str(value))
    (qt / "host.qml").write_text(host)
    process = subprocess.run(["qml6", "--apptype", "widget", str(qt / "host.qml")], cwd=qt, env=environment,
                             capture_output=True, text=True, timeout=240)
    log = process.stdout + process.stderr
    (WORK / "take.log").write_text(log)
    problems = [line for line in log.splitlines() if re.search(r"ReferenceError|TypeError|Error:|Unable to assign", line)]
    if process.returncode or problems:
        sys.exit(f"take failed (exit {process.returncode}); see {WORK / 'take.log'}\n" + "\n".join(problems[:20]))

    def scaled(event):
        for key in ("x", "y"):
            if key in event:
                event[key] *= DPR
        if "rect" in event:
            event["rect"] = [v * DPR for v in event["rect"]]
        return event

    events = [scaled(json.loads(line)) for line in re.findall(r"EVENT (.+)", log)]
    frames = [json.loads(line) for line in re.findall(r"FRAME (.+)", log)]
    with Pool() as pool:
        pool.map(to_png, [qt / frame["path"] for frame in frames])
    for frame in frames:
        frame["path"] = str((qt / frame["path"]).with_suffix(".png"))
    size = Image.open(frames[0]["path"]).size
    if size != (round(SCREEN[0] * DPR), round(SCREEN[1] * DPR)):
        sys.exit(f"shots are {size}, expected the desktop at {DPR}x")
    timeline = {
        "transitionMs": TRANSITION_MS,
        "devices": [{"surface": "desktop", "kind": "display", "size": list(size)}],
        "frames": {"desktop": frames},
        "events": events,
    }
    (WORK / "timeline.json").write_text(json.dumps(timeline, indent=1))
    seconds = (events[-1]["t"] - events[0]["t"]) / 1000
    print(f"{len(frames)} frames over {seconds:.1f} s -> {WORK / 'timeline.json'}")


if __name__ == "__main__":
    main()
