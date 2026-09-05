# Repository Guidelines

`switch-rom-renamer` renames Nintendo Switch ROM dumps (`.nsp` / `.xci` / `.nsz` / `.xcz`) into a canonical filename built from ground truth read out of each file's own metadata via `hactool` — never guessed from the filename or an external titledb. Windows-first, run with `uv`.

## Project Structure & Module Organization

- `rename_roms.py` — the main renamer script (container parsing, CNMT/NACP parsing, hactool invocation, verify-cache, dry-run/apply/undo).
- `cleanup_switch_roms.py` — auxiliary script to remove duplicate DLC, keep only the latest update, and recycle-bin older files.
- `test_rename_roms.py` — `unittest` suite for the parsers and pipelines (uses stubs, no hactool/keys/ROMs required).
- `pyproject.toml` + `uv.lock` — dependency management (`send2trash`; `[tool.uv] package = false`).
- Runtime/local (do not commit): `hactool.exe`, `.keys/`, `rename_roms.cache.json`, `rename_roms.log`, `rename_roms.undo`, `cleanup_roms.log`.

## Build, Test, and Development Commands

Requires [uv](https://docs.astral.sh/uv/) (manages the Python 3.10+ interpreter/venv).

Run the renamer (dry-run is the default — modifies nothing):

```cmd
uv run rename_roms.py "Z:/Games/Systems/Nintendo Switch/roms"
uv run rename_roms.py "Z:/Games/Systems/Nintendo Switch/roms" --apply
uv run rename_roms.py "Z:/Games/Systems/Nintendo Switch/roms" --undo
uv run rename_roms.py "Z:/Games/Systems/Nintendo Switch/roms" --full-scan
```

Point at hactool/keys explicitly when not using the auto-detected defaults:

```cmd
uv run rename_roms.py "Z:/.../roms" --hactool ".\hactool.exe" --keys ".\.keys\prod.keys" --apply
```

Run the cleanup script:

```cmd
python cleanup_switch_roms.py "K:\Games\Systems\Nintendo Switch\roms" --dry-run
```

Run the tests:

```cmd
uv run python -m unittest test_rename_roms -v
```

## Coding Style & Naming Conventions

- Python 3.10+ (`requires-python = ">=3.10"`), 4-space indentation, PEP 8.
- Single-file scripts with a module docstring header and `#!/usr/bin/env python3` shebang; `snake_case` throughout.
- Prefer reading ground truth from file metadata over trusting or parsing filenames — the core design principle; keep new logic consistent with it.

## Testing Guidelines

- Framework: `unittest` (`test_rename_roms.py`). Tests cover the PFS0/HFS0 container parser, raw CNMT/NACP parsers, content-type classification, canonical filename building for all four categories, the id/version + name-resolution pipelines, and the verify-cache — all against stubs.
- No `hactool.exe`, real keys, or real ROM files are required to run the suite. Run `uv run python -m unittest test_rename_roms -v` before committing.

## Commit & Pull Request Guidelines

- Keep commits focused; describe behavior changes and why. Always dry-run before `--apply` when validating a change against a real library.
- Never add `hactool.exe`, keys, cache/log/undo files, or ROM paths to a commit.

## Security & Configuration Tips

- **`.keys/` (your dumped `prod.keys`/`keys.txt`) and `hactool.exe` are required to run against real files but must never be committed to any repository, even a private one** — they are secret and legally sensitive emulation/encryption keys. `.gitignore` already excludes them; do not remove those entries.
- Keys are resolved in order: `keys.txt`/`prod.keys` next to the script, then `.keys/keys.txt`/`.keys/prod.keys`, then the `FALLBACK_KEYS_PATH` constant, or an explicit `--keys PATH`.
- Windows path quoting: don't end a quoted path with a trailing backslash before the closing quote (`".\.keys\"`); drop it or point `--keys` at the actual key file.
