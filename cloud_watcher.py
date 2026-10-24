import os
import sys
import time
import json
import random
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from huggingface_hub import HfApi

import crypto_engine
import account_manager
import activity_tracker
import metadata_helper

MANIFEST_FILE = "upload_manifest.json"
GLOBAL_MANIFEST_LOCK = threading.Lock()

# Global state for client activity tracking
_LAST_CLIENT_ACTIVITY_TIME = time.time()
_WATCHER_INSTANCE = None


def notify_client_active():
    """Notifies the cloud watcher that a client is actively using the web UI."""
    global _LAST_CLIENT_ACTIVITY_TIME
    _LAST_CLIENT_ACTIVITY_TIME = time.time()


def is_client_active(active_window_sec=60) -> bool:
    """Returns True if a client interacted with the server in the last active_window_sec."""
    return (time.time() - _LAST_CLIENT_ACTIVITY_TIME) < active_window_sec


def merge_remote_repo_records(
    repo_id: str,
    remote_records: dict,
    manifest_path: str = MANIFEST_FILE,
    library_dir: str = "jellyfin_library",
    proxy_host: str = "127.0.0.1:8080",
    is_master: bool = False
) -> dict:
    """
    Performs an optimistic union-merge between the remote repository records and
    the local upload_manifest.json. Resolves conflicts deterministically using
    record updated_at timestamps (Last-Write-Wins).
    Automatically updates local Jellyfin .strm and .nfo files if Jellyfin sync is enabled.
    Returns: { "added": [...], "updated": [...], "deleted": [...] }
    """
    with GLOBAL_MANIFEST_LOCK:
        manifest = {"files": {}, "media_hashes": {}}
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
            except Exception:
                pass

        files = manifest.setdefault("files", {})
        media_hashes = manifest.setdefault("media_hashes", {})
        tombstones = manifest.setdefault("tombstones", {})

        added = []
        updated = []
        deleted = []

        # Find all currently local files belonging to this repository (or all files if master index)
        if is_master:
            local_repo_keys = [
                k for k, v in files.items()
                if v.get("status") == "uploaded"
            ]
        else:
            local_repo_keys = [
                k for k, v in files.items()
                if v.get("repo_id") == repo_id and v.get("status") == "uploaded"
            ]

        # 1. Process remote records (additions and updates)
        for key, r_rec in remote_records.items():
            # Never resurrect files that were explicitly deleted!
            if key in tombstones:
                continue

            r_rec["full_key"] = key
            r_rec["repo_id"] = r_rec.get("repo_id") or repo_id
            r_ts = float(r_rec.get("updated_at") or 0.0)

            if key not in files:
                # Newly discovered file from PC 1
                files[key] = r_rec
                m_hash = r_rec.get("media_hash")
                if m_hash:
                    media_hashes[m_hash] = r_rec
                added.append(key)
            else:
                # Existing file: compare updated_at
                l_ts = float(files[key].get("updated_at") or 0.0)
                if r_ts > l_ts:
                    files[key] = r_rec
                    m_hash = r_rec.get("media_hash")
                    if m_hash:
                        media_hashes[m_hash] = r_rec
                    updated.append(key)

        # 2. Detect remote deletions (file existed locally in repo_id, but is gone from remote index)
        remote_keys_set = set(remote_records.keys())
        now_ts = time.time()
        for k in local_repo_keys:
            if k not in remote_keys_set:
                del files[k]
                tombstones[k] = now_ts
                deleted.append(k)

        # 3. Save updated manifest atomically if any changes occurred
        has_changes = bool(added or updated or deleted)
        if has_changes:
            temp_path = f"{manifest_path}.tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2)
            os.replace(temp_path, manifest_path)

        # 4. Handle local Jellyfin sync if enabled
        jelly_enabled = account_manager.is_jellyfin_sync_enabled()
        if jelly_enabled and library_dir and has_changes:
            # Clean up deleted files from Jellyfin library
            for k in deleted:
                try:
                    metadata_helper.delete_nfo_files(library_dir, k)
                    strm_f = Path(library_dir) / f"{k}.strm"
                    if strm_f.exists():
                        strm_f.unlink()
                except Exception:
                    pass

            # Write/update .strm and .nfo for added or updated items
            for k in added + updated:
                try:
                    rec = files.get(k)
                    if not rec:
                        continue
                    strm_p = Path(library_dir) / f"{k}.strm"
                    strm_p.parent.mkdir(parents=True, exist_ok=True)
                    strm_p.write_text(f"http://{proxy_host}/stream/{k}\n", encoding="utf-8")

                    meta = rec.get("metadata") or metadata_helper.extract_default_metadata(k)
                    metadata_helper.write_nfo_files(library_dir, k, meta, media_info=rec.get("media_info"))
                except Exception:
                    pass

        return {
            "added": added,
            "updated": updated,
            "deleted": deleted,
            "has_changes": has_changes
        }


class CloudCommitWatcher:
    """
    Autonomous multi-server synchronizer.
    Continuously monitors all Hugging Face repositories across all configured accounts.
    Uses adaptive polling (5-8s active, 30-60s idle) with lightweight repo SHA checking (<250ms).
    Instantly merges cloud changes into local manifest and signals live in-place UI updates.
    """
    def __init__(
        self,
        acc_mgr=None,
        active_interval: float = 6.0,
        idle_interval: float = 35.0,
        library_dir: str = "jellyfin_library",
        proxy_host: str = "127.0.0.1:8080"
    ):
        self.acc_mgr = acc_mgr or account_manager.AccountManager()
        self.active_interval = active_interval
        self.idle_interval = idle_interval
        self.library_dir = library_dir
        self.proxy_host = proxy_host

        self.running = False
        self.thread = None
        self.known_shas = {}          # repo_id -> commit_sha
        self.is_syncing = False
        self._check_event = threading.Event()

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="CloudCommitWatcher")
        self.thread.start()
        print("[*] Cloud Commit Watcher active: multi-server sync enabled.")

    def stop(self):
        self.running = False
        self._check_event.set()

    def trigger_check(self):
        """Immediately wakes up the watcher to perform a cloud check cycle."""
        self._check_event.set()

    def get_tracked_repos(self) -> list:
        """Collects all unique repositories associated with manifest files and connected accounts."""
        repos = set()
        # 1. Repos from local upload_manifest.json
        if os.path.exists(MANIFEST_FILE):
            try:
                with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for rec in data.get("files", {}).values():
                    r = rec.get("repo_id")
                    if r:
                        repos.add(r.strip())
            except Exception:
                pass

        # 2. Default repos from accounts
        for acc in self.acc_mgr.get_all_accounts():
            u = acc.get("username")
            if u:
                repos.add(f"{u}/encrypted-media-vault")

        return sorted(list(repos))

    def _query_repo_sha(self, repo_id: str) -> tuple:
        """Queries single repo git commit SHA via HF API."""
        try:
            api = self.acc_mgr.get_api_for_repo(repo_id)
            if not api:
                return repo_id, None
            info = api.repo_info(repo_id=repo_id, repo_type="dataset")
            return repo_id, getattr(info, "sha", None)
        except Exception:
            return repo_id, None

    def _check_shas_parallel(self, repos: list) -> dict:
        """Queries commit SHAs for multiple repositories concurrently."""
        results = {}
        if not repos:
            return results
        with ThreadPoolExecutor(max_workers=min(len(repos), 5)) as pool:
            futures = [pool.submit(self._query_repo_sha, r) for r in repos]
            for f in as_completed(futures):
                r_id, sha = f.result()
                if sha:
                    results[r_id] = sha
        return results

    def _sync_master_index(self) -> bool:
        """Downloads canonical master vault_index.enc, decrypts in-memory, and merges all records."""
        master_id = self.acc_mgr.get_master_repo_id()
        token = self.acc_mgr.get_token_for_repo(master_id)
        pwd = self.acc_mgr.password
        if not token or not pwd:
            return False

        url = f"https://huggingface.co/datasets/{master_id}/resolve/main/vault_index.enc"
        try:
            import zlib
            resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=12)
            if resp.status_code == 200 and resp.content:
                dec = crypto_engine.decrypt_bytes(resp.content, pwd)
                try:
                    remote_records = json.loads(zlib.decompress(dec).decode("utf-8"))
                except Exception:
                    remote_records = json.loads(dec.decode("utf-8"))

                res = merge_remote_repo_records(
                    repo_id=master_id,
                    remote_records=remote_records,
                    library_dir=self.library_dir,
                    proxy_host=self.proxy_host,
                    is_master=True
                )
                if res.get("has_changes"):
                    print(f"[{time.strftime('%H:%M:%S')}] [Master Index Auto-Sync] {master_id}: +{len(res['added'])} added, ~{len(res['updated'])} updated, -{len(res['deleted'])} deleted")
                    activity_tracker.tracker.notify_manifest_updated()
                return True
            elif resp.status_code == 404:
                return False
        except Exception as e:
            sys.stderr.write(f"[!] Warning syncing master cloud index ({master_id}): {e}\n")
            return False
        return False

    def _sync_repo_index(self, repo_id: str) -> bool:
        """Downloads vault_index.enc from repo, decrypts in-memory, and merges."""
        token = self.acc_mgr.get_token_for_repo(repo_id)
        pwd = self.acc_mgr.password
        if not token or not pwd:
            return False

        url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/vault_index.enc"
        try:
            import zlib
            resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=12)
            if resp.status_code == 200 and resp.content:
                dec = crypto_engine.decrypt_bytes(resp.content, pwd)
                try:
                    remote_records = json.loads(zlib.decompress(dec).decode("utf-8"))
                except Exception:
                    remote_records = json.loads(dec.decode("utf-8"))

                is_m = (repo_id == self.acc_mgr.get_master_repo_id())
                res = merge_remote_repo_records(
                    repo_id=repo_id,
                    remote_records=remote_records,
                    library_dir=self.library_dir,
                    proxy_host=self.proxy_host,
                    is_master=is_m
                )
                if res.get("has_changes"):
                    print(f"[{time.strftime('%H:%M:%S')}] [Cloud Auto-Sync] {repo_id}: +{len(res['added'])} added, ~{len(res['updated'])} updated, -{len(res['deleted'])} deleted")
                    activity_tracker.tracker.notify_manifest_updated()
                return True
            elif resp.status_code == 404:
                return False
        except Exception as e:
            sys.stderr.write(f"[!] Warning syncing cloud index for {repo_id}: {e}\n")
            return False
        return False

    def _run_loop(self):
        # Initial sleep of 2 seconds on startup to let local services initialize
        time.sleep(2.0)

        master_id = self.acc_mgr.get_master_repo_id()
        print(f"[*] Cloud Commit Watcher initialized in Master Index Mode (Master Repo: {master_id})")

        # Initial startup sync: load master index first
        try:
            self._sync_master_index()
        except Exception:
            pass

        # Record baseline SHA for master repo
        _, m_sha = self._query_repo_sha(master_id)
        if m_sha:
            self.known_shas[master_id] = m_sha

        while self.running:
            # Adaptive sleep interval based on client interaction
            if is_client_active():
                interval = self.active_interval
            else:
                interval = self.idle_interval

            self._check_event.wait(timeout=interval)
            self._check_event.clear()

            if not self.running:
                break

            self.is_syncing = True
            try:
                # 1-Check Master Polling (<200ms)
                _, new_m_sha = self._query_repo_sha(master_id)
                if new_m_sha:
                    old_m_sha = self.known_shas.get(master_id)
                    if old_m_sha and new_m_sha != old_m_sha:
                        # Cloud change detected on PC 1!
                        self.known_shas[master_id] = new_m_sha
                        self._sync_master_index()
                    elif not old_m_sha:
                        self.known_shas[master_id] = new_m_sha
            except Exception as e:
                pass
            finally:
                self.is_syncing = False


def get_cloud_watcher() -> CloudCommitWatcher:
    global _WATCHER_INSTANCE
    if _WATCHER_INSTANCE is None:
        _WATCHER_INSTANCE = CloudCommitWatcher()
    return _WATCHER_INSTANCE
