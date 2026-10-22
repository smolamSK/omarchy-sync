import QtQuick
import Quickshell
import Quickshell.Io

// Owns every run of bin/omarchy-sync for the GUI (SyncWindow.qml). The window is
// unloaded when it is hidden, so a run that lived there would die with it; here an
// export or import keeps going when the window closes, and reopening shows it again.
//
// The CLI runs with --events: lines that are JSON objects with an "event" key drive
// the state below (step / running / plan / done / error); every other line is log.
// A passphrase is written to the process's stdin (--passphrase-stdin), never argv.
//
// At login it also runs `service-start` once: it keeps the app launcher entry current
// and, the first time only, offers (by notification) to add the Omarchy menu rows.
Item {
  id: root
  width: 0
  height: 0
  visible: false

  property var shell: null
  property var manifest: null
  property string omarchyPath: ""

  readonly property string script:
    Qt.resolvedUrl("bin/omarchy-sync").toString().replace(/^file:\/\//, "")

  // `omarchy-sync info --json`: sections, places, rclone, candidates, backups, packages.
  property var info: null
  property bool loadingInfo: false
  // `omarchy-sync inspect FILE --json` for the import tab.
  property var inspected: null
  property string inspectError: ""
  property bool inspecting: false

  // The run in progress (or the last one): "export", "plan", "import", "undo" or "pull".
  property string action: ""
  property bool busy: false
  property string step: ""
  property string error: ""
  // A command that fixes the error (e.g. `rclone config reconnect gdrive:`), from the CLI.
  property string errorFix: ""
  property var plan: []
  property var result: null
  property string pulled: ""
  // Progress of the run: done of total steps (total 0 = unknown, the bar just moves).
  property int progressDone: 0
  property int progressTotal: 0
  property string progressLabel: ""
  // Set by the window: a run that ends while it's closed is announced by notification.
  property bool windowOpen: false
  // The window's result card was dismissed.
  property bool resultSeen: false
  readonly property alias log: logModel

  property string pendingSecret: ""
  property string lastLine: ""

  signal finished(string action, int code)

  ListModel { id: logModel }

  function refreshInfo() {
    if (infoProc.running) return
    root.loadingInfo = true
    infoProc.running = true
  }

  function inspect(file) {
    if (inspectProc.running || !file) return
    root.inspected = null
    root.inspectError = ""
    root.plan = []
    root.inspecting = true
    inspectProc.command = [root.script, "inspect", file, "--json"]
    inspectProc.running = true
  }

  function clearPlan() {
    if (root.action === "plan" && !root.busy) root.plan = []
  }

  // run(kind, args, secret): start `omarchy-sync --events ARGS`; false when busy.
  function run(kind, args, secret) {
    if (root.busy) return false
    root.action = kind
    root.step = ""
    root.error = ""
    root.errorFix = ""
    root.result = null
    root.resultSeen = false
    root.progressDone = 0
    root.progressTotal = 0
    root.progressLabel = ""
    root.lastLine = ""
    if (kind === "plan" || kind === "import") root.plan = kind === "plan" ? [] : root.plan
    if (kind === "pull") root.pulled = ""
    logModel.clear()
    root.pendingSecret = secret || ""
    runner.command = [root.script, "--events"].concat(args)
    root.busy = true
    runner.running = true
    return true
  }

  function addLog(text, kind) {
    if (logModel.count > 3000) logModel.remove(0, 500)
    logModel.append({ "line": String(text), "kind": kind })
  }

  function readLine(line, isError) {
    if (line.indexOf("{\"event\"") === 0) {
      let ev = null
      try { ev = JSON.parse(line) } catch (e) { ev = null }
      if (ev) {
        root.handleEvent(ev)
        return
      }
    }
    if (line.trim() === "") return
    root.lastLine = line
    root.addLog(line, isError ? "err" : "out")
  }

  function handleEvent(ev) {
    var marks = { "ok": "✓", "note": "·", "skipped": "–", "failed": "✗" }
    if (ev.event === "step") {
      root.addLog((marks[ev.status] || " ") + " " + ev.section + ": " + ev.text, ev.status)
    } else if (ev.event === "running") {
      root.step = ev.section + ": " + ev.text
    } else if (ev.event === "progress") {
      root.progressDone = ev.done || 0
      root.progressTotal = ev.total || 0
      root.progressLabel = ev.label || ""
    } else if (ev.event === "plan") {
      root.plan = ev.actions || []
    } else if (ev.event === "done") {
      root.result = ev
    } else if (ev.event === "error") {
      root.error = ev.text || "failed"
      root.errorFix = ev.fix || ""
    }
  }

  Process {
    id: runner
    environment: ({ OMARCHY_SYNC_GUI: "1" })
    stdinEnabled: true
    onStarted: {
      // Over stdin, never argv: argv is visible to every user. Closing stdin right
      // after also keeps any command the CLI runs from waiting for input.
      if (root.pendingSecret !== "") write(root.pendingSecret + "\n")
      root.pendingSecret = ""
      stdinEnabled = false
    }
    stdout: SplitParser { onRead: function(line) { root.readLine(line, false) } }
    stderr: SplitParser { onRead: function(line) { root.readLine(line, true) } }
    // Output may still be in flight when the process exits; settle a moment later.
    onExited: function(code) {
      settle.code = code
      settle.restart()
    }
  }

  Timer {
    id: settle
    property int code: 0
    interval: 250
    onTriggered: {
      if (code !== 0 && root.error === "" && !(root.result && root.result.ok))
        root.error = root.lastLine || ("omarchy-sync stopped (exit " + code + ")")
      if (root.action === "pull" && code === 0 && /\.tar\.gz$/.test(root.lastLine)) root.pulled = root.lastLine
      root.busy = false
      root.step = ""
      runner.stdinEnabled = true
      // Closed window: say it's done. After an import or undo from here the shell restarts,
      // and the detached restart announces the result itself once the new shell is up.
      var announced = root.action === "export" || root.action === "import" || root.action === "undo"
      if (announced && !root.windowOpen && root.result && !root.result.restarting)
        Quickshell.execDetached([root.script, "notify-last"])
      root.finished(root.action, code)
      root.refreshInfo()
    }
  }

  Process {
    id: infoProc
    command: [root.script, "info", "--json"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try { root.info = JSON.parse(text) } catch (e) { console.warn("omarchy-sync: info unreadable") }
        root.loadingInfo = false
      }
    }
  }

  Process {
    id: inspectProc
    // `inspect` always exits 0 and answers with one JSON object ({"error": …} on failure).
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var answer = null
        try { answer = JSON.parse(text) } catch (e) { answer = null }
        if (!answer) root.inspectError = "omarchy-sync gave no answer"
        else if (answer.error) root.inspectError = answer.error
        else root.inspected = answer
        root.inspecting = false
      }
    }
  }

  Process {
    id: start
    command: [root.script, "service-start"]
    stderr: StdioCollector {
      onStreamFinished: if (text.trim() !== "") console.warn("omarchy-sync:", text.trim())
    }
  }

  Timer {
    interval: 8000
    running: true
    repeat: false
    onTriggered: start.running = true
  }
}
