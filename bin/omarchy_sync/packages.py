"""Extra packages: what you installed beyond Omarchy's defaults, and whether this computer can have them."""

import re

from . import common
from .model import Action

SECTION = "packages"

# Never carried: the kernel, boot chain, keyrings, firmware and Omarchy itself belong to
# the machine and its architecture, and the installer already chose them.
DENY = re.compile(
    r"^(base|base-devel|linux(-.*)?|.*-keyring|.*firmware.*|limine.*|mkinitcpio.*|snapper|snap-pac|"
    r"grub|efibootmgr|systemd-boot.*|plymouth.*|omarchy(-.*)?|try-omarchy.*|archlinuxarm-.*|"
    r"asahi-.*|uboot-.*|raspberrypi-.*|nvidia.*|xf86-video-.*|vulkan-(intel|radeon|nouveau|asahi)|"
    r"intel-ucode|amd-ucode|.*-dkms|ex-vi-compat)$"
)


def _names(text: str) -> set:
    return {line.split()[0] for line in text.splitlines() if line.strip()}


def defaults(platform: str) -> set:
    names = _names(common.out(["omarchy-pkg-defaults", platform]))
    other = common.OMARCHY_PATH / "install/omarchy-other.packages"
    try:
        names |= {re.sub(r"\s*#.*$", "", l).strip() for l in other.read_text().splitlines()} - {""}
    except OSError:
        pass
    return names


def extras() -> dict:
    """Explicitly installed packages that are not Omarchy's defaults, split into repo and AUR."""
    explicit = _names(common.out(["pacman", "-Qqe"]))
    foreign = _names(common.out(["pacman", "-Qqm"]))
    base = defaults(common.platform())
    picked = sorted(p for p in explicit - base if not DENY.match(p))
    return {"repo": [p for p in picked if p not in foreign], "aur": [p for p in picked if p in foreign]}


def collect(ctx) -> None:
    found = extras()
    chosen = ctx.options.get("packages")
    if chosen is not None:
        found = {k: [p for p in v if p in chosen] for k, v in found.items()}
    common.write_json(ctx.stage / SECTION / "packages.json", found)
    ctx.report.ok(SECTION, f"{len(found['repo'])} from the repositories, {len(found['aur'])} from the AUR")


def _aur_known(names) -> set:
    if not names or not common.have("yay"):
        return set()
    r = common.run(["yay", "-Si", "--aur", "--", *names])
    return set(re.findall(r"^Name\s*:\s*(\S+)", r.stdout or "", re.M))


def plan(ctx) -> list:
    data = common.read_json(ctx.stage / SECTION / "packages.json", {})
    wanted = sorted(set(data.get("repo", [])) | set(data.get("aur", [])))
    if not wanted:
        return []
    installed = _names(common.out(["pacman", "-Qq"]))
    repo = _names(common.out(["pacman", "-Slq"]))
    missing = [p for p in wanted if p not in installed]
    aur = _aur_known([p for p in missing if p not in repo])
    arch = ctx.here.get("arch", "")
    actions = [Action(SECTION, "same", p) for p in wanted if p in installed]
    from_repo = [p for p in missing if p in repo]
    from_aur = [p for p in missing if p not in repo and p in aur]
    unavailable = [p for p in missing if p not in repo and p not in aur]
    if from_repo:
        actions.append(Action(SECTION, "new", f"install {len(from_repo)}: {' '.join(from_repo)}",
                              lambda: _install(ctx, "omarchy-pkg-add", from_repo)))
    for p in from_aur:
        actions.append(Action(SECTION, "new", f"install {p} (AUR, builds here)",
                              lambda p=p: _install(ctx, "omarchy-pkg-aur-add", [p])))
    for p in unavailable:
        actions.append(Action(SECTION, "skipped", f"{p}: not available for {arch}{_near(p, repo)}"))
    return actions


def _near(name: str, repo: set) -> str:
    stem = re.sub(r"[-_](bin|git|stable|beta|nightly|appimage)$", "", name)
    near = sorted(p for p in repo if p != name and (p == stem or p.startswith(stem + "-")))[:3]
    return f" (similar here: {', '.join(near)})" if near else ""


def _argv(command: str, names) -> list:
    """The install command. Without a terminal (the GUI) sudo can't ask for the password,
    so pacman runs through pkexec and Omarchy's polkit agent asks instead."""
    if not common.gui():
        return [command, *names]
    if command == "omarchy-pkg-add":
        return ["pkexec", "pacman", "-S", "--needed", "--noconfirm", "--", *names]
    return ["yay", "-S", "--needed", "--noconfirm", "--sudo", "pkexec", "--", *names]


def _installed(names) -> bool:
    return common.run(["pacman", "-Q", "--", *names]).returncode == 0


def _install(ctx, command: str, names) -> None:
    common.say(f"\n→ {command} {' '.join(names)}")
    if common.run(_argv(command, names), capture=False).returncode == 0 and _installed(names):
        return
    if len(names) > 1:
        # One bad package fails the whole transaction; find out which, install the rest.
        for p in names:
            if common.run(_argv(command, [p]), capture=False).returncode != 0 or not _installed([p]):
                ctx.report.fail(SECTION, f"{p} did not install")
        return
    raise RuntimeError(f"{names[0]} did not install")
