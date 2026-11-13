import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.kcmutils as KCM
import org.kde.plasma.plasmoid

KCM.SimpleKCM {
    property bool   cfg_enableUsage: true
    property string cfg_cliPath: "/usr/bin/codexbar"
    property int    cfg_refreshSeconds: 30

    // The popup's "Turn off usage tracking" writes the setting while this
    // page may be open; follow it, or Apply would write the old value back.
    Connections {
        target: Plasmoid.configuration
        function onValueChanged(key, value) {
            if (key === "enableUsage") cfg_enableUsage = value
        }
    }

    Kirigami.FormLayout {
        QQC2.Switch {
            objectName: "enableUsageSwitch"
            Kirigami.FormData.label: "Usage:"
            text: "Track usage"
            checked: cfg_enableUsage
            onToggled: cfg_enableUsage = checked
        }
        QQC2.Label {
            text: "Fetches provider usage through the codexbar CLI and shows it in the popup, panel and tooltip. Turn it off if the CLI isn't installed or you track usage elsewhere; Agents and History keep working."
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
            opacity: 0.55
            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
        }

        QQC2.TextField {
            objectName: "cliPathField"
            Kirigami.FormData.label: "codexbar CLI:"
            enabled: cfg_enableUsage
            text: cfg_cliPath
            onTextChanged: cfg_cliPath = text
            placeholderText: "/usr/bin/codexbar"
            Layout.fillWidth: true
        }

        RowLayout {
            Kirigami.FormData.label: "Refresh every:"
            enabled: cfg_enableUsage
            QQC2.SpinBox {
                objectName: "usageRefreshSpinBox"
                from: 10
                to: 3600
                editable: true
                value: cfg_refreshSeconds
                onValueModified: cfg_refreshSeconds = value
            }
            QQC2.Label { text: "seconds" }
        }

        QQC2.Label {
            text: "Path to the codexbar CLI binary that the widget calls to fetch provider usage."
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
            opacity: 0.55
            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
        }
    }
}
