"""Where you start Omarchy Sync: an app launcher entry, and (once you agree) Omarchy menu rows."""

from pathlib import Path

from . import common, jsonc

BLOCK = "omarchy-sync menu"
MENU = Path(".config/omarchy/extensions/omarchy-menu.jsonc")
DESKTOP = Path(".local/share/applications/omarchy-sync.desktop")
OFFERED = "menu-offered"

# Nerd Font glyphs: sync, export, import, undo, terminal.
ICON_SYNC, ICON_EXPORT, ICON_IMPORT, ICON_UNDO, ICON_TERMINAL = (
    chr(0xF04E6), chr(0xF0207), chr(0xF02FA), chr(0xF054C), chr(0xF018D))


def script() -> str:
    """This plugin's command, written with $HOME so the menu row survives a renamed user."""
    path = str(Path(__file__).resolve().parent.parent / "omarchy-sync")
    home = str(common.home())
    return "$HOME" + path[len(home):] if path.startswith(home + "/") else path


def _launch(args: str) -> str:
    return f"omarchy-launch-floating-terminal-with-presentation \"{script()} {args}\""


def _window(tab: str) -> str:
    """Open the GUI (SyncWindow.qml) on a tab."""
    return f"omarchy-shell shell summon {common.PLUGIN_ID} '{{\"tab\":\"{tab}\"}}'"


def entries() -> dict:
    return {
        "setup.sync": {"icon": ICON_SYNC, "label": "Sync Settings",
                       "description": "Carry your Omarchy setup to another computer"},
        "setup.sync.export": {"icon": ICON_EXPORT, "label": "Export…",
                              "description": "Save your settings to a file, USB key or rclone remote",
                              "action": _window("export")},
        "setup.sync.import": {"icon": ICON_IMPORT, "label": "Import…",
                              "description": "Apply settings exported on another Omarchy computer",
                              "action": _window("import")},
        "setup.sync.undo": {"icon": ICON_UNDO, "label": "Undo Last Import",
                            "when": f"\"{script()}\" undo --check",
                            "action": _window("undo")},
        "setup.sync.terminal": {"icon": ICON_TERMINAL, "label": "Terminal Wizard",
                                "description": "The same in a terminal (gum prompts)",
                                "action": _launch("wizard")},
    }


def install() -> None:
    path = common.home() / MENU
    text = path.read_text() if path.exists() else "{\n}\n"
    new = jsonc.put_block(text, BLOCK, entries(), "Added by the Omarchy Sync plugin; `omarchy-sync menu remove` takes it out.")
    jsonc.loads(new)
    if new != text:
        common.write_file(path, new.encode(), 0o644)
    common.say(f"Omarchy menu: Setup → Sync Settings ({common.rel(path)})")


def remove() -> None:
    path = common.home() / MENU
    if path.exists():
        text = path.read_text()
        new = jsonc.remove_block(text, BLOCK)
        if new != text:
            common.write_file(path, new.encode(), 0o644)
    (common.home() / DESKTOP).unlink(missing_ok=True)
    common.say("Removed the Omarchy Sync menu rows and app launcher.")


def desktop_entry() -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Omarchy Sync\n"
        "Comment=Export your Omarchy settings or import them from another computer\n"
        f"Exec=omarchy-shell shell toggle {common.PLUGIN_ID}\n"
        "Icon=folder-sync\n"
        "Categories=Settings;\n"
        "Keywords=backup;restore;migrate;export;import;settings;\n"
        "X-Omarchy-Sync=1\n"
    )


def service_start() -> None:
    """Run by Service.qml at login: keep our own launcher current; offer the menu rows once."""
    desktop = common.home() / DESKTOP
    content = desktop_entry()
    if not desktop.exists() or desktop.read_text() != content:
        if desktop.exists() and "X-Omarchy-Sync=1" not in desktop.read_text():
            return  # someone else's file of the same name: leave it alone
        common.write_file(desktop, content.encode(), 0o644)
    path = common.home() / MENU
    # Rows the user already accepted are ours to keep current (a plugin update may change them).
    if path.exists():
        text = path.read_text()
        if jsonc.block_entries(text, BLOCK) and jsonc.block_entries(text, BLOCK) != entries():
            install()
    marker = common.state_dir() / OFFERED
    if marker.exists():
        return
    if path.exists() and jsonc.block_entries(path.read_text(), BLOCK):
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch()
        return
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    common.run(["omarchy-notification-send", "-g", ICON_SYNC, "--app-name", "Omarchy Sync",
                "Omarchy Sync is ready",
                "Open it from the app launcher. Click here to also add Export / Import under Setup in the Omarchy menu.",
                "--exec", str(Path(__file__).resolve().parent.parent / "omarchy-sync"), "menu", "install", "--notify"])


def notify(text: str) -> None:
    common.run(["omarchy-notification-send", "-g", ICON_SYNC, "--app-name", "Omarchy Sync", "Omarchy Sync", text])

