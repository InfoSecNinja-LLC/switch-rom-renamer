#!/usr/bin/env python3
"""
Unit tests for rename_roms.py.

Run with:  python -m unittest test_rename_roms -v

These tests do NOT require hactool.exe, keys, or any real ROM file --
they validate:
  - the pure PFS0/HFS0 container parser (against hand-built synthetic
    containers with known structure)
  - the raw CNMT parser (against a hand-built synthetic CNMT blob)
  - content-type-from-id and filename-rebuilding logic (pure string ops)
  - the end-to-end extract_real_id_version pipeline, with hactool itself
    replaced by a stub script (no real crypto -- just proves the
    orchestration/plumbing is correct: temp file creation, hactool
    invocation, output discovery, cleanup)
  - dry-run / apply / undo behavior on real (temporary) files on disk

The container parser (parse_pfs0/parse_hfs0/locate_meta_nca) was also
independently cross-checked during development against real ROM files in
this library: the exact byte range it located for the Meta NCA was
extracted and handed to an unrelated, independent NCA/CNMT decryptor,
which decrypted it successfully and returned the same title id/version
already known from the filename -- for both a .nsp and a .xci sample.
That proves the offset/size math is correct; these synthetic tests below
are a faithful, hactool-free stand-in for everyday regression testing.
"""

import json
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


def build_cnmt(title_id_hex: str, version: int, title_type: int = 0x80) -> bytes:
	tid_int = int(title_id_hex, 16)
	data = b""
	data += tid_int.to_bytes(8, "little")
	data += version.to_bytes(4, "little")
	data += bytes([title_type])   # meta type
	data += b"\x00"                # junk
	data += (0x10).to_bytes(2, "little")  # header offset
	data += (0).to_bytes(2, "little")     # content entry count
	data += (0).to_bytes(2, "little")     # meta entry count
	return data


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
		blob = build_cnmt("010059B017F9E800", 786432)
		title_id, version = rr.parse_cnmt(blob)
		self.assertEqual(title_id, "010059B017F9E800")
		self.assertEqual(version, 786432)

	def test_base_version_zero(self):
		blob = build_cnmt("0100EFD00A4FA000", 0)
		title_id, version = rr.parse_cnmt(blob)
		self.assertEqual(title_id, "0100EFD00A4FA000")
		self.assertEqual(version, 0)

	def test_too_short_raises(self):
		with self.assertRaises(rr.HactoolError):
			rr.parse_cnmt(b"\x00" * 4)


class TestContentTypeFromId(unittest.TestCase):
	def test_base(self):
		self.assertEqual(rr.content_type_from_id("0100EFD00A4FA000"), rr.ContentType.BASE)

	def test_update(self):
		self.assertEqual(rr.content_type_from_id("0100EFD00A4FA800"), rr.ContentType.UPDATE)

	def test_dlc(self):
		self.assertEqual(rr.content_type_from_id("010056901A4C9001"), rr.ContentType.DLC)


class TestBuildNewStem(unittest.TestCase):
	def test_base_no_existing_tags(self):
		new = rr.build_new_stem("Test Game", "0100EFD00A4FA000", 0, rr.ContentType.BASE)
		self.assertEqual(new, "Test Game [0100EFD00A4FA000]")

	def test_update_no_existing_tags(self):
		new = rr.build_new_stem("Test Game [UPD]", "0100EFD00A4FA800", 65536, rr.ContentType.UPDATE)
		self.assertEqual(new, "Test Game [UPD] [v65536] [0100EFD00A4FA800]")

	def test_replaces_human_version_and_wrong_id(self):
		old = "Test Game [UPD] [v1.0.5] [DEADBEEF00000800]"
		new = rr.build_new_stem(old, "0100EFD00A4FA800", 65536, rr.ContentType.UPDATE)
		self.assertEqual(new, "Test Game [UPD] [v65536] [0100EFD00A4FA800]")

	def test_replaces_wrong_id_only_no_version(self):
		# Skautfold-style: filename has a completely wrong ID, no version shown
		old = "Skautfold Bloody Pack [UPD] [010074701C2AE000]"
		new = rr.build_new_stem(old, "010015301AEA6800", 131072, rr.ContentType.UPDATE)
		self.assertEqual(new, "Skautfold Bloody Pack [UPD] [v131072] [010015301AEA6800]")

	def test_idempotent_when_already_correct(self):
		old = "Test Game [v65536] [0100EFD00A4FA800]"
		new = rr.build_new_stem(old, "0100EFD00A4FA800", 65536, rr.ContentType.UPDATE)
		self.assertEqual(new, old)

	def test_dlc_gets_version_tag_too(self):
		new = rr.build_new_stem("Test Game DLC", "010056901A4C9001", 0, rr.ContentType.DLC)
		self.assertEqual(new, "Test Game DLC [v0] [010056901A4C9001]")


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
			cnmt_bytes = build_cnmt("0100EFD00A4FA800", 65536)
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
				title_id, version = rr.extract_real_id_version(rom_path, tmp / "hactool.exe", keys_path)
			finally:
				rr.run_hactool_section0 = orig

			self.assertEqual(title_id, "0100EFD00A4FA800")
			self.assertEqual(version, 65536)


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
				str(f1): ("0100AAAAAAAA0800", 65536),
				str(f2): ("0100AAAAAAAA0000", 0),
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
			self.assertIn("Some Game [UPD] [v65536] [0100AAAAAAAA0800].nsp", names)
			self.assertIn("Some Game [0100AAAAAAAA0000].nsp", names)

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
			rr.extract_real_id_version = lambda path, h, k: ("0100AAAAAAAA0000", 0)
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
			rr.extract_real_id_version = lambda path, h, k: ("0100AAAAAAAA0000", 0)
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
				return ("0100AAAAAAAA0000", 0)

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


if __name__ == "__main__":
	unittest.main()
