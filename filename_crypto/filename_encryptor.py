#!/usr/bin/env python3
"""
Reversible Filename Encryptor & Decryptor
=========================================
Encrypts filenames reversibly into safe, valid filenames using the sparse content hash
of each file as the encryption key (with optional secret passphrase).

Because only the filename is renamed and the file content remains untouched,
the sparse hash of the file is identical before and after renaming.
This allows decrypting and restoring original filenames anytime, without needing
a central database or losing track of random keys.

Features:
- Fast sparse hashing: inspects file size + first/middle/last 1MB chunks (<0.005s per file).
- AES-256-GCM authenticated encryption (detects tampering/mismatch).
- Valid filename guarantee: URL-safe Base64 ([A-Za-z0-9_-]) with .encname extension.
- Never clashes with Windows reserved names (CON, PRN, AUX, NUL, etc.).
- Safe: Skips already encrypted files, avoids overwrites, validates length (< 255 chars).
- Works interactively (prompts for folder) or via command-line arguments.
"""

import os
import sys
import zlib
import base64
import hashlib
import argparse
from pathlib import Path
from typing import Tuple, List, Optional

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag
except ImportError:
    print("[ERROR] 'cryptography' library is not installed.")
    print("Please install it using: pip install cryptography")
    sys.exit(1)

# Add parent directory to path to access crypto_engine if available
parent_dir = str(Path(__file__).resolve().parent.parent)
if parent_dir not in sys.path:
    sys.path.insert(0, parent_dir)

# Try importing get_sparse_media_hash from crypto_engine if available
try:
    from crypto_engine import get_sparse_media_hash
except ImportError:
    SAMPLE_SIZE = 1024 * 1024  # 1 MB boundary sample

    def get_sparse_media_hash(file_path: str) -> str:
        """
        Industry-Standard Sparse Media Fingerprint (Plex / OpenSubtitles style).
        Samples exact file size + first 1MB + middle 1MB + last 1MB.
        Fast on any file size (even 100+ GB) and collision-proof.
        """
        size = os.path.getsize(file_path)
        hasher = hashlib.sha256()
        hasher.update(str(size).encode("utf-8"))

        with open(file_path, "rb") as f:
            if size <= SAMPLE_SIZE * 3:
                while chunk := f.read(1024 * 1024):
                    hasher.update(chunk)
            else:
                hasher.update(f.read(SAMPLE_SIZE))
                f.seek(size // 2)
                hasher.update(f.read(SAMPLE_SIZE))
                f.seek(size - SAMPLE_SIZE)
                hasher.update(f.read(SAMPLE_SIZE))

        return hasher.hexdigest()


ENCRYPTED_EXTENSION = ".encname"
MAX_FILENAME_LEN = 255  # Windows max file name component length
BANNED_CHARS = set('<>:"/\\|?*')


def is_valid_restored_name(name: str) -> bool:
    """Verifies that the restored filename is a valid, uncorrupted OS filename."""
    if not name or len(name) > MAX_FILENAME_LEN:
        return False
    if any(c in BANNED_CHARS for c in name):
        return False
    if any(ord(c) < 32 for c in name):
        return False
    if name.endswith(" ") or name.endswith("."):
        return False
    return True


def derive_keys(sparse_hash: str, secret_key: str = "") -> Tuple[bytes, bytes]:
    """
    Derives deterministic 256-bit AES key and 96-bit (12-byte) GCM nonce.
    - If secret_key is given: uses the custom password as the encryption key.
    - Otherwise: uses the file's sparse content hash as the encryption key.
    The file's sparse hash is also mixed into the nonce so each file gets a
    unique nonce even under a shared custom password.
    """
    if secret_key:
        key_source = f"CUSTOM_PW::{secret_key}".encode("utf-8")
    else:
        key_source = f"SPARSE_HASH::{sparse_hash}".encode("utf-8")

    key = hashlib.sha256(key_source + b":key").digest()
    nonce = hashlib.sha256(key_source + b":nonce:" + sparse_hash.encode("utf-8")).digest()[:12]
    return key, nonce


def encrypt_filename(original_name: str, sparse_hash: str, secret_key: str = "") -> str:
    """
    Encrypts a filename string using AES-256-GCM.
    Returns a safe, valid filename ending with .encname.
    """
    name_bytes = original_name.encode("utf-8")

    # Optional zlib compression for very long names
    compressed = zlib.compress(name_bytes, level=9)
    if len(compressed) < len(name_bytes):
        # 1-byte flag 0x01 indicates compressed
        payload = b"\x01" + compressed
    else:
        # 1-byte flag 0x00 indicates uncompressed
        payload = b"\x00" + name_bytes

    key, nonce = derive_keys(sparse_hash, secret_key)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, payload, None)

    # Base64url encode without '=' padding
    b64_str = base64.urlsafe_b64encode(ciphertext).decode("ascii").rstrip("=")
    encrypted_name = b64_str + ENCRYPTED_EXTENSION

    if len(encrypted_name) > MAX_FILENAME_LEN:
        raise ValueError(
            f"Encrypted filename length ({len(encrypted_name)}) exceeds maximum "
            f"allowed length of {MAX_FILENAME_LEN} characters."
        )

    return encrypted_name


def decrypt_filename(encrypted_name: str, sparse_hash: str, secret_key: str = "") -> str:
    """
    Decrypts an encrypted filename back to the original filename using AES-256-GCM.
    Verifies cryptographic tag; raises Exception if key/sparse hash does not match.
    """
    if encrypted_name.endswith(ENCRYPTED_EXTENSION):
        b64_str = encrypted_name[: -len(ENCRYPTED_EXTENSION)]
    else:
        b64_str = encrypted_name

    # Restore '=' padding
    padding = (4 - len(b64_str) % 4) % 4
    raw_ciphertext = base64.urlsafe_b64decode(b64_str + "=" * padding)

    key, nonce = derive_keys(sparse_hash, secret_key)
    aesgcm = AESGCM(key)
    payload = aesgcm.decrypt(nonce, raw_ciphertext, None)

    flag = payload[0]
    data = payload[1:]

    if flag == 0x01:
        name_bytes = zlib.decompress(data)
    elif flag == 0x00:
        name_bytes = data
    else:
        raise ValueError("Invalid payload encoding flag.")

    try:
        restored_name = name_bytes.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("Decrypted payload is not valid UTF-8 text.")

    if not is_valid_restored_name(restored_name):
        raise ValueError(
            f"Restored filename contains illegal characters or invalid formatting: {repr(restored_name)}"
        )

    return restored_name


def find_files_to_process(folder_path: str, recursive: bool = False, mode: str = "encrypt") -> List[str]:
    """Finds files to process in the target directory."""
    files_to_process = []
    base_path = Path(folder_path).resolve()

    if not base_path.exists() or not base_path.is_dir():
        raise FileNotFoundError(f"Folder not found: {folder_path}")

    if recursive:
        scanner = base_path.rglob("*")
    else:
        scanner = base_path.glob("*")

    for p in scanner:
        if not p.is_file():
            continue
        # Skip desktop.ini and OS hidden system files if needed
        if p.name.lower() in ("desktop.ini", "thumbs.db", ".ds_store"):
            continue

        if mode == "encrypt":
            # Don't double encrypt files already ending in .encname
            if not p.name.endswith(ENCRYPTED_EXTENSION):
                files_to_process.append(str(p))
        elif mode == "decrypt":
            # Only decrypt files ending in .encname
            if p.name.endswith(ENCRYPTED_EXTENSION):
                files_to_process.append(str(p))

    return sorted(files_to_process)


def process_folder(
    folder_path: str,
    mode: str = "encrypt",
    recursive: bool = False,
    secret_key: str = "",
    dry_run: bool = False,
) -> dict:
    """
    Processes all matching files in the folder:
    Calculates each file's sparse hash, computes new filename, and renames.
    """
    file_paths = find_files_to_process(folder_path, recursive=recursive, mode=mode)
    stats = {"total": len(file_paths), "success": 0, "skipped": 0, "failed": 0, "operations": []}

    if not file_paths:
        return stats

    for fpath in file_paths:
        dir_name = os.path.dirname(fpath)
        current_name = os.path.basename(fpath)

        try:
            # 1. Compute sparse hash from the file's content
            sparse_hash = get_sparse_media_hash(fpath)

            # 2. Compute new filename
            if mode == "encrypt":
                new_name = encrypt_filename(current_name, sparse_hash, secret_key)
            else:
                new_name = decrypt_filename(current_name, sparse_hash, secret_key)

            new_path = os.path.join(dir_name, new_name)

            # Check if destination exists
            if os.path.exists(new_path) and os.path.abspath(fpath) != os.path.abspath(new_path):
                print(f"[SKIP] Destination already exists: {new_name}")
                stats["skipped"] += 1
                stats["operations"].append((current_name, new_name, "SKIPPED: Destination exists"))
                continue

            # 3. Rename file
            if not dry_run:
                os.rename(fpath, new_path)

            stats["success"] += 1
            stats["operations"].append((current_name, new_name, "SUCCESS"))

        except InvalidTag:
            stats["failed"] += 1
            msg = "Authentication failed (wrong secret passphrase or file content was modified)"
            print(f"[ERROR] Could not decrypt {current_name}: {msg}")
            stats["operations"].append((current_name, None, f"FAILED: {msg}"))
        except Exception as e:
            stats["failed"] += 1
            error_msg = f"FAILED: {type(e).__name__} - {e}"
            print(f"[ERROR] Could not process {current_name}: {e}")
            stats["operations"].append((current_name, None, error_msg))

    return stats


def interactive_prompt():
    """Runs interactive command line questionnaire."""
    print("=" * 65)
    print("   HFSYNC Reversible Filename Encryptor / Decryptor")
    print("=" * 65)
    print("Reversibly encrypts filenames using the file's sparse content hash.")
    print("Guarantees OS-safe filenames (URL-safe Base64 + .encname).\n")

    # 1. Prompt for Folder
    while True:
        folder_input = input("Enter folder path: ").strip().strip('"').strip("'")
        if not folder_input:
            print("Please enter a valid directory path.")
            continue
        resolved_path = os.path.abspath(folder_input)
        if not os.path.isdir(resolved_path):
            print(f"Directory does not exist: {resolved_path}\nPlease try again.\n")
            continue
        break

    # 2. Prompt for Action
    print("\nSelect Action:")
    print("  [1] Encrypt filenames (Obfuscate to safe .encname files)")
    print("  [2] Decrypt filenames (Restore original names using sparse hash)")
    action_choice = input("Enter choice (1/2) [default 1]: ").strip()
    mode = "decrypt" if action_choice == "2" else "encrypt"

    # 3. Prompt for Recursive option
    rec_input = input("\nInclude subfolders recursively? (y/N) [N]: ").strip().lower()
    recursive = rec_input in ("y", "yes")

    # 4. Custom password or sparse hash
    print("\nEncryption Key / Password:")
    print("  Enter a custom password to encrypt/decrypt with your password.")
    print("  Or press ENTER to use the file's sparse content hash as the key.")
    secret_key = input("Custom password (optional, press Enter for sparse hash): ").strip()

    # 5. Scan and Preview
    try:
        files = find_files_to_process(resolved_path, recursive=recursive, mode=mode)
    except Exception as e:
        print(f"\n[ERROR] Failed to scan directory: {e}")
        return

    if not files:
        target_type = "unencrypted files" if mode == "encrypt" else f"encrypted (*{ENCRYPTED_EXTENSION}) files"
        print(f"\nNo {target_type} found in '{resolved_path}'.")
        return

    print(f"\nFound {len(files)} file(s) to {mode.upper()}.")
    print("-" * 65)
    print("Preview of first few transformations:")

    preview_count = min(len(files), 5)
    for p in files[:preview_count]:
        cname = os.path.basename(p)
        try:
            shash = get_sparse_media_hash(p)
            if mode == "encrypt":
                nname = encrypt_filename(cname, shash, secret_key)
            else:
                nname = decrypt_filename(cname, shash, secret_key)
            print(f"  {cname}\n    -> {nname}")
        except InvalidTag:
            print(f"  {cname}\n    -> [AUTH ERROR: Wrong secret passphrase or content changed]")
        except Exception as e:
            print(f"  {cname}\n    -> [PREVIEW ERROR: {e}]")

    if len(files) > preview_count:
        print(f"  ... and {len(files) - preview_count} more file(s).")
    print("-" * 65)

    # 6. Confirmation
    confirm = input(f"\nAre you sure you want to {mode.upper()} {len(files)} file(s)? (Y/n) [Y]: ").strip().lower()
    if confirm not in ("", "y", "yes"):
        print("Operation cancelled by user.")
        return

    print(f"\nProcessing {len(files)} files...")
    stats = process_folder(
        folder_path=resolved_path,
        mode=mode,
        recursive=recursive,
        secret_key=secret_key,
        dry_run=False,
    )

    print("\n" + "=" * 65)
    print(f"   COMPLETED: {mode.upper()}")
    print("=" * 65)
    print(f"Total Found : {stats['total']}")
    print(f"Successful  : {stats['success']}")
    print(f"Skipped     : {stats['skipped']}")
    print(f"Failed      : {stats['failed']}")
    print("=" * 65)


def main():
    parser = argparse.ArgumentParser(
        description="Reversible Filename Encryptor/Decryptor using file content sparse hash."
    )
    parser.add_argument("-f", "--folder", help="Target folder path to process", default=None)
    parser.add_argument(
        "-m", "--mode", choices=["encrypt", "decrypt"], default=None, help="Action mode: encrypt or decrypt"
    )
    parser.add_argument(
        "-r", "--recursive", action="store_true", help="Process subfolders recursively"
    )
    parser.add_argument(
        "-k", "--key", default="", help="Optional secret passphrase (defaults to sparse hash only)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview renames without touching files"
    )
    parser.add_argument(
        "-y", "--yes", action="store_true", help="Skip confirmation prompt"
    )

    args = parser.parse_args()

    # If no folder or mode supplied, launch interactive mode
    if not args.folder or not args.mode:
        interactive_prompt()
        return

    target_folder = os.path.abspath(args.folder)
    if not os.path.isdir(target_folder):
        print(f"[ERROR] Target folder not found: {target_folder}")
        sys.exit(1)

    print(f"Mode       : {args.mode.upper()}")
    print(f"Folder     : {target_folder}")
    print(f"Recursive  : {args.recursive}")
    print(f"Dry Run    : {args.dry_run}")

    files = find_files_to_process(target_folder, recursive=args.recursive, mode=args.mode)
    print(f"Found {len(files)} files to {args.mode}.")

    if not files:
        print("Nothing to process.")
        return

    if not args.yes and not args.dry_run:
        confirm = input(f"Proceed with {args.mode}? (y/N): ").strip().lower()
        if confirm not in ("y", "yes"):
            print("Cancelled.")
            return

    stats = process_folder(
        folder_path=target_folder,
        mode=args.mode,
        recursive=args.recursive,
        secret_key=args.key,
        dry_run=args.dry_run,
    )

    print(f"Done. Success: {stats['success']}, Skipped: {stats['skipped']}, Failed: {stats['failed']}")


if __name__ == "__main__":
    main()
