import QtQuick
import Quickshell
import Quickshell.Io

// Owns every run of bin/omarchy-sync for the GUI (SyncWindow.qml). The window is
// unloaded when it is hidden, and the shell destroys this service too whenever it
// reloads plugins (an import that adds one triggers that) or restarts. So a run is not
// our child: `omarchy-sync job start` launches it in its own session and it writes
// job.log, which we follow. A new instance of this service finds a run still going
// (`job status --json`) and replays its log, so reopening the window shows it again.
//
// The CLI runs with --events: lines that are JSON objects with an "event" key drive
// the state below (step / running / plan / done / error / log / exit); every other
// line is log. A passphrase is written to stdin (--passphrase-stdin), never argv.
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
  // The run's exit code from its `exit` event; -1 until it arrives.
  property int exitCode: -1
  // An import or undo finished while the window was open: the shell restart it needs
  // waits until the window closes (or Restart now), so the result can be read first.
  property bool restartPending: false
  readonly property string pluginId: (manifest && manifest.id) || "io.github.smolamsk.sync"

  // The job remembers this, so a plugin reload that closes the window mid-run can reopen it.
  onWindowOpenChanged: if (root.busy) root.noteWindow()

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

  // run(kind, args, secret): start `omarchy-sync --events ARGS` as a job; false when busy.
  function run(kind, args, secret) {
    if (root.busy) return false
    root.reset(kind)
    root.pendingSecret = secret || ""
    runner.command = [root.script, "job", "start", kind, "--"].concat(args)
    root.busy = true
    runner.running = true
    return true
  }

  function reset(kind) {
    root.action = kind
    root.restartPending = false
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
    root.exitCode = -1
    logModel.clear()
  }

  function noteWindow() {
    Quickshell.execDetached([root.script, "job", "window", root.windowOpen ? "open" : "closed"])
  }

  // Restart the shell to load what an import brought (plugins, bar, widgets); with
  // notify, the result is announced once the new shell is up.
  function restartShell(notify) {
    root.restartPending = false
    Quickshell.execDetached([root.script, "restart-shell"].concat(notify ? ["--notify"] : []))
  }

  // Follow a job's log from the top until its supervisor has exited.
  function follow(job) {
    follower.command = ["tail", "-c", "+1", "-s", "0.3", "-F", "--pid=" + job.pid, job.log]
    follower.running = true
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
    } else if (ev.event === "log") {
      root.readLine(ev.text || "", ev.stream === "err")
    } else if (ev.event === "exit") {
      root.exitCode = ev.code
    }
  }

  // `job start`: hands the run to its own session and answers with one `job` event.
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
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var ev = null
        try { ev = JSON.parse(text.trim().split("\n").pop()) } catch (e) { ev = null }
        runner.stdinEnabled = true
        if (ev && ev.event === "job") {
          root.follow(ev)
          root.noteWindow()
        } else {
          root.error = ev && ev.text ? ev.text : "omarchy-sync didn't start"
          root.busy = false
        }
      }
    }
  }

  // The run's log; tail ends by itself once the run's supervisor has exited.
  Process {
    id: follower
    stdout: SplitParser { onRead: function(line) { root.readLine(line, false) } }
    // Output may still be in flight when tail exits; settle a moment later.
    onExited: function(code) {
      settle.code = root.exitCode >= 0 ? root.exitCode : 1
      settle.restart()
    }
  }

  // A run that a previous instance of this service started (before a plugin reload or
  // a shell restart) is still ours to show. If the reload closed the window just now,
  // open it again. Asked a moment after load, so the closed window has been noted.
  Process {
    id: resume
    command: [root.script, "job", "status", "--json"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var job = null
        try { job = JSON.parse(text) } catch (e) { job = null }
        if (!job || !job.running || root.busy) return
        root.reset(job.kind)
        root.busy = true
        root.follow(job)
        if (job.window === "closed" && job.windowAgo !== undefined && job.windowAgo < 5)
          Quickshell.execDetached(["omarchy-shell", "shell", "summon", root.pluginId,
                                   JSON.stringify({ tab: job.kind === "export" || job.kind === "undo" ? job.kind : "import" })])
      }
    }
  }

  Timer {
    interval: 1000
    running: true
    onTriggered: resume.running = true
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
      // An import or undo needs a shell restart: with the window open it waits until the
      // window closes, so the card can be read; closed, it restarts now and the new shell
      // announces the result. Otherwise a closed window gets a notification.
      var announced = root.action === "export" || root.action === "import" || root.action === "undo"
      if (announced && root.result && root.result.restarting) {
        if (root.windowOpen) root.restartPending = true
        else root.restartShell(true)
      } else if (announced && !root.windowOpen && root.result) {
        Quickshell.execDetached([root.script, "notify-last"])
      }
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
