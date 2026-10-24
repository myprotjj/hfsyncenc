import os
import sys
import time
import json
import re
import shutil
import argparse
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from huggingface_hub import HfApi
import crypto_engine
import commit_helper
import storyboard_helper
import metadata_helper
import account_manager
import activity_tracker
import companion_helper
import screens_helper

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

SUPPORTED_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm",
    ".flv", ".ts", ".m4v", ".srt", ".vtt", ".nfo",
    ".jpg", ".jpeg", ".png", ".webp"
}

VIDEO_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm",
    ".flv", ".ts", ".m4v"
}

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
    clean = re.sub(r'[^a-zA-Z0-9_\-\.]', '-', name).strip('-._')
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
        os.replace(temp_path, MANIFEST_FILE)


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
        api.create_repo(repo_id=repo_id, repo_type="dataset", private=False, exist_ok=True)
        KNOWN_REPOS.add(repo_id)
        return True
    except Exception as e:
        safe_print(f"[!] Warning checking/creating repository {repo_id}: {e}")
        return False


def auto_generate_strm(repo_folder: str, rel_in_repo: str, proxy_host="127.0.0.1:8080", library_dir="jellyfin_library"):
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
        repo_name = sanitize_repo_name(parts[0])
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

        target_api.upload_file(
            path_or_fileobj=temp_sub_enc,
            path_in_repo=sub_blob,
            repo_id=repo_id,
            repo_type="dataset",
            commit_message=commit_helper.get_human_commit_message("upload")
        )

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

        companion_helper.sync_jellyfin_subtitle(library_dir, matching_key, sub_text, lang_code)
        safe_print(f"[{time.strftime('%H:%M:%S')}] [Standalone Subtitle Synced] ({lang_name}) for {matching_key} -> {sub_blob}")

        if os.path.exists(temp_sub_enc):
            os.remove(temp_sub_enc)
        if delete_local and os.path.exists(str(file_path)):
            os.remove(str(file_path))
            safe_print(f"[{time.strftime('%H:%M:%S')}] [Freed Disk Space] Cleaned standalone subtitle: {file_path.name}")
            remove_empty_folders(base_dir)
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
            video_rel_path=matching_key
        )

        temp_screen_enc = f"{str(file_path)}.enc"
        enc_size = crypto_engine.encrypt_file(str(file_path), temp_screen_enc, password)
        screens_blob = obfuscated_blob_path.replace(".enc", ".screens.jpg.enc")

        target_api.upload_file(
            path_or_fileobj=temp_screen_enc,
            path_in_repo=screens_blob,
            repo_id=repo_id,
            repo_type="dataset",
            commit_message=commit_helper.get_human_commit_message("upload")
        )

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
            remove_empty_folders(base_dir)
        return True
    except Exception as e:
        if is_rate_limit_error(e):
            trigger_rate_limit_cooldown(e, hf_username)
        else:
            safe_print(f"[!] Warning processing standalone screens {file_path.name}: {e}")
        return False


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
            process_standalone_screens(
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

        if file_path.suffix.lower() in {".nfo"}:
            return

        rel_to_base = file_path.relative_to(base_dir)
        parts = rel_to_base.parts

        if len(parts) > 1:
            raw_repo_name = parts[0]
            repo_folder = sanitize_repo_name(raw_repo_name)
            rel_in_repo = str(Path(*parts[1:])).replace("\\", "/")
        else:
            repo_folder = sanitize_repo_name(default_repo_name)
            rel_in_repo = str(parts[0]).replace("\\", "/")

        if len(parts) > 1:
            hf_repo_name = crypto_engine.get_obfuscated_repo_name(repo_folder, name_key)
        else:
            hf_repo_name = sanitize_repo_name(default_repo_name)

        repo_id = f"{hf_username}/{hf_repo_name}"
        full_key = f"{repo_folder}/{rel_in_repo}"

        file_size = os.path.getsize(file_path)
        size_mb = file_size / (1024 * 1024)

        activity_tracker.tracker.start_task(full_key, str(rel_to_base), file_size, account=hf_username, repo_id=repo_id)

        # 1. Instant Sparse Media Hash (< 0.005s)
        activity_tracker.tracker.update_stage(full_key, "hashing", "Sparse Media Fingerprinting (<0.005s)", 15)
        t_hash0 = time.time()
        media_hash = crypto_engine.get_sparse_media_hash(str_path)
        hash_time = (time.time() - t_hash0) * 1000

        manifest = load_manifest()

        # Check for duplicate across ALL connected accounts
        activity_tracker.tracker.update_stage(full_key, "dup_check", "Cross-Account Duplicate Check", 25)
        existing = acc_mgr.find_duplicate(media_hash, manifest) if acc_mgr else manifest.get("media_hashes", {}).get(media_hash)
        if existing:
            dup_acc = existing.get("account", hf_username)
            dup_repo = existing.get("repo_id", f"{dup_acc}/{hf_repo_name}")
            safe_print(f"\n[{time.strftime('%H:%M:%S')}] [INSTANT SKIP & ARCHIVE] Duplicate found across accounts! Account: '{dup_acc}' ({hash_time:.1f}ms)")
            safe_print(f"    -> Already hosted as: {existing.get('full_key')} in {dup_repo}")

            target_archive_path = base_archive / rel_to_base
            target_archive_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str_path, str(target_archive_path))
            safe_print(f"    -> Moved duplicate to: {target_archive_path}")

            # Maintain combined list of all file names and aliases for this video
            orig_key = existing.get("full_key") or existing.get("rel_path") or existing.get("key")
            if not orig_key:
                for k, v in manifest.get("files", {}).items():
                    if v == existing or v.get("blob_path") == existing.get("blob_path"):
                        orig_key = k
                        break
            if not orig_key:
                orig_key = full_key

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
            # Only save alternate filename IF it actually differs from primary filename
            if file_path.name != orig_name and file_path.name.lower() != orig_name.lower():
                if file_path.name not in all_aliases:
                    all_aliases.append(file_path.name)

            existing["file_names"] = all_file_names
            existing["aliases"] = all_aliases

            existing_meta = existing.get("metadata") or metadata_helper.extract_default_metadata(orig_key)
            existing_meta["file_names"] = all_file_names
            existing_meta["aliases"] = all_aliases
            existing["metadata"] = existing_meta

            # Update records in manifest
            with MANIFEST_LOCK:
                cur_m = load_manifest()
                dup_blob = existing.get("blob_path") or existing.get("remote_path")
                # Update any existing records sharing this blob
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

            auto_generate_strm(repo_folder, rel_in_repo, library_dir=library_dir)
            metadata_helper.write_nfo_files(library_dir, full_key, existing_meta)
            if orig_key:
                metadata_helper.write_nfo_files(library_dir, orig_key, existing_meta)
            remove_empty_folders(base_dir)

            # Sync updated list of names to Hugging Face cloud metadata
            dup_blob = existing.get("blob_path") or existing.get("remote_path")
            if dup_blob:
                api_to_use = acc_mgr.get_api_for_repo(dup_repo) if acc_mgr else api
                try:
                    metadata_helper.sync_item_metadata_to_hf(api_to_use, dup_repo, dup_blob, existing, password)
                except Exception as e:
                    safe_print(f"[!] Warning syncing updated aliases to HF: {e}")

            SKIPPED_UNMATCHED_FILES.clear()
            activity_tracker.tracker.finish_task(full_key, "skipped_duplicate", f"Duplicate found on '{dup_acc}'! Linked to {len(all_aliases)} file names.", 100)
            activity_tracker.tracker.add_event("skip", f"Duplicate Linked: {rel_to_base}", f"Linked to {orig_key} on {dup_acc}. Names: {', '.join(all_aliases)}", account=dup_acc)
            return

        safe_print(f"\n[{time.strftime('%H:%M:%S')}] NEW VIDEO: {rel_to_base} ({size_mb:.2f} MB)")
        safe_print(f"    -> Sparse Media Hash: {media_hash[:16]}... ({hash_time:.1f}ms)")

        # 2. Obfuscated Blob Destination on HF
        obfuscated_blob_path = crypto_engine.get_obfuscated_blob_path(rel_in_repo, name_key)
        ensure_hf_repo_exists(api, repo_id)

        temp_enc_file = f"{str_path}.tmp.enc"

        # 3. Fast AES-256-GCM Encryption (Pure throughput, no extra hashing)
        activity_tracker.tracker.update_stage(full_key, "encrypting", "AES-256-GCM Encryption", 40, details="Encrypting 64KB blocks...")
        t0 = time.time()
        safe_print(f"[{time.strftime('%H:%M:%S')}] [Encrypting] AES-256-GCM (64KB chunks)...")
        enc_size = crypto_engine.encrypt_file(str_path, temp_enc_file, password)
        enc_time = time.time() - t0
        enc_speed = (file_size / (1024 * 1024)) / max(0.01, enc_time)
        safe_print(f"[{time.strftime('%H:%M:%S')}] [Encrypted] {rel_to_base} in {enc_time:.2f}s ({enc_speed:.1f} MB/s)")

        # 4. Multi-Threaded Upload to Hugging Face
        activity_tracker.tracker.update_stage(full_key, "uploading", f"Uploading to {repo_id}", 65, speed_str=f"{enc_speed:.1f} MB/s enc", details=f"Uploading encrypted blob to {repo_id}")
        t_up = time.time()
        safe_print(f"[{time.strftime('%H:%M:%S')}] [Uploading via {hf_username}] -> {repo_id} ({obfuscated_blob_path}) ...")
        api.upload_file(
            path_or_fileobj=temp_enc_file,
            path_in_repo=obfuscated_blob_path,
            repo_id=repo_id,
            repo_type="dataset",
            commit_message=commit_helper.get_human_commit_message("upload")
        )
        up_time = time.time() - t_up
        upload_speed_mbps = (enc_size * 8 / (1024 * 1024)) / max(0.01, up_time)
        safe_print(f"[{time.strftime('%H:%M:%S')}] [Upload Complete] {rel_to_base} in {up_time:.2f}s ({upload_speed_mbps:.1f} Mbps)")

        # 4a. Detect and Process Companion Screens / Poster from Watch Folder
        # Established BEFORE storyboard generation so companion covers are preserved without ffmpeg overwrite
        screens_record = None
        try:
            matched_screen = screens_helper.find_matching_screens_file(file_path, base_dir=base_dir)
            if matched_screen and matched_screen.is_file():
                safe_id = storyboard_helper.get_safe_id(full_key)
                screens_helper.process_screens_image(
                    screen_path=matched_screen,
                    safe_id=safe_id,
                    library_dir=library_dir,
                    video_rel_path=full_key
                )
                temp_screen_enc = f"{str(matched_screen)}.enc"
                enc_sc_size = crypto_engine.encrypt_file(str(matched_screen), temp_screen_enc, password)
                screens_blob = obfuscated_blob_path.replace(".enc", ".screens.jpg.enc")
                api.upload_file(
                    path_or_fileobj=temp_screen_enc,
                    path_in_repo=screens_blob,
                    repo_id=repo_id,
                    repo_type="dataset",
                    commit_message=commit_helper.get_human_commit_message("upload")
                )
                screens_record = {
                    "blob_path": screens_blob,
                    "repo_id": repo_id,
                    "account": hf_username,
                    "orig_size": os.path.getsize(str(matched_screen)),
                    "enc_size": enc_sc_size,
                    "format": "jpg",
                    "safe_id": safe_id,
                    "verifier": crypto_engine.create_verifier(password)
                }
                safe_print(f"[{time.strftime('%H:%M:%S')}] [Companion Screens Live] ({matched_screen.name}) -> {screens_blob}")
                if os.path.exists(temp_screen_enc):
                    os.remove(temp_screen_enc)
                if delete_local and os.path.exists(str(matched_screen)):
                    os.remove(str(matched_screen))
                    safe_print(f"[{time.strftime('%H:%M:%S')}] [Freed Disk Space] Cleaned screens image: {matched_screen.name}")
                    remove_empty_folders(base_dir)
        except Exception as sc_err:
            safe_print(f"[!] Companion screens check warning: {sc_err}")

        # Pre-generate 100-screen HD storyboard sprite sheet for live hover and seekbar scrubbing
        storyboard_record = None
        try:
            if storyboard_helper.generate_storyboard(full_key, duration=0, local_path=str_path):
                safe_id = storyboard_helper.get_safe_id(full_key)
                sb_jpg = os.path.join(storyboard_helper.STORYBOARD_DIR, f"{safe_id}.jpg")
                if os.path.exists(sb_jpg):
                    temp_sb_enc = f"{sb_jpg}.enc"
                    sb_enc_size = crypto_engine.encrypt_file(sb_jpg, temp_sb_enc, password)
                    sb_blob = obfuscated_blob_path.replace(".enc", ".storyboard.jpg.enc")
                    api.upload_file(
                        path_or_fileobj=temp_sb_enc,
                        path_in_repo=sb_blob,
                        repo_id=repo_id,
                        repo_type="dataset",
                        commit_message=commit_helper.get_human_commit_message("upload")
                    )
                    storyboard_record = {
                        "blob_path": sb_blob,
                        "repo_id": repo_id,
                        "account": hf_username,
                        "orig_size": os.path.getsize(sb_jpg),
                        "enc_size": sb_enc_size,
                        "format": "jpg",
                        "safe_id": safe_id,
                        "total_frames": 100,
                        "verifier": crypto_engine.create_verifier(password)
                    }
                    safe_print(f"[{time.strftime('%H:%M:%S')}] [100-Screen Storyboard Live] -> {sb_blob} ({os.path.getsize(sb_jpg)/(1024*1024):.2f} MB)")
                    if os.path.exists(temp_sb_enc):
                        os.remove(temp_sb_enc)
        except Exception as sb_err:
            safe_print(f"[!] Storyboard upload warning: {sb_err}")

        # 5. Extract Companion Audio (Instant Stream-Copy / AAC fallback)
        audio_record = None
        try:
            temp_audio = f"{str_path}.companion.m4a"
            if companion_helper.extract_companion_audio(str_path, temp_audio):
                temp_audio_enc = f"{temp_audio}.enc"
                a_orig = os.path.getsize(temp_audio)
                a_enc = crypto_engine.encrypt_file(temp_audio, temp_audio_enc, password)
                a_blob = obfuscated_blob_path.replace(".enc", ".audio.m4a.enc")
                api.upload_file(
                    path_or_fileobj=temp_audio_enc,
                    path_in_repo=a_blob,
                    repo_id=repo_id,
                    repo_type="dataset",
                    commit_message=commit_helper.get_human_commit_message("upload")
                )
                audio_record = {
                    "blob_path": a_blob,
                    "repo_id": repo_id,
                    "account": hf_username,
                    "orig_size": a_orig,
                    "enc_size": a_enc,
                    "format": "m4a",
                    "verifier": crypto_engine.create_verifier(password)
                }
                safe_print(f"[{time.strftime('%H:%M:%S')}] [Companion Audio Live] -> {a_blob} ({a_orig / (1024*1024):.2f} MB)")
                if os.path.exists(temp_audio):
                    os.remove(temp_audio)
                if os.path.exists(temp_audio_enc):
                    os.remove(temp_audio_enc)
        except Exception as a_err:
            safe_print(f"[!] Companion audio extraction warning: {a_err}")

        # 6. Detect and Upload Companion Subtitles from Watch Folder
        subtitles_record = []
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
                        temp_sub_enc = f"{str(srt_file)}.enc"
                        sub_enc_size = crypto_engine.encrypt_file(str(srt_file), temp_sub_enc, password)
                        sub_blob = obfuscated_blob_path.replace(".enc", f".sub.{lang_code}.srt.enc")
                        api.upload_file(
                            path_or_fileobj=temp_sub_enc,
                            path_in_repo=sub_blob,
                            repo_id=repo_id,
                            repo_type="dataset",
                            commit_message=commit_helper.get_human_commit_message("upload")
                        )
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
                        companion_helper.sync_jellyfin_subtitle(library_dir, full_key, sub_text, lang_code)
                        safe_print(f"[{time.strftime('%H:%M:%S')}] [Companion Subtitle Live] ({lang_name}) -> {sub_blob}")
                        if os.path.exists(temp_sub_enc):
                            os.remove(temp_sub_enc)
                        if delete_local and os.path.exists(str(srt_file)):
                            os.remove(str(srt_file))
                            safe_print(f"[{time.strftime('%H:%M:%S')}] [Freed Disk Space] Cleaned subtitle from watch folder: {srt_file.name}")
                            remove_empty_folders(base_dir)
        except Exception as s_err:
            safe_print(f"[!] Companion subtitle check warning: {s_err}")



        # 7. Clean up temp encrypted file
        if os.path.exists(temp_enc_file):
            os.remove(temp_enc_file)

        # 8. Probe technical video/audio metadata (resolution, codecs, HDR, channels) before deletion
        safe_print(f"[{time.strftime('%H:%M:%S')}] [Probing Technical Metadata] Analyzing video streams & resolution...")
        media_info = metadata_helper.probe_media_file(str_path, key_path=full_key)
        if media_info and media_info.get("resolution_label"):
            safe_print(f"[{time.strftime('%H:%M:%S')}] [Detected Video Specs] {media_info.get('resolution_label')} ({media_info.get('width')}x{media_info.get('height')}), {media_info.get('video_codec', 'Video')}, {media_info.get('audio_channels', 'Audio')} | Tags: {', '.join(media_info.get('tags', []))}")

        # 9. Delete original local video to free disk space
        if delete_local:
            os.remove(str_path)
            safe_print(f"[{time.strftime('%H:%M:%S')}] [Freed Disk Space] Deleted original: {str_path}")

        # 10. Extract initial metadata & record to manifest
        activity_tracker.tracker.update_stage(full_key, "metadata", "Generating NFO & Cloud Sync", 85, speed_str=f"{upload_speed_mbps:.1f} Mbps", details="Writing Jellyfin .strm / .nfo & syncing cloud metadata")
        meta = metadata_helper.extract_default_metadata(full_key, media_info=media_info)
        meta["file_names"] = [full_key]
        meta["aliases"] = []
        meta["overview"] = ""
        record = {
            "full_key": full_key,
            "account": hf_username,
            "repo_id": repo_id,
            "repo_folder": repo_folder,
            "rel_in_repo": rel_in_repo,
            "blob_path": obfuscated_blob_path,
            "media_hash": media_hash,
            "orig_size": file_size,
            "enc_size": enc_size,
            "uploaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
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
        update_manifest_record(record, media_hash)

        # 11. Generate Jellyfin pointer (.strm) & metadata (.nfo)
        auto_generate_strm(repo_folder, rel_in_repo, library_dir=library_dir)
        metadata_helper.write_nfo_files(library_dir, full_key, meta, media_info=media_info)
        remove_empty_folders(base_dir)

        # 9. Sync encrypted metadata sidecar to Hugging Face
        safe_print(f"[{time.strftime('%H:%M:%S')}] [Syncing Cloud Metadata] Encrypted sidecar -> {repo_id}...")
        metadata_helper.sync_item_metadata_to_hf(api, repo_id, obfuscated_blob_path, record, password)
        cur_manifest = load_manifest()
        repo_files = {k: v for k, v in cur_manifest.get("files", {}).items() if v.get("repo_id") == repo_id}
        metadata_helper.sync_repo_manifest_to_hf(api, repo_id, repo_files, password)

        activity_tracker.tracker.finish_task(full_key, "completed", f"Ready for streaming! ({upload_speed_mbps:.1f} Mbps)", 100)
        activity_tracker.tracker.add_event("success", f"Live on Jellyfin: {rel_to_base}", f"Uploaded to {repo_id} in {up_time:.1f}s ({upload_speed_mbps:.1f} Mbps)", account=hf_username, speed=f"{upload_speed_mbps:.1f} Mbps")

        SKIPPED_UNMATCHED_FILES.clear()
        safe_print(f"[{time.strftime('%H:%M:%S')}] SUCCESS: {full_key} is live and streamable on Jellyfin with NFO & HF Cloud metadata!\n")

    except Exception as e:
        if is_rate_limit_error(e):
            trigger_rate_limit_cooldown(e, hf_username)
            activity_tracker.tracker.finish_task(full_key, "paused", "Paused: HF rate limit reached. Auto-resuming safely.", 0)
        else:
            safe_print(f"[!] Error processing {file_path}: {e}")
            activity_tracker.tracker.finish_task(full_key, "error", f"Error: {e}", 0)
            activity_tracker.tracker.add_event("error", f"Failed: {file_path.name}", str(e), account=hf_username)
        temp_enc = f"{str_path}.tmp.enc"
        if os.path.exists(temp_enc):
            try:
                os.remove(temp_enc)
            except Exception:
                pass
    finally:
        with MANIFEST_LOCK:
            IN_PROGRESS_FILES.discard(str_path)


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
    password = password or acc_mgr.password

    if not password:
        print("[!] Error: ENCRYPTION_PASSWORD not found.")
        sys.exit(1)

    all_accounts = acc_mgr.get_all_accounts()
    extra_users = [a["username"] for a in all_accounts if not a["is_upload"]]

    name_key = crypto_engine.derive_name_key(password)

    # Multi-account sync on startup: check all accounts for existing repos and media hashes
    manifest = load_manifest()
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

    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="Uploader")

    last_env_mtime = os.path.getmtime(acc_mgr.env_path) if os.path.exists(acc_mgr.env_path) else 0

    while True:
        try:
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

            # Check for Hugging Face Rate Limit Cooldown
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
                        safe_print(f"[{time.strftime('%H:%M:%S')}] [Rate Limit Cooldown Active] Paused for {remaining}s. Zero API requests sent. Originals safe on disk.")
                    time.sleep(poll_interval)
                    continue

            # If cooldown just elapsed:
            if CONSECUTIVE_RATE_LIMITS > 0 and time.time() >= RATE_LIMIT_PAUSE_UNTIL:
                safe_print(f"[{time.strftime('%H:%M:%S')}] [Rate Limit Cooldown Finished] Resuming upload pipeline automatically...")
                activity_tracker.tracker.add_event("info", "Uploads Resumed", "Rate limit cooldown finished; resuming processing.")
                CONSECUTIVE_RATE_LIMITS = 0

            candidates = []
            for root, _, files in os.walk(base_dir):
                for file in files:
                    file_path = Path(root) / file
                    if file_path.suffix.lower() in SUPPORTED_EXTENSIONS:
                        if not file.endswith(".tmp.enc") and not file.endswith(".enc"):
                            if str(file_path) not in SKIPPED_UNMATCHED_FILES:
                                candidates.append(file_path)

            if not candidates:
                activity_tracker.tracker.set_idle()
            else:
                unprocessed_videos = [
                    f for f in candidates
                    if f.suffix.lower() in VIDEO_EXTENSIONS and str(f) not in IN_PROGRESS_FILES
                ]
                if unprocessed_videos:
                    activity_tracker.tracker.start_batch(len(unprocessed_videos))

            # Prioritize video files so worker threads begin primary video sync before sidecars
            candidates.sort(key=lambda p: (0 if p.suffix.lower() in VIDEO_EXTENSIONS else 1))

            for file_path in candidates:
                str_path = str(file_path)

                with MANIFEST_LOCK:
                    if str_path in IN_PROGRESS_FILES:
                        continue
                    IN_PROGRESS_FILES.add(str_path)

                executor.submit(
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

        except KeyboardInterrupt:
            print("\nShutting down parallel watcher...")
            executor.shutdown(wait=False)
            break
        except Exception as e:
            safe_print(f"[!] Watcher scan loop error: {e}")

        time.sleep(poll_interval)


def main():
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
