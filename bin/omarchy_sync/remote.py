"""Optional rclone transport: push an export to a remote you configured, pull the newest back."""

import re
from pathlib import Path

from . import common

PATTERN = re.compile(r"^omarchy-settings-.*\.tar\.gz$")


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
    r = common.run(["rclone", "copy", "--", str(file), remote], capture=False)
    if r.returncode != 0:
        raise RuntimeError(f"rclone copy to {remote} failed")
    common.say(f"Uploaded {file.name} to {remote}")
    return remote


def listing(remote: str = "") -> list:
    """Export files on the remote, newest first (the names carry the date)."""
    ensure_rclone()
    remote = _remote(remote)
    r = common.run(["rclone", "lsf", "--files-only", "--", remote])
    if r.returncode != 0:
        raise RuntimeError(f"cannot list {remote}: {(r.stderr or '').strip()[:200]}")
    names = [n for n in r.stdout.splitlines() if PATTERN.match(n)]
    return sorted(names, key=lambda n: n[-20:], reverse=True)


def pull(dest: Path, remote: str = "", name: str = "") -> Path:
    remote = _remote(remote)
    if not name:
        found = listing(remote)
        if not found:
            raise RuntimeError(f"no omarchy-settings-*.tar.gz in {remote}")
        name = found[0]
    if "/" in name or not PATTERN.match(name):
        raise RuntimeError(f"unexpected file name {name!r}")
    dest.mkdir(parents=True, exist_ok=True)
    src = remote.rstrip("/") + ("/" if not remote.endswith(":") else "") + name
    r = common.run(["rclone", "copyto", "--", src, str(dest / name)], capture=False)
    if r.returncode != 0:
        raise RuntimeError(f"rclone could not download {src}")
    return dest / name
