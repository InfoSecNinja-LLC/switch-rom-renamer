#!/usr/bin/env python3
"""
rename_roms.py
==============
Renames Nintendo Switch ROM files into a canonical filename built entirely
from ground truth read out of each file's own metadata via hactool -- never
guessed from the filename, never looked up in an external titledb:

    BASE:    {title} [{title_id}] [BASE][v{version}].{ext}
    UPDATE:  {title} [{title_id}] [UPDATE][v{version}].{ext}
    DLC:     {title} [{dlc_name}] [{title_id}] [DLC][v{version}].{ext}
    UNKNOWN: {title} [{title_id}] [UNKNOWN].{ext}

Why not trust the filename?
  - Human version tags like "v1.0.5" are a developer-chosen display string,
    not derivable from any database -- confirmed empirically that two
    different titles' first (and only) update were tagged "v1.0.1" and
    "v1.0.10" and BOTH were really version 65536.
  - Filenames can just be wrong. Example found in this exact library:
    "Skautfold Bloody Pack [UPD][010074701C2AE000][v1.0.2].nsp" claims
    titleid 010074701C2AE000, but the file's real CNMT titleid is
    010015301AEA6800 -- a completely different title.
  - Names can be flat-out missing or ugly. An earlier titledb-based renamer
    assigned (often wrong) title ids, couldn't match those wrong ids in its
    own database, and fell back to the literal string "Unrecognized" (or
    left slug-style dump names like "v-dispatch_hr_violations_pack_dlc"
    untouched).

Why content type is read from the CNMT, not guessed from the title id:
  Nintendo's NUT/eShop convention (id ending in 000 = Base, 800 = Update,
  else = DLC) is a common heuristic, but it's still a guess about the
  FILENAME's id, and title ids can be wrong (see above). The CNMT itself
  carries a "content meta type" byte that says outright what a piece of
  content is (0x80 = Application/BASE, 0x81 = Patch/UPDATE,
  0x82 = AddOnContent/DLC). Reading that byte is ground truth, the same
  philosophy as everything else here. Anything else (system titles, deltas,
  etc.) is classified [UNKNOWN] rather than guessed into the wrong bucket.

How the real id/version/content-type are extracted (no full-file
extraction needed):
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
  4. Parse the raw CNMT header ourselves (title id + version + content
     meta type -- a simple, well-documented fixed binary layout) to get
     the ground truth.

How a title's BASE/UPDATE/DLC files are grouped together for shared name
resolution: NOT by guessing that a DLC's own title id shares the base
game's id prefix (Nintendo's common convention -- BASE ends "000", UPDATE
"800", DLC "001".."FFF", all otherwise identical -- but NOT a guarantee;
confirmed empirically against a real title in this library: Capcom Arcade
2nd Stadium's DLC ids don't share the base game's id prefix at all). For
UPDATE/DLC, the CNMT's own extended header (right after its fixed 0x20-byte
header) carries an 8-byte ApplicationId field -- the real base id, straight
from Nintendo's own metadata -- and that's read instead. The id-prefix
guess is kept only as a fallback for the rare CNMT that's too
old/exotic to carry that field.

How the real game name(s) are extracted (only when needed):
  5. Parse the CNMT's content entry table (also in the raw .cnmt, right
     after its header) to find the Control-type entry's NCA id, if any.
  6. Locate that "<ncaid>.nca" inside the same container (same trick as
     step 2) and run hactool (-t nca --romfsdir=...) to dump its RomFS,
     which contains "control.nacp".
  7. Parse the raw NACP title-name table ourselves (again a simple, fixed
     binary layout) to get the real game (or DLC) name.

  A title's main {title} is resolved once per base title id (trying the
  BASE file first, since it's the most reliable source of a Control NCA,
  then falling back to whichever other file of that title is processed
  first if there's no BASE in the library) and reused across all of that
  title's UPDATE/DLC files. DLC additionally gets its own {dlc_name}
  resolved from its OWN Control NCA if it has one -- most DLC doesn't, in
  which case the existing filename text is reused (lightly cleaned up:
  underscores/dashes become spaces) rather than lost. A resolved name
  ALWAYS replaces whatever name text is currently in the filename, even if
  that text didn't look like an obvious placeholder -- overwriting
  something that wasn't an obvious placeholder is logged as "NAME CHANGED"
  for easy auditing.

  Some publishers' DLC Control NCAs report a name that already starts with
  the base title verbatim (confirmed in this exact library: Capcom Arcade
  2nd Stadium's DLC each report a name like "Capcom Arcade 2nd Stadium
  1943 Kai Midway Kaisen" -- the base title PLUS the episode name, not
  just the episode name on its own). Using that raw text as {dlc_name}
  would duplicate the title in the final filename, so the shared {title}
  is stripped back off the front of a resolved/fallback dlc_name before
  it's used (see strip_leading_title).

.nsz/.xcz (nsz-compressed) files ARE supported. nsz only compresses the
big Program/Data NCA(s) into ".ncz" entries; the small Meta ("*.cnmt.nca")
and Control ("*.nca") entries we actually read are always left uncompressed
inside the container, so no decompression is needed -- confirmed against
real .nsz/.xcz files in this library (they sit right alongside the .ncz
entries, untouched).

Windows filename compatibility: title/dlc_name text is sanitized -- ":" is
replaced with " -", the other Windows-forbidden characters (<>"/\\|?*  and
control characters) are stripped, "[" and "]" are stripped (so a name can
never be mistaken for -- or corrupt -- our own [tag] delimiters), and
whitespace is collapsed.

A live progress bar (files/titles done, elapsed/ETA) is shown for each
phase whenever stdout is a real terminal -- automatically absent if output
is redirected/piped, or forced off with --no-progress. It never touches
rename_roms.log, which stays exactly as before.

By default, a file hactool already confirmed correct on some PAST run
(tracked in rename_roms.cache.json, keyed to that file's size+mtime) is
trusted without calling hactool on it again -- see the "Verify-cache"
paragraph in process_dirs. This is NOT the same as trusting a name just
because it LOOKS canonical: a canonical-shaped filename can still carry a
wrong title (e.g. leftover from an old buggy pass, or hand-edited), and
this cache never marks a file as verified without hactool itself having
said so first. Pass --full-scan to ignore the cache and re-verify every
file with hactool regardless of past runs (the cache is still refreshed
from the results either way).

Usage:
	python rename_roms.py <dir> [<dir> ...]              # Dry-run
	python rename_roms.py <dir> --apply                  # Apply renames
	python rename_roms.py <dir> --undo                   # Undo last --apply
	python rename_roms.py <dir> --hactool PATH            # hactool.exe location
	python rename_roms.py <dir> --keys PATH               # prod.keys/keys.txt location
	python rename_roms.py <dir> --no-progress             # Disable the progress bar
	python rename_roms.py <dir> --full-scan               # Ignore the verify-cache, re-check everything

Outputs (stored alongside this script):
	rename_roms.log          Session log
	rename_roms.undo         Undo map from last --apply
	rename_roms.cache.json   Verify-cache (path -> hactool-confirmed id/version/
	                         content-type, keyed to that file's size+mtime) --
	                         see the "Verify-cache" paragraph in process_dirs.

Tests: see test_rename_roms.py (python -m unittest test_rename_roms -v).
"""

import re
import os
import sys
import json
import time
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
# and Control ("*.nca") entries we actually read are always left
# uncompressed, so the exact same PFS0/HFS0 parsing used for .nsp/.xci
# works unchanged.
XCI_HFS0_OFFSET      = 0xF000       # fixed offset of the root HFS0 on all standard XCI
HEADER_READ_SIZE     = 512 * 1024   # generous prefix read to cover container header + string table

TITLEID_RE     = re.compile(r"\[([0-9A-Fa-f]{16})\]")
# Matches an existing version tag, whether real numeric ([v65536]) or a
# human display version ([v1.0.5]) -- with any leading whitespace, so it
# can be cleanly removed when computing a fallback title from an old name.
VERSION_TAG_RE = re.compile(r"\s*\[v\d+(?:\.\d+)*\]", re.IGNORECASE)
# Same idea for the ID tag, including leading whitespace.
ID_TAG_RE      = re.compile(r"\s*\[[0-9A-Fa-f]{16}\]")
# Recognizes our OWN canonical filename tail exactly:
#   [dlc_name] [title_id] [BASE|UPDATE|DLC|UNKNOWN][vN]
# with the dlc_name bracket optional (DLC only). This is the ONLY way we
# treat a "[dlc_name]"-shaped bracket group as an already-resolved dlc_name
# to recover, rather than discarding it as noise -- it must sit directly
# between the title and a REAL 16-hex id tag immediately followed by one
# of our own known content-type tags. A generic bracketed tag from some
# other naming convention (e.g. a legacy "[UPD]" scene tag, which has no
# real id tag anywhere near it) will never match this and falls back to
# being discarded as before. Without this distinction, re-running the
# script over an already-canonical DLC filename would silently drop its
# [dlc_name] as if it were just another tag to discard -- a real
# regression seen in this exact library.
CANONICAL_TAIL_RE = re.compile(
	r"^(?P<name>.*?)"
	r"(?:\s*\[(?P<dlc_name>[^\[\]]+)\])?"
	r"\s*\[[0-9A-Fa-f]{16}\]"
	r"\s*\[(?:BASE|UPDATE|DLC|UNKNOWN)\](?:\[v\d+(?:\.\d+)*\])?"
	r"\s*$"
)

# CNMT content entry "content type" byte values (NcmContentType) -- what
# KIND of data a given NCA inside the container holds.
CONTENT_TYPE_META    = 0
CONTENT_TYPE_PROGRAM = 1
CONTENT_TYPE_DATA    = 2
CONTENT_TYPE_CONTROL = 3

# CNMT header "content meta type" byte values (NcmContentMetaType) -- what
# KIND of TITLE this CNMT describes as a whole. This is the ground-truth
# source for BASE/UPDATE/DLC classification (see content_type_from_meta_type).
CONTENT_META_TYPE_APPLICATION    = 0x80  # BASE
CONTENT_META_TYPE_PATCH          = 0x81  # UPDATE
CONTENT_META_TYPE_ADD_ON_CONTENT = 0x82  # DLC

# NACP (control.nacp) title-name table layout.
NACP_LANGUAGE_ENTRY_SIZE = 0x300
NACP_NAME_SIZE           = 0x200
NACP_LANGUAGE_COUNT      = 16
# Prefer AmericanEnglish/BritishEnglish/Japanese, then whatever's non-empty.
NACP_PREFERRED_LANGUAGE_ORDER = [0, 1, 2] + list(range(3, NACP_LANGUAGE_COUNT))

# Matches a leading "name" that's a known placeholder left by some other
# tool when it couldn't identify the title (seen in this exact library as
# both "Unrecognized" and "Unrecognized - Unrecognized").
PLACEHOLDER_NAME_RE = re.compile(r"^unrecognized(\s*-\s*unrecognized)?$", re.IGNORECASE)

# Characters Windows forbids in filenames, plus control characters.
_WINDOWS_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ContentType(Enum):
	BASE    = "BASE"
	UPDATE  = "UPDATE"
	DLC     = "DLC"
	UNKNOWN = "UNKNOWN"


class HactoolError(Exception):
	"""Raised whenever we can't determine a file's real title id/version."""


log = logging.getLogger(__name__)

# ── Progress reporting ──────────────────────────────────────────────────────────

class ProgressLine:
	"""
	A single self-clearing progress line, redrawn in place with '\\r' as
	process_dirs works through each phase (id/version scan, name
	resolution, apply). Reused across phases via reset() rather than one
	instance per phase, so ProgressAwareStreamHandler below only ever
	needs to track one "currently drawn" line to clear.

	Silently does nothing if stdout isn't a live terminal (default,
	auto-detected via isatty()) -- piping output to a file or capturing it
	in a test therefore behaves exactly as if no progress line existed at
	all, byte for byte.
	"""

	def __init__(self, enabled: bool | None = None):
		self.enabled = sys.stdout.isatty() if enabled is None else enabled
		self.total = 0
		self.current = 0
		self.label = ""
		self._start = time.monotonic()
		self._last_width = 0

	def reset(self, total: int, label: str = "") -> None:
		self.total = total
		self.current = 0
		self.label = label
		self._start = time.monotonic()

	def update(self, current: int, detail: str = "") -> None:
		self.current = current
		if not self.enabled or self.total <= 0:
			return
		elapsed = time.monotonic() - self._start
		rate = self.current / elapsed if elapsed > 0 else 0
		eta = (self.total - self.current) / rate if rate > 0 else 0
		pct = 100 * self.current / self.total
		bar_width = 24
		filled = min(bar_width, int(bar_width * self.current / self.total))
		bar = "#" * filled + "-" * (bar_width - filled)
		text = f"  [{bar}] {self.current}/{self.total} ({pct:5.1f}%)  elapsed {elapsed:4.0f}s  eta {eta:4.0f}s"
		if self.label:
			text = f"{self.label}: {text}"
		if detail:
			text += f"  {detail}"
		# Truncate (rather than wrap) to a sane console width so a long
		# filename in `detail` can't push the line onto a second row,
		# which '\r' can't clear.
		text = text[:200]
		pad = max(0, self._last_width - len(text))
		sys.stdout.write("\r" + text + (" " * pad))
		sys.stdout.flush()
		self._last_width = len(text)

	def clear(self) -> None:
		if self.enabled and self._last_width:
			sys.stdout.write("\r" + (" " * self._last_width) + "\r")
			sys.stdout.flush()
			self._last_width = 0


class ProgressAwareStreamHandler(logging.StreamHandler):
	"""A normal console log handler that clears the shared ProgressLine
	before printing each log record, so a log message never ends up with
	leftover progress-bar text stuck to the end of it. The progress line
	itself isn't redrawn here -- it naturally reappears on the next
	progress update, which is frequent enough that the gap isn't
	noticeable."""

	def __init__(self, progress: ProgressLine, stream=None):
		super().__init__(stream)
		self._progress = progress

	def emit(self, record: logging.LogRecord) -> None:
		self._progress.clear()
		super().emit(record)

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


def find_entry_exact(entries: list[dict], name: str) -> dict | None:
	name_lower = name.lower()
	for e in entries:
		if e["name"].lower() == name_lower:
			return e
	return None


def _locate_container_entries(rom_path: Path) -> tuple[int, list[dict]]:
	"""Shared PFS0/HFS0 traversal used by both the Meta and Control NCA
	lookups. Returns (data_base_offset, entries) for the innermost
	partition that actually holds NCA files: the root PFS0 for .nsp/.nsz,
	or the "secure" HFS0 sub-partition for .xci/.xcz."""
	suffix = rom_path.suffix.lower()

	with open(rom_path, "rb") as f:
		if suffix in (".nsp", ".nsz"):
			f.seek(0)
			prefix = f.read(HEADER_READ_SIZE)
			header_size, entries = parse_pfs0(prefix)
			return header_size, entries

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
			return data_base, sec_entries

		else:
			raise HactoolError(f"unsupported container extension: {suffix}")


def locate_meta_nca(rom_path: Path) -> tuple[int, int]:
	"""
	Return (absolute_offset, size) of the "*.cnmt.nca" (Meta content) entry
	inside the given .nsp/.xci, WITHOUT extracting the rest of the container.
	"""
	data_base, entries = _locate_container_entries(rom_path)
	entry = find_entry(entries, ".cnmt.nca")
	return data_base + entry["offset"], entry["size"]


def locate_nca_by_id(rom_path: Path, nca_id_hex: str) -> tuple[int, int] | None:
	"""
	Return (absolute_offset, size) of "<nca_id_hex>.nca" inside the given
	rom, or None if it isn't present (e.g. most DLC don't carry their own
	Control NCA). Never raises -- absence is a normal, expected outcome.
	"""
	try:
		data_base, entries = _locate_container_entries(rom_path)
	except HactoolError:
		return None
	entry = find_entry_exact(entries, f"{nca_id_hex}.nca")
	if entry is None:
		return None
	return data_base + entry["offset"], entry["size"]


def parse_cnmt(data: bytes) -> tuple[str, int, int]:
	"""
	Parse the raw CNMT binary header (same fixed layout used by hactool,
	nut, and every other Switch homebrew tool):
	  0x00  8 bytes  title id, little-endian
	  0x08  4 bytes  title version, little-endian uint32
	  0x0C  1 byte   content meta type (ground truth for BASE/UPDATE/DLC --
	                 see CONTENT_META_TYPE_* / content_type_from_meta_type)
	  ...
	Returns (title_id as 16-char uppercase hex, version as int,
	content meta type as int).
	"""
	if len(data) < 0x0D:
		raise HactoolError(f"CNMT data too short ({len(data)} bytes)")
	title_id_int = int.from_bytes(data[0:8], "little")
	version = int.from_bytes(data[8:12], "little")
	meta_type = data[0x0C]
	return f"{title_id_int:016X}", version, meta_type


def parse_cnmt_content_entries(data: bytes) -> list[dict]:
	"""
	Parse the CNMT's content entry table, which follows the fixed 0x20-byte
	header + a variable-length extended header (whose size is itself given
	in the fixed header, so we don't need to know its internal layout):
	  0x0E  2 bytes  extended header size
	  0x10  2 bytes  content entry count
	  0x12  2 bytes  content meta count
	Each content entry is 0x38 bytes:
	  0x00  0x20 bytes  SHA-256 hash of the NCA
	  0x20  0x10 bytes  NCA id (== the NCA's filename, as hex)
	  0x30  6 bytes     size, little-endian
	  0x36  1 byte      content type (0=Meta,1=Program,2=Data,3=Control,...)
	  0x37  1 byte      id offset
	Returns [{"nca_id_hex", "size", "content_type"}, ...].
	"""
	if len(data) < 0x20:
		raise HactoolError(f"CNMT data too short for header ({len(data)} bytes)")
	ext_header_size, content_count, _meta_count = struct.unpack_from("<HHH", data, 0x0E)
	entries_start = 0x20 + ext_header_size
	entries = []
	for i in range(content_count):
		base = entries_start + i * 0x38
		if base + 0x38 > len(data):
			raise HactoolError("CNMT content entries extend past available data")
		nca_id = data[base + 0x20:base + 0x30]
		size = int.from_bytes(data[base + 0x30:base + 0x36], "little")
		content_type = data[base + 0x36]
		entries.append({
			"nca_id_hex": nca_id.hex(),
			"size": size,
			"content_type": content_type,
		})
	return entries


def parse_cnmt_application_id(data: bytes) -> int | None:
	"""
	For a Patch (UPDATE) or AddOnContent (DLC) CNMT, the extended header --
	right after the fixed 0x20-byte header, same place parse_cnmt_content_entries
	reads ext_header_size from -- begins with an 8-byte ApplicationId field:
	the REAL base/application title id this content belongs to, straight
	from Nintendo's own metadata. This is ground truth, unlike guessing the
	base id from the content's own title id (see base_id_from_title_id) --
	confirmed empirically against a real title in this library where it
	matters: Capcom Arcade 2nd Stadium's DLC title ids don't share the
	base game's id prefix at all (base ends in "...B4000", its DLC ids
	start "...B5xxx"/"...B3xxx"), so the guess can't find them, but this
	field correctly points every one of them back at "...B4000".
	Returns None if the CNMT is an Application/BASE (no such field) or is
	too short to contain one -- never raises.
	"""
	if len(data) < 0x20:
		return None
	ext_header_size, _content_count, _meta_count = struct.unpack_from("<HHH", data, 0x0E)
	if ext_header_size < 8 or 0x20 + 8 > len(data):
		return None
	return int.from_bytes(data[0x20:0x28], "little")

# ── hactool invocation (the only step that needs real decryption) ─────────────

def _run_hactool(hactool_path: Path, keys_path: Path, nca_file: Path, out_dir: Path, dir_flag: str) -> subprocess.CompletedProcess:
	"""Returns the CompletedProcess even on success, so the caller can
	surface hactool's own diagnostic text if the output dir still ends up
	empty (some misconfigurations, like -k pointing at a folder, make
	hactool print an error but still exit 0)."""
	out_dir.mkdir(parents=True, exist_ok=True)
	cmd = [
		str(hactool_path),
		"-t", "nca",
		"-k", str(keys_path),
		"--disablekeywarns",
		f"--{dir_flag}={out_dir}",
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


def run_hactool_section0(hactool_path: Path, keys_path: Path, nca_file: Path, out_dir: Path) -> subprocess.CompletedProcess:
	"""Decrypts a Meta NCA's Section0 (a small PFS0 wrapping the raw .cnmt)."""
	return _run_hactool(hactool_path, keys_path, nca_file, out_dir, "section0dir")


def run_hactool_romfs(hactool_path: Path, keys_path: Path, nca_file: Path, out_dir: Path) -> subprocess.CompletedProcess:
	"""Decrypts a Control NCA's RomFS (contains control.nacp + icons)."""
	return _run_hactool(hactool_path, keys_path, nca_file, out_dir, "romfsdir")


def _copy_range(src_path: Path, offset: int, size: int, dst_path: Path) -> None:
	with open(src_path, "rb") as src, open(dst_path, "wb") as dst:
		src.seek(offset)
		remaining = size
		while remaining > 0:
			chunk = src.read(min(1024 * 1024, remaining))
			if not chunk:
				raise HactoolError("unexpected EOF reading NCA from source file")
			dst.write(chunk)
			remaining -= len(chunk)


def extract_cnmt_bytes(rom_path: Path, hactool_path: Path, keys_path: Path) -> bytes:
	"""Locates the Meta NCA, hands it to hactool to decrypt Section0, and
	returns the raw .cnmt bytes."""
	offset, size = locate_meta_nca(rom_path)

	with tempfile.TemporaryDirectory(prefix="rr_") as tmpdir:
		tmp = Path(tmpdir)
		meta_nca_path = tmp / "meta.nca"
		_copy_range(rom_path, offset, size, meta_nca_path)

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

		return cnmt_files[0].read_bytes()


def extract_real_id_version(
	rom_path:    Path,
	hactool_path: Path,
	keys_path:   Path,
) -> tuple[str, int, int]:
	"""
	Full pipeline: locate the Meta NCA inside rom_path without extracting
	anything else, hand it to hactool to decrypt just its Section0, then
	parse the raw CNMT ourselves. Returns (title_id, version, meta_type).
	"""
	raw = extract_cnmt_bytes(rom_path, hactool_path, keys_path)
	return parse_cnmt(raw)


def extract_content_entries(rom_path: Path, hactool_path: Path, keys_path: Path) -> list[dict]:
	"""Re-extracts the CNMT and returns its content entry table (see
	parse_cnmt_content_entries). Used only when resolving a game name."""
	raw = extract_cnmt_bytes(rom_path, hactool_path, keys_path)
	return parse_cnmt_content_entries(raw)


def base_id_from_title_id(title_id: str) -> str:
	"""Guessed base/application id from a content's OWN title id, assuming
	Nintendo's common convention that BASE/UPDATE/DLC of one title all
	share the same leading 13 hex digits (BASE ends "000", UPDATE "800",
	DLC "001".."FFF"). Reliable for UPDATE (Nintendo enforces this bit
	pattern at the OS level), but NOT guaranteed for DLC -- see
	extract_real_base_id, which reads the real id out of the file's own
	metadata instead of relying on this guess wherever possible."""
	return title_id[:-3] + "000"


def extract_real_base_id(
	rom_path:     Path,
	hactool_path: Path,
	keys_path:    Path,
	title_id:     str,
	meta_type:    int,
) -> str:
	"""
	Ground-truth base/application title id for grouping a BASE/UPDATE/DLC
	trio's files together (see parse_cnmt_application_id for why this
	can't just be guessed from a DLC's own title id). A BASE file's own
	title id already IS the base id. Otherwise, re-extract the CNMT and
	read its extended header's ApplicationId field; only if that's
	unavailable (content too old/exotic to carry one, or hactool fails)
	fall back to the guess in base_id_from_title_id -- best-effort, never
	raises, so one file's CNMT hiccup can't break the whole run.
	"""
	if meta_type == CONTENT_META_TYPE_APPLICATION:
		return title_id
	try:
		raw = extract_cnmt_bytes(rom_path, hactool_path, keys_path)
		application_id = parse_cnmt_application_id(raw)
	except Exception:
		application_id = None
	if application_id is not None:
		return f"{application_id:016X}"
	return base_id_from_title_id(title_id)

# ── Game name resolution (best-effort -- never raises) ─────────────────────────

def parse_nacp_title_name(data: bytes) -> str | None:
	"""
	Parse the NACP title-name table: 16 fixed-size language entries
	(0x300 bytes each: 0x200-byte null-terminated name + 0x100-byte
	publisher), starting at offset 0. Returns the first non-empty name in
	NACP_PREFERRED_LANGUAGE_ORDER, or None if every entry is empty/missing.
	"""
	for lang_idx in NACP_PREFERRED_LANGUAGE_ORDER:
		offset = lang_idx * NACP_LANGUAGE_ENTRY_SIZE
		if offset + NACP_NAME_SIZE > len(data):
			continue
		raw_name = data[offset:offset + NACP_NAME_SIZE]
		name = raw_name.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
		if name:
			return name
	return None


def sanitize_name(name: str) -> str:
	"""Make a title/dlc name safe to use in a Windows filename, and safe to
	embed inside our own [tag] bracket delimiters -- "[" and "]" are
	stripped so a name can never be mistaken for, or prematurely close,
	the [title_id]/[BASE|UPDATE|DLC][vN] tags that follow it."""
	name = name.replace(":", " -")
	name = _WINDOWS_FORBIDDEN_RE.sub("", name)
	name = name.replace("[", "").replace("]", "")
	name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
	return name


def extract_title_name(
	rom_path:      Path,
	hactool_path:  Path,
	keys_path:     Path,
	content_entries: list[dict],
) -> str | None:
	"""
	Best-effort: find a Control-type content entry, decrypt its RomFS via
	hactool, and parse control.nacp for the real name. Returns None (never
	raises) if there's no Control NCA, hactool fails, or no name can be
	parsed out -- any of which are normal, expected outcomes for plenty of
	titles (most DLC has no Control NCA at all).
	"""
	control_entries = [e for e in content_entries if e["content_type"] == CONTENT_TYPE_CONTROL]
	if not control_entries:
		return None

	for centry in control_entries:
		loc = locate_nca_by_id(rom_path, centry["nca_id_hex"])
		if loc is None:
			continue
		offset, size = loc
		try:
			with tempfile.TemporaryDirectory(prefix="rr_ctrl_") as tmpdir:
				tmp = Path(tmpdir)
				control_nca_path = tmp / "control.nca"
				_copy_range(rom_path, offset, size, control_nca_path)

				romfs_dir = tmp / "romfs"
				run_hactool_romfs(hactool_path, keys_path, control_nca_path, romfs_dir)

				nacp_files = sorted(romfs_dir.glob("*.nacp"))
				if not nacp_files:
					continue
				name = parse_nacp_title_name(nacp_files[0].read_bytes())
				if name:
					return sanitize_name(name)
		except Exception:
			continue

	return None

# ── Content type + filename building (pure, no I/O) ────────────────────────────

def content_type_from_meta_type(meta_type: int) -> ContentType:
	"""Ground-truth classification from the CNMT's own declared content
	meta type -- never guessed from the title id or filename."""
	if meta_type == CONTENT_META_TYPE_APPLICATION:
		return ContentType.BASE
	if meta_type == CONTENT_META_TYPE_PATCH:
		return ContentType.UPDATE
	if meta_type == CONTENT_META_TYPE_ADD_ON_CONTENT:
		return ContentType.DLC
	return ContentType.UNKNOWN


def humanize_fallback_text(text: str) -> str:
	"""Light, safe cleanup applied to existing filename text when it's
	being reused as a fallback title/dlc_name because nothing could be
	resolved from the file itself: separators become spaces and an
	all-lowercase/snake_case string gets title-cased. Deliberately does
	NOT try to guess and strip prefixes/suffixes (e.g. a source-site slug)
	-- too unreliable to do safely across an arbitrary library, so the
	result may still need a manual touch-up for badly-named dumps."""
	text = re.sub(r"[_\-]+", " ", text)
	text = re.sub(r"\s+", " ", text).strip()
	if text and text == text.lower():
		text = text.title()
	return text


# Separators left dangling once a leading title has been stripped off a
# dlc_name (e.g. "Title: Episode" or "Title - Episode" -> "Episode").
_DANGLING_SEPARATOR_RE = re.compile(r"^[\s:\-\u2013\u2014]+")


def strip_leading_title(text: str, title: str) -> str:
	"""If dlc_name text begins with the shared {title} as a literal prefix,
	strip that prefix (plus any leftover separator) off before it's used --
	otherwise the title ends up duplicated in the final filename. Repeats
	until no leading copy remains, since some old filenames in this exact
	library carry TWO copies (one bare, tacked on by an earlier ID/version-
	only pass of this script; one baked into the DLC's own display name,
	e.g. Capcom's own convention of naming each Arcade Stadium DLC
	"{title}: {episode}") -- a single pass would leave one copy behind.
	Returns "" (not the original text) if nothing is left after stripping,
	i.e. dlc_name IS (repetitions of) the title with nothing else added.
	"""
	if not text or not title:
		return text
	text = text.strip()
	title_low = title.strip().lower()
	while text.lower().startswith(title_low):
		text = _DANGLING_SEPARATOR_RE.sub("", text[len(title_low):])
	return text


def build_new_stem(
	title:        str,
	title_id:     str,
	version:      int,
	content_type: ContentType,
	dlc_name:     str | None = None,
) -> str:
	"""Build the canonical filename stem (no extension):
	  BASE:    {title} [{title_id}] [BASE][v{version}]
	  UPDATE:  {title} [{title_id}] [UPDATE][v{version}]
	  DLC:     {title} [{dlc_name}] [{title_id}] [DLC][v{version}]
	  UNKNOWN: {title} [{title_id}] [UNKNOWN]
	title/dlc_name are sanitized for Windows filename compatibility (which,
	notably, strips any "[" or "]" out of the name text itself -- so
	dlc_name's own brackets can never be confused with the tags around it).
	"""
	title = sanitize_name(title)

	if content_type == ContentType.BASE:
		return f"{title} [{title_id}] [BASE][v{version}]"
	if content_type == ContentType.UPDATE:
		return f"{title} [{title_id}] [UPDATE][v{version}]"
	if content_type == ContentType.DLC:
		dlc_part = f" [{sanitize_name(dlc_name)}]" if dlc_name else ""
		return f"{title}{dlc_part} [{title_id}] [DLC][v{version}]"
	return f"{title} [{title_id}] [UNKNOWN]"

# ── Verify-cache (skip re-verifying files hactool already confirmed) ──────────

def cache_fingerprint(path: Path, title_id: str, version: int, content_type: ContentType, base_id: str) -> dict:
	"""
	A cache entry recording that hactool itself already confirmed `path`
	(at its CURRENT size/mtime) has this real id/version/content-type/
	base-id -- ground truth from a past run, not a guess about the
	filename. size+mtime (not just the path) are the key: if the file at
	this path is ever replaced by different bytes, its mtime and/or size
	will practically always change too, which invalidates the entry (see
	cache_lookup) rather than silently trusting stale/wrong data.
	"""
	st = path.stat()
	return {
		"size": st.st_size, "mtime": st.st_mtime,
		"title_id": title_id, "version": version,
		"content_type": content_type.value, "base_id": base_id,
	}


def cache_lookup(cache: dict, path: Path) -> dict | None:
	"""Returns the cached fingerprint for `path` if one exists, its
	size/mtime still match the file on disk right now, AND its fields are
	well-formed, else None. Never raises -- a missing file, an unreadable
	file, or a malformed/incompatible entry (this cache file is plain
	JSON someone could hand-edit or half-write) are all just a miss,
	falling through to the normal hactool-verified path rather than
	crashing the whole run over one bad entry."""
	entry = cache.get(str(path))
	if not isinstance(entry, dict):
		return None
	try:
		st = path.stat()
	except OSError:
		return None
	if entry.get("size") != st.st_size or entry.get("mtime") != st.st_mtime:
		return None
	if not isinstance(entry.get("title_id"), str) or not isinstance(entry.get("base_id"), str):
		return None
	if not isinstance(entry.get("version"), int) or isinstance(entry.get("version"), bool):
		return None
	try:
		ContentType(entry.get("content_type"))
	except ValueError:
		return None
	return entry

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
	progress:     ProgressLine | None = None,
	full_scan:    bool = False,
	cache:        dict | None = None,
) -> tuple[dict, list]:
	"""
	Three phases, so a title's real name is resolved (at most once per
	title) and reaches ALL of that title's files -- regardless of which
	order the filesystem happens to hand them to us in:

	  1. Gather: extract the real id/version/content-type for every file,
	  plus a GROUND-TRUTH base/application id used to group a title's
	  BASE/UPDATE/DLC files together for name resolution in phase 2 (read
	  from the CNMT's own ApplicationId field for UPDATE/DLC, not guessed
	  from the content's own title id -- some real titles' DLC ids don't
	  share the base game's id prefix at all, see extract_real_base_id).
	  (This is the required step -- failures here count toward the
	  circuit breaker.)

	  2. Resolve names:
	    a. Shared per-title name: for each distinct base title id, try
	    the BASE file first (most reliable source of a Control NCA); only
	    if a title has no BASE file in the library do we fall back to
	    trying whichever other file of that title comes first. Each base
	    id is attempted at most once per tier, even on failure, so one
	    DLC with no Control NCA doesn't cost a retry for every sibling --
	    and, importantly, doesn't prevent that title's BASE file
	    (processed later) from getting its own attempt.
	    b. DLC's own name: each DLC file additionally gets its own
	    resolution attempt against its own Control NCA, since a DLC's
	    name (e.g. "Champions' Ballad") is usually distinct from its base
	    game's name.
	  Best-effort throughout: never affects unreadable/errors/circuit
	  breaker.

	  3. Apply: rename every file using its own id/version/content-type,
	  the resolved (or, failing that, existing-text-derived) title, and
	  for DLC the resolved (or existing-text-derived) dlc_name. A
	  resolved name always replaces whatever name text is currently in
	  the filename -- overwriting something that wasn't an obvious
	  placeholder is logged as "NAME CHANGED" for easy auditing.

	Verify-cache (skipped when full_scan=True): `cache` maps a file's own
	path to the size/mtime/id/version/content-type/base-id hactool itself
	confirmed for it on some PAST run (see cache_fingerprint). Before
	Phase 1 touches hactool for a given file, cache_lookup checks for a
	matching entry -- an exact size+mtime match means the file hasn't
	changed since that past run actually verified it, so hactool would
	just re-confirm what's already known. This is NOT filename-shape
	trust (that was tried and rejected: a file can carry a fully
	canonical-LOOKING name while its title is still wrong -- e.g. left
	over from an old buggy pass, or a coincidence -- and shape alone can't
	tell the two apart). Every cache entry instead comes from hactool
	itself, so it can't mask a bad title the way shape-matching could.

	A cache hit still gets a `records` entry (marked skip_rename=True) so
	it can act as a Phase 2 name-resolution donor for a sibling that DOES
	need one (e.g. new DLC added next to an already-verified BASE) --
	using its cached id/base_id, no hactool call needed just for that.
	Phase 2 itself still only spends a hactool call on a given base_id
	group when at least one member of that group actually needs a rename;
	a fully cache-hit group costs nothing. Phase 3 skips skip_rename
	records outright (a cache hit is already known correct) and writes a
	fresh cache entry for every file it newly confirms correct (whether
	untouched or freshly renamed), so the next run benefits too. Pass
	full_scan=True to ignore the cache on lookup (verify everything with
	hactool regardless of past runs) -- the cache is still refreshed from
	the results either way.
	"""

	if cache is None:
		cache = {}

	stats = {
		"fixed": 0, "already_correct": 0, "fast_skipped": 0, "unreadable": 0,
		"errors": 0, "names_resolved": 0, "dlc_names_resolved": 0, "aborted_early": False,
	}
	undo_log: list[dict] = []
	consecutive_failures = 0
	last_error = ""
	name_cache: dict[str, str | None] = {}   # base title id -> resolved title (or None = tried, failed)
	records: list[dict] = []

	# --- Phase 1: gather id/version/content-type for every file ---
	# Globbed up front (cheap -- just a directory listing, no hactool)
	# purely so the progress bar below knows a total before the slow part
	# starts.
	dir_files: list[tuple[Path, list[Path]]] = []
	for rom_dir in rom_dirs:
		if not rom_dir.exists():
			log.warning("Directory not found, skipping: %s", rom_dir)
			continue
		glob_pattern = "**/*" if recursive else "*"
		all_files = sorted(p for p in rom_dir.glob(glob_pattern) if p.is_file())
		dir_files.append((rom_dir, [p for p in all_files if p.suffix.lower() in ROM_EXTENSIONS]))

	if progress:
		progress.reset(sum(len(files) for _rom_dir, files in dir_files), "Scanning")
	scanned = 0

	for rom_dir, files in dir_files:
		log.info("")
		log.info("=== %s ===", rom_dir)

		if not files:
			log.info("  (no ROM files found)")
			continue

		for f in files:
			cache_hit = None if full_scan else cache_lookup(cache, f)
			if cache_hit is not None:
				stats["already_correct"] += 1
				stats["fast_skipped"] += 1
				consecutive_failures = 0
				records.append({
					"path": f,
					"title_id": cache_hit["title_id"],
					"version": cache_hit["version"],
					"content_type": ContentType(cache_hit["content_type"]),
					"base_id": cache_hit["base_id"],
					"name_part": "", "is_placeholder": False,
					"existing_dlc_name": None, "dlc_own_name": None,
					"skip_rename": True,
				})
				scanned += 1
				if progress:
					progress.update(scanned, f.name)
				continue

			try:
				title_id, version, meta_type = extract_real_id_version(f, hactool_path, keys_path)
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

				content_type = content_type_from_meta_type(meta_type)
				base_id = extract_real_base_id(f, hactool_path, keys_path, title_id, meta_type)

				canonical_match = CANONICAL_TAIL_RE.match(f.stem)
				if canonical_match:
					name_part = canonical_match.group("name").strip()
					existing_dlc_name = canonical_match.group("dlc_name")
				else:
					stripped = VERSION_TAG_RE.sub("", ID_TAG_RE.sub("", f.stem))
					bracket_idx = stripped.find("[")
					name_part = (stripped[:bracket_idx] if bracket_idx != -1 else stripped).strip()
					existing_dlc_name = None
				is_placeholder = bool(PLACEHOLDER_NAME_RE.match(name_part))

				records.append({
					"path": f, "title_id": title_id, "version": version,
					"content_type": content_type, "base_id": base_id,
					"name_part": name_part, "is_placeholder": is_placeholder,
					"existing_dlc_name": existing_dlc_name, "dlc_own_name": None,
					"skip_rename": False,
				})

			scanned += 1
			if progress:
				progress.update(scanned, f.name)

			if consecutive_failures >= max_consecutive_failures:
				log.error("")
				log.error("ABORTING: %d files in a row all failed the same way -- this looks like "
						  "a setup problem, not %d unlucky files.", consecutive_failures, consecutive_failures)
				log.error("Last error: %s", last_error)
				log.error("Check --hactool and --keys point at the right FILES (not folders), "
						  "then re-run.")
				stats["aborted_early"] = True
				return stats, undo_log

	# --- Phase 2a: resolve each title's shared name, BASE files first ---
	# Only worth a hactool call for a base_id group that has at least one
	# member actually needing a rename decision -- a group made ENTIRELY
	# of cache hits (skip_rename) is already fully correct, so resolving
	# its name would be pure waste; it's never looked at in Phase 3.
	base_ids_needing_resolution = {rec["base_id"] for rec in records if not rec["skip_rename"]}
	if progress:
		progress.reset(len(base_ids_needing_resolution), "Resolving titles")
	titles_attempted = 0

	def try_resolve(rec: dict) -> None:
		nonlocal titles_attempted
		try:
			content_entries = extract_content_entries(rec["path"], hactool_path, keys_path)
			resolved = extract_title_name(rec["path"], hactool_path, keys_path, content_entries)
		except Exception:
			resolved = None
		name_cache[rec["base_id"]] = resolved
		if resolved:
			stats["names_resolved"] += 1
		titles_attempted += 1
		if progress:
			progress.update(titles_attempted, resolved or rec["path"].name)

	for rec in records:
		if (rec["content_type"] == ContentType.BASE
				and rec["base_id"] in base_ids_needing_resolution
				and rec["base_id"] not in name_cache):
			try_resolve(rec)
	for rec in records:
		if rec["base_id"] in base_ids_needing_resolution and rec["base_id"] not in name_cache:
			try_resolve(rec)

	# --- Phase 2b: resolve each DLC's own name (distinct from the shared title) ---
	# skip_rename (cache-hit) records are excluded -- they're already
	# known correct and Phase 3 never looks at them, so resolving their
	# own name would just be a wasted hactool call.
	dlc_records = [rec for rec in records if rec["content_type"] == ContentType.DLC and not rec["skip_rename"]]
	if progress:
		progress.reset(len(dlc_records), "Resolving DLC names")

	for i, rec in enumerate(dlc_records, 1):
		try:
			content_entries = extract_content_entries(rec["path"], hactool_path, keys_path)
			own_name = extract_title_name(rec["path"], hactool_path, keys_path, content_entries)
		except Exception:
			own_name = None
		rec["dlc_own_name"] = own_name
		if own_name:
			stats["dlc_names_resolved"] += 1
		if progress:
			progress.update(i, rec["path"].name)

	# --- Phase 3: apply renames ---
	if progress:
		progress.reset(len(records), "Renaming")

	for i, rec in enumerate(records, 1):
		f = rec["path"]
		if rec["skip_rename"]:
			# Cache hit from Phase 1 -- already known correct, and its
			# stats were already counted there. Nothing left to do.
			continue
		if progress:
			progress.update(i, f.name)
		title_id, version, content_type = rec["title_id"], rec["version"], rec["content_type"]
		resolved_name = name_cache.get(rec["base_id"])

		if resolved_name:
			title = resolved_name
		elif rec["name_part"]:
			title = humanize_fallback_text(rec["name_part"])
		else:
			title = "Unrecognized"

		dlc_name = None
		if content_type == ContentType.DLC:
			if rec["dlc_own_name"]:
				dlc_name = rec["dlc_own_name"]
			elif rec["existing_dlc_name"]:
				# Already had a [dlc_name] tag from a previous canonical
				# rename (recognized by CANONICAL_TAIL_RE) -- reuse it
				# verbatim rather than re-deriving it, since there's
				# nothing left to derive it FROM once this DLC's own
				# Control NCA lookup above comes back empty again.
				dlc_name = rec["existing_dlc_name"]
			elif resolved_name and rec["name_part"] and not rec["is_placeholder"]:
				# The shared title was resolved for real, so reusing the
				# old filename text as a *distinct* dlc_name is still
				# informative -- but only if that old text is actually
				# descriptive. If it's just a placeholder like
				# "Unrecognized", or nothing resolved at all (`title`
				# already fell back to this same text), keep dlc_name
				# empty rather than duplicating noise.
				dlc_name = humanize_fallback_text(rec["name_part"])

			# Some publishers' DLC Control NCAs (and some old dump
			# filenames) report/contain the shared title AS A PREFIX of
			# the dlc_name rather than just the distinct episode/pack name
			# on its own -- strip it back off so the title isn't
			# duplicated in the final filename (see strip_leading_title).
			if dlc_name and title:
				dlc_name = strip_leading_title(dlc_name, title)

			# Avoid "Title [Title]" when the own-name resolution just
			# reproduces the shared title verbatim (e.g. the rare case
			# where a DLC's own Control NCA is also what resolved the
			# shared title because no BASE file exists in the library) --
			# strip_leading_title already reduces this to "" above, but
			# guard the general case too (e.g. differing punctuation).
			if dlc_name and title and dlc_name.strip().lower() == title.strip().lower():
				dlc_name = None

		old_id_match = TITLEID_RE.search(f.stem)
		old_id = old_id_match.group(1).upper() if old_id_match else None

		new_stem = build_new_stem(title, title_id, version, content_type, dlc_name)
		new_path = f.parent / f"{new_stem}{f.suffix}"

		if new_path == f:
			stats["already_correct"] += 1
			cache[str(f)] = cache_fingerprint(f, title_id, version, content_type, rec["base_id"])
			continue

		# The file is about to change (or, in dry-run, has just been
		# PROVEN not to match hactool's ground truth right now) -- any
		# stale cache entry for its current path can no longer be trusted.
		cache.pop(str(f), None)

		if old_id and old_id != title_id:
			log.warning("  ID MISMATCH  filename said [%s], real title id is [%s] -- correcting  %s",
						old_id, title_id, f.name)

		if (resolved_name and rec["name_part"] and not rec["is_placeholder"]
				and rec["name_part"].strip().lower() != resolved_name.strip().lower()):
			log.warning("  NAME CHANGED  \"%s\" -> \"%s\"  (%s)",
						rec["name_part"], resolved_name, f.name)

		if new_path.exists():
			log.warning("  SKIP  target exists: %s", new_path.name)
			stats["errors"] += 1
			continue

		log.info("  [%s]  %s  ->  %s", content_type.value, f.name, new_path.name)

		if not dry_run:
			f.rename(new_path)
			undo_log.append({"from": str(new_path), "to": str(f)})
			cache[str(new_path)] = cache_fingerprint(new_path, title_id, version, content_type, rec["base_id"])

		stats["fixed"] += 1

	if progress:
		progress.clear()
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


def load_cache(cache_file: Path) -> dict:
	"""Loads the verify-cache written by a past run (see cache_fingerprint).
	Missing or unreadable/corrupt is just an empty cache -- everything
	gets verified with hactool as normal, same as a first-ever run."""
	if not cache_file.exists():
		return {}
	try:
		return json.loads(cache_file.read_text(encoding="utf-8"))
	except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
		log.warning("Ignoring unreadable verify-cache at %s (%s) -- starting fresh", cache_file, exc)
		return {}


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
		description="Rename Switch ROMs into {title} [{id}] [BASE|UPDATE|DLC|UNKNOWN][vVERSION], "
					"all read from each file's own metadata via hactool -- never guessed "
					"from the filename.",
		formatter_class=argparse.RawDescriptionHelpFormatter,
		epilog="""
Examples:
  Dry-run a folder:
	python rename_roms.py --root "Z:/Games/Systems/Nintendo Switch/roms"

  Apply renames:
	python rename_roms.py --root "Z:/Games/Systems/Nintendo Switch/roms" --apply

  Undo last apply:
	python rename_roms.py --root "Z:/Games/Systems/Nintendo Switch/roms" --undo
""",
	)
	parser.add_argument("--root", nargs="+", required=True, metavar="PATH", help="ROM folder(s) to process")
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
	parser.add_argument(
		"--no-progress", dest="progress", action="store_false",
		help="Don't show the live progress bar (auto-disabled anyway when output isn't a terminal)",
	)
	parser.set_defaults(progress=True)
	parser.add_argument(
		"--full-scan", action="store_true",
		help="Verify every file with hactool, ignoring rename_roms.cache.json (default: "
			 "trust a file already confirmed correct by a past run, as long as its size "
			 "and modified-time haven't changed since -- see cache_lookup in rename_roms.py)",
	)
	args = parser.parse_args()

	rom_dirs   = [Path(d) for d in args.root]
	script_dir = Path(__file__).parent
	log_file   = script_dir / "rename_roms.log"
	undo_file  = script_dir / "rename_roms.undo"
	cache_file = script_dir / "rename_roms.cache.json"

	progress = ProgressLine(enabled=None if args.progress else False)

	logging.basicConfig(
		level=logging.INFO,
		format="%(asctime)s  %(levelname)-7s  %(message)s",
		datefmt="%H:%M:%S",
		handlers=[
			ProgressAwareStreamHandler(progress, sys.stdout),
			logging.FileHandler(log_file, encoding="utf-8"),
		],
	)

	session = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
	log.info("===========================================")
	log.info("  rename_roms.py v9 (hactool + name resolution + verify-cache)  |  session: %s", session)
	log.info("  Dirs : %s", ", ".join(str(d) for d in rom_dirs))
	log.info("  Mode : %s", "APPLY" if args.apply else "DRY-RUN")
	log.info("  Scan : %s", "FULL (--full-scan, ignoring verify-cache)" if args.full_scan else "FAST (trust the verify-cache)")
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
	cache = load_cache(cache_file)
	stats, undo_log = process_dirs(
		rom_dirs, dry_run=dry_run, recursive=args.recursive,
		hactool_path=hactool_path, keys_path=keys_path, progress=progress,
		full_scan=args.full_scan, cache=cache,
	)
	try:
		cache_file.write_text(json.dumps(cache, indent=2), encoding="utf-8")
	except OSError as exc:
		log.warning("Could not save verify-cache to %s (%s) -- next run will re-verify more than necessary", cache_file, exc)

	log.info("")
	log.info("--- Summary ---")
	log.info("  Fixed            : %d", stats["fixed"])
	log.info("  Already correct  : %d", stats["already_correct"])
	log.info("    fast-skipped   : %d  (verify-cache hit, no hactool call -- pass --full-scan to verify these too)", stats["fast_skipped"])
	log.info("  Names resolved   : %d", stats["names_resolved"])
	log.info("  DLC names resolved: %d", stats["dlc_names_resolved"])
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
