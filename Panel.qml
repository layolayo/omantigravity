import QtQuick
import QtQuick.Layouts
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "omantigravity"
  ipcTarget: "omantigravity"
  manageIpc: false

  // ── State Properties ────────────────────────────────────────────────────────
  property var usageData: ({ status: "loading", groups: [], overall: { lowest_remaining_pct: 100 } })
  property bool loading: fetchProc.running
  property int dataVersion: 0
  property var notifiedAlerts: ({})
  property var exactResetMap: ({})

  // ── Settings ────────────────────────────────────────────────────────────────
  property string agyPath: ""
  property int pollIntervalSec: 300
  property bool showPercentageInBar: true
  property string barMetric: "gemini"
  property string barIcon: "λ"
  property int alertThresholdPct: 20
  property bool enableNotifications: true

  // ── Computed Alerts ─────────────────────────────────────────────────────────
  readonly property var activeAlerts: Model.findAlerts(root.usageData, root.alertThresholdPct)
  readonly property bool hasAlerts: activeAlerts.length > 0
  readonly property bool hasCapacityError: {
    var lat = root.usageData ? root.usageData.latency : null
    if (!lat || !lat.recent_errors) return false
    for (var i = 0; i < lat.recent_errors.length; i++) {
      if (lat.recent_errors[i].is_capacity_error) return true
    }
    return false
  }
  readonly property bool isApiDegraded: {
    var lat = root.usageData ? root.usageData.latency : null
    return !!(lat && (lat.health === "degraded" || lat.health === "slow" || root.hasCapacityError))
  }

  // ── Theme / Palette ─────────────────────────────────────────────────────────
  readonly property color fg: root.bar ? root.bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(fg, 1.45)
  readonly property color subtle: Qt.rgba(fg.r, fg.g, fg.b, 0.08)
  readonly property color borderCol: Qt.rgba(fg.r, fg.g, fg.b, 0.12)
  readonly property color urgent: root.bar ? root.bar.urgent : Color.urgent
  readonly property color warning: "#e5a50a"
  readonly property color track: Style.selectedFillFor(fg, Color.accent)
  readonly property string fontFamily: root.bar ? root.bar.fontFamily : Style.font.family

  // ── Trusted Child Process Paths & Environments ─────────────────────────────
  // Every process this widget launches uses a fixed, root-owned absolute
  // path (never a bare command name resolved through the ambient PATH) and
  // runs with clearEnvironment: true plus only the specific session-identity
  // variables it actually needs -- never the full inherited environment,
  // and never a PATH built from user-writable directories.
  readonly property string python3Bin: "/usr/bin/python3"
  readonly property string notifySendBin: "/usr/bin/notify-send"
  readonly property string omarchyBin: "/usr/bin/omarchy"
  // A minimal PATH for the omarchy CLI, which shells out internally to
  // sibling system tools by bare name -- restricted to root-owned,
  // non-user-writable directories only.
  readonly property string trustedSystemPath: "/usr/share/omarchy/bin:/usr/local/sbin:/usr/local/bin:/usr/bin"
  readonly property var sessionEnv: ({
    "HOME": Quickshell.env("HOME") || "",
    "XDG_RUNTIME_DIR": Quickshell.env("XDG_RUNTIME_DIR") || "",
    "WAYLAND_DISPLAY": Quickshell.env("WAYLAND_DISPLAY") || "",
    "DBUS_SESSION_BUS_ADDRESS": Quickshell.env("DBUS_SESSION_BUS_ADDRESS") || ""
  })
  readonly property var pythonEnv: ({ "HOME": root.sessionEnv.HOME })
  readonly property var notifyEnv: root.sessionEnv
  readonly property var omarchyEnv: Object.assign({}, root.sessionEnv, {
    "OMARCHY_PATH": Quickshell.env("OMARCHY_PATH") || "",
    "USER": Quickshell.env("USER") || "",
    "PATH": root.trustedSystemPath
  })

  // ── Helpers & Actions ───────────────────────────────────────────────────────
  function setting(name, fallback) {
    var value = settings ? settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  function applySettings() {
    agyPath = String(setting("agyPath", ""))
    pollIntervalSec = Math.max(30, Math.min(3600, setting("pollIntervalSec", 300)))
    pollTimer.interval = pollIntervalSec * 1000
    showPercentageInBar = setting("showPercentageInBar", true)
    barMetric = setting("barMetric", "gemini")
    barIcon = setting("barIcon", "λ")
    alertThresholdPct = Math.max(5, Math.min(50, setting("alertThresholdPct", 20)))
    enableNotifications = setting("enableNotifications", true)
  }

  function isExactReset(bucketId) {
    return !!exactResetMap[bucketId]
  }

  function toggleResetFormat(bucketId, toggleAll) {
    var targetState = !isExactReset(bucketId)
    var updated = Object.assign({}, exactResetMap)
    if (toggleAll) {
      if (root.usageData && root.usageData.groups) {
        for (var i = 0; i < root.usageData.groups.length; i++) {
          var grp = root.usageData.groups[i]
          var buckets = grp.buckets || []
          for (var j = 0; j < buckets.length; j++) {
            updated[buckets[j].id] = targetState
          }
        }
      }
    } else {
      updated[bucketId] = targetState
    }
    exactResetMap = updated
  }

  function persistSettings(values) {
    var entry = { id: root.moduleName }
    for (var existing in root.settings) {
      if (existing !== "id") entry[existing] = root.settings[existing]
    }
    for (var key in values) {
      entry[key] = values[key]
    }

    root.settings = entry
    if (root.hostWidget && "settings" in root.hostWidget) {
      root.hostWidget.settings = entry
    }

    if (root.bar && root.bar.shell && typeof root.bar.shell.updateEntryInline === "function") {
      root.bar.shell.updateEntryInline(root.moduleName, entry)
    } else {
      // Fallback via CLI if not embedded in running omarchy-shell
      for (var k in values) {
        var val = values[k]
        var isJson = (typeof val === "number" || typeof val === "boolean")
        saveConfigProc.command = isJson
          ? [root.omarchyBin, "bar", "set", root.moduleName, k, String(val), "--json"]
          : [root.omarchyBin, "bar", "set", root.moduleName, k, String(val)]
        saveConfigProc.running = true
      }
    }
  }

  function setBarMetric(metric) {
    if (!metric) return
    root.barMetric = metric
    persistSettings({ barMetric: metric })
  }

  function setAlertThreshold(thresh) {
    root.alertThresholdPct = thresh
    persistSettings({ alertThresholdPct: thresh })
    checkAndNotify()
  }

  function checkAndNotify() {
    // Drop any previously-notified bucket that has since recovered above the
    // threshold (e.g. a 5h window reset), so the next time it drops back
    // below the threshold it notifies again instead of staying silenced for
    // the rest of the session.
    var activeKeys = {}
    for (var i = 0; i < root.activeAlerts.length; i++) {
      activeKeys[root.activeAlerts[i].id + "_" + root.alertThresholdPct] = true
    }
    var pruned = {}
    for (var existingKey in root.notifiedAlerts) {
      if (activeKeys[existingKey]) pruned[existingKey] = true
    }
    root.notifiedAlerts = pruned

    if (!root.enableNotifications || !root.hasAlerts) return
    var updated = Object.assign({}, root.notifiedAlerts)
    for (var i = 0; i < root.activeAlerts.length; i++) {
      var a = root.activeAlerts[i]
      var key = a.id + "_" + root.alertThresholdPct
      if (!updated[key]) {
        updated[key] = true
        notifyProc.command = [
          root.notifySendBin,
          "-a", "Antigravity",
          "-u", "critical",
          "-i", "dialog-warning",
          "Antigravity Quota Alert",
          a.group + " (" + a.bucket + ") reached " + a.pct + "% remaining (threshold: " + root.alertThresholdPct + "%)."
        ]
        notifyProc.running = true
      }
    }
    root.notifiedAlerts = updated
  }

  function pathFromUrl(url) {
    var value = String(url || "")
    if (value.indexOf("file://") === 0)
      return decodeURIComponent(value.substring(7))
    return value
  }

  function refresh(force) {
    if (fetchProc.running) return
    var scriptPath = pathFromUrl(Qt.resolvedUrl("scripts/fetch_usage.py"))
    var args = [root.python3Bin, scriptPath]
    if (force) args.push("--force")
    else args.push("--cached")
    if (root.agyPath.length > 0) args.push("--agy-path", root.agyPath)
    fetchProc.command = args
    fetchProc.running = true
  }

  function loadInitialCache() {
    var scriptPath = pathFromUrl(Qt.resolvedUrl("scripts/fetch_usage.py"))
    cacheProc.command = [root.python3Bin, scriptPath, "--cached-only"]
    cacheProc.running = true
  }

  function triggerPress(b) {
    if (b === Qt.MiddleButton || b === Qt.RightButton) {
      refresh(true)
      return
    }
    if (opened) close()
    else {
      open()
      refresh(false)
    }
  }

  function state() {
    return JSON.stringify(root.usageData)
  }

  visible: true
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Component.onCompleted: {
    applySettings()
    loadInitialCache()
    refresh(false)
  }

  onSettingsChanged: applySettings()

  onOpenedChanged: {
    if (opened) refresh(false)
  }

  // ── Processes ───────────────────────────────────────────────────────────────
  Process {
    id: cacheProc
    clearEnvironment: true
    environment: root.pythonEnv
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var raw = String(text || "").trim()
        if (raw.length > 0 && raw.indexOf("{") === 0) {
          var parsed = Model.parseData(raw)
          if (parsed && parsed.status === "ok") {
            root.usageData = parsed
            root.dataVersion++
            root.checkAndNotify()
          }
        }
      }
    }
  }

  Process {
    id: fetchProc
    clearEnvironment: true
    environment: root.pythonEnv
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var raw = String(text || "").trim()
        if (raw.length > 0 && raw.indexOf("{") === 0) {
          root.usageData = Model.parseData(raw)
          root.dataVersion++
          root.checkAndNotify()
        }
      }
    }
  }

  Process {
    id: saveConfigProc
    clearEnvironment: true
    environment: root.omarchyEnv
  }

  Process {
    id: notifyProc
    clearEnvironment: true
    environment: root.notifyEnv
  }

  // ── Background Polling Timer ────────────────────────────────────────────────
  Timer {
    id: pollTimer
    interval: root.pollIntervalSec * 1000
    running: true
    repeat: true
    onTriggered: root.refresh(false)
  }

  // ── IPC Handler ─────────────────────────────────────────────────────────────
  IpcHandler {
    target: "omantigravity"

    function refresh() { root.refresh(true) }
    function open() { root.open() }
    function close() { root.close() }
    function toggle() { root.opened ? root.close() : root.open() }
    function state() { return root.state() }
  }

  // ── Bar Button ──────────────────────────────────────────────────────────────
  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: {
      var prefix = ""
      if (root.hasAlerts) {
        prefix = "󰀨 "
      } else if (root.hasCapacityError) {
        prefix = "󰀨 "
      } else if (root.isApiDegraded) {
        prefix = "󰓅 "
      }
      return prefix + Model.formatBarText(root.usageData, root.showPercentageInBar, root.barIcon, root.barMetric)
    }
    fixedWidth: -1
    active: root.hasAlerts || root.isApiDegraded
    useActiveColor: true
    activeColor: (root.hasAlerts || root.hasCapacityError) ? root.urgent : (root.isApiDegraded ? root.warning : root.fg)
    tooltipText: {
      var base = root.usageData && root.usageData.tooltip ? root.usageData.tooltip : "Antigravity CLI Quota"
      if (root.hasAlerts) {
        return "⚠️ LOW QUOTA ALERT! (≤" + root.alertThresholdPct + "%)\n" + base
      }
      if (root.hasCapacityError) {
        return "🛑 UPSTREAM 503 SERVER OVERLOAD!\nGoogle model capacity exhausted.\n" + base
      }
      if (root.isApiDegraded) {
        var latSec = (root.usageData && root.usageData.latency && root.usageData.latency.average_turn_sec) 
          ? (root.usageData.latency.average_turn_sec + "s") : ""
        return "⚠️ HIGH API LATENCY (" + latSec + ")!\n" + base
      }
      return base
    }
    onPressed: function(b) { root.triggerPress(b) }
  }

  // ── Panel Overlay ───────────────────────────────────────────────────────────
  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(430))
    contentHeight: panel.fittedContentHeight(contentColumn.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()

      ColumnLayout {
        id: contentColumn
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        spacing: Style.space(8)

        // ── Panel Hero Header ──────────────────────────────────────────────
        PanelHero {
          Layout.fillWidth: true
          title: "Antigravity Quota"
          meta: {
            var model = root.usageData && root.usageData.active_model ? root.usageData.active_model : null
            if (!model || !model.label) {
              return "Google Antigravity CLI"
            }
            var name = model.label.replace(/\s*\((Low|Medium|High)\)/i, "")
            var effort = model.effort ? (model.effort.charAt(0).toUpperCase() + model.effort.slice(1)) : ""
            if (effort) {
              return name + " · Reasoning: " + effort
            }
            return model.label
          }
          foreground: root.hasAlerts ? root.urgent : root.fg
          fontFamily: root.fontFamily
          iconOpacity: 1.0
          iconComponent: Component {
            Text {
              anchors.centerIn: parent
              text: root.hasAlerts ? "󰀨" : root.barIcon
              color: root.hasAlerts ? root.urgent : root.fg
              font.pixelSize: Style.font.display
              font.family: root.fontFamily
              font.bold: true
            }
          }
        }

        PanelSeparator {
          Layout.fillWidth: true
          foreground: root.fg
        }

        // ── Alert Banner (Visible when any quota is <= alertThresholdPct) ──
        BorderSurface {
          visible: root.hasAlerts
          Layout.fillWidth: true
          color: Qt.rgba(root.urgent.r, root.urgent.g, root.urgent.b, 0.12)
          borderSpec: Border.flat(root.urgent, 1)
          radius: Style.cornerRadius
          padding: Style.space(8)
          implicitHeight: alertBannerRow.implicitHeight + contentTopInset + contentBottomInset

          RowLayout {
            id: alertBannerRow
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: Style.space(8)
            spacing: Style.space(8)

            Text {
              text: "󰀨"
              color: root.urgent
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              Layout.alignment: Qt.AlignVCenter
            }

            Text {
              Layout.fillWidth: true
              text: {
                if (root.activeAlerts.length === 0) return ""
                var parts = []
                for (var i = 0; i < root.activeAlerts.length; i++) {
                  var a = root.activeAlerts[i]
                  parts.push(a.group + " (" + a.bucket + "): " + a.pct + "%")
                }
                return "Critical Quota! " + parts.join(" · ") + " (threshold ≤ " + root.alertThresholdPct + "%)"
              }
              color: root.urgent
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              font.bold: true
              wrapMode: Text.WordWrap
            }
          }
        }

        // ── API Health & Latency Card ──────────────────────────────────────
        BorderSurface {
          visible: root.usageData && !!root.usageData.latency
          Layout.fillWidth: true
          color: {
            var lat = root.usageData ? root.usageData.latency : null
            if (root.hasCapacityError || (lat && lat.health === "degraded")) return Qt.rgba(root.urgent.r, root.urgent.g, root.urgent.b, 0.12)
            if (lat && lat.health === "slow") return Qt.rgba(root.warning.r, root.warning.g, root.warning.b, 0.10)
            return root.subtle
          }
          borderSpec: Border.flat(
            root.hasCapacityError || (root.usageData && root.usageData.latency && root.usageData.latency.health === "degraded") 
              ? root.urgent 
              : ((root.usageData && root.usageData.latency && root.usageData.latency.health === "slow") ? root.warning : root.borderCol), 
            1
          )
          radius: Style.cornerRadius
          padding: Style.space(8)
          implicitHeight: latencyBody.implicitHeight + contentTopInset + contentBottomInset

          ColumnLayout {
            id: latencyBody
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: Style.space(8)
            spacing: Style.space(6)

            // Header Row: Icon, Title, Status Badge
            RowLayout {
              Layout.fillWidth: true
              spacing: Style.space(6)

              Text {
                text: {
                  var lat = root.usageData ? root.usageData.latency : null
                  if (root.hasCapacityError) return "󰀨"
                  if (!lat) return "󰓅"
                  if (lat.health === "degraded") return "󰀨"
                  if (lat.health === "slow") return "󰀦"
                  return "󰓅"
                }
                color: {
                  var lat = root.usageData ? root.usageData.latency : null
                  if (root.hasCapacityError || (lat && lat.health === "degraded")) return root.urgent
                  if (lat && lat.health === "slow") return root.warning
                  return root.fg
                }
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }

              Text {
                text: "Google Antigravity API Health"
                color: root.fg
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
              }

              Item { Layout.fillWidth: true }

              // Status Tag Badge
              Rectangle {
                id: statusBadge
                readonly property var lat: root.usageData ? root.usageData.latency : null
                readonly property string hState: root.hasCapacityError ? "503 OVERLOAD" : (lat ? (lat.health === "degraded" ? "DEGRADED" : (lat.health === "slow" ? "SLOW" : "HEALTHY")) : "HEALTHY")
                readonly property color badgeColor: root.hasCapacityError || (lat && lat.health === "degraded") ? root.urgent : (lat && lat.health === "slow" ? root.warning : root.fg)
                height: Style.space(18)
                implicitWidth: statusText.implicitWidth + Style.space(10)
                radius: Style.cornerRadius
                color: root.hasCapacityError || (lat && lat.health === "degraded")
                  ? Qt.rgba(root.urgent.r, root.urgent.g, root.urgent.b, 0.22)
                  : (lat && lat.health === "slow" ? Qt.rgba(root.warning.r, root.warning.g, root.warning.b, 0.22) : Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.1))
                border.width: 1
                border.color: badgeColor

                Text {
                  id: statusText
                  anchors.centerIn: parent
                  text: statusBadge.hState
                  color: statusBadge.badgeColor
                  font.family: root.fontFamily
                  font.pixelSize: 9
                  font.bold: true
                }
              }
            }

            // Metrics row: Ping, TTFB, Avg Turn
            RowLayout {
              Layout.fillWidth: true
              spacing: Style.space(12)

              // Ping
              RowLayout {
                spacing: Style.space(4)
                Text {
                  text: "Ping:"
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
                Text {
                  text: {
                    var net = root.usageData && root.usageData.latency ? root.usageData.latency.network : null
                    return (net && net.ping_ms !== null && net.ping_ms !== undefined) ? (net.ping_ms + "ms") : "--"
                  }
                  color: root.fg
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: true
                }
              }

              // TTFB
              RowLayout {
                spacing: Style.space(4)
                Text {
                  text: "TTFB:"
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
                Text {
                  text: {
                    var net = root.usageData && root.usageData.latency ? root.usageData.latency.network : null
                    return (net && net.ttfb_ms !== null && net.ttfb_ms !== undefined) ? (net.ttfb_ms + "ms") : "--"
                  }
                  color: root.fg
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: true
                }
              }

              // Turn Latency
              RowLayout {
                spacing: Style.space(4)
                Text {
                  text: "Avg Turn:"
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }
                Text {
                  text: {
                    var lat = root.usageData ? root.usageData.latency : null
                    return (lat && lat.average_turn_sec !== null && lat.average_turn_sec !== undefined) ? (lat.average_turn_sec + "s") : "--"
                  }
                  color: {
                    var lat = root.usageData ? root.usageData.latency : null
                    if (lat && lat.average_turn_sec > 15) return root.urgent
                    if (lat && lat.average_turn_sec > 8) return root.warning
                    return root.fg
                  }
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: true
                }
              }

              Item { Layout.fillWidth: true }
            }

            // 503 Capacity Warning if detected
            Repeater {
              model: {
                var lat = root.usageData ? root.usageData.latency : null
                if (!lat || !lat.recent_errors) return []
                var caps = []
                for (var i = 0; i < lat.recent_errors.length; i++) {
                  if (lat.recent_errors[i].is_capacity_error) {
                    caps.push(lat.recent_errors[i])
                  }
                }
                return caps.slice(-1)
              }

              delegate: Rectangle {
                required property var modelData
                Layout.fillWidth: true
                height: Style.space(24)
                radius: Style.cornerRadius
                color: Qt.rgba(root.urgent.r, root.urgent.g, root.urgent.b, 0.2)
                border.width: 1
                border.color: root.urgent

                RowLayout {
                  anchors.fill: parent
                  anchors.leftMargin: Style.space(8)
                  anchors.rightMargin: Style.space(8)
                  spacing: Style.space(6)

                  Text {
                    text: "󰀨"
                    color: root.urgent
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                  }

                  Text {
                    Layout.fillWidth: true
                    text: "503 Capacity Limit (" + modelData.time + ") — Model overloaded"
                    color: root.urgent
                    font.family: root.fontFamily
                    font.pixelSize: 10
                    font.bold: true
                    elide: Text.ElideRight
                  }
                }
              }
            }
          }
        }

        // ── Quota Group Selector for Bar Widget (Rectangular & Minimalist) ─
        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(6)

          Text {
            text: "On bar:"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
          }

          Repeater {
            model: [
              { key: "gemini", label: "Gemini", icon: "󰘧" },
              { key: "3p", label: "Claude & GPT", icon: "󰚩" },
              { key: "lowest", label: "Lowest", icon: "󰻌" }
            ]

            delegate: Rectangle {
              required property var modelData
              required property int index

              readonly property bool isSelected: {
                var current = (root.barMetric || "gemini").toLowerCase()
                if (modelData.key === "gemini") {
                  return current === "gemini" || current.indexOf("gemini") === 0
                }
                if (modelData.key === "3p") {
                  return current === "3p" || current.indexOf("3p") === 0 || current.indexOf("claude") !== -1 || current.indexOf("gpt") !== -1
                }
                if (modelData.key === "lowest") {
                  return current === "lowest"
                }
                return current === modelData.key
              }
              height: Style.space(24)
              implicitWidth: chipRow.implicitWidth + Style.space(16)
              radius: Style.cornerRadius
              color: isSelected ? Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.2) : Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.04)
              border.width: 1
              border.color: isSelected ? root.fg : Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.14)

              RowLayout {
                id: chipRow
                anchors.centerIn: parent
                spacing: Style.space(5)

                Text {
                  text: modelData.icon
                  color: isSelected ? root.fg : root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                }

                Text {
                  text: modelData.label
                  color: isSelected ? root.fg : root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  font.bold: isSelected
                }
              }

              MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                hoverEnabled: true
                onClicked: root.setBarMetric(modelData.key)
              }
            }
          }

          Item { Layout.fillWidth: true }
        }

        // ── Alert Threshold & Notification Controls (Rectangular & Minimalist)
        RowLayout {
          Layout.fillWidth: true
          spacing: Style.space(6)

          Text {
            text: "Alert:"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            font.bold: true
          }

          Repeater {
            model: [10, 15, 20, 25, 30]

            delegate: Rectangle {
              required property int modelData
              required property int index

              readonly property bool isSelected: root.alertThresholdPct === modelData
              height: Style.space(24)
              implicitWidth: threshText.implicitWidth + Style.space(12)
              radius: Style.cornerRadius
              color: isSelected ? Qt.rgba(root.urgent.r, root.urgent.g, root.urgent.b, 0.18) : Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.04)
              border.width: 1
              border.color: isSelected ? root.urgent : Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.14)

              Text {
                id: threshText
                anchors.centerIn: parent
                text: modelData + "%"
                color: isSelected ? root.urgent : root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: isSelected
              }

              MouseArea {
                anchors.fill: parent
                cursorShape: Qt.PointingHandCursor
                hoverEnabled: true
                onClicked: root.setAlertThreshold(modelData)
              }
            }
          }

          Item { Layout.fillWidth: true }
        }

        // ── Error View ─────────────────────────────────────────────────────
        BorderSurface {
          visible: root.usageData && root.usageData.status === "error"
          Layout.fillWidth: true
          color: Qt.rgba(root.urgent.r, root.urgent.g, root.urgent.b, 0.1)
          borderSpec: Border.flat(root.urgent, 1)
          radius: Style.cornerRadius
          padding: Style.space(10)
          implicitHeight: errBody.implicitHeight + contentTopInset + contentBottomInset

          ColumnLayout {
            id: errBody
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: Style.space(10)
            spacing: Style.space(4)

            RowLayout {
              spacing: Style.space(6)
              Text {
                text: "󰀨"
                color: root.urgent
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
              }
              Text {
                text: "Antigravity CLI Error"
                color: root.fg
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
                font.bold: true
              }
            }

            Text {
              Layout.fillWidth: true
              text: root.usageData.error || "Unable to communicate with Antigravity CLI."
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }

            Button {
              Layout.topMargin: Style.space(2)
              text: "Retry"
              foreground: root.fg
              fontFamily: root.fontFamily
              fontSize: Style.font.caption
              horizontalPadding: Style.spacing.controlPaddingX
              verticalPadding: Style.spacing.controlPaddingY
              onClicked: root.refresh(true)
            }
          }
        }

        // ── Model Groups ───────────────────────────────────────────────────
        Repeater {
          model: root.usageData && root.usageData.groups ? root.usageData.groups : []

          delegate: BorderSurface {
            required property var modelData
            required property int index

            Layout.fillWidth: true
            color: root.subtle
            borderSpec: Border.flat(root.borderCol, 1)
            radius: Style.cornerRadius
            padding: Style.space(8)
            implicitHeight: groupBody.implicitHeight + contentTopInset + contentBottomInset

            ColumnLayout {
              id: groupBody
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.top: parent.top
              anchors.margins: Style.space(8)
              spacing: Style.space(6)

              // Group Header
              RowLayout {
                Layout.fillWidth: true
                spacing: Style.space(6)

                Text {
                  text: modelData.icon || "󰚩"
                  color: root.fg
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                  Layout.alignment: Qt.AlignVCenter
                }

                Text {
                  text: modelData.name || "Model Group"
                  color: root.fg
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  font.bold: true
                }

                Item { Layout.fillWidth: true }

                Text {
                  text: modelData.description ? modelData.description.replace("Models within this group: ", "") : ""
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  elide: Text.ElideRight
                  opacity: 0.7
                }
              }

              // Buckets
              Repeater {
                model: modelData.buckets || []

                delegate: Item {
                  id: bucketItem
                  required property var modelData
                  required property int index

                  readonly property bool isBarActive: root.barMetric === modelData.id
                  readonly property bool isCritical: modelData.remaining_pct <= root.alertThresholdPct
                  Layout.fillWidth: true
                  implicitHeight: bucketCol.implicitHeight + Style.space(6)

                  MouseArea {
                    anchors.fill: parent
                    cursorShape: Qt.PointingHandCursor
                    hoverEnabled: true
                    onClicked: root.setBarMetric(modelData.id)
                  }

                  ColumnLayout {
                    id: bucketCol
                    z: 1
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    spacing: Style.space(5)

                    RowLayout {
                      Layout.fillWidth: true
                      spacing: Style.space(6)

                      Text {
                        text: modelData.window_title || modelData.name || "Limit"
                        color: root.fg
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.caption
                        font.bold: true
                      }

                      // Badge if this specific bucket is selected for bar (Rectangular & Minimalist)
                      Rectangle {
                        visible: bucketItem.isBarActive
                        height: Style.space(16)
                        implicitWidth: barTagText.implicitWidth + Style.space(8)
                        radius: Style.cornerRadius
                        color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.14)
                        border.width: 1
                        border.color: Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.25)

                        Text {
                          id: barTagText
                          anchors.centerIn: parent
                          text: "󰄬 On bar"
                          color: root.fg
                          font.family: root.fontFamily
                          font.pixelSize: Style.font.caption
                          font.bold: true
                        }
                      }

                      Item { Layout.fillWidth: true }

                      // Interactive time pill - click to toggle between remaining time and exact reset date
                      Rectangle {
                        id: timeChip
                        z: 2
                        readonly property bool isExact: root.isExactReset(modelData.id)
                        readonly property string displayText: Model.formatResetDisplay(modelData, isExact)
                        visible: displayText !== ""
                        Layout.alignment: Qt.AlignVCenter
                        height: Style.space(20)
                        implicitHeight: Style.space(20)
                        implicitWidth: timeRow.implicitWidth + Style.space(10)
                        radius: Style.cornerRadius
                        color: timeMouse.containsMouse 
                          ? Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.14)
                          : (isExact ? Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.08) : "transparent")
                        border.width: 1
                        border.color: isExact 
                          ? Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.28) 
                          : (timeMouse.containsMouse ? Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.18) : Qt.rgba(root.fg.r, root.fg.g, root.fg.b, 0.08))

                        RowLayout {
                          id: timeRow
                          anchors.centerIn: parent
                          spacing: Style.space(3)

                          Text {
                            text: timeChip.isExact ? "󰸗" : "󰅐"
                            color: timeChip.isExact ? root.fg : root.dim
                            font.family: root.fontFamily
                            font.pixelSize: Style.font.caption
                          }

                          Text {
                            text: timeChip.displayText
                            color: timeChip.isExact ? root.fg : root.dim
                            font.family: root.fontFamily
                            font.pixelSize: Style.font.caption
                            font.bold: timeChip.isExact
                          }
                        }

                        MouseArea {
                          id: timeMouse
                          anchors.fill: parent
                          cursorShape: Qt.PointingHandCursor
                          hoverEnabled: true
                          acceptedButtons: Qt.LeftButton | Qt.MiddleButton
                          onClicked: function(mouse) {
                            mouse.accepted = true
                            var toggleAll = (mouse.button === Qt.MiddleButton) || (mouse.modifiers & Qt.ShiftModifier)
                            root.toggleResetFormat(modelData.id, toggleAll)
                          }
                        }

                        PanelToolTip {
                          visible: timeMouse.containsMouse
                          text: timeChip.isExact ? "Exact reset date (click to show countdown)" : "Remaining time (click to show exact date)"
                        }
                      }

                      Text {
                        text: modelData.remaining_pct + "%"
                        color: Model.getStatusColor(modelData.remaining_pct, root.fg, root.urgent, root.warning, root.alertThresholdPct)
                        font.family: root.fontFamily
                        font.pixelSize: Style.font.caption
                        font.bold: true
                      }
                    }

                    // Rounded Meter Bar
                    Item {
                      Layout.fillWidth: true
                      Layout.topMargin: Style.space(2)
                      implicitHeight: Style.space(6)

                      Rectangle {
                        id: trackRect
                        anchors.fill: parent
                        radius: height / 2
                        color: root.track
                      }

                      Rectangle {
                        anchors.left: trackRect.left
                        anchors.verticalCenter: trackRect.verticalCenter
                        height: trackRect.height
                        radius: trackRect.radius
                        width: trackRect.width * Math.max(0, Math.min(1, modelData.remaining_fraction))
                        color: Model.getStatusColor(modelData.remaining_pct, root.fg, root.urgent, root.warning, root.alertThresholdPct)

                        Behavior on width {
                          NumberAnimation { duration: 250; easing.type: Easing.OutCubic }
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }

        // ── Footer / Shortcuts ─────────────────────────────────────────────
        RowLayout {
          Layout.fillWidth: true
          Layout.topMargin: Style.space(2)
          Layout.bottomMargin: Style.space(4)

          Text {
            text: "[Esc] Close"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            opacity: 0.6
          }

          Item { Layout.fillWidth: true }

          Text {
            text: "antigravity"
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            opacity: 0.4
          }
        }
      }
    }
  }
}
