import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Logic.js" as Logic

// The orchard clock. Left-click opens the calendar, right-click cycles the
// same format ring as Omarchy's clock, and middle-click opens the timezone
// picker.
BarWidget {
  id: root
  moduleName: "com.buceylabs.orchard"

  property date now: clock.date

  readonly property var service: bar && bar.shell ? bar.shell.serviceFor(moduleName) : null

  // The helper is the source of truth for widget settings. It exposes
  // optimistic writes immediately, which lets consecutive right-clicks walk
  // forward even while the helper is still saving the previous click.
  function pref(name, fallback) {
    if (service && typeof service.setting === "function") return service.setting(name, fallback)
    return setting(name, fallback)
  }

  readonly property string configuredFormat: String(vertical
    ? pref("verticalFormat", Logic.settingDefault("verticalFormat"))
    : pref("format", Logic.settingDefault("format")))
  readonly property string configuredAltFormat: String(vertical
    ? pref("verticalFormatAlt", Logic.settingDefault("verticalFormatAlt"))
    : pref("formatAlt", Logic.settingDefault("formatAlt")))
  readonly property var formatRing: Logic.clockFormatRing(
    configuredFormat, configuredAltFormat, Logic.clockFormats(vertical))
  readonly property string activeFormat: configuredFormat
  readonly property string displayText: formatted(now)
  readonly property var verticalLines: displayText.split("\n")

  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property real openPanelIndicatorWidth: button.labelWidth
  readonly property real openPanelIndicatorHeight:
    Math.max(Style.space(10), Math.round(Style.bar.iconSlot * 0.55))
  readonly property bool popoutSwitchClosing:
    panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false

  function refresh() {
    now = new Date()
    if (panelLoader.item && panelLoader.item.refresh) panelLoader.item.refresh()
  }

  function formatted(date) {
    var key = Qt.formatDate(date, "yyyy-MM-dd")
    return Qt.formatDateTime(date, activeFormat.replace(/ww/g, Logic.isoWeekLiteral(key)))
  }

  function cycleFormat() {
    if (!service || typeof service.setSetting !== "function") return
    var next = Logic.nextClockFormat(formatRing, configuredFormat)
    if (!next || next === configuredFormat) return
    service.setSetting(vertical ? "verticalFormat" : "format", next)
  }

  // Builds the panel if it is not up yet. Anything that opens it goes through
  // here; anything that only closes or refreshes it does not, because there
  // is nothing to close that was never built.
  function panel() {
    if (!panelLoader.active) panelLoader.active = true
    return panelLoader.item
  }

  function open() { var p = root.panel(); if (p) p.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function togglePanel() { var p = root.panel(); if (p) p.toggle() }
  function closeForPopoutSwitch() {
    if (panelLoader.item) panelLoader.item.closeForPopoutSwitch()
  }

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("settings" in target) target.settings = root.settings
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()

  // Match the stock clock's minute cadence; its built-in ring has no seconds.
  SystemClock {
    id: clock
    precision: SystemClock.Minutes
    onDateChanged: root.now = date
  }

  // The panel is four and a half thousand lines and most of this plugin.
  // Building it while the shell rescans its plugins pushed that rescan past
  // the two seconds `omarchy plugin add` waits for, and the install printed
  // "omarchy-shell is not responding" at somebody who had done nothing wrong.
  //
  // It is built a moment after the shell has settled instead — a rescan is
  // then cheap, and by the time anyone reaches for the clock it is ready.
  // Anything that asks for it sooner gets it built on the spot.
  Loader {
    id: panelLoader
    active: false
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  Timer {
    interval: 4000
    running: true
    repeat: false
    onTriggered: panelLoader.active = true
  }

  IpcHandler {
    target: "orchard"

    function refresh(): void { root.broadcast("refresh") }
    function open(): void { root.open() }
    function close(): void { root.close() }
    function toggle(): void { root.togglePanel() }
    function cycleFormat(): void { root.cycleFormat() }

  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.vertical ? "" : root.displayText
    labelVisible: !root.vertical
    hasVisualContent: root.vertical ? root.verticalLines.length > 0 : text !== ""
    fixedHeight: root.vertical ? root.verticalLines.length * Style.bar.iconSlot : -1
    horizontalMargin: 8.75
    verticalPadding: 8.75

    onPressed: function (which) {
      if (which === Qt.RightButton) root.cycleFormat()
      else if (which === Qt.MiddleButton) { if (root.bar) root.bar.run("omarchy-menu-timezone") }
      else root.togglePanel()
    }

    Column {
      visible: root.vertical
      anchors.fill: parent

      Repeater {
        model: root.verticalLines

        OpticalGlyph {
          required property string modelData
          width: button.width
          height: Style.bar.iconSlot
          text: modelData
          fontFamily: button.fontFamily
          fontSize: modelData.length > 3 ? button.fontSize * 0.9 : button.fontSize
          color: button.foreground
        }
      }
    }
  }
}
