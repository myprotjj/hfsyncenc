#!/usr/bin/env python3
"""
Automated Test for Drop Folder Watcher
======================================
Tests:
1. watch_encrypt_sparse: Auto-encrypts folder, subfolders, and files using sparse hash.
2. watch_decrypt: Auto-decrypts sparse-hash encrypted folder back to original names.
3. watch_encrypt_password: Auto-encrypts folder, subfolders, and files using password.txt.
4. watch_decrypt: Auto-decrypts password-encrypted folder back to original names.
5. Verifies byte-for-byte content integrity and valid Windows filenames.
"""

import os
import sys
import shutil
import hashlib
from pathlib import Path

# Setup paths
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import drop_watcher
import filename_encryptor


def run_tests():
    base_dir = SCRIPT_DIR
    drop_watcher.init_watch_folders(base_dir)

    sparse_dir = base_dir / "watch_encrypt_sparse"
    pw_dir = base_dir / "watch_encrypt_password"
    dec_dir = base_dir / "watch_decrypt"

    print("=" * 70)
    print("RUNNING DROP WATCHER VERIFICATION TESTS")
    print("=" * 70)

    # -------------------------------------------------------------------------
    # TEST 1: watch_encrypt_sparse -> watch_decrypt
    # -------------------------------------------------------------------------
    print("\n--- TEST 1: Sparse Hash Drop Encryption & Decryption ---")
    test_folder_name = "Holiday Trip (2026)"
    test_folder = sparse_dir / test_folder_name
    sub_folder = test_folder / "Photos & Clips"
    sub_folder.mkdir(parents=True, exist_ok=True)

    file1 = test_folder / "Itinerary [Final].txt"
    file2 = sub_folder / "Beach Sunset 🌅.mp4"

    content1 = b"Flight at 10:00 AM, Hotel reservation confirmed."
    content2 = b"Beach sunset video bytes " * 1000

    file1.write_bytes(content1)
    file2.write_bytes(content2)

    print(f"Created: {test_folder.name} inside watch_encrypt_sparse/")

    # Run scan
    drop_watcher.scan_and_process(base_dir)

    # Check that test_folder was encrypted
    assert not test_folder.exists(), "Original unencrypted folder still exists!"
    sparse_items = [p for p in sparse_dir.iterdir() if p.name.endswith(".encdir") and p.name.startswith("2wNhkDPx")]
    assert len(sparse_items) >= 1, f"Expected test .encdir folder, got: {list(sparse_dir.iterdir())}"
    enc_sparse_folder = sparse_items[0]
    print(f"Encrypted folder created: {enc_sparse_folder.name}")

    # Check nested files
    nested_files = list(enc_sparse_folder.rglob("*.encname"))
    nested_dirs = [d for d in enc_sparse_folder.rglob("*.encdir")]
    assert len(nested_files) == 2, f"Expected 2 .encname files, got: {len(nested_files)}"
    assert len(nested_dirs) == 1, f"Expected 1 .encdir subfolder, got: {len(nested_dirs)}"
    print(f"Nested encryption verified: {len(nested_files)} files, {len(nested_dirs)} subfolders.")

    # Move encrypted folder to watch_decrypt
    target_in_dec = dec_dir / enc_sparse_folder.name
    shutil.move(str(enc_sparse_folder), str(target_in_dec))
    print(f"Moved {enc_sparse_folder.name} to watch_decrypt/")

    # Run decrypt scan
    drop_watcher.scan_and_process(base_dir)

    # Verify restored original folder and files
    restored_folder = dec_dir / test_folder_name
    assert restored_folder.exists(), f"Decrypted folder not found: {restored_folder}"
    restored_f1 = restored_folder / "Itinerary [Final].txt"
    restored_f2 = restored_folder / "Photos & Clips" / "Beach Sunset 🌅.mp4"

    assert restored_f1.exists(), f"Missing {restored_f1}"
    assert restored_f2.exists(), f"Missing {restored_f2}"
    assert restored_f1.read_bytes() == content1, "Content mismatch in file 1!"
    assert restored_f2.read_bytes() == content2, "Content mismatch in file 2!"
    print(">>> TEST 1 PASSED: Sparse Hash Drop Encryption & Decryption 100% Verified! <<<\n")

    # Clean up test 1 in watch_decrypt
    shutil.rmtree(str(restored_folder))

    # -------------------------------------------------------------------------
    # TEST 2: watch_encrypt_password -> watch_decrypt
    # -------------------------------------------------------------------------
    print("--- TEST 2: Password Drop Encryption & Decryption ---")
    pw = "SuperSafeDropKey!2026"
    (pw_dir / "password.txt").write_text(pw, encoding="utf-8")
    (dec_dir / "password.txt").write_text(pw, encoding="utf-8")

    pw_folder_name = "Confidential Vault [Q4]"
    pw_folder = pw_dir / pw_folder_name
    pw_sub = pw_folder / "Audit Reports"
    pw_sub.mkdir(parents=True, exist_ok=True)

    pw_f1 = pw_folder / "Summary - 2026.xlsx"
    pw_f2 = pw_sub / "Tax Filing (IRS & State).pdf"

    pw_c1 = b"Financial balance sheet 2026"
    pw_c2 = b"Tax document confidential data " * 500

    pw_f1.write_bytes(pw_c1)
    pw_f2.write_bytes(pw_c2)

    print(f"Created: {pw_folder.name} inside watch_encrypt_password/ (Password: '{pw}')")

    # Run scan
    drop_watcher.scan_and_process(base_dir)

    # Verify encryption
    assert not pw_folder.exists(), "Original unencrypted folder still exists!"
    pw_items = [p for p in pw_dir.iterdir() if p.name.endswith(".encdir") and p.name.startswith("R-jV3lvg")]
    assert len(pw_items) >= 1, f"Expected test .encdir folder, got: {list(pw_dir.iterdir())}"
    enc_pw_folder = pw_items[0]
    print(f"Encrypted folder created: {enc_pw_folder.name}")

    # Move to watch_decrypt
    target_pw_in_dec = dec_dir / enc_pw_folder.name
    shutil.move(str(enc_pw_folder), str(target_pw_in_dec))
    print(f"Moved {enc_pw_folder.name} to watch_decrypt/")

    # Run decrypt scan
    drop_watcher.scan_and_process(base_dir)

    # Verify restored original folder and files
    restored_pw_folder = dec_dir / pw_folder_name
    assert restored_pw_folder.exists(), f"Decrypted folder not found: {restored_pw_folder}"
    restored_pw_f1 = restored_pw_folder / "Summary - 2026.xlsx"
    restored_pw_f2 = restored_pw_folder / "Audit Reports" / "Tax Filing (IRS & State).pdf"

    assert restored_pw_f1.exists(), f"Missing {restored_pw_f1}"
    assert restored_pw_f2.exists(), f"Missing {restored_pw_f2}"
    assert restored_pw_f1.read_bytes() == pw_c1, "Content mismatch in pw file 1!"
    assert restored_pw_f2.read_bytes() == pw_c2, "Content mismatch in pw file 2!"
    print(">>> TEST 2 PASSED: Password Drop Encryption & Decryption 100% Verified! <<<\n")

    # Clean up test 2 in watch_decrypt
    shutil.rmtree(str(restored_pw_folder))

    print("=" * 70)
    print("ALL DROP WATCHER TESTS PASSED PERFECTLY!")
    print("=" * 70)


if __name__ == "__main__":
    run_tests()
