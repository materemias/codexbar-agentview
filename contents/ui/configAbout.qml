import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.kcmutils as KCM
import "process" as Process
import "Command.js" as Command

KCM.SimpleKCM {
    id: root

    // codexbar_companions.py's rows: name, benefit, install link and status,
    // read once each time this page opens.
    property var companions: []
    property string companionsError: ""
    readonly property string companionsScriptPath: Command.localPath(
        Qt.resolvedUrl("../scripts/codexbar_companions.py"))

    Process.CommandRunner {
        id: companionsRunner
        onFinished: function(command, exitCode, standardOutput, standardError) {
            try {
                var rows = JSON.parse(standardOutput).companions
                if (!Array.isArray(rows))
                    throw new Error("no companions list")
                root.companions = rows
                root.companionsError = ""
            } catch (err) {
                root.companions = []
                root.companionsError = standardError.trim() || (exitCode === 0 ? err.message : "exit " + exitCode)
            }
        }
    }

    Component.onCompleted: companionsRunner.run("python3 " + Command.shellQuote(root.companionsScriptPath))

    Kirigami.FormLayout {
        QQC2.Label {
            Kirigami.FormData.label: "CodexBar AgentView:"
            text: "KDE Plasma applet and Tern plugin for AI provider usage and coding-agent sessions."
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
        }

        QQC2.Button {
            text: "Open CodexBar AgentView on GitHub"
            onClicked: Qt.openUrlExternally("https://github.com/materemias/codexbar-agentview")
        }

        QQC2.Label {
            Kirigami.FormData.label: "CodexBar:"
            text: "Upstream macOS app and CLI used to fetch provider usage data."
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
        }

        QQC2.Button {
            text: "Open CodexBar on GitHub"
            onClicked: Qt.openUrlExternally("https://github.com/steipete/CodexBar")
        }

        Kirigami.Separator {
            Kirigami.FormData.label: "Works well with"
            Kirigami.FormData.isSection: true
        }

        QQC2.Label {
            visible: root.companionsError !== ""
            text: "Could not check: " + root.companionsError
            wrapMode: Text.WordWrap
            Layout.fillWidth: true
        }

        ColumnLayout {
            objectName: "companions"
            Layout.fillWidth: true
            spacing: Kirigami.Units.largeSpacing

            Repeater {
                model: root.companions

                ColumnLayout {
                    required property var modelData
                    Layout.fillWidth: true
                    spacing: Kirigami.Units.smallSpacing

                    RowLayout {
                        QQC2.Label {
                            text: modelData.name
                            font.bold: true
                        }
                        QQC2.Label {
                            objectName: "companionStatus"
                            text: modelData.status
                            color: modelData.installed ? Kirigami.Theme.positiveTextColor
                                                       : Kirigami.Theme.disabledTextColor
                        }
                    }

                    QQC2.Label {
                        text: modelData.benefit
                        wrapMode: Text.WordWrap
                        Layout.fillWidth: true
                    }

                    QQC2.Button {
                        text: modelData.installed ? "Open GitHub" : "Install steps"
                        enabled: String(modelData.url).indexOf("https://") === 0
                        onClicked: Qt.openUrlExternally(modelData.url)
                    }
                }
            }
        }
    }
}
