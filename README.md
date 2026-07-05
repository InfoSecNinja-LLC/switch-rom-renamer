# switch-rom-renamer

Renames Nintendo Switch ROM dumps (`.nsp` / `.xci` / `.nsz` / `.xcz`) so their
filenames carry the **real** `[titleid]` and, for updates/DLC, the **real**
numeric `[vVERSION]` — both read directly out of each file's own CNMT
metadata via [hactool](https://github.com/SciresM/hactool), never guessed
from the filename.

## Why

Filenames lie, or are just wrong:

- Human version tags like `[v1.0.5]` are a developer-chosen display string
  with no reliable relationship to Nintendo's internal numeric version.
  Two different titles' first (and only) update were tagged `v1.0.1` and
  `v1.0.10` in the wild, and both were really version `65536`.
- IDs can be flat-out mislabeled. One file in this exact library was named
  `Skautfold Bloody Pack [UPD][010074701C2AE000][v1.0.2].nsp`, but the
  file's real CNMT title ID is `010015301AEA6800` — a different title
  entirely.

This tool sidesteps all of that by reading the ground truth out of the file
itself, rather than trusting (or trying to parse) the filename.

## How it works

1. The outer container (PFS0 for `.nsp`/`.nsz`, nested HFS0 for `.xci`/`.xcz`)
   is parsed in pure Python — this is not encrypted, so no keys are needed
   for this step.
2. The small `*.cnmt.nca` (Meta content) entry is located and read directly
   out of the source file — never the multi-GB Program/Data content, so
   this works even on very large libraries without extracting anything.
   `.nsz`/`.xcz` files are supported the same way: nsz only compresses the
   big Program/Data NCA into `.ncz` entries and always leaves the Meta NCA
   uncompressed.
3. Just that small Meta NCA is handed to `hactool` (`-t nca
   --section0dir=...`) to decrypt its Section0, which is a small PFS0
   wrapping the raw `.cnmt`.
4. The raw CNMT header is parsed directly (title id + version, a simple
   fixed binary layout) to get the ground truth.
5. Content type (Base/Update/DLC) is derived from the real title ID's
   suffix (`000`/`800`/other), never from `[UPD]`/`[DLC]` filename tags.

## Requirements

- Python 3.10+
- [`hactool.exe`](https://github.com/SciresM/hactool) (or `hactool` on
  Linux/macOS)
- Your own dumped `prod.keys`/`keys.txt` (e.g. via
  [Lockpick_RCM](https://github.com/shchmue/Lockpick_RCM))

Neither hactool nor your keys are included in this repo — see
[Setup](#setup) below.

## Setup

1. Place `hactool.exe` in this folder (or pass `--hactool PATH`).
2. Place your keys file in one of these locations (checked in order), or
   pass `--keys PATH` explicitly:
   - `keys.txt` or `prod.keys` next to the script
   - `.keys/keys.txt` or `.keys/prod.keys`
   - the `FALLBACK_KEYS_PATH` constant at the top of `rename_roms.py`
     (adjust to wherever you keep your dumped keys)

**Never commit your keys or `hactool.exe` to any repository, even a
private one.** `.gitignore` already excludes them — don't remove those
entries.

## Usage

```
# Dry-run (default) -- shows what would change, modifies nothing
python rename_roms.py "Z:/Games/Systems/Nintendo Switch/roms"

# Apply the renames
python rename_roms.py "Z:/Games/Systems/Nintendo Switch/roms" --apply

# Undo the last --apply session
python rename_roms.py "Z:/Games/Systems/Nintendo Switch/roms" --undo

# Scan a folder flat instead of recursively into subfolders
python rename_roms.py "Z:/Games/Systems/Nintendo Switch/roms" --no-recursive

# Point at hactool/keys explicitly instead of relying on the defaults
python rename_roms.py "Z:/Games/Systems/Nintendo Switch/roms" ^
    --hactool ".\hactool.exe" --keys ".\.keys\prod.keys" --apply
```

Always dry-run before `--apply`. Renames are logged to `rename_roms.log`
and the last `--apply` session's rename map is written to
`rename_roms.undo` so it can be reverted with `--undo` — but only the most
recent session is kept, so undo before running `--apply` again if you want
to keep that option open.

**Windows path quoting note:** don't end a quoted path argument with a
trailing backslash before the closing quote (e.g. `".\.keys\"`) — depending
on your shell this can be parsed as an escaped quote character instead of
closing the string. Either drop the trailing backslash (`".\.keys"`) or
point `--keys` at the actual key *file*, not the folder.

### Output summary

- **Fixed** — file renamed with the corrected ID/version.
- **Already correct** — filename already matched the real ID/version, left
  untouched.
- **Unreadable** — hactool couldn't produce a `.cnmt` for this file (wrong
  master key for that title's key generation, corrupt file, or bad
  `--hactool`/`--keys` path).
- **Errors** — anything else unexpected (e.g. the target filename already
  exists).

If 15 files in a row all fail the same way, the script stops early with an
`ABORTING` message instead of grinding through the whole library — that
many identical failures in a row almost always means `--hactool`/`--keys`
is misconfigured, not that 15 files are individually broken. Fix the setup
issue and re-run.

## Testing

```
python -m unittest test_rename_roms -v
```

The test suite covers the PFS0/HFS0 container parser, the raw CNMT parser,
content-type/filename-rebuilding logic, and the full extraction pipeline
against a stub in place of hactool — none of it requires hactool.exe, real
keys, or real ROM files.

## Supported formats

`.nsp`, `.xci`, `.nsz`, `.xcz` — DLC and multi-title cartridges are
supported the same way as base games/updates.
