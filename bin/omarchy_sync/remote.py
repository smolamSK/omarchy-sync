"""Optional rclone transport: push an export to a remote you configured, pull the newest back."""

import json
import re
from pathlib import Path

from . import common

# The default export name. Renamed exports are found by archive.peek() locally; in the
# remote folder (ours alone) every .tar.gz counts and `inspect` checks it after download.
PATTERN = re.compile(r"^omarchy-settings-.*\.tar\.gz$")
REMOTE_NAME = re.compile(r"^[^/]+\.tar\.gz$")


class RemoteError(RuntimeError):
    """An rclone failure in words, with the command that fixes it when there is one."""

    def __init__(self, message: str, fix: str = ""):
        super().__init__(message + (f"; fix it with `{fix}`" if fix else ""))
        self.fix = fix


def _name(remote: str) -> str:
    """The remote's name ("gdrive:"), only when it is a plain rclone name."""
    name = remote.split(":", 1)[0] + ":"
    return name if re.fullmatch(r"[\w.@ -]+:", name) else ""


def problem(stderr: str, remote: str, doing: str) -> RemoteError:
    """Turn rclone's stderr into something a person can act on."""
    text = stderr or ""
    name = _name(remote)
    low = text.lower()
    if name and ("empty token" in low or "config reconnect" in low or "invalid_grant" in low
                 or "token expired" in low or "unauthorized" in low):
        return RemoteError(f"{name} is not signed in (its login token is missing or expired)",
                           f"rclone config reconnect {name}")
    if "didn't find section in config file" in low or "not found in config file" in low:
        return RemoteError(f"there is no rclone remote called {name or remote}", "rclone config")
    lines = [re.sub(r"^\d{4}/\d\d/\d\d \d\d:\d\d:\d\d (CRITICAL|ERROR|NOTICE) ?: ", "", l)
             for l in text.splitlines() if " ERROR " in l or " CRITICAL " in l]
    detail = lines[-1].strip() if lines else (text.strip().splitlines() or ["no details"])[-1]
    return RemoteError(f"rclone could not {doing} {remote}: {detail[:240]}")


def config() -> dict:
    path = common.config_dir() / "config"
    values = {}
    try:
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    except OSError:
        pass
    return values


def save_remote(remote: str) -> None:
    path = common.config_dir() / "config"
    path.parent.mkdir(parents=True, exist_ok=True)
    values = config()
    values["remote"] = remote
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))


def default_remote() -> str:
    return config().get("remote", "")


def remotes() -> list:
    return [r for r in common.out(["rclone", "listremotes"]).splitlines() if r]


def ensure_rclone() -> None:
    if common.have("rclone"):
        return
    common.say("rclone is not installed; installing it (needs your password).")
    if common.run(["omarchy-pkg-add", "rclone"], capture=False).returncode != 0:
        raise RuntimeError("could not install rclone")


def _remote(remote: str) -> str:
    remote = remote or default_remote()
    if not remote:
        raise RuntimeError("no rclone remote set; pass one (e.g. gdrive:omarchy-sync) or run the wizard once")
    if ":" not in remote:
        raise RuntimeError(f"{remote!r} is not an rclone path (expected name:folder)")
    return remote


def push(file: Path, remote: str = "") -> str:
    ensure_rclone()
    remote = _remote(remote)
    r = common.run(["rclone", "copy", "--", str(file), remote])
    if r.returncode != 0:
        raise problem(r.stderr, remote, "upload to")
    common.say(f"Uploaded {file.name} to {remote}")
    return remote


def listing(remote: str = "") -> list:
    """Export files (*.tar.gz) in the remote folder, newest first."""
    ensure_rclone()
    remote = _remote(remote)
    r = common.run(["rclone", "lsjson", "--files-only", "--", remote])
    if r.returncode != 0:
        raise problem(r.stderr, remote, "list")
    try:
        entries = json.loads(r.stdout or "[]")
    except ValueError:
        entries = []
    entries = [e for e in entries if isinstance(e, dict) and REMOTE_NAME.match(str(e.get("Name", "")))]
    entries.sort(key=lambda e: str(e.get("ModTime", "")), reverse=True)
    return [e["Name"] for e in entries]


def pull(dest: Path, remote: str = "", name: str = "") -> Path:
    remote = _remote(remote)
    if not name:
        found = listing(remote)
        if not found:
            raise RuntimeError(f"no exports (*.tar.gz) in {remote}")
        name = found[0]
    if not REMOTE_NAME.match(name):
        raise RuntimeError(f"unexpected file name {name!r}")
    dest.mkdir(parents=True, exist_ok=True)
    src = remote.rstrip("/") + ("/" if not remote.endswith(":") else "") + name
    r = common.run(["rclone", "copyto", "--", src, str(dest / name)])
    if r.returncode != 0:
        raise problem(r.stderr, src, "download")
    return dest / name
