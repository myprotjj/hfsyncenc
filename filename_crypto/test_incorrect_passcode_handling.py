#!/usr/bin/env python3
"""
Test: Incorrect Passcode Handling & Safe Recovery
==================================================
Tests:
1. An item encrypted with a passcode is copied to watch_decrypt with an INCORRECT passcode.
2. Verifies that the file and folder names are NOT corrupted or renamed.
3. Verifies that DECRYPT_ACTION_REQUIRED.txt is generated with guidance.
4. Updates password.txt with the correct passcode.
5. Verifies that the item is automatically detected, 100% decrypted back to original names,
   and DECRYPT_ACTION_REQUIRED.txt is automatically removed.
"""

import os
import sys
import shutil
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import drop_watcher


def run_test():
    base_dir = SCRIPT_DIR
    drop_watcher.init_watch_folders(base_dir)

    pw_dir = base_dir / "watch_encrypt_password"
    dec_dir = base_dir / "watch_decrypt"

    print("=" * 70)
    print("TEST: INCORRECT PASSCODE SAFETY & AUTO-RECOVERY")
    print("=" * 70)

    # 1. Setup encrypted test folder using passcode 'RealPassword2026'
    correct_pw = "RealPassword2026"
    (pw_dir / "password.txt").write_text(correct_pw, encoding="utf-8")

    test_folder = pw_dir / "Bank Records (2026)"
    test_sub = test_folder / "Q1 Statements"
    test_sub.mkdir(parents=True, exist_ok=True)

    f1 = test_folder / "Tax Return.pdf"
    f2 = test_sub / "Balance.xlsx"
    f1.write_bytes(b"Tax return document data 112233")
    f2.write_bytes(b"Spreadsheet balance data 445566")

    # Encrypt
    drop_watcher.scan_and_process(base_dir)

    enc_folders = [p for p in pw_dir.iterdir() if p.name.endswith(".encdir")]
    assert len(enc_folders) == 1, f"Expected 1 encrypted folder, found: {enc_folders}"
    enc_folder = enc_folders[0]
    print(f"1. Encrypted folder created: {enc_folder.name}")

    # Move encrypted folder to watch_decrypt
    dest_in_dec = dec_dir / enc_folder.name
    shutil.move(str(enc_folder), str(dest_in_dec))
    print(f"2. Moved {enc_folder.name} to watch_decrypt/")

    # 2. Put WRONG passcode into watch_decrypt/password.txt and watch_encrypt_password/password.txt
    wrong_pw = "WrongPassword999"
    (dec_dir / "password.txt").write_text(wrong_pw, encoding="utf-8")
    (pw_dir / "password.txt").write_text(wrong_pw, encoding="utf-8")
    print(f"3. Set password.txt to INCORRECT passcode: '{wrong_pw}'")

    # Run scan with wrong passcode
    print("\n--- Running scan with WRONG passcode ---")
    drop_watcher.scan_and_process(base_dir)

    # Verify files and folder remain safely encrypted
    assert dest_in_dec.exists(), "Encrypted folder was unexpectedly removed!"
    enc_files = list(dest_in_dec.rglob("*.encname"))
    assert len(enc_files) == 2, f"Encrypted files were modified or corrupted! Found: {enc_files}"

    # Verify notice file was created
    action_file = dec_dir / "DECRYPT_ACTION_REQUIRED.txt"
    assert action_file.exists(), "DECRYPT_ACTION_REQUIRED.txt notice file was NOT created!"
    print(f"\n4. VERIFIED: Original files held safe and encrypted. Notice file created:\n{action_file.read_text(encoding='utf-8')}")

    # 3. Now put CORRECT passcode into watch_decrypt/password.txt
    print(f"\n5. Updating watch_decrypt/password.txt to CORRECT passcode: '{correct_pw}'")
    (dec_dir / "password.txt").write_text(correct_pw, encoding="utf-8")

    # Run scan with correct passcode
    print("--- Running scan with CORRECT passcode ---")
    drop_watcher.scan_and_process(base_dir)

    # Verify complete restoration
    restored_folder = dec_dir / "Bank Records (2026)"
    assert restored_folder.exists(), f"Decrypted folder not found: {restored_folder}"
    assert (restored_folder / "Tax Return.pdf").exists(), "Tax Return.pdf missing!"
    assert (restored_folder / "Q1 Statements" / "Balance.xlsx").exists(), "Balance.xlsx missing!"

    # Verify notice file was automatically cleaned up
    assert not action_file.exists(), "Notice file was not cleaned up after successful decryption!"

    # Clean up test files
    shutil.rmtree(str(restored_folder))

    print("\n" + "=" * 70)
    print("TEST PASSED: INCORRECT PASSCODE WAS SAFELY HANDLED AND RECOVERED 100%!")
    print("=" * 70)


if __name__ == "__main__":
    run_test()
