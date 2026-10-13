import QtQuick
import QtQuick.Layouts
import org.kde.plasma.components as PC3
import org.kde.kirigami as Kirigami

RowLayout {
    id: title

    required property var parts
    property int textFormat: Text.PlainText
    readonly property bool truncated: label.truncated
    spacing: Kirigami.Units.smallSpacing

    TextMetrics {
        id: iconMetrics
        text: title.parts.glyph
        font: icon.font
    }

    Item {
        visible: title.parts.glyph.length > 0
        implicitWidth: Math.max(Kirigami.Units.iconSizes.small, iconMetrics.tightBoundingRect.width)
        implicitHeight: Math.max(icon.implicitHeight, iconMetrics.tightBoundingRect.height)
        Layout.alignment: Qt.AlignVCenter

        PC3.Label {
            id: icon
            x: -iconMetrics.tightBoundingRect.x
            anchors.verticalCenter: parent.verticalCenter
            text: title.parts.icon
            textFormat: title.textFormat
            font.bold: title.textFormat === Text.StyledText && text.indexOf("<b>") !== -1
        }
    }

    PC3.Label {
        id: label
        text: title.parts.label
        textFormat: title.textFormat
        elide: Text.ElideRight
        Layout.fillWidth: true
        Layout.alignment: Qt.AlignVCenter
    }
}
