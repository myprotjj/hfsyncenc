import os
import sys
import io
import time
import json
import math
import shutil
import threading
from pathlib import Path
from huggingface_hub import HfApi, CommitOperationAdd

from core import crypto_engine
from core import commit_helper
from core import storyboard_helper
from core import metadata_helper
from core import account_manager
from core import activity_tracker

STAGING_DIR = "staging"
STAGING_MANIFEST_PATH = os.path.join(STAGING_DIR, "staging_manifest.json")
STAGING_LOCK = threading.RLock()

os.makedirs(STAGING_DIR, exist_ok=True)


def load_staging_manifest() -> dict:
    """Loads the staging manifest safely with retries and fallback to backup."""
    with STAGING_LOCK:
        for attempt in range(20):
            if os.path.exists(STAGING_MANIFEST_PATH):
                try:
                    with open(STAGING_MANIFEST_PATH, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        if isinstance(data, dict) and "repositories" in data:
                            return data
                except Exception:
                    time.sleep(0.05 * (attempt + 1))
            else:
                break

        # Fallback to backup if primary failed or was locked
        bak_path = f"{STAGING_MANIFEST_PATH}.bak"
        if os.path.exists(bak_path):
            try:
                with open(bak_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict) and "repositories" in data:
                        return data
            except Exception:
                pass

        if not os.path.exists(STAGING_MANIFEST_PATH):
            return {"repositories": {}}

        return {"repositories": {}}


def save_staging_manifest(data: dict, allow_empty: bool = False):
    """Saves the staging manifest atomically with automated backup protection."""
    with STAGING_LOCK:
        if not allow_empty and ("repositories" not in data or data.get("repositories") is None) and os.path.exists(STAGING_MANIFEST_PATH):
            try:
                old = load_staging_manifest()
                if old.get("repositories"):
                    data["repositories"] = old["repositories"]
            except Exception:
                pass

        temp_path = f"{STAGING_MANIFEST_PATH}.tmp"
        bak_path = f"{STAGING_MANIFEST_PATH}.bak"

        try:
            if os.path.exists(STAGING_MANIFEST_PATH) and os.path.getsize(STAGING_MANIFEST_PATH) > 100:
                shutil.copyfile(STAGING_MANIFEST_PATH, bak_path)
        except Exception:
            pass

        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        for _ in range(15):
            try:
                if os.path.exists(STAGING_MANIFEST_PATH):
                    os.replace(temp_path, STAGING_MANIFEST_PATH)
                else:
                    os.rename(temp_path, STAGING_MANIFEST_PATH)
                return
            except Exception:
                time.sleep(0.05)
        try:
            shutil.copyfile(temp_path, STAGING_MANIFEST_PATH)
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass


def get_repo_staging_dir(repo_folder: str) -> str:
    """Returns directory path for staging a specific repository."""
    d = os.path.join(STAGING_DIR, repo_folder)
    os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.join(d, "sidecars"), exist_ok=True)
    return d


def add_staged_file(
    repo_folder: str,
    repo_id: str,
    account: str,
    full_key: str,
    rel_in_repo: str,
    orig_filename: str,
    blob_path: str,
    staged_enc_path: str,
    media_hash: str,
    safe_id: str,
    orig_size: int,
    enc_size: int,
    record: dict,
    sidecars: list
):
    """
    Records a pre-encrypted video and its sidecars into the staging manifest.
    Appends to any existing batch for this repo folder. Thread-safe for parallel workers.
    """
    with STAGING_LOCK:
        manifest = load_staging_manifest()
        repos = manifest.setdefault("repositories", {})

        if repo_folder not in repos:
            repos[repo_folder] = {
                "repo_folder": repo_folder,
                "repo_id": repo_id,
                "account": account,
                "status": "staging",
                "file_count": 0,
                "total_orig_size": 0,
                "total_enc_size": 0,
                "created_at": time.time(),
                "updated_at": time.time(),
                "files": [],
                "push_progress": {
                    "percent": 0,
                    "uploaded_assets": 0,
                    "total_assets": 0,
                    "speed_mbps": 0.0,
                    "message": ""
                }
            }

        repo_data = repos[repo_folder]
        repo_data["status"] = "staging"
        repo_data["updated_at"] = time.time()
        if not repo_data.get("repo_id") and repo_id:
            repo_data["repo_id"] = repo_id
        if not repo_data.get("account") and account:
            repo_data["account"] = account

        # Deduplicate in files list if already present
        existing_idx = next((i for i, f in enumerate(repo_data["files"]) if f.get("full_key") == full_key), None)
        file_entry = {
            "full_key": full_key,
            "rel_in_repo": rel_in_repo,
            "orig_filename": orig_filename,
            "blob_path": blob_path,
            "staged_enc_path": staged_enc_path,
            "media_hash": media_hash,
            "safe_id": safe_id,
            "orig_size": orig_size,
            "enc_size": enc_size,
            "sidecars": sidecars,
            "record": record
        }

        if existing_idx is not None:
            repo_data["files"][existing_idx] = file_entry
        else:
            repo_data["files"].append(file_entry)

        # Recalculate totals
        repo_data["file_count"] = len(repo_data["files"])
        repo_data["total_orig_size"] = sum(f.get("orig_size", 0) for f in repo_data["files"])
        repo_data["total_enc_size"] = sum(f.get("enc_size", 0) for f in repo_data["files"])

        save_staging_manifest(manifest)


def record_folder_initial_scan(repo_folder: str, file_paths: list, base_dir=None, is_video_checker=None):
    """
    Scans and records the root folder baseline before/during staging.
    Captures: total files, total video files, and total raw size.
    """
    with STAGING_LOCK:
        manifest = load_staging_manifest()
        repo_data = manifest.setdefault("repositories", {}).setdefault(repo_folder, {
            "repo_folder": repo_folder,
            "status": "staging",
            "file_count": 0,
            "total_orig_size": 0,
            "total_enc_size": 0,
            "files": []
        })

        if "account" not in repo_data or not repo_data.get("account") or not repo_data.get("repo_id"):
            try:
                acc_mgr = account_manager.AccountManager()
                bal = acc_mgr.get_balanced_account_for_new_repo(repo_folder=repo_folder)
                if bal:
                    repo_data["account"] = bal["account"]
                    repo_data["repo_id"] = bal["repo_id"]
            except Exception:
                pass

        initial_scan = repo_data.setdefault("initial_scan", {
            "first_scanned_at": time.time(),
            "last_updated_at": time.time(),
            "total_files_discovered": 0,
            "video_files_discovered": 0,
            "total_orig_bytes": 0,
            "files": {}
        })

        base_path = Path(base_dir) if base_dir else None
        changed = False

        for p in file_paths:
            try:
                p_path = Path(p)
                if not p_path.is_file():
                    continue
                rel = str(p_path.relative_to(base_path)).replace("\\", "/") if base_path else p_path.name
                if rel not in initial_scan["files"]:
                    sz = p_path.stat().st_size
                    initial_scan["files"][rel] = sz
                    initial_scan["total_orig_bytes"] += sz
                    initial_scan["total_files_discovered"] += 1
                    is_vid = is_video_checker(p_path) if is_video_checker else False
                    if is_vid:
                        initial_scan["video_files_discovered"] += 1
                    initial_scan["last_updated_at"] = time.time()
                    changed = True
            except Exception:
                pass

        if changed:
            save_staging_manifest(manifest)


def record_file_duplicate_archived(repo_folder: str, rel_path: str, file_size: int = 0):
    """Adjusts initial scan baseline when a video is identified as an existing duplicate and archived."""
    with STAGING_LOCK:
        manifest = load_staging_manifest()
        repo_data = manifest.get("repositories", {}).get(repo_folder)
        if not repo_data or "initial_scan" not in repo_data:
            return
        initial_scan = repo_data["initial_scan"]
        rel_clean = str(rel_path).replace("\\", "/")
        files_dict = initial_scan.get("files", {})
        matched_k = None
        for k in list(files_dict.keys()):
            if k == rel_clean or k.endswith(rel_clean) or rel_clean.endswith(k) or os.path.basename(k) == os.path.basename(rel_clean):
                matched_k = k
                break
        if matched_k:
            sz = files_dict.pop(matched_k)
            initial_scan["total_orig_bytes"] = max(0, initial_scan.get("total_orig_bytes", 0) - sz)
            initial_scan["video_files_discovered"] = max(0, initial_scan.get("video_files_discovered", 0) - 1)
            initial_scan["total_files_discovered"] = max(0, initial_scan.get("total_files_discovered", 0) - 1)
            initial_scan.setdefault("duplicates_archived", {})[matched_k] = sz
            save_staging_manifest(manifest)


def find_staged_media_hash(media_hash: str) -> dict:
    """Finds an existing staged video entry with the same media_hash across all repositories."""
    if not media_hash:
        return None
    try:
        manifest = load_staging_manifest()
        for rf, rdata in manifest.get("repositories", {}).items():
            for f in rdata.get("files", []):
                if f.get("media_hash") == media_hash:
                    staged_enc = f.get("staged_enc_path")
                    if staged_enc and os.path.exists(staged_enc):
                        res = dict(f)
                        res["source_repo_folder"] = rf
                        return res
    except Exception:
        pass
    return None


def audit_and_mark_repo_ready(repo_folder: str, password: str = "") -> tuple:
    """
    End-to-End Post-Staging Reconciliation Audit Gate.
    Verifies that staged files match initial scan 100% before marking ready to push.
    If any discrepancy or flaw is detected, push is BLOCKED and flagged as 'staging_audit_failed'.
    """
    with STAGING_LOCK:
        manifest = load_staging_manifest()
        repo_data = manifest.get("repositories", {}).get(repo_folder)
        if not repo_data:
            return False, f"Repository folder '{repo_folder}' not found in manifest", {}

        initial_scan = repo_data.get("initial_scan") or {}
        expected_files_dict = initial_scan.get("files", {})
        expected_video_count = initial_scan.get("video_files_discovered", 0)
        expected_bytes = initial_scan.get("total_orig_bytes", 0)

        staged_files = repo_data.get("files", [])
        discrepancies = []
        verified_files = []
        verified_orig_bytes = 0
        verified_enc_bytes = 0

        for f_entry in staged_files:
            orig_name = f_entry.get("orig_filename", "")
            enc_p = f_entry.get("staged_enc_path", "")
            orig_sz = f_entry.get("orig_size", 0)
            enc_sz = f_entry.get("enc_size", 0)

            # 1. Physical existence on disk
            if not enc_p or not os.path.exists(enc_p):
                discrepancies.append(f"Missing encrypted video file on disk: '{orig_name}' ({enc_p})")
                continue

            # 2. Exact size check
            actual_enc_sz = os.path.getsize(enc_p)
            chunk_size = crypto_engine.DEFAULT_CHUNK_SIZE
            total_chunks = (orig_sz + chunk_size - 1) // chunk_size if orig_sz > 0 else 0
            expected_enc_sz = crypto_engine.HEADER_SIZE + orig_sz + (total_chunks * crypto_engine.TAG_SIZE)

            if actual_enc_sz != expected_enc_sz:
                discrepancies.append(f"Encrypted size mismatch for '{orig_name}': actual={actual_enc_sz} bytes, expected={expected_enc_sz} bytes")
                continue

            # 3. Cryptographic decryption health check
            if password:
                if not crypto_engine.is_valid_encrypted_file(enc_p, orig_sz, password, chunk_size):
                    discrepancies.append(f"Decryption check failed for '{orig_name}': chunk 0 corrupted or tag invalid")
                    continue

            # 4. Sidecars check
            sc_ok = True
            for sc in f_entry.get("sidecars", []):
                sc_p = sc.get("staged_path")
                if not sc_p or not os.path.exists(sc_p) or os.path.getsize(sc_p) == 0:
                    discrepancies.append(f"Missing/empty sidecar for '{orig_name}': {sc_p} ({sc.get('type')})")
                    sc_ok = False
                    break

            if not sc_ok:
                continue

            verified_files.append(f_entry)
            verified_orig_bytes += orig_sz
            verified_enc_bytes += enc_sz

        # 5. Check against Initial Scan baseline
        if expected_video_count > 0 and len(verified_files) < expected_video_count:
            missing_count = expected_video_count - len(verified_files)
            discrepancies.append(f"File count mismatch: Initial scan discovered {expected_video_count} video file(s), but only {len(verified_files)} verified on disk ({missing_count} missing!)")

        if expected_bytes > 0 and verified_orig_bytes < expected_bytes and len(verified_files) < expected_video_count:
            missing_bytes = expected_bytes - verified_orig_bytes
            discrepancies.append(f"Total size mismatch: Initial scan discovered {_format_size(expected_bytes)}, but verified staged size is {_format_size(verified_orig_bytes)} ({_format_size(missing_bytes)} missing!)")

        # 6. Check individual expected files
        for rel_k, exp_sz in expected_files_dict.items():
            base_k = os.path.basename(rel_k)
            matched = any(
                f.get("orig_filename") == base_k or f.get("full_key", "").endswith(base_k)
                for f in verified_files
            )
            if not matched:
                discrepancies.append(f"File from initial scan was NOT staged: '{rel_k}' ({_format_size(exp_sz)})")

        passed = (len(discrepancies) == 0 and len(verified_files) > 0)
        audit_report = {
            "audited_at": time.time(),
            "passed": passed,
            "initial_scanned_count": expected_video_count or len(verified_files),
            "initial_scanned_bytes": expected_bytes or verified_orig_bytes,
            "initial_scanned_formatted": _format_size(expected_bytes or verified_orig_bytes),
            "verified_staged_count": len(verified_files),
            "verified_staged_bytes": verified_orig_bytes,
            "verified_staged_formatted": _format_size(verified_orig_bytes),
            "discrepancies": discrepancies
        }

        repo_data["audit_report"] = audit_report
        repo_data["files"] = verified_files
        repo_data["file_count"] = len(verified_files)
        repo_data["total_orig_size"] = verified_orig_bytes
        repo_data["total_enc_size"] = verified_enc_bytes

        if passed:
            repo_data["status"] = "ready_to_push"
            repo_data["ready_at"] = time.time()
            save_staging_manifest(manifest)
            activity_tracker.tracker.add_event(
                "info",
                f"Audit 100% Passed: {repo_folder}",
                f"{len(verified_files)} file(s) ({_format_size(verified_orig_bytes)}) 100% matched initial scan! Ready for 1-commit push.",
                account=repo_data.get("account", "")
            )
            return True, "100% audit match passed", audit_report
        else:
            repo_data["status"] = "staging_audit_failed"
            save_staging_manifest(manifest)
            activity_tracker.tracker.add_event(
                "error",
                f"STAGING AUDIT FAILED: {repo_folder}",
                f"Flaw detected! {len(discrepancies)} discrepancy(s). Push strictly BLOCKED to prevent data loss.",
                account=repo_data.get("account", "")
            )
            return False, f"Staging audit failed with {len(discrepancies)} discrepancy(s)", audit_report


def mark_repo_ready(repo_folder: str, password: str = "") -> bool:
    """Marks a repository folder as ready to push ONLY if it passes the full reconciliation audit."""
    passed, _, _ = audit_and_mark_repo_ready(repo_folder, password=password)
    return passed


def _has_actual_files(path: str) -> bool:
    """Checks whether a directory path contains any actual files recursively."""
    if not path or not os.path.exists(path):
        return False
    for _, _, files in os.walk(path):
        if files:
            return True
    return False


def get_staging_status(watch_dir: str = "videos_to_sync") -> dict:
    """Returns the current status of all staged repositories for the web UI."""
    manifest = load_staging_manifest()
    ready_list = []
    staging_list = []
    pushing_list = []
    failed_audit_list = []
    error_list = []
    pushed_list = []
    ignored_list = []
    paused_list = []
    queued_list = []
    paused_staging_list = []

    prune_keys = []

    for folder, data in list(manifest.get("repositories", {}).items()):
        status = data.get("status")
        files = data.get("files", [])
        staged_dir = get_repo_staging_dir(folder)
        watch_folder_dir = os.path.join(watch_dir, folder)

        # Reconcile ghost repositories:
        # If marked as staging, but has 0 files staged AND source directory does not exist on disk
        if status == "staging" and len(files) == 0:
            has_source = _has_actual_files(watch_folder_dir)
            has_staged = _has_actual_files(staged_dir)
            if not has_source and not has_staged:
                prune_keys.append(folder)
                continue

        # Auto-reconcile completed staging:
        # If marked as staging, but all files have already been staged and 0 pending source files remain
        if status == "staging" and len(files) > 0 and not data.get("staging_paused"):
            has_source = _has_actual_files(watch_folder_dir)
            if not has_source:
                audit_rep = data.get("audit_report") or {}
                if audit_rep.get("passed"):
                    status = "ready_to_push"
                    data["status"] = "ready_to_push"
                    data["ready_at"] = data.get("ready_at") or time.time()
                    save_staging_manifest(manifest)

        # For staging status, the count of files staged so far is the actual length of files list
        staged_file_count = len(files) if status == "staging" else data.get("file_count", len(files))

        # Auto-reconcile orphaned pushing or queued status if not currently active in memory
        with _PUSH_LOCK:
            if status == "pushing" and folder not in _ACTIVE_PUSHES and folder != _CURRENT_PUSHING_REPO:
                status = "paused"
                data["status"] = "paused"
                if "push_progress" in data:
                    data["push_progress"]["message"] = "Upload paused (session restarted). Click Resume Push to continue."
                save_staging_manifest(manifest)
            elif status == "queued" and folder not in _PUSH_QUEUE:
                status = "ready_to_push"
                data["status"] = "ready_to_push"
                save_staging_manifest(manifest)

        audit_rep = data.get("audit_report") or {}
        initial_sc = data.get("initial_scan") or {}
        init_count = audit_rep.get("initial_scanned_count") or initial_sc.get("video_files_discovered") or initial_sc.get("total_files_discovered") or staged_file_count
        init_formatted = audit_rep.get("initial_scanned_formatted") or (_format_size(initial_sc.get("total_orig_bytes", 0)) if initial_sc.get("total_orig_bytes") else _format_size(data.get("total_orig_size", 0)))

        total_vids = (
            data.get("total_videos")
            or audit_rep.get("initial_scanned_count")
            or initial_sc.get("video_files_discovered")
            or initial_sc.get("total_files_discovered")
            or len(files)
            or staged_file_count
        )
        total_asts = (
            data.get("total_assets")
            or (data.get("push_progress", {}).get("total_assets") if isinstance(data.get("push_progress", {}).get("total_assets"), int) and data.get("push_progress", {}).get("total_assets") < 100000 else None)
            or sum(1 + len(f.get("sidecars", [])) for f in files)
            or (total_vids * 4 if total_vids else 0)
        )
        total_enc_b = data.get("total_enc_bytes") or data.get("total_enc_size") or (data.get("push_progress", {}).get("total_bytes")) or 0
        total_orig_b = data.get("total_orig_bytes") or data.get("total_orig_size") or initial_sc.get("total_orig_bytes") or 0
        display_bytes = total_enc_b if total_enc_b > 0 else total_orig_b
        display_formatted = _format_size(display_bytes) if display_bytes > 0 else init_formatted
        total_batches = (
            data.get("total_batches")
            or (data.get("push_progress", {}).get("total_batches"))
            or max(1, math.ceil(max(total_enc_b, total_orig_b) / (10 * 1024 * 1024 * 1024)))
        )
        cur_file_count = staged_file_count if status == "staging" else total_vids
        item = {
            "repo_folder": folder,
            "repo_id": data.get("repo_id"),
            "account": data.get("account"),
            "status": data.get("status"),
            "file_count": cur_file_count,
            "video_count": cur_file_count,
            "total_videos": cur_file_count,
            "total_assets": total_asts,
            "total_orig_size": total_orig_b,
            "total_size_bytes": display_bytes,
            "total_enc_size": total_enc_b,
            "total_size_formatted": display_formatted,
            "total_batches": total_batches,
            "updated_at": data.get("updated_at", 0),
            "push_progress": data.get("push_progress", {}),
            "commits": data.get("push_progress", {}).get("commits", []) or data.get("commits", []),
            "audit_report": audit_rep,
            "initial_scanned_count": init_count,
            "initial_scanned_formatted": init_formatted,
            "discrepancies": audit_rep.get("discrepancies", []),
            "staging_paused": bool(data.get("staging_paused") or status == "paused_staging"),
            "preview_thumbs": [
                f"/api/screens/thumb?id={f.get('safe_id')}"
                for f in files[:4]
                if f.get("safe_id")
            ],
            "file_names_preview": [
                f.get("orig_filename") or Path(f.get("full_key", "")).name
                for f in files[:5]
            ]
        }
        if status == "ready_to_push":
            ready_list.append(item)
        elif status == "pushing":
            pushing_list.append(item)
        elif status == "staging":
            if data.get("staging_paused"):
                item["status"] = "paused_staging"
                paused_staging_list.append(item)
            else:
                staging_list.append(item)
        elif status == "paused_staging":
            item["status"] = "paused_staging"
            paused_staging_list.append(item)
        elif status == "staging_audit_failed":
            failed_audit_list.append(item)
        elif status == "error":
            error_list.append(item)
        elif status == "pushed":
            pushed_list.append(item)
        elif status == "ignored":
            ignored_list.append(item)
        elif status == "paused":
            paused_list.append(item)
        elif status == "queued":
            queued_list.append(item)

    if prune_keys:
        for k in prune_keys:
            if k in manifest.get("repositories", {}):
                del manifest["repositories"][k]
        save_staging_manifest(manifest, allow_empty=True)

    return {
        "ready_repos": ready_list,
        "queued_repos": queued_list,
        "staging_repos": staging_list,
        "paused_staging_repos": paused_staging_list,
        "pushing_repos": pushing_list,
        "failed_audit_repos": failed_audit_list,
        "error_repos": error_list,
        "pushed_repos": pushed_list,
        "ignored_repos": ignored_list,
        "paused_repos": paused_list,
        "total_ready_count": len(ready_list),
        "total_ready_bytes": sum(r["total_orig_size"] for r in ready_list)
    }


def _format_size(size_bytes: int) -> str:
    mb = size_bytes / (1024 * 1024)
    if mb < 1024:
        return f"{mb:.1f} MB"
    return f"{mb / 1024:.2f} GB"


# Background push executor and sequential queue controls
_PUSH_EXECUTOR = None
_ACTIVE_PUSHES = set()
_CURRENT_PUSHING_REPO = None
_PUSH_QUEUE = []
_CANCELLED_QUEUED_REPOS = set()
_PUSH_LOCK = threading.Lock()
_PUSH_PAUSE_FLAGS = {}
_PUSH_RETRY_FLAGS = {}


def pause_repo_push(repo_folder: str) -> bool:
    """Signals an in-progress push to pause after the current batch."""
    with _PUSH_LOCK:
        if repo_folder in _PUSH_PAUSE_FLAGS:
            _PUSH_PAUSE_FLAGS[repo_folder].set()
            return True
        manifest = load_staging_manifest()
        r = manifest.get("repositories", {}).get(repo_folder)
        if r and r.get("status") == "pushing":
            r["status"] = "paused"
            save_staging_manifest(manifest)
            return True
        return False


def trigger_retry_now(repo_folder: str) -> bool:
    """Signals an immediate retry for a batch currently in backoff countdown."""
    with _PUSH_LOCK:
        if repo_folder in _PUSH_RETRY_FLAGS:
            _PUSH_RETRY_FLAGS[repo_folder].set()
            return True
        return False


def resume_repo_push(repo_folder: str, library_dir: str = "jellyfin_library") -> tuple:
    """Resumes pushing a paused or interrupted repository."""
    manifest = load_staging_manifest()
    r = manifest.get("repositories", {}).get(repo_folder)
    if not r:
        return False, "Repository not found in staging manifest"
    status = r.get("status")
    if status not in ("paused", "error", "ready_to_push"):
        return False, f"Cannot resume repository in status '{status}'"
    r["status"] = "ready_to_push"
    save_staging_manifest(manifest)
    return push_repo_async(repo_folder, library_dir)


def cancel_queued_push(repo_folder: str) -> tuple:
    """Cancels a queued repository push before it begins executing."""
    with _PUSH_LOCK:
        if repo_folder == _CURRENT_PUSHING_REPO or repo_folder in _ACTIVE_PUSHES:
            return False, "Cannot cancel queue: Repository has already started pushing"

        if repo_folder in _PUSH_QUEUE:
            _PUSH_QUEUE.remove(repo_folder)

        _CANCELLED_QUEUED_REPOS.add(repo_folder)

        manifest = load_staging_manifest()
        r = manifest.get("repositories", {}).get(repo_folder)
        if r and r.get("status") == "queued":
            r["status"] = "ready_to_push"
            r["push_progress"] = {
                "percent": 0,
                "message": "Queue cancelled. Ready to push."
            }
            save_staging_manifest(manifest)

        # Update positions for remaining items in queue
        for idx, q_rf in enumerate(_PUSH_QUEUE, 1):
            qr = manifest.get("repositories", {}).get(q_rf)
            if qr and "push_progress" in qr:
                qr["push_progress"]["queue_position"] = idx
                qr["push_progress"]["message"] = f"Queued (#{idx} in line): Waiting for active upload to finish..."
        save_staging_manifest(manifest)

        return True, "Queued push cancelled successfully"


def partition_staged_files_into_batches(files_list: list, target_bytes: int = 10 * 1024 * 1024 * 1024) -> list:
    """
    Groups complete video packages into chunks targeting ~10 GB.
    Never splits a video from its sidecars (audio, storyboard, meta).
    If a single video is >= target_bytes, it forms its own single-item batch.
    """
    batches = []
    current_batch = []
    current_bytes = 0

    for f in files_list:
        sz = f.get("orig_size", 0) or f.get("enc_size", 0)
        if current_bytes + sz > target_bytes and current_batch:
            batches.append(current_batch)
            current_batch = [f]
            current_bytes = sz
        else:
            current_batch.append(f)
            current_bytes += sz

    if current_batch:
        batches.append(current_batch)

    return batches


def _ensure_local_storyboards_cached(f_entry: dict, password: str):
    """Pre-extracts and verifies local storyboard sheets, thumbnails and JSON before deleting staged files."""
    rec = f_entry.get("record", {})
    full_k = f_entry.get("full_key")
    m_hash = f_entry.get("media_hash")
    safe_id = rec.get("safe_id") or (m_hash[:24] if m_hash else None) or storyboard_helper.get_safe_id(full_k)
    if not safe_id:
        return

    os.makedirs(storyboard_helper.STORYBOARD_DIR, exist_ok=True)
    os.makedirs(storyboard_helper.THUMBS_DIR, exist_ok=True)

    sb_jpg = os.path.join(storyboard_helper.STORYBOARD_DIR, f"{safe_id}.jpg")
    sb_json = os.path.join(storyboard_helper.STORYBOARD_DIR, f"{safe_id}.json")
    sb_thumb = os.path.join(storyboard_helper.THUMBS_DIR, f"{safe_id}.jpg")

    if not os.path.exists(sb_jpg):
        for sc in f_entry.get("sidecars", []):
            if sc.get("type") == "storyboard":
                sc_p = sc.get("staged_path")
                if sc_p and os.path.exists(sc_p):
                    try:
                        crypto_engine.decrypt_file(sc_p, sb_jpg, password)
                        with open(sb_jpg, "rb") as fp:
                            raw_b = fp.read()
                        storyboard_helper.write_encrypted_bytes(sb_jpg, raw_b, password)
                    except Exception as e_sb:
                        print(f"[!] Warning restoring storyboard for {safe_id}: {e_sb}")

    if os.path.exists(sb_jpg) and not os.path.exists(sb_thumb):
        try:
            storyboard_helper.slice_poster_thumbnail(safe_id, overwrite=False, password=password)
        except Exception:
            pass

    if os.path.exists(sb_jpg) and not os.path.exists(sb_json):
        try:
            dur = (rec.get("media_info") or {}).get("duration_sec", 0)
            sb_meta = {
                "cols": 10,
                "rows": 10,
                "thumb_width": 384,
                "thumb_height": 216,
                "duration": dur,
                "total_frames": 100,
                "safe_id": safe_id,
                "key": full_k
            }
            storyboard_helper.write_encrypted_bytes(sb_json, json.dumps(sb_meta, indent=2).encode("utf-8"), password)
        except Exception:
            pass


def push_repo_async(repo_folder: str, library_dir: str = "jellyfin_library"):
    """
    Submits a repository push to the strictly sequential 1-by-1 push queue.
    Only one repository push will ever run at any given moment.
    Additional repositories wait in queue and begin automatically as each completes.
    """
    global _PUSH_EXECUTOR, _CURRENT_PUSHING_REPO
    with _PUSH_LOCK:
        if _PUSH_EXECUTOR is None:
            from concurrent.futures import ThreadPoolExecutor
            # Strictly 1 worker: guarantees serial execution one-by-one!
            _PUSH_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hf_batch_pusher_seq")

        if repo_folder == _CURRENT_PUSHING_REPO or repo_folder in _ACTIVE_PUSHES:
            return False, "Push already in progress for this repository"

        if repo_folder in _PUSH_QUEUE:
            return False, "Repository is already queued for push"

        manifest = load_staging_manifest()
        repo_data = manifest.get("repositories", {}).get(repo_folder)
        if not repo_data or not repo_data.get("files"):
            return False, "Repository not found or has no staged files"

        if repo_data.get("status") == "staging_audit_failed":
            return False, "Cannot push: Staging audit failed due to discrepancy. Push strictly blocked to prevent data loss."

        _CANCELLED_QUEUED_REPOS.discard(repo_folder)
        _PUSH_PAUSE_FLAGS[repo_folder] = threading.Event()
        _PUSH_RETRY_FLAGS[repo_folder] = threading.Event()

        is_currently_pushing = (_CURRENT_PUSHING_REPO is not None or len(_ACTIVE_PUSHES) > 0)

        if is_currently_pushing:
            _PUSH_QUEUE.append(repo_folder)
            q_pos = len(_PUSH_QUEUE)
            repo_data["status"] = "queued"
            repo_data["push_progress"] = {
                "percent": 0,
                "current_batch": 1,
                "total_batches": 1,
                "queue_position": q_pos,
                "message": f"Queued (#{q_pos} in line): Waiting for active upload '{_CURRENT_PUSHING_REPO or 'current'}' to finish...",
                "log_lines": [f"[{time.strftime('%H:%M:%S')}] Queued (#{q_pos} in line) - waiting for current push to finish."]
            }
            save_staging_manifest(manifest)
            _PUSH_EXECUTOR.submit(_execute_repo_push, repo_folder, library_dir)
            return True, f"Repository queued (#{q_pos} in line) for sequential upload"
        else:
            _CURRENT_PUSHING_REPO = repo_folder
            _ACTIVE_PUSHES.add(repo_folder)
            _PUSH_EXECUTOR.submit(_execute_repo_push, repo_folder, library_dir)
            return True, "Push started in background"


class PushProgressTracker:
    def __init__(self, repo_folder: str, repo_id: str, account: str, total_ops: int, total_bytes: int, total_batches: int = 1):
        self.repo_folder = repo_folder
        self.repo_id = repo_id
        self.account = account
        self.total_ops = max(1, total_ops)
        self.total_bytes = max(1, total_bytes)
        self.completed_ops = 0
        self.completed_bytes = 0
        self.current_batch = 1
        self.total_batches = max(1, total_batches)
        self.overall_completed_bytes = 0
        self.overall_total_bytes = max(1, total_bytes)
        self.total_videos = 0
        self.overall_total_assets = max(1, total_ops)
        self.overall_completed_assets = 0
        self.completed_commits = []
        self.is_retrying = False
        self.retry_attempt = 0
        self.retry_countdown = 0
        self.t0 = time.time()
        self.last_save_time = 0
        self._lock = threading.Lock()
        self.log_lines = []
        self._completed_items_set = set()

    def add_log(self, msg: str):
        with self._lock:
            ts = time.strftime('%H:%M:%S')
            line = f"[{ts}] {msg}"
            self.log_lines.append(line)
            if len(self.log_lines) > 250:
                self.log_lines.pop(0)
            print(line)

    def set_retry_state(self, is_retrying: bool, attempt: int = 0, countdown: int = 0, error_msg: str = ""):
        with self._lock:
            self.is_retrying = is_retrying
            self.retry_attempt = attempt
            self.retry_countdown = countdown
            now = time.time()
            self.last_save_time = now
            try:
                manifest = load_staging_manifest()
                rdata = manifest.get("repositories", {}).get(self.repo_folder)
                if rdata and "push_progress" in rdata:
                    prog = rdata["push_progress"]
                    prog["is_retrying"] = is_retrying
                    prog["retry_attempt"] = attempt
                    prog["retry_countdown"] = countdown
                    if is_retrying:
                        prog["message"] = f"⚠️ Network drop (Retry #{attempt} in {countdown}s)..."
                    save_staging_manifest(manifest)
            except Exception:
                pass

    def update_progress(self, current_file: str = "", bytes_done: int = None, ops_done: int = None, force_save: bool = False):
        with self._lock:
            now = time.time()
            if bytes_done is not None:
                self.completed_bytes = max(self.completed_bytes, bytes_done)
            if ops_done is not None:
                self.completed_ops = max(self.completed_ops, ops_done)

            elapsed = max(0.1, now - self.t0)
            overall_done = min(self.overall_total_bytes, self.overall_completed_bytes + self.completed_bytes)
            speed_bps = overall_done / elapsed
            speed_mbps = speed_bps / (1024 * 1024)

            rem_bytes = max(0, self.overall_total_bytes - overall_done)
            eta_sec = int(rem_bytes / speed_bps) if speed_bps > 1024 else 0
            if eta_sec >= 60:
                eta_str = f"{eta_sec // 60}m {eta_sec % 60}s"
            elif eta_sec > 0:
                eta_str = f"{eta_sec}s"
            else:
                eta_str = "finalizing..."

            byte_ratio = min(1.0, max(0.0, overall_done / self.overall_total_bytes))
            pct = int(byte_ratio * 99)
            pct = min(99, max(1, pct))

            if force_save or (now - self.last_save_time >= 0.5):
                self.last_save_time = now
                try:
                    manifest = load_staging_manifest()
                    rdata = manifest.get("repositories", {}).get(self.repo_folder)
                    if rdata:
                        rdata["push_progress"] = {
                            "percent": pct,
                            "current_batch": self.current_batch,
                            "total_batches": self.total_batches,
                            "batch_info": f"Batch {self.current_batch} of {self.total_batches}",
                            "total_videos": getattr(self, "total_videos", 0),
                            "overall_total_assets": getattr(self, "overall_total_assets", self.total_ops),
                            "overall_completed_assets": getattr(self, "overall_completed_assets", 0) + self.completed_ops,
                            "uploaded_assets": self.completed_ops,
                            "total_assets": self.total_ops,
                            "uploaded_bytes": overall_done,
                            "total_bytes": self.overall_total_bytes,
                            "uploaded_bytes_formatted": _format_size(overall_done),
                            "total_bytes_formatted": _format_size(self.overall_total_bytes),
                            "speed_mbps": round(speed_mbps, 1),
                            "speed_str": f"{speed_mbps:.1f} MB/s",
                            "eta_seconds": eta_sec,
                            "eta_str": eta_str,
                            "current_file": os.path.basename(current_file) if current_file else "",
                            "message": f"[Batch {self.current_batch}/{self.total_batches}] {overall_done // (1024*1024):,} MB / {self.overall_total_bytes // (1024*1024):,} MB @ {speed_mbps:.1f} MB/s [ETA: {eta_str}]",
                            "is_retrying": self.is_retrying,
                            "retry_attempt": self.retry_attempt,
                            "retry_countdown": self.retry_countdown,
                            "commits": list(getattr(self, "completed_commits", [])),
                            "repo_url": f"https://huggingface.co/datasets/{self.repo_id}",
                            "log_lines": list(self.log_lines)
                        }
                        save_staging_manifest(manifest)
                except Exception:
                    pass


def dismiss_staged_repo(repo_folder: str) -> bool:
    """Dismisses a completed or error repository card from staging view."""
    with STAGING_LOCK:
        manifest = load_staging_manifest()
        if repo_folder in manifest.get("repositories", {}):
            del manifest["repositories"][repo_folder]
            save_staging_manifest(manifest, allow_empty=True)
            staged_dir = get_repo_staging_dir(repo_folder)
            if os.path.exists(staged_dir):
                try:
                    shutil.rmtree(staged_dir, ignore_errors=True)
                except Exception:
                    pass
            return True
        return False


def dismiss_all_pushed_repos() -> int:
    """Dismisses all completed/pushed repository cards in one action."""
    with STAGING_LOCK:
        manifest = load_staging_manifest()
        repos = manifest.get("repositories", {})
        to_del = [rf for rf, data in repos.items() if data.get("status") == "pushed"]
        for rf in to_del:
            del repos[rf]
            staged_dir = get_repo_staging_dir(rf)
            if os.path.exists(staged_dir):
                try:
                    shutil.rmtree(staged_dir, ignore_errors=True)
                except Exception:
                    pass
        if to_del:
            save_staging_manifest(manifest, allow_empty=True)
        return len(to_del)


def _execute_repo_push(repo_folder: str, library_dir: str):
    """Executes the single sequential repository push to Hugging Face."""
    global _CURRENT_PUSHING_REPO
    with _PUSH_LOCK:
        if repo_folder in _CANCELLED_QUEUED_REPOS:
            _CANCELLED_QUEUED_REPOS.discard(repo_folder)
            if repo_folder in _PUSH_QUEUE:
                _PUSH_QUEUE.remove(repo_folder)
            print(f"[{time.strftime('%H:%M:%S')}] [Sequential Push] Repository '{repo_folder}' was cancelled from queue; skipping.")
            return

        _CURRENT_PUSHING_REPO = repo_folder
        _ACTIVE_PUSHES.add(repo_folder)
        if repo_folder in _PUSH_QUEUE:
            _PUSH_QUEUE.remove(repo_folder)

    manifest = load_staging_manifest()
    repo_data = manifest.get("repositories", {}).get(repo_folder)
    if not repo_data:
        with _PUSH_LOCK:
            if _CURRENT_PUSHING_REPO == repo_folder:
                _CURRENT_PUSHING_REPO = None
            _ACTIVE_PUSHES.discard(repo_folder)
        return

    repo_id = repo_data.get("repo_id")
    account = repo_data.get("account")
    files = repo_data.get("files", [])
    total_files = len(files)
    total_orig_b = repo_data.get("total_orig_size", 0) or sum(f.get("orig_size", 0) for f in files)
    total_enc_b = sum(f.get("enc_size", 0) + sum(sc.get("size", 0) for sc in f.get("sidecars", [])) for f in files)

    acc_mgr = account_manager.AccountManager()

    # Safety auto-resolution for repo_id and account
    if not repo_id or str(repo_id).strip() in ("None", ""):
        for f in files:
            rec = f.get("record", {})
            if rec.get("repo_id") and str(rec.get("repo_id")) != "None":
                repo_id = rec.get("repo_id")
                account = rec.get("account") or account
                break
        if not repo_id or str(repo_id).strip() in ("None", ""):
            bal = acc_mgr.get_balanced_account_for_new_repo(repo_folder=repo_folder)
            account = bal["account"]
            repo_id = bal["repo_id"]
        repo_data["repo_id"] = repo_id
        repo_data["account"] = account

    target_api = acc_mgr.get_api_for_repo(repo_id)
    token = acc_mgr.get_token_for_repo(repo_id)
    password = acc_mgr.password

    # Pre-partition files into ~10 GB batches
    batches = partition_staged_files_into_batches(files, target_bytes=10 * 1024 * 1024 * 1024)
    total_batches = max(1, len(batches))

    repo_data["total_videos"] = total_files
    repo_data["total_orig_size"] = total_orig_b
    repo_data["total_enc_size"] = total_enc_b
    repo_data["total_batches"] = total_batches

    # Update status to pushing
    repo_data["status"] = "pushing"
    repo_data["push_progress"] = {
        "percent": 1,
        "current_batch": 1,
        "total_batches": total_batches,
        "batch_info": f"Batch 1 of {total_batches}",
        "uploaded_assets": 0,
        "total_assets": 0,
        "speed_mbps": 0.0,
        "message": f"Organizing {total_files} file(s) into {total_batches} batch(es) (~10 GB each)..."
    }
    save_staging_manifest(manifest)

    activity_tracker.tracker.add_event(
        "info",
        f"Pushing Batch: {repo_folder}",
        f"Committing {total_files} file(s) ({_format_size(repo_data.get('total_orig_size', 0))}) in {total_batches} commit(s) (~10 GB each) to {repo_id}...",
        account=account
    )

    try:
        watch_dir = "videos_to_sync"
        local_folder_path = os.path.join(watch_dir, repo_folder)

        # Pre-Commit Verification: Check if local watch folder still has unencrypted files
        if os.path.exists(local_folder_path):
            remaining_files = []
            for root, _, fn_list in os.walk(local_folder_path):
                for fn in fn_list:
                    f_low = fn.lower()
                    if f_low.endswith((".tmp.enc", ".enc", ".tmp", ".companion.m4a", ".nfo", ".strm")):
                        continue
                    remaining_files.append(Path(root) / fn)

            if remaining_files:
                print(f"[{time.strftime('%H:%M:%S')}] [Pre-Commit Check] Found {len(remaining_files)} newly added/remaining file(s) in '{local_folder_path}'. Staging with strict verification...")
                repo_data["push_progress"]["message"] = f"Pre-encrypting {len(remaining_files)} newly added file(s) before commit..."
                save_staging_manifest(manifest)

                import watcher_service
                for rem_fp in remaining_files:
                    try:
                        watcher_service.prepare_video_package(
                            file_path=rem_fp,
                            base_dir=Path(watch_dir),
                            base_archive=Path("archive"),
                            library_dir=library_dir,
                            default_repo_name=repo_folder,
                            api=target_api,
                            hf_username=account,
                            password=password,
                            name_key=crypto_engine.derive_name_key(password),
                            delete_local=True,
                            acc_mgr=acc_mgr
                        )
                    except Exception as ex_stg:
                        print(f"[!] Error pre-encrypting remaining file '{rem_fp.name}': {ex_stg}")

                # Re-run full reconciliation audit!
                audit_ok, audit_err, audit_rep = audit_and_mark_repo_ready(repo_folder, password=password)
                if not audit_ok:
                    print(f"[!] [Pre-Commit Audit Failed] {audit_err}. Halting push to protect data!")
                    manifest = load_staging_manifest()
                    repo_data = manifest.get("repositories", {}).get(repo_folder, {})
                    repo_data["status"] = "staging_audit_failed"
                    repo_data["push_progress"] = {
                        "percent": 0,
                        "message": f"Push blocked: {audit_err}"
                    }
                    save_staging_manifest(manifest)
                    with _PUSH_LOCK:
                        _ACTIVE_PUSHES.discard(repo_folder)
                    return

                # Reload manifest & files list after staging additional files
                manifest = load_staging_manifest()
                repo_data = manifest.get("repositories", {}).get(repo_folder, {})
                files = repo_data.get("files", [])
                total_files = len(files)

        # Clean up empty folder tree in videos_to_sync now that all files are verified and staged
        if os.path.exists(local_folder_path):
            try:
                for root, dirs, files_in_d in os.walk(local_folder_path, topdown=False):
                    if not os.listdir(root):
                        os.rmdir(root)
                if os.path.exists(local_folder_path) and not os.listdir(local_folder_path):
                    os.rmdir(local_folder_path)
                    print(f"[{time.strftime('%H:%M:%S')}] [Cleaned Watch Folder] All files staged; removed empty source folder tree: {local_folder_path}")
            except Exception as e_rm:
                print(f"[!] Note removing empty watch folder tree: {e_rm}")

        # Re-partition batches in case pre-commit check added more files
        batches = partition_staged_files_into_batches(files, target_bytes=10 * 1024 * 1024 * 1024)
        total_batches = max(1, len(batches))

        # Calculate overall total bytes and ops across all batches
        overall_total_b = 0
        overall_total_ops = 0
        for b in batches:
            for f_entry in b:
                enc_p = f_entry.get("staged_enc_path")
                if enc_p and os.path.exists(enc_p):
                    overall_total_ops += 1
                    overall_total_b += os.path.getsize(enc_p)
                for sc in f_entry.get("sidecars", []):
                    sc_p = sc.get("staged_path")
                    if sc_p and os.path.exists(sc_p):
                        overall_total_ops += 1
                        overall_total_b += os.path.getsize(sc_p)

        if overall_total_b == 0:
            overall_total_b = repo_data.get("total_enc_size", 0) or repo_data.get("total_orig_size", 0) or 1

        tracker = PushProgressTracker(
            repo_folder=repo_folder,
            repo_id=repo_id,
            account=account,
            total_ops=overall_total_ops,
            total_bytes=overall_total_b,
            total_batches=total_batches
        )
        prev_commits = repo_data.get("push_progress", {}).get("commits", [])
        if prev_commits:
            tracker.completed_commits = list(prev_commits)
        tracker.total_videos = total_files
        tracker.overall_total_assets = overall_total_ops
        repo_data["total_assets"] = overall_total_ops
        save_staging_manifest(manifest)
        tracker.add_log(f"Assembled {total_files} videos ({_format_size(overall_total_b)}) across {total_batches} commit batch(es) (~10 GB each)")
        tracker.add_log(f"Target repository: {repo_id} (Account: {account})")
        tracker.update_progress(force_save=True)

        try:
            target_api.create_repo(repo_id=repo_id, repo_type="dataset", private=False, exist_ok=True)
            tracker.add_log(f"Repository confirmed on Hugging Face: {repo_id}")
        except Exception as e_cr:
            tracker.add_log(f"Repo check: {e_cr}")

        # Hook into huggingface_hub Xet progress and LFS upload
        import huggingface_hub._commit_api as hf_commit_api
        import huggingface_hub.utils._xet_progress_reporting as hf_xet_reporting

        orig_xet_update = hf_xet_reporting.XetUploadProgressReporter.update_progress
        orig_lfs_upload = hf_commit_api.lfs_upload

        def hooked_xet_update(reporter_self, group_report, item_reports):
            try:
                orig_xet_update(reporter_self, group_report, item_reports)
            except Exception:
                pass
            try:
                done_b = getattr(group_report, "total_bytes_completed", 0)
                newly_completed = []
                for it_name, it_obj in item_reports.items():
                    if getattr(it_obj, "bytes_completed", 0) >= getattr(it_obj, "total_bytes", 1):
                        if it_name not in tracker._completed_items_set:
                            tracker._completed_items_set.add(it_name)
                            newly_completed.append((it_name, getattr(it_obj, "total_bytes", 0)))

                cur_active = [it.item_name for it in item_reports.values() if 0 < it.bytes_completed < it.total_bytes]
                cur_f = cur_active[0] if cur_active else ""

                for name, sz in newly_completed:
                    tracker.add_log(f"✓ Transferred {os.path.basename(name)} ({_format_size(sz)})")

                tracker.update_progress(
                    current_file=cur_f,
                    bytes_done=done_b,
                    ops_done=len(tracker._completed_items_set)
                )
            except Exception:
                pass

        def hooked_lfs_upload(operation, lfs_batch_action, **kwargs):
            p_in_repo = getattr(operation, "path_in_repo", "blob")
            op_sz = 0
            if hasattr(operation, "upload_info") and hasattr(operation.upload_info, "size"):
                op_sz = operation.upload_info.size
            elif isinstance(getattr(operation, "path_or_fileobj", None), (str, Path)):
                try:
                    op_sz = os.path.getsize(operation.path_or_fileobj)
                except Exception:
                    pass

            tracker.add_log(f"[Batch {tracker.current_batch}/{tracker.total_batches}] Uploading [{tracker.completed_ops + 1}/{tracker.total_ops}]: {os.path.basename(p_in_repo)} ({_format_size(op_sz)})...")
            tracker.update_progress(current_file=p_in_repo, force_save=True)

            t_start = time.time()
            res = orig_lfs_upload(operation, lfs_batch_action, **kwargs)
            f_dur = max(0.1, time.time() - t_start)
            f_spd = (op_sz / (1024 * 1024)) / f_dur

            tracker.completed_ops += 1
            tracker.completed_bytes += op_sz
            tracker.add_log(f"✓ Uploaded {os.path.basename(p_in_repo)} ({_format_size(op_sz)}) in {f_dur:.1f}s ({f_spd:.1f} MB/s)")
            tracker.update_progress(
                current_file="",
                bytes_done=tracker.completed_bytes,
                ops_done=tracker.completed_ops,
                force_save=True
            )
            return res

        hf_xet_reporting.XetUploadProgressReporter.update_progress = hooked_xet_update
        hf_commit_api.lfs_upload = hooked_lfs_upload

        try:
            for b_idx, batch_files in enumerate(batches, 1):
                pause_ev = _PUSH_PAUSE_FLAGS.get(repo_folder)
                if pause_ev and pause_ev.is_set():
                    tracker.add_log(f"⏸️ Push paused by user before Batch {b_idx}/{total_batches}.")
                    manifest = load_staging_manifest()
                    if repo_folder in manifest.get("repositories", {}):
                        manifest["repositories"][repo_folder]["status"] = "paused"
                        manifest["repositories"][repo_folder]["push_progress"]["message"] = f"Push paused before Batch {b_idx} of {total_batches}"
                        save_staging_manifest(manifest)
                    return

                from stream_proxy import load_manifest as load_vault_manifest, save_manifest as save_vault_manifest
                vault_mf = load_vault_manifest()

                batch_commit_ops = []
                batch_staged_files_to_delete = []
                batch_enc_b = 0

                for f_entry in batch_files:
                    enc_p = f_entry.get("staged_enc_path")
                    blob_p = f_entry.get("blob_path")
                    m_hash = f_entry.get("media_hash")

                    # Push-Time Deduplication against Hugging Face Cloud
                    existing_cloud = None
                    if m_hash and acc_mgr:
                        existing_cloud = acc_mgr.find_duplicate(m_hash, vault_mf)
                    elif m_hash:
                        existing_cloud = vault_mf.get("media_hashes", {}).get(m_hash)

                    if existing_cloud:
                        dup_owner = existing_cloud.get("account") or existing_cloud.get("repo_id", "cloud")
                        dup_blob = existing_cloud.get("blob_path") or existing_cloud.get("remote_path") or blob_p
                        tracker.add_log(f"⚡ [Cloud Dedup] '{f_entry.get('rel_in_repo')}' already uploaded on {dup_owner} ({dup_blob}). Skipping 2GB blob re-upload!")
                        f_entry["cloud_dedup_alias"] = existing_cloud
                        if enc_p and os.path.exists(enc_p):
                            batch_staged_files_to_delete.append(enc_p)
                    else:
                        if enc_p and os.path.exists(enc_p):
                            batch_commit_ops.append(CommitOperationAdd(path_in_repo=blob_p, path_or_fileobj=enc_p))
                            batch_staged_files_to_delete.append(enc_p)
                            batch_enc_b += os.path.getsize(enc_p)

                    for sc in f_entry.get("sidecars", []):
                        sc_p = sc.get("staged_path")
                        sc_b = sc.get("blob_path")
                        if sc_p and os.path.exists(sc_p):
                            batch_commit_ops.append(CommitOperationAdd(path_in_repo=sc_b, path_or_fileobj=sc_p))
                            batch_staged_files_to_delete.append(sc_p)
                            batch_enc_b += os.path.getsize(sc_p)

                tracker.current_batch = b_idx
                tracker.total_batches = total_batches
                tracker.completed_ops = 0
                tracker.total_ops = len(batch_commit_ops)
                tracker.completed_bytes = 0
                tracker.total_bytes = batch_enc_b
                tracker._completed_items_set.clear()

                tracker.add_log(f"--- [Batch {b_idx}/{total_batches}] Packaging {len(batch_files)} video package(s), {len(batch_commit_ops)} assets ({_format_size(batch_enc_b)}) ---")
                tracker.update_progress(force_save=True)

                # Commit execution with 5x auto-retry and exponential backoff [5s, 10s, 20s, 40s, 60s]
                commit_success = False
                last_error = None
                max_attempts = 5
                backoff_delays = [5, 10, 20, 40, 60]

                for attempt in range(1, max_attempts + 1):
                    if pause_ev and pause_ev.is_set():
                        tracker.add_log(f"⏸️ Push paused by user during Batch {b_idx}/{total_batches}.")
                        manifest = load_staging_manifest()
                        if repo_folder in manifest.get("repositories", {}):
                            manifest["repositories"][repo_folder]["status"] = "paused"
                            manifest["repositories"][repo_folder]["push_progress"]["message"] = f"Push paused on Batch {b_idx} of {total_batches}"
                            save_staging_manifest(manifest)
                        return

                    try:
                        if batch_commit_ops:
                            tracker.add_log(f"[Batch {b_idx}/{total_batches}] Transmitting commit to Hugging Face Git Commit API (Attempt {attempt}/{max_attempts})...")
                            commit_msg = commit_helper.get_human_commit_message(
                                f"batch upload: {repo_folder} (Batch {b_idx}/{total_batches}, {len(batch_files)} videos)"
                            )
                            commit_res = target_api.create_commit(
                                repo_id=repo_id,
                                repo_type="dataset",
                                operations=batch_commit_ops,
                                commit_message=commit_msg
                            )
                            commit_success = True
                            tracker.set_retry_state(False)
                            c_url = getattr(commit_res, "commit_url", None)
                            c_oid = getattr(commit_res, "oid", "")
                            if not c_url and c_oid:
                                c_url = f"https://huggingface.co/datasets/{repo_id}/commit/{c_oid}"
                            elif not c_url:
                                c_url = f"https://huggingface.co/datasets/{repo_id}/commits/main"
                        else:
                            tracker.add_log(f"✓ Batch {b_idx}/{total_batches}: All blobs deduplicated against existing cloud items. 0 remote upload operations needed.")
                            c_url = f"https://huggingface.co/datasets/{repo_id}/commits/main"
                            c_oid = ""
                            commit_success = True
                            tracker.set_retry_state(False)

                        commit_entry = {
                            "batch": b_idx,
                            "total_batches": total_batches,
                            "commit_url": c_url,
                            "commit_oid": str(c_oid)[:8] if c_oid else "",
                            "videos": len(batch_files),
                            "assets": len(batch_commit_ops),
                            "size_formatted": _format_size(batch_enc_b)
                        }
                        tracker.completed_commits.append(commit_entry)
                        if batch_commit_ops:
                            tracker.add_log(f"✓ Git Commit accepted for Batch {b_idx}/{total_batches} ({_format_size(batch_enc_b)}) → {c_url}")
                        break
                    except Exception as e_commit:
                        last_error = e_commit
                        tracker.add_log(f"⚠️ Batch {b_idx}/{total_batches} attempt {attempt} failed: {e_commit}")
                        if attempt < max_attempts:
                            delay = backoff_delays[min(attempt - 1, len(backoff_delays) - 1)]
                            tracker.add_log(f"Auto-retrying in {delay}s (Attempt {attempt + 1}/{max_attempts}). Click [⚡ Retry Now] to bypass wait...")
                            retry_ev = _PUSH_RETRY_FLAGS.get(repo_folder)
                            if retry_ev:
                                retry_ev.clear()

                            for sec in range(delay, 0, -1):
                                tracker.set_retry_state(True, attempt=attempt + 1, countdown=sec, error_msg=str(e_commit))
                                if retry_ev and retry_ev.wait(timeout=1.0):
                                    tracker.add_log(f"⚡ Instant retry triggered by user!")
                                    retry_ev.clear()
                                    break
                                if pause_ev and pause_ev.is_set():
                                    break
                        else:
                            tracker.set_retry_state(False)
                            tracker.add_log(f"❌ Batch {b_idx}/{total_batches} failed after {max_attempts} attempts: {e_commit}")

                if not commit_success:
                    manifest = load_staging_manifest()
                    if repo_folder in manifest.get("repositories", {}):
                        manifest["repositories"][repo_folder]["status"] = "error"
                        manifest["repositories"][repo_folder]["push_progress"] = {
                            "percent": int(min(99, max(1, (tracker.overall_completed_bytes / tracker.overall_total_bytes) * 100))),
                            "current_batch": b_idx,
                            "total_batches": total_batches,
                            "message": f"Batch {b_idx}/{total_batches} failed: {last_error}. Click 'Resume Push' to continue from this batch.",
                            "log_lines": list(tracker.log_lines)
                        }
                        save_staging_manifest(manifest)
                    activity_tracker.tracker.add_event(
                        "error",
                        f"Push Error: {repo_folder}",
                        f"Batch {b_idx}/{total_batches} failed: {last_error}",
                        account=account
                    )
                    return

                # --- Batch Post-Commit Finalization ---
                # 1. Pre-extract local storyboards & thumbnails before deleting staged sidecars
                tracker.add_log(f"[Batch {b_idx}/{total_batches}] Verifying storyboards/thumbnails compiled locally...")
                for f_entry in batch_files:
                    try:
                        _ensure_local_storyboards_cached(f_entry, password)
                    except Exception as e_sb:
                        tracker.add_log(f"Note compiling storyboard for {f_entry.get('rel_in_repo')}: {e_sb}")

                # 2. Progressive local library registration: Update upload_manifest.json + Jellyfin STRM/NFO
                from stream_proxy import load_manifest as load_vault_manifest, save_manifest as save_vault_manifest
                vault_mf = load_vault_manifest()
                vault_files = vault_mf.setdefault("files", {})
                vault_hashes = vault_mf.setdefault("media_hashes", {})
                jf_enabled = account_manager.is_jellyfin_sync_enabled()

                for f_entry in batch_files:
                    rec = f_entry.get("record", {})
                    full_k = f_entry.get("full_key")
                    m_hash = f_entry.get("media_hash")
                    rec["status"] = "uploaded"
                    rec["uploaded_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    rec["updated_at"] = time.time()
                    rec["repo_id"] = repo_id
                    rec["account"] = account

                    if f_entry.get("cloud_dedup_alias"):
                        orig_c = f_entry["cloud_dedup_alias"]
                        orig_blob = orig_c.get("blob_path") or orig_c.get("remote_path")
                        orig_repo = orig_c.get("repo_id")
                        orig_acc = orig_c.get("account")
                        orig_k = orig_c.get("full_key")
                        if orig_blob:
                            rec["blob_path"] = orig_blob
                        if orig_repo:
                            rec["repo_id"] = orig_repo
                        if orig_acc:
                            rec["account"] = orig_acc
                        rec["is_alias_of"] = orig_k
                        tracker.add_log(f"🔗 Linked '{full_k}' as cloud alias of '{orig_k}' ({orig_repo})")

                    vault_files[full_k] = rec
                    if m_hash:
                        vault_hashes[m_hash] = rec

                    if jf_enabled:
                        try:
                            from watcher_service import auto_generate_strm
                            auto_generate_strm(repo_folder, f_entry.get("rel_in_repo"), library_dir=library_dir)
                            meta_info = rec.get("metadata") or {}
                            m_info = rec.get("media_info") or {}
                            metadata_helper.write_nfo_files(library_dir, full_k, meta_info, media_info=m_info)
                        except Exception as e_jf:
                            tracker.add_log(f"Note generating Jellyfin STRM/NFO: {e_jf}")

                save_vault_manifest(vault_mf)

                # 3. Continuous disk space reclamation: Delete staged encrypted files & sidecars for this batch
                deleted_count = 0
                reclaimed_bytes = 0
                for p in batch_staged_files_to_delete:
                    if os.path.exists(p):
                        try:
                            sz = os.path.getsize(p)
                            os.remove(p)
                            deleted_count += 1
                            reclaimed_bytes += sz
                        except Exception as e_del:
                            tracker.add_log(f"Note removing staged file {p}: {e_del}")

                tracker.add_log(f"✓ Reclaimed {_format_size(reclaimed_bytes)} SSD space (purged {deleted_count} staged files for Batch {b_idx})")

                # 4. Checkpoint staging manifest: Remove completed batch files from staging manifest
                manifest = load_staging_manifest()
                rdata = manifest.get("repositories", {}).get(repo_folder)
                if rdata:
                    batch_keys = {f.get("full_key") for f in batch_files}
                    remaining_files = [f for f in rdata.get("files", []) if f.get("full_key") not in batch_keys]
                    rdata["files"] = remaining_files
                    rdata["remaining_file_count"] = len(remaining_files)
                    rdata["total_videos"] = total_files
                    rdata["total_assets"] = overall_total_ops
                    rdata["total_orig_size"] = total_orig_b
                    rdata["total_enc_size"] = overall_total_b
                    rdata["total_batches"] = total_batches
                    save_staging_manifest(manifest)

                tracker.overall_completed_assets += len(batch_commit_ops)
                tracker.overall_completed_bytes += batch_enc_b
                tracker.update_progress(force_save=True)

        finally:
            hf_xet_reporting.XetUploadProgressReporter.update_progress = orig_xet_update
            hf_commit_api.lfs_upload = orig_lfs_upload

        # All batches completed successfully!
        elapsed = max(0.1, time.time() - tracker.t0)
        total_mb = tracker.overall_total_bytes / (1024 * 1024)
        speed_mbps = (total_mb * 8) / elapsed

        # Clean up empty staging folder if empty
        repo_stage_dir = os.path.join(STAGING_DIR, repo_folder)
        if os.path.exists(repo_stage_dir) and not os.listdir(repo_stage_dir):
            try:
                os.rmdir(repo_stage_dir)
            except Exception:
                pass

        tracker.add_log(f"✓ Multi-Commit Push SUCCESS: {total_files} file(s) ({_format_size(tracker.overall_total_bytes)}) committed across {total_batches} batch(es) to {repo_id} in {elapsed:.1f}s ({speed_mbps:.1f} Mbps)")
        manifest = load_staging_manifest()
        if repo_folder in manifest.get("repositories", {}):
            p_data = manifest["repositories"][repo_folder]
            p_data["status"] = "pushed"
            p_data["pushed_at"] = time.time()
            p_data["total_videos"] = total_files
            p_data["total_assets"] = overall_total_ops
            p_data["total_orig_size"] = total_orig_b
            p_data["total_enc_size"] = tracker.overall_total_bytes
            p_data["total_batches"] = total_batches
            p_data["push_progress"] = {
                "percent": 100,
                "current_batch": total_batches,
                "total_batches": total_batches,
                "batch_info": f"All {total_batches} batches committed" if total_batches > 1 else "1-Commit Push Complete",
                "total_videos": total_files,
                "uploaded_videos": total_files,
                "total_assets": overall_total_ops,
                "uploaded_assets": overall_total_ops,
                "uploaded_bytes": tracker.overall_total_bytes,
                "total_bytes": tracker.overall_total_bytes,
                "uploaded_bytes_formatted": _format_size(tracker.overall_total_bytes),
                "total_bytes_formatted": _format_size(tracker.overall_total_bytes),
                "speed_mbps": round(speed_mbps, 1),
                "speed_str": f"{speed_mbps:.1f} Mbps",
                "message": f"Successfully committed {total_files} video(s) ({overall_total_ops} assets, {_format_size(tracker.overall_total_bytes)}) in {total_batches} Git Commit{'s (~10 GB each)' if total_batches > 1 else ''} to Hugging Face!",
                "commits": list(tracker.completed_commits),
                "repo_url": f"https://huggingface.co/datasets/{repo_id}",
                "log_lines": list(tracker.log_lines)
            }
            save_staging_manifest(manifest)

        # Synchronize master cloud index once at completion
        master_repo = acc_mgr.get_master_repo_id()
        m_api = acc_mgr.get_master_api()
        try:
            tracker.add_log("Synchronizing master cloud index (vault_index.enc) to Hugging Face...")
            from stream_proxy import load_manifest as load_vault_manifest
            final_vault_mf = load_vault_manifest()
            metadata_helper.sync_master_manifest_to_hf(m_api, master_repo, final_vault_mf, password)
            tracker.add_log("✓ Master cloud index synchronized!")
        except Exception as e:
            tracker.add_log(f"Warning syncing master index: {e}")

        activity_tracker.tracker.add_event(
            "success",
            f"Pushed: {repo_folder}",
            f"Successfully pushed {total_files} file(s) ({_format_size(tracker.overall_total_bytes)}) across {total_batches} commit(s) in {elapsed:.1f}s ({speed_mbps:.1f} Mbps)",
            account=account,
            speed=f"{speed_mbps:.1f} Mbps"
        )

    except Exception as exc:
        print(f"[!] Error pushing repository {repo_folder}: {exc}")
        manifest = load_staging_manifest()
        if repo_folder in manifest.get("repositories", {}):
            manifest["repositories"][repo_folder]["status"] = "error"
            manifest["repositories"][repo_folder]["push_progress"] = {
                "percent": 0,
                "message": f"Error: {exc}"
            }
            save_staging_manifest(manifest)
        activity_tracker.tracker.add_event(
            "error",
            f"Push Failed: {repo_folder}",
            str(exc),
            account=account
        )
    finally:
        with _PUSH_LOCK:
            if _CURRENT_PUSHING_REPO == repo_folder:
                _CURRENT_PUSHING_REPO = None
            _ACTIVE_PUSHES.discard(repo_folder)
            _PUSH_PAUSE_FLAGS.pop(repo_folder, None)
            _PUSH_RETRY_FLAGS.pop(repo_folder, None)

            # Update queue positions for remaining queued repos
            try:
                manifest = load_staging_manifest()
                for idx, q_rf in enumerate(_PUSH_QUEUE, 1):
                    qr = manifest.get("repositories", {}).get(q_rf)
                    if qr and qr.get("status") == "queued" and "push_progress" in qr:
                        qr["push_progress"]["queue_position"] = idx
                        qr["push_progress"]["message"] = f"Queued (#{idx} in line): Next up to push..." if idx == 1 else f"Queued (#{idx} in line)..."
                save_staging_manifest(manifest)
            except Exception:
                pass


def pause_staging(repo_folder: str) -> tuple:
    """Pauses local pre-encryption and asset staging for a repository folder."""
    manifest = load_staging_manifest()
    repo_data = manifest.get("repositories", {}).get(repo_folder)
    if not repo_data:
        source_dir = os.path.join("videos_to_sync", repo_folder)
        if os.path.exists(source_dir):
            repo_data = {
                "status": "paused_staging",
                "staging_paused": True,
                "files": [],
                "created_at": time.time(),
                "paused_at": time.time()
            }
            manifest.setdefault("repositories", {})[repo_folder] = repo_data
        else:
            return False, f"Repository '{repo_folder}' not found in staging manifest."

    repo_data["staging_paused"] = True
    repo_data["status"] = "paused_staging"
    repo_data["paused_at"] = time.time()
    save_staging_manifest(manifest)

    activity_tracker.tracker.add_event(
        "warning",
        f"Staging Paused: {repo_folder}",
        f"Pre-encryption paused for {repo_folder}."
    )
    return True, f"Staging paused for '{repo_folder}'."


def resume_staging(repo_folder: str) -> tuple:
    """Resumes local pre-encryption and asset staging for a paused repository folder."""
    manifest = load_staging_manifest()
    repo_data = manifest.get("repositories", {}).get(repo_folder)
    if not repo_data:
        return False, f"Repository '{repo_folder}' not found in staging manifest."

    repo_data["staging_paused"] = False
    audit_rep = repo_data.get("audit_report") or {}
    source_dir = os.path.join("videos_to_sync", repo_folder)
    has_pending = _has_actual_files(source_dir)
    if not has_pending and audit_rep.get("passed"):
        repo_data["status"] = "ready_to_push"
        repo_data["ready_at"] = repo_data.get("ready_at") or time.time()
    else:
        repo_data["status"] = "staging"
    repo_data["resumed_at"] = time.time()
    save_staging_manifest(manifest)

    activity_tracker.tracker.add_event(
        "info",
        f"Staging Resumed: {repo_folder}",
        f"Pre-encryption resumed for {repo_folder}."
    )
    return True, f"Staging resumed for '{repo_folder}'."


def is_staging_paused(repo_folder: str) -> bool:
    """Returns True if staging is currently paused for the given repository folder."""
    try:
        manifest = load_staging_manifest()
        repo_data = manifest.get("repositories", {}).get(repo_folder)
        if not repo_data:
            return False
        return bool(repo_data.get("staging_paused") or repo_data.get("status") == "paused_staging")
    except Exception:
        return False


def discard_staged_repo(repo_folder: str) -> tuple:
    """Purges the staged repository data and deletes its folder entry from staging manifest."""
    manifest = load_staging_manifest()
    repo_data = manifest.get("repositories", {}).get(repo_folder)
    if not repo_data:
        return False, f"Repository '{repo_folder}' not found in staging manifest."
    
    stage_dir = get_repo_staging_dir(repo_folder)
    if os.path.exists(stage_dir):
        try:
            shutil.rmtree(stage_dir, ignore_errors=True)
        except Exception as e:
            print(f"[!] Warning cleaning stage dir '{stage_dir}': {e}")

    # Also clean up archived duplicates for this repo_folder
    archive_dir = os.path.join("archive", repo_folder)
    if os.path.exists(archive_dir):
        try:
            shutil.rmtree(archive_dir, ignore_errors=True)
        except Exception as e:
            print(f"[!] Warning cleaning archive dir '{archive_dir}': {e}")

    # Clean up alias records in upload_manifest.json created for this repo_folder
    try:
        from stream_proxy import load_manifest as load_vault_m, save_manifest as save_vault_m, MANIFEST_LOCK
        with MANIFEST_LOCK:
            up_m = load_vault_m()
            changed_m = False
            for k in list(up_m.get("files", {}).keys()):
                if k.startswith(f"{repo_folder}/") or k == repo_folder:
                    del up_m["files"][k]
                    changed_m = True
            for mh, rec in list(up_m.get("media_hashes", {}).items()):
                if rec.get("full_key", "").startswith(f"{repo_folder}/"):
                    del up_m["media_hashes"][mh]
                    changed_m = True
            if changed_m:
                save_vault_m(up_m)
    except Exception:
        pass

    source_dir = os.path.join("videos_to_sync", repo_folder)
    has_source = _has_actual_files(source_dir)
    if has_source:
        repo_data["status"] = "ignored"
        repo_data["files"] = []
        repo_data["file_count"] = 0
        repo_data["total_orig_size"] = 0
        repo_data["total_enc_size"] = 0
        repo_data["ignored_at"] = time.time()
        if "staging_paused" in repo_data:
            del repo_data["staging_paused"]
    else:
        del manifest["repositories"][repo_folder]

    save_staging_manifest(manifest)
    activity_tracker.tracker.add_event(
        "info",
        f"Staging Discarded: {repo_folder}",
        f"Staged files purged for {repo_folder}."
    )
    return True, f"Staged repository '{repo_folder}' discarded successfully."


def unstage_and_restore_repo(repo_folder: str, password: str, base_dir: str = "videos_to_sync", progress_callback=None) -> tuple:
    """
    Safely unstages a repository by decrypting all pre-encrypted video blobs back to videos_to_sync/.
    Performs strict 100% size and integrity validation on each restored file.
    ONLY purges staged encrypted files once ALL files in the repository have been verified intact on disk.
    Marks the folder as 'ignored' so watcher_service will not immediately re-stage it.
    """
    manifest = load_staging_manifest()
    repo_data = manifest.get("repositories", {}).get(repo_folder)
    if not repo_data:
        return False, f"Repository '{repo_folder}' not found in staging manifest."

    files = repo_data.get("files", [])
    if not files:
        return False, f"No staged files recorded for repository '{repo_folder}' to unstage."

    if not password:
        return False, "Master encryption password required for unstage decryption."

    # Pre-flight check: ensure all staged encrypted files exist before attempting restore
    for item in files:
        staged_path = item.get("staged_enc_path")
        if not staged_path or not os.path.exists(staged_path):
            return False, f"Missing staged encrypted file on disk: {staged_path}. Cannot unstage."

    restored_files = []
    total_files = len(files)
    total_bytes = repo_data.get("total_orig_size", 0)
    bytes_restored = 0

    try:
        for idx, item in enumerate(files, 1):
            staged_path = item.get("staged_enc_path")
            rel_in_repo = item.get("rel_in_repo") or item.get("orig_filename")
            # Preserve repo_folder structure in videos_to_sync
            target_path = os.path.join(base_dir, repo_folder, rel_in_repo)
            orig_size = item.get("orig_size", 0)

            # Decrypt file with chunk-by-chunk AES-256-GCM authentication
            dec_size = crypto_engine.decrypt_file(staged_path, target_path, password)
            if dec_size != orig_size:
                raise ValueError(f"Restored file '{target_path}' size mismatch: expected {orig_size}, got {dec_size}")

            restored_files.append(target_path)
            bytes_restored += dec_size
            if progress_callback:
                try:
                    progress_callback(idx, total_files, bytes_restored, total_bytes)
                except Exception:
                    pass

    except Exception as e:
        # SAFETY INTERVENTION: Do not delete any staged files!
        for p in restored_files:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
        return False, f"Unstage failed during decryption of '{item.get('orig_filename')}': {e}. Staged encrypted files preserved intact."

    # ALL FILES 100% VERIFIED ON DISK: Safely clean up staged encrypted files
    stage_dir = get_repo_staging_dir(repo_folder)
    if os.path.exists(stage_dir):
        try:
            shutil.rmtree(stage_dir, ignore_errors=True)
        except Exception as e:
            print(f"[!] Warning cleaning stage dir '{stage_dir}': {e}")

    # RESTORE ALL ARCHIVED DUPLICATE FILES BACK TO videos_to_sync/
    archive_dir = os.path.join("archive", repo_folder)
    archived_restored = 0
    if os.path.exists(archive_dir):
        for root, _, arch_files in os.walk(archive_dir):
            for af in arch_files:
                src_af = os.path.join(root, af)
                rel_af = os.path.relpath(src_af, archive_dir)
                dest_af = os.path.join(base_dir, repo_folder, rel_af)
                os.makedirs(os.path.dirname(dest_af), exist_ok=True)
                try:
                    if os.path.exists(dest_af):
                        os.remove(dest_af)
                    shutil.move(src_af, dest_af)
                    restored_files.append(dest_af)
                    archived_restored += 1
                except Exception as ex_m:
                    print(f"[!] Warning restoring archived file '{src_af}' -> '{dest_af}': {ex_m}")
        try:
            shutil.rmtree(archive_dir, ignore_errors=True)
        except Exception:
            pass

    # Clean up alias records in upload_manifest.json created for this repo_folder
    try:
        from stream_proxy import load_manifest as load_vault_m, save_manifest as save_vault_m, MANIFEST_LOCK
        with MANIFEST_LOCK:
            up_m = load_vault_m()
            changed_m = False
            for k in list(up_m.get("files", {}).keys()):
                if k.startswith(f"{repo_folder}/") or k == repo_folder:
                    del up_m["files"][k]
                    changed_m = True
            for mh, rec in list(up_m.get("media_hashes", {}).items()):
                if rec.get("full_key", "").startswith(f"{repo_folder}/"):
                    del up_m["media_hashes"][mh]
                    changed_m = True
            if changed_m:
                save_vault_m(up_m)
    except Exception:
        pass

    repo_data["status"] = "ignored"
    repo_data["files"] = []
    repo_data["total_orig_size"] = 0
    repo_data["total_enc_size"] = 0
    repo_data["ignored_at"] = time.time()
    if "staging_paused" in repo_data:
        del repo_data["staging_paused"]
    save_staging_manifest(manifest)

    msg_details = f"100% Decrypted and verified {len(restored_files) - archived_restored} staged files"
    if archived_restored > 0:
        msg_details += f" + restored {archived_restored} archived duplicate files"
    msg_details += f" back to {base_dir}/{repo_folder}. Staged cache purged."

    activity_tracker.tracker.add_event(
        "success",
        f"Staging Unstaged & Restored: {repo_folder}",
        msg_details
    )
    return True, f"Successfully unstaged and verified {len(restored_files)} files to '{base_dir}/{repo_folder}'."


def revert_and_ignore_staged_repo(repo_folder: str, password: str = None) -> tuple:
    """
    Reverts staged status by un-staging and decrypting files back to videos_to_sync/ if files exist,
    or directly purging if no staged files remain. Marks folder as 'ignored'.
    """
    manifest = load_staging_manifest()
    repo_data = manifest.get("repositories", {}).get(repo_folder)
    if not repo_data:
        return False, f"Repository '{repo_folder}' not found in staging manifest."

    files = repo_data.get("files", [])
    if files and password:
        # If staged files exist and password is provided, unstage and restore with 100% verification
        return unstage_and_restore_repo(repo_folder, password)

    stage_dir = get_repo_staging_dir(repo_folder)
    if os.path.exists(stage_dir):
        try:
            shutil.rmtree(stage_dir, ignore_errors=True)
        except Exception as e:
            print(f"[!] Warning cleaning stage dir '{stage_dir}': {e}")

    # Restore any archived duplicate files if files was empty
    archive_dir = os.path.join("archive", repo_folder)
    if os.path.exists(archive_dir):
        for root, _, arch_files in os.walk(archive_dir):
            for af in arch_files:
                src_af = os.path.join(root, af)
                rel_af = os.path.relpath(src_af, archive_dir)
                dest_af = os.path.join("videos_to_sync", repo_folder, rel_af)
                os.makedirs(os.path.dirname(dest_af), exist_ok=True)
                try:
                    if os.path.exists(dest_af):
                        os.remove(dest_af)
                    shutil.move(src_af, dest_af)
                except Exception:
                    pass
        try:
            shutil.rmtree(archive_dir, ignore_errors=True)
        except Exception:
            pass

    # Clean up alias records in upload_manifest.json created for this repo_folder
    try:
        from stream_proxy import load_manifest as load_vault_m, save_manifest as save_vault_m, MANIFEST_LOCK
        with MANIFEST_LOCK:
            up_m = load_vault_m()
            changed_m = False
            for k in list(up_m.get("files", {}).keys()):
                if k.startswith(f"{repo_folder}/") or k == repo_folder:
                    del up_m["files"][k]
                    changed_m = True
            for mh, rec in list(up_m.get("media_hashes", {}).items()):
                if rec.get("full_key", "").startswith(f"{repo_folder}/"):
                    del up_m["media_hashes"][mh]
                    changed_m = True
            if changed_m:
                save_vault_m(up_m)
    except Exception:
        pass

    repo_data["status"] = "ignored"
    repo_data["files"] = []
    repo_data["file_count"] = 0
    repo_data["total_orig_size"] = 0
    repo_data["total_enc_size"] = 0
    repo_data["ignored_at"] = time.time()
    if "staging_paused" in repo_data:
        del repo_data["staging_paused"]
    save_staging_manifest(manifest)
    activity_tracker.tracker.add_event(
        "info",
        f"Staging Reverted & Ignored: {repo_folder}",
        f"Staged files purged. Folder '{repo_folder}' marked as ignored."
    )
    return True, f"Folder '{repo_folder}' reverted and marked as ignored."


def unignore_staged_repo(repo_folder: str, watch_dir: str = "videos_to_sync") -> tuple:
    """Removes the 'ignored' mark so watcher_service can stage the folder again."""
    manifest = load_staging_manifest()
    repo_data = manifest.get("repositories", {}).get(repo_folder)
    if not repo_data:
        return False, f"Repository '{repo_folder}' not found."

    source_path = os.path.join(watch_dir, repo_folder)
    staged_path = get_repo_staging_dir(repo_folder)
    has_staged = bool(repo_data.get("files")) or _has_actual_files(staged_path)
    has_source = _has_actual_files(source_path)

    # If neither source files nor staged files exist on disk, clean up the ghost entry entirely
    if not has_staged and not has_source:
        del manifest["repositories"][repo_folder]
        save_staging_manifest(manifest)
        activity_tracker.tracker.add_event(
            "info",
            f"Staging Entry Cleared: {repo_folder}",
            f"Folder '{repo_folder}' had no files on disk and was removed from the queue."
        )
        return True, f"Folder '{repo_folder}' had no files on disk and was removed from the queue."

    repo_data["status"] = "staging"
    repo_data["file_count"] = len(repo_data.get("files", []))
    if "ignored_at" in repo_data:
        del repo_data["ignored_at"]
    if "staging_paused" in repo_data:
        del repo_data["staging_paused"]
    save_staging_manifest(manifest)
    activity_tracker.tracker.add_event(
        "info",
        f"Staging Re-enabled: {repo_folder}",
        f"Folder '{repo_folder}' is no longer ignored and ready for staging."
    )
    return True, f"Folder '{repo_folder}' unignored and re-enabled for staging."
