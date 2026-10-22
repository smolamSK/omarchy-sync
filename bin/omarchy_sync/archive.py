"""The export file: a tar.gz of a staging folder, checksummed; secrets encrypted with gpg."""

import hashlib
import io
import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path

from . import common

MANIFEST = "manifest.json"
SECRETS = "secrets.tar.gpg"


class ArchiveError(Exception):
    pass


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def checksums(stage: Path) -> dict:
    sums = {}
    for p in common.walk_files(stage, skip_dirs=()):
        name = str(p.relative_to(stage))
        if name != MANIFEST and p.is_file() and not p.is_symlink():
            sums[name] = sha256(p)
    return sums


def staging(prefix: str) -> Path:
    """A private temporary folder (mode 0700), in XDG_RUNTIME_DIR when there is one."""
    return Path(tempfile.mkdtemp(prefix=prefix, dir=common.runtime_dir()))


def write(stage: Path, target: Path) -> None:
    """Pack the staging folder into target (written beside it, then renamed)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name("." + target.name + ".part")
    try:
        with tarfile.open(tmp, "w:gz", compresslevel=6) as tar:
            # The manifest goes first, so peek() can recognise an export by reading its head.
            for p in sorted(stage.iterdir(), key=lambda p: (p.name != MANIFEST, p.name)):
                tar.add(p, arcname=p.name)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def peek(path: Path):
    """The manifest of an Omarchy Sync export, read from the head of the file only (without
    checksums), or None. Finds exports whatever they are named; exports from before the
    manifest went first are only recognised by their default name."""
    try:
        with tarfile.open(path, "r|gz") as tar:
            first = tar.next()
            if first is None or first.name != MANIFEST or not first.isfile() or first.size > (1 << 20):
                return None
            manifest = json.loads(tar.extractfile(first).read())
    except (tarfile.TarError, OSError, EOFError, ValueError, AttributeError):
        return None
    if not isinstance(manifest, dict) or manifest.get("kind") != "omarchy-sync":
        return None
    manifest.pop("checksums", None)
    return manifest


def extract(source: Path, dest: Path) -> dict:
    """Unpack into dest and verify every checksum. Returns the manifest."""
    try:
        with tarfile.open(source, "r:*") as tar:
            # The "data" filter refuses absolute paths, .., devices and links leaving dest.
            tar.extractall(dest, filter="data")
    except (tarfile.TarError, OSError, EOFError) as e:
        raise ArchiveError(f"cannot read {source}: {e}") from e
    manifest = common.read_json(dest / MANIFEST)
    if not isinstance(manifest, dict) or manifest.get("kind") != "omarchy-sync":
        raise ArchiveError(f"{source} is not an Omarchy Sync export")
    if manifest.get("format", 0) > common.FORMAT:
        raise ArchiveError(f"{source} was made by a newer Omarchy Sync (format {manifest['format']}); update this plugin")
    expected = manifest.get("checksums", {})
    actual = checksums(dest)
    bad = sorted(n for n in expected if actual.get(n) != expected[n])
    extra = sorted(set(actual) - set(expected))
    if bad or extra:
        raise ArchiveError(f"{source} is damaged ({len(bad)} files differ, {len(extra)} unexpected)")
    return manifest


def _gpg(args, passphrase: str, data: bytes) -> bytes:
    """Run gpg with the passphrase on a pipe, never in argv or the environment."""
    r, w = os.pipe()
    try:
        proc = subprocess.Popen(
            ["gpg", "--batch", "--quiet", "--no-tty", "--pinentry-mode", "loopback",
             "--passphrase-fd", str(r), *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=(r,),
        )
    except FileNotFoundError as e:
        os.close(r)
        os.close(w)
        raise ArchiveError("gpg is not installed") from e
    os.close(r)
    os.write(w, passphrase.encode() + b"\n")
    os.close(w)
    stdout, stderr = proc.communicate(data)
    if proc.returncode != 0:
        msg = "wrong passphrase" if b"decrypt" in stderr.lower() or b"bad session key" in stderr.lower() else "gpg failed"
        raise ArchiveError(msg)
    return stdout


def seal(folder: Path, target: Path, passphrase: str) -> None:
    """tar the folder and encrypt it symmetrically (AES256) into target.

    The key derivation is spelled out rather than left to each gpg's defaults:
    iterated and salted S2K with SHA-512 and the largest count gpg allows, which makes
    every passphrase guess as slow as gpg can. Decryption reads these settings from
    the file, so exports made before this change still open."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(folder.iterdir()):
            tar.add(p, arcname=p.name)
    target.write_bytes(_gpg(["--symmetric", "--cipher-algo", "AES256", "--s2k-mode", "3",
                             "--s2k-digest-algo", "SHA512", "--s2k-count", "65011712", "--output", "-"],
                            passphrase, buf.getvalue()))


def unseal(source: Path, dest: Path, passphrase: str) -> None:
    data = _gpg(["--decrypt", "--output", "-"], passphrase, source.read_bytes())
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(dest, filter="data")
