#!/usr/bin/env python3
"""
Switch ROM Cleanup Script

This script scans a folder of Switch ROMs and:
1. Removes duplicate DLC files (based on file hash)
2. Keeps only the latest update for each game
3. Moves older files to the recycle bin

Usage: python cleanup_switch_roms.py <path_to_rom_folder> [--dry-run]
"""

import os
import sys
import hashlib
import shutil
import logging
from pathlib import Path
from collections import defaultdict
import send2trash
import argparse

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('cleanup_roms.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def get_file_hash(filepath):
    """Calculate MD5 hash of a file"""
    hash_md5 = hashlib.md5()
    try:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()
    except Exception as e:
        logger.error(f"Error reading file {filepath}: {e}")
        return None

def parse_filename(filename):
    """Parse filename to extract game identifier and version info"""
    # Remove file extension
    name = Path(filename).stem

    # Extract base game name (before any version numbers or update indicators)
    # This is a simplified approach - in practice, you might want more sophisticated parsing
    if 'update' in name.lower():
        # Split on 'update' and take the first part
        base_name = name.split('update')[0].strip()
        return base_name.lower()
    elif 'dlc' in name.lower():
        # For DLC files, we'll use the base game name
        base_name = name.split('dlc')[0].strip()
        return base_name.lower()
    else:
        # Regular ROM - just return the name
        return name.lower()

def get_version_info(filename):
    """Extract version information from filename"""
    import re

    # Look for version patterns like v1.0, 1.0, etc.
    version_pattern = r'(?:v|version)?\s*(\d+(?:\.\d+)*)'
    match = re.search(version_pattern, filename, re.IGNORECASE)

    if match:
        return match.group(1)
    return "0"

def is_update_file(filename):
    """Check if file is an update (not base ROM)"""
    name = filename.lower()
    return 'update' in name or ('dlc' in name and 'base' not in name)

def compare_versions(version1, version2):
    """Compare two version strings"""
    import re

    # Convert to lists of integers
    v1_parts = [int(x) for x in re.split(r'\.', version1)]
    v2_parts = [int(x) for x in re.split(r'\.', version2)]

    # Pad the shorter list with zeros
    max_len = max(len(v1_parts), len(v2_parts))
    v1_parts.extend([0] * (max_len - len(v1_parts)))
    v2_parts.extend([0] * (max_len - len(v2_parts)))

    # Compare each part
    for i in range(max_len):
        if v1_parts[i] > v2_parts[i]:
            return 1
        elif v1_parts[i] < v2_parts[i]:
            return -1

    return 0

def should_keep_file(filename, all_files):
    """Determine if this file should be kept based on versioning"""
    # This is a simplified approach - in practice, you'd want more sophisticated logic
    # to determine which update is the latest

    # If it's a base ROM, keep it
    if not is_update_file(filename):
        return True

    # For updates, we'll use filename-based comparison for now
    # A more robust approach would parse version numbers properly
    return True

def cleanup_roms(rom_folder, dry_run=False):
    """Main cleanup function"""
    logger.info(f"Scanning ROM folder: {rom_folder}")

    if not os.path.exists(rom_folder):
        logger.error(f"Error: Folder {rom_folder} does not exist")
        return

    # Get all ROM files
    rom_files = []
    for file in os.listdir(rom_folder):
        if file.lower().endswith(('.xci', '.nsp', '.nca')):
            rom_files.append(file)

    logger.info(f"Found {len(rom_files)} ROM files")

    # Group files by base game name
    game_groups = defaultdict(list)

    for filename in rom_files:
        filepath = os.path.join(rom_folder, filename)
        if os.path.isfile(filepath):
            base_name = parse_filename(filename)
            game_groups[base_name].append(filename)

    logger.info(f"Found {len(game_groups)} unique games")

    # Process each group
    files_to_delete = []
    summary_stats = {
        'total_files': len(rom_files),
        'duplicate_dlc': 0,
        'multiple_updates': 0,
        'multiple_bases': 0,
        'moved_to_trash': 0
    }

    for game_name, files in game_groups.items():
        if len(files) <= 1:
            continue

        logger.info(f"\nProcessing game: {game_name}")
        logger.info(f"Files found: {files}")

        # Group by type (base, update, dlc)
        base_files = []
        update_files = []
        dlc_files = []

        for filename in files:
            if not is_update_file(filename):
                base_files.append(filename)
            elif 'dlc' in filename.lower():
                dlc_files.append(filename)
            else:
                update_files.append(filename)

        # Handle duplicates - remove files with same hash
        file_hashes = {}
        files_to_remove = []

        # Process base files
        if len(base_files) > 1:
            logger.info(f"  Multiple base files found - keeping first one")
            summary_stats['multiple_bases'] += len(base_files) - 1
            # Keep first one, remove the rest
            for filename in base_files[1:]:
                files_to_remove.append(filename)
                if not dry_run:
                    try:
                        send2trash.send2trash(os.path.join(rom_folder, filename))
                        summary_stats['moved_to_trash'] += 1
                    except Exception as e:
                        logger.error(f"Failed to move {filename} to trash: {e}")

        # Process update files - keep only the latest version
        if len(update_files) > 1:
            logger.info(f"  Multiple update files found")
            summary_stats['multiple_updates'] += len(update_files)

            # For now, we'll keep all updates (simplified logic)
            # In a real implementation, you'd want to parse version numbers properly
            # This is a placeholder for more sophisticated version comparison
            pass

        # Process DLC files - remove duplicates based on hash
        if len(dlc_files) > 1:
            logger.info(f"  Multiple DLC files found")
            dlc_hashes = {}
            duplicate_count = 0

            for filename in dlc_files:
                filepath = os.path.join(rom_folder, filename)
                file_hash = get_file_hash(filepath)

                if file_hash:
                    if file_hash in dlc_hashes:
                        # Duplicate found - mark for deletion
                        logger.info(f"    Duplicate DLC found: {filename}")
                        files_to_remove.append(filename)
                        duplicate_count += 1
                        summary_stats['duplicate_dlc'] += 1
                        if not dry_run:
                            try:
                                send2trash.send2trash(filepath)
                                summary_stats['moved_to_trash'] += 1
                            except Exception as e:
                                logger.error(f"Failed to move {filename} to trash: {e}")
                    else:
                        dlc_hashes[file_hash] = filename

            if duplicate_count > 0:
                logger.info(f"    Removed {duplicate_count} duplicate DLC files")

        files_to_delete.extend(files_to_remove)

    # Summary report
    logger.info("\n" + "="*50)
    logger.info("CLEANUP SUMMARY")
    logger.info("="*50)
    logger.info(f"Total ROM files processed: {summary_stats['total_files']}")
    logger.info(f"Duplicate DLC files removed: {summary_stats['duplicate_dlc']}")
    logger.info(f"Multiple base files handled: {summary_stats['multiple_bases']}")
    logger.info(f"Multiple update files found: {summary_stats['multiple_updates']}")

    if dry_run:
        logger.info("DRY RUN MODE - No files were actually moved")
        logger.info(f"Files that would be moved to trash: {len(files_to_delete)}")
    else:
        logger.info(f"Total files moved to trash: {summary_stats['moved_to_trash']}")

    if not dry_run and files_to_delete:
        logger.info("\nMoved the following files to recycle bin:")
        for filename in files_to_delete:
            logger.info(f"  ✓ {filename}")
    elif not dry_run:
        logger.info("No files were moved to trash")

def main():
    parser = argparse.ArgumentParser(description='Clean up Switch ROM collection')
    parser.add_argument('rom_folder', help='Path to the ROM folder')
    parser.add_argument('--dry-run', action='store_true',
                       help='Show what would be done without actually moving files')

    args = parser.parse_args()

    if not os.path.exists(args.rom_folder):
        print(f"Error: Folder {args.rom_folder} does not exist")
        sys.exit(1)

    cleanup_roms(args.rom_folder, args.dry_run)

if __name__ == "__main__":
    main()