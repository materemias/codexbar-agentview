import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PC3
import org.kde.kirigami as Kirigami
import "Filter.js" as Filter

ColumnLayout {
    id: agents
    spacing: Kirigami.Units.smallSpacing

    readonly property var snap: root.agentSnapshot
    readonly property var counts: snap && snap.counts ? snap.counts : ({})
    readonly property var list: snap && Array.isArray(snap.agents) ? snap.agents : []
    readonly property var recordsByKey: {
        var out = {}
        for (var i = 0; i < list.length; i++) {
            if (list[i]) out[agentKey(list[i])] = list[i]
        }
        return out
    }
    readonly property bool hasSomething: (counts.total || 0) > 0
    readonly property bool showPrompts: Plasmoid.configuration.showAgentPrompts === true
    // Header eye switch: hide sessions in their state for over a day.
    // Working and blocked sessions always stay, and a filter searches the
    // hidden ones too.
    readonly property bool hideOld: Plasmoid.configuration.hideOldAgents === true
    readonly property int oldCount: list.filter(isOld).length

    function isOld(a) {
        var since = Number(a && a.stateChangedAt) || 0
        return since > 0 && root.nowMs - since > 86400000
            && a.state !== "working" && a.state !== "blocked"
    }

    function recordShown(a) {
        if (filterWords.length) return recordMatches(a)
        return !!a && !(hideOld && isOld(a))
    }

    // Cluster sessions by cwd. Groups are sorted alphabetically by folder name,
    // and rows within each group are newest first by stateChangedAt.
    // Groups ignore the filter: typing toggles row visibility instead of
    // rebuilding every delegate on each keystroke.
    readonly property var groups: {
        var byFolder = {}
        var order = []
        var src = agents.list
        for (var i = 0; i < src.length; i++) {
            var a = src[i]
            if (!a) continue
            var folder = root.cwdLabel(a.cwd || "") || (a.provider || "agent")
            var g = byFolder[folder]
            if (!g) {
                g = { folder: folder, sessions: [] }
                byFolder[folder] = g
                order.push(folder)
            }
            g.sessions.push(a)
        }
        var arr = order.map(function(f) { return byFolder[f] })
        for (var j = 0; j < arr.length; j++) {
            arr[j].sessions.sort(function(a, b) {
                var aTime = Number(a.stateChangedAt) || 0
                var bTime = Number(b.stateChangedAt) || 0
                if (aTime !== bTime) return bTime - aTime
                var aId = String(a.sessionId || "")
                var bId = String(b.sessionId || "")
                return aId < bId ? -1 : aId > bId ? 1 : 0
            })
        }

        arr.sort(function(a, b) {
            return a.folder.toLowerCase().localeCompare(b.folder.toLowerCase())
        })
        return arr
    }

    // Keep identity across poll-driven reordering. A vanished selection
    // clears instead of silently targeting the next session at its index.
    property string selectedKey: ""
    readonly property int selectedIndex: indexForKey(selectedKey)
    readonly property var flatAgents: {
        var out = []
        for (var i = 0; i < groups.length; i++) {
            for (var j = 0; j < groups[i].sessions.length; j++) {
                var a = groups[i].sessions[j]
                if (agents.recordShown(a)) out.push(a)
            }
        }
        return out
    }

    // Composite provider/session identity survives fresh JSON objects.
    property string peekKey: ""

    // Type-to-filter: printable keys typed while the popup has focus
    // accumulate here (FullRepresentation forwards them). Empty = no filter.
    property string filterText: ""
    readonly property var filterWords: Filter.words(filterText)

    // Matching rules are in Filter.js. Every match is shown: highlighted in
    // a visible label, or as a line in the row's snippet panel.
    readonly property var _fields: [
        { name: "windowTitle", label: "title" },
        { name: "lastPrompt", label: "prompt" },
        { name: "cwd", label: "cwd" },
        { name: "provider", label: "provider" },
        { name: "model", label: "model" }
    ]

    function _fieldText(record, name) {
        var value = name === "model" ? _modelName(record.model) : record[name]
        return String(value || "").replace(/\s+/g, " ")
    }

    // The recent prose turns, whitespace collapsed. Tool-call summaries are
    // tool names, not conversation, so a filter never matches them.
    function _textTurns(record) {
        return (record.recent || []).filter(function(t) { return t.kind !== "tools" })
            .map(function(t) {
                return { user: t.role === "user", text: String(t.text || "").replace(/\s+/g, " ") }
            })
    }

    function recordMatches(a) {
        if (!filterWords.length) return true
        if (!a) return false
        return Filter.matchesAll(_fields.map(function(f) { return _fieldText(a, f.name) }),
            _textTurns(a).map(function(t) { return t.text }), filterWords)
    }

    // A visible label: StyledText with the filter matches highlighted while
    // a filter is active, otherwise the text unchanged (render PlainText).
    function highlighted(text) {
        var t = String(text || "")
        if (!filterText) return t
        var norm = t.replace(/\s+/g, " ")
        return Filter.styled(norm, Filter.flat(Filter.hits(norm, filterWords, true)), 0, norm.length)
    }

    function titleParts(text) {
        var t = String(text || "")
        var q = filterText.length > 0
        if (q) t = t.replace(/\s+/g, " ")
        var code = t.codePointAt(0)
        var privateUse = (code >= 0xe000 && code <= 0xf8ff)
            || (code >= 0xf0000 && code <= 0xffffd)
            || (code >= 0x100000 && code <= 0x10fffd)
        var iconEnd = privateUse ? (code > 0xffff ? 2 : 1) : 0
        var labelStart = iconEnd
        if (privateUse) {
            while (labelStart < t.length && /\s/.test(t[labelStart])) labelStart++
        }
        var positions = q ? Filter.flat(Filter.hits(t, filterWords, true)) : null
        return {
            glyph: t.slice(0, iconEnd),
            icon: q ? Filter.styled(t, positions, 0, iconEnd) : t.slice(0, iconEnd),
            label: q ? Filter.styled(t, positions, labelStart, t.length) : t.slice(labelStart)
        }
    }

    // The record field a session row shows as its task label.
    function labelField(record) {
        if (record.windowTitle) return "windowTitle"
        if (showPrompts && record.lastPrompt) return "lastPrompt"
        return "provider"
    }

    // The fields in `[[name, label], …]` whose label shows its full text.
    // An elided label can hide the match, so its field gets a snippet line.
    function shownFields(entries) {
        return entries.filter(function(e) { return !e[1].truncated })
            .map(function(e) { return e[0] })
    }

    // While a filter is active: StyledText lines showing where every query
    // word matched beyond the fields the row shows (names in `shown`).
    function filterSnippets(record, shown) {
        if (!filterWords.length || !record) return []
        return Filter.snippets(_fields.map(function(f) {
            return { name: f.name, label: f.label, text: _fieldText(record, f.name),
                shown: shown.indexOf(f.name) !== -1 }
        }), _textTurns(record), filterWords)
    }

    // Model family for display and filtering: provider path and the
    // redundant "claude-" vendor prefix dropped (claude-opus-5-5 -> opus-5-5).
    function _modelName(model) {
        if (typeof model !== "string") return ""
        var value = model.trim()
        var slash = value.lastIndexOf("/")
        if (slash >= 0) value = value.slice(slash + 1)
        return value.replace(/^claude-/, "")
    }

    // Age brightness by recency: the last 15 minutes full, hours muted,
    // a day or more dim.
    function ageOpacity(ms) {
        if (!ms) return 0.55
        var age = root.nowMs - ms
        return age < 900000 ? 1 : age < 86400000 ? 0.7 : 0.5
    }

    // State marker, distinct by shape as well as color: working is a
    // filled dot with an expanding halo, blocked a diamond, idle a hollow
    // ring (green and breathing while `fresh`), untracked a filled dot.
    component StateMark: Item {
        id: mark
        property string agentState: "idle"
        property bool fresh: false
        property bool animated: true
        property real size: 10
        readonly property color tint: fresh
            ? Kirigami.Theme.positiveTextColor : root.agentStateColor(agentState)
        implicitWidth: size
        implicitHeight: size

        Rectangle {
            id: halo
            property real t: 0
            anchors.centerIn: parent
            width: mark.size; height: mark.size; radius: width / 2
            color: "transparent"
            border.width: 1.5
            border.color: mark.tint
            visible: mark.animated && mark.agentState === "working"
            scale: 1 + t * 1.1
            opacity: 0.8 * (1 - t)
            NumberAnimation on t {
                running: halo.visible
                loops: Animation.Infinite
                from: 0; to: 1
                duration: 1400
                easing.type: Easing.OutQuad
            }
        }

        Rectangle {
            id: core
            readonly property bool diamond: mark.agentState === "blocked"
            readonly property bool ring: mark.agentState === "idle"
            property real pulse: 0
            anchors.centerIn: parent
            width: diamond ? mark.size * 0.78 : mark.size
            height: width
            radius: diamond ? 1.5 : width / 2
            rotation: diamond ? 45 : 0
            color: ring ? "transparent" : mark.tint
            border.width: ring ? 1.5 : 0
            border.color: mark.tint
            opacity: mark.fresh ? 1 - pulse * 0.6 : 1
            SequentialAnimation on pulse {
                running: mark.animated && mark.fresh
                loops: Animation.Infinite
                alwaysRunToEnd: true
                NumberAnimation { to: 1; duration: 700; easing.type: Easing.InOutSine }
                NumberAnimation { to: 0; duration: 700; easing.type: Easing.InOutSine }
            }
        }
    }

    function agentKey(record) {
        if (!record || !record.provider || !record.sessionId) return ""
        return JSON.stringify([record.provider, record.sessionId])
    }

    function indexForKey(key) {
        if (!key) return -1
        for (var i = 0; i < flatAgents.length; i++) {
            if (agentKey(flatAgents[i]) === key) return i
        }
        return -1
    }

    function togglePeek() {
        if (indexForKey(selectedKey) < 0) return
        peekKey = peekKey === selectedKey ? "" : selectedKey
    }

    onFlatAgentsChanged: {
        if (selectedKey && indexForKey(selectedKey) < 0) selectedKey = ""
        if (peekKey && indexForKey(peekKey) < 0) peekKey = ""
    }


    function selectAt(index) {
        if (index < 0 || index >= flatAgents.length) return
        var followPeek = peekKey !== ""
        selectedKey = agentKey(flatAgents[index])
        if (followPeek) peekKey = selectedKey
    }
    function selectNext() {
        if (flatAgents.length === 0) return
        selectAt((selectedIndex + 1) % flatAgents.length)
    }
    function selectPrevious() {
        if (flatAgents.length === 0) return
        var n = flatAgents.length
        selectAt(((selectedIndex < 0 ? 0 : selectedIndex) - 1 + n) % n)
    }
    function activateSelected() {
        var index = indexForKey(selectedKey)
        if (index < 0) return
        var a = flatAgents[index]
        if (a && a.sessionId) {
            Qt.openUrlExternally("codexbar-agentview://focus/" + a.sessionId)
        }
    }

    // Header row: section label + count summary on the right.
    RowLayout {
        Layout.fillWidth: true
        spacing: Kirigami.Units.smallSpacing

        PC3.Label {
            text: "ACTIVE AGENTS"
            font.weight: Font.Bold
            font.pixelSize: Kirigami.Theme.defaultFont.pixelSize * 1.02
            font.letterSpacing: 0.4
            Layout.alignment: Qt.AlignVCenter
        }

        Item { Layout.fillWidth: true }

        // Count chips. Render only non-zero states so the row stays compact
        // when most agents are idle.
        Repeater {
            model: ["blocked", "working", "idle", "untracked"]
            delegate: RowLayout {
                required property string modelData
                readonly property int v: agents.counts[modelData] || 0
                visible: v > 0
                spacing: 3
                Layout.alignment: Qt.AlignVCenter

                StateMark {
                    agentState: modelData
                    animated: false
                    size: 8
                    Layout.alignment: Qt.AlignVCenter
                }
                PC3.Label {
                    text: v + " " + modelData
                    font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                    opacity: 0.85
                    Layout.alignment: Qt.AlignVCenter
                    rightPadding: Kirigami.Units.smallSpacing
                }
            }
        }

        PC3.ToolButton {
            readonly property string tip: (agents.hideOld
                ? "Show " + agents.oldCount + " sessions older than a day"
                : "Hide sessions older than a day") + " (Tab)"
            visible: agents.hasSomething
            checkable: true
            checked: agents.hideOld
            onToggled: Plasmoid.configuration.hideOldAgents = checked
            icon.name: "view-hidden"
            icon.width: Kirigami.Units.iconSizes.small
            icon.height: Kirigami.Units.iconSizes.small
            text: agents.hideOld && agents.oldCount > 0 ? String(agents.oldCount) : ""
            display: QQC2.AbstractButton.TextBesideIcon
            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            Layout.alignment: Qt.AlignVCenter
            Accessible.name: tip
            QQC2.ToolTip.text: tip
            QQC2.ToolTip.visible: hovered
            QQC2.ToolTip.delay: Kirigami.Units.toolTipDelay
        }

        PC3.Label {
            visible: !agents.hasSomething
            text: "none"
            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            opacity: 0.55
        }
    }

    // Active filter query + match count. Only shown while typing.
    RowLayout {
        visible: agents.filterText.length > 0
        Layout.fillWidth: true
        spacing: Kirigami.Units.smallSpacing

        PC3.Label {
            text: "filter: " + agents.filterText
            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            opacity: 0.85
            Layout.fillWidth: true
        }
        PC3.Label {
            text: agents.flatAgents.length
                + (agents.flatAgents.length === 1 ? " match" : " matches")
                + " · Esc clears"
            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            opacity: 0.6
        }
    }

    PC3.Label {
        visible: root.agentsError && root.agentsError.length > 0
        Layout.fillWidth: true
        wrapMode: Text.WordWrap
        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
        color: Kirigami.Theme.negativeTextColor
        text: root.agentsError
    }

    PC3.Label {
        visible: root.teleportError.length > 0
        Layout.fillWidth: true
        wrapMode: Text.WordWrap
        textFormat: Text.PlainText
        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
        color: Kirigami.Theme.negativeTextColor
        text: root.teleportError
    }

    // Folder-grouped session rows. Each group is a card: a small
    // uppercase folder header followed by its session rows.
    Repeater {
        // Folder names and session keys, not records: a Repeater rebuilds
        // every delegate when its model's content changes, and records
        // change on every turn. Rows look their record up by key instead.
        model: agents.groups.map(function(g) { return g.folder })
        delegate: SectionCard {
            id: groupItem
            required property int index
            readonly property var group: agents.groups[index] || { folder: "", sessions: [] }
            title: groupItem.group.folder
            visible: groupItem.group.sessions.some(function(s) {
                return agents.recordShown(s)
            })

            Repeater {
                model: groupItem.group.sessions.map(agents.agentKey)
                delegate: sessionRow
            }
        }
    }

    // Per-session row delegate — single line. Folder name is already shown
    // as the group header, so the row's primary label is the task title.
    Component {
        id: sessionRow
        Item {
            id: rowItem
            Layout.fillWidth: true
            // The session key; `record` follows the latest snapshot.
            required property string modelData
            readonly property var record: agents.recordsByKey[modelData] || ({})
            visible: agents.recordShown(record)

            implicitHeight: rowCol.implicitHeight + 4
            readonly property string sessionKey: modelData
            readonly property bool peekOpen: sessionKey !== ""
                && sessionKey === agents.peekKey

            readonly property var filterSnippets: agents.filterSnippets(record,
                agents.shownFields([[agents.labelField(record), taskText],
                    ["model", modelText]]))

            // Row + optional peek panel stacked. The panel grows inside the
            // popup's ScrollView when open; only one row peeks at a time
            // (agents.peekKey), so height stays bounded.
            ColumnLayout {
                id: rowCol
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                spacing: 0

                // Right-hand fields sit in fixed-width columns so they line
                // up across rows; the desktop chip ends at the row's edge.
                RowLayout {
                    id: rowContent
                    Layout.fillWidth: true
                    Layout.leftMargin: 4
                    Layout.rightMargin: 4
                    Layout.preferredHeight: Kirigami.Units.iconSizes.smallMedium + 6
                    spacing: Kirigami.Units.smallSpacing
                    opacity: rowItem.staleIdle ? 0.55 : 1

                    // State dot. A thin ring around it marks the session
                    // shown in the focused window (root.focusedAgentKey);
                    // the slot keeps the ring's size so rows stay aligned.
                    Item {
                        implicitWidth: 16; implicitHeight: 16
                        Layout.alignment: Qt.AlignVCenter

                        Rectangle {
                            anchors.fill: parent
                            radius: width / 2
                            color: "transparent"
                            border.width: 1.5
                            border.color: Kirigami.Theme.textColor
                            opacity: 0.75
                            visible: rowItem.sessionKey !== ""
                                && rowItem.sessionKey === root.focusedAgentKey
                        }

                        StateMark {
                            anchors.centerIn: parent
                            agentState: rowItem.state
                            fresh: rowItem.recentlyIdle
                        }
                    }

                    Kirigami.Icon {
                        source: rowItem.record.provider
                            ? Qt.resolvedUrl("../icons/" + rowItem.record.provider + ".svg")
                            : ""
                        implicitWidth: Kirigami.Units.iconSizes.small
                        implicitHeight: Kirigami.Units.iconSizes.small
                        smooth: true
                        visible: source.toString().length > 0
                        Layout.alignment: Qt.AlignVCenter
                    }

                    AgentTitle {
                        id: taskText
                        parts: agents.titleParts(rowItem.taskLabel)
                        textFormat: agents.filterText ? Text.StyledText : Text.PlainText
                        Layout.fillWidth: true
                        Layout.alignment: Qt.AlignVCenter
                    }

                    PC3.Label {
                        id: modelText
                        text: agents.highlighted(agents._modelName(rowItem.record.model))
                        textFormat: agents.filterText ? Text.StyledText : Text.PlainText
                        color: root.modelTint(rowItem.record.model)
                        opacity: 0.85
                        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                        elide: Text.ElideMiddle
                        horizontalAlignment: Text.AlignRight
                        Layout.preferredWidth: Kirigami.Units.gridUnit * 5
                        Layout.alignment: Qt.AlignVCenter
                    }

                    PC3.Label {
                        text: rowItem.record.host || ""
                        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                        opacity: 0.55
                        elide: Text.ElideRight
                        Layout.preferredWidth: Kirigami.Units.gridUnit * 3
                        Layout.alignment: Qt.AlignVCenter
                    }

                    PC3.Label {
                        text: root.ageFrom(rowItem.record.stateChangedAt, root.nowMs)
                        opacity: agents.ageOpacity(rowItem.record.stateChangedAt)
                        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                        font.weight: opacity === 1 ? Font.DemiBold : Font.Normal
                        horizontalAlignment: Text.AlignRight
                        Layout.preferredWidth: Kirigami.Units.gridUnit * 2
                        Layout.alignment: Qt.AlignVCenter
                    }

                    // Desktop chip: the virtual desktop hosting this
                    // session's terminal window. Highlighted when that
                    // desktop (or "all" desktops) is the current one;
                    // absent when no window resolved, its slot kept so
                    // the columns stay aligned.
                    Item {
                        implicitWidth: Math.max(Kirigami.Units.gridUnit * 1.4,
                            desktopChip.width)
                        implicitHeight: desktopChip.height
                        Layout.alignment: Qt.AlignVCenter

                        Rectangle {
                            id: desktopChip
                            anchors.right: parent.right
                            visible: rowItem.desktopInfo !== null
                            width: desktopChipLabel.implicitWidth + 8
                            height: desktopChipLabel.implicitHeight + 3
                            radius: 3
                            // Current desktop: a soft fill instead of the
                            // solid highlight, so a column of them stays quiet.
                            color: rowItem.desktopInfo && rowItem.desktopInfo.onCurrent
                                ? Qt.rgba(Kirigami.Theme.highlightColor.r,
                                    Kirigami.Theme.highlightColor.g,
                                    Kirigami.Theme.highlightColor.b, 0.3)
                                : "transparent"
                            border.width: 1
                            border.color: rowItem.desktopInfo && rowItem.desktopInfo.onCurrent
                                ? Qt.rgba(Kirigami.Theme.highlightColor.r,
                                    Kirigami.Theme.highlightColor.g,
                                    Kirigami.Theme.highlightColor.b, 0.6)
                                : Qt.rgba(Kirigami.Theme.textColor.r,
                                    Kirigami.Theme.textColor.g,
                                    Kirigami.Theme.textColor.b, 0.3)

                            PC3.Label {
                                id: desktopChipLabel
                                anchors.centerIn: parent
                                text: rowItem.desktopInfo ? rowItem.desktopInfo.label : ""
                                font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                                font.weight: Font.DemiBold
                                color: Kirigami.Theme.textColor
                                opacity: rowItem.desktopInfo && rowItem.desktopInfo.onCurrent
                                    ? 0.9 : 0.55
                            }
                        }
                    }
                }

                // The agent's latest recap (omp when a session goes idle,
                // Claude when you return to one); "Earlier:" marks one that
                // turns have since followed.
                PC3.Label {
                    visible: !!rowItem.record.recap && rowItem.selected
                    Layout.fillWidth: true
                    Layout.leftMargin: 26
                    Layout.rightMargin: 4
                    Layout.bottomMargin: 2
                    text: (rowItem.record.recapStale ? "Earlier: " : "")
                        + (rowItem.record.recap || "")
                    textFormat: Text.PlainText
                    wrapMode: Text.WordWrap
                    maximumLineCount: 3
                    elide: Text.ElideRight
                    font.italic: true
                    font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                    opacity: 0.6
                }

                // Filter-hit snippet panel: the conversation lines that
                // matched the active filter, query highlighted.
                Rectangle {
                    visible: agents.filterText.length > 0
                        && rowItem.filterSnippets.length > 0
                    Layout.fillWidth: true
                    Layout.leftMargin: 18
                    Layout.topMargin: 2
                    implicitHeight: snipCol.implicitHeight + 8
                    radius: 4
                    color: Kirigami.Theme.backgroundColor
                    border.color: Qt.rgba(
                        Kirigami.Theme.textColor.r,
                        Kirigami.Theme.textColor.g,
                        Kirigami.Theme.textColor.b,
                        0.14
                    )
                    border.width: 1

                    ColumnLayout {
                        id: snipCol
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.margins: 4
                        spacing: 1

                        Repeater {
                            model: rowItem.filterSnippets
                            delegate: PC3.Label {
                                required property string modelData
                                Layout.fillWidth: true
                                text: modelData
                                textFormat: Text.StyledText
                                wrapMode: Text.WordWrap
                                maximumLineCount: 2
                                elide: Text.ElideRight
                                font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                                opacity: 0.85
                            }
                        }
                    }
                }

                // Inline peek panel: the last few turns of the session,
                // straight from its transcript via the aggregator's
                // `recent` field. Reads like a chat: your turns in tinted
                // bubbles on the right, the agent's as plain text, tool
                // runs as one muted line.
                Rectangle {
                    visible: rowItem.peekOpen
                    Layout.fillWidth: true
                    Layout.leftMargin: 26
                    Layout.rightMargin: 4
                    Layout.topMargin: 2
                    Layout.bottomMargin: 6
                    implicitHeight: peekCol.implicitHeight + 16
                    radius: 6
                    color: Qt.rgba(0, 0, 0, 0.18)

                    ColumnLayout {
                        id: peekCol
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.margins: 8
                        spacing: 6

                        PC3.Label {
                            Layout.fillWidth: true
                            text: (rowItem.record.cwd || "").replace(/^\/home\/[^\/]+/, "~")
                                + (rowItem.record.host ? "  ·  " + rowItem.record.host : "")
                            textFormat: Text.PlainText
                            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                            opacity: 0.5
                            elide: Text.ElideMiddle
                        }

                        Repeater {
                            model: rowItem.record.recent || []
                            delegate: TurnBubble {}
                        }

                        PC3.Label {
                            visible: ((rowItem.record.recent || []).length === 0)
                            text: "no messages captured yet"
                            textFormat: Text.PlainText
                            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                            opacity: 0.7
                        }
                    }

                    // Swallow clicks so clicking inside the panel doesn't
                    // focus the terminal — reading shouldn't teleport.
                    MouseArea {
                        anchors.fill: parent
                        hoverEnabled: true
                    }
                }
            }

            readonly property bool selected: sessionKey !== ""
                && sessionKey === agents.selectedKey

            // 1.0 at the moment a session goes idle, linearly down to 0 at
            // five minutes. Drives the "just finished" green background wash.
            readonly property real _idleFreshness: {
                if (rowItem.state !== "idle") return 0
                var since = rowItem.record.stateChangedAt || 0
                if (!since) return 0
                var age = root.nowMs - since
                return age >= 300000 ? 0 : (300000 - age) / 300000
            }

            // State wash: working green and blocked red while in that
            // state; "just finished" green fading over five minutes. Sits
            // below the selection/hover highlight so both can apply.
            Rectangle {
                z: -1
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                height: rowContent.implicitHeight + 4
                radius: 4
                color: root.agentStateColor(rowItem.state === "blocked" ? "blocked" : "working")
                opacity: rowItem.state === "working" ? 0.09
                    : rowItem.state === "blocked" ? 0.12
                    : rowItem._idleFreshness * 0.08
            }

            Rectangle {
                z: -1
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                height: rowContent.implicitHeight + 4
                radius: 4
                color: Kirigami.Theme.alternateBackgroundColor
                opacity: rowItem.selected ? 0.1
                    : rowMouse.containsMouse ? 0.04 : 0
                Behavior on opacity { NumberAnimation { duration: 120 } }
            }

            Rectangle {
                anchors.left: parent.left
                anchors.top: parent.top
                width: 3
                height: rowContent.implicitHeight + 4
                radius: 1
                color: rowItem.tint
                opacity: rowItem.selected || rowItem.state === "working"
                    || rowItem.state === "blocked" ? 1
                    : rowMouse.containsMouse ? 0.7 : 0
                Behavior on opacity { NumberAnimation { duration: 120 } }
            }

            readonly property string state: record.state || "idle"
            readonly property color tint: root.agentStateColor(state)
            // Desktop badge data for this row, or null when the host
            // window didn't resolve against Plasma's task list.
            readonly property var desktopInfo: root.desktopInfoFor(record)
            // Prefer the agent-generated session title. Only fall through
            // to the raw user prompt when the user has opted into showing
            // it (otherwise the row just shows the provider name).
            readonly property string taskLabel:
                record[agents.labelField(record)] || "agent"

            // "Just finished" highlight: true while the existing freshness
            // value remains above zero.
            readonly property bool recentlyIdle: _idleFreshness > 0

            // Idle for over a day: the row content is dimmed.
            readonly property bool staleIdle: state === "idle"
                && (record.stateChangedAt || 0) > 0
                && root.nowMs - record.stateChangedAt >= 86400000

            MouseArea {
                id: rowMouse
                // Only the single-line row is clickable-to-focus; the peek
                // panel below swallows its own clicks.
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                height: rowContent.implicitHeight + 6
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                acceptedButtons: Qt.LeftButton
                onClicked: {
                    if (!rowItem.record.sessionId) return
                    Qt.openUrlExternally(
                        "codexbar-agentview://focus/" + rowItem.record.sessionId
                    )
                }
            }

            // Row actions float over the row's right end, so they take no
            // column space; shown on hover and while the peek is open.
            Rectangle {
                readonly property bool shown: rowMouse.containsMouse
                    || peekButton.hovered || teleportButton.hovered || rowItem.peekOpen
                anchors.right: parent.right
                anchors.rightMargin: 4
                y: (rowContent.height - height) / 2
                width: rowActions.implicitWidth
                height: rowActions.implicitHeight
                radius: 4
                color: Kirigami.Theme.backgroundColor
                opacity: shown ? 1 : 0
                visible: opacity > 0
                Behavior on opacity { NumberAnimation { duration: 120 } }

                Row {
                    id: rowActions

                    // Moves a live kitty session into a new Tern tab.
                    PC3.ToolButton {
                        id: teleportButton
                        readonly property bool idle: rowItem.state === "idle"
                        visible: root.canTeleport(rowItem.record)
                        enabled: !root.teleports[rowItem.sessionKey]
                        opacity: idle ? 1 : 0.4
                        icon.name: "tab-new"
                        implicitWidth: Kirigami.Units.iconSizes.smallMedium + 6
                        implicitHeight: Kirigami.Units.iconSizes.smallMedium + 6
                        padding: 1
                        onClicked: root.teleportAgent(rowItem.record)
                        PC3.ToolTip.visible: hovered
                        PC3.ToolTip.text: idle
                            ? "Teleport to a new Tern tab (closes this kitty window)"
                            : "Teleport to Tern once the session is idle"
                        PC3.ToolTip.delay: 400
                    }

                    PC3.ToolButton {
                        id: peekButton
                        icon.name: rowItem.peekOpen ? "arrow-up" : "arrow-down"
                        implicitWidth: Kirigami.Units.iconSizes.smallMedium + 6
                        implicitHeight: Kirigami.Units.iconSizes.smallMedium + 6
                        padding: 1
                        onClicked: {
                            agents.peekKey = rowItem.peekOpen ? "" : rowItem.sessionKey
                        }
                        PC3.ToolTip.visible: hovered
                        PC3.ToolTip.text: "Peek at recent messages (or press Space)"
                        PC3.ToolTip.delay: 400
                    }
                }
            }

            PC3.ToolTip.visible: rowMouse.containsMouse
                && (rowItem.record.cwd || "").length > 0
            PC3.ToolTip.text: (rowItem.record.cwd || "")
                + (rowItem.record.host ? "  (" + rowItem.record.host + ")" : "")
                + "\nclick to focus · peek button or Space for recent messages"
            PC3.ToolTip.delay: 600
        }
    }

    // Untracked summary row — shown only when untracked count > 0 since these
    // sessions have no per-record info.
    RowLayout {
        visible: (agents.counts.untracked || 0) > 0
        Layout.fillWidth: true
        Layout.topMargin: 2
        spacing: Kirigami.Units.smallSpacing

        Rectangle {
            width: 10; height: 10; radius: 5
            color: root.agentStateColor("untracked")
            Layout.alignment: Qt.AlignVCenter
        }
        PC3.Label {
            text: (agents.counts.untracked || 0) + " more running (no hook sentinel)"
            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            opacity: 0.7
            Layout.fillWidth: true
        }
    }
}
