# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

An Omarchy shell plugin (`io.github.smolamsk.sync`) that exports a user's Omarchy settings to one `omarchy-settings-<host>-<date>.tar.gz` file and imports it on another Omarchy computer, including aarch64 ↔ x86_64. It replaced the one-off `restore.sh` written for the 2026-10-22 VM migration.

- `bin/omarchy-sync` (Bash) only starts `bin/omarchy_sync` (Python standard library) with `python3 -I -B`; the wrapper inserts its own folder into `sys.path`. All logic lives there; both UIs drive it.
- GUI: `SyncWindow.qml` (kind `overlay`, an `OverlayWindow` so text fields get keyboard focus) is a view only. `Service.qml` owns every CLI run (`run(kind, args, secret)`), because the host unloads a hidden overlay and would kill a run that lived there. The window gets the service through the injected `service` property. The service also runs `service-start` once, 8 s after load.
- Terminal: `wizard.py` (gum), started by `omarchy-sync` with no arguments or the *Terminal Wizard* menu row.

## Layout

- `sections.py`: one class per section, each with `collect(ctx)` (export) and `plan(ctx)` (import). `plan` returns `Action`s; `Action.fn` does the change, and `late=True` actions run after every section. `SECTIONS` is the import order and `EXPORT_ORDER` the export order. Both orders matter: packages come before fonts and default apps, plugins before `shell.json`, shell before secrets on export (redacted values), and omarchy/hypr before apps (`~/.local/bin` references).
- `model.py`: `Ctx`, `Action`, `Backup` (+ `undo`), and `Files`, the generic "files under HOME" staging used by most sections (`<stage>/<section>/files/<relpath>` + `files.json`).
- `engine.py`: `export()`, `Import` (extract → plan → apply → report), `undo()`, `backups()`.
- `packages.py`: extras = `pacman -Qqe` − `omarchy-pkg-defaults` − `DENY`; on import, each package is checked against `pacman -Slq`, then `yay -Si --aur`.
- `archive.py`: tar.gz + sha256 manifest (`kind: omarchy-sync`, `format`); secrets are `secrets.tar.gpg` (gpg symmetric AES256, S2K mode 3 / SHA512 / count 65011712, passphrase via `--passphrase-fd`).
- `scan.py`: secret detection for plain files (`findings()` → line numbers and kinds, never values). `Files._holds_secrets` runs it on every staged file outside the secrets section.
- `jsonc.py`: comment- and trailing-comma-aware JSONC, plus fenced blocks (`// >>> name >>>`) in `omarchy-menu.jsonc`.
- `menu.py`: the app launcher `.desktop` (own file, marked `X-Omarchy-Sync=1`), the Omarchy menu rows (block `omarchy-sync menu`) and `service_start`. `remote.py`: rclone push/pull. `wizard.py`: gum prompts. `cli.py`: argparse and `HELP`.

## GUI ↔ CLI protocol

- `info --json` and `inspect FILE --json` answer with one JSON object. `inspect` always exits 0 and returns `{"error": …}` on failure, because Quickshell doesn't order `exited` against `StdioCollector.streamFinished`.
- Runs are `omarchy-sync --events …`. Each event is a stdout line holding a JSON object with an `event` key:
  - `step` comes from `Report.add`.
  - `running` is emitted before each action.
  - `plan` comes from `import --plan-json`, or from any import before it applies.
  - `done` carries `file` (export), `backup` (import) or `undone`, plus `counts`.
  - `error` comes from the CLI's exception handler.
  - Every other line is log text. Never print a line starting with `{"event"` that isn't an event.
- The runner finishes 250 ms after `exited` (the `settle` timer) so trailing output is parsed. The passphrase is written to stdin in `onStarted` (`--passphrase-stdin`), then stdin is closed so no child command waits on it.
- `OMARCHY_SYNC_GUI=1` (set by the runner) means no terminal: `packages._argv` uses `pkexec pacman` / `yay --sudo pkexec` (Omarchy's polkit agent prompts), and `SettingsSection._default` drops `--install` so `omarchy-default-*` opens its own terminal for a missing app.
- Menu rows run `omarchy-shell shell summon io.github.smolamsk.sync '{"tab":"…"}'`, and the launcher runs `… toggle …`. `service_start` refreshes our menu block when `entries()` changes, but only if the block is already there.
- QML lint: `qmllint -I <dir with qs → /usr/share/omarchy/shell> -I /usr/lib/qt6/qml SyncWindow.qml Service.qml`. Warnings like "not found on type QObject" (Style.spacing/font) and the QVariantMap → QVariantHash `environment` warning are expected.

## Contracts

- **Consent.** The plugin writes only its own files on its own: the launcher and its state marker. The user's menu file changes only after the notification click or `menu install`. An import changes nothing before the plan has been shown and confirmed (or `--yes` given).
- **Everything replaced is backed up first** (`Backup.save` in `Files.write`, `ShellSection._write`, `_write_menu`). Any new writer must call `ctx.backup.save(path)` before writing.
- **Only what the user changed travels**: `Files.add` drops files identical to `/etc/skel` (`keep_skel=True` overrides). Links are followed, and links leading outside HOME are dropped.
- **Machine-specific things stay behind**: `monitors.lua` and `input.lua` (unless `--include-hardware` or the same host), VM toggles (`VM_TOGGLES`) when either side is a VM, hardware toggle flags (`HARDWARE_FLAG`), Try Omarchy menu rows (`TRY_OMARCHY`), and `packages.DENY`.
- **Secrets never appear in plain text**: `shell.json` values whose key matches `SECRET_KEY` are blanked in the plain part and stored (by JSON path) in the sealed `values.json`. Any other file `scan.findings` flags is added to `ctx.secret_files` and sealed with the secrets (when a passphrase was given) or left out. Secrets must stay last in `EXPORT_ORDER` for this. Never log file contents; the report names files, lines and kinds only. Prefer false alarms to misses when changing `scan.py`.
- Paths into the source home are rewritten to the target home (`common.rewrite_home`) in every text file and in menu entries.
- Section titles must not contain commas: `gum choose --selected` splits on them.
- Bump `common.FORMAT` only for an incompatible archive change; `archive.extract` refuses newer formats.

## Development

- Checks: `bash -n bin/omarchy-sync`, `python3 -I -m py_compile bin/omarchy_sync/*.py`, `omarchy plugin validate "$PWD"`, `omarchy-preflight`.
- `OMARCHY_SYNC_PLATFORM=x86` pretends to be another platform (`common.platform`), e.g. for `import --dry-run`.
- Safe to try on this machine: `export -o <dir>` (it only reads, except that it creates `~/.config/omarchy-sync/apps.list`) and `import FILE --dry-run`. A real import changes the desktop; `undo` reverts its files.
