"""Export, import (plan → backup → apply → report) and undo."""

import json
import os
import shutil
import time
from pathlib import Path

from . import archive, common, remote, sections
from .model import Backup, Ctx, undo as undo_backup

VERSION = "1.0.0"


def finish(action: str, **fields) -> None:
    """The `done` event, also kept in last-run.json so the window can show it after a
    shell restart (which an import ends with) has reloaded it."""
    record = {"action": action, "time": time.strftime("%Y-%m-%d %H:%M"), **fields}
    report = record.pop("report", None)
    if report is not None:
        record["ok"] = not report.failed
        record["counts"] = report.counts()
        # The card in the window lists what went wrong (names and reasons, no contents).
        record["failures"] = [{"section": s, "text": t} for s, st, t in report.rows if st == "failed"][:10]
    common.emit("done", **record)
    try:
        common.write_json(common.state_dir() / "last-run.json", record)
    except OSError:
        pass


def result_text(record: dict) -> tuple:
    """(title, detail) of a finished run, for notifications."""
    counts = record.get("counts", {}) or {}
    failed = counts.get("failed", 0)
    trouble = f" with {failed} problem{'s' if failed != 1 else ''}" if failed else ""
    action = record.get("action")
    if action == "export":
        path = Path(record.get("file", ""))
        detail = f"{path.name} in {common.rel(path.parent)}"
        if record.get("remote"):
            detail += f", uploaded to {record['remote']}"
        return ("Export saved" if not failed else "Export finished" + trouble), detail
    if action == "import":
        detail = f"{counts.get('ok', 0)} changes applied, {counts.get('skipped', 0)} skipped"
        if record.get("backup"):
            detail += ". Undo is in Setup → Sync Settings"
        return "Import finished" + trouble, detail
    if action == "undo":
        return "Undo finished" + trouble, f"{counts.get('ok', 0)} files put back"
    return "Omarchy Sync finished", ""


def notify_last() -> None:
    """Announce the last run (used after a shell restart, and when the window is closed)."""
    record = common.read_json(common.state_dir() / "last-run.json")
    if not isinstance(record, dict):
        return
    title, detail = result_text(record)
    failed = (record.get("counts", {}) or {}).get("failed", 0)
    tab = "undo" if record.get("action") == "import" and record.get("backup") else record.get("action", "export")
    common.run(["omarchy-notification-send", "-g", chr(0xF04E6), "--app-name", "Omarchy Sync",
                "-u", "normal" if failed else "low", title, detail,
                "--exec", "omarchy-shell", "shell", "summon", common.PLUGIN_ID, json.dumps({"tab": tab})])


def default_name(info: dict) -> str:
    host = "".join(c if c.isalnum() or c in "-_" else "-" for c in info.get("host", "omarchy"))
    return f"omarchy-settings-{host}-{time.strftime('%Y%m%d-%H%M')}.tar.gz"


def export(target: Path, chosen: list, options: dict, push=None) -> Path:
    """Write the export file. target may be a folder (the file is named for you).
    push: an rclone remote to upload it to afterwards ("" = the configured one)."""
    os.umask(0o077)
    here = common.machine_info()
    if target.is_dir():
        target = target / default_name(here)
    stage = archive.staging("omarchy-sync-export-")
    report = common.Report()
    ctx = Ctx(stage=stage, report=report, here=here, options=options)
    steps = [n for n in sections.EXPORT_ORDER if n in chosen]
    total = len(steps) + 1 + (push is not None)
    try:
        done = []
        for name in steps:
            common.emit("progress", done=len(done), total=total, label=sections.BY_NAME[name].title)
            (stage / name).mkdir(exist_ok=True)
            try:
                sections.BY_NAME[name].collect(ctx)
                done.append(name)
            except Exception as e:  # one broken section must not lose the others
                report.fail(name, f"{type(e).__name__}: {e}")
            if not any((stage / name).iterdir()):
                (stage / name).rmdir()
        manifest = {
            "kind": "omarchy-sync",
            "format": common.FORMAT,
            "version": VERSION,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "source": here,
            "sections": done,
        }
        manifest["checksums"] = archive.checksums(stage)
        common.write_json(stage / archive.MANIFEST, manifest)
        common.emit("progress", done=len(steps), total=total, label="packing " + target.name)
        archive.write(stage, target)
        os.chmod(target, 0o600)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    uploaded = ""
    if push is not None:
        common.emit("progress", done=len(steps) + 1, total=total, label="uploading with rclone")
        try:
            uploaded = remote.push(target, push)
        except RuntimeError as e:
            report.fail("upload", str(e))
    report.print("Exported")
    size = target.stat().st_size
    common.say(f"\n{target}  ({size / (1 << 20):.1f} MB)")
    finish("export", report=report, file=str(target), size=size, remote=uploaded)
    return target


class Import:
    """An unpacked export, ready to plan and apply. Use as a context manager."""

    def __init__(self, source: Path, options: dict):
        os.umask(0o077)
        self.source = source
        self.options = options
        self.stage = archive.staging("omarchy-sync-import-")
        self.secrets_dir = None
        try:
            self.manifest = archive.extract(source, self.stage)
        except Exception:
            self.close()
            raise
        self.ctx = Ctx(stage=self.stage, report=common.Report(), here=common.machine_info(),
                       source=self.manifest.get("source", {}), options=options)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        shutil.rmtree(self.stage, ignore_errors=True)
        if self.secrets_dir:
            shutil.rmtree(self.secrets_dir, ignore_errors=True)

    def available(self) -> list:
        return [s.name for s in sections.SECTIONS if s.present(self.stage)]

    def has_secrets(self) -> bool:
        return (self.stage / archive.SECRETS).exists()

    def unlock(self, passphrase: str) -> None:
        """Decrypt the secrets part; raises ArchiveError on a wrong passphrase."""
        folder = archive.staging("omarchy-sync-secrets-")
        try:
            archive.unseal(self.stage / archive.SECRETS, folder, passphrase)
        except Exception:
            shutil.rmtree(folder, ignore_errors=True)
            raise
        self.secrets_dir = folder
        self.ctx.secrets = folder
        self.ctx.secret_values = common.read_json(folder / "values.json", {}) or {}

    def hardware_files(self) -> list:
        index = common.read_json(self.stage / "hypr" / "files.json", {})
        return [Path(i["path"]).name for i in index.get("files", []) if i.get("hardware")]

    def summary(self) -> dict:
        """What the GUI shows before planning: where the export comes from and what it holds."""
        return {
            "file": str(self.source),
            "describe": self.describe(),
            "source": self.ctx.source,
            "created": self.manifest.get("created", ""),
            "sections": [{"name": s.name, "title": s.title} for s in sections.SECTIONS if s.present(self.stage)],
            "hasSecrets": self.has_secrets(),
            "hardwareFiles": self.hardware_files(),
            "sameMachine": self.ctx.same_machine,
            "otherArch": self.ctx.source.get("arch", "") != self.ctx.here.get("arch", ""),
        }

    def describe(self) -> str:
        s, h = self.ctx.source, self.ctx.here
        lines = [f"Export from {s.get('host', '?')} ({s.get('platform', '?')}, "
                 f"{'VM' if common.is_vm(s) else 'bare metal'}, Omarchy {s.get('omarchy', '?')}) "
                 f"made {self.manifest.get('created', '?')}",
                 f"Importing into {h.get('host')} ({h.get('platform')}, "
                 f"{'VM' if common.is_vm(h) else 'bare metal'}, Omarchy {h.get('omarchy', '?')})"]
        if s.get("arch") and s.get("arch") != h.get("arch"):
            lines.append(f"Different CPU architecture ({s['arch']} → {h['arch']}): "
                         "packages are checked one by one against this computer's repositories.")
        return "\n".join(lines)

    def plan(self, chosen: list) -> list:
        actions = []
        for section in sections.SECTIONS:
            if section.name in chosen and section.present(self.stage):
                try:
                    actions += section.plan(self.ctx)
                except Exception as e:
                    self.ctx.report.fail(section.name, f"could not plan: {type(e).__name__}: {e}")
        if any(a.pending for a in actions):
            actions += [
                sections.Action("reload", "new", "reload Hyprland",
                                lambda: common.run(["hyprctl", "reload"]), late=True),
                sections.Action("reload", "new", "restart the Omarchy shell" + (" (a few seconds after)" if common.gui() else ""),
                                common.restart_shell, late=True),
            ]
        return actions

    def apply(self, actions: list) -> Path:
        root = common.data_dir() / "backups" / common.timestamp()
        self.ctx.backup = Backup(root)
        self.ctx.backup.flush()
        common.write_json(root / "about.json", {"source": str(self.source), "from": self.ctx.source,
                                                "manifest": {k: v for k, v in self.manifest.items() if k != "checksums"}})
        report = self.ctx.report
        todo = sum(1 for a in actions if a.pending)
        step = 0
        for late in (False, True):
            for a in actions:
                if a.late != late:
                    continue
                if not a.pending:
                    if a.status == "skipped":
                        report.skip(a.section, a.text)
                    continue
                common.emit("running", section=a.section, text=a.text)
                common.emit("progress", done=step, total=todo, label=f"{a.section}: {a.text}")
                step += 1
                try:
                    a.fn()
                    report.ok(a.section, a.text)
                except Exception as e:
                    report.fail(a.section, f"{a.text}: {e}")
        report.print("Imported")
        if self.ctx.backup.entries:
            common.say(f"\nReplaced files are saved in {common.rel(root)}; `omarchy-sync undo` puts them back.")
        else:
            shutil.rmtree(root, ignore_errors=True)
        finish("import", report=report, backup=str(root) if self.ctx.backup.entries else "",
               restarting=common.gui() and todo > 0)
        return root


def plan_event(actions: list) -> None:
    common.emit("plan", actions=[{"section": a.section, "status": a.status, "text": a.text, "late": a.late}
                                 for a in actions])


def print_plan(actions: list) -> None:
    by_section = {}
    for a in actions:
        by_section.setdefault(a.section, []).append(a)
    marks = {"new": "+", "changed": "~", "skipped": "–", "manual": "!"}
    for section, items in by_section.items():
        same = sum(1 for a in items if a.status == "same")
        common.say(f"\n{section}" + (f"  ({same} already the same)" if same else ""))
        for a in items:
            if a.status != "same":
                common.say(f"  {marks.get(a.status, ' ')} {a.text}" + ("  [at the end]" if a.late and a.section != "reload" else ""))


def backups() -> list:
    root = common.data_dir() / "backups"
    return sorted((p for p in root.iterdir() if (p / "index.json").exists() and not p.name.endswith(".undone")),
                  reverse=True) if root.is_dir() else []


def backup_list() -> list:
    out = []
    for root in backups():
        about = common.read_json(root / "about.json", {})
        index = common.read_json(root / "index.json", {})
        out.append({"name": root.name, "host": about.get("from", {}).get("host", ""),
                    "source": about.get("source", ""), "files": len(index.get("entries", []))})
    return out


def undo(which: str = "") -> bool:
    found = backups()
    if which:
        found = [p for p in found if p.name == which or str(p) == which]
    if not found:
        common.warn("no import to undo")
        return False
    root = found[0]
    report = common.Report()
    undo_backup(root, report)
    report.print(f"Undid the import of {root.name}")
    common.run(["hyprctl", "reload"])
    done = root.with_name(root.name + ".undone")
    os.replace(root, done)
    finish("undo", report=report, undone=root.name, restarting=common.gui())
    common.restart_shell()
    common.say("\nPackages, plugins and themes the import installed stay; remove them with "
               "`omarchy pkg drop`, `omarchy plugin remove` or the theme menu if you don't want them.")
    return not report.failed
