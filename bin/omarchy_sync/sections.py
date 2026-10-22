"""Every kind of setting Omarchy Sync carries. Each section collects (export) and plans (import)."""

import copy
import json
import os
import re
import shutil
from pathlib import Path

from . import archive, common, jsonc, packages
from .model import Action, Files

H = common.home


def _git(path: Path, *args) -> str:
    return common.out(["git", "-C", str(path), *args])


def _origin(path: Path) -> str:
    return _git(path, "remote", "get-url", "origin") if (path / ".git").exists() else ""


def _clone_pinned(origin: str, dest: Path, commit: str) -> None:
    """Clone origin at exactly the exported commit, so both computers run the same code.
    The branch is moved there (not a detached HEAD), so later updates still fast-forward."""
    if not re.fullmatch(r"[0-9a-f]{40}", commit or ""):
        raise RuntimeError(f"the export has no commit for {origin}")
    if common.run(["omarchy-git-url-check", origin]).returncode not in (0, 127):
        raise RuntimeError(f"refusing URL {origin}")
    if common.run(["git", "clone", "--quiet", "--", origin, str(dest)]).returncode != 0:
        raise RuntimeError(f"git clone {origin} failed")
    branch = _git(dest, "symbolic-ref", "--short", "HEAD") or "main"
    pinned = common.run(["git", "-C", str(dest), "checkout", "--quiet", "-B", branch, commit]).returncode == 0
    if not pinned or _git(dest, "rev-parse", "HEAD") != commit:
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(f"commit {commit[:7]} is no longer in {origin}; install the current one by hand if you trust it")


# Rows Try Omarchy (the Mac VM app) adds to the menu: they mean nothing on another computer.
TRY_OMARCHY = re.compile(r"try-omarchy|tryomarchy|omarchy-native-settings")

OMARCHY_SKIP = re.compile(
    r"^\.config/omarchy/(shell\.json.*|plugins/.*|themes/.*|extensions/omarchy-menu\.jsonc|"
    r"defaults/dictation|hooks/post-update\.d/setup-[^/]*\.hook|current/.*)$"
)

HYPR_HARDWARE = {".config/hypr/monitors.lua", ".config/hypr/input.lua"}

DEFAULT_APPS = """\
# Paths under your home folder that Omarchy Sync carries in "App configs".
# One per line; folders are taken whole (minus caches and .git). Files you
# never changed from Omarchy's defaults are left out automatically.
.bashrc
.XCompose
.config/alacritty
.config/btop
.config/fastfetch
.config/foot
.config/ghostty
.config/git
.config/kitty
.config/lazygit
.config/mise
.config/nvim/lua
.config/satty
.config/starship.toml
.config/tmux
.config/walker
.local/share/fonts
"""

DEFAULT_SECRETS = [
    ".ssh", ".gnupg", ".config/gh", ".local/share/keyrings", ".config/rclone", ".password-store",
]
SECRET_DIRS = {".ssh", ".gnupg", ".config/gh", ".local/share/keyrings", ".password-store"}
SECRET_KEY = re.compile(r"(api[-_]?key|token|secret|passw(or)?d|credential)", re.I)

BIN_REF = re.compile(r"\.local/bin/([A-Za-z0-9._+-]+)")


class Section:
    name = ""
    title = ""
    default = True  # ticked unless the user says otherwise

    def collect(self, ctx) -> None:
        raise NotImplementedError

    def plan(self, ctx) -> list:
        raise NotImplementedError

    def present(self, stage: Path) -> bool:
        return (stage / self.name).exists()


# --------------------------------------------------------------------------- packages

class PackagesSection(Section):
    name = packages.SECTION
    title = "Extra packages"

    def collect(self, ctx):
        packages.collect(ctx)

    def plan(self, ctx):
        return packages.plan(ctx)


# --------------------------------------------------------------------------- secrets

class SecretsSection(Section):
    name = "secrets"
    title = "Secrets — encrypted (SSH · GnuPG · GitHub CLI · keyrings · rclone)"
    default = False

    def present(self, stage):
        return (stage / archive.SECRETS).exists()

    def collect(self, ctx):
        passphrase = ctx.options.get("passphrase")
        if not passphrase:
            ctx.report.skip(self.name, "no passphrase given, secrets left out")
            return
        folder = archive.staging("omarchy-sync-secrets-")
        try:
            files = Files(ctx, "secrets", stage=folder)
            count = 0
            for relpath in DEFAULT_SECRETS:
                count += files.add_tree(relpath, skip=lambda r: re.search(r"/(S\.[^/]*|.*\.lock|random_seed)$", r))
            # Config files the other sections found holding a token (see Files._holds_secrets).
            count += sum(files.add(relpath, keep_skel=True) for relpath in ctx.secret_files)
            files.save()
            common.write_json(folder / "values.json", ctx.secret_values)
            archive.seal(folder, ctx.stage / archive.SECRETS, passphrase)
            ctx.report.ok(self.name, f"{count} files and {len(ctx.secret_values)} settings values, encrypted")
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    def plan(self, ctx):
        if not ctx.secrets:
            return [Action(self.name, "skipped", "not decrypted")]
        actions = Files(ctx, "secrets", stage=ctx.secrets).plan()
        for a in actions:
            if a.pending:
                a.fn = self._private(a.fn)
        return actions

    @staticmethod
    def _private(fn):
        def run():
            fn()
            for d in SECRET_DIRS:
                p = H() / d
                if p.is_dir():
                    for dirpath, _, _ in os.walk(p):
                        os.chmod(dirpath, 0o700)
        return run


# --------------------------------------------------------------------------- plugins

class PluginsSection(Section):
    name = "plugins"
    title = "Shell plugins"

    def collect(self, ctx):
        out = []
        root = H() / ".config/omarchy/plugins"
        for d in sorted(root.iterdir()) if root.is_dir() else []:
            if d.name.startswith(".") or not d.is_dir():
                continue
            manifest = common.read_json(d / "manifest.json", {})
            pid = manifest.get("id") or d.name
            if pid == common.PLUGIN_ID:
                continue
            entry = {"id": pid, "name": manifest.get("name", pid), "folder": d.name}
            origin = _origin(d)
            if origin:
                entry.update(origin=origin, commit=_git(d, "rev-parse", "HEAD"))
                # Not _git(): its strip() would eat the status column of the first line.
                changed = common.run(["git", "-C", str(d), "status", "--porcelain"]).stdout or ""
                if changed:
                    # Your own fixes on top of upstream (e.g. a manifest patch): carry the
                    # changed files and lay them over a fresh clone on the other side.
                    entry["overlay"] = self._bundle(ctx, d, changed)
                    ctx.report.note(self.name, f"{pid}: {len(entry['overlay'])} locally changed files carried along")
            else:
                entry["bundled"] = True
                self._copy_tree(d, ctx.stage / self.name / "bundled" / d.name)
                ctx.report.note(self.name, f"{pid}: not a git clone, copied whole")
            out.append(entry)
        common.write_json(ctx.stage / self.name / "plugins.json", {"plugins": out})
        ctx.report.ok(self.name, f"{len(out)} plugins")

    @staticmethod
    def _copy_tree(src: Path, dest: Path):
        shutil.copytree(src, dest, symlinks=False, ignore_dangling_symlinks=True,
                        ignore=shutil.ignore_patterns(".git", "__pycache__"))

    def _bundle(self, ctx, d: Path, porcelain: str) -> list:
        files = []
        for line in porcelain.splitlines():
            if len(line) < 4:
                continue
            relpath = line[3:].split(" -> ")[-1].strip('"')
            src = d / relpath
            if src.is_file():
                dest = ctx.stage / self.name / "overlay" / d.name / relpath
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest)
                files.append(relpath)
        return files

    def plan(self, ctx):
        data = common.read_json(ctx.stage / self.name / "plugins.json", {})
        root = H() / ".config/omarchy/plugins"
        have = installed_plugins()
        actions = []
        for p in data.get("plugins", []):
            ctx.expected_plugins.add(p["id"])
            if p["id"] in have:
                note = ""
                there = have[p["id"]]
                if p.get("origin") and _origin(there) and _origin(there) != p["origin"]:
                    note = f" (installed from {_origin(there)}, the export used {p['origin']})"
                actions.append(Action(self.name, "same", f"{p['id']}{note}"))
            elif p.get("bundled"):
                actions.append(Action(self.name, "new", f"{p['id']} (copied from the export)",
                                      lambda p=p: self._place_bundle(ctx, p, root)))
            elif p.get("overlay"):
                actions.append(Action(self.name, "new", f"{p['id']} from {p['origin']} + your {len(p['overlay'])} changed files",
                                      lambda p=p: self._clone_overlay(ctx, p, root)))
            else:
                actions.append(Action(self.name, "new", f"{p['id']} from {p['origin']}",
                                      lambda p=p: self._add(p)))
        return actions

    @staticmethod
    def _add(p):
        r = common.run(["omarchy", "plugin", "add", p["origin"], "--yes"], capture=False)
        if r.returncode != 0 or p["id"] not in installed_plugins():
            raise RuntimeError(f"omarchy plugin add {p['origin']} failed")

    def _place_bundle(self, ctx, p, root):
        dest = root / p["id"]
        self._copy_tree(ctx.stage / self.name / "bundled" / p["folder"], dest)
        self._validate_or_remove(dest)

    def _clone_overlay(self, ctx, p, root):
        tmp = root / f".{p['id']}.omarchy-sync.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        _clone_pinned(p["origin"], tmp, p.get("commit", ""))
        for relpath in p["overlay"]:
            src = ctx.stage / self.name / "overlay" / p["folder"] / relpath
            dest = tmp / relpath
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        dest = root / p["id"]
        os.replace(tmp, dest)
        self._validate_or_remove(dest)

    @staticmethod
    def _validate_or_remove(dest: Path):
        r = common.run(["omarchy", "plugin", "validate", str(dest)])
        if r.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            raise RuntimeError(f"{dest.name} does not validate on this Omarchy: {(r.stdout or r.stderr).strip()[:200]}")
        common.run(["omarchy-shell", "shell", "rescanPlugins"])


def installed_plugins() -> dict:
    """id -> folder of every third-party plugin installed here."""
    found = {}
    root = H() / ".config/omarchy/plugins"
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        if d.is_dir() and not d.name.startswith("."):
            pid = common.read_json(d / "manifest.json", {}).get("id") or d.name
            found[pid] = d
    return found


# --------------------------------------------------------------------------- theme

class ThemeSection(Section):
    name = "theme"
    title = "Themes and background"

    def collect(self, ctx):
        state = H() / ".local/state/omarchy/current"
        current = (state / "theme.name").read_text().strip() if (state / "theme.name").exists() else ""
        background = Path(os.path.realpath(state / "background")).name if (state / "background").is_symlink() else ""
        git_themes, local = [], []
        files = Files(ctx, self.name)
        root = H() / ".config/omarchy/themes"
        for d in sorted(root.iterdir()) if root.is_dir() else []:
            if d.is_symlink():
                ctx.report.skip(self.name, f"{d.name} is a link to {os.path.realpath(d)}, not carried")
                continue
            if not d.is_dir():
                continue
            origin = _origin(d)
            if origin:
                git_themes.append({"name": d.name, "origin": origin, "commit": _git(d, "rev-parse", "HEAD")})
            else:
                files.add_tree(str(d.relative_to(H())))
                local.append(d.name)
        files.save()
        common.write_json(ctx.stage / self.name / "theme.json",
                          {"current": current, "background": background, "git": git_themes, "local": local})
        ctx.report.ok(self.name, f"current {current or '?'}; {len(git_themes)} git themes, {len(local)} copied")

    def plan(self, ctx):
        data = common.read_json(ctx.stage / self.name / "theme.json", {})
        root = H() / ".config/omarchy/themes"
        actions = []
        for t in data.get("git", []):
            if (root / t["name"]).exists():
                actions.append(Action(self.name, "same", f"theme {t['name']}"))
            else:
                actions.append(Action(self.name, "new", f"theme {t['name']} from {t['origin']}",
                                      lambda t=t: self._clone(ctx, t, root)))
        files = Files(ctx, self.name)
        actions += files.plan()
        current_here = H() / ".local/state/omarchy/current/theme.name"
        here = current_here.read_text().strip() if current_here.exists() else ""
        want = data.get("current", "")
        if want and want != here:
            actions.append(Action(self.name, "changed", f"switch theme {here or '?'} → {want}",
                                  lambda: self._switch(want), late=True))
        bg = data.get("background", "")
        if bg:
            actions.append(Action(self.name, "changed" if want != here or self._bg_now() != bg else "same",
                                  f"background {bg}", lambda: self._background(ctx, want, bg), late=True))
        return actions

    def _clone(self, ctx, t, root):
        # As omarchy-theme-install does, minus switching to each theme as it lands.
        if not re.fullmatch(r"[a-z0-9_][a-z0-9._+-]*", t["name"]):
            raise RuntimeError(f"unusable theme name {t['name']!r}")
        _clone_pinned(t["origin"], root / t["name"], t.get("commit", ""))

    @staticmethod
    def _switch(name):
        if common.run(["omarchy-theme-set", name], capture=False).returncode != 0:
            raise RuntimeError(f"omarchy-theme-set {name} failed")

    @staticmethod
    def _bg_now() -> str:
        link = H() / ".local/state/omarchy/current/background"
        return Path(os.path.realpath(link)).name if link.is_symlink() else ""

    def _background(self, ctx, theme, name):
        if self._bg_now() == name:
            return
        places = [H() / ".local/state/omarchy/current/theme/backgrounds",
                  H() / ".config/omarchy/backgrounds" / theme]
        for place in places:
            if (place / name).is_file():
                if common.run(["omarchy-theme-bg-set", str(place / name)]).returncode != 0:
                    raise RuntimeError("omarchy-theme-bg-set failed")
                return
        ctx.report.note(self.name, f"background {name} is not in this theme here; kept the theme's own")


# --------------------------------------------------------------------------- shell.json

class ShellSection(Section):
    name = "shell"
    title = "Bar · dock · idle · widget settings (shell.json)"

    PATH = ".config/omarchy/shell.json"

    def collect(self, ctx):
        data = common.read_json(H() / self.PATH)
        if not isinstance(data, dict):
            ctx.report.skip(self.name, "no shell.json")
            return
        redacted = []

        def walk(node, path):
            if isinstance(node, dict):
                for k, v in node.items():
                    if isinstance(v, str) and v and SECRET_KEY.search(k):
                        ctx.secret_values[path + "/" + k] = v
                        redacted.append(path + "/" + k)
                        node[k] = ""
                    else:
                        walk(v, path + "/" + k)
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    key = v.get("id") if isinstance(v, dict) and isinstance(v.get("id"), str) else str(i)
                    walk(v, path + "/" + key)

        walk(data, "")
        common.write_json(ctx.stage / self.name / "shell.json", data)
        common.write_json(ctx.stage / self.name / "redacted.json", redacted)
        ctx.report.ok(self.name, "bar layout and widget settings")
        if redacted:
            where = "the encrypted secrets" if ctx.options.get("passphrase") else "nowhere (secrets not exported)"
            ctx.report.note(self.name, f"{len(redacted)} key/token values moved to {where}")

    def merged(self, ctx, known: set) -> dict:
        src = common.read_json(ctx.stage / self.name / "shell.json", {})
        target = common.read_json(H() / self.PATH, {}) or {}
        result = copy.deepcopy(target)
        for k, v in src.items():
            if k != "version":  # the shell's own schema version stays this computer's
                result[k] = copy.deepcopy(v)

        def keep(entry):
            pid = entry if isinstance(entry, str) else entry.get("id") if isinstance(entry, dict) else None
            return not isinstance(pid, str) or pid.startswith("omarchy.") or pid in known

        layout = result.get("bar", {}).get("layout", {})
        if isinstance(layout, dict):
            for section, entries in layout.items():
                if isinstance(entries, list):
                    layout[section] = [e for e in entries if keep(e)]
        if isinstance(result.get("plugins"), list):
            result["plugins"] = [e for e in result["plugins"] if keep(e)]
        bar_id = result.get("bar", {}).get("id")
        if isinstance(bar_id, str) and not keep(bar_id):
            del result["bar"]["id"]

        for pointer in common.read_json(ctx.stage / self.name / "redacted.json", []):
            value = ctx.secret_values.get(pointer)
            if value is None:
                value = _get(target, pointer)  # keep this computer's own key, if it has one
            if isinstance(value, str):
                _set(result, pointer, value)
        return result

    def plan(self, ctx):
        if not (ctx.stage / self.name / "shell.json").exists():
            return []
        known = set(installed_plugins()) | ctx.expected_plugins
        result = self.merged(ctx, known)
        target = common.read_json(H() / self.PATH)
        dropped = self._dropped(ctx, known)
        actions = []
        if dropped:
            actions.append(Action(self.name, "skipped", f"not in the layout (plugin missing): {', '.join(dropped)}"))
        status = "same" if result == target else ("changed" if target is not None else "new")
        actions.append(Action(self.name, status, common.rel(H() / self.PATH), lambda: self._write(ctx)))
        return actions

    def _dropped(self, ctx, known):
        """Plugin ids the exported bar or plugin list uses that will not be installed here."""
        src = common.read_json(ctx.stage / self.name / "shell.json", {})
        referenced = set()
        layout = src.get("bar", {}).get("layout", {})
        for entries in layout.values() if isinstance(layout, dict) else []:
            for e in entries if isinstance(entries, list) else []:
                pid = e if isinstance(e, str) else e.get("id") if isinstance(e, dict) else None
                if isinstance(pid, str):
                    referenced.add(pid)
        for e in src.get("plugins", []) or []:
            if isinstance(e, dict) and isinstance(e.get("id"), str):
                referenced.add(e["id"])
        return sorted(p for p in referenced if not p.startswith("omarchy.") and p not in known)

    def _write(self, ctx):
        # Recomputed now: a plugin that failed to install must not stay in the bar.
        result = self.merged(ctx, set(installed_plugins()))
        target = H() / self.PATH
        ctx.backup.save(target)
        common.write_file(target, (json.dumps(result, indent=2, ensure_ascii=False) + "\n").encode(), 0o600)


def _get(node, pointer):
    for part in pointer.strip("/").split("/"):
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list):
            node = next((e for e in node if isinstance(e, dict) and e.get("id") == part), None) \
                if not part.isdigit() else (node[int(part)] if int(part) < len(node) else None)
        else:
            return None
    return node


def _set(root, pointer, value):
    parts = pointer.strip("/").split("/")
    parent = _get(root, "/".join(parts[:-1])) if len(parts) > 1 else root
    if isinstance(parent, dict):
        parent[parts[-1]] = value


# --------------------------------------------------------------------------- ~/.config/omarchy

class OmarchySection(Section):
    name = "omarchy"
    title = "Omarchy files (hooks · menu additions · backgrounds · branding · keyboard)"

    MENU = ".config/omarchy/extensions/omarchy-menu.jsonc"
    BLOCK = "omarchy-sync import"

    def collect(self, ctx):
        files = Files(ctx, self.name)
        n = files.add_tree(".config/omarchy", skip=lambda r: OMARCHY_SKIP.match(r))
        n += sum(files.add(r) for r in (".config/uwsm/env.d/50-omarchy-language",))
        files.save()
        menu = self._menu_entries(H() / self.MENU)
        if menu:
            common.write_json(ctx.stage / self.name / "menu.json", menu)
        ctx.report.ok(self.name, f"{n} files, {len(menu)} menu entries")

    @staticmethod
    def _menu_entries(path: Path) -> dict:
        try:
            text = path.read_text()
        except OSError:
            return {}
        own = set(jsonc.block_entries(text, "omarchy-sync menu"))
        entries = jsonc.loads(text)
        return {k: v for k, v in entries.items()
                if k not in own and not TRY_OMARCHY.search(json.dumps(v))}

    def plan(self, ctx):
        actions = Files(ctx, self.name).plan()
        entries = common.read_json(ctx.stage / self.name / "menu.json", {})
        if entries:
            moved = common.rewrite_home(json.dumps(entries, ensure_ascii=False).encode(), ctx.source.get("home", ""))
            entries = json.loads(moved)
            path = H() / self.MENU
            text = path.read_text() if path.exists() else (common.SKEL / self.MENU).read_text() if (common.SKEL / self.MENU).exists() else ""
            mine = jsonc.loads(jsonc.remove_block(text, self.BLOCK))
            wanted = {k: v for k, v in entries.items() if mine.get(k) != v}
            current = jsonc.block_entries(text, self.BLOCK)
            status = "same" if wanted == current else ("changed" if path.exists() else "new")
            actions.append(Action(self.name, status, f"{len(wanted)} Omarchy menu entries in {common.rel(path)}",
                                  lambda: self._write_menu(ctx, path, text, wanted)))
        return actions

    def _write_menu(self, ctx, path, text, wanted):
        ctx.backup.save(path)
        new = jsonc.put_block(text, self.BLOCK, wanted,
                              "Imported by Omarchy Sync; the next import replaces this block.")
        jsonc.loads(new)  # never write a file the menu could not read
        common.write_file(path, new.encode(), 0o644)


# --------------------------------------------------------------------------- Hyprland

class HyprSection(Section):
    name = "hypr"
    title = "Hyprland (bindings · look and feel · autostart · window rules)"

    def collect(self, ctx):
        files = Files(ctx, self.name)
        n = files.add_tree(".config/hypr", skip=lambda r: r.endswith(".luarc.json"),
                           hardware=lambda r: r in HYPR_HARDWARE)
        files.save()
        ctx.report.ok(self.name, f"{n} changed files")

    def plan(self, ctx):
        def select(item):
            if item.get("hardware") and not ctx.options.get("include_hardware") and not ctx.same_machine:
                return "this computer's own screens/input devices (use --include-hardware to copy)"
            return None
        return Files(ctx, self.name).plan(select=select)


# --------------------------------------------------------------------------- app configs

def apps_list() -> list:
    path = common.config_dir() / "apps.list"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_APPS)
    return [l.strip() for l in path.read_text().splitlines() if l.strip() and not l.lstrip().startswith("#")]


class AppsSection(Section):
    name = "apps"
    title = "App configs (from ~/.config/omarchy-sync/apps.list) and your scripts"

    def collect(self, ctx):
        files = Files(ctx, self.name)
        n = 0
        for relpath in apps_list():
            relpath = relpath.removeprefix("~/").strip("/")
            if ".." in relpath.split("/") or any(relpath == d or relpath.startswith(d + "/") for d in DEFAULT_SECRETS):
                ctx.report.skip(self.name, f"{relpath}: not allowed here")
                continue
            n += files.add_tree(relpath)
        # ~/.local/bin scripts that bindings, hooks or menu entries call.
        refs = set()
        for section in ("omarchy", "hypr"):
            for p in common.walk_files(ctx.stage / section, skip_dirs=()):
                if p.is_file():
                    refs |= set(BIN_REF.findall(p.read_text(errors="replace")))
        for name in sorted(refs):
            if files.add(f".local/bin/{name}", keep_skel=True):
                n += 1
                ctx.report.note(self.name, f"~/.local/bin/{name} (used by your settings)")
        files.save()
        ctx.report.ok(self.name, f"{n} files")

    def plan(self, ctx):
        actions = Files(ctx, self.name).plan()
        if any(a.pending and a.text.startswith("~/.config/mise/") for a in actions) and common.have("mise"):
            actions.append(Action(self.name, "new", "mise install (tools from your mise config)",
                                  lambda: common.run(["mise", "install"], capture=False), late=True))
        return actions


# --------------------------------------------------------------------------- web apps and TUIs

class WebappsSection(Section):
    name = "webapps"
    title = "Web apps and TUI launchers"

    LAUNCHER = re.compile(r"^Exec=.*(omarchy-launch-webapp|omarchy-webapp-handler|--app-id=TUI\.)", re.M)

    def collect(self, ctx):
        files = Files(ctx, self.name)
        apps = H() / ".local/share/applications"
        n = 0
        for desktop in sorted(apps.glob("*.desktop")):
            try:
                text = desktop.read_text()
            except OSError:
                continue
            if not self.LAUNCHER.search(text):
                continue
            if files.add(str(desktop.relative_to(H()))):
                n += 1
                icon = re.search(r"^Icon=(.+)$", text, re.M)
                if icon:
                    self._add_icon(files, icon.group(1).strip())
        files.save()
        ctx.report.ok(self.name, f"{n} launchers")

    @staticmethod
    def _add_icon(files, icon):
        p = Path(os.path.expanduser(icon))
        if p.is_absolute():
            if common.is_inside(p, H()):
                files.add(str(p.relative_to(H())), keep_skel=True)
            return
        for found in sorted((H() / ".local/share/icons/hicolor").glob(f"*/apps/{icon}.*")):
            files.add(str(found.relative_to(H())), keep_skel=True)

    def plan(self, ctx):
        actions = Files(ctx, self.name).plan(
            describe=lambda item: Path(item["path"]).stem if item["path"].endswith(".desktop") else common.rel(H() / item["path"]))
        if any(a.pending for a in actions):
            actions.append(Action(self.name, "new", "refresh the launcher and icon caches", self._refresh, late=True))
        return actions

    @staticmethod
    def _refresh():
        common.run(["update-desktop-database", str(H() / ".local/share/applications")])
        common.run(["gtk-update-icon-cache", "-f", "-t", str(H() / ".local/share/icons/hicolor")])


# --------------------------------------------------------------------------- settings

HARDWARE_FLAG = re.compile(r"gpu|touchpad|touchscreen|input|hibernat|suspend|battery|crash|lid")


class SettingsSection(Section):
    name = "settings"
    title = "Font · default apps · toggles · weather location"

    def collect(self, ctx):
        state = H() / ".local/state/omarchy"
        toggles = state / "toggles"
        data = {
            "font": common.out(["omarchy-font-current"]),
            "terminal": common.out(["omarchy-default-terminal"]),
            "editor": common.out(["omarchy-default-editor"]),
            "browser": common.out(["omarchy-default-browser"]),
            "hyprToggles": sorted(p.stem for p in (toggles / "hypr").glob("*.lua")
                                  if p.stem != "flags" and not p.stem.startswith("zz-")),
            "flags": sorted(p.name for p in toggles.iterdir() if p.is_file()) if toggles.is_dir() else [],
        }
        common.write_json(ctx.stage / self.name / "settings.json", data)
        files = Files(ctx, self.name)
        files.add(".local/state/omarchy/settings/weather.json")
        files.save()
        ctx.report.ok(self.name, f"font {data['font'] or '?'}, terminal {data['terminal'] or '?'}, "
                                 f"browser {data['browser'] or '?'}, {len(data['hyprToggles'])} look toggles")

    def plan(self, ctx):
        data = common.read_json(ctx.stage / self.name / "settings.json", {})
        actions = Files(ctx, self.name).plan()
        actions += self._defaults(data)
        actions += self._toggles(ctx, data)
        return actions

    def _defaults(self, data):
        actions = []
        font = data.get("font")
        if font:
            here = common.out(["omarchy-font-current"])
            if here == font:
                actions.append(Action(self.name, "same", f"font {font}"))
            else:
                actions.append(Action(self.name, "changed", f"font {here} → {font}",
                                      lambda: self._font(font), late=True))
        for kind, valid in (("terminal", {"alacritty", "foot", "ghostty", "kitty"}),
                            ("editor", {"code", "cursor", "zed", "sublime_text", "subl", "helix", "vim", "emacs", "nvim"}),
                            ("browser", {"chromium", "chrome", "brave", "brave-origin", "edge", "firefox", "zen"})):
            want = data.get(kind, "")
            if want not in valid:
                continue
            here = common.out([f"omarchy-default-{kind}"])
            if here == want:
                actions.append(Action(self.name, "same", f"default {kind} {want}"))
            else:
                actions.append(Action(self.name, "changed", f"default {kind} {here or '?'} → {want}",
                                      lambda kind=kind, want=want: self._default(kind, want)))
        return actions

    @staticmethod
    def _font(font):
        if font not in common.out(["fc-list", ":", "family"]):
            raise RuntimeError(f"font {font} is not installed here (export with Extra packages to bring it)")
        if common.run(["omarchy-font-set", font]).returncode != 0:
            raise RuntimeError("omarchy-font-set failed")

    @staticmethod
    def _default(kind, want):
        # In a terminal, --install installs a missing app right here. From the GUI there is
        # no terminal, and without --install the command opens its own to install the app.
        args = [f"omarchy-default-{kind}", want] if common.gui() else [f"omarchy-default-{kind}", "--install", want]
        if common.run(args, capture=False).returncode != 0:
            raise RuntimeError(f"omarchy-default-{kind} {want} failed")

    def _toggles(self, ctx, data):
        actions = []
        available = {p.stem for p in (common.OMARCHY_PATH / "default/hypr/toggles").glob("*.lua")} - {"flags"}
        on_here = {p.stem for p in (H() / ".local/state/omarchy/toggles/hypr").glob("*.lua")}
        want = set(data.get("hyprToggles", []))
        src_vm, here_vm = common.is_vm(ctx.source), common.is_vm(ctx.here)
        for name in sorted(available):
            if name in common.VM_TOGGLES and (src_vm or here_vm):
                continue  # set for the VM's sake, not by choice
            wanted, now = name in want, name in on_here
            if wanted == now:
                if wanted:
                    actions.append(Action(self.name, "same", f"look: {name} on"))
                continue
            state = "on" if wanted else "off"
            actions.append(Action(self.name, "changed", f"look: {name} {state}",
                                  lambda name=name, state=state: self._run(["omarchy-hyprland-toggle", name, state])))
        flags_dir = H() / ".local/state/omarchy/toggles"
        flags_here = {p.name for p in flags_dir.iterdir() if p.is_file()} if flags_dir.is_dir() else set()
        flags_want = set(data.get("flags", []))
        for name in sorted((flags_here | flags_want)):
            if HARDWARE_FLAG.search(name):
                continue
            wanted, now = name in flags_want, name in flags_here
            if wanted != now:
                state = "on" if wanted else "off"
                actions.append(Action(self.name, "changed", f"toggle {name} {state}",
                                      lambda name=name, state=state: self._run(["omarchy-toggle", name, state])))
        return actions

    @staticmethod
    def _run(args):
        if common.run(args).returncode != 0:
            raise RuntimeError(" ".join(args) + " failed")


# Import order: packages first (fonts, apps the settings need), secrets before git
# clones that may use SSH, plugins before shell.json enables them, settings after the
# app configs they edit. Late actions (theme switch, font, reloads) run after all.
SECTIONS = [PackagesSection(), SecretsSection(), PluginsSection(), ThemeSection(), ShellSection(),
            OmarchySection(), HyprSection(), AppsSection(), WebappsSection(), SettingsSection()]

# Export order differs in one place: the files sections must exist before Apps looks
# for ~/.local/bin references, and shell.json values must be known before secrets seal.
EXPORT_ORDER = ["packages", "plugins", "theme", "shell", "omarchy", "hypr", "apps", "webapps", "settings", "secrets"]

BY_NAME = {s.name: s for s in SECTIONS}
