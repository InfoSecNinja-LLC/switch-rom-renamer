#!/usr/bin/env python3
"""
Unit tests for rename_roms.py.

Run with:  python -m unittest test_rename_roms -v

These tests do NOT require hactool.exe, keys, or any real ROM file --
they validate:
  - the pure PFS0/HFS0 container parser (against hand-built synthetic
    containers with known structure)
  - the raw CNMT parser, including its content meta type byte and content
    entry table (against hand-built synthetic CNMT blobs)
  - the raw NACP title-name parser (against hand-built synthetic NACP blobs)
  - content-type-from-meta-type and canonical filename-building logic (pure
    string ops), including the BASE/UPDATE/DLC/UNKNOWN templates
  - the end-to-end extract_real_id_version and extract_title_name
    pipelines, with hactool itself replaced by stub scripts (no real
    crypto -- just proves the orchestration/plumbing is correct: temp file
    creation, hactool invocation, output discovery, cleanup)
  - dry-run / apply / undo behavior on real (temporary) files on disk,
    including cross-file name-cache propagation (a BASE title's resolved
    name reaching its UPDATE/DLC siblings) and per-DLC own-name resolution

The container parser (parse_pfs0/parse_hfs0/locate_meta_nca) was also
independently cross-checked during development against real ROM files in
this library: the exact byte range it located for the Meta NCA was
extracted and handed to an unrelated, independent NCA/CNMT decryptor,
which decrypted it successfully and returned the same title id/version
already known from the filename -- for both a .nsp and a .xci sample.
That proves the offset/size math is correct; these synthetic tests below
are a faithful, hactool-free stand-in for everyday regression testing.
"""

import contextlib
import io
import json
import logging
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import rename_roms as rr


def build_pfs0(entries: list[tuple[str, bytes]]) -> bytes:
	"""Build a real, valid PFS0 blob from [(name, data), ...] for testing."""
	names = [n for n, _ in entries]
	string_table = b"\x00".join(n.encode() for n in names) + b"\x00"

	body = b"".join(data for _, data in entries)
	dir_entries = b""
	running_offset = 0
	name_offset = 0
	for name, data in entries:
		dir_entries += struct.pack("<QQII", running_offset, len(data), name_offset, 0)
		running_offset += len(data)
		name_offset += len(name) + 1

	header = b"PFS0" + struct.pack("<III", len(entries), len(string_table), 0)
	return header + dir_entries + string_table + body


def build_hfs0(entries: list[tuple[str, bytes]]) -> bytes:
	"""Build a real, valid HFS0 blob (same idea, 0x40-byte entries + hash)."""
	names = [n for n, _ in entries]
	string_table = b"\x00".join(n.encode() for n in names) + b"\x00"

	body = b"".join(data for _, data in entries)
	dir_entries = b""
	running_offset = 0
	name_offset = 0
	for name, data in entries:
		# offset, size, nameOffset, hashedSize, reserved(8), hash(32)
		dir_entries += struct.pack("<QQII", running_offset, len(data), name_offset, 0)
		dir_entries += b"\x00" * 8 + b"\x00" * 32
		running_offset += len(data)
		name_offset += len(name) + 1

	header = b"HFS0" + struct.pack("<III", len(entries), len(string_table), 0)
	return header + dir_entries + string_table + body


def build_cnmt(
	title_id_hex: str,
	version: int,
	title_type: int = 0x80,
	content_entries: list[tuple[str, int, int]] | None = None,
	ext_header_size: int = 0,
	application_id_hex: str | None = None,
) -> bytes:
	"""Build a synthetic raw CNMT blob: the fixed 0x20-byte header, then
	ext_header_size bytes of extended header, then a proper content entry
	table if content_entries is given -- a list of (nca_id_hex, size,
	content_type) tuples. title_type is the content meta type byte at
	0x0C (default 0x80 = Application/BASE). If application_id_hex is
	given, it's written as the first 8 bytes of the extended header (the
	real ApplicationId field on a Patch/AddOnContent CNMT); the rest of
	the extended header is zeroed either way."""
	tid_int = int(title_id_hex, 16)
	header = b""
	header += tid_int.to_bytes(8, "little")                     # 0x00 title id
	header += version.to_bytes(4, "little")                      # 0x08 version
	header += bytes([title_type])                                # 0x0C meta type
	header += b"\x00"                                            # 0x0D reserved
	header += ext_header_size.to_bytes(2, "little")               # 0x0E ext header size
	header += len(content_entries or []).to_bytes(2, "little")    # 0x10 content count
	header += (0).to_bytes(2, "little")                           # 0x12 meta count
	header += b"\x00" * (0x20 - len(header))                      # pad to fixed 0x20 header

	ext_header = bytearray(ext_header_size)
	if application_id_hex is not None:
		ext_header[0:8] = int(application_id_hex, 16).to_bytes(8, "little")
	ext_header = bytes(ext_header)

	body = b""
	for nca_id_hex, size, content_type in (content_entries or []):
		body += b"\x00" * 0x20                 # hash (unused by our parser)
		body += bytes.fromhex(nca_id_hex)        # 0x20 nca id (16 bytes)
		body += size.to_bytes(6, "little")       # 0x30 size
		body += bytes([content_type])            # 0x36 content type
		body += b"\x00"                          # 0x37 id offset

	return header + ext_header + body


def build_nacp(name: str, lang_index: int = 0) -> bytes:
	"""Build a minimal synthetic control.nacp blob with just one language
	entry populated -- enough for parse_nacp_title_name to find it."""
	data = bytearray(rr.NACP_LANGUAGE_ENTRY_SIZE * rr.NACP_LANGUAGE_COUNT)
	name_bytes = name.encode("utf-8")[:rr.NACP_NAME_SIZE - 1]
	offset = lang_index * rr.NACP_LANGUAGE_ENTRY_SIZE
	data[offset:offset + len(name_bytes)] = name_bytes
	return bytes(data)


class TestPfs0Parsing(unittest.TestCase):
	def test_basic_roundtrip(self):
		blob = build_pfs0([
			("game.cert", b"C" * 0x700),
			("game.tik", b"T" * 0x2c0),
			("aaaa.nca", b"N" * 1000),
			("bbbb.cnmt.nca", b"M" * 200),
		])
		header_size, entries = rr.parse_pfs0(blob)
		names = [e["name"] for e in entries]
		self.assertEqual(names, ["game.cert", "game.tik", "aaaa.nca", "bbbb.cnmt.nca"])
		# offsets are relative to header_size (data region start)
		self.assertEqual(entries[0]["offset"], 0)
		self.assertEqual(entries[1]["offset"], 0x700)
		self.assertEqual(entries[3]["size"], 200)

	def test_bad_magic_raises(self):
		with self.assertRaises(rr.HactoolError):
			rr.parse_pfs0(b"NOPE" + b"\x00" * 100)

	def test_find_entry(self):
		blob = build_pfs0([("x.nca", b"1"), ("y.cnmt.nca", b"22")])
		_, entries = rr.parse_pfs0(blob)
		found = rr.find_entry(entries, ".cnmt.nca")
		self.assertEqual(found["name"], "y.cnmt.nca")

	def test_find_entry_missing_raises(self):
		blob = build_pfs0([("x.nca", b"1")])
		_, entries = rr.parse_pfs0(blob)
		with self.assertRaises(rr.HactoolError):
			rr.find_entry(entries, ".cnmt.nca")


class TestHfs0Parsing(unittest.TestCase):
	def test_basic_roundtrip(self):
		blob = build_hfs0([
			("aaaa.nca", b"N" * 5000),
			("bbbb.cnmt.nca", b"M" * 300),
		])
		header_size, entries = rr.parse_hfs0(blob)
		self.assertEqual(entries[0]["name"], "aaaa.nca")
		self.assertEqual(entries[1]["name"], "bbbb.cnmt.nca")
		self.assertEqual(entries[1]["offset"], 5000)
		self.assertEqual(entries[1]["size"], 300)


class TestCnmtParsing(unittest.TestCase):
	def test_known_values(self):
		blob = build_cnmt("010059B017F9E800", 786432, title_type=rr.CONTENT_META_TYPE_PATCH)
		title_id, version, meta_type = rr.parse_cnmt(blob)
		self.assertEqual(title_id, "010059B017F9E800")
		self.assertEqual(version, 786432)
		self.assertEqual(meta_type, rr.CONTENT_META_TYPE_PATCH)

	def test_base_version_zero(self):
		blob = build_cnmt("0100EFD00A4FA000", 0, title_type=rr.CONTENT_META_TYPE_APPLICATION)
		title_id, version, meta_type = rr.parse_cnmt(blob)
		self.assertEqual(title_id, "0100EFD00A4FA000")
		self.assertEqual(version, 0)
		self.assertEqual(meta_type, rr.CONTENT_META_TYPE_APPLICATION)

	def test_dlc_meta_type(self):
		blob = build_cnmt("010056901A4C9001", 0, title_type=rr.CONTENT_META_TYPE_ADD_ON_CONTENT)
		_, _, meta_type = rr.parse_cnmt(blob)
		self.assertEqual(meta_type, rr.CONTENT_META_TYPE_ADD_ON_CONTENT)

	def test_too_short_raises(self):
		with self.assertRaises(rr.HactoolError):
			rr.parse_cnmt(b"\x00" * 4)


class TestCnmtContentEntries(unittest.TestCase):
	def test_basic_roundtrip(self):
		nca_id_1 = "11" * 16
		nca_id_2 = "22" * 16
		blob = build_cnmt(
			"0100EFD00A4FA000", 0,
			content_entries=[
				(nca_id_1, 123456, rr.CONTENT_TYPE_PROGRAM),
				(nca_id_2, 789, rr.CONTENT_TYPE_CONTROL),
			],
			ext_header_size=0x10,
		)
		entries = rr.parse_cnmt_content_entries(blob)
		self.assertEqual(len(entries), 2)
		self.assertEqual(entries[0]["nca_id_hex"], nca_id_1)
		self.assertEqual(entries[0]["size"], 123456)
		self.assertEqual(entries[0]["content_type"], rr.CONTENT_TYPE_PROGRAM)
		self.assertEqual(entries[1]["nca_id_hex"], nca_id_2)
		self.assertEqual(entries[1]["size"], 789)
		self.assertEqual(entries[1]["content_type"], rr.CONTENT_TYPE_CONTROL)

	def test_no_entries(self):
		blob = build_cnmt("0100EFD00A4FA000", 0)
		self.assertEqual(rr.parse_cnmt_content_entries(blob), [])

	def test_too_short_raises(self):
		with self.assertRaises(rr.HactoolError):
			rr.parse_cnmt_content_entries(b"\x00" * 10)

	def test_truncated_entries_raises(self):
		blob = build_cnmt(
			"0100EFD00A4FA000", 0,
			content_entries=[("33" * 16, 100, rr.CONTENT_TYPE_DATA)],
		)
		truncated = blob[:-10]
		with self.assertRaises(rr.HactoolError):
			rr.parse_cnmt_content_entries(truncated)


class TestCnmtApplicationId(unittest.TestCase):
	"""parse_cnmt_application_id reads the real base/application id out of
	a Patch/AddOnContent CNMT's extended header -- ground truth, unlike
	guessing the base id from the content's own title id (see
	TestBaseIdFromTitleId for why that guess isn't always safe)."""

	def test_reads_application_id_from_dlc_extended_header(self):
		blob = build_cnmt(
			"010056901A4C9013", 0,
			title_type=rr.CONTENT_META_TYPE_ADD_ON_CONTENT,
			ext_header_size=0x10,
			application_id_hex="010056901A4C9000",
		)
		self.assertEqual(rr.parse_cnmt_application_id(blob), 0x010056901A4C9000)

	def test_reads_application_id_from_patch_extended_header(self):
		blob = build_cnmt(
			"010059B017F9E800", 786432,
			title_type=rr.CONTENT_META_TYPE_PATCH,
			ext_header_size=0x18,
			application_id_hex="010059B017F9E000",
		)
		self.assertEqual(rr.parse_cnmt_application_id(blob), 0x010059B017F9E000)

	def test_no_extended_header_returns_none(self):
		# An Application/BASE CNMT has no ApplicationId field -- it IS the
		# application.
		blob = build_cnmt("0100EFD00A4FA000", 0, title_type=rr.CONTENT_META_TYPE_APPLICATION)
		self.assertIsNone(rr.parse_cnmt_application_id(blob))

	def test_too_short_returns_none_not_raise(self):
		self.assertIsNone(rr.parse_cnmt_application_id(b"\x00" * 10))

	def test_ext_header_too_small_for_application_id_returns_none(self):
		blob = build_cnmt(
			"010056901A4C9013", 0,
			title_type=rr.CONTENT_META_TYPE_ADD_ON_CONTENT,
			ext_header_size=4,
		)
		self.assertIsNone(rr.parse_cnmt_application_id(blob))


class TestBaseIdFromTitleId(unittest.TestCase):
	def test_strips_last_three_hex_digits(self):
		self.assertEqual(rr.base_id_from_title_id("010056901A4C9013"), "010056901A4C9000")

	def test_base_id_of_a_base_is_itself(self):
		self.assertEqual(rr.base_id_from_title_id("0100EFD00A4FA000"), "0100EFD00A4FA000")


class TestNacpParsing(unittest.TestCase):
	def test_finds_preferred_language(self):
		blob = build_nacp("Super Test Game", lang_index=0)
		self.assertEqual(rr.parse_nacp_title_name(blob), "Super Test Game")

	def test_falls_back_when_preferred_languages_empty(self):
		blob = build_nacp("Fallback Title", lang_index=5)
		self.assertEqual(rr.parse_nacp_title_name(blob), "Fallback Title")

	def test_all_empty_returns_none(self):
		blob = bytes(rr.NACP_LANGUAGE_ENTRY_SIZE * rr.NACP_LANGUAGE_COUNT)
		self.assertIsNone(rr.parse_nacp_title_name(blob))

	def test_too_short_data_returns_none(self):
		self.assertIsNone(rr.parse_nacp_title_name(b"\x00" * 10))


class TestSanitizeName(unittest.TestCase):
	def test_replaces_colon(self):
		self.assertEqual(rr.sanitize_name("Zelda: Breath of the Wild"), "Zelda - Breath of the Wild")

	def test_strips_forbidden_chars(self):
		self.assertEqual(rr.sanitize_name('Name<>"/\\|?*Test'), "NameTest")

	def test_collapses_whitespace_and_trims(self):
		self.assertEqual(rr.sanitize_name("  Multi   Space   Name  . "), "Multi Space Name")

	def test_strips_brackets(self):
		# Brackets are stripped so a title/dlc_name can never be mistaken
		# for -- or corrupt -- our own [tag] delimiters.
		self.assertEqual(rr.sanitize_name("Cool Game [DELUXE]"), "Cool Game DELUXE")


class TestHumanizeFallbackText(unittest.TestCase):
	def test_underscores_and_dashes_become_spaces(self):
		self.assertEqual(rr.humanize_fallback_text("some_game-name"), "Some Game Name")

	def test_mixed_case_not_retitled(self):
		# Already has some capitalization -- don't clobber it with a guess.
		self.assertEqual(rr.humanize_fallback_text("AAA_dlc_slug"), "AAA dlc slug")

	def test_collapses_whitespace(self):
		self.assertEqual(rr.humanize_fallback_text("a   b_c"), "A B C")


class TestContentTypeFromMetaType(unittest.TestCase):
	def test_application_is_base(self):
		self.assertEqual(rr.content_type_from_meta_type(rr.CONTENT_META_TYPE_APPLICATION), rr.ContentType.BASE)

	def test_patch_is_update(self):
		self.assertEqual(rr.content_type_from_meta_type(rr.CONTENT_META_TYPE_PATCH), rr.ContentType.UPDATE)

	def test_add_on_content_is_dlc(self):
		self.assertEqual(rr.content_type_from_meta_type(rr.CONTENT_META_TYPE_ADD_ON_CONTENT), rr.ContentType.DLC)

	def test_other_values_are_unknown(self):
		for other in (0x00, 0x01, 0x02, 0x03, 0x04, 0x83, 0xFF):
			self.assertEqual(rr.content_type_from_meta_type(other), rr.ContentType.UNKNOWN)


class TestBuildNewStem(unittest.TestCase):
	def test_base_format(self):
		new = rr.build_new_stem("Test Game", "0100EFD00A4FA000", 0, rr.ContentType.BASE)
		self.assertEqual(new, "Test Game [0100EFD00A4FA000] [BASE][v0]")

	def test_update_format(self):
		new = rr.build_new_stem("Test Game", "0100EFD00A4FA800", 65536, rr.ContentType.UPDATE)
		self.assertEqual(new, "Test Game [0100EFD00A4FA800] [UPDATE][v65536]")

	def test_dlc_format_with_name(self):
		new = rr.build_new_stem("Test Game", "010056901A4C9001", 0, rr.ContentType.DLC, dlc_name="Bonus Pack")
		self.assertEqual(new, "Test Game [Bonus Pack] [010056901A4C9001] [DLC][v0]")

	def test_dlc_format_without_name(self):
		new = rr.build_new_stem("Test Game", "010056901A4C9001", 0, rr.ContentType.DLC)
		self.assertEqual(new, "Test Game [010056901A4C9001] [DLC][v0]")

	def test_unknown_format_has_no_version(self):
		new = rr.build_new_stem("Some System Title", "0100000000000001", 999, rr.ContentType.UNKNOWN)
		self.assertEqual(new, "Some System Title [0100000000000001] [UNKNOWN]")

	def test_title_is_sanitized(self):
		new = rr.build_new_stem("Zelda: Test", "0100EFD00A4FA000", 0, rr.ContentType.BASE)
		self.assertEqual(new, "Zelda - Test [0100EFD00A4FA000] [BASE][v0]")

	def test_dlc_name_is_sanitized(self):
		new = rr.build_new_stem("Game", "010056901A4C9001", 0, rr.ContentType.DLC, dlc_name="Bonus: Pack")
		self.assertEqual(new, "Game [Bonus - Pack] [010056901A4C9001] [DLC][v0]")


class TestCanonicalTailRe(unittest.TestCase):
	"""CANONICAL_TAIL_RE is what lets a second run recognize an already-
	resolved [dlc_name] tag as such (to preserve it) instead of discarding
	it as generic bracketed noise -- regression coverage for a real bug:
	re-running the script over an already-canonical DLC filename silently
	dropped its dlc_name, because it looked exactly like any other
	"[tag] to discard" to the old parser."""

	def test_matches_dlc_with_name(self):
		m = rr.CANONICAL_TAIL_RE.match(
			"Capcom Arcade 2nd Stadium [1943 Kai Midway Kaisen] [0100DC60167B5013] [DLC][v0]"
		)
		self.assertIsNotNone(m)
		self.assertEqual(m.group("name"), "Capcom Arcade 2nd Stadium")
		self.assertEqual(m.group("dlc_name"), "1943 Kai Midway Kaisen")

	def test_matches_dlc_without_name(self):
		m = rr.CANONICAL_TAIL_RE.match("Some Game [010056901A4C9001] [DLC][v0]")
		self.assertIsNotNone(m)
		self.assertEqual(m.group("name"), "Some Game")
		self.assertIsNone(m.group("dlc_name"))

	def test_matches_base(self):
		m = rr.CANONICAL_TAIL_RE.match("Test Game [0100EFD00A4FA000] [BASE][v0]")
		self.assertIsNotNone(m)
		self.assertEqual(m.group("name"), "Test Game")
		self.assertIsNone(m.group("dlc_name"))

	def test_matches_unknown_with_no_version(self):
		m = rr.CANONICAL_TAIL_RE.match("Some System Title [0100000000000001] [UNKNOWN]")
		self.assertIsNotNone(m)
		self.assertEqual(m.group("name"), "Some System Title")

	def test_legacy_scene_tag_does_not_match(self):
		# No real 16-hex id tag anywhere -- "[UPD]" must NOT be mistaken
		# for a dlc_name (there's nothing canonical about this filename at
		# all), so the caller falls back to discarding it as noise.
		m = rr.CANONICAL_TAIL_RE.match("Some Game [UPD]")
		self.assertIsNone(m)

	def test_bracket_before_real_tail_without_valid_id_does_not_match(self):
		m = rr.CANONICAL_TAIL_RE.match("Some Game [Region] [DLC][v0]")
		self.assertIsNone(m)


class TestCacheFingerprintAndLookup(unittest.TestCase):
	"""cache_fingerprint/cache_lookup are the hactool-free fast path's
	building blocks: a fingerprint is only ever written from a REAL
	hactool-verified result (see process_dirs), and a lookup only counts
	as a hit when size+mtime still match the file on disk right now --
	unlike a filename-shape check, this can never mistake a wrong-but-
	plausible-looking name for a verified one, since nothing is trusted
	until hactool itself vouched for it."""

	def test_lookup_hits_when_fingerprint_matches_current_file(self):
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "game.nsp"
			p.write_bytes(b"hello")
			entry = rr.cache_fingerprint(p, "0100EFD00A4FA000", 0, rr.ContentType.BASE, "0100EFD00A4FA000")
			cache = {str(p): entry}
			hit = rr.cache_lookup(cache, p)
			self.assertIsNotNone(hit)
			self.assertEqual(hit["title_id"], "0100EFD00A4FA000")
			self.assertEqual(hit["content_type"], "BASE")

	def test_lookup_misses_when_not_in_cache(self):
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "game.nsp"
			p.write_bytes(b"hello")
			self.assertIsNone(rr.cache_lookup({}, p))

	def test_lookup_misses_when_size_changed(self):
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "game.nsp"
			p.write_bytes(b"hello")
			entry = rr.cache_fingerprint(p, "0100EFD00A4FA000", 0, rr.ContentType.BASE, "0100EFD00A4FA000")
			p.write_bytes(b"hello world, now longer")  # content (and size) changed
			self.assertIsNone(rr.cache_lookup({str(p): entry}, p))

	def test_lookup_misses_when_mtime_changed(self):
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "game.nsp"
			p.write_bytes(b"hello")
			entry = rr.cache_fingerprint(p, "0100EFD00A4FA000", 0, rr.ContentType.BASE, "0100EFD00A4FA000")
			entry["mtime"] -= 100  # simulate the file having been touched since
			self.assertIsNone(rr.cache_lookup({str(p): entry}, p))

	def test_lookup_misses_when_file_no_longer_exists(self):
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "game.nsp"
			p.write_bytes(b"hello")
			entry = rr.cache_fingerprint(p, "0100EFD00A4FA000", 0, rr.ContentType.BASE, "0100EFD00A4FA000")
			p.unlink()
			self.assertIsNone(rr.cache_lookup({str(p): entry}, p))

	def test_lookup_treats_malformed_entry_as_a_miss_not_a_crash(self):
		# rename_roms.cache.json is plain, user-editable JSON -- a bad
		# entry (hand-edited, half-written, or from an incompatible future
		# format) must degrade to "re-verify with hactool", never crash
		# the whole run.
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "game.nsp"
			p.write_bytes(b"hello")
			st = p.stat()
			base = {"size": st.st_size, "mtime": st.st_mtime}

			self.assertIsNone(rr.cache_lookup({str(p): {**base, "content_type": "NOT_A_REAL_TYPE",
															"title_id": "X", "version": 0, "base_id": "X"}}, p))
			self.assertIsNone(rr.cache_lookup({str(p): {**base, "content_type": "BASE",
															"title_id": "X", "version": "not-an-int", "base_id": "X"}}, p))
			self.assertIsNone(rr.cache_lookup({str(p): {**base, "content_type": "BASE", "base_id": "X"}}, p))  # missing title_id
			self.assertIsNone(rr.cache_lookup({str(p): "not even a dict"}, p))


class TestLoadCache(unittest.TestCase):
	def test_missing_file_returns_empty_dict(self):
		with tempfile.TemporaryDirectory() as d:
			self.assertEqual(rr.load_cache(Path(d) / "nope.json"), {})

	def test_valid_cache_file_loads(self):
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "cache.json"
			p.write_text('{"foo": {"size": 1, "mtime": 2.0}}', encoding="utf-8")
			self.assertEqual(rr.load_cache(p), {"foo": {"size": 1, "mtime": 2.0}})

	def test_corrupt_cache_file_returns_empty_dict_not_raise(self):
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "cache.json"
			p.write_text("{not valid json", encoding="utf-8")
			self.assertEqual(rr.load_cache(p), {})


class TestStripLeadingTitle(unittest.TestCase):
	def test_strips_verbatim_prefix(self):
		# Real case from this library: Capcom Arcade 2nd Stadium's DLC
		# Control NCAs report a name that's the base title PLUS the
		# episode name run together, not just the episode name alone.
		self.assertEqual(
			rr.strip_leading_title(
				"Capcom Arcade 2nd Stadium 1943 Kai Midway Kaisen",
				"Capcom Arcade 2nd Stadium",
			),
			"1943 Kai Midway Kaisen",
		)

	def test_strips_prefix_with_dash_separator(self):
		self.assertEqual(rr.strip_leading_title("Cool Game - Bonus Pack", "Cool Game"), "Bonus Pack")

	def test_strips_prefix_with_colon_separator(self):
		self.assertEqual(rr.strip_leading_title("Cool Game: Bonus Pack", "Cool Game"), "Bonus Pack")

	def test_case_insensitive_match(self):
		self.assertEqual(rr.strip_leading_title("cool game bonus pack", "Cool Game"), "bonus pack")

	def test_no_prefix_returns_text_unchanged(self):
		self.assertEqual(rr.strip_leading_title("Bonus Pack", "Cool Game"), "Bonus Pack")

	def test_exact_match_returns_empty_string(self):
		self.assertEqual(rr.strip_leading_title("Cool Game", "Cool Game"), "")

	def test_strips_repeated_leading_copies(self):
		# Real case from this library: old filename text already carried
		# the title TWICE (once tacked on by an earlier ID/version-only
		# pass of this script, once baked into the DLC's own display name
		# per Capcom's "{title}: {episode}" convention) -- a single strip
		# pass would leave one copy behind.
		self.assertEqual(
			rr.strip_leading_title(
				"Capcom Arcade 2nd Stadium Capcom Arcade 2nd Stadium 1943 Kai Midway Kaisen",
				"Capcom Arcade 2nd Stadium",
			),
			"1943 Kai Midway Kaisen",
		)

	def test_empty_inputs_are_safe(self):
		self.assertEqual(rr.strip_leading_title("", "Cool Game"), "")
		self.assertEqual(rr.strip_leading_title("Bonus Pack", ""), "Bonus Pack")


class TestLocateMetaNca(unittest.TestCase):
	def test_nsp(self):
		blob = build_pfs0([
			("x.cert", b"C" * 100),
			("x.tik", b"T" * 50),
			("aaaa.nca", b"N" * 2000),
			("bbbb.cnmt.nca", b"M" * 123),
		])
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "game.nsp"
			p.write_bytes(blob)
			offset, size = rr.locate_meta_nca(p)
			self.assertEqual(size, 123)
			with open(p, "rb") as f:
				f.seek(offset)
				self.assertEqual(f.read(123), b"M" * 123)

	def test_xci(self):
		secure_inner = build_hfs0([
			("aaaa.nca", b"N" * 4000),
			("bbbb.cnmt.nca", b"M" * 77),
		])
		root = build_hfs0([
			("update", b"\x00" * 200),
			("normal", b"\x00" * 200),
			("secure", secure_inner),
		])
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "game.xci"
			# pad up to the fixed root-HFS0 offset with a fake gamecard header
			p.write_bytes(b"\x00" * rr.XCI_HFS0_OFFSET + root)
			offset, size = rr.locate_meta_nca(p)
			self.assertEqual(size, 77)
			with open(p, "rb") as f:
				f.seek(offset)
				self.assertEqual(f.read(77), b"M" * 77)

	def test_nsz_with_compressed_ncz_entry_alongside(self):
		# nsz only compresses the big Program/Data NCA into a ".ncz" entry --
		# the Meta ("*.cnmt.nca") entry stays uncompressed. Confirmed against
		# real .nsz files in this library. Prove locate_meta_nca finds the
		# real cnmt.nca and ignores the (fake, "compressed") .ncz entry.
		blob = build_pfs0([
			("x.cert", b"C" * 100),
			("x.tik", b"T" * 50),
			("bignca.ncz", b"Z" * 9000),          # stands in for compressed Program NCA
			("bbbb.cnmt.nca", b"M" * 123),         # still plain/uncompressed
		])
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "game.nsz"
			p.write_bytes(blob)
			offset, size = rr.locate_meta_nca(p)
			self.assertEqual(size, 123)
			with open(p, "rb") as f:
				f.seek(offset)
				self.assertEqual(f.read(123), b"M" * 123)

	def test_xcz_with_compressed_ncz_entry_alongside(self):
		secure_inner = build_hfs0([
			("bignca.ncz", b"Z" * 9000),
			("bbbb.cnmt.nca", b"M" * 77),
		])
		root = build_hfs0([
			("update", b"\x00" * 200),
			("normal", b"\x00" * 200),
			("secure", secure_inner),
		])
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "game.xcz"
			p.write_bytes(b"\x00" * rr.XCI_HFS0_OFFSET + root)
			offset, size = rr.locate_meta_nca(p)
			self.assertEqual(size, 77)
			with open(p, "rb") as f:
				f.seek(offset)
				self.assertEqual(f.read(77), b"M" * 77)

	def test_unsupported_extension_raises(self):
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "game.rom"
			p.write_bytes(b"whatever")
			with self.assertRaises(rr.HactoolError):
				rr.locate_meta_nca(p)


class TestLocateNcaById(unittest.TestCase):
	def test_present_in_nsp(self):
		nca_id = "ab" * 16
		blob = build_pfs0([
			("x.cert", b"C" * 10),
			("bbbb.cnmt.nca", b"M" * 50),
			(f"{nca_id}.nca", b"K" * 321),
		])
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "game.nsp"
			p.write_bytes(blob)
			loc = rr.locate_nca_by_id(p, nca_id)
			self.assertIsNotNone(loc)
			offset, size = loc
			self.assertEqual(size, 321)
			with open(p, "rb") as f:
				f.seek(offset)
				self.assertEqual(f.read(321), b"K" * 321)

	def test_absent_returns_none(self):
		blob = build_pfs0([("bbbb.cnmt.nca", b"M" * 50)])
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "game.nsp"
			p.write_bytes(blob)
			self.assertIsNone(rr.locate_nca_by_id(p, "ff" * 16))

	def test_present_in_xci(self):
		nca_id = "cd" * 16
		secure_inner = build_hfs0([
			("bbbb.cnmt.nca", b"M" * 40),
			(f"{nca_id}.nca", b"K" * 88),
		])
		root = build_hfs0([
			("update", b"\x00" * 100),
			("normal", b"\x00" * 100),
			("secure", secure_inner),
		])
		with tempfile.TemporaryDirectory() as d:
			p = Path(d) / "game.xci"
			p.write_bytes(b"\x00" * rr.XCI_HFS0_OFFSET + root)
			loc = rr.locate_nca_by_id(p, nca_id)
			self.assertIsNotNone(loc)
			offset, size = loc
			self.assertEqual(size, 88)


class TestExtractRealIdVersionWithStubHactool(unittest.TestCase):
	"""
	Verifies the full extract_real_id_version pipeline WITHOUT real hactool
	or real crypto: hactool.exe is replaced by a tiny stub script that just
	copies its input NCA bytes straight into --section0dir as a .cnmt file
	(standing in for "decryption"). This proves the plumbing -- temp file
	creation, correct byte range read from source, correct hactool args,
	locating hactool's output, cleanup -- all work correctly.
	"""

	def test_pipeline_with_stub(self):
		with tempfile.TemporaryDirectory() as d:
			tmp = Path(d)
			cnmt_bytes = build_cnmt("0100EFD00A4FA800", 65536, title_type=rr.CONTENT_META_TYPE_PATCH)
			meta_nca_bytes = b"FAKE_NCA_HEADER_" + cnmt_bytes  # stub just copies this whole blob through

			blob = build_pfs0([
				("x.cert", b"C" * 10),
				("aaaa.nca", b"N" * 500),
				("bbbb.cnmt.nca", meta_nca_bytes),
			])
			rom_path = tmp / "Test Game [UPD].nsp"
			rom_path.write_bytes(blob)

			stub_py = tmp / "hactool_stub.py"
			stub_py.write_text(
				"import sys, shutil\n"
				"from pathlib import Path\n"
				"args = sys.argv[1:]\n"
				"section0dir = None\n"
				"nca_file = None\n"
				"for a in args:\n"
				"    if a.startswith('--section0dir='):\n"
				"        section0dir = a.split('=', 1)[1]\n"
				"    elif not a.startswith('-') and Path(a).exists():\n"
				"        nca_file = a\n"
				"Path(section0dir).mkdir(parents=True, exist_ok=True)\n"
				"data = Path(nca_file).read_bytes()\n"
				"cnmt = data[len(b'FAKE_NCA_HEADER_'):]\n"  # simulate decrypt: strip fake header
				"(Path(section0dir) / 'stub.cnmt').write_bytes(cnmt)\n"
			)

			# Monkeypatch: make run_hactool_section0 call our python stub instead of a real exe
			orig = rr.run_hactool_section0
			def fake_run(hactool_path, keys_path, nca_file, out_dir):
				import subprocess as sp
				r = sp.run([sys.executable, str(stub_py), f"--section0dir={out_dir}", str(nca_file)],
						   capture_output=True, text=True)
				if r.returncode != 0:
					raise rr.HactoolError(r.stderr)
			rr.run_hactool_section0 = fake_run
			try:
				keys_path = tmp / "keys.txt"
				keys_path.write_text("stub")
				title_id, version, meta_type = rr.extract_real_id_version(rom_path, tmp / "hactool.exe", keys_path)
			finally:
				rr.run_hactool_section0 = orig

			self.assertEqual(title_id, "0100EFD00A4FA800")
			self.assertEqual(version, 65536)
			self.assertEqual(meta_type, rr.CONTENT_META_TYPE_PATCH)


class TestExtractRealBaseIdWithStubHactool(unittest.TestCase):
	"""
	extract_real_base_id must read the real ApplicationId out of the CNMT
	for UPDATE/DLC (ground truth) rather than assume a DLC's own title id
	shares the base game's id prefix -- regression coverage for a real
	title in this library (Capcom Arcade 2nd Stadium) whose DLC ids don't
	share the base id prefix, which silently broke shared-name resolution
	(and therefore DLC renaming) for every one of that title's DLC files.
	"""

	def _stub_section0(self, tmp: Path, cnmt_bytes: bytes) -> Path:
		meta_nca_bytes = b"FAKE_NCA_HEADER_" + cnmt_bytes
		blob = build_pfs0([("bbbb.cnmt.nca", meta_nca_bytes)])
		rom_path = tmp / "content.nsp"
		rom_path.write_bytes(blob)

		stub_py = tmp / "hactool_stub.py"
		stub_py.write_text(
			"import sys\n"
			"from pathlib import Path\n"
			"args = sys.argv[1:]\n"
			"section0dir = None\n"
			"nca_file = None\n"
			"for a in args:\n"
			"    if a.startswith('--section0dir='):\n"
			"        section0dir = a.split('=', 1)[1]\n"
			"    elif not a.startswith('-') and Path(a).exists():\n"
			"        nca_file = a\n"
			"Path(section0dir).mkdir(parents=True, exist_ok=True)\n"
			"data = Path(nca_file).read_bytes()\n"
			"cnmt = data[len(b'FAKE_NCA_HEADER_'):]\n"
			"(Path(section0dir) / 'stub.cnmt').write_bytes(cnmt)\n"
		)
		return rom_path

	def test_base_returns_its_own_title_id_without_calling_hactool(self):
		with tempfile.TemporaryDirectory() as d:
			tmp = Path(d)
			orig = rr.run_hactool_section0
			def fails_if_called(*a, **k):
				raise AssertionError("hactool should never be invoked for a BASE file")
			rr.run_hactool_section0 = fails_if_called
			try:
				base_id = rr.extract_real_base_id(
					tmp / "unused.nsp", tmp / "hactool.exe", tmp / "keys.txt",
					"0100EFD00A4FA000", rr.CONTENT_META_TYPE_APPLICATION,
				)
			finally:
				rr.run_hactool_section0 = orig
			self.assertEqual(base_id, "0100EFD00A4FA000")

	def test_dlc_with_non_standard_id_resolves_via_application_id_field(self):
		# Real case from this library: base is "...B4000", but this DLC's
		# own title id is "...B5013" -- doesn't share the base's id prefix
		# at all, so the guess (base_id_from_title_id) would compute
		# "...B5000", a base id that doesn't exist in the library.
		with tempfile.TemporaryDirectory() as d:
			tmp = Path(d)
			cnmt_bytes = build_cnmt(
				"0100DC60167B5013", 0,
				title_type=rr.CONTENT_META_TYPE_ADD_ON_CONTENT,
				ext_header_size=0x10,
				application_id_hex="0100DC60167B4000",
			)
			rom_path = self._stub_section0(tmp, cnmt_bytes)

			orig = rr.run_hactool_section0
			def fake_run(hactool_path, keys_path, nca_file, out_dir):
				import subprocess as sp
				r = sp.run([sys.executable, str(tmp / "hactool_stub.py"), f"--section0dir={out_dir}", str(nca_file)],
						   capture_output=True, text=True)
				if r.returncode != 0:
					raise rr.HactoolError(r.stderr)
			rr.run_hactool_section0 = fake_run
			try:
				base_id = rr.extract_real_base_id(
					rom_path, tmp / "hactool.exe", tmp / "keys.txt",
					"0100DC60167B5013", rr.CONTENT_META_TYPE_ADD_ON_CONTENT,
				)
			finally:
				rr.run_hactool_section0 = orig

			self.assertEqual(base_id, "0100DC60167B4000")

	def test_falls_back_to_guess_when_hactool_fails(self):
		with tempfile.TemporaryDirectory() as d:
			tmp = Path(d)
			blob = build_pfs0([("bbbb.cnmt.nca", b"M" * 50)])
			rom_path = tmp / "content.nsp"
			rom_path.write_bytes(blob)

			orig = rr.run_hactool_section0
			def fails(*a, **k):
				raise rr.HactoolError("simulated failure")
			rr.run_hactool_section0 = fails
			try:
				base_id = rr.extract_real_base_id(
					rom_path, tmp / "hactool.exe", tmp / "keys.txt",
					"010056901A4C9001", rr.CONTENT_META_TYPE_ADD_ON_CONTENT,
				)
			finally:
				rr.run_hactool_section0 = orig
			self.assertEqual(base_id, "010056901A4C9000")


class TestExtractTitleNameWithStubHactool(unittest.TestCase):
	"""
	Verifies the extract_title_name pipeline WITHOUT real hactool or real
	crypto: run_hactool_romfs is replaced by a stub script that copies its
	input NCA bytes straight into --romfsdir as control.nacp (standing in
	for "decryption"). Proves the Control-NCA lookup, temp file handling,
	and NACP parsing are all wired together correctly.
	"""

	def test_resolves_name_via_control_nca(self):
		with tempfile.TemporaryDirectory() as d:
			tmp = Path(d)
			control_nca_id = "11" * 16
			nacp_bytes = build_nacp("Stubbed Game Title")
			fake_control_nca_bytes = b"FAKE_CONTROL_NCA_" + nacp_bytes

			blob = build_pfs0([
				("x.cert", b"C" * 10),
				("aaaa.nca", b"N" * 500),
				("bbbb.cnmt.nca", b"M" * 50),
				(f"{control_nca_id}.nca", fake_control_nca_bytes),
			])
			rom_path = tmp / "Some Game.nsp"
			rom_path.write_bytes(blob)

			stub_py = tmp / "hactool_romfs_stub.py"
			stub_py.write_text(
				"import sys\n"
				"from pathlib import Path\n"
				"args = sys.argv[1:]\n"
				"romfsdir = None\n"
				"nca_file = None\n"
				"for a in args:\n"
				"    if a.startswith('--romfsdir='):\n"
				"        romfsdir = a.split('=', 1)[1]\n"
				"    elif not a.startswith('-') and Path(a).exists():\n"
				"        nca_file = a\n"
				"Path(romfsdir).mkdir(parents=True, exist_ok=True)\n"
				"data = Path(nca_file).read_bytes()\n"
				"nacp = data[len(b'FAKE_CONTROL_NCA_'):]\n"
				"(Path(romfsdir) / 'control.nacp').write_bytes(nacp)\n"
			)

			orig = rr.run_hactool_romfs
			def fake_run(hactool_path, keys_path, nca_file, out_dir):
				import subprocess as sp
				r = sp.run([sys.executable, str(stub_py), f"--romfsdir={out_dir}", str(nca_file)],
						   capture_output=True, text=True)
				if r.returncode != 0:
					raise rr.HactoolError(r.stderr)
			rr.run_hactool_romfs = fake_run
			try:
				content_entries = [{
					"nca_id_hex": control_nca_id,
					"size": len(fake_control_nca_bytes),
					"content_type": rr.CONTENT_TYPE_CONTROL,
				}]
				name = rr.extract_title_name(rom_path, tmp / "hactool.exe", tmp / "keys.txt", content_entries)
			finally:
				rr.run_hactool_romfs = orig

			self.assertEqual(name, "Stubbed Game Title")

	def test_no_control_entry_returns_none_without_calling_hactool(self):
		called = {"n": 0}
		orig = rr.run_hactool_romfs
		def fake_run(*a, **k):
			called["n"] += 1
			raise AssertionError("should not be called when there's no Control entry")
		rr.run_hactool_romfs = fake_run
		try:
			content_entries = [{"nca_id_hex": "aa" * 16, "size": 100, "content_type": rr.CONTENT_TYPE_PROGRAM}]
			name = rr.extract_title_name(Path("/nonexistent.nsp"), Path("hactool"), Path("keys"), content_entries)
		finally:
			rr.run_hactool_romfs = orig
		self.assertIsNone(name)
		self.assertEqual(called["n"], 0)

	def test_hactool_failure_returns_none_gracefully(self):
		with tempfile.TemporaryDirectory() as d:
			tmp = Path(d)
			control_nca_id = "22" * 16
			blob = build_pfs0([
				("bbbb.cnmt.nca", b"M" * 50),
				(f"{control_nca_id}.nca", b"X" * 100),
			])
			rom_path = tmp / "Some Game.nsp"
			rom_path.write_bytes(blob)

			orig = rr.run_hactool_romfs
			def fake_fail(hactool_path, keys_path, nca_file, out_dir):
				raise rr.HactoolError("simulated hactool failure")
			rr.run_hactool_romfs = fake_fail
			try:
				content_entries = [{"nca_id_hex": control_nca_id, "size": 100, "content_type": rr.CONTENT_TYPE_CONTROL}]
				name = rr.extract_title_name(rom_path, tmp / "hactool.exe", tmp / "keys.txt", content_entries)
			finally:
				rr.run_hactool_romfs = orig
			self.assertIsNone(name)


class TestProgressLine(unittest.TestCase):
	"""ProgressLine writes nothing at all unless explicitly enabled (it
	auto-detects a live terminal via isatty() by default, which a test
	run under a test runner never is) -- these tests force enabled=True/
	False directly rather than relying on that detection."""

	def test_disabled_writes_nothing(self):
		pl = rr.ProgressLine(enabled=False)
		pl.reset(10, "Scanning")
		buf = io.StringIO()
		with contextlib.redirect_stdout(buf):
			pl.update(5, "some_file.nsp")
			pl.clear()
		self.assertEqual(buf.getvalue(), "")

	def test_enabled_renders_progress_and_label(self):
		pl = rr.ProgressLine(enabled=True)
		pl.reset(10, "Scanning")
		buf = io.StringIO()
		with contextlib.redirect_stdout(buf):
			pl.update(5, "some_file.nsp")
		out = buf.getvalue()
		self.assertIn("Scanning", out)
		self.assertIn("5/10", out)
		self.assertIn("some_file.nsp", out)
		self.assertTrue(out.startswith("\r"))

	def test_zero_total_writes_nothing(self):
		# Guards the empty-library case: total=0 must never divide by zero.
		pl = rr.ProgressLine(enabled=True)
		pl.reset(0, "Scanning")
		buf = io.StringIO()
		with contextlib.redirect_stdout(buf):
			pl.update(0, "")
		self.assertEqual(buf.getvalue(), "")

	def test_shorter_redraw_pads_over_previous_text(self):
		# A second, shorter update must fully overwrite leftover characters
		# from a longer previous one, not just prefix over part of it.
		pl = rr.ProgressLine(enabled=True)
		pl.reset(100, "")
		buf = io.StringIO()
		with contextlib.redirect_stdout(buf):
			pl.update(1, "a very long detail string here")
			first_width = pl._last_width
			pl.update(2, "x")
		out = buf.getvalue()
		second_write = out.split("\r")[-1]
		self.assertGreaterEqual(len(second_write), first_width)

	def test_clear_erases_and_resets(self):
		pl = rr.ProgressLine(enabled=True)
		pl.reset(10, "Scanning")
		buf = io.StringIO()
		with contextlib.redirect_stdout(buf):
			pl.update(5, "x")
			pl.clear()
		self.assertEqual(pl._last_width, 0)
		# clear() on an already-clear line is a no-op, not a stray "\r".
		buf2 = io.StringIO()
		with contextlib.redirect_stdout(buf2):
			pl.clear()
		self.assertEqual(buf2.getvalue(), "")


class TestProgressAwareStreamHandler(unittest.TestCase):
	def test_emit_clears_progress_before_logging(self):
		pl = rr.ProgressLine(enabled=True)
		pl.reset(10, "Scanning")
		buf = io.StringIO()
		with contextlib.redirect_stdout(buf):
			pl.update(3, "mid-progress")
		self.assertGreater(pl._last_width, 0)

		stream = io.StringIO()
		handler = rr.ProgressAwareStreamHandler(pl, stream)
		logger = logging.getLogger("test_progress_aware_handler")
		logger.handlers = [handler]
		logger.setLevel(logging.INFO)
		logger.propagate = False

		buf3 = io.StringIO()
		with contextlib.redirect_stdout(buf3):
			logger.info("a real log line")
		# clear() writes to stdout (redirected here), independently of the
		# handler's own `stream` (the StringIO passed to the constructor).
		self.assertIn("\r", buf3.getvalue())
		self.assertEqual(pl._last_width, 0)
		self.assertIn("a real log line", stream.getvalue())


class TestProcessDirsDryRunAndUndo(unittest.TestCase):
	"""End-to-end on real temp files, with extract_real_id_version mocked
	(no hactool/keys needed) to prove dry-run / apply / undo file handling."""

	def test_apply_then_undo(self):
		with tempfile.TemporaryDirectory() as d:
			rom_dir = Path(d) / "roms"
			rom_dir.mkdir()
			f1 = rom_dir / "Some Game [UPD] [v1.0.5].nsp"
			f1.write_bytes(b"x")
			f2 = rom_dir / "Some Game.nsp"
			f2.write_bytes(b"x")

			fake_results = {
				str(f1): ("0100AAAAAAAA0800", 65536, rr.CONTENT_META_TYPE_PATCH),
				str(f2): ("0100AAAAAAAA0000", 0, rr.CONTENT_META_TYPE_APPLICATION),
			}

			orig = rr.extract_real_id_version
			def fake_extract(path, hactool_path, keys_path):
				return fake_results[str(path)]
			rr.extract_real_id_version = fake_extract
			try:
				stats, undo_log = rr.process_dirs(
					[rom_dir], dry_run=False, recursive=False,
					hactool_path=Path("unused"), keys_path=Path("unused"),
				)
			finally:
				rr.extract_real_id_version = orig

			self.assertEqual(stats["fixed"], 2)
			names = sorted(p.name for p in rom_dir.iterdir())
			self.assertIn("Some Game [0100AAAAAAAA0800] [UPDATE][v65536].nsp", names)
			self.assertIn("Some Game [0100AAAAAAAA0000] [BASE][v0].nsp", names)

			# undo restores original names
			undo_file = Path(d) / "undo.json"
			undo_file.write_text(json.dumps({"session": "t", "renames": undo_log}))
			rr.do_undo(undo_file)
			names_after_undo = sorted(p.name for p in rom_dir.iterdir())
			self.assertIn("Some Game [UPD] [v1.0.5].nsp", names_after_undo)
			self.assertIn("Some Game.nsp", names_after_undo)

	def test_dry_run_does_not_modify_files(self):
		with tempfile.TemporaryDirectory() as d:
			rom_dir = Path(d) / "roms"
			rom_dir.mkdir()
			f1 = rom_dir / "Some Game.nsp"
			f1.write_bytes(b"x")

			orig = rr.extract_real_id_version
			rr.extract_real_id_version = lambda path, h, k: ("0100AAAAAAAA0000", 0, rr.CONTENT_META_TYPE_APPLICATION)
			try:
				stats, undo_log = rr.process_dirs(
					[rom_dir], dry_run=True, recursive=False,
					hactool_path=Path("unused"), keys_path=Path("unused"),
				)
			finally:
				rr.extract_real_id_version = orig

			self.assertEqual(stats["fixed"], 1)
			self.assertEqual(undo_log, [])
			self.assertTrue(f1.exists())  # untouched

	def test_nsz_is_processed_like_any_other_rom(self):
		# .nsz used to be treated as unsupported; it's now handled through
		# the normal pipeline like .nsp/.xci/.xcz.
		with tempfile.TemporaryDirectory() as d:
			rom_dir = Path(d) / "roms"
			rom_dir.mkdir()
			(rom_dir / "Some Game.nsz").write_bytes(b"x")

			orig = rr.extract_real_id_version
			rr.extract_real_id_version = lambda path, h, k: ("0100AAAAAAAA0000", 0, rr.CONTENT_META_TYPE_APPLICATION)
			try:
				stats, undo_log = rr.process_dirs(
					[rom_dir], dry_run=True, recursive=False,
					hactool_path=Path("unused"), keys_path=Path("unused"),
				)
			finally:
				rr.extract_real_id_version = orig

			self.assertEqual(stats["fixed"], 1)

	def test_truly_unrelated_extension_ignored(self):
		with tempfile.TemporaryDirectory() as d:
			rom_dir = Path(d) / "roms"
			rom_dir.mkdir()
			(rom_dir / "readme.txt").write_bytes(b"x")
			stats, undo_log = rr.process_dirs(
				[rom_dir], dry_run=True, recursive=False,
				hactool_path=Path("unused"), keys_path=Path("unused"),
			)
			self.assertEqual(stats["fixed"], 0)

	def test_aborts_early_after_consecutive_failures(self):
		# Simulates the real bug report: every single file fails the same
		# way (e.g. --keys pointed at a folder). Should stop early instead
		# of grinding through the whole library with identical warnings.
		with tempfile.TemporaryDirectory() as d:
			rom_dir = Path(d) / "roms"
			rom_dir.mkdir()
			for i in range(20):
				(rom_dir / f"Game {i} [0100AAAAAAAA000{i % 10}].nsp").write_bytes(b"x")

			def always_fails(path, hactool_path, keys_path):
				raise rr.HactoolError("hactool ran but produced no .cnmt (simulated)")

			orig = rr.extract_real_id_version
			rr.extract_real_id_version = always_fails
			try:
				stats, undo_log = rr.process_dirs(
					[rom_dir], dry_run=True, recursive=False,
					hactool_path=Path("unused"), keys_path=Path("unused"),
					max_consecutive_failures=5,
				)
			finally:
				rr.extract_real_id_version = orig

			self.assertTrue(stats["aborted_early"])
			# stopped at the threshold, not after all 20 files
			self.assertEqual(stats["unreadable"], 5)

	def test_does_not_abort_when_failures_are_not_consecutive(self):
		with tempfile.TemporaryDirectory() as d:
			rom_dir = Path(d) / "roms"
			rom_dir.mkdir()
			names = [f"Game {i}.nsp" for i in range(10)]
			for n in names:
				(rom_dir / n).write_bytes(b"x")

			call_count = {"n": 0}
			def alternating(path, hactool_path, keys_path):
				call_count["n"] += 1
				if call_count["n"] % 2 == 0:
					raise rr.HactoolError("simulated failure")
				return ("0100AAAAAAAA0000", 0, rr.CONTENT_META_TYPE_APPLICATION)

			orig = rr.extract_real_id_version
			rr.extract_real_id_version = alternating
			try:
				stats, undo_log = rr.process_dirs(
					[rom_dir], dry_run=True, recursive=False,
					hactool_path=Path("unused"), keys_path=Path("unused"),
					max_consecutive_failures=3,
				)
			finally:
				rr.extract_real_id_version = orig

			self.assertFalse(stats["aborted_early"])
			self.assertEqual(stats["unreadable"], 5)
			self.assertEqual(stats["fixed"], 5)


class TestProcessDirsVerifyCache(unittest.TestCase):
	"""process_dirs' default fast path: a file with a matching verify-cache
	entry (see cache_fingerprint/cache_lookup) must never reach hactool
	again, unless full_scan=True forces the old always-verify behavior.
	Critically, a file that merely LOOKS canonical but has no cache entry
	must still be fully verified -- that's the regression this class
	guards (test_dlc_with_non_prefix_sharing_id_still_reaches_shared_name
	in TestProcessDirsNameResolution caught the earlier filename-shape-
	trust design doing exactly the wrong thing here)."""

	def test_cache_hit_skips_hactool_entirely(self):
		with tempfile.TemporaryDirectory() as d:
			rom_dir = Path(d) / "roms"
			rom_dir.mkdir()
			stem = rr.build_new_stem("Test Game", "0100EFD00A4FA000", 0, rr.ContentType.BASE)
			f = rom_dir / f"{stem}.nsp"
			f.write_bytes(b"x")
			cache = {str(f): rr.cache_fingerprint(f, "0100EFD00A4FA000", 0, rr.ContentType.BASE, "0100EFD00A4FA000")}

			def fails_if_called(path, hactool_path, keys_path):
				raise AssertionError("hactool should never be invoked for a verify-cache hit")

			orig = rr.extract_real_id_version
			rr.extract_real_id_version = fails_if_called
			try:
				stats, undo_log = rr.process_dirs(
					[rom_dir], dry_run=True, recursive=False,
					hactool_path=Path("unused"), keys_path=Path("unused"),
					cache=cache,
				)
			finally:
				rr.extract_real_id_version = orig

			self.assertEqual(stats["already_correct"], 1)
			self.assertEqual(stats["fast_skipped"], 1)
			self.assertEqual(stats["fixed"], 0)
			self.assertTrue(f.exists())

	def test_canonical_looking_name_without_cache_entry_is_still_verified(self):
		# The regression case: a name that already LOOKS fully canonical
		# must NOT be trusted just because of its shape -- only an actual
		# cache hit (hactool-verified on a past run) may skip hactool.
		with tempfile.TemporaryDirectory() as d:
			rom_dir = Path(d) / "roms"
			rom_dir.mkdir()
			stem = rr.build_new_stem("Wrong Leftover Title", "0100EFD00A4FA000", 0, rr.ContentType.BASE)
			f = rom_dir / f"{stem}.nsp"
			f.write_bytes(b"x")

			call_count = {"n": 0}
			def fake_extract(path, hactool_path, keys_path):
				call_count["n"] += 1
				return ("0100EFD00A4FA000", 0, rr.CONTENT_META_TYPE_APPLICATION)

			orig = rr.extract_real_id_version
			rr.extract_real_id_version = fake_extract
			try:
				stats, undo_log = rr.process_dirs(
					[rom_dir], dry_run=True, recursive=False,
					hactool_path=Path("unused"), keys_path=Path("unused"),
					cache={},  # empty -- nothing has ever been verified
				)
			finally:
				rr.extract_real_id_version = orig

			self.assertEqual(call_count["n"], 1)
			self.assertEqual(stats["fast_skipped"], 0)

	def test_stale_cache_entry_triggers_reverify(self):
		with tempfile.TemporaryDirectory() as d:
			rom_dir = Path(d) / "roms"
			rom_dir.mkdir()
			stem = rr.build_new_stem("Test Game", "0100EFD00A4FA000", 0, rr.ContentType.BASE)
			f = rom_dir / f"{stem}.nsp"
			f.write_bytes(b"x")
			entry = rr.cache_fingerprint(f, "0100EFD00A4FA000", 0, rr.ContentType.BASE, "0100EFD00A4FA000")
			entry["size"] = 999999  # doesn't match the real file anymore
			cache = {str(f): entry}

			call_count = {"n": 0}
			def fake_extract(path, hactool_path, keys_path):
				call_count["n"] += 1
				return ("0100EFD00A4FA000", 0, rr.CONTENT_META_TYPE_APPLICATION)

			orig = rr.extract_real_id_version
			rr.extract_real_id_version = fake_extract
			try:
				rr.process_dirs(
					[rom_dir], dry_run=True, recursive=False,
					hactool_path=Path("unused"), keys_path=Path("unused"),
					cache=cache,
				)
			finally:
				rr.extract_real_id_version = orig

			self.assertEqual(call_count["n"], 1)

	def test_full_scan_ignores_cache_hit(self):
		with tempfile.TemporaryDirectory() as d:
			rom_dir = Path(d) / "roms"
			rom_dir.mkdir()
			stem = rr.build_new_stem("Test Game", "0100EFD00A4FA000", 0, rr.ContentType.BASE)
			f = rom_dir / f"{stem}.nsp"
			f.write_bytes(b"x")
			cache = {str(f): rr.cache_fingerprint(f, "0100EFD00A4FA000", 0, rr.ContentType.BASE, "0100EFD00A4FA000")}

			call_count = {"n": 0}
			def fake_extract(path, hactool_path, keys_path):
				call_count["n"] += 1
				return ("0100EFD00A4FA000", 0, rr.CONTENT_META_TYPE_APPLICATION)

			orig = rr.extract_real_id_version
			rr.extract_real_id_version = fake_extract
			try:
				stats, undo_log = rr.process_dirs(
					[rom_dir], dry_run=True, recursive=False,
					hactool_path=Path("unused"), keys_path=Path("unused"),
					cache=cache, full_scan=True,
				)
			finally:
				rr.extract_real_id_version = orig

			self.assertEqual(call_count["n"], 1)
			self.assertEqual(stats["fast_skipped"], 0)
			# full_scan still refreshes the cache from the fresh result.
			self.assertIn(str(f), cache)

	def test_successful_run_populates_cache_for_already_correct_and_renamed(self):
		with tempfile.TemporaryDirectory() as d:
			rom_dir = Path(d) / "roms"
			rom_dir.mkdir()
			already_correct_stem = rr.build_new_stem("Test Game", "0100EFD00A4FA000", 0, rr.ContentType.BASE)
			already_correct_f = rom_dir / f"{already_correct_stem}.nsp"
			already_correct_f.write_bytes(b"x")
			messy_f = rom_dir / "Unrecognized [0100BBBBBBBB0000].nsp"
			messy_f.write_bytes(b"x")

			fake_results = {
				str(already_correct_f): ("0100EFD00A4FA000", 0, rr.CONTENT_META_TYPE_APPLICATION),
				str(messy_f): ("0100BBBBBBBB0000", 0, rr.CONTENT_META_TYPE_APPLICATION),
			}
			orig = rr.extract_real_id_version
			rr.extract_real_id_version = lambda path, h, k: fake_results[str(path)]
			try:
				cache = {}
				stats, undo_log = rr.process_dirs(
					[rom_dir], dry_run=False, recursive=False,
					hactool_path=Path("unused"), keys_path=Path("unused"),
					cache=cache,
				)
			finally:
				rr.extract_real_id_version = orig

			# The already-correct file is cached under its untouched path.
			self.assertIn(str(already_correct_f), cache)
			self.assertEqual(cache[str(already_correct_f)]["title_id"], "0100EFD00A4FA000")

			# The renamed file is cached under its NEW path, not the old one.
			new_messy_path = rom_dir / "Unrecognized [0100BBBBBBBB0000] [BASE][v0].nsp"
			self.assertIn(str(new_messy_path), cache)
			self.assertNotIn(str(messy_f), cache)
			self.assertEqual(cache[str(new_messy_path)]["base_id"], "0100BBBBBBBB0000")

	def test_dry_run_pending_rename_is_not_cached(self):
		# A file dry-run determined needs a rename isn't actually correct
		# yet -- caching it (under either the old or a not-yet-real new
		# path) would let a later fast run wrongly trust it.
		with tempfile.TemporaryDirectory() as d:
			rom_dir = Path(d) / "roms"
			rom_dir.mkdir()
			f = rom_dir / "Unrecognized [0100BBBBBBBB0000].nsp"
			f.write_bytes(b"x")

			orig = rr.extract_real_id_version
			rr.extract_real_id_version = lambda path, h, k: ("0100BBBBBBBB0000", 0, rr.CONTENT_META_TYPE_APPLICATION)
			try:
				cache = {}
				rr.process_dirs(
					[rom_dir], dry_run=True, recursive=False,
					hactool_path=Path("unused"), keys_path=Path("unused"),
					cache=cache,
				)
			finally:
				rr.extract_real_id_version = orig

			self.assertEqual(cache, {})

	def test_cache_hit_base_can_still_donate_name_to_new_sibling(self):
		# Closes the gap a naive "exclude fast-skipped files from grouping
		# entirely" design would have: a BASE file that's a cache hit (so
		# its OWN id/version is never re-verified) can still act as the
		# Phase 2 donor for a sibling DLC that genuinely needs a name,
		# using its cached base_id -- no re-verification of the BASE
		# file's own id/version required for that to work.
		with tempfile.TemporaryDirectory() as d:
			rom_dir = Path(d) / "roms"
			rom_dir.mkdir()
			base_f = rom_dir / "Some Game [0100AAAA00000000] [BASE][v0].nsp"
			base_f.write_bytes(b"x")
			dlc_f = rom_dir / "Unrecognized [0100AAAA00000001].nsp"
			dlc_f.write_bytes(b"x")

			cache = {str(base_f): rr.cache_fingerprint(
				base_f, "0100AAAA00000000", 0, rr.ContentType.BASE, "0100AAAA00000000",
			)}

			def fails_if_base(path, hactool_path, keys_path):
				raise AssertionError("BASE file's id/version must not be re-verified on a cache hit")
			def dlc_id_version(path, hactool_path, keys_path):
				return ("0100AAAA00000001", 0, rr.CONTENT_META_TYPE_ADD_ON_CONTENT)

			orig_id = rr.extract_real_id_version
			orig_base_id = rr.extract_real_base_id
			orig_content = rr.extract_content_entries
			orig_name = rr.extract_title_name
			rr.extract_real_id_version = lambda path, h, k: (
				fails_if_base(path, h, k) if path == base_f else dlc_id_version(path, h, k)
			)
			rr.extract_real_base_id = lambda path, h, k, title_id, meta_type: "0100AAAA00000000"
			rr.extract_content_entries = lambda path, h, k: []
			rr.extract_title_name = lambda path, h, k, entries: (
				"Resolved Shared Title" if str(path) == str(base_f) else None
			)
			try:
				stats, undo_log = rr.process_dirs(
					[rom_dir], dry_run=True, recursive=False,
					hactool_path=Path("unused"), keys_path=Path("unused"),
					cache=cache,
				)
			finally:
				rr.extract_real_id_version = orig_id
				rr.extract_real_base_id = orig_base_id
				rr.extract_content_entries = orig_content
				rr.extract_title_name = orig_name

			self.assertEqual(stats["names_resolved"], 1)
			self.assertEqual(stats["fixed"], 1)
			# base_f itself is untouched (its cached name was already correct).
			self.assertTrue(base_f.exists())

	def test_fully_cached_title_group_never_calls_name_resolution(self):
		# When every file sharing a base_id is a cache hit, nothing in
		# that group needs a name -- resolving one anyway would be a pure
		# wasted hactool call.
		with tempfile.TemporaryDirectory() as d:
			rom_dir = Path(d) / "roms"
			rom_dir.mkdir()
			base_f = rom_dir / "Some Game [0100AAAA00000000] [BASE][v0].nsp"
			base_f.write_bytes(b"x")
			upd_f = rom_dir / "Some Game [0100AAAA00000800] [UPDATE][v65536].nsp"
			upd_f.write_bytes(b"x")

			cache = {
				str(base_f): rr.cache_fingerprint(base_f, "0100AAAA00000000", 0, rr.ContentType.BASE, "0100AAAA00000000"),
				str(upd_f): rr.cache_fingerprint(upd_f, "0100AAAA00000800", 65536, rr.ContentType.UPDATE, "0100AAAA00000000"),
			}

			def fails_if_called(*a, **k):
				raise AssertionError("no hactool call should be needed for a fully cache-hit title group")

			orig_id = rr.extract_real_id_version
			orig_content = rr.extract_content_entries
			orig_name = rr.extract_title_name
			rr.extract_real_id_version = fails_if_called
			rr.extract_content_entries = fails_if_called
			rr.extract_title_name = fails_if_called
			try:
				stats, undo_log = rr.process_dirs(
					[rom_dir], dry_run=True, recursive=False,
					hactool_path=Path("unused"), keys_path=Path("unused"),
					cache=cache,
				)
			finally:
				rr.extract_real_id_version = orig_id
				rr.extract_content_entries = orig_content
				rr.extract_title_name = orig_name

			self.assertEqual(stats["fast_skipped"], 2)
			self.assertEqual(stats["names_resolved"], 0)


class TestProcessDirsNameResolution(unittest.TestCase):
	"""Proves the priority-based name resolution design: a BASE title's
	resolved name reaches its UPDATE/DLC siblings regardless of file
	processing order, a non-BASE resolution failure never blocks a later
	BASE resolution, DLC-only libraries can still self-resolve, DLC gets
	its own distinct name when available, and a resolved name always
	overwrites whatever name text was already there."""

	def test_base_resolves_name_and_updates_dlc_reuse_it(self):
		with tempfile.TemporaryDirectory() as d:
			rom_dir = Path(d) / "roms"
			rom_dir.mkdir()
			base_f = rom_dir / "Unrecognized [0100AAAAAAAA0000].nsp"
			base_f.write_bytes(b"x")
			upd_f = rom_dir / "Unrecognized [0100AAAAAAAA0800].nsp"
			upd_f.write_bytes(b"x")
			dlc_f = rom_dir / "Unrecognized - Unrecognized [0100AAAAAAAA0001].nsp"
			dlc_f.write_bytes(b"x")

			fake_id_version = {
				str(base_f): ("0100AAAAAAAA0000", 0, rr.CONTENT_META_TYPE_APPLICATION),
				str(upd_f): ("0100AAAAAAAA0800", 65536, rr.CONTENT_META_TYPE_PATCH),
				str(dlc_f): ("0100AAAAAAAA0001", 0, rr.CONTENT_META_TYPE_ADD_ON_CONTENT),
			}

			orig_id = rr.extract_real_id_version
			orig_content = rr.extract_content_entries
			orig_name = rr.extract_title_name

			def fake_id(path, hactool_path, keys_path):
				return fake_id_version[str(path)]

			def fake_content(path, hactool_path, keys_path):
				return []  # contents don't matter -- extract_title_name is mocked directly

			def fake_name(path, hactool_path, keys_path, content_entries):
				# Only the BASE file's own Control NCA "resolves" -- the
				# common real-world case where updates/DLC don't carry one.
				return "Cool Game" if str(path) == str(base_f) else None

			rr.extract_real_id_version = fake_id
			rr.extract_content_entries = fake_content
			rr.extract_title_name = fake_name
			try:
				stats, undo_log = rr.process_dirs(
					[rom_dir], dry_run=False, recursive=False,
					hactool_path=Path("unused"), keys_path=Path("unused"),
				)
			finally:
				rr.extract_real_id_version = orig_id
				rr.extract_content_entries = orig_content
				rr.extract_title_name = orig_name

			self.assertEqual(stats["names_resolved"], 1)
			names = sorted(p.name for p in rom_dir.iterdir())
			self.assertIn("Cool Game [0100AAAAAAAA0000] [BASE][v0].nsp", names)
			self.assertIn("Cool Game [0100AAAAAAAA0800] [UPDATE][v65536].nsp", names)
			# DLC's own name didn't resolve, and its old text was just the
			# "Unrecognized - Unrecognized" placeholder -- no dlc_name tacked on.
			self.assertIn("Cool Game [0100AAAAAAAA0001] [DLC][v0].nsp", names)

	def test_non_placeholder_name_is_overwritten_when_resolved(self):
		# A resolved name always wins over the existing filename text, even
		# when that text isn't an obvious placeholder -- e.g. a slug-style
		# name like "v-dispatch_hr_violations_pack_dlc" from some other
		# tool. Reported directly against a real DLC in this library.
		with tempfile.TemporaryDirectory() as d:
			rom_dir = Path(d) / "roms"
			rom_dir.mkdir()
			f = rom_dir / "Actual Game Name [0100BBBBBBBB0000].nsp"
			f.write_bytes(b"x")

			orig_id = rr.extract_real_id_version
			orig_content = rr.extract_content_entries
			orig_name = rr.extract_title_name
			rr.extract_real_id_version = lambda path, h, k: ("0100BBBBBBBB0000", 0, rr.CONTENT_META_TYPE_APPLICATION)
			rr.extract_content_entries = lambda path, h, k: []
			rr.extract_title_name = lambda path, h, k, entries: "Some Other Resolved Name"
			try:
				stats, undo_log = rr.process_dirs(
					[rom_dir], dry_run=False, recursive=False,
					hactool_path=Path("unused"), keys_path=Path("unused"),
				)
			finally:
				rr.extract_real_id_version = orig_id
				rr.extract_content_entries = orig_content
				rr.extract_title_name = orig_name

			names = sorted(p.name for p in rom_dir.iterdir())
			self.assertIn("Some Other Resolved Name [0100BBBBBBBB0000] [BASE][v0].nsp", names)
			self.assertEqual(stats["fixed"], 1)
			self.assertEqual(stats["already_correct"], 0)

	def test_no_base_present_falls_back_to_first_non_base_with_control_nca(self):
		# DLC-only library (no BASE file present at all) -- the title's
		# name should still get resolved via the DLC's own Control NCA, and
		# the redundant "Title - Title" isn't produced when the DLC's own
		# name is identical to the (also-DLC-sourced) shared title.
		with tempfile.TemporaryDirectory() as d:
			rom_dir = Path(d) / "roms"
			rom_dir.mkdir()
			dlc_f = rom_dir / "some_slug_name_dlc.nsp"
			dlc_f.write_bytes(b"x")

			orig_id = rr.extract_real_id_version
			orig_content = rr.extract_content_entries
			orig_name = rr.extract_title_name
			rr.extract_real_id_version = lambda path, h, k: ("0100CCCCCCCC0001", 0, rr.CONTENT_META_TYPE_ADD_ON_CONTENT)
			rr.extract_content_entries = lambda path, h, k: []
			rr.extract_title_name = lambda path, h, k, entries: "Resolved DLC Name"
			try:
				stats, undo_log = rr.process_dirs(
					[rom_dir], dry_run=False, recursive=False,
					hactool_path=Path("unused"), keys_path=Path("unused"),
				)
			finally:
				rr.extract_real_id_version = orig_id
				rr.extract_content_entries = orig_content
				rr.extract_title_name = orig_name

			names = sorted(p.name for p in rom_dir.iterdir())
			self.assertIn("Resolved DLC Name [0100CCCCCCCC0001] [DLC][v0].nsp", names)
			self.assertEqual(stats["names_resolved"], 1)

	def test_failed_non_base_attempt_does_not_block_later_base_resolution(self):
		# Regression test for a real ordering bug: if a non-BASE file for a
		# title is processed (and its own resolution attempt fails) before
		# that title's BASE file, the BASE file must still get its own
		# attempt rather than inheriting a cached failure.
		with tempfile.TemporaryDirectory() as d:
			rom_dir = Path(d) / "roms"
			rom_dir.mkdir()
			# "AAA" sorts before "ZZZ" -- the DLC is processed first.
			dlc_f = rom_dir / "AAA_dlc_slug [0100DDDDDDDD0001].nsp"
			dlc_f.write_bytes(b"x")
			base_f = rom_dir / "ZZZ_base_slug [0100DDDDDDDD0000].nsp"
			base_f.write_bytes(b"x")

			fake_id_version = {
				str(dlc_f): ("0100DDDDDDDD0001", 0, rr.CONTENT_META_TYPE_ADD_ON_CONTENT),
				str(base_f): ("0100DDDDDDDD0000", 0, rr.CONTENT_META_TYPE_APPLICATION),
			}

			orig_id = rr.extract_real_id_version
			orig_content = rr.extract_content_entries
			orig_name = rr.extract_title_name
			rr.extract_real_id_version = lambda path, h, k: fake_id_version[str(path)]
			rr.extract_content_entries = lambda path, h, k: []
			# Only the BASE file can actually resolve a name -- the DLC's
			# own-name attempt (Phase 2b) also fails, so its old descriptive
			# text ("AAA dlc slug") is kept as a distinct dlc_name.
			rr.extract_title_name = lambda path, h, k, entries: (
				"Resolved Base Name" if str(path) == str(base_f) else None
			)
			try:
				stats, undo_log = rr.process_dirs(
					[rom_dir], dry_run=False, recursive=False,
					hactool_path=Path("unused"), keys_path=Path("unused"),
				)
			finally:
				rr.extract_real_id_version = orig_id
				rr.extract_content_entries = orig_content
				rr.extract_title_name = orig_name

			names = sorted(p.name for p in rom_dir.iterdir())
			self.assertIn("Resolved Base Name [0100DDDDDDDD0000] [BASE][v0].nsp", names)
			self.assertIn("Resolved Base Name [AAA dlc slug] [0100DDDDDDDD0001] [DLC][v0].nsp", names)

	def test_dlc_own_name_used_when_distinct_from_shared_title(self):
		# The common "good" case: the base game resolves a shared title,
		# AND this specific DLC has its own Control NCA with its own
		# (distinct) name -- both should show up in the final filename.
		with tempfile.TemporaryDirectory() as d:
			rom_dir = Path(d) / "roms"
			rom_dir.mkdir()
			base_f = rom_dir / "base.nsp"
			base_f.write_bytes(b"x")
			dlc_f = rom_dir / "dlc.nsp"
			dlc_f.write_bytes(b"x")

			fake_id_version = {
				str(base_f): ("0100EEEEEEEE0000", 0, rr.CONTENT_META_TYPE_APPLICATION),
				str(dlc_f): ("0100EEEEEEEE0001", 0, rr.CONTENT_META_TYPE_ADD_ON_CONTENT),
			}

			def fake_name(path, hactool_path, keys_path, content_entries):
				if str(path) == str(base_f):
					return "Cool Game"
				if str(path) == str(dlc_f):
					return "Bonus Pack"
				return None

			orig_id = rr.extract_real_id_version
			orig_content = rr.extract_content_entries
			orig_name = rr.extract_title_name
			rr.extract_real_id_version = lambda path, h, k: fake_id_version[str(path)]
			rr.extract_content_entries = lambda path, h, k: []
			rr.extract_title_name = fake_name
			try:
				stats, undo_log = rr.process_dirs(
					[rom_dir], dry_run=False, recursive=False,
					hactool_path=Path("unused"), keys_path=Path("unused"),
				)
			finally:
				rr.extract_real_id_version = orig_id
				rr.extract_content_entries = orig_content
				rr.extract_title_name = orig_name

			names = sorted(p.name for p in rom_dir.iterdir())
			self.assertIn("Cool Game [0100EEEEEEEE0000] [BASE][v0].nsp", names)
			self.assertIn("Cool Game [Bonus Pack] [0100EEEEEEEE0001] [DLC][v0].nsp", names)
			self.assertEqual(stats["dlc_names_resolved"], 1)

	def test_dlc_own_name_with_title_prefix_is_deduplicated(self):
		# Real-world bug reported against this exact library: some
		# publishers' DLC Control NCAs (e.g. Capcom Arcade 2nd Stadium)
		# report a name that's the base title PLUS the episode name run
		# together, not just the episode name alone. Using that raw text
		# as dlc_name would duplicate the title in the final filename.
		with tempfile.TemporaryDirectory() as d:
			rom_dir = Path(d) / "roms"
			rom_dir.mkdir()
			base_f = rom_dir / "base.nsp"
			base_f.write_bytes(b"x")
			dlc_f = rom_dir / "dlc.nsp"
			dlc_f.write_bytes(b"x")

			fake_id_version = {
				str(base_f): ("0100DC60167B5000", 0, rr.CONTENT_META_TYPE_APPLICATION),
				str(dlc_f): ("0100DC60167B5013", 0, rr.CONTENT_META_TYPE_ADD_ON_CONTENT),
			}

			def fake_name(path, hactool_path, keys_path, content_entries):
				if str(path) == str(base_f):
					return "Capcom Arcade 2nd Stadium"
				if str(path) == str(dlc_f):
					return "Capcom Arcade 2nd Stadium 1943 Kai Midway Kaisen"
				return None

			orig_id = rr.extract_real_id_version
			orig_content = rr.extract_content_entries
			orig_name = rr.extract_title_name
			rr.extract_real_id_version = lambda path, h, k: fake_id_version[str(path)]
			rr.extract_content_entries = lambda path, h, k: []
			rr.extract_title_name = fake_name
			try:
				stats, undo_log = rr.process_dirs(
					[rom_dir], dry_run=False, recursive=False,
					hactool_path=Path("unused"), keys_path=Path("unused"),
				)
			finally:
				rr.extract_real_id_version = orig_id
				rr.extract_content_entries = orig_content
				rr.extract_title_name = orig_name

			names = sorted(p.name for p in rom_dir.iterdir())
			self.assertIn("Capcom Arcade 2nd Stadium [0100DC60167B5000] [BASE][v0].nsp", names)
			self.assertIn(
				"Capcom Arcade 2nd Stadium [1943 Kai Midway Kaisen] [0100DC60167B5013] [DLC][v0].nsp",
				names,
			)

	def test_dlc_with_non_prefix_sharing_id_still_reaches_shared_name(self):
		# Regression test for a real bug: this title's DLC ids don't share
		# the base game's id prefix at all (base "...B4000", DLC
		# "...B5013") -- the OLD base_id-from-title-id guess
		# (title_id[:-3] + "000") would compute "...B5000" for the DLC, a
		# base id that doesn't exist in the library, so the DLC's record
		# never found the BASE file's resolved name and process_dirs fell
		# all the way back to reusing the (duplicated) old filename text
		# as if it were the title -- the DLC was never actually renamed
		# because the resulting name happened to already match the old
		# (broken) filename on disk. extract_real_base_id (reading the
		# ApplicationId straight from the DLC's own CNMT) must be used
		# instead of the guess for this to resolve correctly.
		with tempfile.TemporaryDirectory() as d:
			rom_dir = Path(d) / "roms"
			rom_dir.mkdir()
			base_f = rom_dir / "Unrecognized [0100DC60167B4000] [BASE][v0].nsp"
			base_f.write_bytes(b"x")
			dlc_f = rom_dir / "Capcom Arcade 2nd Stadium Capcom Arcade 2nd Stadium 1943 Kai Midway Kaisen [0100DC60167B5013] [DLC][v0].nsz"
			dlc_f.write_bytes(b"x")

			fake_id_version = {
				str(base_f): ("0100DC60167B4000", 0, rr.CONTENT_META_TYPE_APPLICATION),
				str(dlc_f): ("0100DC60167B5013", 0, rr.CONTENT_META_TYPE_ADD_ON_CONTENT),
			}

			def fake_base_id(path, hactool_path, keys_path, title_id, meta_type):
				# Simulates reading the real ApplicationId out of the CNMT:
				# both files correctly point back at the BASE's real id,
				# even though the DLC's own title id doesn't share its
				# prefix.
				return "0100DC60167B4000"

			orig_id = rr.extract_real_id_version
			orig_base_id = rr.extract_real_base_id
			orig_content = rr.extract_content_entries
			orig_name = rr.extract_title_name
			rr.extract_real_id_version = lambda path, h, k: fake_id_version[str(path)]
			rr.extract_real_base_id = fake_base_id
			rr.extract_content_entries = lambda path, h, k: []
			# Only the BASE resolves a name; this DLC has no Control NCA
			# of its own, matching the real title in this library.
			rr.extract_title_name = lambda path, h, k, entries: (
				"Capcom Arcade 2nd Stadium" if str(path) == str(base_f) else None
			)
			try:
				stats, undo_log = rr.process_dirs(
					[rom_dir], dry_run=False, recursive=False,
					hactool_path=Path("unused"), keys_path=Path("unused"),
				)
			finally:
				rr.extract_real_id_version = orig_id
				rr.extract_real_base_id = orig_base_id
				rr.extract_content_entries = orig_content
				rr.extract_title_name = orig_name

			names = sorted(p.name for p in rom_dir.iterdir())
			self.assertIn("Capcom Arcade 2nd Stadium [0100DC60167B4000] [BASE][v0].nsp", names)
			self.assertIn(
				"Capcom Arcade 2nd Stadium [1943 Kai Midway Kaisen] [0100DC60167B5013] [DLC][v0].nsz",
				names,
			)
			self.assertEqual(stats["fixed"], 2)

	def test_rerun_over_already_canonical_dlc_name_does_not_drop_it(self):
		# Regression test for a real bug introduced alongside the
		# "[dlc_name]" bracket format: a DLC with no Control NCA of its own
		# (the common case) falls back to reusing whatever descriptive
		# text was already in its filename. On an ALREADY-canonical file
		# ("Title [DlcName] [id] [DLC][vN]"), the old fallback parser
		# treated "[DlcName]" as just another tag to discard (same as
		# "[DLC]" itself), so a second run silently stripped the name
		# entirely instead of leaving the file alone as already-correct --
		# this actually happened to real files in this library.
		with tempfile.TemporaryDirectory() as d:
			rom_dir = Path(d) / "roms"
			rom_dir.mkdir()
			base_f = rom_dir / "Capcom Arcade 2nd Stadium [0100DC60167B4000] [BASE][v0].nsp"
			base_f.write_bytes(b"x")
			dlc_f = rom_dir / "Capcom Arcade 2nd Stadium [1943 Kai Midway Kaisen] [0100DC60167B5013] [DLC][v0].nsz"
			dlc_f.write_bytes(b"x")

			fake_id_version = {
				str(base_f): ("0100DC60167B4000", 0, rr.CONTENT_META_TYPE_APPLICATION),
				str(dlc_f): ("0100DC60167B5013", 0, rr.CONTENT_META_TYPE_ADD_ON_CONTENT),
			}

			orig_id = rr.extract_real_id_version
			orig_base_id = rr.extract_real_base_id
			orig_content = rr.extract_content_entries
			orig_name = rr.extract_title_name
			rr.extract_real_id_version = lambda path, h, k: fake_id_version[str(path)]
			rr.extract_real_base_id = lambda path, h, k, tid, mt: "0100DC60167B4000"
			rr.extract_content_entries = lambda path, h, k: []
			# Same as the real title: only the BASE resolves a name; the
			# DLC has no Control NCA of its own on repeat attempts either.
			rr.extract_title_name = lambda path, h, k, entries: (
				"Capcom Arcade 2nd Stadium" if str(path) == str(base_f) else None
			)
			try:
				stats, undo_log = rr.process_dirs(
					[rom_dir], dry_run=False, recursive=False,
					hactool_path=Path("unused"), keys_path=Path("unused"),
				)
			finally:
				rr.extract_real_id_version = orig_id
				rr.extract_real_base_id = orig_base_id
				rr.extract_content_entries = orig_content
				rr.extract_title_name = orig_name

			names = sorted(p.name for p in rom_dir.iterdir())
			self.assertIn(
				"Capcom Arcade 2nd Stadium [1943 Kai Midway Kaisen] [0100DC60167B5013] [DLC][v0].nsz",
				names,
			)
			self.assertEqual(stats["already_correct"], 2)
			self.assertEqual(stats["fixed"], 0)

	def test_unknown_content_type_has_no_version_in_filename(self):
		with tempfile.TemporaryDirectory() as d:
			rom_dir = Path(d) / "roms"
			rom_dir.mkdir()
			f = rom_dir / "SystemThing.nsp"
			f.write_bytes(b"x")

			orig_id = rr.extract_real_id_version
			orig_content = rr.extract_content_entries
			orig_name = rr.extract_title_name
			# 0x01 == SystemProgram -- not Application/Patch/AddOnContent.
			rr.extract_real_id_version = lambda path, h, k: ("0100FFFFFFFF0001", 5, 0x01)
			rr.extract_content_entries = lambda path, h, k: []
			rr.extract_title_name = lambda path, h, k, entries: None
			try:
				stats, undo_log = rr.process_dirs(
					[rom_dir], dry_run=False, recursive=False,
					hactool_path=Path("unused"), keys_path=Path("unused"),
				)
			finally:
				rr.extract_real_id_version = orig_id
				rr.extract_content_entries = orig_content
				rr.extract_title_name = orig_name

			names = sorted(p.name for p in rom_dir.iterdir())
			self.assertIn("SystemThing [0100FFFFFFFF0001] [UNKNOWN].nsp", names)


if __name__ == "__main__":
	unittest.main()
