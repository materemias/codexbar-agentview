import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.kcmutils as KCM

KCM.SimpleKCM {
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
    }
}
