#!/usr/bin/env python3
"""
Stress Test: Complex Real-World Windows Filenames
=================================================
Tests encryption and decryption on tricky, complex, and international Windows filenames:
1. Spaces, brackets, parentheses, semicolons, ampersands, commas, plus, hashes.
2. Multi-language Unicode: Japanese, Chinese, Korean, Cyrillic, Arabic, French, German, Spanish.
3. Emojis, currency, math symbols.
4. Multiple dots, leading dots (.gitignore), multiple extensions (.tar.gz).
5. Extremely long scene-release filenames.
6. Subfolder hierarchy.
7. Validates:
   - Windows filename legality (no illegal chars, valid length <= 255).
   - Atomic rename to encrypted name.
   - Decryption using Custom Password.
   - Decryption using Sparse Hash (no password).
   - Byte-for-byte filename matching.
   - Byte-for-byte file content matching.
"""

import os
import sys
import shutil
import hashlib
import tempfile
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Ensure current directory is in sys.path
script_dir = str(Path(__file__).resolve().parent)
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

import filename_encryptor

# Windows illegal characters
BANNED_CHARS = set('<>:"/\\|?*')


def is_valid_windows_filename(name: str) -> bool:
    """Checks if a filename is valid on Windows."""
    if not name or len(name) > 255:
        return False
    if any(c in BANNED_CHARS for c in name):
        return False
    if any(ord(c) < 32 for c in name):
        return False
    if name.endswith(" ") or name.endswith("."):
        return False
    return True


def run_test_suite():
    temp_dir = tempfile.mkdtemp(prefix="hf_complex_test_")
    print(f"Creating complex test folder at:\n  {temp_dir}\n")

    test_files_def = [
        # 1. Punctuation, spaces & brackets
        ("Notes & Secrets (2024-2025); Vol. #1 + Bonus [100% Verified].pdf", b"PDF Document content 1010101"),
        ("O'Connor & Sons - Annual Audit Report, v2.0 {Final}.xlsx", b"Spreadsheet data with formulas"),
        ("[HorribleSubs] Shingeki no Kyojin - 59 [1080p] (v2) ~Final~.mkv", b"Video stream bytes " * 1000),

        # 2. International scripts & accents
        ("【推しの子】第01話 「Mother and Children」 (1080p).mkv", b"Japanese anime video data"),
        ("卧虎藏龙 (Crouching Tiger, Hidden Dragon) 4K.HDR.mkv", b"Chinese movie video data"),
        ("기생충 Parasite (2019) BDRip.x265.mkv", b"Korean movie video data"),
        ("Сталкер (Stalker, 1979) Remastered [1080p].mkv", b"Russian classic cinema data"),
        ("تقرير_مشروع_2026_النهائي.docx", b"Arabic document text bytes"),
        ("Crème Brûlée, Über-Größe & Niño (2024).txt", b"Accented latin text: e, u, n"),

        # 3. Emojis, math & currency
        ("🎬 Movie Night! 🍿🔥 (Director's Cut).mp4", b"Emoji test video data " * 500),
        ("Financial Statement €1,500,000 & ¥20,000,000 ± 5.0%.csv", b"Currency figures 1500000 2000000"),

        # 4. Multiple dots & extensions
        ("backup.daily.2026-10-25.final.tar.gz", b"Archive gzip data 11223344"),
        ("data.bundle.min.js.map", b"Source map JSON data"),
        (".env.production.local", b"Environment variables data"),

        # 5. Short & very long filenames
        ("a.x", b"Tiny filename single char"),
        (
            "This.Is.An.Extremely.Long.Filename.To.Stress.Test.The.Filename.Encryption.Engine."
            "With.All.Sorts.Of.Tags.And.Metadata.2026.UHD.BluRay.2160p.TrueHD.Atmos.7.1.DV.HEVC.mkv",
            b"Long filename test payload " * 800,
        ),

        # 6. Subfolder hierarchy
        ("Season 01/Episode 01 - The Pilot [1080p].mp4", b"Season 1 episode 1 data"),
        ("Season 01/Subs/Episode 01 (English & Español).srt", b"Subtitle data: 00:01:00 --> 00:01:05"),
        ("Vault/Docs/Top-Secret & Confidential Notes.docx", b"Confidential document bytes"),
    ]

    # Create files on disk
    created_map = {}
    for rel_path, content in test_files_def:
        full_path = os.path.join(temp_dir, rel_path.replace("/", os.sep))
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "wb") as f:
            f.write(content)
        content_hash = hashlib.sha256(content).hexdigest()
        created_map[rel_path] = content_hash

    print(f"Successfully created {len(created_map)} complex test files on disk.")

    # =========================================================================
    # TEST ROUND 1: Using Custom Password
    # =========================================================================
    CUSTOM_PW = "SuperSecret#2026!Password"
    print("\n" + "=" * 70)
    print("TEST ROUND 1: Encryption & Decryption with CUSTOM PASSWORD")
    print(f"Password: '{CUSTOM_PW}'")
    print("=" * 70)

    # 1. Encrypt with custom password
    enc_stats = filename_encryptor.process_folder(
        folder_path=temp_dir,
        mode="encrypt",
        recursive=True,
        secret_key=CUSTOM_PW,
        dry_run=False,
    )
    print(f"Encrypt result: Total={enc_stats['total']}, Success={enc_stats['success']}, Failed={enc_stats['failed']}")
    assert enc_stats["failed"] == 0, "Round 1 Encrypt had failures!"
    assert enc_stats["success"] == len(created_map), "Not all files encrypted in Round 1!"

    # 2. Verify all encrypted filenames are valid Windows filenames
    print("\nVerifying encrypted filenames validity on Windows:")
    for root, _, files in os.walk(temp_dir):
        for f in files:
            assert f.endswith(".encname"), f"Unexpected non-encrypted file: {f}"
            assert is_valid_windows_filename(f), f"ILLEGAL Windows filename: {f}"
            assert len(f) <= 255, f"Filename exceeds 255 chars: {len(f)}"
            # Print sample
            rel = os.path.relpath(os.path.join(root, f), temp_dir)
            print(f"  [VALID WINDOWS FILENAME: {len(f)} chars] {rel}")

    # 3. Test wrong password failure (must fail safely without modifying files)
    print("\nTesting wrong password rejection:")
    wrong_stats = filename_encryptor.process_folder(
        folder_path=temp_dir,
        mode="decrypt",
        recursive=True,
        secret_key="WrongPassword123",
        dry_run=False,
    )
    print(f"Wrong password result: Failed={wrong_stats['failed']}, Success={wrong_stats['success']}")
    assert wrong_stats["success"] == 0, "Decrypted with wrong password unexpectedly!"
    assert wrong_stats["failed"] == len(created_map), "Expected all files to fail with wrong password!"

    # 4. Decrypt with correct custom password
    print("\nDecrypting with correct custom password:")
    dec_stats = filename_encryptor.process_folder(
        folder_path=temp_dir,
        mode="decrypt",
        recursive=True,
        secret_key=CUSTOM_PW,
        dry_run=False,
    )
    print(f"Decrypt result: Total={dec_stats['total']}, Success={dec_stats['success']}, Failed={dec_stats['failed']}")
    assert dec_stats["failed"] == 0, "Round 1 Decrypt had failures!"
    assert dec_stats["success"] == len(created_map), "Not all files decrypted in Round 1!"

    # 5. Verify byte-for-byte filename and content matching
    print("\nVerifying 100% original filename and content integrity:")
    for rel_path, expected_hash in created_map.items():
        full_path = os.path.join(temp_dir, rel_path.replace("/", os.sep))
        assert os.path.exists(full_path), f"File missing after decryption: {rel_path}"
        with open(full_path, "rb") as f:
            actual_hash = hashlib.sha256(f.read()).hexdigest()
        assert actual_hash == expected_hash, f"Content corrupted in {rel_path}!"
        print(f"  [100% MATCH] {rel_path}")

    print("\n>>> ROUND 1 (CUSTOM PASSWORD) PASSED PERFECTLY! <<<")

    # =========================================================================
    # TEST ROUND 2: Using File Sparse Content Hash (No Password)
    # =========================================================================
    print("\n" + "=" * 70)
    print("TEST ROUND 2: Encryption & Decryption with SPARSE HASH ALONE (No Password)")
    print("=" * 70)

    # 1. Encrypt with sparse hash alone (secret_key="")
    enc_stats2 = filename_encryptor.process_folder(
        folder_path=temp_dir,
        mode="encrypt",
        recursive=True,
        secret_key="",
        dry_run=False,
    )
    print(f"Encrypt result: Total={enc_stats2['total']}, Success={enc_stats2['success']}, Failed={enc_stats2['failed']}")
    assert enc_stats2["failed"] == 0, "Round 2 Encrypt had failures!"
    assert enc_stats2["success"] == len(created_map), "Not all files encrypted in Round 2!"

    # 2. Decrypt with sparse hash alone (secret_key="")
    print("\nDecrypting with sparse hash alone:")
    dec_stats2 = filename_encryptor.process_folder(
        folder_path=temp_dir,
        mode="decrypt",
        recursive=True,
        secret_key="",
        dry_run=False,
    )
    print(f"Decrypt result: Total={dec_stats2['total']}, Success={dec_stats2['success']}, Failed={dec_stats2['failed']}")
    assert dec_stats2["failed"] == 0, "Round 2 Decrypt had failures!"
    assert dec_stats2["success"] == len(created_map), "Not all files decrypted in Round 2!"

    # 3. Verify byte-for-byte filename and content matching
    print("\nVerifying 100% original filename and content integrity:")
    for rel_path, expected_hash in created_map.items():
        full_path = os.path.join(temp_dir, rel_path.replace("/", os.sep))
        assert os.path.exists(full_path), f"File missing after decryption: {rel_path}"
        with open(full_path, "rb") as f:
            actual_hash = hashlib.sha256(f.read()).hexdigest()
        assert actual_hash == expected_hash, f"Content corrupted in {rel_path}!"
        print(f"  [100% MATCH] {rel_path}")

    print("\n>>> ROUND 2 (SPARSE HASH ALONE) PASSED PERFECTLY! <<<")

    # Clean up
    shutil.rmtree(temp_dir)
    print("\n" + "=" * 70)
    print("ALL TESTS PASSED: 100% REVERSIBLE, 100% VALID WINDOWS FILENAMES!")
    print("=" * 70)


if __name__ == "__main__":
    run_test_suite()
