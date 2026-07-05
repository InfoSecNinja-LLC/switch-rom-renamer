#!/usr/bin/env python3
"""
rename_roms.py
==============
Renames Nintendo Switch ROM files with the REAL [titleid] and, for
updates/DLC, the REAL numeric [vVERSION] -- both read directly out of each
file's own CNMT metadata via hactool, never guessed from the filename.

Why not trust the filename?
  - Human version tags like "v1.0.5" are a developer-chosen display string,
    not derivable from any database -- confirmed empirically that two
    different titles' first (and only) update were tagged "v1.0.1" and
    "v1.0.10" and BOTH were really version 65536.
  - Filenames can just be wrong. Example found in this exact library:
    "Skautfold Bloody Pack [UPD][010074701C2AE000][v1.0.2].nsp" claims
    titleid 010074701C2AE000, but the file's real CNMT titleid is
    010015301AEA6800 -- a completely different title.

How content type is determined:
  NUT/eShop convention -- ends in 000 = Base, 800 = Update, else = DLC.
  This script derives it from the REAL extracted title id, not from
  [UPD]/[DLC] filename tags.

How the real id/version are extracted (no full-file extraction needed):
  1. Parse the outer container directory ourselves in pure Python --
     PFS0 for .nsp/.nsz, nested HFS0 (root -> "secure" partition) for
     .xci/.xcz. This is NOT encrypted, so no keys/hactool needed for
     this step.
  2. Find the "*.cnmt.nca" (Meta) entry and read ONLY those bytes
     (always small, a few KB) directly out of the source file -- no need
     to extract the multi-GB Program/Data content at all.
  3. Write just that small Meta NCA to a temp file and run hactool
     (-t nca --section0dir=...) to decrypt its Section0, which is a small
     PFS0 wrapping the raw .cnmt.
  4. Parse the raw CNMT header ourselves (title id + version -- a simple,
     well-documented fixed binary layout) to get the ground truth.

.nsz/.xcz (nsz-compressed) files ARE supported. nsz only compresses the
big Program/Data NCA(s) into ".ncz" entries; the small Meta ("*.cnmt.nca")
entry we actually read is always left uncompressed inside the container,
so no decompression is needed -- confirmed against real .nsz/.xcz files
in this library (the Meta NCA sits right alongside the .ncz entries,
untouched).

Usage:
	python rename_roms.py <dir> [<dir> ...]              # Dry-run
	python rename_roms.py <dir> --apply                  # Apply renames
	python rename_roms.py <dir> --undo                   # Undo last --apply
	python rename_roms.py <dir> --hactool PATH            # hactool.exe location
	python rename_roms.py <dir> --keys PATH               # prod.keys/keys.txt location

Outputs (stored alongside this script):
	rename_roms.log     Session log
	rename_roms.undo    Undo map from last --apply

Tests: see test_rename_roms.py (python -m unittest test_rename_roms -v).
"""

import re
import os
import sys
import json
import struct
import logging
import argparse
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime
from enum import Enum

# ── Defaults ────────────────────────────────────────────────────────────────────

ROM_EXTENSIONS       = {".nsp", ".xci", ".nsz", ".xcz"}
# .nsz/.xcz (nsz-compressed) ARE supported: nsz only compresses the big
# Program/Data content into ".ncz" entries -- the small Meta ("*.cnmt.nca")
# entry we actually read is always left uncompressed, so the exact same
# PFS0/HFS0 parsing used for .nsp/.xci works unchanged.
XCI_HFS0_OFFSET      = 0xF000       # fixed offset of the root HFS0 on all standard XCI
HEADER_READ_SIZE     = 512 * 1024   # generous prefix read to cover container header + string table

TITLEID_RE     = re.compile(r"\[([0-9A-Fa-f]{16})\]")
# Matches an existing version tag, whether real numeric ([v65536]) or a
# human display version ([v1.0.5]) -- with any leading whitespace, so it
# can be cleanly removed and replaced.
VERSION_TAG_RE = re.compile(r"\s*\[v\d+(?:\.\d+)*\]", re.IGNORECASE)
# Same idea for the ID tag, including leading whitespace.
ID_TAG_RE      = re.compile(r"\s*\[[0-9A-Fa-f]{16}\]")


class ContentType(Enum):
	BASE   = "BASE"
	UPDATE = "UPDATE"
	DLC    = "DLC"


class HactoolError(Exception):
	"""Raised whenever we can't determine a file's real title id/version."""


log = logging.getLogger(__name__)

# ── Pure container parsing (no crypto, no hactool needed) ─────────────────────

def parse_pfs0(data: bytes) -> tuple[int, list[dict]]:
	"""Parse a PFS0 (NSP container) header/string-table prefix.
	Returns (header_size, [{"name","offset","size"}, ...])."""
	if data[0:4] != b"PFS0":
		raise HactoolError(f"not a PFS0 container (magic={data[0:4]!r})")
	file_count, string_table_size, _junk = struct.unpack_from("<III", data, 4)
	entries_end = 0x10 + file_count * 0x18
	header_size = entries_end + string_table_size
	if len(data) < header_size:
		raise HactoolError("PFS0 header/string table longer than the bytes we read")
	string_table = data[entries_end:header_size]
	entries = []
	for i in range(file_count):
		base = 0x10 + i * 0x18
		offset, size, name_off, _junk2 = struct.unpack_from("<QQII", data, base)
		end = string_table.find(b"\x00", name_off)
		name = string_table[name_off:end if end >= 0 else len(string_table)].decode("utf-8", "replace")
		entries.append({"name": name, "offset": offset, "size": size})
	return header_size, entries


def parse_hfs0(data: bytes) -> tuple[int, list[dict]]:
	"""Parse an HFS0 (XCI partition) header/string-table prefix.
	Returns (header_size, [{"name","offset","size"}, ...])."""
	if data[0:4] != b"HFS0":
		raise HactoolError(f"not an HFS0 partition (magic={data[0:4]!r})")
	file_count, string_table_size, _junk = struct.unpack_from("<III", data, 4)
	entries_end = 0x10 + file_count * 0x40
	header_size = entries_end + string_table_size
	if len(data) < header_size:
		raise HactoolError("HFS0 header/string table longer than the bytes we read")
	string_table = data[entries_end:header_size]
	entries = []
	for i in range(file_count):
		base = 0x10 + i * 0x40
		offset, size, name_off, _hashed_size = struct.unpack_from("<QQII", data, base)
		end = string_table.find(b"\x00", name_off)
		name = string_table[name_off:end if end >= 0 else len(string_table)].decode("utf-8", "replace")
		entries.append({"name": name, "offset": offset, "size": size})
	return header_size, entries


def find_entry(entries: list[dict], name_suffix: str) -> dict:
	matches = [e for e in entries if e["name"].lower().endswith(name_suffix)]
	if not matches:
		raise HactoolError(f"no entry ending in '{name_suffix}' found "
							f"(entries: {[e['name'] for e in entries]})")
	return matches[0]


def locate_meta_nca(rom_path: Path) -> tuple[int, int]:
	"""
	Return (absolute_offset, size) of the "*.cnmt.nca" (Meta content) entry
	inside the given .nsp/.xci, WITHOUT extracting the rest of the container.
	"""
	suffix = rom_path.suffix.lower()

	with open(rom_path, "rb") as f:
		if suffix in (".nsp", ".nsz"):
			f.seek(0)
			prefix = f.read(HEADER_READ_SIZE)
			header_size, entries = parse_pfs0(prefix)
			data_base = 0 + header_size
			entry = find_entry(entries, ".cnmt.nca")
			return data_base + entry["offset"], entry["size"]

		elif suffix in (".xci", ".xcz"):
			f.seek(XCI_HFS0_OFFSET)
			prefix = f.read(HEADER_READ_SIZE)
			root_header_size, root_entries = parse_hfs0(prefix)
			secure = find_entry(root_entries, "secure")
			secure_base = XCI_HFS0_OFFSET + root_header_size + secure["offset"]

			f.seek(secure_base)
			prefix2 = f.read(HEADER_READ_SIZE)
			sec_header_size, sec_entries = parse_hfs0(prefix2)
			data_base = secure_base + sec_header_size
			entry = find_entry(sec_entries, ".cnmt.nca")
			return data_base + entry["offset"], entry["size"]

		else:
			raise HactoolError(f"unsupported container extension: {suffix}")


def parse_cnmt(data: bytes) -> tuple[str, int]:
	"""
	Parse the raw CNMT binary header (same fixed layout used by hactool,
	nut, and every other Switch homebrew tool):
	  0x00  8 bytes  title id, little-endian
	  0x08  4 bytes  title version, little-endian uint32
	  0x0C  1 byte   content meta type
	  ...
	Returns (title_id as 16-char uppercase hex, version as int).
	"""
	if len(data) < 0x0D:
		raise HactoolError(f"CNMT data too short ({len(data)} bytes)")
	title_id_int = int.from_bytes(data[0:8], "little")
	version = int.from_bytes(data[8:12], "little")
	return f"{title_id_int:016X}", version

# ── hactool invocation (the only step that needs real decryption) ─────────────

def run_hactool_section0(hactool_path: Path, keys_path: Path, nca_file: Path, out_dir: Path) -> subprocess.CompletedProcess:
	"""Returns the CompletedProcess even on success, so the caller can
	surface hactool's own diagnostic text if Section0 still ends up empty
	(some misconfigurations, like -k pointing at a folder, make hactool
	print an error but still exit 0)."""
	out_dir.mkdir(parents=True, exist_ok=True)
	cmd = [
		str(hactool_path),
		"-t", "nca",
		"-k", str(keys_path),
		"--disablekeywarns",
		f"--section0dir={out_dir}",
		str(nca_file),
	]
	try:
		result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
	except FileNotFoundError:
		raise HactoolError(f"hactool not found at {hactool_path}")
	except subprocess.TimeoutExpired:
		raise HactoolError("hactool timed out")

	if result.returncode != 0:
		msg = (result.stderr or result.stdout or "").strip()
		raise HactoolError(f"hactool exited {result.returncode}: {msg or '(no output)'}")

	return result


def extract_real_id_version(
	rom_path:    Path,
	hactool_path: Path,
	keys_path:   Path,
) -> tuple[str, int]:
	"""
	Full pipeline: locate the Meta NCA inside rom_path without extracting
	anything else, hand it to hactool to decrypt just its Section0, then
	parse the raw CNMT ourselves. Returns (title_id, version).
	"""
	offset, size = locate_meta_nca(rom_path)

	with tempfile.TemporaryDirectory(prefix="rr_") as tmpdir:
		tmp = Path(tmpdir)
		meta_nca_path = tmp / "meta.nca"
		with open(rom_path, "rb") as src, open(meta_nca_path, "wb") as dst:
			src.seek(offset)
			remaining = size
			while remaining > 0:
				chunk = src.read(min(1024 * 1024, remaining))
				if not chunk:
					raise HactoolError("unexpected EOF reading Meta NCA from source file")
				dst.write(chunk)
				remaining -= len(chunk)

		section0_dir = tmp / "section0"
		result = run_hactool_section0(hactool_path, keys_path, meta_nca_path, section0_dir)

		cnmt_files = sorted(section0_dir.glob("*.cnmt"))
		if not cnmt_files:
			detail = (result.stderr or result.stdout or "").strip()
			raise HactoolError(
				"hactool ran (exit 0) but produced no .cnmt file in Section0. "
				"Most common causes: --keys points at a FOLDER instead of the "
				"actual prod.keys/keys.txt file, or a missing master key for "
				"this title's key generation.\n"
				f"    hactool output: {detail or '(hactool printed nothing)'}"
			)

		return parse_cnmt(cnmt_files[0].read_bytes())

# ── Content type + renaming (pure, no I/O) ─────────────────────────────────────

def content_type_from_id(title_id: str) -> ContentType:
	if title_id.endswith("000"):
		return ContentType.BASE
	if title_id.endswith("800"):
		return ContentType.UPDATE
	return ContentType.DLC


def build_new_stem(old_stem: str, title_id: str, version: int, content_type: ContentType) -> str:
	"""Strip any existing ID/version tags and append the correct ones."""
	stem = ID_TAG_RE.sub("", old_stem)
	stem = VERSION_TAG_RE.sub("", stem)
	stem = re.sub(r"\s{2,}", " ", stem).strip()
	if content_type == ContentType.BASE:
		return f"{stem} [{title_id}]"
	return f"{stem} [v{version}] [{title_id}]"

# ── Core Rename Logic ──────────────────────────────────────────────────────────

# If this many files IN A ROW all fail the same way, it's almost certainly a
# setup problem (wrong --hactool/--keys path, bad keys file, etc.) rather
# than N unlucky files in a row -- stop early instead of spamming identical
# warnings across an entire library.
DEFAULT_MAX_CONSECUTIVE_FAILURES = 15


def process_dirs(
	rom_dirs:     list[Path],
	dry_run:      bool,
	recursive:    bool,
	hactool_path: Path,
	keys_path:    Path,
	max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
) -> tuple[dict, list]:

	stats = {"fixed": 0, "already_correct": 0, "unreadable": 0, "errors": 0, "aborted_early": False}
	undo_log: list[dict] = []
	consecutive_failures = 0
	last_error = ""

	for rom_dir in rom_dirs:
		if not rom_dir.exists():
			log.warning("Directory not found, skipping: %s", rom_dir)
			continue

		log.info("")
		log.info("=== %s ===", rom_dir)

		glob_pattern = "**/*" if recursive else "*"
		all_files = sorted(p for p in rom_dir.glob(glob_pattern) if p.is_file())
		files = [p for p in all_files if p.suffix.lower() in ROM_EXTENSIONS]

		if not files:
			log.info("  (no ROM files found)")
			continue

		for f in files:
			try:
				title_id, version = extract_real_id_version(f, hactool_path, keys_path)
			except HactoolError as exc:
				log.warning("  UNREADABLE  %s  (%s)", f.name, exc)
				stats["unreadable"] += 1
				consecutive_failures += 1
				last_error = str(exc)
			except Exception as exc:
				log.warning("  ERROR  %s  (%s)", f.name, exc)
				stats["errors"] += 1
				consecutive_failures += 1
				last_error = str(exc)
			else:
				consecutive_failures = 0

				content_type = content_type_from_id(title_id)
				old_id_match = TITLEID_RE.search(f.stem)
				old_id = old_id_match.group(1).upper() if old_id_match else None

				new_stem = build_new_stem(f.stem, title_id, version, content_type)
				new_path = f.parent / f"{new_stem}{f.suffix}"

				if new_path == f:
					stats["already_correct"] += 1
					continue

				if old_id and old_id != title_id:
					log.warning("  ID MISMATCH  filename said [%s], real title id is [%s] -- correcting  %s",
								old_id, title_id, f.name)

				if new_path.exists():
					log.warning("  SKIP  target exists: %s", new_path.name)
					stats["errors"] += 1
					continue

				log.info("  [%s]  %s  ->  %s", content_type.value, f.name, new_path.name)

				if not dry_run:
					f.rename(new_path)
					undo_log.append({"from": str(new_path), "to": str(f)})

				stats["fixed"] += 1

			if consecutive_failures >= max_consecutive_failures:
				log.error("")
				log.error("ABORTING: %d files in a row all failed the same way -- this looks like "
						  "a setup problem, not %d unlucky files.", consecutive_failures, consecutive_failures)
				log.error("Last error: %s", last_error)
				log.error("Check --hactool and --keys point at the right FILES (not folders), "
						  "then re-run.")
				stats["aborted_early"] = True
				return stats, undo_log

	return stats, undo_log

# ── Undo ───────────────────────────────────────────────────────────────────────

def do_undo(undo_file: Path) -> None:
	if not undo_file.exists():
		log.error("No undo manifest at %s", undo_file)
		sys.exit(1)
	manifest = json.loads(undo_file.read_text())
	entries  = manifest.get("renames", [])
	if not entries:
		log.info("Nothing to undo.")
		return
	log.info("Undoing %d rename(s) from session %s",
			 len(entries), manifest.get("session"))
	ok = err = 0
	for e in entries:
		src, dst = Path(e["from"]), Path(e["to"])
		if not src.exists():
			log.warning("  MISS  %s", src.name)
			err += 1
		elif dst.exists():
			log.warning("  SKIP  target exists: %s", dst.name)
			err += 1
		else:
			src.rename(dst)
			log.info("  UNDO  %s", dst.name)
			ok += 1
	log.info("Done: %d restored, %d skipped/errors", ok, err)
	undo_file.unlink(missing_ok=True)

# ── Entry Point ────────────────────────────────────────────────────────────────

# Fallback if no keys.txt/prod.keys is found next to this script -- adjust
# to wherever you keep your dumped keys.
FALLBACK_KEYS_PATH = Path("Z:/Games/Systems/Nintendo Switch/keys/prod.keys")


def default_keys_path(script_dir: Path) -> Path:
	for candidate in (
		script_dir / "keys.txt",
		script_dir / "prod.keys",
		script_dir / ".keys" / "keys.txt",
		script_dir / ".keys" / "prod.keys",
		FALLBACK_KEYS_PATH,
	):
		if candidate.exists():
			return candidate
	return script_dir / "keys.txt"


def main() -> None:
	parser = argparse.ArgumentParser(
		description="Rename Switch ROMs with the REAL [titleid]/[vVERSION], "
					"read from each file's own CNMT via hactool -- never guessed "
					"from the filename.",
		formatter_class=argparse.RawDescriptionHelpFormatter,
		epilog="""
Examples:
  Dry-run a folder:
	python rename_roms.py "Z:/Games/Systems/Nintendo Switch/roms"

  Apply renames:
	python rename_roms.py "Z:/Games/Systems/Nintendo Switch/roms" --apply

  Undo last apply:
	python rename_roms.py "Z:/Games/Systems/Nintendo Switch/roms" --undo
""",
	)
	parser.add_argument("dirs", nargs="+", metavar="DIR", help="ROM folder(s) to process")
	parser.add_argument("--apply", action="store_true", help="Apply renames (default is dry-run)")
	parser.add_argument("--undo", action="store_true", help="Undo last --apply session")
	parser.add_argument(
		"--no-recursive", dest="recursive", action="store_false",
		help="Scan each DIR flat (default: recursive into subdirectories)",
	)
	parser.set_defaults(recursive=True)
	parser.add_argument(
		"--hactool", type=Path, default=None, metavar="PATH",
		help="Path to hactool.exe (default: hactool.exe next to this script)",
	)
	parser.add_argument(
		"--keys", type=Path, default=None, metavar="PATH",
		help="Path to keys.txt/prod.keys (default: keys.txt or prod.keys next to this script)",
	)
	args = parser.parse_args()

	rom_dirs   = [Path(d) for d in args.dirs]
	script_dir = Path(__file__).parent
	log_file   = script_dir / "rename_roms.log"
	undo_file  = script_dir / "rename_roms.undo"

	logging.basicConfig(
		level=logging.INFO,
		format="%(asctime)s  %(levelname)-7s  %(message)s",
		datefmt="%H:%M:%S",
		handlers=[
			logging.StreamHandler(sys.stdout),
			logging.FileHandler(log_file, encoding="utf-8"),
		],
	)

	session = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
	log.info("===========================================")
	log.info("  rename_roms.py v6 (hactool)  |  session: %s", session)
	log.info("  Dirs : %s", ", ".join(str(d) for d in rom_dirs))
	log.info("  Mode : %s", "APPLY" if args.apply else "DRY-RUN")
	log.info("===========================================")

	if args.undo:
		do_undo(undo_file)
		return

	hactool_path = args.hactool or (script_dir / "hactool.exe")
	keys_path    = args.keys or default_keys_path(script_dir)

	if hactool_path.is_dir():
		log.error("--hactool points at a folder (%s), not hactool.exe itself.", hactool_path)
		sys.exit(1)
	if not hactool_path.exists():
		log.error("hactool not found at %s (pass --hactool PATH)", hactool_path)
		sys.exit(1)
	if not keys_path.exists():
		log.error("keys.txt/prod.keys not found at %s (pass --keys PATH)", keys_path)
		sys.exit(1)
	if keys_path.is_dir():
		log.error("--keys points at a folder (%s), not a key file.", keys_path)
		log.error("Point it at the actual file inside it, for example:")
		for candidate in ("prod.keys", "keys.txt"):
			if (keys_path / candidate).exists():
				log.error("  --keys \"%s\"", keys_path / candidate)
		sys.exit(1)

	log.info("  hactool: %s", hactool_path)
	log.info("  keys   : %s", keys_path)
	log.info("===========================================")

	dry_run = not args.apply
	stats, undo_log = process_dirs(
		rom_dirs, dry_run=dry_run, recursive=args.recursive,
		hactool_path=hactool_path, keys_path=keys_path,
	)

	log.info("")
	log.info("--- Summary ---")
	log.info("  Fixed            : %d", stats["fixed"])
	log.info("  Already correct  : %d", stats["already_correct"])
	log.info("  Unreadable       : %d", stats["unreadable"])
	log.info("  Errors           : %d", stats["errors"])
	if stats.get("aborted_early"):
		log.info("")
		log.info("  STOPPED EARLY -- see the ABORTING message above and fix the setup issue.")

	if dry_run:
		log.info("")
		log.info("  DRY-RUN -- no files were modified.")
		log.info("  Re-run with --apply to execute renames.")
	else:
		if undo_log:
			manifest = {"session": session, "renames": undo_log}
			undo_file.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
			log.info("  Undo file     -> %s", undo_file)
		log.info("  Log           -> %s", log_file)

	log.info("===========================================")


if __name__ == "__main__":
	main()
