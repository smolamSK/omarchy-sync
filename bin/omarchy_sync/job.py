"""GUI runs that outlive the shell: `job start`, `job run` and `job status`.

The shell kills every plugin service when it reloads its plugins, which an import
triggers itself as soon as it adds one, and again when it restarts. A run started as a
child of Service.qml died with it. So the window starts runs here instead: `job start`
launches a supervisor (`job run`) in its own session and returns at once. The
supervisor runs `omarchy-sync --events ARGS` and writes every line to job.log, the
child's stderr wrapped as `log` events so the window can still tell them apart, and
ends with an `exit` event. Service.qml follows job.log with `tail -F --pid`, and after
a reload or restart it finds the run again with `job status --json` and replays the
log from the top.
"""

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import common


def _paths() -> tuple:
    d = common.state_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / "job.json", d / "job.log"


def _script() -> str:
    return str(Path(__file__).resolve().parent.parent / "omarchy-sync")


def _alive(pid: int) -> bool:
    try:
        return b"omarchy_sync" in Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False


def status() -> dict:
    """The current or last run: kind, pid, log, started, and code/ended once it's over."""
    record, _ = _paths()
    job = common.read_json(record)
    if not isinstance(job, dict) or not job.get("pid"):
        return {"running": False}
    job["running"] = "ended" not in job and _alive(int(job["pid"]))
    return job


def start(kind: str, args: list) -> int:
    """Launch the supervisor and print one `job` event; any passphrase passes through stdin."""
    if status().get("running"):
        raise RuntimeError("Another Omarchy Sync run is still going")
    secret = sys.stdin.readline() if "--passphrase-stdin" in args else ""
    record, log = _paths()
    with open(log, "w") as out:
        proc = subprocess.Popen([_script(), "job", "run", "--", *args],
                                stdin=subprocess.PIPE, stdout=out, stderr=out,
                                start_new_session=True, text=True)
    job = {"kind": kind, "pid": proc.pid, "log": str(log), "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    common.write_json(record, job)
    # Out of argv, as before: the pipe holds the line until the run reads it.
    proc.stdin.write(secret)
    proc.stdin.close()
    print(json.dumps({"event": "job", **job}, ensure_ascii=False), flush=True)
    return 0


def run(args: list) -> int:
    """The supervisor: run the CLI, copy its output to stdout (job.log), then `exit`."""
    lock = threading.Lock()

    def write(line: str) -> None:
        with lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    child = subprocess.Popen([_script(), "--events", *args], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, errors="replace")

    def errors() -> None:
        for line in child.stderr:
            write(json.dumps({"event": "log", "stream": "err", "text": line.rstrip("\n")}, ensure_ascii=False))

    reader = threading.Thread(target=errors, daemon=True)
    reader.start()
    for line in child.stdout:
        write(line.rstrip("\n"))
    code = child.wait()
    reader.join(timeout=2)
    write(json.dumps({"event": "exit", "code": code}))
    record, _ = _paths()
    job = common.read_json(record)
    if isinstance(job, dict) and job.get("pid") == os.getpid():
        job.update(code=code, ended=time.strftime("%Y-%m-%d %H:%M:%S"))
        common.write_json(record, job)
    return code


def main(argv: list) -> int:
    action = argv[0] if argv else ""
    rest = argv[1:]
    if rest[:1] == ["--"]:
        rest = rest[1:]
    if action == "status":
        print(json.dumps(status(), ensure_ascii=False))
        return 0
    if action == "run":
        return run(rest)
    if action == "start" and rest:
        kind, rest = rest[0], rest[1:]
        if rest[:1] == ["--"]:
            rest = rest[1:]
        try:
            return start(kind, rest)
        except (RuntimeError, OSError) as e:
            print(json.dumps({"event": "error", "text": str(e)}, ensure_ascii=False), flush=True)
            return 1
    print("usage: omarchy-sync job start KIND -- ARGS | job status --json", file=sys.stderr)
    return 2
