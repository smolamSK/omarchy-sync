"""The terminal wizard the menu rows open (gum prompts, plain prompts without gum)."""

import getpass
import os
import sys
from pathlib import Path

from . import archive, common, engine, packages, remote, sections


class Cancel(Exception):
    pass


def _gum(args, capture=True):
    if not common.have("gum"):
        return None
    r = common.run(["gum", *args]) if capture else common.run(["gum", *args], capture=False)
    if r.returncode == 130:
        raise Cancel()
    return r


def choose(header: str, options: list, multi=False, selected=None):
    """One option (multi=False) or a list of them."""
    if not options:
        return [] if multi else None
    args = ["choose", "--header", header, "--height", str(min(len(options) + 2, 22))]
    if multi:
        args.append("--no-limit")
        if selected:
            args.append("--selected=" + ",".join(selected))
    r = _gum(args + ["--", *options])
    if r is None:
        return _plain_choose(header, options, multi, selected)
    picked = [l for l in r.stdout.splitlines() if l]
    if multi:
        return picked
    if not picked:
        raise Cancel()
    return picked[0]


def _plain_choose(header, options, multi, selected):
    print(header)
    for i, o in enumerate(options, 1):
        mark = "*" if selected and o in selected else " "
        print(f" {mark}{i:2}. {o}")
    answer = input("Numbers separated by spaces (Enter keeps *): " if multi else "Number: ").split()
    if multi and not answer:
        return list(selected or [])
    try:
        picked = [options[int(a) - 1] for a in answer]
    except (ValueError, IndexError):
        raise Cancel()
    return picked if multi else (picked[0] if picked else None)


def confirm(question: str, default=True) -> bool:
    r = _gum(["confirm", f"--default={'true' if default else 'false'}", question])
    if r is None:
        answer = input(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
        return default if not answer else answer.startswith("y")
    return r.returncode == 0


def ask(prompt: str, password=False, placeholder="") -> str:
    args = ["input", "--prompt", prompt + " "]
    if password:
        args.append("--password")
    if placeholder:
        args += ["--placeholder", placeholder]
    r = _gum(args)
    if r is None:
        return getpass.getpass(prompt + " ") if password else input(prompt + " ")
    return r.stdout.strip()


def new_passphrase() -> str:
    while True:
        first = ask("Passphrase for the secrets:", password=True)
        if len(first) < 8:
            common.say("Use at least 8 characters; you will need it on the other computer.")
            continue
        if ask("Same passphrase again:", password=True) == first:
            return first
        common.say("They differ, try again.")


def removable() -> list:
    """Mounted USB keys and other removable drives."""
    roots = [Path("/run/media") / os.environ.get("USER", ""), Path("/media")]
    return [d for r in roots if r.is_dir() for d in sorted(r.iterdir()) if d.is_dir() and os.access(d, os.W_OK)]


# --------------------------------------------------------------------------- export

def export_flow() -> int:
    titles = {s.title: s.name for s in sections.SECTIONS}
    order = [s for name in sections.EXPORT_ORDER for s in sections.SECTIONS if s.name == name]
    picked = choose("What to export (space toggles, enter confirms)", [s.title for s in order], multi=True,
                    selected=[s.title for s in order if s.default])
    chosen = [titles[t] for t in picked]
    if not chosen:
        common.say("Nothing selected.")
        return 1
    options = {}
    if "packages" in chosen:
        found = packages.extras()
        names = found["repo"] + [f"{p} (AUR)" for p in found["aur"]]
        if names:
            kept = choose(f"{len(names)} packages you added to Omarchy — untick any you don't want carried",
                          names, multi=True, selected=names)
            options["packages"] = [k.removesuffix(" (AUR)") for k in kept]
    if "secrets" in chosen:
        common.say("Secrets are encrypted with a passphrase (gpg, AES-256). Without it the file can't open them.")
        options["passphrase"] = new_passphrase()

    places = {}
    for d in removable():
        places[f"USB / drive: {d}"] = ("dir", d)
    places[f"Downloads: {common.home() / 'Downloads'}"] = ("dir", common.home() / "Downloads")
    if common.have("rclone"):
        r = remote.default_remote()
        if r:
            places[f"rclone remote: {r}"] = ("remote", r)
        places["rclone remote: choose…"] = ("remote", "")
    else:
        places["rclone remote (installs rclone)…"] = ("remote", "")
    places["Another folder…"] = ("ask", None)
    kind, where = places[choose("Where to save it", list(places))]
    if kind == "ask":
        where = Path(os.path.expanduser(ask("Folder:", placeholder=str(common.home())))).resolve()
        where.mkdir(parents=True, exist_ok=True)
    if kind == "remote":
        where = where or pick_remote()
        target = engine.export(common.data_dir() / "exports", chosen, options)
        remote.push(target, where)
    else:
        target = engine.export(Path(where), chosen, options)
        if str(target).startswith(("/run/media", "/media")):
            common.run(["sync"])
            common.say("Written and flushed; you can unplug the drive.")
    return 0


def pick_remote() -> str:
    remote.ensure_rclone()
    names = remote.remotes()
    if not names:
        raise RuntimeError("rclone has no remotes yet; set one up with `rclone config` (e.g. Google Drive)")
    name = choose("rclone remote", names)
    folder = ask("Folder on it:", placeholder="omarchy-sync") or "omarchy-sync"
    path = f"{name}{folder.strip('/')}"
    if confirm(f"Use {path} from now on?"):
        remote.save_remote(path)
    return path


# --------------------------------------------------------------------------- import

def candidates() -> list:
    found = []
    for d in removable() + [common.home() / "Downloads", common.data_dir() / "exports", common.data_dir() / "downloads"]:
        for depth in ("*", "*/*"):
            found += [p for p in d.glob(depth) if remote.PATTERN.match(p.name) and p.is_file()]
    return sorted(set(found), key=lambda p: p.stat().st_mtime, reverse=True)


def import_flow() -> int:
    files = candidates()
    options = {str(p): ("file", p) for p in files[:12]}
    if common.have("rclone") or remote.default_remote():
        options["From an rclone remote…"] = ("remote", None)
    options["Another file…"] = ("ask", None)
    kind, value = options[choose("Which export to import", list(options))]
    if kind == "remote":
        r = remote.default_remote() or pick_remote()
        names = remote.listing(r)
        if not names:
            common.say(f"No exports in {r}.")
            return 1
        value = remote.pull(common.data_dir() / "downloads", r, choose(f"Exports in {r}", names))
    elif kind == "ask":
        value = Path(os.path.expanduser(ask("File:", placeholder="~/Downloads/omarchy-settings-….tar.gz")))
    try:
        imp = engine.Import(Path(value), {})
    except archive.ArchiveError as e:
        common.warn(str(e))
        return 1
    with imp:
        common.say("\n" + imp.describe() + "\n")
        available = imp.available()
        titles = {s.title: s.name for s in sections.SECTIONS if s.name in available}
        picked = choose("What to apply here (space toggles)", list(titles), multi=True, selected=list(titles))
        chosen = [titles[t] for t in picked]
        if "secrets" in chosen and not unlock(imp):
            chosen.remove("secrets")
        if "hypr" in chosen and not imp.ctx.same_machine and hardware_files(imp):
            imp.ctx.options["include_hardware"] = confirm(
                f"Also copy {', '.join(hardware_files(imp))} from {imp.ctx.source.get('host')}? "
                "Usually no: they describe that computer's screens and input devices.", default=False)
        actions = imp.plan(chosen)
        engine.print_plan(actions)
        if not any(a.pending for a in actions):
            common.say("\nNothing to change: this computer already matches the export.")
            return 0
        if not confirm("\nApply these changes? Everything replaced is backed up first."):
            return 1
        imp.apply(actions)
        return 1 if imp.ctx.report.failed else 0


def hardware_files(imp) -> list:
    return imp.hardware_files()


def unlock(imp) -> bool:
    for _ in range(3):
        passphrase = ask("Passphrase for the secrets (empty skips them):", password=True)
        if not passphrase:
            return False
        try:
            imp.unlock(passphrase)
            return True
        except archive.ArchiveError as e:
            common.say(str(e))
    return False


# --------------------------------------------------------------------------- entry

def main(what: str = "") -> int:
    if not sys.stdin.isatty():
        common.warn("the wizard needs a terminal; use `omarchy-sync export` / `import` from scripts")
        return 2
    try:
        if not what:
            pick = choose("Omarchy Sync", ["Export settings", "Import settings", "Undo the last import",
                                           "Set the rclone remote"])
            what = {"Export settings": "export", "Import settings": "import",
                    "Undo the last import": "undo", "Set the rclone remote": "remote"}[pick]
        if what == "export":
            return export_flow()
        if what == "import":
            return import_flow()
        if what == "undo":
            if not engine.backups():
                common.say("There is no import to undo.")
                return 1
            if confirm(f"Put back the files the import of {engine.backups()[0].name} replaced?"):
                return 0 if engine.undo() else 1
            return 1
        if what == "remote":
            common.say(f"Now: {remote.default_remote() or 'none'}")
            remote.save_remote(pick_remote())
            return 0
        common.warn(f"unknown wizard step {what!r}")
        return 2
    except Cancel:
        return 130
    except (RuntimeError, archive.ArchiveError, OSError) as e:
        common.warn(str(e))
        return 1
