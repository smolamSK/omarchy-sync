"""Shared helpers: paths, commands, the run report and file copying."""

import filecmp
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

PLUGIN_ID = "io.github.smolamsk.sync"
FORMAT = 1
SKEL = Path("/etc/skel")
OMARCHY_PATH = Path(os.environ.get("OMARCHY_PATH", "/usr/share/omarchy"))

# Files bigger than this are left out of the export and named in the report.
MAX_FILE = 25 * 1024 * 1024

# Hardware families that need different packages, see omarchy-hw-platform.
VM_TOGGLES = {"no-animations", "window-opaque"}


# --events: JSON lines on stdout for the GUI (see emit). Plain output is unchanged otherwise.
EVENTS = False


def emit(event: str, **fields) -> None:
    if EVENTS:
        print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def gui() -> bool:
    """True when the GUI runs us: no terminal, so sudo is replaced by pkexec (polkit)."""
    return os.environ.get("OMARCHY_SYNC_GUI") == "1"


def restart_shell() -> None:
    """omarchy-restart-shell. From the GUI the window does it (`restart-shell`) once the
    result has been seen, because the restart takes the window with it."""
    if not gui():
        run(["omarchy-restart-shell"])


def restart_shell_later(notify: bool) -> None:
    """omarchy-restart-shell in its own session a moment later, so the shell's own
    service that asked for it isn't waiting on it. With notify, the result is announced
    by `notify-last` once the new shell, which owns the notification daemon, is up."""
    script = shlex.quote(str(Path(__file__).resolve().parent.parent / "omarchy-sync"))
    after = f"; exec {script} notify-last" if notify else ""
    subprocess.Popen(["sh", "-c", f"sleep 1; omarchy-restart-shell{after}"],
                     start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def home() -> Path:
    return Path(os.environ["HOME"])


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", home() / ".config")) / "omarchy-sync"


def data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", home() / ".local/share")) / "omarchy-sync"


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", home() / ".local/state")) / "omarchy-sync"


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    return Path(base) if base and os.path.isdir(base) else Path("/tmp")


def say(msg: str = "") -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"omarchy-sync: {msg}", file=sys.stderr, flush=True)


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def run(args, check=False, capture=True, input=None, env=None) -> subprocess.CompletedProcess:
    """Run a command without a shell. A missing program counts as exit 127."""
    try:
        return subprocess.run(
            args, check=check, text=True, input=input, env=env,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
        )
    except FileNotFoundError:
        if check:
            raise
        return subprocess.CompletedProcess(args, 127, "", f"{args[0]}: not found")


def out(args) -> str:
    r = run(args)
    return r.stdout.strip() if r.returncode == 0 else ""


def platform() -> str:
    """x86, aarch64, aarch64-apple… — OMARCHY_SYNC_PLATFORM overrides it for tests."""
    forced = os.environ.get("OMARCHY_SYNC_PLATFORM")
    if forced:
        return forced
    return out(["omarchy-hw-platform"]) or ("x86" if os.uname().machine == "x86_64" else "aarch64")


def machine_info() -> dict:
    return {
        "host": socket.gethostname(),
        "user": os.environ.get("USER", ""),
        "home": str(home()),
        "arch": os.uname().machine,
        "platform": platform(),
        "virt": out(["systemd-detect-virt"]) or "none",
        "omarchy": out(["omarchy-version"]),
        "tryOmarchy": Path("/usr/bin/try-omarchy-import-previous").exists(),
    }


def is_vm(info: dict) -> bool:
    return info.get("virt", "none") not in ("", "none")


def timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def rel(path: Path) -> str:
    """A path for messages: ~/… inside the home folder."""
    try:
        return "~/" + str(Path(path).relative_to(home()))
    except ValueError:
        return str(path)


def same_as_skel(relpath: str) -> bool:
    """True when ~/relpath is still the file Omarchy installed (untouched by the user)."""
    skel = SKEL / relpath
    mine = home() / relpath
    try:
        return skel.is_file() and mine.is_file() and filecmp.cmp(skel, mine, shallow=False)
    except OSError:
        return False


def is_text(data: bytes) -> bool:
    if b"\0" in data:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def rewrite_home(data: bytes, src_home: str) -> bytes:
    """Point absolute paths into the old home folder at this one."""
    dst = str(home())
    if not src_home or src_home == dst or not is_text(data):
        return data
    pattern = re.compile(re.escape(src_home.encode()) + rb"(?=/|\b|$)")
    return pattern.sub(dst.encode(), data)


# Spellings of a CPU in tool paths, by this computer's CPU: each old spelling and what it
# may be here, tried in order (gh_2.103.0_linux_arm64 → …_linux_amd64, node …-linux-x64).
ARCH_SWAP = {
    "x86_64": {"aarch64": ["x86_64"], "arm64": ["amd64", "x64"]},
    "aarch64": {"x86_64": ["aarch64"], "amd64": ["arm64"], "x64": ["arm64"]},
}
# Tool folders whose paths are trusted before they exist here: the import installs them
# at the end (`mise install`), or the user does with the same config.
ARCH_MANAGED = (".local/share/mise/installs/", ".rustup/toolchains/")
# A "/" after "/" is the host part of a URL (https://host/x86_64/…), not a path.
_ABS_PATH = re.compile(rb"(?<![\w.~/-])/(?:[\w.@+~-]+/)*[\w.@+~-]+")


def rewrite_arch(data: bytes, src_arch: str) -> tuple:
    """Point absolute paths that name the other CPU at this one's, as rewrite_home does for
    the home folder. Returns (data, moved, missing): a path changes only when it doesn't
    exist here and its counterpart does (or lies under ARCH_MANAGED); one that still
    doesn't resolve is left as it is and listed in missing, for the report."""
    here = os.uname().machine
    swap = ARCH_SWAP.get(here, {})
    if not src_arch or src_arch == here or not swap or not is_text(data):
        return data, [], []
    token = re.compile(rb"(?<![A-Za-z0-9])(" + b"|".join(re.escape(k.encode()) for k in swap) + rb")(?![A-Za-z0-9])")
    managed = tuple(str(home() / m) for m in ARCH_MANAGED)
    moved, missing = [], []

    def fix(match):
        path = match.group(0)
        if not token.search(path) or os.path.lexists(path):
            return path
        old = path.decode(errors="replace")
        candidates = []
        for i in range(max(len(v) for v in swap.values())):
            pick = lambda t: swap[t.group(1).decode()][min(i, len(swap[t.group(1).decode()]) - 1)].encode()
            candidate = token.sub(pick, path)
            if candidate not in candidates:
                candidates.append(candidate)
        found = next((c for c in candidates if os.path.lexists(c)), None)
        if found is None and old.startswith(managed):
            found = candidates[0]
        if found is None:
            if old not in missing:
                missing.append(old)
            return path
        if (old, found.decode(errors="replace")) not in moved:
            moved.append((old, found.decode(errors="replace")))
        return found

    return _ABS_PATH.sub(fix, data), moved, missing


def move_arch(ctx, section: str, label: str, data: bytes) -> bytes:
    """rewrite_arch for an import, with what it did (or couldn't) in the report."""
    src = ctx.source.get("arch", "")
    data, moved, missing = rewrite_arch(data, src)
    for old, new in moved:
        ctx.report.note(section, f"{label}: {old} → {new} (this computer's CPU)")
    for old in missing:
        ctx.report.note(section, f"{label}: {old} looks {src}-only and doesn't exist here")
    return data


def walk_files(root: Path, skip_dirs=(".git",), skip=None):
    """Regular files and symlinks under root (or root itself), sorted, as Paths."""
    if root.is_symlink() or root.is_file():
        yield root
        return
    if not root.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        keep = []
        for d in sorted(dirnames):
            p = Path(dirpath) / d
            if d in skip_dirs:
                continue
            if p.is_symlink():
                yield p  # a link to a folder is kept as a link
            else:
                keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames):
            p = Path(dirpath) / f
            if skip and skip(p):
                continue
            if p.is_symlink() or p.is_file():
                yield p


class Report:
    """What happened, section by section. Printed at the end of every run."""

    def __init__(self):
        self.rows = []  # (section, status, text)

    def add(self, section: str, status: str, text: str) -> None:
        self.rows.append((section, status, text))
        emit("step", section=section, status=status, text=text)

    def counts(self) -> dict:
        counts = {}
        for _, s, _ in self.rows:
            counts[s] = counts.get(s, 0) + 1
        return counts

    def ok(self, section, text):
        self.add(section, "ok", text)

    def note(self, section, text):
        self.add(section, "note", text)

    def skip(self, section, text):
        self.add(section, "skipped", text)

    def fail(self, section, text):
        self.add(section, "failed", text)

    @property
    def failed(self) -> bool:
        return any(s == "failed" for _, s, _ in self.rows)

    def print(self, title: str) -> None:
        marks = {"ok": "✓", "note": "·", "skipped": "–", "failed": "✗"}
        say(f"\n== {title}")
        current = None
        for section, status, text in self.rows:
            if section != current:
                say(f"\n{section}")
                current = section
            say(f"  {marks.get(status, ' ')} {text}")
        counts = self.counts()
        say("\n" + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) if counts else "\nnothing to do")


# Folders that never travel: version control, caches, logs.
SKIP_DIRS = (".git", "node_modules", "__pycache__", "cache", "Cache", "CachedData", "logs", ".cache")


def safe_relpath(relpath) -> bool:
    """A path from an export file, which anyone could have made: relative, no "..", no NUL,
    so joined to the home folder it stays inside it."""
    if not isinstance(relpath, str) or not relpath or "\0" in relpath or relpath.startswith(("/", "~")):
        return False
    return all(part not in ("", ".", "..") for part in relpath.split("/"))


def is_inside(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def write_file(target: Path, data: bytes, mode: int = 0o644) -> None:
    """Replace target (a file or a link) with data, atomically."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        target.unlink()
    tmp = target.with_name(f".{target.name}.omarchy-sync.tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, mode & 0o777)
    os.replace(tmp, target)
