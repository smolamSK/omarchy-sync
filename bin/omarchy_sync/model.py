"""The pieces every section shares: the run context, planned actions, backups and file items."""

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import common, scan


@dataclass
class Action:
    section: str
    status: str  # new | changed | same | skipped | manual
    text: str
    fn: Optional[Callable[[], None]] = None
    late: bool = False  # run after every section (theme switch, reloads)

    @property
    def pending(self) -> bool:
        return self.status in ("new", "changed") and self.fn is not None


@dataclass
class Ctx:
    stage: Path  # export: staging folder; import: the unpacked export
    report: common.Report
    here: dict  # machine_info() of this computer
    source: dict = field(default_factory=dict)  # import: machine_info() of the exporting computer
    options: dict = field(default_factory=dict)
    backup: Optional["Backup"] = None
    secrets: Optional[Path] = None  # export: secrets staging; import: decrypted secrets
    secret_values: dict = field(default_factory=dict)  # shell.json values kept out of the plain part
    expected_plugins: set = field(default_factory=set)  # ids present once plugins are applied
    secret_files: list = field(default_factory=list)  # export: files moved into the encrypted part

    @property
    def same_machine(self) -> bool:
        return (self.source.get("host") == self.here.get("host")
                and self.source.get("platform") == self.here.get("platform"))


class Backup:
    """Copies of every file an import replaces or creates, so `undo` can put them back."""

    def __init__(self, root: Path):
        self.root = root
        self.entries = []  # {"path": HOME-relative, "existed": bool}
        self.seen = set()

    def save(self, target: Path) -> None:
        relpath = str(target.relative_to(common.home()))
        if relpath in self.seen:
            return
        self.seen.add(relpath)
        existed = target.exists() or target.is_symlink()
        if existed:
            copy = self.root / "files" / relpath
            copy.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                os.symlink(os.readlink(target), copy)
            elif target.is_dir():
                shutil.copytree(target, copy, symlinks=True)
            else:
                shutil.copy2(target, copy)
        self.entries.append({"path": relpath, "existed": existed})
        self.flush()

    def flush(self) -> None:
        common.write_json(self.root / "index.json", {"entries": self.entries})


def undo(root: Path, report: common.Report) -> None:
    index = common.read_json(root / "index.json", {})
    for entry in reversed(index.get("entries", [])):
        relpath = entry["path"]
        target = common.home() / relpath
        copy = root / "files" / relpath
        try:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
            if entry["existed"]:
                target.parent.mkdir(parents=True, exist_ok=True)
                if copy.is_symlink():
                    os.symlink(os.readlink(copy), target)
                elif copy.is_dir():
                    shutil.copytree(copy, target, symlinks=True)
                else:
                    shutil.copy2(copy, target)
                report.ok("undo", f"restored ~/{relpath}")
            else:
                report.ok("undo", f"removed ~/{relpath} (it did not exist before)")
        except OSError as e:
            report.fail("undo", f"~/{relpath}: {e.strerror or e}")


def skip_name(name: str) -> bool:
    """Backups, editor leftovers and samples never travel."""
    return (".pre-migration" in name or ".bak" in name or name.endswith("~")
            or name.endswith(".sample") or name.endswith(".swp") or name == ".DS_Store")


class Files:
    """A set of files under HOME, staged under <stage>/<section>/files with a files.json index.

    Links are followed: what travels is the file's content, so a link into a folder
    that does not exist on the other computer cannot break. Links that lead outside
    the home folder point at things the system installed (Try Omarchy's wallpaper,
    for one) and are left out.
    """

    def __init__(self, ctx: Ctx, section: str, stage: Optional[Path] = None):
        self.ctx = ctx
        self.section = section
        stage = stage or ctx.stage
        self.dir = stage / section / "files"
        self.index_path = stage / section / "files.json"
        self.items = common.read_json(self.index_path, {}).get("files", [])

    # -- export ------------------------------------------------------------

    def add(self, relpath: str, hardware: bool = False, keep_skel: bool = False) -> bool:
        src = common.home() / relpath
        name = src.name
        if skip_name(name) or any(i["path"] == relpath for i in self.items):
            return False
        real = Path(os.path.realpath(src))
        if src.is_symlink() and not common.is_inside(real, common.home()):
            self.ctx.report.skip(self.section, f"{common.rel(src)} is a link to {real}, outside your home folder")
            return False
        if not real.is_file():
            return False
        if not keep_skel and common.same_as_skel(relpath):
            return False
        size = real.stat().st_size
        if size > common.MAX_FILE:
            self.ctx.report.skip(self.section, f"{common.rel(src)} is {size // (1 << 20)} MB, too big to carry")
            return False
        if self.section != "secrets" and self._holds_secrets(relpath, real):
            return False
        dest = self.dir / relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(real, dest)
        mode = real.stat().st_mode & 0o777
        self.items.append({"path": relpath, "mode": mode, "hardware": hardware})
        return True

    def _holds_secrets(self, relpath: str, real: Path) -> bool:
        """A file that looks like it holds a token never goes into the plain part: it moves
        into the encrypted secrets when those are exported, and is left out otherwise."""
        hits = scan.findings(real.read_bytes())
        if not hits:
            return False
        where = f"{common.rel(common.home() / relpath)}: {scan.describe(hits)} looks like a secret"
        if self.ctx.options.get("passphrase"):
            if relpath not in self.ctx.secret_files:
                self.ctx.secret_files.append(relpath)
            self.ctx.report.note(self.section, f"{where}; moved to the encrypted secrets")
        else:
            self.ctx.report.skip(self.section, f"{where}; left out (export with secrets to carry it encrypted)")
        return True

    def add_tree(self, relpath: str, skip=None, hardware=None) -> int:
        count = 0
        root = common.home() / relpath
        for p in common.walk_files(root, skip_dirs=common.SKIP_DIRS):
            r = str(p.relative_to(common.home()))
            if skip and skip(r):
                continue
            if self.add(r, hardware=bool(hardware and hardware(r))):
                count += 1
        return count

    def save(self) -> None:
        if self.items:
            common.write_json(self.index_path, {"files": self.items})

    # -- import ------------------------------------------------------------

    def desired(self, item) -> bytes:
        data = (self.dir / item["path"]).read_bytes()
        data = common.rewrite_home(data, self.ctx.source.get("home", ""))
        return common.move_arch(self.ctx, self.section, common.rel(common.home() / item["path"]), data)

    def plan(self, select=None, describe=None) -> list:
        """One action per file: new, changed or same. select(item) may return a skip reason."""
        actions = []
        for item in self.items:
            target = common.home() / item["path"]
            label = describe(item) if describe else common.rel(target)
            reason = select(item) if select else None
            if reason:
                actions.append(Action(self.section, "skipped", f"{label}: {reason}"))
                continue
            data = self.desired(item)
            if target.is_file() and not target.is_symlink():
                status = "same" if target.read_bytes() == data else "changed"
            else:
                status = "changed" if target.exists() or target.is_symlink() else "new"
            actions.append(Action(self.section, status, label,
                                  lambda item=item, data=data: self.write(item, data)))
        return actions

    def write(self, item, data: bytes) -> None:
        target = common.home() / item["path"]
        self.ctx.backup.save(target)
        common.write_file(target, data, item.get("mode", 0o644))

