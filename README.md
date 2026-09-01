# switch-rom-renamer

Renames Nintendo Switch ROM dumps (`.nsp` / `.xci` / `.nsz` / `.xcz`) into a
canonical filename built entirely from ground truth read directly out of
each file's own metadata via [hactool](https://github.com/SciresM/hactool)
— never guessed from the filename and never looked up in an external
titledb:

```
BASE:    {title} [{title_id}] [BASE][v{version}].{ext}
UPDATE:  {title} [{title_id}] [UPDATE][v{version}].{ext}
DLC:     {title} [{dlc_name}] [{title_id}] [DLC][v{version}].{ext}
UNKNOWN: {title} [{title_id}] [UNKNOWN].{ext}
```

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
- Names can be flat-out missing or ugly. A titledb-based renamer that was
  run on this library earlier assigned wrong title IDs to hundreds of files
  (see above), couldn't match those wrong IDs in its own database, and fell
  back to the literal filename `Unrecognized`. Other files were left with
  dump-site slug names like `v-dispatch_hr_violations_pack_dlc`.

This tool sidesteps all of that by reading the ground truth out of the file
itself, rather than trusting (or trying to parse) the filename, and rather
than trusting a separate database that can itself be wrong or incomplete.
That includes content type: BASE/UPDATE/DLC is read from the CNMT's own
declared "content meta type" byte, not guessed from the title ID's suffix
(`000`/`800`/other) — a common convention, but still a guess about a
filename-adjacent ID rather than the file's own ground truth. Anything that
isn't Application/Patch/AddOnContent (system titles, deltas, etc.) is
classified `[UNKNOWN]` instead of being forced into the wrong bucket.

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
4. The raw CNMT header is parsed directly (title id + version + content
   meta type, a simple fixed binary layout) to get the ground truth.
   Content type comes straight from that content meta type byte:
   `0x80` (Application) → BASE, `0x81` (Patch) → UPDATE, `0x82`
   (AddOnContent) → DLC, anything else → UNKNOWN.

The script also always tries to recover each title's real game name (and,
for DLC, its own distinct name):

5. The CNMT's content entry table (also inside the raw `.cnmt`) is parsed
   to find the Control-type entry's NCA id, if the title has one.
6. That `<ncaid>.nca` is located inside the same container (same trick as
   step 2) and handed to `hactool` (`-t nca --romfsdir=...`) to decrypt its
   RomFS, which contains `control.nacp`.
7. The raw NACP title-name table is parsed directly to get the real name.

A title's shared `{title}` is resolved once per base title ID (trying the
BASE file first, since it's the most reliable source of a Control NCA, then
falling back to whichever other file of that title is processed first if
there's no BASE in the library) and reused across all of that title's
UPDATE/DLC files, regardless of which order files happen to be scanned in.

Grouping a title's BASE/UPDATE/DLC files together for that shared-name
lookup is *also* read from ground truth rather than guessed: Nintendo's
common convention is that BASE/UPDATE/DLC of one title all share the same
leading 13 hex digits of their title ID (BASE ends `000`, UPDATE `800`, DLC
`001`–`FFF`), but that's not guaranteed — confirmed in this exact library:
Capcom Arcade 2nd Stadium's DLC title IDs don't share the base game's ID
prefix at all. Guessing the base ID from a DLC's own ID would silently fail
to find that title's BASE record, meaning the shared name (and therefore
the rename) never reaches that DLC. Instead, a Patch/AddOnContent CNMT's
extended header carries its own `ApplicationId` field — the real base ID,
straight from Nintendo's metadata — and that's read instead; the ID-prefix
guess is kept only as a fallback for a CNMT too old/exotic to carry it.

DLC additionally gets its own `{dlc_name}` resolved from its own Control
NCA if it has one — most DLC doesn't, in which case the existing filename
text is reused instead (lightly cleaned up: underscores/dashes become
spaces, and an all-lowercase slug gets title-cased), rather than losing
whatever descriptive text was already there. If neither the DLC's own name
nor any old descriptive text is available, the DLC filename just omits the
` [{dlc_name}]` part.

Some publishers' DLC Control NCAs report a name that already starts with
the base title verbatim — confirmed in this exact library: Capcom Arcade
2nd Stadium's DLC each report a name like "Capcom Arcade 2nd Stadium 1943
Kai Midway Kaisen" (the base title *plus* the episode name, not just the
episode name alone). If `{dlc_name}` starts with the shared `{title}`, that
prefix is stripped back off before use, so the title isn't duplicated in
the final filename.

A resolved name **always** replaces whatever name text is currently in the
filename, even if that text didn't look like an obvious placeholder — the
real name read from the file itself is trusted over the filename.
Overwriting something that wasn't an obvious placeholder is logged as
`NAME CHANGED` for easy auditing.

## Requirements

- [uv](https://docs.astral.sh/uv/) (manages the Python 3.10+ interpreter/venv)
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
uv run rename_roms.py "Z:/Games/Systems/Nintendo Switch/roms"

# Apply the renames
uv run rename_roms.py "Z:/Games/Systems/Nintendo Switch/roms" --apply

# Undo the last --apply session
uv run rename_roms.py "Z:/Games/Systems/Nintendo Switch/roms" --undo

# Scan a folder flat instead of recursively into subfolders
uv run rename_roms.py "Z:/Games/Systems/Nintendo Switch/roms" --no-recursive

# Point at hactool/keys explicitly instead of relying on the defaults
uv run rename_roms.py "Z:/Games/Systems/Nintendo Switch/roms" ^
    --hactool ".\hactool.exe" --keys ".\.keys\prod.keys" --apply

# Disable the live progress bar (auto-disabled anyway when piped/redirected)
uv run rename_roms.py "Z:/Games/Systems/Nintendo Switch/roms" --no-progress

# Ignore the verify-cache and re-verify every file with hactool
uv run rename_roms.py "Z:/Games/Systems/Nintendo Switch/roms" --full-scan
```

### Verify-cache vs. `--full-scan`

Every time hactool confirms a file's real id/version/content-type, that
result is saved to `rename_roms.cache.json` alongside the file's size and
modified-time. On the next run, if a file's size and modified-time are
still exactly what's in the cache, it's trusted without calling hactool
on it again — scanning a library that's already fully renamed becomes a
fast, hactool-free pass instead of re-verifying every file from scratch.
Only files that are new, changed, or not yet in the cache pay the
hactool cost.

This is deliberately **not** the same as trusting a name just because it
*looks* canonical — a canonical-shaped filename can still carry a wrong
title (leftover from an old buggy pass, or a hand edit), and shape alone
can't tell the two apart. The cache only ever marks a file as trusted
after hactool itself said so, so it can't reintroduce the class of bug
this tool exists to catch. A cached BASE/UPDATE file can still donate its
title to a sibling that needs one (e.g. new DLC added later) without
itself being re-verified, and a title group where every file is a cache
hit costs nothing at all — no hactool calls for it whatsoever.

Pass `--full-scan` to ignore the cache and re-verify every file with
hactool regardless of past runs (the cache is still refreshed from the
results either way, so subsequent runs stay fast). The summary output
breaks out how many "Already correct" files were fast-skipped via the
cache vs. freshly verified.

A live progress bar — files/titles done, elapsed time, ETA — is shown for
each phase (scanning, resolving titles, resolving DLC names, renaming)
whenever stdout is a real terminal. It writes only to the console via
carriage-return redraws; `rename_roms.log` is completely unaffected by it.

Always dry-run before `--apply`. Renames are logged to `rename_roms.log`
and the last `--apply` session's rename map is written to
`rename_roms.undo` so it can be reverted with `--undo` — but only the most
recent session is kept, so undo before running `--apply` again if you want
to keep that option open. Every run also updates `rename_roms.cache.json`
(see [Verify-cache vs. `--full-scan`](#verify-cache-vs---full-scan) below)
— safe to delete any time, it just means the next run re-verifies
everything with hactool once to rebuild it.

**Windows path quoting note:** don't end a quoted path argument with a
trailing backslash before the closing quote (e.g. `".\.keys\"`) — depending
on your shell this can be parsed as an escaped quote character instead of
closing the string. Either drop the trailing backslash (`".\.keys"`) or
point `--keys` at the actual key *file*, not the folder.

### Output summary

- **Fixed** — file renamed into the canonical format.
- **Already correct** — filename already matched the canonical format,
  left untouched.
- **Names resolved** — how many distinct titles got a real shared `{title}`
  recovered from a Control NCA (informational; a title only needs to
  resolve once, reused across all of its UPDATE/DLC files).
- **DLC names resolved** — how many DLC files additionally got their own
  distinct `{dlc_name}` recovered from their own Control NCA.
- **Unreadable** — hactool couldn't produce a `.cnmt` for this file (wrong
  master key for that title's key generation, corrupt file, or bad
  `--hactool`/`--keys` path).
- **Errors** — anything else unexpected (e.g. the target filename already
  exists).

Name resolution is always best-effort: if a title has no Control NCA
anywhere in the library, or hactool fails on it, the file falls back to a
lightly-cleaned-up version of its existing name text, and this has no
effect on Fixed/Unreadable/Errors or the circuit breaker below.

If 15 files in a row all fail the same way, the script stops early with an
`ABORTING` message instead of grinding through the whole library — that
many identical failures in a row almost always means `--hactool`/`--keys`
is misconfigured, not that 15 files are individually broken. Fix the setup
issue and re-run.

## Testing

```
uv run python -m unittest test_rename_roms -v
```

The test suite covers the PFS0/HFS0 container parser, the raw CNMT parser
(including its content meta type byte and content entry table), the raw
NACP name parser, content-type-from-meta-type classification, the
canonical filename-building logic for all four categories (BASE/UPDATE/
DLC/UNKNOWN), the full id/version and name-resolution pipelines (including
per-DLC own-name resolution), and the verify-cache (fingerprint/lookup
validation, cache hits and misses, `--full-scan`, and cross-file name
donation from a cached BASE/UPDATE to a sibling that still needs a name)
against stubs in place of hactool — none of it requires hactool.exe, real
keys, or real ROM files.

## Supported formats

`.nsp`, `.xci`, `.nsz`, `.xcz` — DLC and multi-title cartridges are
supported the same way as base games/updates.

## ROM Cleanup Script

A new script `cleanup_switch_roms.py` is available to clean up your ROM collection by:
- Removing duplicate DLC files (based on file hash)
- Keeping only the latest update for each game
- Moving older files to the recycle bin

Usage:
```
python cleanup_switch_roms.py "K:\Games\Systems\Nintendo Switch\roms" [--dry-run]
```
