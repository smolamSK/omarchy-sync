pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC
import Quickshell
import Quickshell.Wayland
import qs.Commons
import qs.Commons as Commons
import qs.Ui

// The Omarchy Sync window: Export, Import and Undo over bin/omarchy-sync. It only
// collects choices and shows results; Service.qml runs the CLI (so a run survives
// the window closing) and holds its state: info, inspected, plan, log, result.
//
// Open with `omarchy-shell shell summon io.github.smolamsk.sync '{"tab":"import"}'`
// (or toggle). Escape closes; it never stops a run in progress.
Item {
  id: root

  property var shell: null
  property var manifest: null
  property var service: null

  property bool opened: false
  property string tab: "export"

  // Export choices: section name -> bool, and extra packages left out.
  property var exportChosen: ({})
  property var excludedPackages: ({})
  property string destination: ""
  property bool showPackages: false

  // Import choices.
  property string importFile: ""
  property var importChosen: ({})
  property bool includeHardware: false

  property string confirmKind: ""  // "import" | "undo" while the dialog asks

  readonly property var info: service ? service.info : null
  readonly property var inspected: service ? service.inspected : null
  readonly property bool busy: service ? service.busy : false
  readonly property string pluginId: (manifest && manifest.id) || "io.github.smolamsk.sync"

  property color foreground: Commons.Color.menu.text
  property color background: Commons.Color.menu.background
  property color accent: Commons.Color.accent
  property color urgent: Commons.Color.urgent
  property string fontFamily: Style.font.menuFamily

  function open(payloadJson) {
    var payload = ({})
    try { payload = JSON.parse(payloadJson || "{}") } catch (e) { payload = ({}) }
    if (payload.tab === "export" || payload.tab === "import" || payload.tab === "undo") root.tab = payload.tab
    root.opened = true
    if (root.service) {
      root.service.windowOpen = true
      root.service.refreshInfo()
    }
    if (!root.nameEdited) root.resetFileName()
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  function close() {
    root.opened = false
    if (root.service) root.service.windowOpen = false
  }

  function dismiss() {
    root.opened = false
    if (root.service) root.service.windowOpen = false
    if (root.shell && typeof root.shell.hide === "function") root.shell.hide(root.pluginId)
  }

  // ------------------------------------------------------------------ helpers

  function sectionLabel(title) {
    var i = String(title).indexOf(" (")
    return i > 0 ? String(title).slice(0, i) : String(title)
  }

  function sectionDetail(title) {
    var m = /\((.*)\)$/.exec(String(title))
    return m ? m[1] : ""
  }

  function chosenList(map, names) {
    var out = []
    for (var i = 0; i < names.length; i++) if (map[names[i]]) out.push(names[i])
    return out
  }

  function setKey(mapName, key, value) {
    var copy = Object.assign({}, root[mapName])
    copy[key] = value
    root[mapName] = copy
  }

  // ------------------------------------------------------------------ export

  readonly property var exportSections: info ? info.sections : []
  readonly property var allPackages: info ? (info.packages.repo || []).concat(info.packages.aur || []) : []
  readonly property var keptPackages: allPackages.filter(function(p) { return !root.excludedPackages[p] })
  readonly property bool exportSecrets: !!exportChosen["secrets"]
  // The name typed in File name, with .tar.gz added; unused when "another file" names one.
  readonly property string fileNameFull: {
    var n = fileNameField.text.trim()
    return n === "" ? "" : (/\.tar\.gz$/.test(n) ? n : n + ".tar.gz")
  }
  readonly property bool otherIsFile: destination === "other" && /\.tar\.gz$/.test(otherPath.text.trim())

  onInfoChanged: {
    if (!info) return
    // Defaults the first time only; later refreshes keep the user's choices.
    if (Object.keys(root.exportChosen).length === 0) {
      var chosen = {}
      for (var i = 0; i < info.sections.length; i++) chosen[info.sections[i].name] = !!info.sections[i]["default"]
      root.exportChosen = chosen
    }
    if (root.destination === "" && root.destinations.length) root.destination = root.destinations[0].value
    if (root.importFile === "" && info.candidates.length) root.importFile = info.candidates[0]
    // The default name carries the hostname, known only once info has arrived.
    if (!root.nameEdited) root.resetFileName()
  }

  readonly property var destinations: {
    var out = []
    if (!info) return out
    for (var i = 0; i < info.places.length; i++) out.push({ value: "dir:" + info.places[i].path, label: info.places[i].label })
    if (info.rclone.default) out.push({ value: "remote:" + info.rclone["default"], label: "rclone: " + info.rclone["default"] })
    for (var j = 0; j < info.rclone.remotes.length; j++) {
      var r = info.rclone.remotes[j] + "omarchy-sync"
      if (r !== info.rclone["default"]) out.push({ value: "remote:" + r, label: "rclone: " + r })
    }
    out.push({ value: "other", label: "Another folder or file…" })
    return out
  }

  readonly property string exportProblem: {
    if (!info) return "Reading this computer…"
    if (chosenList(exportChosen, exportSections.map(function(s) { return s.name })).length === 0) return "Tick at least one section."
    if (exportSecrets && passOne.text.length < 8) return "The secrets passphrase needs at least 8 characters."
    if (exportSecrets && passOne.text !== passTwo.text) return "The two passphrases differ."
    if (destination === "other" && otherPath.text.trim() === "") return "Type a folder or file to save to."
    if (!otherIsFile) {
      var name = fileNameField.text.trim()
      if (name === "") return "Type a file name."
      if (name.indexOf("/") >= 0 || name.charAt(0) === ".") return "The file name can't contain / or start with a dot."
    }
    return ""
  }

  onExportSecretsChanged: if (exportSecrets) Qt.callLater(function() { passOne.forceActiveFocus() })

  function startExport() {
    if (root.exportProblem !== "" || root.busy) return
    var names = root.exportSections.map(function(s) { return s.name })
    var args = ["export", "--sections", root.chosenList(root.exportChosen, names).join(",")]
    if (root.exportChosen["packages"] && root.keptPackages.length !== root.allPackages.length)
      args.push("--packages", root.keptPackages.join(","))
    if (root.destination.indexOf("remote:") === 0)
      args.push("-o", root.info.dataDir + "/exports/" + root.fileNameFull, "--push", root.destination.slice(7))
    else if (root.destination.indexOf("dir:") === 0)
      args.push("-o", root.destination.slice(4) + "/" + root.fileNameFull)
    else if (root.otherIsFile)
      args.push("-o", otherPath.text.trim())
    else
      args.push("-o", otherPath.text.trim().replace(/\/+$/, "") + "/" + root.fileNameFull)
    var secret = root.exportSecrets ? passOne.text : ""
    if (secret) args.push("--passphrase-stdin")
    if (root.service.run("export", args, secret)) {
      passOne.text = ""
      passTwo.text = ""
    }
  }

  // ------------------------------------------------------------------ import

  readonly property var importSections: inspected ? inspected.sections : []
  readonly property bool importSecrets: !!importChosen["secrets"] && !!(inspected && inspected.hasSecrets)
  readonly property bool hardwareChoice: !!(inspected && inspected.hardwareFiles.length && !inspected.sameMachine)
  readonly property var planActions: (service && service.action !== "export" && service.action !== "undo") ? service.plan : []
  readonly property int pendingCount: planActions.filter(function(a) { return a.status === "new" || a.status === "changed" }).length

  onInspectedChanged: {
    var chosen = {}
    if (inspected) for (var i = 0; i < inspected.sections.length; i++) chosen[inspected.sections[i].name] = true
    root.importChosen = chosen
    root.includeHardware = false
  }

  function chosenFile() {
    return root.importFile === "other" ? importPath.text.trim() : root.importFile
  }

  function importArgs(extra) {
    var names = root.importSections.map(function(s) { return s.name })
    var args = ["import", root.inspected.file, "--sections", root.chosenList(root.importChosen, names).join(",")]
    if (root.includeHardware) args.push("--include-hardware")
    if (root.importSecrets) args.push("--passphrase-stdin")
    return args.concat(extra)
  }

  function preview() {
    if (!root.inspected || root.busy) return
    root.service.run("plan", root.importArgs(["--plan-json"]), root.importSecrets ? importPass.text : "")
  }

  function apply() {
    if (!root.inspected || root.busy) return
    if (root.service.run("import", root.importArgs(["--yes"]), root.importSecrets ? importPass.text : ""))
      importPass.text = ""
  }

  function optionsChanged() {
    if (root.service) root.service.clearPlan()
  }

  readonly property var candidateOptions: {
    var out = []
    if (info) for (var i = 0; i < info.candidates.length; i++) {
      var path = info.candidates[i]
      out.push({ value: path, label: path.split("/").pop() + "  —  " + path.slice(0, path.lastIndexOf("/")) })
    }
    out.push({ value: "other", label: "Another file…" })
    return out
  }

  readonly property var planRows: {
    var rows = []
    var bySection = {}
    var order = []
    for (var i = 0; i < planActions.length; i++) {
      var a = planActions[i]
      if (!bySection[a.section]) { bySection[a.section] = { same: 0, items: [] }; order.push(a.section) }
      if (a.status === "same") bySection[a.section].same++
      else bySection[a.section].items.push(a)
    }
    var marks = { "new": "+", "changed": "~", "skipped": "–", "manual": "!" }
    for (var j = 0; j < order.length; j++) {
      var s = bySection[order[j]]
      rows.push({ header: true, text: order[j] + (s.same ? "   (" + s.same + " already the same)" : ""), status: "" })
      for (var k = 0; k < s.items.length; k++)
        rows.push({ header: false, text: (marks[s.items[k].status] || " ") + " " + s.items[k].text + (s.items[k].late && order[j] !== "reload" ? "   [at the end]" : ""), status: s.items[k].status })
    }
    return rows
  }

  // ------------------------------------------------------------------ status

  // ------------------------------------------------------------------ progress and result

  property bool showLog: false

  readonly property string progressTitle: {
    if (!service || !service.busy) return ""
    var what = { "export": "Exporting", "plan": "Working out the changes", "import": "Importing", "undo": "Undoing", "pull": "Downloading" }[service.action] || "Working"
    var count = service.progressTotal > 0 ? " " + Math.min(service.progressDone + 1, service.progressTotal) + " of " + service.progressTotal : ""
    var label = service.progressLabel || service.step
    return what + "\u2026" + count + (label ? "  \u00b7  " + label : "")
  }

  // The finished run to show: this shell's, or the saved one after a restart.
  readonly property var lastResult: {
    if (!service || service.busy || service.resultSeen) return null
    var r = service.result
    if (r && (r.action === "export" || r.action === "import" || r.action === "undo")) return r
    if (!r && service.action === "" && info && info.lastRun) return info.lastRun
    return null
  }
  readonly property bool cardIsEarlier: !!lastResult && lastResult !== (service ? service.result : null)
  readonly property bool cardFailed: !!lastResult && ((lastResult.counts || {}).failed || 0) > 0

  function cardTitle(r) {
    var failed = (r.counts || {}).failed || 0
    var trouble = failed ? " with " + failed + (failed === 1 ? " problem" : " problems") : ""
    var mark = failed ? "\u2717 " : "\u2713 "
    if (r.action === "export") return mark + (failed ? "Export finished" + trouble : "Export saved")
    if (r.action === "import") return mark + "Import finished" + trouble
    return mark + "Undo finished" + trouble
  }

  function cardLines(r) {
    var c = r.counts || {}
    var lines = []
    if (r.action === "export") {
      var path = String(r.file || "")
      lines.push("File:  " + path.slice(path.lastIndexOf("/") + 1))
      lines.push("Folder:  " + path.slice(0, path.lastIndexOf("/")))
      if (r.size) lines.push("Size:  " + (r.size / 1048576).toFixed(1) + " MB")
      if (r.remote) lines.push("Uploaded to:  " + r.remote)
    } else if (r.action === "import") {
      lines.push((c.ok || 0) + " changes applied, " + (c.skipped || 0) + " skipped" + (c.failed ? ", " + c.failed + " failed" : ""))
      if (r.backup) lines.push("Replaced files are backed up; Undo puts them back.")
      if (r.restarting && !root.cardIsEarlier) lines.push("The Omarchy shell restarts in a few seconds to load everything.")
    } else {
      lines.push((c.ok || 0) + " files put back")
    }
    if (root.cardIsEarlier) lines.push("(" + r.time + ")")
    return lines.join("\n")
  }

  function defaultFileName() {
    var host = root.info ? String(root.info.host.host).replace(/[^A-Za-z0-9_-]/g, "-") : "omarchy"
    return "omarchy-settings-" + host + "-" + Qt.formatDateTime(new Date(), "yyyyMMdd-HHmm")
  }

  // Runs a fix the CLI suggested (only the rclone ones it knows) in a terminal.
  function runFix(command) {
    if (!/^rclone config( reconnect [\w.@ -]+:)?$/.test(command)) return
    Quickshell.execDetached(["omarchy-launch-floating-terminal-with-presentation", command])
  }

  property bool nameEdited: false
  function resetFileName() {
    fileNameField.text = root.defaultFileName()
    root.nameEdited = false
  }


  Connections {
    target: root.service
    function onFinished(action, code) {
      root.showLog = false
      if (action === "export" && !root.nameEdited) root.resetFileName()
      if (action === "pull" && root.service.pulled) {
        root.importFile = root.service.pulled
        root.service.inspect(root.service.pulled)
      }
    }
  }

  // ------------------------------------------------------------------ window

  OverlayWindow {
    id: panel
    shown: root.opened
    WlrLayershell.namespace: "omarchy-sync"

    Rectangle { anchors.fill: parent; color: Commons.Color.menu.scrim }
    MouseArea { anchors.fill: parent; onClicked: root.dismiss() }

    BorderSurface {
      id: card
      width: Math.min(Style.space(860), panel.width - Style.gapsOut * 2)
      height: Math.min(Style.space(780), panel.height - Style.gapsOut * 2)
      anchors.centerIn: parent
      radius: Style.cornerRadius
      color: root.background
      borderSpec: Border.surfaceSpec("menu", "border", Commons.Color.menu.border, Math.max(1, Style.space(2)))
      padding: Style.spacing.panelPadding

      MouseArea { anchors.fill: parent; onClicked: keyCatcher.forceActiveFocus() }

      Item {
        id: keyCatcher
        anchors.fill: parent
        focus: true
        Keys.priority: Keys.BeforeItem
        Keys.onPressed: function(event) {
          if (confirm.handleKey(event)) { event.accepted = true; return }
          if (event.key === Qt.Key_Escape) {
            root.dismiss()
            event.accepted = true
          }
        }

        ColumnLayout {
          anchors.fill: parent
          anchors.topMargin: card.contentTopInset
          anchors.rightMargin: card.contentRightInset
          anchors.bottomMargin: card.contentBottomInset
          anchors.leftMargin: card.contentLeftInset
          spacing: Style.spacing.panelGap

          RowLayout {
            Layout.fillWidth: true
            spacing: Style.spacing.controlGap

            ColumnLayout {
              spacing: 0
              Text {
                textFormat: Text.PlainText
                text: "Omarchy Sync"
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.title
                font.bold: true
              }
              Text {
                textFormat: Text.PlainText
                text: root.info ? (root.info.host.host + " · " + root.info.host.platform + " · Omarchy " + root.info.host.omarchy) : ""
                color: root.foreground
                opacity: 0.6
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }

            Item { Layout.fillWidth: true }

            ButtonGroup {
              value: root.tab
              options: [
                { value: "export", label: "Export" },
                { value: "import", label: "Import" },
                { value: "undo", label: "Undo" }
              ]
              onChanged: function(value) { root.tab = value }
            }

            Button {
              text: "Close"
              bordered: true
              onClicked: root.dismiss()
            }
          }

          PanelSeparator { Layout.fillWidth: true; foreground: root.foreground }

          // ---------------------------------------------------------- pages
          StackLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: root.tab === "export" ? 0 : (root.tab === "import" ? 1 : 2)

            // ======================================================== EXPORT
            // Only the section switches and package list scroll. The passphrase, Save to
            // and Export sit under them and never scroll out of sight.
            ColumnLayout {
              spacing: Style.spacing.controlGap

              Flickable {
                id: exportFlick
                Layout.minimumHeight: Style.space(120)
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                contentWidth: width
                contentHeight: exportColumn.implicitHeight
                boundsBehavior: Flickable.StopAtBounds
                QQC.ScrollBar.vertical: QQC.ScrollBar { policy: size < 1.0 ? QQC.ScrollBar.AlwaysOn : QQC.ScrollBar.AsNeeded }

                ColumnLayout {
                  id: exportColumn
                  width: parent.width - Style.spacing.md
                  spacing: Style.spacing.controlGap

                  PanelSectionHeader { text: "What to export"; foreground: root.foreground }

                  GridLayout {
                    Layout.fillWidth: true
                    columns: 2
                    columnSpacing: Style.spacing.controlGap
                    rowSpacing: Style.spacing.controlGap

                    Repeater {
                      model: root.exportSections
                      delegate: Toggle {
                        required property var modelData
                        Layout.fillWidth: true
                        label: root.sectionLabel(modelData.title)
                        description: root.sectionDetail(modelData.title)
                        checked: !!root.exportChosen[modelData.name]
                        foreground: root.foreground
                        onClicked: root.setKey("exportChosen", modelData.name, !checked)
                      }
                    }
                  }

                  // Extra packages: all go unless untucked here.
                  RowLayout {
                    visible: !!root.exportChosen["packages"] && root.allPackages.length > 0
                    Layout.fillWidth: true
                    Button {
                      text: (root.showPackages ? "Hide packages" : "Choose packages") + "  (" + root.keptPackages.length + " of " + root.allPackages.length + ")"
                      bordered: true
                      onClicked: root.showPackages = !root.showPackages
                    }
                    Item { Layout.fillWidth: true }
                  }

                  Flow {
                    visible: root.showPackages && !!root.exportChosen["packages"]
                    Layout.fillWidth: true
                    spacing: Style.spacing.xs
                    Repeater {
                      model: root.allPackages
                      delegate: Button {
                        required property var modelData
                        text: modelData
                        bordered: true
                        selected: !root.excludedPackages[modelData]
                        tooltipText: selected ? "Carried; click to leave it out" : "Left out; click to carry it"
                        onClicked: root.setKey("excludedPackages", modelData, !root.excludedPackages[modelData])
                      }
                    }
                  }
                }
              }

              // Where and how: always on screen, under the scrolling choices.
              ColumnLayout {
                id: secretsBlock
                visible: root.exportSecrets
                Layout.fillWidth: true
                spacing: Style.spacing.xs
                PanelSectionHeader { text: "Secrets passphrase (needed again on the other computer)"; foreground: root.foreground }
                RowLayout {
                  Layout.fillWidth: true
                  spacing: Style.spacing.controlGap
                  TextField {
                    id: passOne
                    Layout.fillWidth: true
                    password: true
                    placeholderText: "Passphrase, 8 characters or more"
                    foreground: root.foreground
                    Keys.onEscapePressed: keyCatcher.forceActiveFocus()
                  }
                  TextField {
                    id: passTwo
                    Layout.fillWidth: true
                    password: true
                    placeholderText: "Same passphrase again"
                    foreground: root.foreground
                    Keys.onEscapePressed: keyCatcher.forceActiveFocus()
                  }
                }
              }

              PanelSectionHeader { text: "Save to"; foreground: root.foreground }

              Dropdown {
                Layout.fillWidth: true
                showLabel: false
                options: root.destinations
                value: root.destination
                onChanged: function(value) { root.destination = value }
              }

              TextField {
                id: otherPath
                visible: root.destination === "other"
                Layout.fillWidth: true
                placeholderText: "A folder, or a full path ending in .tar.gz"
                foreground: root.foreground
                Keys.onEscapePressed: keyCatcher.forceActiveFocus()
              }

              RowLayout {
                visible: !root.otherIsFile
                Layout.fillWidth: true
                spacing: Style.spacing.controlGap
                Text {
                  textFormat: Text.PlainText
                  text: "File name"
                  color: root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                }
                TextField {
                  id: fileNameField
                  Layout.fillWidth: true
                  placeholderText: "omarchy-settings-\u2026"
                  foreground: root.foreground
                  onTextEdited: root.nameEdited = true
                  Keys.onEscapePressed: keyCatcher.forceActiveFocus()
                }
                Text {
                  textFormat: Text.PlainText
                  text: ".tar.gz"
                  visible: !/\.tar\.gz$/.test(fileNameField.text.trim())
                  color: root.foreground
                  opacity: 0.6
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                }
                Button {
                  text: "Reset"
                  bordered: true
                  tooltipText: "Back to the default: hostname, date and time"
                  onClicked: root.resetFileName()
                }
              }

              RowLayout {
                Layout.fillWidth: true
                spacing: Style.spacing.controlGap
                Button {
                  text: "Export"
                  bordered: true
                  active: root.exportProblem === "" && !root.busy
                  accent: root.accent
                  onClicked: root.startExport()
                }
                Text {
                  Layout.fillWidth: true
                  textFormat: Text.PlainText
                  text: root.exportProblem
                  color: root.foreground
                  opacity: 0.65
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  elide: Text.ElideRight
                }
              }
            }

            // ======================================================== IMPORT
            ColumnLayout {
              spacing: Style.spacing.controlGap

              Flickable {
                Layout.fillWidth: true
                Layout.fillHeight: true
                clip: true
                contentWidth: width
                contentHeight: importColumn.implicitHeight
                boundsBehavior: Flickable.StopAtBounds
                QQC.ScrollBar.vertical: QQC.ScrollBar { policy: size < 1.0 ? QQC.ScrollBar.AlwaysOn : QQC.ScrollBar.AsNeeded }

                ColumnLayout {
                  id: importColumn
                  width: parent.width - Style.spacing.md
                  spacing: Style.spacing.controlGap

                  PanelSectionHeader { text: "Export file"; foreground: root.foreground }

                  Dropdown {
                    Layout.fillWidth: true
                    showLabel: false
                    options: root.candidateOptions
                    value: root.importFile
                    onChanged: function(value) { root.importFile = value }
                  }

                  TextField {
                    id: importPath
                    visible: root.importFile === "other"
                    Layout.fillWidth: true
                    placeholderText: "Path to omarchy-settings-….tar.gz"
                    foreground: root.foreground
                    Keys.onEscapePressed: keyCatcher.forceActiveFocus()
                  }

                  RowLayout {
                    Layout.fillWidth: true
                    spacing: Style.spacing.controlGap
                    Button {
                      text: root.service && root.service.inspecting ? "Reading…" : "Read"
                      bordered: true
                      active: root.chosenFile() !== "" && !root.busy
                      onClicked: if (!root.busy) root.service.inspect(root.chosenFile())
                    }
                    Button {
                      visible: !!(root.info && root.info.rclone["default"])
                      text: "Fetch newest from " + (root.info ? root.info.rclone["default"] : "")
                      bordered: true
                      onClicked: if (!root.busy) root.service.run("pull", ["pull", root.info.rclone["default"], "-o", root.info.dataDir + "/downloads"], "")
                    }
                    Item { Layout.fillWidth: true }
                  }

                  Text {
                    visible: !!(root.service && root.service.inspectError)
                    Layout.fillWidth: true
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    text: root.service ? "✗ " + root.service.inspectError : ""
                    color: root.urgent
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.body
                  }

                  ColumnLayout {
                    visible: !!root.inspected
                    Layout.fillWidth: true
                    spacing: Style.spacing.controlGap

                    Text {
                      Layout.fillWidth: true
                      textFormat: Text.PlainText
                      wrapMode: Text.Wrap
                      text: root.inspected ? root.inspected.describe : ""
                      color: root.foreground
                      font.family: root.fontFamily
                      font.pixelSize: Style.font.body
                    }

                    PanelSectionHeader { text: "What to apply here"; foreground: root.foreground }

                    GridLayout {
                      Layout.fillWidth: true
                      columns: 2
                      columnSpacing: Style.spacing.controlGap
                      rowSpacing: Style.spacing.controlGap

                      Repeater {
                        model: root.importSections
                        delegate: Toggle {
                          required property var modelData
                          Layout.fillWidth: true
                          label: root.sectionLabel(modelData.title)
                          description: root.sectionDetail(modelData.title)
                          checked: !!root.importChosen[modelData.name]
                          foreground: root.foreground
                          onClicked: {
                            root.setKey("importChosen", modelData.name, !checked)
                            root.optionsChanged()
                          }
                        }
                      }

                      Toggle {
                        visible: root.hardwareChoice
                        Layout.fillWidth: true
                        label: "Also copy screens and input"
                        description: root.inspected ? root.inspected.hardwareFiles.join(", ") + " from " + root.inspected.source.host + "; usually not" : ""
                        checked: root.includeHardware
                        foreground: root.foreground
                        onClicked: {
                          root.includeHardware = !checked
                          root.optionsChanged()
                        }
                      }
                    }

                    TextField {
                      id: importPass
                      visible: root.importSecrets
                      Layout.fillWidth: true
                      password: true
                      placeholderText: "Passphrase for the secrets"
                      foreground: root.foreground
                      Keys.onEscapePressed: keyCatcher.forceActiveFocus()
                    }

                    Repeater {
                      model: root.planRows
                      delegate: Text {
                        required property var modelData
                        Layout.fillWidth: true
                        Layout.topMargin: modelData.header ? Style.spacing.xs : 0
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                        text: modelData.text
                        color: modelData.status === "skipped" ? Qt.darker(root.foreground, 1.4) : root.foreground
                        opacity: modelData.status === "skipped" ? 0.7 : 1
                        font.family: root.fontFamily
                        font.pixelSize: modelData.header ? Style.font.bodySmall : Style.font.body
                        font.bold: modelData.header
                        leftPadding: modelData.header ? 0 : Style.spacing.md
                      }
                    }
                  }
                }
              }

              RowLayout {
                visible: !!root.inspected
                Layout.fillWidth: true
                spacing: Style.spacing.controlGap
                Button {
                  text: "Preview changes"
                  bordered: true
                  active: !root.busy && !(root.importSecrets && importPass.text === "")
                  onClicked: if (!(root.importSecrets && importPass.text === "")) root.preview()
                }
                Button {
                  text: root.pendingCount ? "Apply " + root.pendingCount + " changes" : "Apply"
                  bordered: true
                  active: !root.busy && root.pendingCount > 0 && root.service.action === "plan"
                  onClicked: if (active) { root.confirmKind = "import"; confirm.opened = true }
                }
                Text {
                  Layout.fillWidth: true
                  textFormat: Text.PlainText
                  text: root.planActions.length && !root.pendingCount && root.service.action === "plan" ? "Nothing to change: this computer already matches." : ""
                  color: root.foreground
                  opacity: 0.65
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                  elide: Text.ElideRight
                }
              }
            }

            // ======================================================== UNDO
            ColumnLayout {
              spacing: Style.spacing.controlGap

              PanelSectionHeader { text: "Imports you can undo"; foreground: root.foreground }

              Text {
                visible: !(root.info && root.info.backups.length)
                textFormat: Text.PlainText
                text: "No import to undo on this computer."
                color: root.foreground
                opacity: 0.65
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
              }

              Repeater {
                model: root.info ? root.info.backups : []
                delegate: Text {
                  required property var modelData
                  required property int index
                  Layout.fillWidth: true
                  textFormat: Text.PlainText
                  elide: Text.ElideRight
                  text: (index === 0 ? "▸ " : "   ") + modelData.name + "   from " + (modelData.host || "?") + "   ·   " + modelData.files + " files"
                  color: root.foreground
                  opacity: index === 0 ? 1 : 0.65
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.body
                }
              }

              Button {
                visible: !!(root.info && root.info.backups.length)
                text: "Undo the last import"
                bordered: true
                active: !root.busy
                onClicked: if (!root.busy) { root.confirmKind = "undo"; confirm.opened = true }
              }

              Text {
                Layout.fillWidth: true
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
                text: "Undo puts back every file the import replaced or created. Packages, plugins and themes it installed stay."
                color: root.foreground
                opacity: 0.65
                font.family: root.fontFamily
                font.pixelSize: Style.font.bodySmall
              }

              Item { Layout.fillHeight: true }
            }
          }

          PanelSeparator { Layout.fillWidth: true; foreground: root.foreground }

          // ---------------------------------------------------------- progress, result, log
          ColumnLayout {
            visible: root.busy
            Layout.fillWidth: true
            spacing: Style.spacing.xs

            Text {
              Layout.fillWidth: true
              textFormat: Text.PlainText
              text: root.progressTitle
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
              elide: Text.ElideRight
            }

            Rectangle {
              id: track
              Layout.fillWidth: true
              Layout.preferredHeight: Style.space(6)
              radius: height / 2
              color: Util.alpha(root.foreground, 0.15)
              clip: true

              // Known steps fill left to right; an unknown length sweeps back and forth.
              readonly property bool known: !!root.service && root.service.progressTotal > 0
              property real sweep: 0
              SequentialAnimation on sweep {
                running: root.busy && root.opened && !track.known
                loops: Animation.Infinite
                NumberAnimation { from: 0; to: 1; duration: 900; easing.type: Easing.InOutQuad }
                NumberAnimation { from: 1; to: 0; duration: 900; easing.type: Easing.InOutQuad }
              }

              Rectangle {
                height: parent.height
                radius: parent.radius
                color: root.accent
                width: track.known
                  ? track.width * Math.min(1, root.service.progressDone / root.service.progressTotal)
                  : track.width * 0.25
                x: track.known ? 0 : track.sweep * (track.width - width)
                Behavior on width { enabled: track.known; NumberAnimation { duration: 250 } }
              }
            }
          }

          Rectangle {
            visible: !!root.lastResult
            Layout.fillWidth: true
            Layout.preferredHeight: cardColumn.implicitHeight + Style.spacing.panelPadding
            radius: Style.cornerRadius
            color: Util.alpha(root.cardFailed ? root.urgent : root.accent, 0.10)
            border.width: Math.max(1, Style.space(1))
            border.color: root.cardFailed ? root.urgent : root.accent

            ColumnLayout {
              id: cardColumn
              anchors.left: parent.left
              anchors.right: parent.right
              anchors.verticalCenter: parent.verticalCenter
              anchors.leftMargin: Style.spacing.panelPadding
              anchors.rightMargin: Style.spacing.panelPadding
              spacing: Style.spacing.xs

              RowLayout {
                Layout.fillWidth: true
                Text {
                  Layout.fillWidth: true
                  textFormat: Text.PlainText
                  text: root.lastResult ? root.cardTitle(root.lastResult) : ""
                  color: root.cardFailed ? root.urgent : root.foreground
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.subtitle
                  font.bold: true
                  elide: Text.ElideRight
                }
                Button {
                  text: "Dismiss"
                  bordered: true
                  onClicked: root.service.resultSeen = true
                }
              }

              Text {
                Layout.fillWidth: true
                textFormat: Text.PlainText
                wrapMode: Text.Wrap
                text: root.lastResult ? root.cardLines(root.lastResult) : ""
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                lineHeight: 1.15
              }

              Repeater {
                model: root.lastResult ? (root.lastResult.failures || []) : []
                delegate: Text {
                  required property var modelData
                  Layout.fillWidth: true
                  textFormat: Text.PlainText
                  wrapMode: Text.Wrap
                  text: "\u2717 " + modelData.section + ": " + modelData.text
                  color: root.urgent
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.bodySmall
                }
              }

              RowLayout {
                Layout.fillWidth: true
                spacing: Style.spacing.controlGap
                Button {
                  visible: !!root.lastResult && root.lastResult.action === "export"
                  text: "Open folder"
                  bordered: true
                  onClicked: {
                    var f = String(root.lastResult.file)
                    Quickshell.execDetached(["xdg-open", f.slice(0, f.lastIndexOf("/"))])
                  }
                }
                Button {
                  visible: !!root.lastResult && root.lastResult.action === "export"
                  text: "Copy path"
                  bordered: true
                  onClicked: Quickshell.execDetached(["wl-copy", "--", String(root.lastResult.file)])
                }
                Button {
                  visible: !!root.lastResult && !!root.lastResult.fix
                  text: root.lastResult && root.lastResult.fix ? "Fix: " + root.lastResult.fix : ""
                  bordered: true
                  tooltipText: "Opens a terminal for it; try again afterwards"
                  onClicked: root.runFix(root.lastResult.fix)
                }
                Button {
                  visible: !!root.lastResult && root.lastResult.action === "import" && !!root.lastResult.backup
                  text: "Undo this import"
                  bordered: true
                  onClicked: root.tab = "undo"
                }
                Button {
                  visible: !!root.lastResult && !root.cardIsEarlier && logView.count > 0
                  text: root.showLog ? "Hide details" : "Show details"
                  bordered: true
                  onClicked: root.showLog = !root.showLog
                }
                Item { Layout.fillWidth: true }
              }
            }
          }

          RowLayout {
            visible: !root.busy && !!root.service && root.service.error !== ""
            Layout.fillWidth: true
            spacing: Style.spacing.controlGap
            Text {
              Layout.fillWidth: true
              textFormat: Text.PlainText
              wrapMode: Text.Wrap
              text: root.service ? "\u2717 " + root.service.error : ""
              color: root.urgent
              font.family: root.fontFamily
              font.pixelSize: Style.font.body
            }
            Button {
              visible: !!root.service && root.service.errorFix !== ""
              text: root.service ? "Fix: " + root.service.errorFix : ""
              bordered: true
              tooltipText: "Opens a terminal for it; try again afterwards"
              onClicked: root.runFix(root.service.errorFix)
            }
          }

          ListView {
            id: logView
            Layout.fillWidth: true
            Layout.preferredHeight: Style.space(150)
            visible: count > 0 && (root.busy || root.showLog || !root.lastResult)
            clip: true
            model: root.service ? root.service.log : null
            boundsBehavior: Flickable.StopAtBounds
            QQC.ScrollBar.vertical: QQC.ScrollBar { policy: size < 1.0 ? QQC.ScrollBar.AlwaysOn : QQC.ScrollBar.AsNeeded }
            onCountChanged: Qt.callLater(function() { logView.positionViewAtEnd() })
            delegate: Text {
              required property string line
              required property string kind
              width: logView.width - Style.spacing.md
              textFormat: Text.PlainText
              elide: Text.ElideRight
              text: line
              color: kind === "failed" || kind === "err" ? root.urgent : root.foreground
              opacity: kind === "out" || kind === "err" ? 0.7 : 1
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
            }
          }
        }
      }

      ConfirmDialog {
        id: confirm
        anchors.fill: parent
        z: 10
        message: root.confirmKind === "undo"
          ? "Put back the files the last import replaced?"
          : "Apply " + root.pendingCount + " changes? Every file replaced is backed up first."
        confirmText: root.confirmKind === "undo" ? "Undo" : "Apply"
        onCanceled: { opened = false; keyCatcher.forceActiveFocus() }
        onConfirmed: {
          opened = false
          if (root.confirmKind === "undo") root.service.run("undo", ["undo"], "")
          else root.apply()
          keyCatcher.forceActiveFocus()
        }
      }
    }
  }
}
