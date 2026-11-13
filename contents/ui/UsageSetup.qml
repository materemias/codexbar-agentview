import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PC3
import org.kde.kirigami as Kirigami
import "Filter.js" as Filter

// Shown on the Usage tab, in place of the provider cards, while the fetch
// helper reports `fatal.code == "cli_missing"`: what is missing, the steps
// that set it up, and the two ways out. Kept short: Plasma clamps the
// popup height.
SectionCard {
    id: setup
    objectName: "usageSetup"

    // The helper's `fatal` object: { code, message, setup: [lines] }.
    property var fatal: null
    readonly property var steps: setup.fatal && Array.isArray(setup.fatal.setup)
        ? setup.fatal.setup.filter(function(line) { return typeof line === "string" && line.length > 0 })
        : []

    // A step as rich text: escaped, `code` in a monospace face, URLs as
    // links. The match runs on escaped text, so an `&` counts only as
    // `&amp;`, keeping `&gt;` and friends out; a sentence's closing
    // punctuation stays outside the link.
    function stepHtml(line) {
        return Filter.escape(line)
            .replace(/`([^`]+)`/g, "<code>$1</code>")
            .replace(/https?:\/\/(?:[^\s<"&]|&amp;)*[^\s<"&.,;:)]/g, function(url) {
                return '<a href="' + url + '">' + url + "</a>"
            })
    }

    contentMargins: 8
    bodySpacing: Kirigami.Units.smallSpacing

    PC3.Label {
        objectName: "usageSetupTitle"
        Layout.fillWidth: true
        text: "Usage tracking isn't set up"
        font.weight: Font.DemiBold
        wrapMode: Text.WordWrap
    }
    PC3.Label {
        objectName: "usageSetupMessage"
        Layout.fillWidth: true
        visible: text.length > 0
        text: setup.fatal && setup.fatal.message ? String(setup.fatal.message) : ""
        textFormat: Text.PlainText
        color: Kirigami.Theme.negativeTextColor
        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
        wrapMode: Text.Wrap
    }
    Repeater {
        model: setup.steps
        delegate: RowLayout {
            required property int index
            required property string modelData
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing

            PC3.Label {
                text: (index + 1) + "."
                font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                opacity: 0.7
                Layout.alignment: Qt.AlignTop
            }
            PC3.Label {
                objectName: "usageSetupStep"
                Layout.fillWidth: true
                text: setup.stepHtml(modelData)
                textFormat: Text.RichText
                font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                wrapMode: Text.Wrap
                onLinkActivated: function(link) { Qt.openUrlExternally(link) }

                HoverHandler {
                    cursorShape: parent.hoveredLink ? Qt.PointingHandCursor : Qt.ArrowCursor
                }
            }
        }
    }
    RowLayout {
        Layout.fillWidth: true
        Layout.topMargin: Kirigami.Units.smallSpacing
        spacing: Kirigami.Units.smallSpacing

        PC3.Button {
            objectName: "usageSetupSettings"
            icon.name: "configure"
            text: "Open settings"
            onClicked: {
                // Absent when kiosk settings lock the configuration.
                var configure = Plasmoid.internalAction("configure")
                if (configure) configure.trigger()
            }
        }
        PC3.Button {
            objectName: "usageSetupTurnOff"
            icon.name: "process-stop"
            text: "Turn off usage tracking"
            onClicked: Plasmoid.configuration.enableUsage = false
        }
        Item { Layout.fillWidth: true }
    }
}
