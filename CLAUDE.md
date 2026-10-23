# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

An Omarchy shell plugin (`io.github.smolamsk.sync`) that exports a user's Omarchy settings to one `omarchy-settings-<host>-<date>.tar.gz` file and imports it on another Omarchy computer, including aarch64 ↔ x86_64. It replaced the one-off `restore.sh` written for the 2026-10-22 VM migration.

- `bin/omarchy-sync` (Bash) only starts `bin/omarchy_sync` (Python standard library) with `python3 -I -B`; the wrapper inserts its own folder into `sys.path`. All logic lives there; both UIs drive it.
- GUI: `SyncWindow.qml` (kind `overlay`, a fullscreen `PanelWindow` on the overlay layer with exclusive keyboard focus so text fields get typing; released Omarchy shells have no `OverlayWindow`) is a view only. `Service.qml` owns every CLI run (`run(kind, args, secret)`), because the host unloads a hidden overlay. The run itself isn't the service's child either: the host also destroys every plugin service when it hot-reloads plugins (which an import triggers as soon as it adds one) and on a shell restart, so runs go through `job.py` (see the protocol below). The window gets the service through the injected `service` property. The service also runs `service-start` once, 8 s after load.
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
  - `progress` (`done`, `total`, `label`): on export one per section, plus packing and upload; on import one per pending action. Plan, pull and undo send none, so the bar sweeps.
  - `plan` comes from `import --plan-json`, or from any import before it applies.
  - `done` (`engine.finish`, also saved to `~/.local/state/omarchy-sync/last-run.json`) carries `action`, `ok`, `counts`, up to 10 `failures`, and `file`/`size`/`remote` (export), `backup`/`restarting` (import) or `undone`. The window's result card reads it, or `info.lastRun` after a shell restart.
  - `error` comes from the CLI's exception handler.
  - Every other line is log text. Never print a line starting with `{"event"` that isn't an event.
- Runs are jobs (`job.py`): the service runs `omarchy-sync job start KIND -- ARGS`, which launches a supervisor (`job run`) in its own session, writes `~/.local/state/omarchy-sync/job.json` (kind, pid, log) and prints one `job` event. The supervisor runs `omarchy-sync --events ARGS`, writes its stdout lines to `job.log`, its stderr lines as `log` events (`stream: "err"`), and a final `exit` event (`code`). The service follows the log with `tail -c +1 -F --pid=<supervisor>`. Each new service instance asks `job status --json` and, if a run is still going, replays its log from the top, so a plugin reload or shell restart mid-run loses nothing. `job start` refuses while a run is going. The service notes the window opening and closing in job.json (`job window open|closed`); a reload closes the window and destroys the service together, so a new service that finds the window closed less than 5 s ago (`windowAgo`) summons it again.
- The runner finishes 250 ms after tail exits (the `settle` timer) so trailing output is parsed. The passphrase is written to `job start`'s stdin in `onStarted` (`--passphrase-stdin`) and passed to the supervisor through a pipe, which the run then reads; stdin is closed so no child command waits on it.
- `OMARCHY_SYNC_GUI=1` (set by the runner) means no terminal: `packages._argv` uses `pkexec pacman` / `yay --sudo pkexec` (Omarchy's polkit agent prompts), and `SettingsSection._default` drops `--install` so `omarchy-default-*` opens its own terminal for a missing app.
- Notifications: `omarchy-sync notify-last` turns last-run.json into one (`engine.result_text`). The service runs it when a run ends while the window is closed (`windowOpen`). An import or undo from the GUI doesn't restart the shell itself (`common.restart_shell` is a no-op under `OMARCHY_SYNC_GUI`; `done` says `restarting`). The service does it with `omarchy-sync restart-shell`: when the window closes or on *Restart shell now* if the run ended with the window open (`restartPending`), or at once with `--notify` if it was closed, which runs `notify-last` once the new shell, which owns the notification daemon, is up.
- File names: the default is `engine.default_name()`; the GUI sends `-o <dir>/<name>.tar.gz` (`info.dataDir`/exports for rclone). `archive.write` puts `manifest.json` first, so `archive.peek()` can recognise renamed exports from the file's head (`wizard.candidates`). Older exports only match by the default-name `remote.PATTERN`. The rclone folder lists every `*.tar.gz` (`remote.REMOTE_NAME`), newest by ModTime.
- Menu rows run `omarchy-shell shell summon io.github.smolamsk.sync '{"tab":"…"}'`, and the launcher runs `… toggle …`. `service_start` refreshes our menu block when `entries()` changes, but only if the block is already there.
- QML lint: `qmllint -I <dir with qs → /usr/share/omarchy/shell> -I /usr/lib/qt6/qml SyncWindow.qml Service.qml`. Warnings like "not found on type QObject" (Style.spacing/font) and the QVariantMap → QVariantHash `environment` warning are expected.

## Contracts

- **Consent.** The plugin writes only its own files on its own: the launcher and its state marker. The user's menu file changes only after the notification click or `menu install`. An import changes nothing before the plan has been shown and confirmed (or `--yes` given).
- **Everything replaced is backed up first** (`Backup.save` in `Files.write`, `ShellSection._write`, `_write_menu`). Any new writer must call `ctx.backup.save(path)` before writing.
- **Only what the user changed travels**: `Files.add` drops files identical to `/etc/skel` (`keep_skel=True` overrides). Links are followed, and links leading outside HOME are dropped.
- **Machine-specific things stay behind**: `monitors.lua` and `input.lua` (unless `--include-hardware` or the same host), VM toggles (`VM_TOGGLES`) when either side is a VM, hardware toggle flags (`HARDWARE_FLAG`), Try Omarchy menu rows (`TRY_OMARCHY`), and `packages.DENY`.
- **Secrets never appear in plain text**: `shell.json` values whose key matches `SECRET_KEY` are blanked in the plain part and stored (by JSON path) in the sealed `values.json`. Any other file `scan.findings` flags is added to `ctx.secret_files` and sealed with the secrets (when a passphrase was given) or left out. Secrets must stay last in `EXPORT_ORDER` for this. Never log file contents; the report names files, lines and kinds only. Prefer false alarms to misses when changing `scan.py`.
- **An export is untrusted input** (anyone can craft one). Every path from it goes through `common.safe_relpath` and `model.may_write` (`SECTION_ROOTS`, `SECRET_ROOTS`, `NEVER`) before `Files` writes it. Plugin entries go through `sections._plain_plugin`, toggle flags through `FLAG_NAME` (omarchy-toggle joins them into a path it deletes), theme and background names through plain-name checks. Anything new that turns export data into a path or an argument needs the same treatment.
- Paths into the source home are rewritten to the target home (`common.rewrite_home`) in every text file and in menu entries. Between CPUs, absolute paths that name the source CPU (`aarch64`/`arm64` ↔ `x86_64`/`amd64`/`x64`, `common.ARCH_SWAP`) are rewritten too (`common.rewrite_arch`, via `move_arch`), but only when the path doesn't exist here and its counterpart does, or lies under a tool folder that the import or the user installs (`ARCH_MANAGED`: mise installs, rustup toolchains). Paths still missing are left alone and named in the report.
- Section titles must not contain commas: `gum choose --selected` splits on them.
- Bump `common.FORMAT` only for an incompatible archive change; `archive.extract` refuses newer formats.

## Development

- Checks: `bash -n bin/omarchy-sync`, `python3 -I -m py_compile bin/omarchy_sync/*.py`, `omarchy plugin validate "$PWD"`, `omarchy-preflight`.
- `OMARCHY_SYNC_PLATFORM=x86` pretends to be another platform (`common.platform`), e.g. for `import --dry-run`.
- Safe to try on this machine: `export -o <dir>` (it only reads, except that it creates `~/.config/omarchy-sync/apps.list`) and `import FILE --dry-run`. A real import changes the desktop; `undo` reverts its files.
