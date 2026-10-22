"""Command line: omarchy-sync [wizard|export|import|push|pull|undo|menu|sections]."""

import argparse
import getpass
import json
import sys
from pathlib import Path

from . import archive, common, engine, menu, packages, remote, sections, wizard

HELP = """\
Omarchy Sync carries your Omarchy setup to another Omarchy computer, aarch64 or x86_64.

  omarchy-sync                       the wizard (what the menu rows open)
  omarchy-sync export [-o PATH] [--sections a,b] [--secrets] [--push [REMOTE]]
  omarchy-sync import FILE|--pull [REMOTE] [--dry-run] [--sections a,b]
                      [--include-hardware] [--yes]
  omarchy-sync push FILE [REMOTE]     upload with rclone (REMOTE like gdrive:omarchy-sync)
  omarchy-sync pull [REMOTE] [-o DIR] download the newest export
  omarchy-sync undo [BACKUP]          put back the files the last import replaced
  omarchy-sync menu install|remove    Omarchy menu rows under Setup → Sync Settings
  omarchy-sync sections               the section names for --sections

Exports are named omarchy-settings-<hostname>-<YYYYmmdd-HHMM>.tar.gz unless -o names
the file. Any name works: renamed exports are still recognised by their contents.

For the GUI (and scripts): `info --json`, `inspect FILE --json`, `import FILE --plan-json`,
`--passphrase-stdin`, `export --packages a,b`, and `--events` before the command for
JSON-lines progress on stdout.

Sections: """ + ", ".join(sections.EXPORT_ORDER) + """ (secrets only with --secrets).
"""


def _sections(value: str, default: list) -> list:
    if not value:
        return default
    names = [n.strip() for n in value.split(",") if n.strip()]
    unknown = [n for n in names if n not in sections.BY_NAME]
    if unknown:
        raise SystemExit(f"omarchy-sync: unknown section(s): {', '.join(unknown)}")
    return names


def _passphrase(args, confirm: bool) -> str:
    if getattr(args, "passphrase_stdin", False):
        value = sys.stdin.readline().rstrip("\n")
        if not value:
            raise SystemExit("omarchy-sync: no passphrase on stdin")
        return value
    if args.passphrase_file:
        return Path(args.passphrase_file).read_text().rstrip("\n")
    if not sys.stdin.isatty():
        raise SystemExit("omarchy-sync: secrets need --passphrase-file when not run in a terminal")
    first = getpass.getpass("Passphrase for the secrets: ")
    if confirm and getpass.getpass("Again: ") != first:
        raise SystemExit("omarchy-sync: the passphrases differ")
    return first


def cmd_export(args) -> int:
    default = [s.name for s in sections.SECTIONS if s.default]
    chosen = _sections(args.sections, default)
    if args.secrets and "secrets" not in chosen:
        chosen.append("secrets")
    options = {}
    if args.packages is not None:
        options["packages"] = [p for p in args.packages.split(",") if p]
    if "secrets" in chosen:
        options["passphrase"] = _passphrase(args, confirm=True)
    if args.output:
        # An existing folder (or one written with a trailing /) gets the default name;
        # any other path is the file itself, and its folder is created when written.
        out = Path(args.output).expanduser()
        if args.output.endswith("/"):
            out.mkdir(parents=True, exist_ok=True)
    else:
        out = common.data_dir() / "exports" if args.push is not None else Path.cwd()
        out.mkdir(parents=True, exist_ok=True)
    engine.export(out, chosen, options, push=args.push)
    return 0


def cmd_import(args) -> int:
    if args.pull is not None:
        source = remote.pull(common.data_dir() / "downloads", args.pull)
    elif args.file:
        source = Path(args.file).expanduser()
    else:
        raise SystemExit("omarchy-sync: import needs FILE or --pull")
    with engine.Import(source, {"include_hardware": args.include_hardware}) as imp:
        common.say(imp.describe())
        available = imp.available()
        chosen = [n for n in _sections(args.sections, available) if n in available]
        if "secrets" in chosen:
            if (args.dry_run or args.plan_json) and not (args.passphrase_file or args.passphrase_stdin):
                chosen.remove("secrets")
                imp.ctx.report.note("secrets", "not decrypted in a dry run")
            else:
                imp.unlock(_passphrase(args, confirm=False))
        actions = imp.plan(chosen)
        engine.plan_event(actions)
        if args.plan_json:
            return 0
        engine.print_plan(actions)
        pending = [a for a in actions if a.pending]
        if not pending:
            common.say("\nNothing to change.")
            return 0
        if args.dry_run:
            common.say(f"\nDry run: {len(pending)} changes not applied.")
            return 0
        if not args.yes:
            if not sys.stdin.isatty():
                raise SystemExit("omarchy-sync: pass --yes to apply without a terminal")
            if input("\nApply these changes? [y/N] ").strip().lower()[:1] != "y":
                return 1
        imp.apply(actions)
        return 1 if imp.ctx.report.failed else 0


def cmd_info() -> int:
    """Everything the GUI needs to draw its forms, in one read-only call."""
    try:
        extras = packages.extras()
    except Exception:  # never let a pacman hiccup hide the window
        extras = {"repo": [], "aur": []}
    rclone = common.have("rclone")
    info = {
        "host": common.machine_info(),
        "sections": [{"name": s.name, "title": s.title, "default": s.default}
                     for name in sections.EXPORT_ORDER for s in sections.SECTIONS if s.name == name],
        "dataDir": str(common.data_dir()),
        "places": [{"label": f"USB / drive: {d}", "path": str(d)} for d in wizard.removable()]
                  + [{"label": f"Downloads ({common.rel(common.home() / 'Downloads')})",
                      "path": str(common.home() / "Downloads")}],
        "rclone": {"installed": rclone, "remotes": remote.remotes() if rclone else [],
                   "default": remote.default_remote()},
        "candidates": [str(p) for p in wizard.candidates()[:20]],
        "backups": engine.backup_list(),
        "lastRun": common.read_json(common.state_dir() / "last-run.json"),
        "packages": extras,
    }
    print(json.dumps(info, ensure_ascii=False))
    return 0


def cmd_inspect(args) -> int:
    """Always exits 0 with one JSON object: {"error": …} when the file can't be read."""
    try:
        with engine.Import(Path(args.file).expanduser(), {}) as imp:
            print(json.dumps(imp.summary(), ensure_ascii=False))
    except (archive.ArchiveError, OSError, RuntimeError) as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--events" in argv:
        argv.remove("--events")
        common.EVENTS = True
    if not argv:
        return wizard.main()
    if argv[0] in ("-h", "--help", "help"):
        print(HELP, end="")
        return 0
    if argv[0] in ("-V", "--version", "version"):
        print(engine.VERSION)
        return 0

    p = argparse.ArgumentParser(prog="omarchy-sync", add_help=True)
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("wizard")
    w.add_argument("step", nargs="?", default="", choices=["", "export", "import", "undo", "remote"])

    e = sub.add_parser("export")
    e.add_argument("-o", "--output", help="file or folder (default: current folder)")
    e.add_argument("--sections", default="")
    e.add_argument("--secrets", action="store_true")
    e.add_argument("--passphrase-file")
    e.add_argument("--passphrase-stdin", action="store_true")
    e.add_argument("--packages", default=None, help="only these extra packages (comma-separated)")
    e.add_argument("--push", nargs="?", const="", default=None, metavar="REMOTE")

    i = sub.add_parser("import")
    i.add_argument("file", nargs="?")
    i.add_argument("--pull", nargs="?", const="", default=None, metavar="REMOTE")
    i.add_argument("--dry-run", "-n", action="store_true")
    i.add_argument("--sections", default="")
    i.add_argument("--include-hardware", action="store_true")
    i.add_argument("--yes", "-y", action="store_true")
    i.add_argument("--passphrase-file")
    i.add_argument("--passphrase-stdin", action="store_true")
    i.add_argument("--plan-json", action="store_true", help="emit the plan as JSON and stop")

    ps = sub.add_parser("push")
    ps.add_argument("file")
    ps.add_argument("remote", nargs="?", default="")

    pl = sub.add_parser("pull")
    pl.add_argument("remote", nargs="?", default="")
    pl.add_argument("-o", "--output", default=".")

    u = sub.add_parser("undo")
    u.add_argument("backup", nargs="?", default="")
    u.add_argument("--check", action="store_true", help="exit 0 if there is an import to undo")

    m = sub.add_parser("menu")
    m.add_argument("action", choices=["install", "remove"])
    m.add_argument("--notify", action="store_true")

    sub.add_parser("sections")
    sub.add_parser("service-start")
    sub.add_parser("notify-last")
    inf = sub.add_parser("info")
    inf.add_argument("--json", action="store_true", required=True)
    ins = sub.add_parser("inspect")
    ins.add_argument("file")
    ins.add_argument("--json", action="store_true", required=True)

    args = p.parse_args(argv)
    try:
        if args.cmd == "wizard":
            return wizard.main(args.step)
        if args.cmd == "export":
            return cmd_export(args)
        if args.cmd == "import":
            return cmd_import(args)
        if args.cmd == "push":
            remote.push(Path(args.file).expanduser(), args.remote)
            return 0
        if args.cmd == "pull":
            print(remote.pull(Path(args.output).expanduser(), args.remote))
            return 0
        if args.cmd == "undo":
            if args.check:
                return 0 if engine.backups() else 1
            return 0 if engine.undo(args.backup) else 1
        if args.cmd == "menu":
            menu.install() if args.action == "install" else menu.remove()
            if args.notify:
                menu.notify("Added under Setup → Sync Settings in the Omarchy menu.")
            return 0
        if args.cmd == "sections":
            for s in sections.SECTIONS:
                print(f"{s.name:10} {s.title}")
            return 0
        if args.cmd == "service-start":
            menu.service_start()
            return 0
        if args.cmd == "notify-last":
            engine.notify_last()
            return 0
        if args.cmd == "info":
            return cmd_info()
        if args.cmd == "inspect":
            return cmd_inspect(args)
    except (RuntimeError, archive.ArchiveError, OSError) as e:
        common.warn(str(e))
        common.emit("error", text=str(e))
        return 1
    except KeyboardInterrupt:
        return 130
    return 2
