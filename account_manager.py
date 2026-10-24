import os
import re
import json
import shutil
from pathlib import Path
from huggingface_hub import HfApi
import requests
import crypto_engine
import metadata_helper

ENV_FILE = "env.txt"


def check_token_write_permission(api: HfApi, token: str) -> tuple:
    """
    Checks whether a Hugging Face token has write/delete permissions.
    Returns (can_write: bool, role_desc: str).
    """
    if not token or not api:
        return False, "No token or API"
    try:
        info = api.whoami()
        auth = info.get("auth", {})
        access_token = auth.get("accessToken", {})
        role = access_token.get("role", "")

        # Classic Hugging Face tokens
        if role in ("write", "admin"):
            return True, f"Classic {role.title()}"
        if role == "read":
            return False, "Classic Read-Only (Write operations disabled)"

        # Fine-grained tokens
        if role == "fineGrained":
            fg = access_token.get("fineGrained", {})
            scoped = fg.get("scoped", [])
            global_perms = fg.get("global", [])

            has_write = any("repo.write" in p or "repo.contribute.write" in p for p in global_perms)
            if not has_write:
                for sc in scoped:
                    perms = sc.get("permissions", [])
                    if any("repo.write" in p or "repo.contribute.write" in p for p in perms):
                        has_write = True
                        break

            if has_write:
                return True, "Fine-Grained Write"
            else:
                return False, "Fine-Grained Read-Only (repo.write missing)"

        return True, "Standard Access"
    except Exception as e:
        err_str = str(e).lower()
        if "401" in err_str or "unauthorized" in err_str:
            return False, "Invalid / Unauthorized Token"
        if "403" in err_str or "forbidden" in err_str:
            return False, "Forbidden / Lacks Access"
        return True, "Assumed Valid"


def load_env_config(env_path=ENV_FILE):
    config = {}
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    config[k.strip()] = v.strip()
    # Cloud & container fallback: pull secrets and config from system environment (e.g. HF Spaces)
    for k, v in os.environ.items():
        if k not in config and v:
            config[k] = v
    return config


def is_jellyfin_sync_enabled(env_path=ENV_FILE) -> bool:
    """Returns True if local Jellyfin folder sync is enabled in env.txt or system env."""
    cfg = load_env_config(env_path)
    val = cfg.get("ENABLE_JELLYFIN_SYNC", "1").strip().lower()
    return val in ("1", "true", "yes", "on")


def set_jellyfin_sync_enabled(enabled: bool, env_path=ENV_FILE):
    """Saves the ENABLE_JELLYFIN_SYNC setting to env.txt."""
    save_env_config({"ENABLE_JELLYFIN_SYNC": "1" if enabled else "0"}, env_path)


def save_env_config(updates: dict, env_path=ENV_FILE):
    """
    Safely updates keys in env.txt while preserving comments, ordering,
    and existing keys.
    """
    lines = []
    keys_written = set()
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    k, v = stripped.split("=", 1)
                    k = k.strip()
                    if k in updates:
                        lines.append(f"{k}={updates[k]}\n")
                        keys_written.add(k)
                    else:
                        lines.append(f"{line.rstrip()}\n")
                else:
                    lines.append(f"{line.rstrip()}\n")

    for k, v in updates.items():
        if k not in keys_written:
            lines.append(f"{k}={v}\n")

    temp_path = f"{env_path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
    os.replace(temp_path, env_path)


class AccountManager:
    def __init__(self, env_path=ENV_FILE):
        self.env_path = env_path
        self.config = load_env_config(env_path)
        self.password = self.config.get("ENCRYPTION_PASSWORD", "")
        self.accounts = {}         # username -> { "username": ..., "token": ..., "api": ..., "is_upload": ..., "enabled": ... }
        self.token_to_user = {}     # token -> username
        self.primary_username = None
        self.upload_token = None
        self.disabled_users = set()
        self._init_accounts()

    def is_jellyfin_sync_enabled(self) -> bool:
        return is_jellyfin_sync_enabled(self.env_path)

    def set_jellyfin_sync_enabled(self, enabled: bool):
        set_jellyfin_sync_enabled(enabled, self.env_path)
        self.reload()

    def reload(self):
        """Reloads configuration from env.txt and re-authenticates."""
        self.config = load_env_config(self.env_path)
        self.password = self.config.get("ENCRYPTION_PASSWORD", "")
        self.accounts.clear()
        self.token_to_user.clear()
        self.primary_username = None
        self.upload_token = None
        self.disabled_users.clear()
        self._init_accounts()

    def _init_accounts(self):
        # Disabled accounts list
        disabled_raw = self.config.get("HF_DISABLED_USERS", "")
        if disabled_raw:
            for u in re.split(r"[,;\s]+", disabled_raw):
                if u.strip():
                    self.disabled_users.add(u.strip())

        # 1. Primary Upload Token
        upload_token = self.config.get("HF_UPLOAD_TOKEN") or self.config.get("HF_TOKEN")
        extra_tokens_raw = self.config.get("HF_EXTRA_TOKENS", "") or self.config.get("HF_READ_TOKENS", "")

        extra_tokens = []
        if extra_tokens_raw:
            for tok in re.split(r"[,;\s]+", extra_tokens_raw):
                tok = tok.strip()
                if tok and tok != upload_token and tok not in extra_tokens:
                    extra_tokens.append(tok)

        # Connect Primary Account
        if upload_token:
            try:
                api = HfApi(token=upload_token)
                user = api.whoami()["name"]
                can_write, role_desc = check_token_write_permission(api, upload_token)
                self.primary_username = user
                self.upload_token = upload_token
                acc_pw = self._get_account_password_from_config(user)
                has_custom = bool(acc_pw and acc_pw != self.password)
                effective_pw = acc_pw if acc_pw else self.password
                self.accounts[user] = {
                    "username": user,
                    "token": upload_token,
                    "password": effective_pw,
                    "has_custom_key": has_custom,
                    "api": api,
                    "is_upload": True,
                    "enabled": True,  # Primary is always active
                    "can_write": can_write,
                    "role_desc": role_desc
                }
                self.token_to_user[upload_token] = user
                self.disabled_users.discard(user)
                if not can_write:
                    print(f"[!] Warning: Primary HF token for user '{user}' lacks write permissions ({role_desc}). Write and delete operations will be disabled for this account to prevent runtime errors.")
            except Exception as e:
                print(f"[!] Warning: Primary HF upload token auth failed: {e}")

        # Connect Extra Read/Scan Accounts
        for tok in extra_tokens:
            try:
                api = HfApi(token=tok)
                user = api.whoami()["name"]
                if user in self.accounts:
                    continue
                is_enabled = (user not in self.disabled_users)
                can_write, role_desc = check_token_write_permission(api, tok)
                acc_pw = self._get_account_password_from_config(user)
                has_custom = bool(acc_pw and acc_pw != self.password)
                effective_pw = acc_pw if acc_pw else self.password
                self.accounts[user] = {
                    "username": user,
                    "token": tok,
                    "password": effective_pw,
                    "has_custom_key": has_custom,
                    "api": api,
                    "is_upload": False,
                    "enabled": is_enabled,
                    "can_write": can_write,
                    "role_desc": role_desc
                }
                self.token_to_user[tok] = user
                if not can_write:
                    print(f"[!] Info: Extra token for user '{user}' is read-only ({role_desc}). Write and delete operations will be safely skipped.")
            except Exception as e:
                print(f"[!] Warning: Extra HF token auth failed ({tok[:10]}...): {e}")

    def _get_account_password_from_config(self, username: str) -> str:
        """Looks up HF_PASSWORD_<username> in self.config case-insensitively."""
        if not username:
            return ""
        if f"HF_PASSWORD_{username}" in self.config:
            return self.config[f"HF_PASSWORD_{username}"].strip()
        upper_target = f"HF_PASSWORD_{username}".upper()
        for k, v in self.config.items():
            if k.upper() == upper_target and v:
                return v.strip()
        return ""

    def get_password_for_account(self, username: str) -> str:
        """
        Returns the encryption password designated for a specific account.
        Falls back to master self.password if the account has no custom key.
        """
        if not username:
            return self.password
        if username in self.accounts and self.accounts[username].get("password"):
            return self.accounts[username]["password"]
        custom = self._get_account_password_from_config(username)
        return custom if custom else self.password

    def get_password_for_repo(self, repo_id: str) -> str:
        """
        Returns the encryption password for a repository by extracting the owner username.
        e.g., 'HFSYNC/vault-xxxx' -> returns password for 'HFSYNC'.
        """
        if not repo_id:
            return self.password
        parts = repo_id.split("/", 1)
        if len(parts) == 2:
            owner = parts[0]
            return self.get_password_for_account(owner)
        return self.password

    def get_upload_account(self):
        """Returns designated account for uploading all new content (must have write permission)."""
        if self.primary_username and self.primary_username in self.accounts:
            acc = self.accounts[self.primary_username]
            if acc.get("enabled", True) and acc.get("can_write", True):
                return acc
        for user, acc in self.accounts.items():
            if acc.get("enabled", True) and acc.get("can_write", True):
                return acc
        if self.primary_username and self.primary_username in self.accounts:
            return self.accounts[self.primary_username]
        if self.accounts:
            first_user = next(iter(self.accounts))
            return self.accounts[first_user]
        return None

    def get_alternative_upload_account(self, current_username: str):
        """Returns another enabled account with verified write API when current_username is throttled."""
        for user, acc in self.accounts.items():
            if user != current_username and acc.get("enabled", True) and acc.get("can_write", True) and acc.get("api"):
                return acc
        return None

    def get_all_accounts(self):
        """Returns list of all configured Hugging Face accounts."""
        return list(self.accounts.values())

    def get_master_repo_id(self) -> str:
        """Returns the canonical Master Repository ID containing the unified vault_index.enc."""
        cfg_repo = self.config.get("REPO_ID", "").strip()
        if cfg_repo:
            return cfg_repo
        if self.primary_username:
            return f"{self.primary_username}/encrypted-media-vault"
        return "myprotjj/encrypted-media-vault"

    def get_master_api(self):
        """Returns the HfApi instance for the master repository."""
        master_id = self.get_master_repo_id()
        return self.get_api_for_repo(master_id)

    def fetch_repo_sidecars_via_git(self, repo_id: str, token: str, detailed: bool = False):
        """
        Method 3: Batch fetches all .meta.enc sidecars from a dataset repo in ~2 seconds
        using a shallow git clone without Git-LFS, completely bypassing API rate limits.
        If detailed=True, returns (records, stats_dict).
        If detailed=False, returns records dict.
        """
        import tempfile, subprocess, shutil, time
        git_exe = r"C:\Users\opc\tools\git\cmd\git.exe"
        if not os.path.exists(git_exe):
            git_exe = shutil.which("git") or "git"

        temp_dir = tempfile.mkdtemp(prefix="hfsync_meta_")
        records = {}
        stats = {
            "success": False,
            "clone_time": 0.0,
            "checkout_time": 0.0,
            "decrypt_time": 0.0,
            "sidecars_count": 0,
            "error": None
        }
        try:
            url = f"https://user:{token}@huggingface.co/datasets/{repo_id}"
            env = dict(os.environ, GIT_LFS_SKIP_SMUDGE="1")
            t0 = time.time()
            p1 = subprocess.run(
                [git_exe, "clone", "--depth", "1", "--no-checkout", url, temp_dir],
                env=env, capture_output=True, timeout=30
            )
            stats["clone_time"] = round(time.time() - t0, 2)
            if p1.returncode != 0:
                err_text = p1.stderr.decode("utf-8", errors="replace").strip()
                if token and token in err_text:
                    err_text = err_text.replace(token, "[REDACTED]")
                stats["error"] = f"Git clone failed (exit {p1.returncode}): {err_text}"
                return (records, stats) if detailed else records

            t_chk = time.time()
            subprocess.run(
                [git_exe, "-C", temp_dir, "checkout", "HEAD", "--", "*.meta.enc", "blobs/*.meta.enc", "blobs/*/*.meta.enc"],
                env=env, capture_output=True, timeout=15
            )
            stats["checkout_time"] = round(time.time() - t_chk, 2)

            t_dec = time.time()
            count = 0
            pw = self.get_password_for_repo(repo_id)
            for root, _, files in os.walk(temp_dir):
                for f in files:
                    if f.endswith(".meta.enc"):
                        fp = os.path.join(root, f)
                        try:
                            with open(fp, "rb") as ef:
                                dec = crypto_engine.decrypt_bytes(ef.read(), pw)
                                item = json.loads(dec.decode("utf-8"))
                                k = item.get("full_key")
                                if k:
                                    verifier = item.get("verifier")
                                    if verifier and not crypto_engine.verify_password(verifier, pw):
                                        continue  # Discard undecryptable with this account's key!
                                    records[k] = item
                                    count += 1
                        except Exception:
                            pass
            stats["decrypt_time"] = round(time.time() - t_dec, 2)
            stats["sidecars_count"] = count
            stats["success"] = True
        except Exception as e:
            stats["error"] = str(e)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
        return (records, stats) if detailed else records

    def batch_fetch_all_repos_parallel(self, repo_targets: list, max_workers: int = 4, progress_callback = None) -> tuple:
        """
        Fetches .meta.enc sidecars across multiple repositories in parallel using
        a bounded worker pool (default 4 workers) with staggered dispatch to prevent
        Hugging Face connection limits.
        Returns: (all_records: dict, errors: list, repo_stats: dict)
        """
        import time
        from concurrent.futures import ThreadPoolExecutor, as_completed

        all_records = {}
        errors = []
        repo_stats = {}
        if not repo_targets:
            return all_records, errors, repo_stats

        workers = max(1, min(len(repo_targets), max_workers))

        def _fetch_worker(target, worker_num):
            repo_id, token, user = target
            pw = self.get_password_for_account(user) if user else self.get_password_for_repo(repo_id)
            if progress_callback:
                progress_callback("worker_start", {
                    "repo_id": repo_id,
                    "user": user,
                    "worker_id": worker_num
                })
            t0 = time.time()
            try:
                # 1. Quick check: does repo have a local vault_index.enc?
                if pw and token:
                    try:
                        url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/vault_index.enc"
                        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=8)
                        if resp.status_code == 200:
                            dec = crypto_engine.decrypt_bytes(resp.content, pw)
                            try:
                                import zlib
                                recs = json.loads(zlib.decompress(dec).decode("utf-8"))
                            except Exception:
                                recs = json.loads(dec.decode("utf-8"))
                            if recs:
                                valid_recs = {}
                                for rk, rv in recs.items():
                                    ver = rv.get("verifier")
                                    if not ver or crypto_engine.verify_password(ver, pw):
                                        valid_recs[rk] = rv
                                if valid_recs:
                                    st = {
                                        "success": True,
                                        "clone_time": 0.0,
                                        "checkout_time": 0.0,
                                        "decrypt_time": 0.0,
                                        "sidecars_count": len(valid_recs),
                                        "error": None,
                                        "elapsed": round(time.time() - t0, 2),
                                        "source": "vault_index"
                                    }
                                    if progress_callback:
                                        progress_callback("worker_success", {
                                            "repo_id": repo_id,
                                            "user": user,
                                            "worker_id": worker_num,
                                            "count": len(valid_recs),
                                            "stats": st
                                        })
                                    return repo_id, valid_recs, None, st
                    except Exception:
                        pass

                # 2. Method 3: Parallel Git Batch Fetch for all .meta.enc sidecars
                recs, stats = self.fetch_repo_sidecars_via_git(repo_id, token, detailed=True)
                stats["elapsed"] = round(time.time() - t0, 2)
                stats["source"] = "git_shallow"
                if stats.get("error"):
                    if progress_callback:
                        progress_callback("worker_error", {
                            "repo_id": repo_id,
                            "user": user,
                            "worker_id": worker_num,
                            "error": stats["error"],
                            "stats": stats
                        })
                    return repo_id, {}, stats["error"], stats
                else:
                    if progress_callback:
                        progress_callback("worker_success", {
                            "repo_id": repo_id,
                            "user": user,
                            "worker_id": worker_num,
                            "count": len(recs),
                            "stats": stats
                        })
                    return repo_id, recs, None, stats
            except Exception as e:
                err_msg = str(e)
                st = {
                    "success": False,
                    "error": err_msg,
                    "elapsed": round(time.time() - t0, 2),
                    "sidecars_count": 0
                }
                if progress_callback:
                    progress_callback("worker_error", {
                        "repo_id": repo_id,
                        "user": user,
                        "worker_id": worker_num,
                        "error": err_msg,
                        "stats": st
                    })
                return repo_id, {}, err_msg, st

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = []
            for idx, t in enumerate(repo_targets, 1):
                w_num = ((idx - 1) % workers) + 1
                futures.append(pool.submit(_fetch_worker, t, w_num))
                time.sleep(0.08)  # Staggered dispatch avoids TLS thundering-herd

            for f in as_completed(futures):
                r_id, recs, err, st = f.result()
                repo_stats[r_id] = st
                if recs:
                    all_records.update(recs)
                if err:
                    errors.append({"repo_id": r_id, "error": err})

        return all_records, errors, repo_stats

    def can_write_to_repo(self, repo_id: str) -> bool:
        """
        Returns True if the designated account for repo_id has verified write/delete permissions.
        Returns False if token is read-only or permission has been revoked.
        """
        if not repo_id:
            acc = self.get_upload_account()
            return bool(acc and acc.get("can_write", True) and acc.get("enabled", True))
        parts = repo_id.split("/", 1)
        if len(parts) == 2:
            owner = parts[0]
            if owner in self.accounts:
                acc = self.accounts[owner]
                return bool(acc.get("enabled", True) and acc.get("can_write", True))
        acc = self.get_upload_account()
        return bool(acc and acc.get("can_write", True) and acc.get("enabled", True))

    def mark_user_write_disabled(self, username: str, reason: str = "403 Forbidden / No Write Access"):
        """
        Disables write/delete operations for an account at runtime to prevent repeated rejections.
        """
        if username in self.accounts:
            self.accounts[username]["can_write"] = False
            self.accounts[username]["role_desc"] = f"Disabled: {reason}"
            print(f"[!] Account Manager: Disabled write/delete operations for '{username}': {reason}")

    def get_account_usernames(self):
        return list(self.accounts.keys())

    def get_token_for_repo(self, repo_id: str) -> str:
        """
        Returns correct HF token for a repo based on owner username in repo_id.
        e.g., 'user2/Vault' -> returns user2's token.
        """
        if not repo_id:
            return self.upload_token
        parts = repo_id.split("/", 1)
        if len(parts) == 2:
            owner = parts[0]
            if owner in self.accounts:
                acc = self.accounts[owner]
                if acc.get("enabled", True):
                    return acc["token"]
        return self.upload_token

    def get_api_for_repo(self, repo_id: str) -> HfApi:
        """Returns correct HfApi instance for a repo."""
        if not repo_id:
            acc = self.get_upload_account()
            return acc["api"] if acc else None
        parts = repo_id.split("/", 1)
        if len(parts) == 2:
            owner = parts[0]
            if owner in self.accounts:
                acc = self.accounts[owner]
                if acc.get("enabled", True):
                    return acc["api"]
        acc = self.get_upload_account()
        return acc["api"] if acc else None

    def find_duplicate(self, media_hash: str, manifest: dict):
        """
        Checks whether a sparse media hash exists anywhere across
        all connected active accounts in upload_manifest.json.
        """
        if not media_hash or not manifest:
            return None

        # Check media_hashes table
        if "media_hashes" in manifest and media_hash in manifest["media_hashes"]:
            rec = manifest["media_hashes"][media_hash]
            owner = rec.get("account")
            if not owner or owner not in self.accounts or self.accounts[owner].get("enabled", True):
                return rec

        # Fallback check through files list
        for key, info in manifest.get("files", {}).items():
            if info.get("media_hash") == media_hash or info.get("content_hash") == media_hash:
                owner = info.get("account")
                if not owner or owner not in self.accounts or self.accounts[owner].get("enabled", True):
                    return info

        return None

    def scan_and_merge_all_accounts(self, manifest: dict, library_dir="jellyfin_library", proxy_host="127.0.0.1:8080", deep_scan: bool = False) -> dict:
        """
        Queries connected HF accounts, downloads encrypted indexes,
        registers media hashes, and merges all accounts into manifest.
        Fast Path: Loads Master Index from master_repo in <0.5s.
        """
        discovered_count = 0
        skipped_count = 0
        skipped_items = []
        master_repo = self.get_master_repo_id()
        tombstones = set(manifest.get("tombstones", {}).keys())

        # Fast Path 0: Check Master Index on master_repo (<0.5s)
        if self.password and not deep_scan:
            token = self.get_token_for_repo(master_repo)
            if token:
                try:
                    url = f"https://huggingface.co/datasets/{master_repo}/resolve/main/vault_index.enc"
                    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=12)
                    if resp.status_code == 200:
                        dec = crypto_engine.decrypt_bytes(resp.content, self.password)
                        try:
                            import zlib
                            master_records = json.loads(zlib.decompress(dec).decode("utf-8"))
                        except Exception:
                            master_records = json.loads(dec.decode("utf-8"))

                        if master_records:
                            files = manifest.setdefault("files", {})
                            media_hashes = manifest.setdefault("media_hashes", {})
                            for k, rec in master_records.items():
                                if k in tombstones:
                                    continue
                                if k not in files:
                                    files[k] = rec
                                    discovered_count += 1
                                else:
                                    r_ts = float(rec.get("updated_at") or 0.0)
                                    l_ts = float(files[k].get("updated_at") or 0.0)
                                    if r_ts > l_ts:
                                        files[k] = rec
                                m_hash = rec.get("media_hash")
                                if m_hash:
                                    media_hashes[m_hash] = rec

                            print(f"[*] Fast Path: Resynced from Master Index {master_repo} ({len(files)} total, +{discovered_count} new) in <0.5s")
                            return {
                                "discovered_count": discovered_count,
                                "skipped_count": 0,
                                "skipped_items": [],
                                "total_accounts": len(self.accounts)
                            }
                except Exception as e:
                    print(f"[!] Master index fast path fallback: {e}")

        # Deep Fallback or deep_scan=True: Parallel Git Batch Fetch across all accounts & repos
        repo_targets = []
        for user, acc in self.accounts.items():
            if not acc.get("enabled", True):
                continue
            api = acc["api"]
            token = acc["token"]
            try:
                datasets = list(api.list_datasets(author=user))
                for ds in datasets:
                    if ds.id != master_repo:
                        repo_targets.append((ds.id, token, user))
            except Exception as e:
                print(f"[!] Error listing datasets for user {user}: {e}")

        # Execute parallel sidecar fetch across all targets with 4 bounded workers
        parallel_records, fetch_errors, _ = self.batch_fetch_all_repos_parallel(repo_targets, max_workers=4)

        files = manifest.setdefault("files", {})
        media_hashes = manifest.setdefault("media_hashes", {})
        for k, rec in parallel_records.items():
            if k in tombstones:
                continue
            if k not in files:
                files[k] = rec
                discovered_count += 1
            else:
                r_ts = float(rec.get("updated_at") or 0.0)
                l_ts = float(files[k].get("updated_at") or 0.0)
                if r_ts > l_ts:
                    files[k] = rec
            m_hash = rec.get("media_hash")
            if m_hash:
                media_hashes[m_hash] = rec

        print(f"[*] Parallel Deep Resync complete: Discovered {discovered_count} new video(s) across {len(repo_targets)} repositories.")
        return {
            "discovered_count": discovered_count,
            "skipped_count": len(fetch_errors),
            "skipped_items": fetch_errors,
            "total_accounts": len(self.accounts)
        }

        # Legacy sequential loop fallback (retained for reference)
        for user, acc in self.accounts.items():
            if not acc.get("enabled", True):
                continue

            api = acc["api"]
            token = acc["token"]
            try:
                datasets = list(api.list_datasets(author=user))
            except Exception as e:
                print(f"[!] Error listing datasets for user {user}: {e}")
                continue

            for ds in datasets:
                repo_id = ds.id
                repo_records = {}
                vault_index_failed = False
                files_in_repo = None

                # 1. Fast Path: Try direct vault_index.enc download first (Instant 1-request restore, avoids listing 100k files)
                if self.password:
                    try:
                        url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/vault_index.enc"
                        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
                        if resp.status_code == 200:
                            dec = crypto_engine.decrypt_bytes(resp.content, self.password)
                            try:
                                import zlib
                                repo_records = json.loads(zlib.decompress(dec).decode("utf-8"))
                            except Exception:
                                repo_records = json.loads(dec.decode("utf-8"))
                    except Exception as e:
                        vault_index_failed = True

                # 2. Fallback: Only if vault_index was missing/corrupt, use Method 3 (Git Batch Clone)
                if not repo_records:
                    try:
                        repo_records = self.fetch_repo_sidecars_via_git(repo_id, token)
                    except Exception:
                        repo_records = {}

                    try:
                        files_in_repo = api.list_repo_files(repo_id=repo_id, repo_type="dataset")
                    except Exception:
                        files_in_repo = []

                    meta_files = [f for f in files_in_repo if f.endswith(".meta.enc")]
                    if meta_files:
                        from concurrent.futures import ThreadPoolExecutor
                        def _fetch_meta(mf):
                            try:
                                url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{mf}"
                                resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=8)
                                if resp.status_code == 200:
                                    dec = crypto_engine.decrypt_bytes(resp.content, self.password).decode("utf-8")
                                    item = json.loads(dec)
                                    k = item.get("full_key")
                                    if k:
                                        return (k, item, None)
                            except Exception:
                                pass
                            return (None, None, {
                                "key": mf,
                                "repo_id": repo_id,
                                "blob": mf,
                                "reason": "Failed to decrypt sidecar metadata with master key"
                            })

                        with ThreadPoolExecutor(max_workers=8) as ex:
                            results = ex.map(_fetch_meta, meta_files)
                            for k, item, err in results:
                                if k and item:
                                    repo_records[k] = item
                                elif err:
                                    skipped_count += 1
                                    skipped_items.append(err)

                    # If vault_index failed and no individual records could be decrypted,
                    # record all encrypted media blobs as skipped so the user gets accurate clues
                    if vault_index_failed and not repo_records:
                        enc_media_files = [f for f in files_in_repo if f.endswith(".enc") and f != "vault_index.enc" and not f.endswith(".meta.enc")]
                        for emf in enc_media_files:
                            skipped_count += 1
                            skipped_items.append({
                                "key": emf,
                                "repo_id": repo_id,
                                "blob": emf,
                                "reason": "Failed to decrypt vault index with current master encryption key"
                            })

                # 2.5 Ghost verification: Verify that all records correspond to actual blobs in HF
                if repo_records:
                    if files_in_repo is None:
                        try:
                            files_in_repo = api.list_repo_files(repo_id=repo_id, repo_type="dataset")
                        except Exception:
                            files_in_repo = []

                    files_set = set(files_in_repo)
                    valid_records = {}
                    ghost_found = False
                    for key, rec in repo_records.items():
                        blob = rec.get("blob_path")
                        rec_repo = rec.get("repo_id") or repo_id
                        if rec_repo == repo_id:
                            if blob and blob in files_set:
                                valid_records[key] = rec
                            else:
                                ghost_found = True
                                print(f"[!] Pruning ghost record {key} (blob {blob} missing in {repo_id})")
                        else:
                            # Item blobs reside in a different repo in the unified multi-repo index
                            valid_records[key] = rec

                    if ghost_found and self.can_write_to_repo(repo_id):
                        metadata_helper.sync_repo_manifest_to_hf(api, repo_id, valid_records, self.password)
                        if not valid_records and repo_id.split("/")[-1].startswith("vault-"):
                            rem_enc = [f for f in files_set if f.endswith(".enc")]
                            if not rem_enc:
                                try:
                                    api.delete_repo(repo_id=repo_id, repo_type="dataset")
                                    print(f"[*] Cleaned up empty vault repo {repo_id}")
                                except Exception:
                                    pass

                    repo_records = valid_records

                # 3. Merge records into manifest
                for key, rec in repo_records.items():
                    # Validate verifier with current password
                    verifier = rec.get("verifier")
                    if verifier and not crypto_engine.verify_password(verifier, self.password):
                        skipped_count += 1
                        skipped_items.append({
                            "key": key,
                            "repo_id": repo_id,
                            "blob": rec.get("blob_path"),
                            "reason": "Encryption key verifier mismatch"
                        })
                        continue

                    rec["account"] = user
                    rec["repo_id"] = repo_id

                    # Ensure file_names and aliases are populated
                    if "file_names" not in rec or not rec["file_names"]:
                        rec["file_names"] = [key]
                    if "aliases" not in rec or rec["aliases"] is None:
                        rec["aliases"] = []
                    else:
                        orig_n = Path(key).name
                        rec["aliases"] = [a for a in rec["aliases"] if a != orig_n and a.lower() != orig_n.lower()]

                    # Safely merge with existing local manifest record to protect local media specs
                    existing = manifest.get("files", {}).get(key, {})
                    if existing:
                        if existing.get("media_info") and not rec.get("media_info"):
                            rec["media_info"] = existing["media_info"]
                        if existing.get("extra_media_hashes") and not rec.get("extra_media_hashes"):
                            rec["extra_media_hashes"] = list(existing["extra_media_hashes"])
                        ex_meta = existing.get("metadata", {})
                        if ex_meta:
                            rec_meta = rec.get("metadata", {})
                            if not rec_meta:
                                rec["metadata"] = dict(ex_meta)
                            elif ex_meta.get("tags") and not rec_meta.get("tags"):
                                rec["metadata"]["tags"] = list(ex_meta["tags"])
                            if ex_meta.get("genres") and not rec_meta.get("genres"):
                                rec["metadata"]["genres"] = list(ex_meta["genres"])
                            if ex_meta.get("title") and (not rec_meta.get("title") or rec_meta["title"].startswith("blob_")):
                                rec["metadata"]["title"] = ex_meta["title"]

                    manifest["files"][key] = rec

                    media_hash = rec.get("media_hash") or rec.get("content_hash")
                    if media_hash:
                        if "media_hashes" not in manifest:
                            manifest["media_hashes"] = {}
                        manifest["media_hashes"][media_hash] = rec

                    # Register any extra media hashes (from duplicate merges) so they are never re-uploaded
                    for eh in rec.get("extra_media_hashes", []):
                        eh_val = eh.get("hash") if isinstance(eh, dict) else str(eh)
                        if eh_val:
                            if "media_hashes" not in manifest:
                                manifest["media_hashes"] = {}
                            manifest["media_hashes"][eh_val] = rec

                    if library_dir:
                        strm_path = Path(library_dir) / f"{key}.strm"
                        if not strm_path.exists():
                            strm_path.parent.mkdir(parents=True, exist_ok=True)
                            with open(strm_path, "w", encoding="utf-8") as sf:
                                sf.write(f"http://{proxy_host}/stream/{key}\n")
                        meta = rec.get("metadata") or metadata_helper.extract_default_metadata(key)
                        metadata_helper.write_nfo_files(library_dir, key, meta)

                    discovered_count += 1

        return {
            "total_accounts": len([a for a in self.accounts.values() if a.get("enabled", True)]),
            "accounts": [a["username"] for a in self.accounts.values() if a.get("enabled", True)],
            "discovered_count": discovered_count,
            "skipped_count": skipped_count,
            "skipped_items": skipped_items
        }

    # =========================================================================
    # Account Management Operations (Add, Toggle, Delete, Set Primary)
    # =========================================================================

    def anonymize_plaintext_repos(self, manifest: dict = None, delete_empty: bool = False) -> dict:
        """
        Scans all connected active HF accounts for plaintext repositories (e.g. 'LOLAH-VIBE')
        and safely renames them on Hugging Face to their deterministic encrypted names ('vault-<hash>').
        Updates manifest and index so streaming and indexing remain 100% intact.
        """
        renamed = []
        errors = []

        name_key = crypto_engine.derive_name_key(self.password) if self.password else None
        if not name_key:
            return {"renamed": [], "errors": ["No encryption password set."]}

        for user, acc in self.accounts.items():
            if not acc.get("enabled", True):
                continue

            api = acc["api"]
            try:
                datasets = list(api.list_datasets(author=user))
            except Exception as e:
                errors.append(f"Error listing datasets for {user}: {e}")
                continue

            for ds in datasets:
                repo_id = ds.id
                parts = repo_id.split("/", 1)
                if len(parts) != 2:
                    continue
                owner, repo_name = parts
                if owner != user:
                    continue

                if repo_name.startswith("vault-") or repo_name.lower() in ("encrypted-media-vault",):
                    continue

                obf_name = crypto_engine.get_obfuscated_repo_name(repo_name, name_key)
                new_repo_id = f"{user}/{obf_name}"

                try:
                    files = api.list_repo_files(repo_id, repo_type="dataset")
                    media_blobs = [f for f in files if f.endswith(".enc") and f != "vault_index.enc" and not f.endswith(".meta.enc")]

                    if not media_blobs and delete_empty:
                        api.delete_repo(repo_id=repo_id, repo_type="dataset")
                        renamed.append({"old_repo": repo_id, "action": "deleted_empty", "media_count": 0})
                    else:
                        api.move_repo(from_id=repo_id, to_id=new_repo_id, repo_type="dataset")
                        renamed.append({"old_repo": repo_id, "new_repo": new_repo_id, "action": "renamed", "media_count": len(media_blobs)})

                        if manifest and "files" in manifest:
                            for k, v in manifest["files"].items():
                                if v.get("repo_id") == repo_id:
                                    v["repo_id"] = new_repo_id

                except Exception as e:
                    errors.append(f"Error processing {repo_id}: {e}")

        return {"success": True, "renamed": renamed, "errors": errors}

    def purge_repo_commit_history(self, repo_id: str, commit_message: str = "Initial commit") -> dict:
        """
        Squashes Git commit history on Hugging Face to remove any historic plaintext commit messages.
        """
        api = self.get_api_for_repo(repo_id)
        try:
            commits_before = len(list(api.list_repo_commits(repo_id=repo_id, repo_type="dataset")))
            api.super_squash_history(repo_id=repo_id, repo_type="dataset", commit_message=commit_message)
            return {"success": True, "repo_id": repo_id, "commits_before": commits_before, "commits_after": 1}
        except Exception as e:
            return {"success": False, "repo_id": repo_id, "error": str(e)}

    def purge_all_repo_commit_histories(self, commit_message: str = "Initial commit") -> dict:
        """
        Squashes Git commit history across ALL repositories on Hugging Face.
        """
        results = []
        errors = []
        for user, info in self.accounts.items():
            api = info["api"]
            try:
                datasets = list(api.list_datasets(author=user))
            except Exception as e:
                errors.append(f"Error listing datasets for {user}: {e}")
                continue
            for ds in datasets:
                res = self.purge_repo_commit_history(ds.id, commit_message)
                if res.get("success"):
                    results.append(res)
                else:
                    errors.append(f"Error squashing {ds.id}: {res.get('error')}")
        return {"success": len(errors) == 0, "squashed": results, "errors": errors}

    def verify_account_encryption_key(self, username: str, token: str, test_password: str) -> dict:
        """
        Instant verification: Checks if test_password can decrypt files in username's Hugging Face repositories.
        Returns:
            {"valid": True/False, "tested_repo": str, "message": str, "error": str}
        """
        if not test_password:
            test_password = self.password
        if not test_password:
            return {"valid": False, "error": "No encryption password provided and no master password exists."}

        try:
            api = HfApi(token=token)
            datasets = list(api.list_datasets(author=username))
        except Exception as e:
            return {"valid": False, "error": f"Failed to list datasets for '{username}': {e}"}

        master_repo = self.get_master_repo_id()
        vault_datasets = [ds for ds in datasets if ds.id != master_repo]

        if not vault_datasets:
            return {
                "valid": True,
                "tested_repo": None,
                "message": f"Account '{username}' has no existing vault repositories. New uploads will use this encryption key."
            }

        # 1. Search for repositories containing .meta.enc sidecars (ground truth of encrypted media)
        for ds in vault_datasets:
            repo_id = ds.id
            try:
                repo_files = list(api.list_repo_files(repo_id=repo_id, repo_type="dataset"))
                meta_files = [f for f in repo_files if f.endswith(".meta.enc")]
                if meta_files:
                    sample_file = meta_files[0]
                    url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{sample_file}"
                    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=8)
                    if resp.status_code == 200 and resp.content:
                        try:
                            dec = crypto_engine.decrypt_bytes(resp.content, test_password)
                            item = json.loads(dec.decode("utf-8"))
                            return {
                                "valid": True,
                                "tested_repo": repo_id,
                                "message": f"Encryption key verified! Decrypted media metadata in '{repo_id}'."
                            }
                        except Exception:
                            return {
                                "valid": False,
                                "tested_repo": repo_id,
                                "error": f"Invalid encryption key: Unable to decrypt metadata sidecar '{sample_file}' in repository '{repo_id}'."
                            }
            except Exception:
                pass

        # 2. Fallback: check for vault_index.enc if no sidecars were found
        for ds in vault_datasets:
            repo_id = ds.id
            try:
                url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/vault_index.enc"
                resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=8)
                if resp.status_code == 200 and resp.content:
                    try:
                        dec = crypto_engine.decrypt_bytes(resp.content, test_password)
                        import zlib
                        try:
                            recs = json.loads(zlib.decompress(dec).decode("utf-8"))
                        except Exception:
                            recs = json.loads(dec.decode("utf-8"))
                        if isinstance(recs, dict) and recs:
                            return {
                                "valid": True,
                                "tested_repo": repo_id,
                                "message": f"Encryption key verified! Decrypted index in '{repo_id}'."
                            }
                    except Exception:
                        pass
            except Exception:
                pass

        if not tested_any:
            return {
                "valid": True,
                "tested_repo": None,
                "message": f"No encrypted media found in existing repositories for '{username}'. Key accepted."
            }

        return {
            "valid": True,
            "tested_repo": None,
            "message": f"Account '{username}' verified successfully."
        }

    def add_account(self, token: str, is_upload: bool = False, password: str = None) -> dict:
        """
        Validates token with Hugging Face, instantly verifies encryption key against cloud files,
        saves to env.txt, and connects.
        """
        token = token.strip()
        if not token:
            raise ValueError("Token cannot be empty.")

        api = HfApi(token=token)
        try:
            user_info = api.whoami()
            username = user_info["name"]
        except Exception as e:
            raise ValueError(f"Hugging Face authorization failed: {e}")

        # Instant verification of encryption key
        effective_pw = password.strip() if (password and password.strip()) else self.password
        verify_res = self.verify_account_encryption_key(username, token, effective_pw)
        if not verify_res.get("valid"):
            raise ValueError(verify_res.get("error", "Encryption key verification failed."))

        # Current extra tokens
        extra_tokens_raw = self.config.get("HF_EXTRA_TOKENS", "")
        extra_tokens = [t.strip() for t in re.split(r"[,;\s]+", extra_tokens_raw) if t.strip()]

        updates = {}
        if is_upload:
            old_upload = self.upload_token
            if old_upload and old_upload != token and old_upload not in extra_tokens:
                extra_tokens.append(old_upload)
            if token in extra_tokens:
                extra_tokens.remove(token)

            updates["HF_UPLOAD_TOKEN"] = token
            updates["HF_TOKEN"] = token
            updates["HF_EXTRA_TOKENS"] = ", ".join(extra_tokens)
        else:
            if token != self.upload_token and token not in extra_tokens:
                extra_tokens.append(token)
            updates["HF_EXTRA_TOKENS"] = ", ".join(extra_tokens)

        # Save account-specific password if provided and distinct from master
        key_name = f"HF_PASSWORD_{username}"
        if password and password.strip() and password.strip() != self.password:
            updates[key_name] = password.strip()

        # Un-disable if it was previously disabled
        if username in self.disabled_users:
            self.disabled_users.remove(username)
            updates["HF_DISABLED_USERS"] = ", ".join(sorted(self.disabled_users))

        save_env_config(updates, self.env_path)
        self.reload()

        return {
            "username": username,
            "is_upload": is_upload,
            "enabled": True,
            "has_custom_key": bool(password and password.strip() and password.strip() != self.password),
            "verification": verify_res,
            "message": f"Successfully connected Hugging Face account '{username}'. {verify_res.get('message', '')}"
        }

    def update_account_password(self, username: str, new_password: str) -> dict:
        """
        Updates the encryption key for an account after verifying it against cloud repositories.
        Discards any files in the local manifest that cannot be decrypted with the new key.
        """
        if username not in self.accounts:
            raise ValueError(f"Account '{username}' is not configured.")

        token = self.accounts[username]["token"]
        new_password = (new_password or "").strip()
        effective_pw = new_password if new_password else self.password

        # Instant verification against existing repositories on Hugging Face
        verify_res = self.verify_account_encryption_key(username, token, effective_pw)
        if not verify_res.get("valid"):
            raise ValueError(verify_res.get("error", "Failed to verify encryption key against cloud repositories."))

        # Update env config
        key_name = f"HF_PASSWORD_{username}"
        updates = {key_name: new_password}
        save_env_config(updates, self.env_path)
        self.reload()

        # Prune / discard any files in local manifest for this account that cannot decrypt with the key
        manifest_path = "upload_manifest.json"
        discarded_count = 0
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    manifest = json.load(f)
                files = manifest.get("files", {})
                keys_to_discard = []
                for k, v in files.items():
                    owner = v.get("account") or (v.get("repo_id", "").split("/")[0] if "/" in v.get("repo_id", "") else None)
                    if owner == username:
                        verifier = v.get("verifier")
                        if verifier and not crypto_engine.verify_password(verifier, effective_pw):
                            keys_to_discard.append(k)

                for k in keys_to_discard:
                    files.pop(k, None)
                    discarded_count += 1
                    strm_p = Path("jellyfin_library") / f"{k}.strm"
                    if strm_p.exists():
                        try:
                            strm_p.unlink()
                        except Exception:
                            pass

                if discarded_count > 0:
                    with open(manifest_path, "w", encoding="utf-8") as f:
                        json.dump(manifest, f, indent=2)
            except Exception as pe:
                print(f"[!] Warning during manifest prune for {username}: {pe}")

        return {
            "success": True,
            "username": username,
            "has_custom_key": bool(new_password and new_password != self.password),
            "discarded_count": discarded_count,
            "verification": verify_res,
            "message": f"Successfully updated encryption key for '{username}'. {verify_res.get('message', '')} ({discarded_count} undecryptable file(s) discarded)."
        }

    def toggle_account(self, username: str, enabled: bool = None) -> dict:
        """
        Toggles enable/disable state of a secondary account.
        """
        if username == self.primary_username:
            raise ValueError("The Primary Upload Account cannot be disabled.")

        if username not in self.accounts:
            raise ValueError(f"Account '{username}' is not configured.")

        curr_enabled = self.accounts[username].get("enabled", True)
        new_state = (not curr_enabled) if enabled is None else bool(enabled)

        if new_state:
            self.disabled_users.discard(username)
        else:
            self.disabled_users.add(username)

        save_env_config({"HF_DISABLED_USERS": ", ".join(sorted(self.disabled_users))}, self.env_path)
        self.reload()

        return {
            "username": username,
            "enabled": new_state,
            "message": f"Account '{username}' is now {'active' if new_state else 'disabled'}."
        }

    def set_primary_account(self, username: str) -> dict:
        """
        Swaps designated primary upload account to another connected user.
        """
        if username not in self.accounts:
            raise ValueError(f"Account '{username}' is not found.")

        if username == self.primary_username:
            return {"username": username, "message": "Already the primary upload account."}

        target_token = self.accounts[username]["token"]
        old_primary_token = self.upload_token

        extra_tokens_raw = self.config.get("HF_EXTRA_TOKENS", "")
        extra_tokens = [t.strip() for t in re.split(r"[,;\s]+", extra_tokens_raw) if t.strip()]

        if target_token in extra_tokens:
            extra_tokens.remove(target_token)

        if old_primary_token and old_primary_token not in extra_tokens:
            extra_tokens.append(old_primary_token)

        self.disabled_users.discard(username)

        updates = {
            "HF_UPLOAD_TOKEN": target_token,
            "HF_TOKEN": target_token,
            "HF_EXTRA_TOKENS": ", ".join(extra_tokens),
            "HF_DISABLED_USERS": ", ".join(sorted(self.disabled_users))
        }

        save_env_config(updates, self.env_path)
        self.reload()

        return {
            "username": username,
            "message": f"'{username}' is now the primary upload account (👑)."
        }

    def delete_account(self, username: str, remove_manifest: bool = False, manifest: dict = None, library_dir: str = "jellyfin_library") -> dict:
        """
        Removes an account from configuration and optionally purges its manifest entries.
        """
        if username == self.primary_username:
            raise ValueError("Cannot delete the Primary Upload Account. Set another account as primary first.")

        if username not in self.accounts:
            raise ValueError(f"Account '{username}' not found.")

        target_token = self.accounts[username]["token"]

        extra_tokens_raw = self.config.get("HF_EXTRA_TOKENS", "")
        extra_tokens = [t.strip() for t in re.split(r"[,;\s]+", extra_tokens_raw) if t.strip() and t.strip() != target_token]

        self.disabled_users.discard(username)

        updates = {
            "HF_EXTRA_TOKENS": ", ".join(extra_tokens),
            "HF_DISABLED_USERS": ", ".join(sorted(self.disabled_users)),
            f"HF_PASSWORD_{username}": ""
        }

        # Purge local manifest/strm if requested
        purged_count = 0
        if remove_manifest and manifest and "files" in manifest:
            keys_to_del = [k for k, v in manifest["files"].items() if v.get("account") == username or v.get("repo_id", "").startswith(f"{username}/")]
            for k in keys_to_del:
                manifest["files"].pop(k, None)
                strm_p = Path(library_dir) / f"{k}.strm"
                if strm_p.exists():
                    try:
                        strm_p.unlink()
                    except Exception:
                        pass
                metadata_helper.delete_nfo_files(library_dir, k)
                purged_count += 1

        save_env_config(updates, self.env_path)
        self.reload()

        return {
            "username": username,
            "purged_files": purged_count,
            "message": f"Account '{username}' deleted successfully."
        }

    def get_accounts_summary(self, manifest: dict = None) -> list:
        """
        Returns rich summary of all connected accounts for the Web UI.
        """
        summary = []
        files_by_acc = {}
        size_by_acc = {}

        if manifest and "files" in manifest:
            for k, f in manifest["files"].items():
                owner = f.get("account") or (f.get("repo_id", "").split("/")[0] if "/" in f.get("repo_id", "") else self.primary_username)
                files_by_acc[owner] = files_by_acc.get(owner, 0) + 1
                size_by_acc[owner] = size_by_acc.get(owner, 0) + f.get("orig_size", 0)

        for user, acc in self.accounts.items():
            tok = acc["token"]
            masked = f"{tok[:7]}...{tok[-4:]}" if len(tok) > 12 else "••••••••"
            v_count = files_by_acc.get(user, 0)
            bytes_tot = size_by_acc.get(user, 0)
            size_mb = bytes_tot / (1024 * 1024)
            size_str = f"{size_mb:.2f} MB" if size_mb < 1024 else f"{size_mb / 1024:.2f} GB"
            has_custom = acc.get("has_custom_key", False)

            summary.append({
                "username": user,
                "is_primary": acc["is_upload"],
                "enabled": acc.get("enabled", True),
                "can_write": acc.get("can_write", True),
                "role_desc": acc.get("role_desc", "Write Access"),
                "has_custom_key": has_custom,
                "key_status": "Custom Key" if has_custom else "Master Key",
                "token_masked": masked,
                "video_count": v_count,
                "total_bytes": bytes_tot,
                "size_str": size_str
            })

        return summary
