import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import "process" as Process
import "Command.js" as Command

Item {
    id: root

    property var cfg_trayIndicators: []
    property var cfg_trayOverPaceOnly: []
    property int cfg_compactStyle: 0
    property int cfg_trayIconSize: 22
    property string cfg_cliPath: "/usr/bin/codexbar"

    // Accounts with an email, per provider, from the last saved usage.
    property var accounts: ({ claude: [], codex: [] })
    property bool accountsLoading: true
    property string accountsError: ""
    property bool initialized: false
    property string activeCommand: ""
    property string activeCliPath: ""
    readonly property string cliPath: Command.cliPath(cfg_cliPath)
    onCliPathChanged: {
        if (!initialized) return
        accounts = { claude: [], codex: [] }
        accountsError = ""
        accountsLoading = true
        loadAccounts()
    }
    readonly property string fetchScriptPath: Command.localPath(
        Qt.resolvedUrl("../scripts/codexbar_fetch.py"))
    implicitWidth: Kirigami.Units.gridUnit * 22
    implicitHeight: Kirigami.Units.gridUnit * 22

    function _has(key) {
        return cfg_trayIndicators && cfg_trayIndicators.indexOf(key) >= 0
    }
    function _toggle(key, on) {
        var current = cfg_trayIndicators ? cfg_trayIndicators.slice() : []
        var idx = current.indexOf(key)
        if (on && idx < 0) current.push(key)
        if (!on && idx >= 0) current.splice(idx, 1)
        cfg_trayIndicators = current
    }
    function _overPaceOnly(key) {
        return cfg_trayOverPaceOnly && cfg_trayOverPaceOnly.indexOf(key) >= 0
    }
    function _toggleOverPaceOnly(key, on) {
        var current = cfg_trayOverPaceOnly ? cfg_trayOverPaceOnly.slice() : []
        var idx = current.indexOf(key)
        if (on && idx < 0) current.push(key)
        if (!on && idx >= 0) current.splice(idx, 1)
        cfg_trayOverPaceOnly = current
    }

    // One tray meter: the main on/off box plus, while on, a secondary box
    // that hides the meter until its projected usage at reset passes 100%.
    component MeterCheck: RowLayout {
        id: meter
        property string key
        property string text
        property bool isOn: root._has(key)
        property var setOn: function (on) { root._toggle(meter.key, on) }
        spacing: Kirigami.Units.smallSpacing

        QQC2.CheckBox {
            text: meter.text
            checked: meter.isOn
            onToggled: meter.setOn(checked)
        }
        QQC2.CheckBox {
            visible: meter.isOn
            text: "only if proj > 100%"
            opacity: 0.7
            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            checked: root._overPaceOnly(meter.key)
            onToggled: root._toggleOverPaceOnly(meter.key, checked)
            QQC2.ToolTip.text: "Hide this meter from the tray unless the current rate projects above 100% at reset"
            QQC2.ToolTip.visible: hovered
            QQC2.ToolTip.delay: Kirigami.Units.toolTipDelay
        }
    }

    // Per-account meters for the providers that can have several accounts:
    // one block per account with its own keys `<provider>:<encoded email>:<window>`.
    // Without known accounts the provider falls back to legacy
    // `<provider>:<window>` keys, which cover every account.
    component AccountMeters: ColumnLayout {
        id: meters
        property string providerId
        readonly property bool ready: root._ready(providerId)
        spacing: ready ? Kirigami.Units.smallSpacing : 0

        Repeater {
            model: meters.ready ? [] : root._fallbackWindows(meters.providerId)
            delegate: MeterCheck {
                id: fallbackWindow
                required property var modelData
                key: meters.providerId + ":" + fallbackWindow.modelData.id
                text: fallbackWindow.modelData.text
                isOn: root._allChecked(meters.providerId, fallbackWindow.modelData.id)
                setOn: function (on) {
                    root._toggleAll(meters.providerId, fallbackWindow.modelData.id, on)
                }
            }
        }

        Repeater {
            model: meters.ready ? root.accounts[meters.providerId] : []
            delegate: ColumnLayout {
                id: accountGroup
                required property var modelData
                spacing: 0

                QQC2.Label {
                    text: accountGroup.modelData.accountEmail
                        + (accountGroup.modelData.loginMethod
                           ? " · " + accountGroup.modelData.loginMethod : "")
                    font.weight: Font.DemiBold
                    opacity: 0.8
                }

                Repeater {
                    model: root._accountWindows(accountGroup.modelData)
                    delegate: MeterCheck {
                        id: accountWindow
                        required property var modelData
                        key: root._accountKey(accountGroup.modelData, accountWindow.modelData.id)
                        text: accountWindow.modelData.text
                        isOn: root._accountChecked(
                            accountGroup.modelData, accountWindow.modelData.id)
                        setOn: function (on) {
                            root._toggleAccount(
                                accountGroup.modelData, accountWindow.modelData.id, on)
                        }
                    }
                }
            }
        }
    }

    readonly property var _windowLabels: ({
        claude: {
            "primary": "5h session",
            "secondary": "7d weekly (all models)",
            "tertiary": "7d weekly (Sonnet)",
            "claude-design": "Claude Design",
            "claude-routines": "Daily Routines"
        },
        codex: {
            "primary": "5h session",
            "secondary": "7d weekly",
            "tertiary": "Additional limit",
            "codex-base-model-inference": "Reserve 7d",
            "gpt-reserve": "Reserve 7d"
        }
    })
    // Windows offered when no account is known.
    readonly property var _fallbackSlots: ({
        claude: ["primary", "secondary", "tertiary", "claude-design", "claude-routines"],
        codex: ["primary", "secondary"]
    })

    function _windowLabel(providerId, id, fallback) {
        var labels = _windowLabels[providerId] || {}
        return labels[id] || fallback || id
    }

    function _ready(providerId) {
        return !accountsLoading && accountsError.length === 0
            && (accounts[providerId] || []).length > 0
    }

    function _accountWindows(account) {
        var rows = []
        var slots = ["primary", "secondary", "tertiary"]
        for (var i = 0; i < slots.length; i++) {
            var window = account[slots[i]]
            if (window && window.usedPercent !== undefined && window.usedPercent !== null) {
                rows.push({ id: slots[i], text: _windowLabel(account.id, slots[i]) })
            }
        }
        var extras = account.extraRateWindows || []
        for (var e = 0; e < extras.length; e++) {
            if (!extras[e] || !extras[e].id || !extras[e].window) continue
            rows.push({ id: extras[e].id, text: _windowLabel(account.id, extras[e].id, extras[e].title) })
        }
        return rows
    }

    function _accountKey(account, slot) {
        return account.id + ":" + encodeURIComponent(String(account.accountEmail)) + ":" + slot
    }

    function _accountChecked(account, slot) {
        return _has(_accountKey(account, slot)) || _has(account.id + ":" + slot)
    }

    // The slot of a `<provider>:<window>` or `<provider>:<account>:<window>`
    // key of `providerId`, else "".
    function _keySlot(providerId, key) {
        var parts = String(key).split(":")
        if (parts[0] !== providerId || parts.length < 2) return ""
        return parts.length > 2 ? parts.slice(2).join(":") : parts[1]
    }

    function _fallbackWindows(providerId) {
        var rows = []
        var seen = {}
        var base = _fallbackSlots[providerId] || []
        for (var b = 0; b < base.length; b++) {
            rows.push({ id: base[b], text: _windowLabel(providerId, base[b]) })
            seen[base[b]] = true
        }
        var configured = cfg_trayIndicators || []
        for (var i = 0; i < configured.length; i++) {
            var slot = _keySlot(providerId, configured[i])
            if (!slot || seen[slot]) continue
            rows.push({ id: slot, text: _windowLabel(providerId, slot) })
            seen[slot] = true
        }
        return rows
    }

    function _allChecked(providerId, slot) {
        var configured = cfg_trayIndicators || []
        for (var i = 0; i < configured.length; i++) {
            if (_keySlot(providerId, configured[i]) === slot) return true
        }
        return false
    }

    function _toggleAll(providerId, slot, on) {
        var current = cfg_trayIndicators ? cfg_trayIndicators.slice() : []
        for (var i = current.length - 1; i >= 0; i--) {
            if (_keySlot(providerId, current[i]) === slot) current.splice(i, 1)
        }
        if (on) current.push(providerId + ":" + slot)
        cfg_trayIndicators = current
    }

    // A legacy all-accounts key turns into one key per account that has the
    // window before the toggled account changes.
    function _toggleAccount(account, slot, on) {
        var current = cfg_trayIndicators ? cfg_trayIndicators.slice() : []
        var legacyIndex = current.indexOf(account.id + ":" + slot)
        if (legacyIndex >= 0) {
            current.splice(legacyIndex, 1)
            var siblings = accounts[account.id] || []
            for (var i = 0; i < siblings.length; i++) {
                var windows = _accountWindows(siblings[i])
                for (var w = 0; w < windows.length; w++) {
                    if (windows[w].id !== slot) continue
                    var migrated = _accountKey(siblings[i], slot)
                    if (current.indexOf(migrated) < 0) current.push(migrated)
                    break
                }
            }
        }
        var key = _accountKey(account, slot)
        var index = current.indexOf(key)
        if (on && index < 0) current.push(key)
        if (!on && index >= 0) current.splice(index, 1)
        cfg_trayIndicators = current
    }

    // Accounts come from the last saved usage: reading it runs no CLI and no
    // OAuth call, so opening this page never starts a fetch.
    Process.CommandRunner {
        id: accountsRunner
        onFinished: function(command, exitCode, standardOutput, standardError) {
            if (command !== root.activeCommand) return
            root.activeCommand = ""
            if (root.activeCliPath !== root.cliPath) {
                root.loadAccounts()
                return
            }
            root.accountsLoading = false
            var stdout = standardOutput.trim()
            if (!stdout) {
                root.accounts = { claude: [], codex: [] }
                root.accountsError = (standardError || "No account data").trim()
                return
            }
            try {
                var parsed = JSON.parse(stdout)
                if (parsed.fatal) {
                    root.accounts = { claude: [], codex: [] }
                    root.accountsError = parsed.fatal.message || "Could not read accounts"
                    return
                }
                var found = { claude: [], codex: [] }
                var providers = parsed.providers || []
                for (var i = 0; i < providers.length; i++) {
                    var record = providers[i]
                    if (record && found[record.id] && record.ok && record.accountEmail)
                        found[record.id].push(record)
                }
                root.accounts = found
                root.accountsError = ""
            } catch (err) {
                root.accounts = { claude: [], codex: [] }
                root.accountsError = "Could not read accounts: " + err.message
            }
        }
    }

    function loadAccounts() {
        if (root.activeCommand) return
        root.accountsLoading = true
        root.activeCliPath = root.cliPath
        root.activeCommand = "python3 " + Command.shellQuote(root.fetchScriptPath)
            + " --cli-path " + Command.shellQuote(root.activeCliPath)
            + " --providers claude,codex --cache-only"
        accountsRunner.run(root.activeCommand)
    }

    Component.onCompleted: {
        root.initialized = true
        root.loadAccounts()
    }

    QQC2.ScrollView {
        id: scroller
        anchors.fill: parent
        contentWidth: availableWidth
        clip: true

        Kirigami.FormLayout {
            width: scroller.availableWidth

            QQC2.Label {
                text: "Pick which (provider, window) meters to render as separate tray indicators. "
                    + "\"only if proj > 100%\" keeps a meter hidden while it is on pace to last until reset."
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
                opacity: 0.7
                font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            }

            AccountMeters {
                Kirigami.FormData.label: "Claude:"
                providerId: "claude"
            }
            AccountMeters {
                Kirigami.FormData.label: "Codex:"
                providerId: "codex"
            }

            QQC2.Label {
                Kirigami.FormData.label: "Accounts:"
                visible: root.accountsLoading || root.accountsError.length > 0
                text: root.accountsLoading ? "Loading accounts…" : root.accountsError
                color: root.accountsError.length > 0
                    ? Kirigami.Theme.negativeTextColor : Kirigami.Theme.textColor
                opacity: root.accountsError.length > 0 ? 1.0 : 0.6
            }
            MeterCheck {
                Kirigami.FormData.label: "z.ai:"
                key: "zai:primary"
                text: "5h window"
            }
            MeterCheck { key: "zai:secondary"; text: "Monthly" }
            MeterCheck {
                Kirigami.FormData.label: "OpenCode Go:"
                key: "opencodego:primary"
                text: "5h window"
            }
            MeterCheck { key: "opencodego:secondary"; text: "7d weekly" }
            MeterCheck { key: "opencodego:tertiary"; text: "Monthly" }
            MeterCheck {
                Kirigami.FormData.label: "OpenRouter:"
                key: "openrouter:primary"
                text: "credit usage"
            }
            MeterCheck {
                Kirigami.FormData.label: "Kilo:"
                key: "kilo:primary"
                text: "credit usage"
            }

            QQC2.ComboBox {
                Kirigami.FormData.label: "Indicator style:"
                model: [
                    { text: "Ring + percent", value: 0 },
                    { text: "Ring only",      value: 1 },
                    { text: "Percent only",   value: 2 }
                ]
                textRole: "text"
                currentIndex: {
                    var v = cfg_compactStyle
                    for (var i = 0; i < model.length; i++) if (model[i].value === v) return i
                    return 0
                }
                onActivated: cfg_compactStyle = model[currentIndex].value
            }

            RowLayout {
                Kirigami.FormData.label: "Icon + ring size:"
                spacing: Kirigami.Units.smallSpacing
                Layout.fillWidth: true

                QQC2.Slider {
                    from: 14
                    to: 48
                    stepSize: 2
                    snapMode: QQC2.Slider.SnapAlways
                    value: cfg_trayIconSize
                    Layout.fillWidth: true
                    onMoved: cfg_trayIconSize = Math.round(value)
                }
                QQC2.Label {
                    text: cfg_trayIconSize + " px"
                    Layout.minimumWidth: Kirigami.Units.gridUnit * 3
                    horizontalAlignment: Text.AlignRight
                    opacity: 0.7
                }
            }

            QQC2.Label {
                text: "Capped automatically by your panel thickness."
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
                opacity: 0.55
                font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            }
        }
    }
}
