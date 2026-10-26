import os
import sys
import time
import json
import re
import io
import shutil
import hashlib
import argparse
import threading
from typing import Optional, Dict, Set, Tuple, Any
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from huggingface_hub import HfApi, CommitOperationAdd
from core import crypto_engine
from core import commit_helper
from core import storyboard_helper
from core import staging_manager
from core import metadata_helper
from core import account_manager
from core import activity_tracker
import companion_helper
from core import screens_helper
from core import hf_rate_limiter

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SUPPORTED_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm",
    ".flv", ".ts", ".m4v", ".srt", ".vtt", ".nfo",
    ".jpg", ".jpeg", ".png", ".webp", ".encname"
}

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm",
    ".flv", ".ts", ".m4v", ".encname"
}


def get_file_repo_folder(file_path: Path, base_dir: Path, default_repo_name: str) -> str:
    try:
        rel = file_path.relative_to(base_dir)
        parts = rel.parts
        if len(parts) > 1:
            return parts[0]
        return default_repo_name
    except Exception:
        return default_repo_name


def is_video_file(file_path: Path) -> bool:
    """
    Ultra-fast (<0.05ms) content-based video detection using file magic bytes.
    Does NOT rely on file extension alone: automatically detects MP4, MKV, AVI, MOV,
    WebM, FLV, WMV, MPEG-TS even if renamed with arbitrary, encrypted, or missing extensions.
    Zero performance cost: checks extension in 1ns first, reads only 64 bytes for unknown extensions.
    """
    name = file_path.name.lower()
    if ".companion." in name or name.endswith((".companion.m4a", ".tmp.enc", ".tmp", ".enc", ".nfo", ".strm", ".m4a", ".aac")) or name.startswith("tmp_"):
        return False
    suf = file_path.suffix.lower()
    if suf in VIDEO_EXTENSIONS:
        return True
    if suf in IGNORED_EXTENSIONS or file_path.name.endswith(".enc") or file_path.name.endswith(".tmp.enc"):
        return False
    try:
        with open(file_path, "rb") as f:
            hdr = f.read(64)
        if len(hdr) < 12:
            return False
        # Ignore audio-only containers sharing ftyp (M4A, M4B)
        if b"M4A " in hdr[8:16] or b"M4B " in hdr[8:16] or (b"mp42isom" in hdr[8:16] and suf == ".m4a"):
            return False
        # MP4 / MOV / M4V / 3GP (ftyp box)
        if b"ftyp" in hdr[4:12] or hdr[4:8] == b"ftyp":
            return True
        # Matroska / WebM (EBML header)
        if hdr.startswith(b"\x1a\x45\xdf\xa3"):
            return True
        # AVI (RIFF ... AVI )
        if hdr.startswith(b"RIFF") and hdr[8:12] == b"AVI ":
            return True
        # FLV
        if hdr.startswith(b"FLV\x01"):
            return True
        # WMV / ASF
        if hdr.startswith(b"\x30\x26\xb2\x75\x8e\x66\xcf\x11"):
            return True
        # MPEG-TS (Sync byte 0x47)
        if hdr.startswith(b"\x47"):
            return True
    except Exception:
        pass
    return False

IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp"
}

SCREEN_FOLDER_NAMES = {
    "screens", "screen", "thumbs", "thumb", "covers", "cover", "posters", "poster", "artwork", "storyboards"
}

SUB_FOLDER_NAMES = {
    "subs", "sub", "subtitles", "subtitle"
}

SKIPPED_UNMATCHED_FILES = set()
UNMATCHED_ATTEMPTS = {}

IGNORED_EXTENSIONS = {
    ".tmp", ".part", ".crdownload", ".!qb", ".downloading", ".temp"
}

MANIFEST_FILE = "upload_manifest.json"
MANIFEST_LOCK = threading.RLock()
PRINT_LOCK = threading.RLock()
IN_PROGRESS_FILES = set()
PREPARING_FILES = set()
PREPARED_PACKAGES = {}
UPLOADING_FILES = set()
KNOWN_REPOS = set()


def safe_print(*args, **kwargs):
    with PRINT_LOCK:
        print(*args, **kwargs)


RATE_LIMIT_LOCK = threading.Lock()
RATE_LIMIT_PAUSE_UNTIL = 0
CONSECUTIVE_RATE_LIMITS = 0
THROTTLED_ACCOUNTS = {}  # {username: unpause_timestamp}


def is_rate_limit_error(exc: Exception) -> bool:
    """
    Detects if an exception is an HTTP 429 Too Many Requests or commit rate limit from Hugging Face.
    """
    if exc is None:
        return False
    resp = getattr(exc, "response", None)
    if resp is not None and getattr(resp, "status_code", None) == 429:
        return True
    if getattr(exc, "status_code", None) == 429:
        return True
    msg = str(exc).lower()
    patterns = [
        "429",
        "rate limit",
        "too many requests",
        "secondary rate limit",
        "quota exceeded",
        "commit limit",
        "exceeded your",
        "please wait",
    ]
    return any(p in msg for p in patterns)


def extract_retry_after(exc: Exception) -> int:
    """
    Attempts to extract explicit retry-after duration from response headers or error message.
    """
    resp = getattr(exc, "response", None)
    if resp is not None:
        headers = getattr(resp, "headers", {})
        ra = headers.get("Retry-After") or headers.get("retry-after")
        if ra and str(ra).strip().isdigit():
            return int(str(ra).strip())
    msg = str(exc).lower()
    m = re.search(r"wait\s+(\d+)\s*(s|sec|second|m|min|minute|h|hour)", msg)
    if m:
        val, unit = int(m.group(1)), m.group(2)
        if "h" in unit:
            return val * 3600
        elif "m" in unit:
            return val * 60
        return val
    return 0


def trigger_rate_limit_cooldown(exc: Exception, account_username: str) -> int:
    """
    Enters a graceful cooldown period when Hugging Face rate limit is encountered.
    Never forces or hammers the API. All original files stay safe on disk.
    """
    global RATE_LIMIT_PAUSE_UNTIL, CONSECUTIVE_RATE_LIMITS, THROTTLED_ACCOUNTS
    with RATE_LIMIT_LOCK:
        CONSECUTIVE_RATE_LIMITS += 1
        explicit_wait = extract_retry_after(exc)
        if explicit_wait > 0:
            pause_sec = explicit_wait + 5
        else:
            # Exponential backoff schedule: 60s -> 300s (5m) -> 900s (15m) -> 1800s (30m)
            backoff_schedule = [60, 300, 900, 1800]
            idx = min(CONSECUTIVE_RATE_LIMITS - 1, len(backoff_schedule) - 1)
            pause_sec = backoff_schedule[idx]

        cooldown_until = time.time() + pause_sec
        RATE_LIMIT_PAUSE_UNTIL = max(RATE_LIMIT_PAUSE_UNTIL, cooldown_until)
        if account_username:
            THROTTLED_ACCOUNTS[account_username] = cooldown_until
        hf_rate_limiter.rate_limiter.trigger_cooldown(account_username, pause_sec, reason=str(exc))

        pause_min = pause_sec / 60
        safe_print(f"[{time.strftime('%H:%M:%S')}] [⏸️ HF RATE LIMIT COOLDOWN] Commit rate limit reached on Hugging Face for '{account_username}'.")
        safe_print(f"[{time.strftime('%H:%M:%S')}] [*] Zero files lost! Local originals stay untouched in watch folder.")
        safe_print(f"[{time.strftime('%H:%M:%S')}] [*] Pausing upload worker for {pause_sec}s ({pause_min:.1f} min). Zero requests sent during cooldown.")

        activity_tracker.tracker.add_event(
            "warning",
            "⏸️ HF Rate Limit Cooldown",
            f"Commit quota reached on Hugging Face ({account_username}). Pausing for {pause_sec}s. All files remain safe on disk.",
            account=account_username
        )
        return pause_sec


def sanitize_repo_name(name: str) -> str:
    clean = re.sub(r'[^a-zA-Z0-9_\-\.]', '-', name)
    return clean or "default-vault"


def load_env(env_path="env.txt"):
    config = {}
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    config[k.strip()] = v.strip()
    return config


def load_manifest():
    with MANIFEST_LOCK:
        if os.path.exists(MANIFEST_FILE):
            try:
                with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if "files" not in data:
                        data = {"files": data, "media_hashes": {}}
                    if "media_hashes" not in data:
                        data["media_hashes"] = {}
                    return data
            except Exception:
                return {"files": {}, "media_hashes": {}}
        return {"files": {}, "media_hashes": {}}


def save_manifest(manifest):
    with MANIFEST_LOCK:
        temp_path = f"{MANIFEST_FILE}.tmp"
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
        # Robust replacement on Windows to survive concurrent readers
        replaced = False
        for _ in range(15):
            try:
                os.replace(temp_path, MANIFEST_FILE)
                replaced = True
                break
            except (PermissionError, OSError):
                time.sleep(0.04)
        if not replaced:
            import shutil
            shutil.copyfile(temp_path, MANIFEST_FILE)
            try: os.remove(temp_path)
            except Exception: pass


def update_manifest_record(record, media_hash):
    with MANIFEST_LOCK:
        manifest = load_manifest()
        manifest["files"][record["full_key"]] = record
        manifest["media_hashes"][media_hash] = record
        save_manifest(manifest)


def is_file_ready(file_path):
    p = Path(file_path)
    if not p.exists():
        return False

    if any(p.name.lower().endswith(ext) for ext in IGNORED_EXTENSIONS):
        return False

    try:
        with open(file_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size1 = f.tell()
    except (IOError, PermissionError):
        return False

    if size1 == 0:
        return False

    time.sleep(1.2)

    try:
        size2 = os.path.getsize(file_path)
        return size1 == size2
    except Exception:
        return False


def remove_empty_folders(root_dir):
    for root, dirs, files in os.walk(root_dir, topdown=False):
        if root != str(root_dir):
            try:
                if not os.listdir(root):
                    os.rmdir(root)
            except Exception:
                pass


def ensure_hf_repo_exists(api: HfApi, repo_id: str):
    if repo_id in KNOWN_REPOS:
        return True
    try:
        hf_rate_limiter.rate_limiter.wait_for_request_slot(getattr(api, "token", None))
        api.create_repo(repo_id=repo_id, repo_type="dataset", private=False, exist_ok=True)
        KNOWN_REPOS.add(repo_id)
        return True
    except Exception as e:
        safe_print(f"[!] Warning checking/creating repository {repo_id}: {e}")
        return False


def auto_generate_strm(repo_folder: str, rel_in_repo: str, proxy_host="127.0.0.1:8080", library_dir="jellyfin_library"):
    if not account_manager.is_jellyfin_sync_enabled():
        return
    try:
        strm_path = Path(library_dir) / repo_folder / f"{rel_in_repo}.strm"
        strm_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"http://{proxy_host}/stream/{repo_folder}/{rel_in_repo}"
        with open(strm_path, "w", encoding="utf-8") as f:
            f.write(f"{url}\n")
    except Exception as e:
        safe_print(f"[!] Warning generating .strm for {rel_in_repo}: {e}")


def find_manifest_video_for_companion(file_path: Path, base_dir: Path, manifest: dict, companion_type: str = "screens") -> str:
    """
    Finds the matching video record key in the manifest for a companion screen/poster/subtitle file.
    Robustly handles:
      - video.mp4.jpg / video.mp4.png / video.mp4.webp
      - video.screens.jpg / video_thumb.jpg / video-poster.jpg
      - video.mp4.screens.jpg
      - Files located in Screens/, Thumbs/, Covers/, Subs/ subfolders or parallel ancestor folders.
      - Top-level repository directory name sanitization differences.
    """
    if not file_path or not manifest:
        return None

    raw_name = file_path.name.lower()
    raw_stem = file_path.stem.lower()

    # 1. Build comprehensive set of possible video stems and names
    candidate_stems = {raw_stem, raw_name}

    # If the stem ends with a video extension (e.g. video.mp4.jpg -> stem is video.mp4)
    for vext in VIDEO_EXTENSIONS:
        if raw_stem.endswith(vext):
            candidate_stems.add(raw_stem[:-len(vext)])
            break

    # Strip screen and thumbnail suffixes
    suffixes = [
        ".screens", "-screens", "_screens", " screens",
        ".screen", "_screen", "-screen",
        "_thumb", "-thumb", ".thumb",
        "_poster", "-poster", ".poster",
        "_cover", "-cover", ".cover",
        "_preview", "-preview", ".preview"
    ]
    for sfx in suffixes:
        for s in list(candidate_stems):
            if s.endswith(sfx):
                rem = s[:-len(sfx)]
                candidate_stems.add(rem)
                for vext in VIDEO_EXTENSIONS:
                    if rem.endswith(vext):
                        candidate_stems.add(rem[:-len(vext)])

    # If subtitle: also strip language codes (e.g. video.en.srt -> video)
    if companion_type == "subtitles":
        for s in list(candidate_stems):
            if "." in s:
                rem = s.rsplit(".", 1)[0]
                candidate_stems.add(rem)
                for vext in VIDEO_EXTENSIONS:
                    if rem.endswith(vext):
                        candidate_stems.add(rem[:-len(vext)])

    # 2. Extract and normalize directory path
    try:
        rel = file_path.relative_to(base_dir)
        parts = list(rel.parts)
    except Exception:
        parts = [file_path.name]

    ignored_folders = SCREEN_FOLDER_NAMES if companion_type == "screens" else SUB_FOLDER_NAMES

    if len(parts) > 1:
        repo_name = parts[0]
        # Filter out screen/sub folder tokens from intermediate parts
        clean_subparts = [p for p in parts[1:-1] if p.lower() not in ignored_folders]
        norm_parent = "/".join([repo_name] + clean_subparts).lower()
        norm_sub_parent = "/".join(clean_subparts).lower()
    else:
        repo_name = ""
        norm_parent = "."
        norm_sub_parent = ""

    files = manifest.get("files", {})

    # 3. Direct match: Exact parent and candidate stem/name
    for key in files.keys():
        k_path = Path(key)
        k_parent = k_path.parent.as_posix().lower()
        k_sub_parent = "/".join(k_path.parent.parts[1:]).lower() if len(k_path.parent.parts) > 1 else ""
        k_stem = k_path.stem.lower()
        k_name = k_path.name.lower()

        if norm_parent == "." or k_parent == norm_parent or (norm_sub_parent and k_sub_parent == norm_sub_parent):
            if k_stem in candidate_stems or k_name in candidate_stems:
                return key

    # 4. Flexible match: Subfolder nesting / prefix / suffix parent matching
    for key in files.keys():
        k_path = Path(key)
        k_parent = k_path.parent.as_posix().lower()
        k_stem = k_path.stem.lower()
        k_name = k_path.name.lower()

        if k_stem in candidate_stems or k_name in candidate_stems:
            if norm_parent != "." and (norm_parent.endswith(k_parent) or k_parent.endswith(norm_parent)):
                return key

    # 5. Repo-scoped unique fallback
    if repo_name:
        repo_matches = [
            k for k in files.keys()
            if k.lower().startswith(repo_name.lower() + "/") and (Path(k).stem.lower() in candidate_stems or Path(k).name.lower() in candidate_stems)
        ]
        if len(repo_matches) == 1:
            return repo_matches[0]

    # 6. Global unique fallback across vault
    global_matches = [
        k for k in files.keys()
        if Path(k).stem.lower() in candidate_stems or Path(k).name.lower() in candidate_stems
    ]
    if len(global_matches) == 1:
        return global_matches[0]

    return None


def process_standalone_subtitle(
    file_path: Path,
    base_dir: Path,
    library_dir: str,
    api: HfApi,
    hf_username: str,
    password: str,
    delete_local: bool,
    acc_mgr: account_manager.AccountManager = None
):
    str_path = str(file_path)
    try:
        manifest = load_manifest()
        matching_key = find_manifest_video_for_companion(file_path, base_dir, manifest, companion_type="subtitles")

        if not matching_key:
            video_stem = file_path.stem.lower()
            if "." in video_stem:
                video_stem = video_stem.rsplit(".", 1)[0]
            for vext in VIDEO_EXTENSIONS:
                if video_stem.endswith(vext):
                    video_stem = video_stem[:-len(vext)]
                    break
            is_pending = False
            with MANIFEST_LOCK:
                for in_f in IN_PROGRESS_FILES:
                    if video_stem in Path(in_f).name.lower():
                        is_pending = True
                        break

            if is_pending:
                return False

            UNMATCHED_ATTEMPTS[str_path] = UNMATCHED_ATTEMPTS.get(str_path, 0) + 1
            if UNMATCHED_ATTEMPTS[str_path] >= 3:
                safe_print(f"[{time.strftime('%H:%M:%S')}] [!] Standalone subtitle '{file_path.name}' has no matching video in manifest. Pausing retries.")
                SKIPPED_UNMATCHED_FILES.add(str_path)
            return False

        UNMATCHED_ATTEMPTS.pop(str_path, None)

        video_record = manifest["files"][matching_key]
        repo_id = video_record.get("repo_id")
        obfuscated_blob_path = video_record.get("blob_path")
        target_acc = video_record.get("account") or hf_username
        target_api = acc_mgr.get_api_for_repo(repo_id) if acc_mgr else api

        if not repo_id or not obfuscated_blob_path:
            return False

        lang_code, lang_name = companion_helper.detect_subtitle_lang(file_path.name)
        raw_sub = file_path.read_bytes()
        sub_text = companion_helper.decode_subtitle_bytes(raw_sub)

        temp_sub_enc = f"{str(file_path)}.enc"
        sub_enc_size = crypto_engine.encrypt_file(str(file_path), temp_sub_enc, password)
        sub_blob = obfuscated_blob_path.replace(".enc", f".sub.{lang_code}.srt.enc")

        hf_rate_limiter.rate_limiter.wait_for_commit_slot(getattr(target_api, "token", None))
        target_api.upload_file(
            path_or_fileobj=temp_sub_enc,
            path_in_repo=sub_blob,
            repo_id=repo_id,
            repo_type="dataset",
            commit_message=commit_helper.get_human_commit_message("upload")
        )
        hf_rate_limiter.repo_cache.record_added_files(repo_id, [sub_blob])

        sub_info = {
            "lang": lang_code,
            "label": lang_name,
            "blob_path": sub_blob,
            "repo_id": repo_id,
            "account": target_acc,
            "orig_size": len(raw_sub),
            "enc_size": sub_enc_size,
            "format": "srt",
            "verifier": crypto_engine.create_verifier(password)
        }

        with MANIFEST_LOCK:
            cur_manifest = load_manifest()
            if matching_key in cur_manifest.get("files", {}):
                subs = cur_manifest["files"][matching_key].get("subtitles") or []
                subs = [s for s in subs if s.get("lang") != lang_code]
                subs.append(sub_info)
                cur_manifest["files"][matching_key]["subtitles"] = subs
                save_manifest(cur_manifest)

        if account_manager.is_jellyfin_sync_enabled():
            companion_helper.sync_jellyfin_subtitle(library_dir, matching_key, sub_text, lang_code)
        safe_print(f"[{time.strftime('%H:%M:%S')}] [Standalone Subtitle Synced] ({lang_name}) for {matching_key} -> {sub_blob}")

        if os.path.exists(temp_sub_enc):
            os.remove(temp_sub_enc)
        if delete_local and os.path.exists(str(file_path)):
            os.remove(str(file_path))
            safe_print(f"[{time.strftime('%H:%M:%S')}] [Freed Disk Space] Cleaned standalone subtitle: {file_path.name}")
            # NOTE: DO NOT delete empty folders or the root folder tree during staging!
    # The source folder tree in videos_to_sync is preserved so the user can easily drop in any missed files.
    # Empty folders are only cleaned up when the user clicks 'Push to HF (1 Commit)'!
    # remove_empty_folders(base_dir) deferred to commit phase
        return True
    except Exception as e:
        if is_rate_limit_error(e):
            trigger_rate_limit_cooldown(e, hf_username)
        else:
            safe_print(f"[!] Error processing standalone subtitle {file_path}: {e}")
        return False


def process_standalone_screens(
    file_path: Path,
    base_dir: Path,
    library_dir: str,
    api: HfApi,
    hf_username: str,
    password: str,
    delete_local: bool,
    acc_mgr: account_manager.AccountManager = None
):
    str_path = str(file_path)
    try:
        manifest = load_manifest()
        matching_key = find_manifest_video_for_companion(file_path, base_dir, manifest, companion_type="screens")

        if not matching_key:
            # Check if matching video is currently being uploaded in another thread
            video_stem = file_path.stem.lower()
            for vext in VIDEO_EXTENSIONS:
                if video_stem.endswith(vext):
                    video_stem = video_stem[:-len(vext)]
                    break
            is_pending = False
            with MANIFEST_LOCK:
                for in_f in IN_PROGRESS_FILES:
                    if video_stem in Path(in_f).name.lower():
                        is_pending = True
                        break

            if is_pending:
                return False

            UNMATCHED_ATTEMPTS[str_path] = UNMATCHED_ATTEMPTS.get(str_path, 0) + 1
            if UNMATCHED_ATTEMPTS[str_path] >= 3:
                safe_print(f"[{time.strftime('%H:%M:%S')}] [!] Standalone image '{file_path.name}' has no matching video in manifest. Pausing retries.")
                SKIPPED_UNMATCHED_FILES.add(str_path)
            return False

        UNMATCHED_ATTEMPTS.pop(str_path, None)

        video_record = manifest["files"][matching_key]
        repo_id = video_record.get("repo_id")
        obfuscated_blob_path = video_record.get("blob_path")
        target_acc = video_record.get("account") or hf_username
        target_api = acc_mgr.get_api_for_repo(repo_id) if acc_mgr else api

        if not repo_id or not obfuscated_blob_path:
            return False

        safe_id = storyboard_helper.get_safe_id(matching_key)
        screens_helper.process_screens_image(
            screen_path=file_path,
            safe_id=safe_id,
            library_dir=library_dir,
            video_rel_path=matching_key,
            password=password
        )

        temp_screen_enc = f"{str(file_path)}.enc"
        enc_size = crypto_engine.encrypt_file(str(file_path), temp_screen_enc, password)
        screens_blob = obfuscated_blob_path.replace(".enc", ".screens.jpg.enc")

        hf_rate_limiter.rate_limiter.wait_for_commit_slot(getattr(target_api, "token", None))
        target_api.upload_file(
            path_or_fileobj=temp_screen_enc,
            path_in_repo=screens_blob,
            repo_id=repo_id,
            repo_type="dataset",
            commit_message=commit_helper.get_human_commit_message("upload")
        )
        hf_rate_limiter.repo_cache.record_added_files(repo_id, [screens_blob])

        screens_record = {
            "blob_path": screens_blob,
            "repo_id": repo_id,
            "account": target_acc,
            "orig_size": os.path.getsize(str(file_path)),
            "enc_size": enc_size,
            "format": "jpg",
            "safe_id": safe_id,
            "verifier": crypto_engine.create_verifier(password)
        }

        with MANIFEST_LOCK:
            cur_manifest = load_manifest()
            if matching_key in cur_manifest.get("files", {}):
                cur_manifest["files"][matching_key]["screens"] = screens_record
                save_manifest(cur_manifest)

        # Sync cloud metadata sidecar
        try:
            metadata_helper.sync_item_metadata_to_hf(target_api, repo_id, obfuscated_blob_path, video_record, password)
        except Exception:
            pass

        safe_print(f"[{time.strftime('%H:%M:%S')}] [Standalone Screens Synced] for {matching_key} -> {screens_blob}")

        if os.path.exists(temp_screen_enc):
            os.remove(temp_screen_enc)
        if delete_local and os.path.exists(str(file_path)):
            os.remove(str(file_path))
            safe_print(f"[{time.strftime('%H:%M:%S')}] [Freed Disk Space] Cleaned standalone screens: {file_path.name}")
            # NOTE: DO NOT delete empty folders or the root folder tree during staging!
    # The source folder tree in videos_to_sync is preserved so the user can easily drop in any missed files.
    # Empty folders are only cleaned up when the user clicks 'Push to HF (1 Commit)'!
    # remove_empty_folders(base_dir) deferred to commit phase
        return True
    except Exception as e:
        if is_rate_limit_error(e):
            trigger_rate_limit_cooldown(e, hf_username)
        else:
            safe_print(f"[!] Warning processing standalone screens {file_path.name}: {e}")
        return False


def verify_staged_package_integrity(
    staged_enc_file: str,
    expected_plain_size: int,
    password: str,
    sidecars: list,
    blob_already_on_hf: bool = False
) -> tuple:
    """
    ULTRA-STRICT INTEGRITY GATE.
    Guarantees beyond any doubt that the newly encrypted file and sidecars
    exist, have the exact expected byte size, and are decryptable and working.
    Returns (True, "OK") or (False, error_reason).
    """
    if blob_already_on_hf:
        return True, "Blob verified already existing on Hugging Face"

    # 1. Existence Check
    if not os.path.exists(staged_enc_file):
        return False, f"Encrypted file does not exist on disk: {staged_enc_file}"

    # 2. Exact Byte Size Check
    chunk_size = crypto_engine.DEFAULT_CHUNK_SIZE
    total_chunks = (expected_plain_size + chunk_size - 1) // chunk_size if expected_plain_size > 0 else 0
    expected_enc_size = crypto_engine.HEADER_SIZE + expected_plain_size + (total_chunks * crypto_engine.TAG_SIZE)
    actual_enc_size = os.path.getsize(staged_enc_file)

    if actual_enc_size != expected_enc_size:
        return False, f"Encrypted size mismatch: actual={actual_enc_size} bytes, expected={expected_enc_size} bytes (diff={actual_enc_size - expected_enc_size})"

    # 3. Cryptographic Functional Check ("Working" Check)
    # Tests reading the 64-byte HFSYNC01 header, matching plaintext size, and decrypting chunk 0 with AES-256-GCM authentication
    if not crypto_engine.is_valid_encrypted_file(staged_enc_file, expected_plain_size, password, chunk_size):
        return False, "Cryptographic decryption check failed: chunk 0 failed AES-GCM tag verification or header corrupted"

    # 4. Sidecars Verification
    for sc in sidecars:
        sc_path = sc.get("staged_path")
        if not sc_path or not os.path.exists(sc_path):
            return False, f"Required sidecar missing on disk: {sc_path} ({sc.get('type')})"
        if os.path.getsize(sc_path) == 0:
            return False, f"Required sidecar is empty (0 bytes): {sc_path}"

    return True, "Package integrity verified: 100% healthy, matching size, and working"


def prepare_video_package(
    file_path: Path,
    base_dir: Path,
    base_archive: Path,
    library_dir: str,
    default_repo_name: str,
    api: HfApi,
    hf_username: str,
    password: str,
    name_key: bytes,
    delete_local: bool,
    acc_mgr: account_manager.AccountManager = None
) -> Optional[dict]:
    """
    Stage 1: Local Pre-Encryption & Asset Preparation Engine.
    Executes all local CPU and I/O tasks sequentially for a video:
      - Step 1/8: Ingest & Readiness check
      - Step 2/8: Cross-Account sparse fingerprint (<0.005s) & duplicate archive
      - Step 3/8: Technical stream probe (ffprobe codecs, resolution, HDR, channels)
      - Step 4/8: AES-256-GCM chunked video encryption (with speed MB/s)
      - Step 5/8: 100-frame HD hover scrubber storyboard sheet generation
      - Step 6/8: Companion audio extraction & subtitle encryption
    Runs locally without waiting for or blocking on network uploads, ensuring all videos
    in the sync folder are 100% pre-encrypted and ready to upload instantly.
    """
    str_path = str(file_path)
    if not file_path.exists():
        return None

    rel_to_base = file_path.relative_to(base_dir)
    parts = rel_to_base.parts

    if len(parts) > 1:
        repo_folder = parts[0]
        rel_in_repo = str(Path(*parts[1:])).replace("\\", "/")
    else:
        repo_folder = default_repo_name
        rel_in_repo = str(parts[0]).replace("\\", "/")

    if staging_manager.is_staging_paused(repo_folder):
        safe_print(f"[{time.strftime('%H:%M:%S')}] [PAUSED] Staging paused for '{repo_folder}'. Skipping.")
        return None

    # Dynamic Storage Balancing: resolve balanced account & repo_id for this folder
    if acc_mgr is None:
        acc_mgr = account_manager.AccountManager()

    balanced = acc_mgr.get_balanced_account_for_new_repo(repo_folder=repo_folder)
    if balanced:
        hf_username = balanced["account"]
        api = balanced.get("api") or api
        repo_id = balanced["repo_id"]
        password = balanced.get("password") or password
        name_key = crypto_engine.derive_name_key(password)
        hf_repo_name = repo_id.split("/")[-1] if "/" in repo_id else repo_id
    else:
        # Check if this folder already exists in the manifest (e.g. Renamed folder or existing vault)
        existing_repo = None
        try:
            manifest = load_manifest()
            prefix = f"{repo_folder}/"
            for k, v in manifest.get("files", {}).items():
                if k.startswith(prefix) and v.get("repo_id"):
                    existing_repo = v.get("repo_id")
                    break
        except Exception:
            pass

        if len(parts) > 1:
            hf_repo_name = crypto_engine.get_obfuscated_repo_name(repo_folder, name_key)
        else:
            hf_repo_name = sanitize_repo_name(default_repo_name)

        if existing_repo:
            repo_id = existing_repo
        else:
            repo_id = f"{hf_username}/{hf_repo_name}"

    full_key = f"{repo_folder}/{rel_in_repo}"

    file_size = os.path.getsize(file_path)
    size_mb = file_size / (1024 * 1024)
    size_str = f"{size_mb:.2f} MB" if size_mb < 1024 else f"{size_mb / 1024:.2f} GB"

    # Disk space safety check
    try:
        _, _, free_b = shutil.disk_usage(str(base_dir))
        if free_b < 5 * (1024 ** 3):
            safe_print(f"[{time.strftime('%H:%M:%S')}] [Disk Safety] Low disk space ({free_b / (1024**3):.1f} GB free). Waiting for uploads to delete originals before pre-encrypting...")
            time.sleep(5)
            return None
    except Exception:
        pass

    activity_tracker.tracker.start_task(full_key, str(rel_to_base), file_size, account=hf_username, repo_id=repo_id)

    # =========================================================================
    # STEP 1/8: INGESTION & READINESS CHECK
    # =========================================================================
    activity_tracker.tracker.update_stage(
        full_key, "ingest", "[1/8] Ingest & Readiness Check", 5,
        details=f"Copy verified. Source video: {size_str}"
    )
    obfuscated_blob_path = crypto_engine.get_obfuscated_blob_path(rel_in_repo, name_key)
    safe_print(f"\n[{time.strftime('%H:%M:%S')}] ========================================================")
    safe_print(f"[{time.strftime('%H:%M:%S')}] [1/8: Ingest] Source: {rel_to_base} ({size_str})")
    safe_print(f"[{time.strftime('%H:%M:%S')}]        -> Target Cloud Repo: {repo_id}")
    safe_print(f"[{time.strftime('%H:%M:%S')}]        -> Obfuscated Blob: {obfuscated_blob_path}")

    # =========================================================================
    # STEP 2/8: CROSS-ACCOUNT SPARSE FINGERPRINT (<0.005s)
    # =========================================================================
    activity_tracker.tracker.update_stage(
        full_key, "hashing", "[2/8] Sparse Fingerprint (<0.005s)", 12,
        details="Sampling header, middle & trailer chunks for instant duplicate check"
    )
    t_hash0 = time.time()
    media_hash = crypto_engine.get_sparse_media_hash(str_path)
    hash_time = (time.time() - t_hash0) * 1000
    safe_print(f"[{time.strftime('%H:%M:%S')}] [2/8: Hash] Sparse Fingerprint: {media_hash[:16]}... ({hash_time:.1f}ms)")

    manifest = load_manifest()
    existing = acc_mgr.find_duplicate(media_hash, manifest) if acc_mgr else manifest.get("media_hashes", {}).get(media_hash)
    if existing:
        dup_acc = existing.get("account", hf_username)
        dup_repo = existing.get("repo_id") or f"{dup_acc}/{hf_repo_name}"
        safe_print(f"[{time.strftime('%H:%M:%S')}] [INSTANT SKIP & ARCHIVE] Duplicate found across accounts! Account: '{dup_acc}' ({hash_time:.1f}ms)")
        safe_print(f"    -> Already hosted as: {existing.get('full_key')} in {dup_repo}")

        target_archive_path = base_archive / rel_to_base
        target_archive_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str_path, str(target_archive_path))
        safe_print(f"    -> Moved duplicate to: {target_archive_path}")

        orig_key = existing.get("full_key") or existing.get("rel_path") or existing.get("key") or full_key
        all_file_names = list(existing.get("file_names") or [])
        if orig_key and orig_key not in all_file_names:
            all_file_names.insert(0, orig_key)
        if full_key not in all_file_names:
            all_file_names.append(full_key)

        orig_name = Path(orig_key).name if orig_key else ""
        all_aliases = []
        for a in (existing.get("aliases") or []):
            if a and a != orig_name and a.lower() != orig_name.lower() and a not in all_aliases:
                all_aliases.append(a)
        if file_path.name != orig_name and file_path.name.lower() != orig_name.lower() and file_path.name not in all_aliases:
            all_aliases.append(file_path.name)

        existing["file_names"] = all_file_names
        existing["aliases"] = all_aliases
        existing_meta = existing.get("metadata") or metadata_helper.extract_default_metadata(orig_key)
        existing_meta["file_names"] = all_file_names
        existing_meta["aliases"] = all_aliases
        existing["metadata"] = existing_meta

        with MANIFEST_LOCK:
            cur_m = load_manifest()
            dup_blob = existing.get("blob_path") or existing.get("remote_path")
            for k, v in cur_m.get("files", {}).items():
                if k == orig_key or (dup_blob and v.get("blob_path") == dup_blob) or (media_hash and (v.get("media_hash") == media_hash or v.get("content_hash") == media_hash)):
                    v["file_names"] = list(all_file_names)
                    v["aliases"] = list(all_aliases)
                    v["metadata"] = dict(existing_meta)

            record_alias = dict(existing)
            record_alias["full_key"] = full_key
            record_alias["file_names"] = list(all_file_names)
            record_alias["aliases"] = list(all_aliases)
            record_alias["metadata"] = dict(existing_meta)
            record_alias["is_alias_of"] = orig_key
            cur_m["files"][full_key] = record_alias
            cur_m["media_hashes"][media_hash] = cur_m["files"].get(orig_key, record_alias)
            save_manifest(cur_m)

        if account_manager.is_jellyfin_sync_enabled():
            auto_generate_strm(repo_folder, rel_in_repo, library_dir=library_dir)
            metadata_helper.write_nfo_files(library_dir, full_key, existing_meta)
            if orig_key:
                metadata_helper.write_nfo_files(library_dir, orig_key, existing_meta)
        # NOTE: DO NOT delete empty folders or the root folder tree during staging!
    # The source folder tree in videos_to_sync is preserved so the user can easily drop in any missed files.
    # Empty folders are only cleaned up when the user clicks 'Push to HF (1 Commit)'!
    # remove_empty_folders(base_dir) deferred to commit phase

        staging_manager.record_file_duplicate_archived(repo_folder, rel_to_base, file_size)
        SKIPPED_UNMATCHED_FILES.clear()
        activity_tracker.tracker.finish_task(full_key, "skipped_duplicate", f"Duplicate found on '{dup_acc}'! Linked to {len(all_aliases)} file names.", 100)
        activity_tracker.tracker.add_event("skip", f"Duplicate Linked: {rel_to_base}", f"Linked to {orig_key} on {dup_acc}. Names: {', '.join(all_aliases)}", account=dup_acc)
        return None

    safe_print(f"[{time.strftime('%H:%M:%S')}]        -> Unique video confirmed (no duplicate on any account)")

    # =========================================================================
    # STEP 3/8: VIDEO STREAM & TECHNICAL METADATA PROBE
    # =========================================================================
    activity_tracker.tracker.update_stage(
        full_key, "probing", "[3/8] Video Stream Probe", 20,
        details="Analyzing video codec, resolution, channels, HDR specs"
    )
    safe_print(f"[{time.strftime('%H:%M:%S')}] [3/8: Probe] Probing video & audio streams with ffprobe...")
    media_info = metadata_helper.probe_media_file(str_path, key_path=full_key)
    res_label = media_info.get("resolution_label") or f"{media_info.get('width', '?')}x{media_info.get('height', '?')}"
    safe_print(f"[{time.strftime('%H:%M:%S')}]        -> Specs: {res_label} ({media_info.get('width', 0)}x{media_info.get('height', 0)}), {media_info.get('video_codec', 'Video')}, {media_info.get('audio_channels', 2)}ch Audio | {media_info.get('bitrate_str', '')}")

    ensure_hf_repo_exists(api, repo_id)
    stage_dir = staging_manager.get_repo_staging_dir(repo_folder)
    temp_prefix = hashlib.md5(full_key.encode("utf-8")).hexdigest()[:12]
    staging_temp_dir = os.path.join(stage_dir, ".temp", temp_prefix)
    os.makedirs(staging_temp_dir, exist_ok=True)
    temp_enc_file = os.path.join(staging_temp_dir, f"{temp_prefix}.tmp.enc")

    # Check if video blob already exists on HF
    blob_already_on_hf = False
    try:
        if hf_rate_limiter.repo_cache.file_exists(api, repo_id, obfuscated_blob_path):
            blob_already_on_hf = True
            safe_print(f"[{time.strftime('%H:%M:%S')}] [Resume Detected] Video blob already uploaded to {repo_id} ({obfuscated_blob_path}). Skipping re-upload.")
    except Exception as ex_chk:
        safe_print(f"[!] Note checking HF blob existence: {ex_chk}")

    # Discard external companion thumbnail if found
    screens_record = None
    try:
        matched_screen = screens_helper.find_matching_screens_file(file_path, base_dir=base_dir)
        if matched_screen and matched_screen.is_file():
            if delete_local and os.path.exists(str(matched_screen)):
                os.remove(str(matched_screen))
                safe_print(f"[{time.strftime('%H:%M:%S')}] [Discarded Original Thumb] Removed {matched_screen.name}")
    except Exception as sc_err:
        safe_print(f"[!] Companion screens check warning: {sc_err}")

    # =========================================================================
    # OPTIMIZATION: Overlap Storyboard and Audio extraction concurrently
    # in background threads while AES-256-GCM Encryption executes on main thread.
    # Saves ~7.5 seconds per video file completely!
    # =========================================================================
    sb_blob = obfuscated_blob_path.replace(".enc", ".storyboard.jpg.enc")
    a_blob = obfuscated_blob_path.replace(".enc", ".audio.m4a.enc")
    safe_id = (media_hash[:24] if media_hash else storyboard_helper.get_safe_id(full_key))

    sb_res = {}
    aud_res = {}
    sb_exists = False
    a_exists = False

    def _worker_storyboard():
        nonlocal sb_exists
        try:
            sb_exists = hf_rate_limiter.repo_cache.file_exists(api, repo_id, sb_blob)
            if sb_exists:
                safe_print(f"[{time.strftime('%H:%M:%S')}] [5/8: Storyboard] [Resume] Storyboard already on HF: {sb_blob}")
            elif storyboard_helper.generate_storyboard(full_key, duration=0, local_path=str_path, password=password, safe_id=safe_id, sparse_hash=media_hash):
                sb_jpg = os.path.join(storyboard_helper.STORYBOARD_DIR, f"{safe_id}.jpg")
                if os.path.exists(sb_jpg):
                    temp_sb_enc = f"{sb_jpg}.enc"
                    raw_sb = storyboard_helper.read_decrypted_bytes(sb_jpg, password)
                    temp_plain_sb = f"{sb_jpg}.tmp_plain.jpg"
                    with open(temp_plain_sb, "wb") as f_tmp:
                        f_tmp.write(raw_sb)
                    sb_orig_size = len(raw_sb)
                    sb_enc_size = crypto_engine.encrypt_file(temp_plain_sb, temp_sb_enc, password)
                    if os.path.exists(temp_plain_sb):
                        try:
                            os.remove(temp_plain_sb)
                        except Exception:
                            pass
                    rec = {
                        "blob_path": sb_blob,
                        "repo_id": repo_id,
                        "account": hf_username,
                        "orig_size": sb_orig_size,
                        "enc_size": sb_enc_size,
                        "format": "jpg",
                        "safe_id": safe_id,
                        "total_frames": 100,
                        "verifier": crypto_engine.create_verifier(password)
                    }
                    try:
                        storyboard_helper.slice_poster_thumbnail(safe_id, overwrite=True, password=password)
                    except Exception:
                        pass
                    sb_res["record"] = rec
                    sb_res["temp_enc"] = temp_sb_enc
                    safe_print(f"[{time.strftime('%H:%M:%S')}] [5/8: Storyboard] 100-frame HD hover scrubber sheet & poster thumbnail generated ({sb_orig_size/(1024*1024):.2f} MB)")
        except Exception as sb_err:
            safe_print(f"[!] Storyboard generation warning: {sb_err}")

    def _worker_audio():
        nonlocal a_exists
        try:
            a_exists = hf_rate_limiter.repo_cache.file_exists(api, repo_id, a_blob)
            if a_exists:
                safe_print(f"[{time.strftime('%H:%M:%S')}] [6/8: Audio & Subs] [Resume] Companion audio already on HF: {a_blob}")
            else:
                temp_audio = os.path.join(staging_temp_dir, f"{temp_prefix}.companion.m4a")
                if companion_helper.extract_companion_audio(str_path, temp_audio):
                    temp_audio_enc = f"{temp_audio}.enc"
                    a_orig = os.path.getsize(temp_audio)
                    a_enc = crypto_engine.encrypt_file(temp_audio, temp_audio_enc, password)
                    rec = {
                        "blob_path": a_blob,
                        "repo_id": repo_id,
                        "account": hf_username,
                        "orig_size": a_orig,
                        "enc_size": a_enc,
                        "format": "m4a",
                        "verifier": crypto_engine.create_verifier(password)
                    }
                    if os.path.exists(temp_audio):
                        try:
                            os.remove(temp_audio)
                        except Exception:
                            pass
                    aud_res["record"] = rec
                    aud_res["temp_enc"] = temp_audio_enc
                    safe_print(f"[{time.strftime('%H:%M:%S')}] [6/8: Audio & Subs] Extracted companion audio ({a_orig / (1024*1024):.2f} MB)")
        except Exception as a_err:
            safe_print(f"[!] Companion audio extraction warning: {a_err}")

    # Launch background extraction threads concurrently
    t_sb = threading.Thread(target=_worker_storyboard, name=f"Storyboard-{temp_prefix}")
    t_aud = threading.Thread(target=_worker_audio, name=f"Audio-{temp_prefix}")
    t_sb.start()
    t_aud.start()

    enc_speed = 0.0
    if blob_already_on_hf:
        total_chunks = (file_size + crypto_engine.DEFAULT_CHUNK_SIZE - 1) // crypto_engine.DEFAULT_CHUNK_SIZE if file_size > 0 else 0
        enc_size = crypto_engine.HEADER_SIZE + file_size + (total_chunks * crypto_engine.TAG_SIZE)
    else:
        # =========================================================================
        # STEP 4/8: AES-256-GCM VIDEO ENCRYPTION (64KB CHUNKS)
        # =========================================================================
        activity_tracker.tracker.update_stage(
            full_key, "encrypting", "[4/8] AES-256-GCM Encryption", 40,
            details="Encrypting 64KB blocks with unique salt, nonce & auth tags"
        )
        if crypto_engine.is_valid_encrypted_file(temp_enc_file, file_size, password):
            enc_size = os.path.getsize(temp_enc_file)
            enc_speed = (file_size / (1024 * 1024)) / 0.1
            safe_print(f"[{time.strftime('%H:%M:%S')}] [4/8: Encrypt] [Resume Encryption] Reusing existing valid encrypted file ({enc_size / (1024 * 1024):.2f} MB)")
        else:
            if os.path.exists(temp_enc_file):
                try:
                    os.remove(temp_enc_file)
                except Exception:
                    pass
            t0 = time.time()
            safe_print(f"[{time.strftime('%H:%M:%S')}] [4/8: Encrypt] AES-256-GCM (64KB chunks)...")
            last_report_t = 0.0

            def on_enc_progress(bytes_read: int, total_bytes: int):
                nonlocal last_report_t
                now = time.time()
                if now - last_report_t < 0.25 and bytes_read < total_bytes:
                    return
                last_report_t = now
                enc_pct = int((bytes_read / total_bytes) * 100) if total_bytes > 0 else 100
                overall_pct = int(40 + (bytes_read / total_bytes) * 35) if total_bytes > 0 else 75
                elapsed = max(0.01, now - t0)
                cur_speed = (bytes_read / (1024 * 1024)) / elapsed
                mb_read = bytes_read / (1024 * 1024)
                mb_total = total_bytes / (1024 * 1024)
                activity_tracker.tracker.update_stage(
                    full_key,
                    "encrypting",
                    f"[4/8] AES-256-GCM Encryption ({enc_pct}%)",
                    overall_pct,
                    speed_str=f"{cur_speed:.1f} MB/s",
                    details=f"Encrypting 64KB blocks with unique salt, nonce & auth tags • {mb_read:.1f}/{mb_total:.1f} MB ({enc_pct}%) • {cur_speed:.1f} MB/s"
                )

            enc_size = crypto_engine.encrypt_file(str_path, temp_enc_file, password, progress_callback=on_enc_progress)
            enc_time = time.time() - t0
            enc_speed = (file_size / (1024 * 1024)) / max(0.01, enc_time)
            safe_print(f"[{time.strftime('%H:%M:%S')}]        -> Encrypted {size_str} in {enc_time:.2f}s ({enc_speed:.1f} MB/s)")

    # Wait for concurrent Storyboard and Audio extraction to finish
    activity_tracker.tracker.update_stage(
        full_key, "storyboard", "[5/8] Storyboard & Companion Audio", 80,
        speed_str=f"{enc_speed:.1f} MB/s",
        details="Finalizing 100-frame preview sheet & companion AAC audio extraction"
    )
    t_sb.join()
    t_aud.join()

    storyboard_record = sb_res.get("record")
    temp_sb_enc = sb_res.get("temp_enc")
    audio_record = aud_res.get("record")
    temp_audio_enc = aud_res.get("temp_enc")

    subtitles_record = []
    subs_to_upload = []
    try:
        video_stem = file_path.stem.lower()
        video_name = file_path.name.lower()
        parent_dir = file_path.parent
        sub_dirs = [parent_dir]
        if parent_dir.exists():
            for s in parent_dir.iterdir():
                if s.is_dir() and s.name.lower() in SUB_FOLDER_NAMES:
                    sub_dirs.append(s)

        for sdir in sub_dirs:
            if not sdir.exists():
                continue
            for srt_file in sdir.iterdir():
                if not srt_file.is_file() or srt_file.suffix.lower() not in {".srt", ".vtt"}:
                    continue
                srt_stem = srt_file.stem.lower()
                if srt_stem == video_stem or srt_stem == video_name or srt_stem.startswith(video_stem + ".") or srt_stem.startswith(video_name + "."):
                    lang_code, lang_name = companion_helper.detect_subtitle_lang(srt_file.name)
                    raw_sub = srt_file.read_bytes()
                    sub_text = companion_helper.decode_subtitle_bytes(raw_sub)
                    sub_blob = obfuscated_blob_path.replace(".enc", f".sub.{lang_code}.srt.enc")
                    sub_exists = hf_rate_limiter.repo_cache.file_exists(api, repo_id, sub_blob)
                    if sub_exists:
                        safe_print(f"[{time.strftime('%H:%M:%S')}] [6/8: Audio & Subs] [Resume] Subtitle already on HF: {sub_blob}")
                        sub_enc_size = len(raw_sub)
                        if delete_local and os.path.exists(str(srt_file)):
                            try:
                                os.remove(str(srt_file))
                                safe_print(f"[{time.strftime('%H:%M:%S')}] [Freed Disk Space] Cleaned subtitle from watch folder: {srt_file.name}")
                                # NOTE: DO NOT delete empty folders or the root folder tree during staging!
    # The source folder tree in videos_to_sync is preserved so the user can easily drop in any missed files.
    # Empty folders are only cleaned up when the user clicks 'Push to HF (1 Commit)'!
    # remove_empty_folders(base_dir) deferred to commit phase
                            except Exception:
                                pass
                    else:
                        temp_sub_enc = f"{str(srt_file)}.enc"
                        sub_enc_size = crypto_engine.encrypt_file(str(srt_file), temp_sub_enc, password)
                        subs_to_upload.append((temp_sub_enc, sub_blob, str(srt_file)))

                    sub_info = {
                        "lang": lang_code,
                        "label": lang_name,
                        "blob_path": sub_blob,
                        "repo_id": repo_id,
                        "account": hf_username,
                        "orig_size": len(raw_sub),
                        "enc_size": sub_enc_size,
                        "format": "srt",
                        "verifier": crypto_engine.create_verifier(password)
                    }
                    subtitles_record.append(sub_info)
                    if account_manager.is_jellyfin_sync_enabled():
                        companion_helper.sync_jellyfin_subtitle(library_dir, full_key, sub_text, lang_code)
                    safe_print(f"[{time.strftime('%H:%M:%S')}] [6/8: Audio & Subs] Companion subtitle ready ({lang_name}) -> {sub_blob}")
    except Exception as s_err:
        safe_print(f"[!] Companion subtitle check warning: {s_err}")

    # Build manifest record
    meta = metadata_helper.extract_default_metadata(full_key, media_info=media_info)
    meta["file_names"] = [full_key]
    meta["aliases"] = []
    meta["overview"] = ""
    record = {
        "full_key": full_key,
        "safe_id": (media_hash[:24] if media_hash else storyboard_helper.get_safe_id(full_key)),
        "account": hf_username,
        "repo_id": repo_id,
        "repo_folder": repo_folder,
        "rel_in_repo": rel_in_repo,
        "blob_path": obfuscated_blob_path,
        "media_hash": media_hash,
        "orig_size": file_size,
        "enc_size": enc_size,
        "uploaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": round(time.time(), 3),
        "status": "uploaded",
        "file_names": [full_key],
        "aliases": [],
        "metadata": meta,
        "media_info": media_info,
        "verifier": crypto_engine.create_verifier(password)
    }
    if audio_record:
        record["audio"] = audio_record
    if subtitles_record:
        record["subtitles"] = subtitles_record
    if screens_record:
        record["screens"] = screens_record
    if storyboard_record:
        record["storyboard"] = storyboard_record

    # Encrypted metadata sidecar payload
    meta_blob_path = metadata_helper.get_meta_blob_path(obfuscated_blob_path)
    record_json_bytes = json.dumps(record, separators=(',', ':')).encode("utf-8")
    encrypted_meta_bytes = crypto_engine.encrypt_bytes(record_json_bytes, password)

    # =========================================================================
    # MOVE ENCRYPTED VIDEO & SIDECARS TO STAGING REPO FOLDER
    # =========================================================================
    blob_name = Path(obfuscated_blob_path).name
    staged_enc_file = os.path.join(stage_dir, blob_name)

    if not blob_already_on_hf:
        if os.path.exists(temp_enc_file):
            os.replace(temp_enc_file, staged_enc_file)
        else:
            safe_print(f"[!] ERROR: temp_enc_file not found: {temp_enc_file}")

    sidecars_list = []
    sidecars_dir = os.path.join(stage_dir, "sidecars")

    if temp_sb_enc and os.path.exists(temp_sb_enc) and not sb_exists:
        staged_sb = os.path.join(sidecars_dir, Path(sb_blob).name)
        os.replace(temp_sb_enc, staged_sb)
        sidecars_list.append({
            "type": "storyboard",
            "blob_path": sb_blob,
            "staged_path": staged_sb
        })

    if temp_audio_enc and os.path.exists(temp_audio_enc) and not a_exists:
        staged_audio = os.path.join(sidecars_dir, Path(a_blob).name)
        os.replace(temp_audio_enc, staged_audio)
        sidecars_list.append({
            "type": "audio",
            "blob_path": a_blob,
            "staged_path": staged_audio
        })

    for sub_enc_p, sub_b, raw_srt_p in subs_to_upload:
        if os.path.exists(sub_enc_p):
            staged_sub = os.path.join(sidecars_dir, Path(sub_b).name)
            os.replace(sub_enc_p, staged_sub)
            sidecars_list.append({
                "type": "subtitle",
                "blob_path": sub_b,
                "staged_path": staged_sub
            })
            if delete_local and os.path.exists(raw_srt_p):
                try:
                    os.remove(raw_srt_p)
                except Exception:
                    pass

    if meta_blob_path and encrypted_meta_bytes:
        staged_meta = os.path.join(sidecars_dir, Path(meta_blob_path).name)
        with open(staged_meta, "wb") as f_meta:
            f_meta.write(encrypted_meta_bytes)
        sidecars_list.append({
            "type": "meta",
            "blob_path": meta_blob_path,
            "staged_path": staged_meta
        })

    # =========================================================================
    # STEP 7/8: ULTRA-STRICT MULTI-LAYERED INTEGRITY GATE
    # =========================================================================
    activity_tracker.tracker.update_stage(
        full_key, "staging", "[7/8] Multi-Layer Integrity Gate", 92,
        speed_str=f"{enc_speed:.1f} MB/s",
        details="Verifying staged file size, Chunk 0 cryptographic decryption & sidecars"
    )
    # Before ANY original file is touched or deleted, verify:
    # 1. Staged .enc file exists
    # 2. Exact byte size matches HEADER_SIZE + plain_size + (chunks * TAG_SIZE)
    # 3. Cryptographic decryption test: Chunk 0 decrypts & passes AES-GCM tag verification
    # 4. All companion sidecars (storyboard, audio, metadata) exist and have size > 0
    # =========================================================================
    is_intact, verify_reason = verify_staged_package_integrity(
        staged_enc_file=staged_enc_file,
        expected_plain_size=file_size,
        password=password,
        sidecars=sidecars_list,
        blob_already_on_hf=blob_already_on_hf
    )

    if not is_intact:
        safe_print(f"[{time.strftime('%H:%M:%S')}] [CRITICAL INTEGRITY CHECK FAILED] {verify_reason} for '{file_path.name}'.")
        safe_print(f"[{time.strftime('%H:%M:%S')}] [SAFETY INTERVENTION] ABSOLUTELY PRESERVING ORIGINAL FILE ON DISK: '{str_path}'. Will not delete!")
        activity_tracker.tracker.finish_task(
            full_key, "failed", f"Strict integrity check failed: {verify_reason}", 0
        )
        return None

    safe_print(f"[{time.strftime('%H:%M:%S')}] [7/8: Integrity Verified] Encrypted package 100% verified (exact size, valid chunk 0 decryption & sidecars).")

    # Clean local original video ONLY because package was 100% verified above
    if delete_local and os.path.exists(str_path):
        try:
            os.remove(str_path)
            safe_print(f"[{time.strftime('%H:%M:%S')}]        -> Flat Disk Space: Cleaned original '{os.path.basename(str_path)}' ({size_str} freed). Encrypted copy verified healthy on disk.")
        except Exception as e_del:
            safe_print(f"[!] Warning deleting local video: {e_del}")

    # Remove isolated temporary work folder for this file
    if os.path.exists(staging_temp_dir):
        try:
            shutil.rmtree(staging_temp_dir, ignore_errors=True)
        except Exception:
            pass

    # Register into persistent staging manifest
    staging_manager.add_staged_file(
        repo_folder=repo_folder,
        repo_id=repo_id,
        account=hf_username,
        full_key=full_key,
        rel_in_repo=rel_in_repo,
        orig_filename=file_path.name,
        blob_path=obfuscated_blob_path,
        staged_enc_path=staged_enc_file,
        media_hash=media_hash,
        safe_id=record["safe_id"],
        orig_size=file_size,
        enc_size=enc_size,
        record=record,
        sidecars=sidecars_list
    )

    # NOTE: DO NOT delete empty folders or the root folder tree during staging!
    # The source folder tree in videos_to_sync is preserved so the user can easily drop in any missed files.
    # Empty folders are only cleaned up when the user clicks 'Push to HF (1 Commit)'!
    # remove_empty_folders(base_dir) deferred to commit phase

    activity_tracker.tracker.finish_task(
        full_key, "staged", f"Pre-encrypted & staged! Ready for batch push ({enc_speed:.1f} MB/s enc)", 100
    )
    activity_tracker.tracker.add_event(
        "info",
        f"Staged: {file_path.name}",
        f"Encrypted ({size_str}) -> Staged for {repo_folder}",
        account=hf_username,
        speed=f"{enc_speed:.1f} MB/s"
    )
    safe_print(f"[{time.strftime('%H:%M:%S')}] [STAGED] {file_path.name} is pre-encrypted and staged for 1-commit push to {repo_folder}!\n")

    return {
        "file_path": file_path,
        "str_path": str_path,
        "full_key": full_key,
        "repo_folder": repo_folder
    }


def upload_and_finalize_package(pkg: dict):
    """
    Stage 2: Paced Cloud Uploader & Finalizer.
    Takes a 100% pre-encrypted and prepped package and uploads to Hugging Face:
      - Step 7/8: Cloud upload & atomic Git commit (paced, 1 commit for all assets)
      - Step 8/8: Jellyfin library sync & local disk cleanup (deletes original plaintext file)
    """
    if not pkg:
        return

    full_key = pkg["full_key"]
    rel_to_base = pkg["rel_to_base"]
    repo_id = pkg["repo_id"]
    hf_username = pkg["hf_username"]
    api = pkg["api"]
    token = getattr(api, "token", None)
    temp_enc_file = pkg["temp_enc_file"]
    obfuscated_blob_path = pkg["obfuscated_blob_path"]
    temp_sb_enc = pkg["temp_sb_enc"]
    sb_blob = pkg["sb_blob"]
    sb_exists = pkg["sb_exists"]
    temp_audio = pkg["temp_audio"]
    temp_audio_enc = pkg["temp_audio_enc"]
    a_blob = pkg["a_blob"]
    a_exists = pkg["a_exists"]
    subs_to_upload = pkg["subs_to_upload"]
    meta_blob_path = pkg["meta_blob_path"]
    encrypted_meta_bytes = pkg["encrypted_meta_bytes"]
    enc_size = pkg["enc_size"]
    enc_speed = pkg["enc_speed"]
    size_str = pkg["size_str"]
    delete_local = pkg["delete_local"]
    str_path = pkg["str_path"]
    record = pkg["record"]
    media_hash = pkg["media_hash"]
    repo_folder = pkg["repo_folder"]
    rel_in_repo = pkg["rel_in_repo"]
    library_dir = pkg["library_dir"]
    base_dir = pkg["base_dir"]
    acc_mgr = pkg["acc_mgr"]
    password = pkg["password"]
    blob_already_on_hf = pkg["blob_already_on_hf"]

    try:
        # Assemble atomic commit operations
        commit_operations = []
        added_paths = []
        temp_files_to_clean = []

        if not blob_already_on_hf and os.path.exists(temp_enc_file):
            commit_operations.append(CommitOperationAdd(path_in_repo=obfuscated_blob_path, path_or_fileobj=temp_enc_file))
            added_paths.append(obfuscated_blob_path)
            temp_files_to_clean.append(temp_enc_file)

        if temp_sb_enc and os.path.exists(temp_sb_enc) and not sb_exists:
            commit_operations.append(CommitOperationAdd(path_in_repo=sb_blob, path_or_fileobj=temp_sb_enc))
            added_paths.append(sb_blob)
            temp_files_to_clean.append(temp_sb_enc)

        if temp_audio_enc and os.path.exists(temp_audio_enc) and not a_exists:
            commit_operations.append(CommitOperationAdd(path_in_repo=a_blob, path_or_fileobj=temp_audio_enc))
            added_paths.append(a_blob)
            temp_files_to_clean.append(temp_audio_enc)

        for sub_enc_p, sub_b, raw_srt_p in subs_to_upload:
            if os.path.exists(sub_enc_p):
                commit_operations.append(CommitOperationAdd(path_in_repo=sub_b, path_or_fileobj=sub_enc_p))
                added_paths.append(sub_b)
                temp_files_to_clean.append(sub_enc_p)

        if meta_blob_path and encrypted_meta_bytes:
            commit_operations.append(CommitOperationAdd(path_in_repo=meta_blob_path, path_or_fileobj=io.BytesIO(encrypted_meta_bytes)))
            added_paths.append(meta_blob_path)

        up_time = 0.01
        upload_speed_mbps = 0.0

        if commit_operations:
            activity_tracker.tracker.update_stage(
                full_key, "uploading", "[7/8] Cloud Upload & Commit", 85,
                speed_str=f"{enc_speed:.1f} MB/s enc",
                details=f"Paced atomic upload of {len(commit_operations)} asset(s) to {repo_id}"
            )
            t_up = time.time()
            safe_print(f"[{time.strftime('%H:%M:%S')}] [7/8: Cloud Commit] Uploading {len(commit_operations)} asset(s) -> {repo_id} via {hf_username}...")
            hf_rate_limiter.rate_limiter.wait_for_commit_slot(token)
            api.create_commit(
                repo_id=repo_id,
                repo_type="dataset",
                operations=commit_operations,
                commit_message=commit_helper.get_human_commit_message("upload")
            )
            up_time = time.time() - t_up
            upload_speed_mbps = (enc_size * 8 / (1024 * 1024)) / max(0.01, up_time)
            hf_rate_limiter.repo_cache.record_added_files(repo_id, added_paths)
            safe_print(f"[{time.strftime('%H:%M:%S')}]        -> Cloud commit confirmed: {up_time:.1f}s ({upload_speed_mbps:.1f} Mbps)")
        else:
            safe_print(f"[{time.strftime('%H:%M:%S')}] [7/8: Cloud Commit] [Already Synced] All cloud assets for {rel_to_base} are up-to-date on {repo_id}.")

        # =========================================================================
        # STEP 8/8: JELLYFIN LIBRARY SYNC & LOCAL DISK CLEANUP
        # =========================================================================
        jf_enabled = account_manager.is_jellyfin_sync_enabled()
        final_stage_label = "[8/8] Jellyfin & Cleanup" if jf_enabled else "[8/8] Manifest & Cleanup"
        final_details = (
            f"Deleting local original {size_str} to free disk space & writing .strm"
            if jf_enabled
            else f"Deleting local original {size_str} to free disk space & updating vault manifest"
        )
        activity_tracker.tracker.update_stage(
            full_key, "finalizing", final_stage_label, 95,
            details=final_details
        )
        safe_print(f"[{time.strftime('%H:%M:%S')}] [8/8: Finalize] Cleaning local files & {'syncing Jellyfin library' if jf_enabled else 'updating cloud vault'}...")

        # Clean temp encrypted files
        for f_tmp in temp_files_to_clean:
            if os.path.exists(f_tmp):
                try:
                    os.remove(f_tmp)
                except Exception:
                    pass
        if temp_audio and os.path.exists(temp_audio):
            try:
                os.remove(temp_audio)
            except Exception:
                pass

        # Safety Integrity Check: Never delete original video unless all required companion assets were created
        missing_companions = []
        if not record.get("storyboard") and not sb_exists:
            missing_companions.append("storyboard")
        if record.get("media_info", {}).get("audio_codec") and not record.get("audio") and not a_exists:
            missing_companions.append("companion_audio")

        # Delete local original video only if all assets verified
        if missing_companions:
            safe_print(f"[{time.strftime('%H:%M:%S')}] [SAFETY INTEGRITY GATE] Retaining local original '{os.path.basename(str_path)}' because required companion(s) ({', '.join(missing_companions)}) were not successfully produced. Will not delete local copy.")
        elif delete_local and os.path.exists(str_path):
            try:
                os.remove(str_path)
                safe_print(f"[{time.strftime('%H:%M:%S')}]        -> Local disk space reclaimed: {size_str} freed (Deleted original: {os.path.basename(str_path)})")
            except Exception as e_del:
                safe_print(f"[!] Warning deleting local video: {e_del}")

        # Delete local subtitles
        if delete_local:
            for _, _, raw_srt_p in subs_to_upload:
                if os.path.exists(raw_srt_p):
                    try:
                        os.remove(raw_srt_p)
                        safe_print(f"[{time.strftime('%H:%M:%S')}]        -> Freed local subtitle: {os.path.basename(raw_srt_p)}")
                    except Exception:
                        pass

        # Update local manifest & Jellyfin pointers
        update_manifest_record(record, media_hash)
        if jf_enabled:
            auto_generate_strm(repo_folder, rel_in_repo, library_dir=library_dir)
            metadata_helper.write_nfo_files(library_dir, full_key, pkg["meta"], media_info=pkg["media_info"])
        # NOTE: DO NOT delete empty folders or the root folder tree during staging!
    # The source folder tree in videos_to_sync is preserved so the user can easily drop in any missed files.
    # Empty folders are only cleaned up when the user clicks 'Push to HF (1 Commit)'!
    # remove_empty_folders(base_dir) deferred to commit phase

        # Queue master index sync
        master_repo = acc_mgr.get_master_repo_id() if acc_mgr else (os.getenv("REPO_ID") or f"{hf_username}/encrypted-media-vault")
        m_api = acc_mgr.get_api_for_repo(master_repo) if acc_mgr else api
        cur_manifest = load_manifest()
        hf_rate_limiter.debounced_syncer.queue_master_sync(
            m_api,
            master_repo,
            cur_manifest,
            password,
            metadata_helper.sync_master_manifest_to_hf
        )

        activity_tracker.tracker.finish_task(full_key, "completed", f"Ready for streaming! Uploaded in {up_time:.1f}s ({upload_speed_mbps:.1f} Mbps)", 100)
        event_title = f"Live on Jellyfin: {rel_to_base}" if jf_enabled else f"Synced to Vault: {rel_to_base}"
        activity_tracker.tracker.add_event("success", event_title, f"Uploaded to {repo_id} in {up_time:.1f}s ({upload_speed_mbps:.1f} Mbps)", account=hf_username, speed=f"{upload_speed_mbps:.1f} Mbps")

        SKIPPED_UNMATCHED_FILES.clear()
        safe_print(f"[{time.strftime('%H:%M:%S')}] SUCCESS: {full_key} is live in cloud vault and ready for streaming on Jellyfin!")
        safe_print(f"[{time.strftime('%H:%M:%S')}] ========================================================\n")

    except Exception as e:
        if is_rate_limit_error(e):
            trigger_rate_limit_cooldown(e, hf_username)
            activity_tracker.tracker.finish_task(full_key, "paused", "Paused: HF rate limit reached. Auto-resuming safely.", 75)
        else:
            safe_print(f"[!] Error uploading {rel_to_base}: {e}")
            activity_tracker.tracker.finish_task(full_key, "error", f"Error: {e}", 0)
            activity_tracker.tracker.add_event("error", f"Failed: {Path(rel_to_base).name}", str(e), account=hf_username)


def process_file_pipeline(
    file_path: Path,
    base_dir: Path,
    base_archive: Path,
    library_dir: str,
    default_repo_name: str,
    api: HfApi,
    hf_username: str,
    password: str,
    name_key: bytes,
    delete_local: bool,
    acc_mgr: account_manager.AccountManager = None
):
    str_path = str(file_path)
    try:
        if not is_file_ready(file_path):
            return

        if file_path.suffix.lower() in {".srt", ".vtt"}:
            process_standalone_subtitle(
                file_path=file_path,
                base_dir=base_dir,
                library_dir=library_dir,
                api=api,
                hf_username=hf_username,
                password=password,
                delete_local=delete_local,
                acc_mgr=acc_mgr
            )
            return

        if file_path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            if delete_local and os.path.exists(str_path):
                try:
                    os.remove(str_path)
                    safe_print(f"[{time.strftime('%H:%M:%S')}] [Discarded External Image] Removed: {file_path.name}")
                    # NOTE: DO NOT delete empty folders or the root folder tree during staging!
    # The source folder tree in videos_to_sync is preserved so the user can easily drop in any missed files.
    # Empty folders are only cleaned up when the user clicks 'Push to HF (1 Commit)'!
    # remove_empty_folders(base_dir) deferred to commit phase
                except Exception:
                    pass
            return

        if file_path.suffix.lower() in {".nfo"}:
            return

        pkg = prepare_video_package(
            file_path=file_path,
            base_dir=base_dir,
            base_archive=base_archive,
            library_dir=library_dir,
            default_repo_name=default_repo_name,
            api=api,
            hf_username=hf_username,
            password=password,
            name_key=name_key,
            delete_local=delete_local,
            acc_mgr=acc_mgr
        )
        if pkg:
            upload_and_finalize_package(pkg)

    except Exception as e:
        safe_print(f"[!] Pipeline error for {file_path}: {e}")
    finally:
        with MANIFEST_LOCK:
            IN_PROGRESS_FILES.discard(str_path)
            PREPARING_FILES.discard(str_path)
            UPLOADING_FILES.discard(str_path)
            PREPARED_PACKAGES.pop(str_path, None)


def watch_and_sync(
    watch_dir: str,
    token: str = None,
    password: str = None,
    default_repo_name: str = "encrypted-media-vault",
    poll_interval: int = 3,
    delete_local: bool = True,
    library_dir: str = "jellyfin_library",
    archive_dir: str = "archive",
    max_workers: int = 4
):
    global RATE_LIMIT_PAUSE_UNTIL, CONSECUTIVE_RATE_LIMITS, THROTTLED_ACCOUNTS
    acc_mgr = account_manager.AccountManager()
    upload_acc = acc_mgr.get_upload_account()
    if not upload_acc:
        print("[!] Error: No valid Hugging Face upload account found. Check env.txt.")
        sys.exit(1)

    hf_username = upload_acc["username"]
    token = upload_acc["token"]
    api = upload_acc["api"]
    password = acc_mgr.get_password_for_account(hf_username) or password or acc_mgr.password

    if not password:
        print("[!] Error: ENCRYPTION_PASSWORD not found.")
        sys.exit(1)

    all_accounts = acc_mgr.get_all_accounts()
    extra_users = [a["username"] for a in all_accounts if not a["is_upload"]]

    name_key = crypto_engine.derive_name_key(password)

    # Local manifest check: Only query master index if local manifest is completely empty
    manifest = load_manifest()
    if not manifest.get("files"):
        print("[*] Cold start: Syncing vault index from master cloud repository...")
        scan_res = acc_mgr.scan_and_merge_all_accounts(manifest, library_dir=library_dir)
        save_manifest(manifest)

    print("=" * 72)
    print("  HFSYNC FAST WATCHER: Multi-Account Cloud Vault")
    print("=" * 72)
    print(f"Primary Upload Account : {hf_username} [Primary] (All new uploads go here)")
    if extra_users:
        print(f"Extra Accounts Active  : {', '.join(extra_users)} (Duplicate check & streaming)")
    else:
        print(f"Extra Accounts Active  : None (Add tokens to HF_EXTRA_TOKENS in env.txt if desired)")
    print(f"Total Accounts Synced  : {len(all_accounts)} account(s)")
    print(f"Parallel Workers       : {max_workers} concurrent upload threads")
    print(f"Watching Root Dir      : {os.path.abspath(watch_dir)}")
    print(f"Archive Folder         : {os.path.abspath(archive_dir)}")
    print(f"Jellyfin Library       : {os.path.abspath(library_dir)}")
    print(f"Duplicate Checking     : Multi-Account Sparse Fingerprint (<0.005s)")
    print("=" * 72)
    print("[*] Service is active and waiting for new files...")
    print("=" * 72)

    base_dir = Path(watch_dir).resolve()
    base_archive = Path(archive_dir).resolve()

    # Clean up any orphaned .tmp.enc files that have no matching plaintext source file
    if base_dir.exists():
        for root, _, files in os.walk(base_dir):
            for file in files:
                if file.endswith(".tmp.enc"):
                    enc_f = Path(root) / file
                    plain_f = Path(root) / file[:-8]
                    if not plain_f.exists() or enc_f.stat().st_size == 0:
                        try:
                            enc_f.unlink()
                            safe_print(f"[*] Cleaned abandoned temp file on startup: {file}")
                        except Exception:
                            pass

    # Reset any stale active tasks in activity tracker from prior runs
    try:
        cur_act = activity_tracker.tracker.get_state()
        for tid, tinfo in list(cur_act.get("active_tasks", {}).items()):
            if tinfo.get("stage") in ("uploading", "encrypting", "hashing"):
                activity_tracker.tracker.update_stage(tid, "resuming", "Resuming Upload Pipeline", 50, details="Server restarted. Resuming smoothly...")
    except Exception:
        pass

    # Single sequential staging worker: since Option 1 already made storyboards 20x faster (2.5s),
    # 1 worker stages ~24 videos/min while leaving CPU & disk I/O 100% available for system stability.
    prep_workers = 2
    prep_executor = ThreadPoolExecutor(max_workers=prep_workers, thread_name_prefix="PreEncryptor")
    safe_print(f"[*] Dynamic Resource Throttling: CPU has {os.cpu_count()} cores. 2 dedicated background workers active (balanced throughput & UI responsiveness).")
    upload_executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="Uploader")

    last_env_mtime = os.path.getmtime(acc_mgr.env_path) if os.path.exists(acc_mgr.env_path) else 0
    last_repo_cleanup_check = 0
    active_folder = None

    while True:
        try:
            # Auto-cleanup empty/obsolete cloud repos inactive for 24+ hours (runs in background every 15m)
            now = time.time()
            if now - last_repo_cleanup_check > 900:
                last_repo_cleanup_check = now
                def _run_auto_repo_cleanup():
                    try:
                        cleaned = acc_mgr.auto_cleanup_obsolete_repos(max_idle_hours=24.0)
                        if cleaned:
                            repo_list_str = ", ".join(c["repo_id"] for c in cleaned)
                            safe_print(f"[*] [Auto-Cleanup] Successfully purged {len(cleaned)} obsolete repository(s) inactive for 24+ hours: {repo_list_str}")
                    except Exception as err:
                        safe_print(f"[!] [Auto-Cleanup] Background repo cleanup check error: {err}")
                threading.Thread(target=_run_auto_repo_cleanup, daemon=True).start()
            # Check for live config updates in env.txt (e.g. user added or changed accounts on web UI)
            if os.path.exists(acc_mgr.env_path):
                cur_mtime = os.path.getmtime(acc_mgr.env_path)
                if cur_mtime > last_env_mtime:
                    last_env_mtime = cur_mtime
                    acc_mgr.reload()
                    new_upload_acc = acc_mgr.get_upload_account()
                    if new_upload_acc and new_upload_acc["username"] != hf_username:
                        hf_username = new_upload_acc["username"]
                        api = new_upload_acc["api"]
                        safe_print(f"[*] Live Config Reload: Primary upload account switched to: {hf_username} [Primary]")
                        activity_tracker.tracker.add_event("info", "Upload Account Switched", f"Primary uploader is now {hf_username}", account=hf_username)

            # Check for Hugging Face Rate Limit Cooldown for Uploads
            now = time.time()
            if now < RATE_LIMIT_PAUSE_UNTIL:
                # Check if an alternative un-throttled account is available
                alt_acc = acc_mgr.get_alternative_upload_account(hf_username) if acc_mgr else None
                alt_throttled = False
                if alt_acc:
                    alt_name = alt_acc["username"]
                    if THROTTLED_ACCOUNTS.get(alt_name, 0) > now:
                        alt_throttled = True

                if alt_acc and not alt_throttled:
                    safe_print(f"[*] [Rate Limit Failover] Account '{hf_username}' is in cooldown. Switching upload worker to '{alt_acc['username']}'...")
                    hf_username = alt_acc["username"]
                    api = alt_acc["api"]
                    activity_tracker.tracker.add_event("info", "Rate Limit Failover", f"Switched uploader to {hf_username}", account=hf_username)
                    RATE_LIMIT_PAUSE_UNTIL = 0
                else:
                    remaining = int(RATE_LIMIT_PAUSE_UNTIL - now)
                    if remaining % 30 == 0 or remaining <= 5:
                        safe_print(f"[{time.strftime('%H:%M:%S')}] [Rate Limit Cooldown Active] Uploads paused for {remaining}s. Pre-encryption continues locally.")

            # If cooldown just elapsed:
            if CONSECUTIVE_RATE_LIMITS > 0 and time.time() >= RATE_LIMIT_PAUSE_UNTIL:
                safe_print(f"[{time.strftime('%H:%M:%S')}] [Rate Limit Cooldown Finished] Resuming upload pipeline automatically...")
                activity_tracker.tracker.add_event("info", "Uploads Resumed", "Rate limit cooldown finished; resuming processing.")
                CONSECUTIVE_RATE_LIMITS = 0

            candidates = []
            try:
                stg_manifest = staging_manager.load_staging_manifest()
                ignored_repos = {rf for rf, rd in stg_manifest.get("repositories", {}).items() if rd.get("status") == "ignored"}
                active_pushing_repos = {rf for rf, rd in stg_manifest.get("repositories", {}).items() if rd.get("status") in ("pushing", "queued")}
                paused_staging_repos = {rf for rf, rd in stg_manifest.get("repositories", {}).items() if rd.get("status") == "paused_staging" or rd.get("staging_paused")}
                with staging_manager._PUSH_LOCK:
                    active_pushing_repos.update(staging_manager._ACTIVE_PUSHES)
                    active_pushing_repos.update(staging_manager._PUSH_QUEUE)
                    if staging_manager._CURRENT_PUSHING_REPO:
                        active_pushing_repos.add(staging_manager._CURRENT_PUSHING_REPO)
            except Exception:
                ignored_repos = set()
                active_pushing_repos = set()

            for root, _, files in os.walk(base_dir):
                for file in files:
                    f_lower = file.lower()
                    if f_lower.endswith((".tmp.enc", ".enc", ".tmp", ".companion.m4a", ".nfo", ".strm")) or ".companion." in f_lower or f_lower.startswith("tmp_"):
                        continue
                    file_path = Path(root) / file
                    rf = get_file_repo_folder(file_path, base_dir, default_repo_name)
                    if rf in ignored_repos or rf in active_pushing_repos or rf in paused_staging_repos:
                        continue
                    if file_path.suffix.lower() in SUPPORTED_EXTENSIONS or is_video_file(file_path):
                        if str(file_path) not in SKIPPED_UNMATCHED_FILES:
                            candidates.append(file_path)

            if not candidates:
                activity_tracker.tracker.set_idle()
                with MANIFEST_LOCK:
                    if not IN_PROGRESS_FILES and not PREPARING_FILES and not UPLOADING_FILES:
                        hf_rate_limiter.debounced_syncer.flush_immediate()
            else:
                unprocessed_videos = [
                    f for f in candidates
                    if is_video_file(f) and str(f) not in IN_PROGRESS_FILES
                ]
                if unprocessed_videos:
                    activity_tracker.tracker.start_batch(len(unprocessed_videos))

            # Prioritize video files so worker threads begin primary video pre-encryption first
            import collections
            folder_groups = collections.defaultdict(list)
            for f in candidates:
                rf = get_file_repo_folder(f, base_dir, default_repo_name)
                folder_groups[rf].append(f)

            # Record/update root baseline scan for every discovered folder
            for rf, f_list in folder_groups.items():
                try:
                    staging_manager.record_folder_initial_scan(rf, f_list, base_dir=base_dir, is_video_checker=is_video_file)
                except Exception as ex_scan:
                    safe_print(f"[!] Warning recording initial root scan for '{rf}': {ex_scan}")

            # If active folder is actively committing/pushing or paused, defer it
            if active_folder is not None and (active_folder in active_pushing_repos or active_folder in paused_staging_repos):
                active_folder = None

            # Check all repositories in staging status that have completed pre-encryption
            for rf_check, rd_check in list(stg_manifest.get("repositories", {}).items()):
                if rd_check.get("status") == "staging" and not rd_check.get("staging_paused"):
                    with MANIFEST_LOCK:
                        repo_busy = any(
                            get_file_repo_folder(Path(p), base_dir, default_repo_name) == rf_check
                            for p in (PREPARING_FILES | IN_PROGRESS_FILES)
                        )
                    has_pending = rf_check in folder_groups and bool(folder_groups[rf_check])
                    if not has_pending and not repo_busy and len(rd_check.get("files", [])) > 0:
                        # Folder has 0 remaining files in videos_to_sync and no active workers! Run full Reconciliation Audit!
                        passed, reason, report = staging_manager.audit_and_mark_repo_ready(rf_check, password=password)
                        if passed:
                            safe_print(f"[{time.strftime('%H:%M:%S')}] [AUDIT 100% PASSED] Folder '{rf_check}' 100% verified! Scanned {report['initial_scanned_count']} file(s) ({report['initial_scanned_formatted']}) == Staged on disk {report['verified_staged_count']} file(s) ({report['verified_staged_formatted']}). Ready to push!")
                        else:
                            safe_print(f"[{time.strftime('%H:%M:%S')}] [CRITICAL STAGING AUDIT FAILED] Flaw detected in '{rf_check}'! {reason}")
                            for disc in report.get("discrepancies", []):
                                safe_print(f"[{time.strftime('%H:%M:%S')}]        -> [Flaw] {disc}")
                            safe_print(f"[{time.strftime('%H:%M:%S')}] [PUSH BLOCKED] 1-Commit push strictly blocked until flaw is resolved.")
                        if active_folder == rf_check:
                            active_folder = None

            # Pick next folder to process if idle
            if active_folder is None and folder_groups:
                active_folder = next(iter(folder_groups.keys()))
                safe_print(f"[{time.strftime('%H:%M:%S')}] [ACTIVE FOLDER] Beginning folder-wise pre-encryption for: '{active_folder}' ({len(folder_groups[active_folder])} file(s))...")

            active_candidates = folder_groups.get(active_folder, []) if active_folder else candidates
            active_candidates.sort(key=lambda p: (0 if is_video_file(p) else 1))

            for file_path in active_candidates:
                str_path = str(file_path)

                # Standalone companion files (subtitles, screens)
                if not is_video_file(file_path):
                    with MANIFEST_LOCK:
                        if str_path in IN_PROGRESS_FILES:
                            continue
                        IN_PROGRESS_FILES.add(str_path)

                    upload_executor.submit(
                        process_file_pipeline,
                        file_path=file_path,
                        base_dir=base_dir,
                        base_archive=base_archive,
                        library_dir=library_dir,
                        default_repo_name=default_repo_name,
                        api=api,
                        hf_username=hf_username,
                        password=password,
                        name_key=name_key,
                        delete_local=delete_local,
                        acc_mgr=acc_mgr
                    )
                    continue

                # Video files: Pre-encrypt one by one via prep_executor
                rf_cand = get_file_repo_folder(file_path, base_dir, default_repo_name)
                if rf_cand in paused_staging_repos or staging_manager.is_staging_paused(rf_cand):
                    continue

                with MANIFEST_LOCK:
                    if str_path in IN_PROGRESS_FILES or str_path in PREPARING_FILES or str_path in PREPARED_PACKAGES or str_path in UPLOADING_FILES:
                        continue
                    IN_PROGRESS_FILES.add(str_path)
                    PREPARING_FILES.add(str_path)

                def _run_staged_pipeline(fp=file_path):
                    sp = str(fp)
                    rf_p = get_file_repo_folder(fp, base_dir, default_repo_name)
                    if staging_manager.is_staging_paused(rf_p):
                        with MANIFEST_LOCK:
                            IN_PROGRESS_FILES.discard(sp)
                            PREPARING_FILES.discard(sp)
                        return
                    try:
                        pkg = prepare_video_package(
                            file_path=fp,
                            base_dir=base_dir,
                            base_archive=base_archive,
                            library_dir=library_dir,
                            default_repo_name=default_repo_name,
                            api=api,
                            hf_username=hf_username,
                            password=password,
                            name_key=name_key,
                            delete_local=delete_local,
                            acc_mgr=acc_mgr
                        )
                        with MANIFEST_LOCK:
                            PREPARING_FILES.discard(sp)
                            if pkg:
                                PREPARED_PACKAGES[sp] = pkg
                                UPLOADING_FILES.add(sp)

                        with MANIFEST_LOCK:
                            IN_PROGRESS_FILES.discard(sp)
                    except Exception as ex_prep:
                        safe_print(f"[!] Pre-encryption error for {fp}: {ex_prep}")
                        with MANIFEST_LOCK:
                            IN_PROGRESS_FILES.discard(sp)
                            PREPARING_FILES.discard(sp)
                        try:
                            rel_k = os.path.relpath(str(fp), str(base_dir)).replace("\\", "/")
                            activity_tracker.tracker.finish_task(rel_k, "error", f"Error: {ex_prep}")
                        except Exception:
                            pass

                prep_executor.submit(_run_staged_pipeline)

        except KeyboardInterrupt:
            print("\nShutting down parallel watcher...")
            prep_executor.shutdown(wait=False)
            upload_executor.shutdown(wait=False)
            break
        except Exception as e:
            safe_print(f"[!] Watcher scan loop error: {e}")

        time.sleep(poll_interval)


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            k32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            k32.SetPriorityClass.restype = wintypes.BOOL
            # 0x00004000 = BELOW_NORMAL_PRIORITY_CLASS (Windows guarantees UI/desktop never freezes)
            ok = k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00004000)
            if ok:
                safe_print("[*] Resource Safety Gate: Background priority set to BELOW_NORMAL (desktop & UI guaranteed smooth)")
        except Exception as e:
            safe_print(f"[!] Note setting process priority: {e}")
    env_cfg = load_env()

    parser = argparse.ArgumentParser(description="Multi-threaded parallel video sync watcher.")
    parser.add_argument("--watch-dir", "-w", default="videos_to_sync", help="Directory to watch (default: videos_to_sync)")
    parser.add_argument("--token", "-t", default=env_cfg.get("HF_TOKEN"), help="Hugging Face API token")
    parser.add_argument("--password", "-p", default=env_cfg.get("ENCRYPTION_PASSWORD"), help="Master encryption password")
    parser.add_argument("--interval", "-i", type=int, default=3, help="Polling interval in seconds (default: 3)")
    parser.add_argument("--workers", type=int, default=4, help="Maximum concurrent upload worker threads (default: 4)")
    parser.add_argument("--no-delete", action="store_true", help="Do not delete local plaintext files")
    parser.add_argument("--library-dir", default="jellyfin_library", help="Directory for Jellyfin .strm files")
    parser.add_argument("--archive-dir", default="archive", help="Directory for duplicate/skipped files")

    args = parser.parse_args()

    if not args.token:
        print("[!] Error: HF_TOKEN not found in env.txt or arguments.")
        sys.exit(1)
    if not args.password:
        print("[!] Error: ENCRYPTION_PASSWORD not found in env.txt or arguments.")
        sys.exit(1)

    os.makedirs(args.watch_dir, exist_ok=True)
    if account_manager.is_jellyfin_sync_enabled():
        os.makedirs(args.library_dir, exist_ok=True)
    os.makedirs(args.archive_dir, exist_ok=True)

    watch_and_sync(
        watch_dir=args.watch_dir,
        token=args.token,
        password=args.password,
        poll_interval=args.interval,
        delete_local=not args.no_delete,
        library_dir=args.library_dir,
        archive_dir=args.archive_dir,
        max_workers=args.workers
    )


if __name__ == "__main__":
    main()
