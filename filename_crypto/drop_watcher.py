#!/usr/bin/env python3
"""
Automated Drop Folder Watcher Service
====================================
Monitors three dedicated drop folders for automatic encryption and decryption:

1. watch_encrypt_sparse/
   - Drop any folder or file here.
   - Automatically encrypts all nested files and folder names using the sparse content hash.
   - 100% self-contained and reversible without passwords.

2. watch_encrypt_password/
   - Drop any folder or file here.
   - Automatically encrypts all nested files and folder names using the password
     stored in 'password.txt' inside this folder.

3. watch_decrypt/
   - Drop any encrypted folder or file here.
   - Automatically detects whether sparse hash OR password was used to encrypt.
   - Restores all original filenames and folder names seamlessly.

Features:
- Handles deep nested folder hierarchies and loose files.
- Copy-completion detector: Waits until Windows file copying finishes before renaming.
- Trial decryption via AES-256-GCM authentication tags (zero false positives).
- Can run continuously in the background or as a one-shot scan (--once).
"""

import os
import sys
import time
import shutil
import hashlib
import argparse
import gc
from pathlib import Path
from typing import List, Optional, Tuple

# UTF-8 Console output
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Ensure parent and current directory are on sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
if str(SCRIPT_DIR.parent) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR.parent))

import filename_encryptor

ENCRYPTED_FILE_EXT = filename_encryptor.ENCRYPTED_EXTENSION  # .encname
ENCRYPTED_DIR_EXT = ".encdir"
PASSWORD_FILE_NAME = "password.txt"
ACTION_REQUIRED_FILE = "DECRYPT_ACTION_REQUIRED.txt"
SYSTEM_FILES = {"desktop.ini", "thumbs.db", ".ds_store", PASSWORD_FILE_NAME.lower(), ACTION_REQUIRED_FILE.lower()}

# Cache to avoid spamming the console for known failed decryptions until password.txt changes
_failed_decrypt_cache = set()


def safe_rename(src: str, dst: str, max_retries: int = 8, initial_delay: float = 0.25) -> None:
    """
    Safely renames a file or directory on Windows with exponential backoff retry.
    Catches transient [WinError 5] Access is Denied and [WinError 32] Sharing Violations
    caused by Windows Defender, Search Indexer, or Explorer holding temporary handles
    on recently modified directories/files.
    Also gracefully handles already-renamed targets or items moved/deleted externally (WinError 2).
    """
    src = os.path.normpath(src)
    dst = os.path.normpath(dst)

    if not os.path.exists(src):
        # Source does not exist: already renamed, or moved/deleted externally
        return

    delay = initial_delay
    gc.collect()
    for attempt in range(max_retries):
        try:
            os.rename(src, dst)
            return
        except (PermissionError, OSError) as e:
            winerr = getattr(e, "winerror", None)
            # If source file or folder is not found (already renamed or moved away)
            if winerr == 2 or isinstance(e, FileNotFoundError):
                if not os.path.exists(src) or os.path.exists(dst):
                    return
            if winerr in (5, 32, 13) or isinstance(e, PermissionError):
                if attempt < max_retries - 1:
                    time.sleep(delay)
                    delay *= 1.5
                    gc.collect()
                    continue
            raise


def get_passwords_signature(passwords: List[str], base_dir: Path) -> str:
    """Computes a signature of the current passwords and password.txt modification times."""
    mtimes = []
    for d in ("watch_decrypt", "watch_encrypt_password"):
        pw_file = base_dir / d / PASSWORD_FILE_NAME
        if pw_file.exists():
            mtimes.append(str(pw_file.stat().st_mtime_ns))
    return "|".join(passwords) + "@" + "-".join(mtimes)


def update_action_required_file(dec_dir: Path, failed_items: List[str]):
    """Creates or removes the DECRYPT_ACTION_REQUIRED.txt notice in watch_decrypt."""
    notice_file = dec_dir / ACTION_REQUIRED_FILE
    if not failed_items:
        if notice_file.exists():
            try:
                notice_file.unlink()
            except Exception:
                pass
        return

    content = [
        "=" * 70,
        "   DECRYPTION NOTICE: PASSCODE REQUIRED OR INCORRECT",
        "=" * 70,
        "The following item(s) could not be decrypted:",
    ]
    for item in failed_items:
        content.append(f"  - {item}")
    content.extend([
        "",
        "REASON:",
        "These items were encrypted with a passcode, but the passcode currently",
        f"entered in '{PASSWORD_FILE_NAME}' is incorrect or missing.",
        "Your original encrypted files are 100% SAFE and have NOT been corrupted.",
        "",
        "HOW TO RESTORE:",
        f"1. Open '{PASSWORD_FILE_NAME}' inside this folder.",
        "2. Enter the correct passcode that was used to encrypt the files.",
        "3. Save the file (Ctrl + S).",
        "",
        "The watcher will automatically detect your change and decrypt everything!",
        "=" * 70,
    ])

    try:
        notice_file.write_text("\n".join(content), encoding="utf-8")
    except Exception as e:
        print(f"[WARN] Could not write notice file: {e}")


def get_folder_sparse_hash(folder_path: str) -> str:
    """
    Computes a deterministic, collision-proof sparse fingerprint of an entire folder.
    Recursively collects the sparse hashes of all files inside, sorts them,
    and produces a SHA-256 digest. Renaming files does not change this hash.
    """
    hashes = []
    for root, _, files in os.walk(folder_path):
        for f in files:
            if f.lower() in SYSTEM_FILES:
                continue
            fp = os.path.join(root, f)
            try:
                hashes.append(filename_encryptor.get_sparse_media_hash(fp))
            except Exception:
                pass

    if not hashes:
        return hashlib.sha256(b"EMPTY_DIRECTORY_HASH_V1").hexdigest()

    hashes.sort()
    return hashlib.sha256(",".join(hashes).encode("utf-8")).hexdigest()


def read_password_file(folder_path: str) -> str:
    """Reads the first non-empty, non-comment line from password.txt in the folder."""
    pw_path = os.path.join(folder_path, PASSWORD_FILE_NAME)
    if not os.path.exists(pw_path):
        return ""
    try:
        with open(pw_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    except Exception as e:
        print(f"[WARN] Error reading {pw_path}: {e}")
    return ""


def get_known_passwords(base_dir: Path, item_path: Optional[str] = None) -> List[str]:
    """Collects known passwords from watch folders and optionally the item itself."""
    passwords = []
    # 1. From item directory itself (if it contains password.txt)
    if item_path and os.path.isdir(item_path):
        p_item = read_password_file(item_path)
        if p_item and p_item not in passwords:
            passwords.append(p_item)
    # 2. From watch_decrypt
    p1 = read_password_file(str(base_dir / "watch_decrypt"))
    if p1 and p1 not in passwords:
        passwords.append(p1)
    # 3. From watch_encrypt_password
    p2 = read_password_file(str(base_dir / "watch_encrypt_password"))
    if p2 and p2 not in passwords:
        passwords.append(p2)
    return passwords


def is_file_locked(file_path: str) -> bool:
    """Fast lock check: tries to open file with write access to verify it's not locked."""
    if not os.path.exists(file_path):
        return True
    try:
        with open(file_path, "a+b"):
            return False
    except (PermissionError, OSError):
        return True


def get_dir_size_and_count(dir_path: str) -> Tuple[int, int]:
    """Fast calculation of total directory size and file count."""
    total_size = 0
    count = 0
    for root, _, files in os.walk(dir_path):
        for f in files:
            if f.lower() in SYSTEM_FILES:
                continue
            fp = os.path.join(root, f)
            try:
                total_size += os.path.getsize(fp)
                count += 1
            except OSError:
                pass
    return total_size, count


def is_item_ready(item_path: str) -> bool:
    """
    Checks if a file or directory tree is completely done copying.
    Fast & non-blocking: Checks file locks immediately and does a single folder-level stability check.
    """
    if not os.path.exists(item_path):
        return False

    if os.path.isfile(item_path):
        if is_file_locked(item_path):
            return False
        # Single file size stability check (0.1s)
        try:
            s1 = os.path.getsize(item_path)
            time.sleep(0.1)
            return os.path.getsize(item_path) == s1
        except OSError:
            return False

    if os.path.isdir(item_path):
        # 1. Fast check: is ANY file currently locked by Windows Explorer or a copy operation?
        for root, _, files in os.walk(item_path):
            for f in files:
                if f.lower() in SYSTEM_FILES:
                    continue
                fp = os.path.join(root, f)
                if is_file_locked(fp):
                    return False

        # 2. Folder-level stability check: check total size once, sleep 0.3s once, check again
        size1, count1 = get_dir_size_and_count(item_path)
        if count1 == 0:
            return True  # Empty directory is ready
        time.sleep(0.3)
        size2, count2 = get_dir_size_and_count(item_path)
        return size1 == size2 and count1 == count2

    return False


def encrypt_directory_tree(target_path: str, secret_key: str = "") -> str:
    """
    Encrypts all nested files, subfolders, and the root folder itself (bottom-up).
    Returns the new path of the renamed root folder.
    """
    target_path = os.path.normpath(target_path)
    if not os.path.exists(target_path):
        return target_path

    # 1. Encrypt all nested files
    for root, dirs, files in os.walk(target_path, topdown=False):
        for f in files:
            if f.lower() in SYSTEM_FILES or f.endswith(ENCRYPTED_FILE_EXT):
                continue
            fpath = os.path.join(root, f)
            if not os.path.exists(fpath):
                continue
            try:
                shash = filename_encryptor.get_sparse_media_hash(fpath)
                enc_name = filename_encryptor.encrypt_filename(f, shash, secret_key)
                new_fpath = os.path.join(root, enc_name)
                safe_rename(fpath, new_fpath)
                print(f"    [FILE ENCRYPTED] {f} -> {enc_name}")
            except (FileNotFoundError, OSError) as e:
                if getattr(e, "winerror", None) == 2 or isinstance(e, FileNotFoundError):
                    continue
                raise

    # 2. Encrypt all nested subdirectories
    for root, dirs, files in os.walk(target_path, topdown=False):
        for d in dirs:
            if d.endswith(ENCRYPTED_DIR_EXT):
                continue
            dpath = os.path.join(root, d)
            if not os.path.exists(dpath):
                continue
            try:
                dhash = get_folder_sparse_hash(dpath)
                enc_dname = filename_encryptor.encrypt_filename(d, dhash, secret_key).replace(
                    ENCRYPTED_FILE_EXT, ENCRYPTED_DIR_EXT
                )
                new_dpath = os.path.join(root, enc_dname)
                safe_rename(dpath, new_dpath)
                print(f"    [FOLDER ENCRYPTED] {d} -> {enc_dname}")
            except (FileNotFoundError, OSError) as e:
                if getattr(e, "winerror", None) == 2 or isinstance(e, FileNotFoundError):
                    continue
                raise

    # 3. Encrypt the root folder itself
    if not os.path.exists(target_path):
        return target_path

    parent_dir = os.path.dirname(target_path)
    base_name = os.path.basename(target_path)
    if not base_name.endswith(ENCRYPTED_DIR_EXT):
        rhash = get_folder_sparse_hash(target_path)
        enc_root = filename_encryptor.encrypt_filename(base_name, rhash, secret_key).replace(
            ENCRYPTED_FILE_EXT, ENCRYPTED_DIR_EXT
        )
        new_root_path = os.path.join(parent_dir, enc_root)
        try:
            safe_rename(target_path, new_root_path)
            print(f"  [ROOT FOLDER ENCRYPTED] {base_name} -> {enc_root}")
            return new_root_path
        except (FileNotFoundError, OSError) as e:
            if getattr(e, "winerror", None) == 2 or isinstance(e, FileNotFoundError):
                return new_root_path
            raise

    return target_path


def decrypt_name(enc_name: str, shash: str, passwords: List[str]) -> Tuple[str, str]:
    """
    Intelligently determines whether sparse hash or a passcode was used:
    1. First tests sparse content hash (no password).
       If AES-GCM tag authenticates: returns (original_name, "SPARSE HASH").
    2. Then tests passcode(s) from password.txt.
       If AES-GCM tag authenticates: returns (original_name, "PASSCODE (password.txt)").
    Raises Exception if neither matches.
    """
    # 1. Try sparse hash alone (you know the key directly from the content hash!)
    try:
        dec = filename_encryptor.decrypt_filename(enc_name, shash, secret_key="")
        return dec, "SPARSE HASH"
    except Exception:
        pass

    # 2. Try passcode(s) from password.txt
    for pw in passwords:
        if not pw:
            continue
        try:
            dec = filename_encryptor.decrypt_filename(enc_name, shash, secret_key=pw)
            return dec, "PASSCODE (password.txt)"
        except Exception:
            pass

    raise ValueError("Authentication failed: Mismatched sparse content hash and incorrect/missing passcode.")


def decrypt_directory_tree(target_path: str, passwords: List[str]) -> bool:
    """
    Decrypts all nested files, subfolders, and the root folder itself (bottom-up).
    Returns True if 100% of items were successfully decrypted, False if any failed.
    """
    target_path = os.path.normpath(target_path)
    if not os.path.exists(target_path):
        return True

    total_failures = 0
    total_success = 0

    # 1. Decrypt all nested files
    for root, dirs, files in os.walk(target_path, topdown=False):
        for f in files:
            if not f.endswith(ENCRYPTED_FILE_EXT):
                continue
            fpath = os.path.join(root, f)
            if not os.path.exists(fpath):
                continue
            try:
                shash = filename_encryptor.get_sparse_media_hash(fpath)
                orig_name, method = decrypt_name(f, shash, passwords)
                new_fpath = os.path.join(root, orig_name)
                # Collision resolution
                counter = 1
                f_stem, f_ext = os.path.splitext(orig_name)
                while os.path.exists(new_fpath) and os.path.abspath(fpath) != os.path.abspath(new_fpath):
                    orig_name = f"{f_stem} ({counter}){f_ext}"
                    new_fpath = os.path.join(root, orig_name)
                    counter += 1
                safe_rename(fpath, new_fpath)
                print(f"    [FILE DECRYPTED via {method}] {f} -> {orig_name}")
                total_success += 1
            except (FileNotFoundError, OSError) as e:
                if getattr(e, "winerror", None) == 2 or isinstance(e, FileNotFoundError):
                    continue
                print(f"    [FILE DECRYPT FAILED: INCORRECT PASSCODE] {f} - kept safely encrypted.")
                total_failures += 1
            except Exception as e:
                print(f"    [FILE DECRYPT FAILED: INCORRECT PASSCODE] {f} - kept safely encrypted.")
                total_failures += 1

    # 2. Decrypt all nested subdirectories
    for root, dirs, files in os.walk(target_path, topdown=False):
        for d in dirs:
            if not d.endswith(ENCRYPTED_DIR_EXT):
                continue
            dpath = os.path.join(root, d)
            if not os.path.exists(dpath):
                continue
            try:
                dhash = get_folder_sparse_hash(dpath)
                fake_name = d[: -len(ENCRYPTED_DIR_EXT)] + ENCRYPTED_FILE_EXT
                orig_dname, method = decrypt_name(fake_name, dhash, passwords)
                new_dpath = os.path.join(root, orig_dname)
                # Collision resolution
                counter = 1
                base_d = orig_dname
                while os.path.exists(new_dpath) and os.path.abspath(dpath) != os.path.abspath(new_dpath):
                    orig_dname = f"{base_d} ({counter})"
                    new_dpath = os.path.join(root, orig_dname)
                    counter += 1
                safe_rename(dpath, new_dpath)
                print(f"    [FOLDER DECRYPTED via {method}] {d} -> {orig_dname}")
                total_success += 1
            except (FileNotFoundError, OSError) as e:
                if getattr(e, "winerror", None) == 2 or isinstance(e, FileNotFoundError):
                    continue
                print(f"    [FOLDER DECRYPT FAILED: INCORRECT PASSCODE] {d} - kept safely encrypted.")
                total_failures += 1
            except Exception as e:
                print(f"    [FOLDER DECRYPT FAILED: INCORRECT PASSCODE] {d} - kept safely encrypted.")
                total_failures += 1

    # 3. Decrypt the root folder only if all nested items succeeded
    if total_failures > 0:
        print(f"  [ROOT FOLDER PROTECTED] {os.path.basename(target_path)} held in safe encrypted state until correct passcode is provided in {PASSWORD_FILE_NAME}.")
        return False

    parent_dir = os.path.dirname(target_path)
    base_name = os.path.basename(target_path)
    if base_name.endswith(ENCRYPTED_DIR_EXT):
        try:
            rhash = get_folder_sparse_hash(target_path)
            fake_root = base_name[: -len(ENCRYPTED_DIR_EXT)] + ENCRYPTED_FILE_EXT
            orig_root, method = decrypt_name(fake_root, rhash, passwords)
            new_root_path = os.path.join(parent_dir, orig_root)
            # Collision resolution
            counter = 1
            base_r = orig_root
            while os.path.exists(new_root_path) and os.path.abspath(target_path) != os.path.abspath(new_root_path):
                orig_root = f"{base_r} ({counter})"
                new_root_path = os.path.join(parent_dir, orig_root)
                counter += 1
            safe_rename(target_path, new_root_path)
            print(f"  [ROOT FOLDER DECRYPTED via {method}] {base_name} -> {orig_root}")
            return True
        except Exception as e:
            print(f"  [ROOT FOLDER DECRYPT FAILED] {base_name}: Incorrect passcode or altered content ({e})")
            return False

    return True


def init_watch_folders(base_dir: Path):
    """Creates watch folders and default password.txt files if they do not exist."""
    sparse_dir = base_dir / "watch_encrypt_sparse"
    pw_dir = base_dir / "watch_encrypt_password"
    dec_dir = base_dir / "watch_decrypt"

    for d in (sparse_dir, pw_dir, dec_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Setup default password file in watch_encrypt_password
    pw_file = pw_dir / PASSWORD_FILE_NAME
    if not pw_file.exists():
        with open(pw_file, "w", encoding="utf-8") as f:
            f.write("# Enter your encryption password on the line below.\n")
            f.write("# Any folder copied into this directory will be encrypted using this password.\n")
            f.write("MyVaultPassword2026!\n")

    # Setup default password file in watch_decrypt
    dec_pw_file = dec_dir / PASSWORD_FILE_NAME
    if not dec_pw_file.exists():
        with open(dec_pw_file, "w", encoding="utf-8") as f:
            f.write("# Put passwords here to decrypt password-encrypted folders.\n")
            f.write("# (Sparse hash encrypted items do not need any password).\n")
            f.write("MyVaultPassword2026!\n")


def scan_and_process(base_dir: Path):
    """Single-pass scan of all 3 watch folders."""
    sparse_dir = base_dir / "watch_encrypt_sparse"
    pw_dir = base_dir / "watch_encrypt_password"
    dec_dir = base_dir / "watch_decrypt"
    passwords = get_known_passwords(base_dir)

    # 1. Process watch_encrypt_sparse
    if sparse_dir.exists():
        for item in sorted(sparse_dir.iterdir()):
            if item.name.lower() in SYSTEM_FILES or item.name.endswith(ENCRYPTED_DIR_EXT) or item.name.endswith(ENCRYPTED_FILE_EXT):
                continue
            if not is_item_ready(str(item)):
                continue
            print(f"\n[NEW ITEM DETECTED in watch_encrypt_sparse]: {item.name}")
            try:
                if item.is_dir():
                    encrypt_directory_tree(str(item), secret_key="")
                elif item.is_file():
                    shash = filename_encryptor.get_sparse_media_hash(str(item))
                    enc_name = filename_encryptor.encrypt_filename(item.name, shash, secret_key="")
                    safe_rename(str(item), str(item.parent / enc_name))
                    print(f"  [FILE ENCRYPTED]: {item.name} -> {enc_name}")
            except Exception as e:
                if not item.exists():
                    continue
                print(f"  [ERROR]: {e}")

    # 2. Process watch_encrypt_password
    if pw_dir.exists():
        current_pw = read_password_file(str(pw_dir))
        for item in sorted(pw_dir.iterdir()):
            if item.name.lower() in SYSTEM_FILES or item.name.endswith(ENCRYPTED_DIR_EXT) or item.name.endswith(ENCRYPTED_FILE_EXT):
                continue
            if not is_item_ready(str(item)):
                continue
            print(f"\n[NEW ITEM DETECTED in watch_encrypt_password]: {item.name}")
            try:
                if item.is_dir():
                    encrypt_directory_tree(str(item), secret_key=current_pw)
                elif item.is_file():
                    shash = filename_encryptor.get_sparse_media_hash(str(item))
                    enc_name = filename_encryptor.encrypt_filename(item.name, shash, secret_key=current_pw)
                    safe_rename(str(item), str(item.parent / enc_name))
                    print(f"  [FILE ENCRYPTED]: {item.name} -> {enc_name}")
            except Exception as e:
                if not item.exists():
                    continue
                print(f"  [ERROR]: {e}")

    # 3. Process watch_decrypt
    if dec_dir.exists():
        failed_items_in_dec = []
        pw_sig = get_passwords_signature(passwords, base_dir)

        for item in sorted(dec_dir.iterdir()):
            if item.name.lower() in SYSTEM_FILES:
                continue
            is_enc_dir = item.is_dir() and item.name.endswith(ENCRYPTED_DIR_EXT)
            is_enc_file = item.is_file() and item.name.endswith(ENCRYPTED_FILE_EXT)
            if not (is_enc_dir or is_enc_file):
                continue
            if not is_item_ready(str(item)):
                continue

            cache_key = (str(item), pw_sig)
            already_warned = cache_key in _failed_decrypt_cache

            if not already_warned:
                print(f"\n[ENCRYPTED ITEM DETECTED in watch_decrypt]: {item.name}")

            success = False
            try:
                if is_enc_dir:
                    success = decrypt_directory_tree(str(item), passwords=passwords)
                elif is_enc_file:
                    shash = filename_encryptor.get_sparse_media_hash(str(item))
                    orig_name, method = decrypt_name(item.name, shash, passwords=passwords)
                    safe_rename(str(item), str(item.parent / orig_name))
                    print(f"  [FILE DECRYPTED via {method}]: {item.name} -> {orig_name}")
                    success = True

                if not success:
                    failed_items_in_dec.append(item.name)
                    _failed_decrypt_cache.add(cache_key)

            except Exception as e:
                if not item.exists():
                    continue
                failed_items_in_dec.append(item.name)
                _failed_decrypt_cache.add(cache_key)
                if not already_warned:
                    print(f"  [DECRYPT FAILED: INCORRECT PASSCODE] {item.name}: {e}")
                    print(f"  [SAFETY NOTICE] Files remain safely encrypted. Please enter the correct passcode in {PASSWORD_FILE_NAME}.")

        update_action_required_file(dec_dir, failed_items_in_dec)

    # 4. Check if user accidentally placed a folder in the parent base_dir instead of the watch folders
    for item in base_dir.iterdir():
        if item.is_dir() and item.name not in ("watch_encrypt_sparse", "watch_encrypt_password", "watch_decrypt", "__pycache__"):
            if not (item.name.endswith(ENCRYPTED_DIR_EXT) or item.name.endswith(ENCRYPTED_FILE_EXT)):
                cache_k = (str(item), "base_dir_notice")
                if cache_k not in _failed_decrypt_cache:
                    _failed_decrypt_cache.add(cache_k)
                    print(f"\n[NOTICE] Found unencrypted folder '{item.name}' inside '{base_dir.name}'.")
                    print(f"  To encrypt it, move it inside:")
                    print(f"    -> '{base_dir.name}\\watch_encrypt_sparse'   (to encrypt using Sparse Hash)")
                    print(f"    -> '{base_dir.name}\\watch_encrypt_password' (to encrypt using Password)")


def acquire_single_instance_mutex():
    """
    Ensures only ONE instance of drop_watcher runs on the system at a time.
    Prevents race conditions where two running watchers try to rename the same files.
    """
    if sys.platform == "win32":
        import ctypes
        mutex_name = "Local\\HFSYNC_DROP_WATCHER_SINGLETON_MUTEX"
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
        last_error = ctypes.windll.kernel32.GetLastError()
        ERROR_ALREADY_EXISTS = 183
        if last_error == ERROR_ALREADY_EXISTS:
            print("=" * 70)
            print("   [NOTICE] Another instance of drop_watcher is already active!")
            print("   Only one watcher instance can run at a time to prevent file conflicts.")
            print("   This duplicate instance will now safely exit.")
            print("=" * 70)
            sys.exit(0)
        return handle
    return None


def run_watcher_loop(base_dir: Path, poll_interval: float = 1.5):
    """Runs continuous background watching loop."""
    print("=" * 70)
    print("   AUTOMATED FOLDER WATCHER SERVICE STARTED")
    print("=" * 70)
    print(f"Base Directory: {base_dir}")
    print("Watching folders:")
    print("  1. watch_encrypt_sparse/   -> Auto-encrypts (Sparse Hash)")
    print("  2. watch_encrypt_password/ -> Auto-encrypts (Password from password.txt)")
    print("  3. watch_decrypt/          -> Auto-decrypts (Sparse Hash OR Password)")
    print("\nCopy or drag folders/files into any of these watching folders.")
    print("Press Ctrl+C to stop.\n" + "-" * 70)

    try:
        while True:
            scan_and_process(base_dir)
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        print("\nWatcher stopped by user.")


def main():
    parser = argparse.ArgumentParser(description="Automated Drop Folder Watcher Service.")
    parser.add_argument(
        "--dir",
        default=str(SCRIPT_DIR),
        help="Base directory containing the watch folders (default: script directory)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single scan pass and exit without looping",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.5,
        help="Polling interval in seconds (default: 1.5)",
    )
    args = parser.parse_args()

    base_path = Path(args.dir).resolve()
    init_watch_folders(base_path)

    if args.once:
        print(f"Running single-pass scan on: {base_path}")
        scan_and_process(base_path)
        print("Scan complete.")
    else:
        _mutex_handle = acquire_single_instance_mutex()
        run_watcher_loop(base_path, poll_interval=args.interval)


if __name__ == "__main__":
    main()
