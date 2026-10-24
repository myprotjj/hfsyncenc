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
                self.primary_username = user
                self.upload_token = upload_token
                self.accounts[user] = {
                    "username": user,
                    "token": upload_token,
                    "api": api,
                    "is_upload": True,
                    "enabled": True  # Primary is always active
                }
                self.token_to_user[upload_token] = user
                self.disabled_users.discard(user)
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
                self.accounts[user] = {
                    "username": user,
                    "token": tok,
                    "api": api,
                    "is_upload": False,
                    "enabled": is_enabled
                }
                self.token_to_user[tok] = user
            except Exception as e:
                print(f"[!] Warning: Extra HF token auth failed ({tok[:10]}...): {e}")

    def get_upload_account(self):
        """Returns designated account for uploading all new content."""
        if self.primary_username and self.primary_username in self.accounts:
            return self.accounts[self.primary_username]
        if self.accounts:
            first_user = next(iter(self.accounts))
            return self.accounts[first_user]
        return None

    def get_alternative_upload_account(self, current_username: str):
        """Returns another enabled account with valid write API when current_username is throttled."""
        for user, acc in self.accounts.items():
            if user != current_username and acc.get("enabled", True) and acc.get("api"):
                return acc
        return None

    def get_all_accounts(self):
        """Returns list of all configured Hugging Face accounts."""
        return list(self.accounts.values())

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

    def scan_and_merge_all_accounts(self, manifest: dict, library_dir="jellyfin_library", proxy_host="127.0.0.1:8080") -> dict:
        """
        Queries all connected active HF accounts, downloads encrypted indexes,
        registers media hashes, and merges all accounts into manifest.
        """
        discovered_count = 0
        skipped_count = 0
        skipped_items = []

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

                # 1. Fast Path: Try direct vault_index.enc download first (Instant 1-request restore, avoids listing 100k files)
                if self.password:
                    try:
                        url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/vault_index.enc"
                        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
                        if resp.status_code == 200:
                            dec = crypto_engine.decrypt_bytes(resp.content, self.password).decode("utf-8")
                            repo_records = json.loads(dec)
                    except Exception as e:
                        vault_index_failed = True

                # 2. Fallback: Only if vault_index was missing/corrupt, list repo files for individual .meta.enc sidecars
                if not repo_records:
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

    def add_account(self, token: str, is_upload: bool = False) -> dict:
        """
        Validates token with Hugging Face, saves to env.txt, and connects.
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
            "message": f"Successfully connected Hugging Face account '{username}'"
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
            "HF_DISABLED_USERS": ", ".join(sorted(self.disabled_users))
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

            summary.append({
                "username": user,
                "is_primary": acc["is_upload"],
                "enabled": acc.get("enabled", True),
                "token_masked": masked,
                "video_count": v_count,
                "total_bytes": bytes_tot,
                "size_str": size_str
            })

        return summary
