import os
import sys
import time
import json
import logging

logger = logging.getLogger("stream_proxy")
import shutil
import mimetypes
mimetypes.add_type("video/mp4", ".encname")
import re
import requests
import threading
from urllib.parse import urlparse, unquote, parse_qs, quote
from core import storyboard_helper
from core import staging_manager
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from huggingface_hub import HfApi
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from core import crypto_engine
from core import commit_helper
from core import metadata_helper
from core import account_manager
from core import activity_tracker
import companion_helper
import cloud_watcher
from core import screens_helper
from core.encrypted_stream_cache import stream_cache

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

FILE_CACHE = {}


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


_MANIFEST_PROXY_CACHE = None
_MANIFEST_PROXY_MTIME = 0


def load_manifest(manifest_path="upload_manifest.json"):
    global _MANIFEST_PROXY_CACHE, _MANIFEST_PROXY_MTIME
    if os.path.exists(manifest_path):
        try:
            mtime = os.path.getmtime(manifest_path)
            if _MANIFEST_PROXY_CACHE is not None and mtime == _MANIFEST_PROXY_MTIME:
                return _MANIFEST_PROXY_CACHE
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "files" in data:
                    _MANIFEST_PROXY_CACHE = data
                else:
                    _MANIFEST_PROXY_CACHE = {"files": data, "media_hashes": {}}
                _MANIFEST_PROXY_MTIME = mtime
                return _MANIFEST_PROXY_CACHE
        except Exception:
            if _MANIFEST_PROXY_CACHE is not None:
                return _MANIFEST_PROXY_CACHE
            return {"files": {}, "media_hashes": {}}
    return {"files": {}, "media_hashes": {}}


def save_manifest(manifest, manifest_path="upload_manifest.json"):
    global _MANIFEST_PROXY_CACHE, _MANIFEST_PROXY_MTIME
    _MANIFEST_PROXY_CACHE = None
    _MANIFEST_PROXY_MTIME = 0
    temp_path = f"{manifest_path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    # Robust replacement on Windows to survive concurrent readers
    replaced = False
    for _ in range(15):
        try:
            os.replace(temp_path, manifest_path)
            replaced = True
            break
        except (PermissionError, OSError):
            time.sleep(0.04)
    if not replaced:
        import shutil
        shutil.copyfile(temp_path, manifest_path)
        try: os.remove(temp_path)
        except Exception: pass


def remove_empty_folders(root_dir):
    for root, dirs, files in os.walk(root_dir, topdown=False):
        if root != str(root_dir):
            try:
                if not os.listdir(root):
                    os.rmdir(root)
            except Exception:
                pass


def format_duration(sec) -> str:
    if not sec or sec <= 0:
        return ""
    sec = int(round(sec))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class InteractiveJellyfinStreamHandler(BaseHTTPRequestHandler):
    acc_mgr = None
    hf_username = None
    default_repo = "encrypted-media-vault"
    token = None
    password = None
    name_key = None
    hf_session = None

    def log_message(self, format, *args):
        sys.stdout.write(f"[{self.log_date_time_string()}] {self.command} {self.path} - {args[1] if len(args) > 1 else ''}\n")


    def sync_master_index_to_cloud(self, async_mode: bool = True):
        """Syncs the canonical master vault_index.enc to the Master Repository."""
        def _do_sync():
            try:
                master_repo = self.acc_mgr.get_master_repo_id() if hasattr(self, "acc_mgr") and self.acc_mgr else (os.getenv("REPO_ID") or f"{self.hf_username}/{self.default_repo}")
                api = self.acc_mgr.get_api_for_repo(master_repo) if hasattr(self, "acc_mgr") and self.acc_mgr else HfApi(token=self.token)
                manifest = load_manifest()
                master_pw = self.acc_mgr.get_password_for_repo(master_repo) if hasattr(self, "acc_mgr") and self.acc_mgr else self.password
                metadata_helper.sync_master_manifest_to_hf(api, master_repo, manifest, master_pw)
            except Exception as e:
                logger.warning("Error updating master index: %s", e)

        if async_mode:
            import threading
            threading.Thread(target=_do_sync, daemon=True).start()
        else:
            _do_sync()

    def read_json_body(self) -> dict:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length <= 0:
            return {}
        try:
            body = self.rfile.read(content_length).decode("utf-8")
            return json.loads(body) if body else {}
        except Exception:
            return {}
    def resolve_remote_blob(self, path_after_stream: str):
        manifest = load_manifest()
        clean_key = path_after_stream.strip("/").replace("\\", "/")
        if clean_key in manifest.get("tombstones", {}):
            return None, None

        if clean_key in manifest.get("files", {}):
            info = manifest["files"][clean_key]
            repo_id = info.get("repo_id", f"{self.hf_username}/{self.default_repo}")
            blob_path = info.get("blob_path") or info.get("remote_path")
            return repo_id, blob_path

        clean_base = Path(clean_key).name
        for k, info in manifest.get("files", {}).items():
            file_names = info.get("file_names") or []
            aliases = info.get("aliases") or []
            if clean_key in file_names or clean_base in aliases or any(Path(fn).name == clean_base for fn in file_names):
                repo_id = info.get("repo_id", f"{self.hf_username}/{self.default_repo}")
                blob_path = info.get("blob_path") or info.get("remote_path")
                return repo_id, blob_path

        parts = clean_key.split("/", 1)
        if len(parts) == 2:
            raw_folder, rel_in_repo = parts
            hf_repo_name = crypto_engine.get_obfuscated_repo_name(raw_folder, self.name_key)
            repo_id = f"{self.hf_username}/{hf_repo_name}"
        else:
            rel_in_repo = parts[0]
            repo_id = f"{self.hf_username}/{self.default_repo}"

        obfuscated_blob = crypto_engine.get_obfuscated_blob_path(rel_in_repo, self.name_key)
        return repo_id, obfuscated_blob

    def resolve_key_from_id_or_path(self, raw_id: str) -> str:
        clean = unquote(raw_id).strip("/").replace("\\", "/")
        manifest = load_manifest()
        files = manifest.get("files", {})
        # 1. Exact key match
        if clean in files:
            return clean
        # 2. Fast direct dictionary match (safe_id, media_hash, aliases, file_names)
        for k, v in files.items():
            if v.get("safe_id") == clean:
                return k
            if isinstance(v.get("storyboard"), dict) and v["storyboard"].get("safe_id") == clean:
                return k
            m_hash = v.get("media_hash") or v.get("content_hash")
            if m_hash and (clean == m_hash or clean == m_hash[:24]):
                return k
            if clean in (v.get("file_names") or []) or clean in (v.get("aliases") or []):
                return k
        # 3. Fallback: match via helper
        for k, v in files.items():
            if storyboard_helper.get_safe_id(k) == clean:
                return k
            for fn in (v.get("file_names") or []):
                if storyboard_helper.get_safe_id(fn) == clean:
                    return k
        return clean

    def get_file_metadata(self, path_after_stream: str, repo_id: str = None, blob_path: str = None, custom_content_type: str = None):
        is_custom_blob = bool(repo_id and blob_path)
        clean_key = path_after_stream.strip("/").replace(b"\\".decode("ascii"), "/")
        if is_custom_blob:
            cache_key = f"{repo_id}:{blob_path}"
            if cache_key in FILE_CACHE:
                return FILE_CACHE[cache_key]
            if blob_path in FILE_CACHE:
                return FILE_CACHE[blob_path]
        else:
            if clean_key in FILE_CACHE:
                return FILE_CACHE[clean_key]
            if path_after_stream in FILE_CACHE:
                return FILE_CACHE[path_after_stream]
            repo_id, blob_path = self.resolve_remote_blob(path_after_stream)
            cache_key = f"{repo_id}:{blob_path}"
            if cache_key in FILE_CACHE:
                return FILE_CACHE[cache_key]

        manifest = load_manifest()
        file_info = manifest.get("files", {}).get(clean_key)
        if not file_info:
            for k, v in manifest.get("files", {}).items():
                if clean_key in (v.get("file_names") or []) or Path(clean_key).name in (v.get("aliases") or []):
                    file_info = v
                    break

        safe_id = file_info.get("safe_id") if file_info else None
        if not safe_id:
            safe_id = storyboard_helper.get_safe_id(clean_key)

        is_audio = bool(custom_content_type and "audio" in custom_content_type)
        cache_id = f"{safe_id}_audio" if is_audio else safe_id

        hf_url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{blob_path}"
        repo_token = self.acc_mgr.get_token_for_repo(repo_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else self.token

        # Check local encrypted sparse cache for header
        if stream_cache.has_cached_header(cache_id):
            hdr_bytes = stream_cache.read_header_bytes(cache_id)
            if hdr_bytes and len(hdr_bytes) == crypto_engine.HEADER_SIZE:
                try:
                    hdr = crypto_engine.read_header(hdr_bytes)
                    candidate_keys = self.acc_mgr.get_candidate_keys_for_repo(repo_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else [self.password]
                    verifier = file_info.get("verifier") if file_info else None
                    working_pw = crypto_engine.find_working_key_for_verifier(candidate_keys, verifier) if verifier else None
                    if not working_pw:
                        working_pw = candidate_keys[0] if candidate_keys else self.password
                    key = crypto_engine.derive_key(working_pw, hdr["salt"])
                    aesgcm = AESGCM(key)
                    metadata = {
                        "header": hdr,
                        "key": key,
                        "aesgcm": aesgcm,
                        "password": working_pw,
                        "url": hf_url,
                        "repo_id": repo_id,
                        "repo_token": repo_token,
                        "cache_id": cache_id
                    }
                    FILE_CACHE[cache_key] = metadata
                    if blob_path:
                        FILE_CACHE[blob_path] = metadata
                    if not is_custom_blob:
                        FILE_CACHE[path_after_stream] = metadata
                        FILE_CACHE[clean_key] = metadata
                    return metadata
                except Exception:
                    pass

        headers = {}
        if repo_token:
            headers["Authorization"] = f"Bearer {repo_token}"
        headers["Range"] = f"bytes=0-{crypto_engine.HEADER_SIZE - 1}"

        try:
            resp = self.hf_session.get(hf_url, headers=headers, allow_redirects=True, timeout=15)
        except Exception as e:
            print(f"[!] Network error fetching from {hf_url}: {e}")
            return None

        if resp.status_code not in (200, 206):
            resp.close()
            fallback_url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{path_after_stream.split('/', 1)[-1]}.enc"
            try:
                resp = self.hf_session.get(fallback_url, headers=headers, allow_redirects=True, timeout=15)
                if resp.status_code in (200, 206):
                    hf_url = fallback_url
                else:
                    resp.close()
                    return None
            except Exception:
                return None

        try:
            content_bytes = resp.content
            resp.close()
            hdr = crypto_engine.read_header(content_bytes)
            candidate_keys = self.acc_mgr.get_candidate_keys_for_repo(repo_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else [self.password]
            verifier = file_info.get("verifier") if file_info else None
            working_pw = crypto_engine.find_working_key_for_verifier(candidate_keys, verifier) if verifier else None
            if not working_pw:
                working_pw = candidate_keys[0] if candidate_keys else self.password
            key = crypto_engine.derive_key(working_pw, hdr["salt"])
            aesgcm = AESGCM(key)
            if stream_cache.is_pinned_offline(cache_id) or stream_cache.is_downloading_offline(cache_id):
                stream_cache.write_header_bytes(cache_id, resp.content, hdr["total_size"], hdr["chunk_size"])
        except Exception as e:
            print(f"[!] Header parsing error: {e}")
            return None

        metadata = {
            "header": hdr,
            "key": key,
            "aesgcm": aesgcm,
            "password": working_pw,
            "url": hf_url,
            "repo_id": repo_id,
            "repo_token": repo_token,
            "cache_id": cache_id
        }
        FILE_CACHE[cache_key] = metadata
        if blob_path:
            FILE_CACHE[blob_path] = metadata
        if not is_custom_blob:
            FILE_CACHE[path_after_stream] = metadata
            FILE_CACHE[clean_key] = metadata
        return metadata

    def do_HEAD(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path.startswith("/stream/audio/"):
            path_after_stream = path[len("/stream/audio/"):]
            clean_key = path_after_stream.strip("/").replace("\\", "/")
            manifest = load_manifest()
            file_info = manifest.get("files", {}).get(clean_key)
            if not file_info:
                for k, v in manifest.get("files", {}).items():
                    if clean_key in (v.get("file_names") or []) or Path(clean_key).name in (v.get("aliases") or []):
                        file_info = v
                        clean_key = k
                        break
            audio_info = file_info.get("audio") if file_info else None
            if audio_info and audio_info.get("blob_path"):
                meta = self.get_file_metadata(clean_key, repo_id=audio_info.get("repo_id"), blob_path=audio_info.get("blob_path"))
            else:
                meta = self.get_file_metadata(clean_key)
            if not meta:
                self.send_error(404, "Audio not found")
                return
            total_size = meta["header"]["total_size"]
            self.send_response(200)
            self.send_header("Content-Type", "audio/mp4")
            self.send_header("Content-Disposition", f'inline; filename="{Path(clean_key).stem}.m4a"')
            self.send_header("Content-Length", str(total_size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
        elif path.startswith("/stream/play/"):
            raw_id = path[len("/stream/play/"):].strip("/")
            resolved_key = self.resolve_key_from_id_or_path(raw_id)
            meta = self.get_file_metadata(resolved_key)
            if not meta:
                self.send_error(404, "File not found")
                return

            total_size = meta["header"]["total_size"]
            mime, _ = mimetypes.guess_type(resolved_key)
            content_type = mime or "video/mp4"

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", "inline")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(total_size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
        elif path.startswith("/stream/"):
            path_after_stream = path[len("/stream/"):]
            meta = self.get_file_metadata(path_after_stream)
            if not meta:
                self.send_error(404, "File not found")
                return

            total_size = meta["header"]["total_size"]
            mime, _ = mimetypes.guess_type(path_after_stream)
            content_type = mime or "video/mp4"

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Disposition", "inline")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(total_size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
        else:
            self.send_response(200)
            self.end_headers()

    def do_POST(self):
        cloud_watcher.notify_client_active()
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/api/delete":
            self.handle_delete()
        elif path == "/api/delete-folder":
            self.handle_delete_folder()
        elif path == "/api/metadata":
            self.handle_update_metadata()
        elif path == "/api/video-tag":
            self.handle_video_tag_action()
        elif path == "/api/folder-tags":
            self.handle_folder_tags()
        elif path == "/api/sync-hf":
            self.handle_sync_hf()
        elif path in ("/api/resync-stream", "/api/recheck-cloud"):
            self.handle_resync_stream()
        elif path == "/api/restore-hf":
            self.handle_restore_hf()
        elif path == "/api/jellyfin_sync/toggle":
            self.handle_toggle_jellyfin_sync()
        elif path == "/api/cloud_sync/trigger":
            self.handle_cloud_sync_trigger()
        elif path == "/api/delete-skipped":
            self.handle_delete_skipped()
        elif path == "/api/staging/push":
            self.handle_staging_push()
        elif path == "/api/staging/push-all":
            self.handle_staging_push_all()
        elif path == "/api/staging/pause":
            self.handle_staging_pause()
        elif path == "/api/staging/pause-staging":
            self.handle_staging_pause_staging()
        elif path == "/api/staging/resume":
            self.handle_staging_resume()
        elif path == "/api/staging/resume-staging":
            self.handle_staging_resume_staging()
        elif path == "/api/staging/cancel-queued":
            self.handle_staging_cancel_queued()
        elif path == "/api/staging/dismiss":
            self.handle_staging_dismiss()
        elif path == "/api/staging/discard":
            self.handle_staging_discard()
        elif path == "/api/staging/revert":
            self.handle_staging_revert()
        elif path == "/api/staging/unignore":
            self.handle_staging_unignore()
        elif path == "/api/accounts/add":
            self.handle_accounts_add()
        elif path == "/api/accounts/update-key":
            self.handle_accounts_update_key()
        elif path == "/api/accounts/toggle":
            self.handle_accounts_toggle()
        elif path == "/api/accounts/set-primary":
            self.handle_accounts_set_primary()
        elif path == "/api/accounts/delete":
            self.handle_accounts_delete()
        elif path == "/api/undecryptable-files/delete":
            self.handle_delete_undecryptable_files()
        elif path == "/api/undecryptable-files/delete-repo":
            self.handle_delete_undecryptable_repo()
        elif path == "/api/undecryptable-files/auto-cleanup":
            self.handle_auto_cleanup_undecryptable_repos()
        elif path == "/api/aliases/remove":
            self.handle_remove_alias()
        elif path == "/api/aliases/add":
            self.handle_add_alias()
        elif path == "/api/subtitles/upload":
            self.handle_subtitles_upload()
        elif path == "/api/subtitles/delete":
            self.handle_subtitles_delete()
        elif path == "/api/purge-history":
            self.handle_purge_history()
        elif path == "/api/activity/clear":
            self.handle_activity_clear()
        elif path == "/api/scan-tech-tags":
            self.handle_scan_tech_tags()
        elif path == "/api/tags/rename":
            self.handle_rename_tag()
        elif path == "/api/tags/merge":
            self.handle_merge_tags()
        elif path == "/api/folders/rename":
            self.handle_rename_folder()
        elif path in ("/api/folders/power-rename", "/api/files/power-rename"):
            self.handle_power_rename()
        elif path in ("/api/system/purge-local-data", "/api/purge-local-data"):
            self.handle_purge_local_data()
        elif path == "/api/tag-category/delete":
            self.handle_delete_tag_category()
        elif path == "/api/tag-category/rename":
            self.handle_rename_tag_category()
        elif path == "/api/videos/merge":
            self.handle_merge_videos()
        elif path == "/api/folders/open-sync":
            self.handle_open_sync_folder()
        elif path == "/api/videos/unlink-hash":
            self.handle_unlink_video_hash()

        elif path == "/api/cache/clear":
            data = self.read_json_body()
            cache_id = data.get("safe_id") if data else None
            stream_cache.clear_cache(cache_id=cache_id)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "message": "Encrypted cache cleared"}).encode("utf-8"))
            return

        elif path == "/api/cache/offline":
            data = self.read_json_body()
            safe_id = data.get("safe_id") if data else None
            enabled = bool(data.get("enabled", True)) if data else True
            if not safe_id:
                self.send_error(400, "Missing safe_id")
                return

            manifest = load_manifest()
            file_key = None
            file_info = None
            for k, v in manifest.get("files", {}).items():
                if v.get("safe_id") == safe_id or storyboard_helper.get_safe_id(k) == safe_id:
                    file_key = k
                    file_info = v
                    break
            if not file_key:
                file_key = safe_id
                file_info = manifest.get("files", {}).get(file_key, {})

            repo_id = file_info.get("repo_id") if file_info else None
            blob_path = file_info.get("blob_path") if file_info else None
            meta = self.get_file_metadata(file_key, repo_id=repo_id, blob_path=blob_path)
            if not meta:
                self.send_error(404, "Video metadata not found")
                return

            if enabled:
                stream_cache.start_offline_download(
                    cache_id=safe_id,
                    hf_url=meta["url"],
                    repo_token=meta.get("repo_token"),
                    total_size=meta["header"]["total_size"],
                    chunk_size=meta["header"]["chunk_size"],
                    base_nonce=meta["header"]["base_nonce"],
                    aesgcm=meta["aesgcm"],
                    session=self.hf_session
                )
                msg = "Offline caching initiated in background"
            else:
                stream_cache.stop_offline_download(safe_id)
                stream_cache.set_pinned_offline(safe_id, False, meta["header"]["total_size"], meta["header"]["chunk_size"])
                msg = "Offline pin removed and local cache cleared."

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "message": msg, "pinned_offline": enabled}).encode("utf-8"))
            return
        elif path == "/api/cache/stop_playback":
            data = self.read_json_body()
            safe_id = data.get("safe_id") if data else None
            if safe_id:
                stream_cache.stop_offline_download(safe_id)
                if not stream_cache.is_pinned_offline(safe_id):
                    stream_cache.clear_cache(cache_id=safe_id)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode("utf-8"))
            return
        else:
            self.send_error(404, "Not Found")

    def handle_scan_tech_tags(self):
        port = self.server.server_port if hasattr(self.server, "server_port") else 8080
        def _bg_scan_tags():
            try:
                manifest = load_manifest()
                files = manifest.get("files", {})
                updated_count = 0
                activity_tracker.tracker.add_event(
                    "info",
                    "Tech Specs Scan Started",
                    f"Analyzing resolution, codecs & tags for {len(files)} video(s)..."
                )
                for k, rec in list(files.items()):
                    meta = rec.get("metadata", {})
                    tags = meta.get("tags", [])
                    has_res = any(t in tags for t in ["4K", "2160p", "1440p", "1080p", "Full HD", "720p", "HD Ready", "480p", "SD"])
                    has_dur = any(t in tags for t in ["< 1m", "1-5m", "5-15m", "15-30m", "30-60m", "1-2h", "2-3h", "3h+"])
                    if not has_res or not has_dur or not rec.get("media_info"):
                        url = f"http://127.0.0.1:{port}/stream/{quote(k)}"
                        try:
                            mi = metadata_helper.probe_media_file(url, key_path=k, timeout=12)
                            if mi and mi.get("resolution_label"):
                                rec["media_info"] = mi
                                if "metadata" not in rec or not rec["metadata"]:
                                    rec["metadata"] = {}
                                cur_tags = rec["metadata"].get("tags", [])
                                for t in mi.get("tags", []):
                                    if t not in cur_tags:
                                        cur_tags.append(t)
                                rec["metadata"]["tags"] = cur_tags
                                rec["metadata"]["resolution"] = mi.get("resolution_label", "")
                                rec["metadata"]["duration"] = mi.get("duration_str", "")

                                metadata_helper.write_nfo_files("jellyfin_library", k, rec["metadata"], media_info=mi)
                                storyboard_helper.update_storyboard_duration(k, mi.get("duration_sec", 0))
                                updated_count += 1
                                activity_tracker.tracker.add_event(
                                    "info",
                                    f"Tagged Specs: {Path(k).name}",
                                    f"{mi.get('resolution_label')} ({mi.get('width')}x{mi.get('height')}) | {mi.get('video_codec', 'Video')} | {mi.get('audio_channels', 'Audio')} | {', '.join(mi.get('tags', []))}"
                                )
                        except Exception:
                            pass
                if updated_count > 0:
                    save_manifest(manifest)
                    try:
                        acc = self.acc_mgr.get_upload_account() if hasattr(self, "acc_mgr") and self.acc_mgr else None
                        if acc:
                            for r_id in set(r.get("repo_id") for r in files.values() if r.get("repo_id")):
                                r_files = {rk: rv for rk, rv in files.items() if rv.get("repo_id") == r_id}
                                # Master Index Only mode: canonical index updated via sync_master_index_to_cloud
                                self.sync_master_index_to_cloud()
                    except Exception:
                        pass
                activity_tracker.tracker.add_event(
                    "success",
                    "Tech Specs Scan Complete",
                    f"Finished! Tagged {updated_count} video(s) with technical resolutions & specs."
                )
            except Exception as e:
                print(f"[!] Error in scan_tech_tags: {e}")

        threading.Thread(target=_bg_scan_tags, daemon=True).start()
        resp_body = json.dumps({"success": True, "message": "Technical specs extraction started in background."}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_purge_history(self):
        try:
            if hasattr(self, "acc_mgr") and self.acc_mgr:
                res = self.acc_mgr.purge_all_repo_commit_histories()
            else:
                acc = account_manager.AccountManager()
                res = acc.purge_all_repo_commit_histories()
        except Exception as e:
            res = {"success": False, "error": str(e)}

        resp_body = json.dumps(res).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_activity_clear(self):
        activity_tracker.tracker.clear_events()
        resp_body = json.dumps({"success": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_restore_hf(self):
        deep_scan = ("deep=1" in self.path or "deep=true" in self.path.lower())
        manifest = load_manifest()
        if hasattr(self, "acc_mgr") and self.acc_mgr:
            res = self.acc_mgr.scan_and_merge_all_accounts(manifest, library_dir="jellyfin_library", deep_scan=deep_scan)
            save_manifest(manifest)
            self.sync_master_index_to_cloud(async_mode=True)
            recovered_count = res.get("discovered_count", 0)
            skipped_count = res.get("skipped_count", 0)
            total_files = len(manifest.get("files", {}))
            total_accounts = len(self.acc_mgr.get_all_accounts())
            msg = f"Resynced {total_files} item(s) across {total_accounts} HF account(s) ({skipped_count} skipped)."
        else:
            api = HfApi(token=self.token)
            res = metadata_helper.restore_library_from_hf(
                api=api,
                password=self.password,
                hf_username=self.hf_username,
                library_dir="jellyfin_library",
                manifest_path="upload_manifest.json"
            )
            self.sync_master_index_to_cloud(async_mode=True)
            recovered_count = res.get("recovered_count", 0)
            skipped_count = res.get("skipped_count", 0)
            total_files = len(manifest.get("files", {}))
            total_accounts = 1
            msg = f"Resynced {total_files} item(s) from HF ({skipped_count} skipped)."

        resp_body = json.dumps({
            "success": True,
            "recovered_count": recovered_count,
            "total_files": total_files,
            "total_accounts": total_accounts,
            "skipped_count": skipped_count,
            "skipped_items": res.get("skipped_items", []),
            "message": msg
        }).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_get_jellyfin_sync(self):
        enabled = account_manager.is_jellyfin_sync_enabled()
        resp_body = json.dumps({"enabled": enabled}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_toggle_jellyfin_sync(self):
        cur = account_manager.is_jellyfin_sync_enabled()
        new_val = not cur
        account_manager.set_jellyfin_sync_enabled(new_val)
        if new_val:
            def _bg_gen():
                try:
                    manifest = load_manifest()
                    for k, rec in manifest.get("files", {}).items():
                        if rec.get("status") == "uploaded":
                            strm_p = Path("jellyfin_library") / f"{k}.strm"
                            strm_p.parent.mkdir(parents=True, exist_ok=True)
                            strm_p.write_text(f"http://127.0.0.1:8080/stream/{k}\n", encoding="utf-8")
                            meta = rec.get("metadata") or metadata_helper.extract_default_metadata(k)
                            metadata_helper.write_nfo_files("jellyfin_library", k, meta, media_info=rec.get("media_info"))
                except Exception:
                    pass
            threading.Thread(target=_bg_gen, daemon=True).start()

        resp_body = json.dumps({"success": True, "enabled": new_val}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_cloud_sync_trigger(self):
        watcher = cloud_watcher.get_cloud_watcher()
        watcher.trigger_check()
        resp_body = json.dumps({"success": True, "status": "triggered"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        self.wfile.write(resp_body)


    def handle_accounts_toggle(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            username = req_data.get("username")
            enabled = req_data.get("enabled", None)
        except Exception:
            self.send_error(400, "Invalid JSON payload")
            return

        try:
            res = self.acc_mgr.toggle_account(username=username, enabled=enabled)
            activity_tracker.tracker.add_event(
                "info",
                f"Account {'Enabled' if res['enabled'] else 'Disabled'}: {username}",
                res["message"],
                account=username
            )
            resp_body = json.dumps({"success": True, **res}).encode("utf-8")
        except Exception as e:
            resp_body = json.dumps({"success": False, "message": str(e)}).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_accounts_set_primary(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            username = req_data.get("username")
        except Exception:
            self.send_error(400, "Invalid JSON payload")
            return

        try:
            res = self.acc_mgr.set_primary_account(username=username)
            self.token = self.acc_mgr.upload_token
            self.hf_username = self.acc_mgr.primary_username
            activity_tracker.tracker.add_event(
                "info",
                f"Primary Account Changed: {username}",
                f"All new uploads will now route to {username}",
                account=username
            )
            resp_body = json.dumps({"success": True, **res}).encode("utf-8")
        except Exception as e:
            resp_body = json.dumps({"success": False, "message": str(e)}).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_accounts_delete(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            username = req_data.get("username")
            remove_manifest = bool(req_data.get("remove_manifest", False))
        except Exception:
            self.send_error(400, "Invalid JSON payload")
            return

        try:
            manifest = load_manifest()
            res = self.acc_mgr.delete_account(
                username=username,
                remove_manifest=remove_manifest,
                manifest=manifest,
                library_dir="jellyfin_library"
            )
            if remove_manifest:
                save_manifest(manifest)

            activity_tracker.tracker.add_event(
                "info",
                f"Account Removed: {username}",
                f"Removed from configuration. {'Purged local stream pointers.' if remove_manifest else 'Preserved cached pointers.'}",
                account=username
            )
            resp_body = json.dumps({"success": True, **res}).encode("utf-8")
        except Exception as e:
            resp_body = json.dumps({"success": False, "message": str(e)}).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_sync_hf(self):
        manifest = load_manifest()
        count = 0
        if hasattr(self, "acc_mgr") and self.acc_mgr:
            repos_files = {}
            for k, rec in manifest["files"].items():
                if rec.get("status") == "uploaded":
                    r = rec.get("repo_id", f"{self.hf_username}/{self.default_repo}")
                    if r not in repos_files:
                        repos_files[r] = {}
                    repos_files[r][k] = rec
            for repo_id, r_files in repos_files.items():
                if hasattr(self, "acc_mgr") and self.acc_mgr and not self.acc_mgr.can_write_to_repo(repo_id):
                    logger.info("Skipping HF sync for repo %s: token lacks write permissions.", repo_id)
                    continue
                api = self.acc_mgr.get_api_for_repo(repo_id)
                for k, rec in r_files.items():
                    blob_path = rec.get("blob_path") or rec.get("remote_path")
                    if blob_path:
                        rec_copy = dict(rec)
                        rec_copy["full_key"] = k
                        metadata_helper.sync_item_metadata_to_hf(api, repo_id, blob_path, rec_copy, self.password)
                        count += 1
                # Master Index Only mode: canonical index updated via sync_master_index_to_cloud
                self.sync_master_index_to_cloud()
        else:
            api = HfApi(token=self.token)
            count = metadata_helper.sync_all_manifest_metadata_to_hf(
                api=api,
                manifest=manifest,
                password=self.password,
                hf_username=self.hf_username,
                default_repo=self.default_repo
            )

        self.sync_master_index_to_cloud(async_mode=True)

        resp_body = json.dumps({
            "success": True,
            "synced_items": count,
            "message": f"Encrypted and synced {count} metadata records across Hugging Face accounts."
        }).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_subtitles_upload(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            key = req_data.get("key")
            lang = req_data.get("lang") or "en"
            label = req_data.get("label") or companion_helper.LANGUAGE_MAP.get(lang, "English")
            content = req_data.get("content", "").strip()
        except Exception:
            self.send_error(400, "Invalid JSON payload")
            return

        if not key or not content:
            self.send_error(400, "Missing 'key' or 'content' in request")
            return

        manifest = load_manifest()
        video_info = manifest.get("files", {}).get(key)
        if not video_info:
            for k, v in manifest.get("files", {}).items():
                if key in (v.get("file_names") or []) or Path(key).name in (v.get("aliases") or []):
                    video_info = v
                    key = k
                    break

        if not video_info:
            self.send_error(404, f"Video key '{key}' not found in manifest")
            return

        try:
            repo_id = video_info.get("repo_id", f"{self.hf_username}/{self.default_repo}")
            blob_path = video_info.get("blob_path") or video_info.get("remote_path")
            sub_blob = blob_path.replace(".enc", f".sub.{lang}.srt.enc")

            # Encrypt subtitle content with master password
            sub_enc_bytes = crypto_engine.encrypt_bytes(content.encode("utf-8"), self.password)

            # Upload to Hugging Face
            repo_token = self.acc_mgr.get_token_for_repo(repo_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else self.token
            api = HfApi(token=repo_token)
            api.upload_file(
                path_or_fileobj=sub_enc_bytes,
                path_in_repo=sub_blob,
                repo_id=repo_id,
                repo_type="dataset",
                commit_message=commit_helper.get_human_commit_message("upload")
            )

            account_owner = video_info.get("account") or (repo_id.split("/")[0] if "/" in repo_id else self.hf_username)
            sub_entry = {
                "lang": lang,
                "label": label,
                "blob_path": sub_blob,
                "repo_id": repo_id,
                "account": account_owner,
                "orig_size": len(content.encode("utf-8")),
                "enc_size": len(sub_enc_bytes),
                "format": "srt",
                "verifier": crypto_engine.create_verifier(self.password)
            }

            subs = video_info.get("subtitles") or []
            subs = [s for s in subs if s.get("lang") != lang]
            subs.append(sub_entry)
            video_info["subtitles"] = subs
            save_manifest(manifest)

            # Sync decrypted .srt into Jellyfin vault folder
            companion_helper.sync_jellyfin_subtitle("jellyfin_library", key, content, lang)

            # Sync encrypted sidecar to Hugging Face
            try:
                metadata_helper.sync_item_metadata_to_hf(api, repo_id, blob_path, video_info, self.password)
            except Exception:
                pass

            resp_body = json.dumps({"success": True, "subtitles": subs, "added": sub_entry}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(resp_body)
        except Exception as e:
            sys.stderr.write(f"[!] Error uploading subtitle: {e}\n")
            self.send_error(500, f"Error uploading subtitle: {e}")

    def handle_subtitles_delete(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            key = req_data.get("key")
            lang = req_data.get("lang")
        except Exception:
            self.send_error(400, "Invalid JSON payload")
            return

        if not key or not lang:
            self.send_error(400, "Missing 'key' or 'lang' in request")
            return

        manifest = load_manifest()
        video_info = manifest.get("files", {}).get(key)
        if not video_info:
            for k, v in manifest.get("files", {}).items():
                if key in (v.get("file_names") or []) or Path(key).name in (v.get("aliases") or []):
                    video_info = v
                    key = k
                    break

        if not video_info:
            self.send_error(404, f"Video key '{key}' not found in manifest")
            return

        subs = video_info.get("subtitles") or []
        deleted_subs = [s for s in subs if s.get("lang") == lang]
        video_info["subtitles"] = [s for s in subs if s.get("lang") != lang]
        save_manifest(manifest)

        # Delete local subtitle from jellyfin_library
        try:
            library_dir = Path("jellyfin_library")
            clean_rel = key.strip("/").replace("\\", "/")
            base_stem = Path(clean_rel).stem
            parent_dir = library_dir / Path(clean_rel).parent
            for cand in [parent_dir / f"{base_stem}.{lang}.srt", parent_dir / f"{base_stem}.srt"]:
                if cand.exists():
                    cand.unlink()
        except Exception as e:
            sys.stderr.write(f"[!] Warning: failed to delete local Jellyfin subtitle: {e}\n")

        # Delete remote encrypted subtitle blob from HF
        for ds in deleted_subs:
            ds_blob = ds.get("blob_path")
            ds_repo = ds.get("repo_id") or video_info.get("repo_id")
            if ds_blob and ds_repo:
                try:
                    tok = self.acc_mgr.get_token_for_repo(ds_repo) if (hasattr(self, "acc_mgr") and self.acc_mgr) else self.token
                    if tok:
                        from huggingface_hub import HfApi
                        api = HfApi(token=tok)
                        api.delete_file(path_in_repo=ds_blob, repo_id=ds_repo, repo_type="dataset")
                except Exception as e:
                    sys.stderr.write(f"[!] Remote subtitle delete error: {e}\n")

        resp_body = json.dumps({"success": True, "subtitles": video_info["subtitles"]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)


    def handle_update_metadata(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            key = req_data.get("key")
            meta_in = req_data.get("metadata", {})
            resolve_conflicts = bool(req_data.get("resolve_conflicts", True))
        except Exception:
            self.send_error(400, "Invalid JSON payload")
            return

        if not key:
            self.send_error(400, "Missing 'key' in request")
            return

        # Sanitize incoming metadata against test/conflict boilerplate
        if isinstance(meta_in, dict):
            ov_val = str(meta_in.get("overview", "")).strip()
            if any(w in ov_val.lower() for w in ("conflict test", "dummy test", "test overview")):
                meta_in["overview"] = ""
            if "genres" in meta_in and isinstance(meta_in["genres"], list):
                meta_in["genres"] = [g for g in meta_in["genres"] if "777" not in g and "dummy" not in str(g).lower()]

        manifest = load_manifest()
        if key not in manifest.get("files", {}):
            found_key = None
            for k in manifest.get("files", {}):
                if k == key or Path(k).name == Path(key).name or k.endswith("/" + Path(key).name):
                    found_key = k
                    break
            if found_key:
                key = found_key
            else:
                self.send_error(404, f"Video key '{key}' not found in manifest")
                return

        file_info = manifest["files"][key]
        if key.endswith(".encname"):
            default_title = Path(key).name
        else:
            default_title = Path(key).stem.replace("_", " ").title()
        title = str(meta_in.get("title", "")).strip() or default_title
        year = str(meta_in.get("year", "")).strip()
        rating = str(meta_in.get("rating", "")).strip()
        overview = str(meta_in.get("overview", "") or meta_in.get("plot", "")).strip()

        genres = meta_in.get("genres", [])
        if isinstance(genres, str):
            genres = [g.strip() for g in genres.split(",") if g.strip()]
        elif isinstance(genres, list):
            genres = [str(g).strip() for g in genres if str(g).strip()]

        tags = meta_in.get("tags", [])
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",") if t.strip()]
        elif isinstance(tags, list):
            tags = [str(t).strip() for t in tags if str(t).strip()]

        clean_meta = {
            "title": title,
            "year": year,
            "genres": genres,
            "tags": tags,
            "rating": rating,
            "overview": overview
        }

        # Resolve conflicts & canonicalize filename to chosen title if requested
        old_aliases = list(file_info.get("aliases") or [])
        old_file_names = list(file_info.get("file_names") or [])
        blob_path = file_info.get("blob_path") or file_info.get("remote_path")
        media_hash = file_info.get("media_hash") or file_info.get("content_hash")
        old_key = key
        target_key = key

        if resolve_conflicts:
            safe_title = "".join([c for c in title if c.isalnum() or c in " ._-'()"]).strip()
            if safe_title:
                parent_dir = str(Path(key).parent).replace("\\", "/")
                parent_dir = "" if parent_dir == "." else parent_dir
                if key.endswith(".encname") or safe_title.endswith(".encname"):
                    ext = "" if safe_title.endswith(".encname") else ".encname"
                else:
                    ext = Path(key).suffix or ".mp4"
                cand_key = f"{parent_dir}/{safe_title}{ext}".lstrip("/")
                target_key = cand_key

            # If target_key differs from key, rename on disk and manifest to eliminate "(2)" conflict
            if target_key != key:
                if account_manager.is_jellyfin_sync_enabled():
                    old_strm = Path("jellyfin_library") / f"{key}.strm"
                    if old_strm.exists():
                        try: old_strm.unlink()
                        except Exception: pass
                    metadata_helper.delete_nfo_files("jellyfin_library", key)

                manifest["files"].pop(key, None)
                FILE_CACHE.pop(key, None)
                manifest["files"][target_key] = file_info
                FILE_CACHE[target_key] = file_info

                if account_manager.is_jellyfin_sync_enabled():
                    new_strm = Path("jellyfin_library") / f"{target_key}.strm"
                    new_strm.parent.mkdir(parents=True, exist_ok=True)
                    port = self.server.server_port if hasattr(self.server, "server_port") else 8080
                    strm_url = f"http://127.0.0.1:{port}/stream/{quote(target_key)}"
                    new_strm.write_text(strm_url, encoding="utf-8")
                key = target_key

            if account_manager.is_jellyfin_sync_enabled():
                canonical_files = set()
                try:
                    canonical_files.add((Path("jellyfin_library") / f"{key}.strm").resolve())
                    canonical_files.add((Path("jellyfin_library") / f"{key}.nfo").resolve())
                    canonical_files.add(((Path("jellyfin_library") / key).parent / f"{Path(key).stem}.nfo").resolve())
                    canonical_files.add(((Path("jellyfin_library") / key).parent / f"{Path(key).name}.strm.nfo").resolve())
                except Exception:
                    pass

                # 1. Unlink specific conflict paths from old_file_names
                for other_fn in old_file_names:
                    if other_fn != key:
                        p = Path("jellyfin_library") / other_fn
                        p_stem = p.stem
                        p_name = p.name
                        targets = [
                            p.parent / f"{p_name}.strm",
                            p.parent / f"{p_name}.nfo",
                            p.parent / f"{p_stem}.nfo",
                            p.parent / f"{p_name}.strm.nfo",
                        ]
                        for t in targets:
                            try:
                                if t.exists() and t.resolve() not in canonical_files:
                                    t.unlink()
                            except Exception as e:
                                logger.warning("Error deleting conflict file %s: %s", t, e)

                # 2. Unlink any matching conflict aliases
                canonical_stem = Path(key).stem
                for a in old_aliases:
                    a_stem = Path(a).stem
                    if a_stem != canonical_stem:
                        for cand in Path("jellyfin_library").glob(f"**/*{a_stem}*"):
                            try:
                                if cand.is_file() and cand.resolve() not in canonical_files:
                                    if cand.suffix in [".strm", ".nfo"]:
                                        cand.unlink()
                            except Exception as e:
                                logger.warning("Error unlinking conflict alias %s: %s", cand, e)

            # 3. Remove other duplicate keys in manifest pointing to this same blob / hash
            if blob_path or media_hash:
                for other_k in list(manifest.get("files", {}).keys()):
                    if other_k != key:
                        other_v = manifest["files"][other_k]
                        if (blob_path and other_v.get("blob_path") == blob_path) or (media_hash and (other_v.get("media_hash") == media_hash or other_v.get("content_hash") == media_hash)):
                            manifest["files"].pop(other_k, None)

            # 4. Reset aliases and file_names to only the canonical key
            file_info["aliases"] = []
            file_info["file_names"] = [key]

        if media_hash:
            for table in ("media_hashes", "content_hashes", "fingerprints"):
                if table in manifest:
                    manifest[table][media_hash] = file_info

        # Update manifest & record verifier
        file_info["metadata"] = clean_meta
        file_info["verifier"] = crypto_engine.create_verifier(self.password)
        save_manifest(manifest)

        # Write clean Jellyfin NFO files (.nfo, .mp4.nfo, .strm.nfo)
        try:
            metadata_helper.write_nfo_files("jellyfin_library", key, clean_meta, media_info=file_info.get("media_info"))
        except Exception as e:
            logger.warning("Error writing NFO for %s: %s", key, e)

        # Fast background thread for Hugging Face sync so HTTP response is INSTANT (< 15ms)
        repo_id = file_info.get("repo_id", f"{self.hf_username}/{self.default_repo}")
        blob_path = file_info.get("blob_path") or file_info.get("remote_path")

        def _bg_sync_meta(item_key, r_id, b_path, f_record):
            try:
                if hasattr(self, "acc_mgr") and self.acc_mgr and not self.acc_mgr.can_write_to_repo(r_id):
                    logger.info("Skipping HF metadata sync for %s: Account token lacks write permissions.", r_id)
                    return
                api = self.acc_mgr.get_api_for_repo(r_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else HfApi(token=self.token)
                if b_path:
                    rec = dict(f_record)
                    rec["full_key"] = item_key
                    metadata_helper.sync_item_metadata_to_hf(api, r_id, b_path, rec, self.password)
                m = load_manifest()
                repo_files = {k: v for k, v in m.get("files", {}).items() if v.get("repo_id") == r_id}
                # Master Index Only mode: canonical index updated via sync_master_index_to_cloud
                self.sync_master_index_to_cloud()
            except Exception as ex:
                logger.warning("Background metadata HF sync warning: %s", ex)

        if blob_path:
            threading.Thread(target=_bg_sync_meta, args=(key, repo_id, blob_path, file_info), daemon=True).start()

        resp_body = json.dumps({
            "success": True,
            "key": key,
            "old_key": old_key,
            "metadata": clean_meta,
            "aliases": file_info.get("aliases", []),
            "file_names": file_info.get("file_names", [key]),
            "conflicts_resolved": bool(resolve_conflicts),
            "message": "Metadata saved to Jellyfin .nfo, conflicts resolved, and syncing to Hugging Face."
        }).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_video_tag_action(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            key = str(req_data.get("key", "")).strip()
            action = str(req_data.get("action", "add")).strip().lower()
            tag_type = str(req_data.get("type", "tag")).strip().lower()
            val = str(req_data.get("value", "")).strip()
        except Exception:
            self.send_error(400, "Invalid JSON payload")
            return

        if not key or action not in ("add", "remove") or not val:
            self.send_error(400, "Missing or invalid key, action, or value")
            return

        is_genre = tag_type in ("genre", "category", "cat")

        manifest = load_manifest()
        files = manifest.get("files", {})
        if key not in files:
            found_key = None
            for k in files:
                if k == key or Path(k).name == Path(key).name or k.endswith("/" + Path(key).name):
                    found_key = k
                    break
            if found_key:
                key = found_key
            else:
                self.send_error(404, f"Video key '{key}' not found in manifest")
                return

        finfo = files[key]
        meta = finfo.get("metadata") or {}

        field_name = "genres" if is_genre else "tags"
        existing = meta.get(field_name) or []
        if isinstance(existing, str):
            existing = [x.strip() for x in existing.split(",") if x.strip()]
        elif isinstance(existing, list):
            existing = [str(x).strip() for x in existing if str(x).strip()]
        else:
            existing = []

        modified = False
        if action == "add":
            if not any(x.lower() == val.lower() for x in existing):
                existing.append(val)
                modified = True
        elif action == "remove":
            new_list = [x for x in existing if x.lower() != val.lower()]
            if len(new_list) != len(existing):
                existing = new_list
                modified = True

        if modified:
            meta[field_name] = existing
            finfo["metadata"] = meta
            finfo["verifier"] = crypto_engine.create_verifier(self.password)
            save_manifest(manifest)

            # Write clean Jellyfin NFO files (.nfo, .mp4.nfo, .strm.nfo)
            try:
                metadata_helper.write_nfo_files("jellyfin_library", key, meta)
            except Exception as e:
                logger.warning("Error writing NFO for %s: %s", key, e)

            # Fast background thread for Hugging Face sync so HTTP response is INSTANT (< 15ms)
            repo_id = finfo.get("repo_id", f"{self.hf_username}/{self.default_repo}")
            blob_path = finfo.get("blob_path") or finfo.get("remote_path")

            def _bg_sync_meta(item_key, r_id, b_path, f_record):
                try:
                    api = self.acc_mgr.get_api_for_repo(r_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else HfApi(token=self.token)
                    if b_path:
                        rec = dict(f_record)
                        rec["full_key"] = item_key
                        metadata_helper.sync_item_metadata_to_hf(api, r_id, b_path, rec, self.password)
                    m = load_manifest()
                    repo_files = {k: v for k, v in m.get("files", {}).items() if v.get("repo_id") == r_id}
                    # Master Index Only mode: canonical index updated via sync_master_index_to_cloud
                    self.sync_master_index_to_cloud()
                except Exception as ex:
                    logger.warning("Background metadata HF sync warning: %s", ex)

            if blob_path:
                threading.Thread(target=_bg_sync_meta, args=(key, repo_id, blob_path, finfo), daemon=True).start()

        resp_body = json.dumps({
            "success": True,
            "key": key,
            "metadata": meta,
            "action": action,
            "type": "genre" if is_genre else "tag",
            "value": val,
            "message": f"Successfully {action}ed {'Category' if is_genre else 'Tag'} '{val}'"
        }).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_folder_tags(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            folder = str(req_data.get("folder", "")).strip().replace("\\", "/")
            action = str(req_data.get("action", "")).strip().lower()
            tag = str(req_data.get("tag", "")).strip()
        except Exception:
            self.send_error(400, "Invalid JSON payload")
            return

        if not folder or action not in ("add", "remove") or not tag:
            self.send_error(400, "Missing or invalid folder, action, or tag")
            return

        manifest = load_manifest()
        files = manifest.get("files", {})
        prefix = folder.rstrip("/") + "/"
        matched_keys = [k for k in files if k == folder or k.replace("\\", "/").startswith(prefix)]

        updated_count = 0
        for k in matched_keys:
            finfo = files[k]
            meta = finfo.get("metadata") or {}
            existing_tags = meta.get("tags") or []
            if isinstance(existing_tags, str):
                existing_tags = [t.strip() for t in existing_tags.split(",") if t.strip()]
            elif isinstance(existing_tags, list):
                existing_tags = [str(t).strip() for t in existing_tags if str(t).strip()]
            else:
                existing_tags = []

            modified = False
            if action == "add":
                if tag not in existing_tags:
                    existing_tags.append(tag)
                    modified = True
            elif action == "remove":
                if tag in existing_tags:
                    existing_tags = [t for t in existing_tags if t != tag]
                    modified = True

            if modified:
                meta["tags"] = existing_tags
                finfo["metadata"] = meta
                finfo["verifier"] = crypto_engine.create_verifier(self.password)
                try:
                    metadata_helper.write_nfo_files("jellyfin_library", k, meta)
                except Exception as e:
                    logger.warning("Error writing NFO for %s: %s", k, e)
                updated_count += 1

        if updated_count > 0:
            save_manifest(manifest)

            # Sync updated items to HF in background so HTTP response is instant
            def _bg_sync_hf(keys_to_sync):
                try:
                    m = load_manifest()
                    repos_touched = set()
                    for item_key in keys_to_sync:
                        finfo = m.get("files", {}).get(item_key)
                        if not finfo:
                            continue
                        repo_id = finfo.get("repo_id", f"{self.hf_username}/{self.default_repo}")
                        repos_touched.add(repo_id)
                        api = self.acc_mgr.get_api_for_repo(repo_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else HfApi(token=self.token)
                        blob_path = finfo.get("blob_path") or finfo.get("remote_path")
                        if blob_path:
                            record = dict(finfo)
                            record["full_key"] = item_key
                            metadata_helper.sync_item_metadata_to_hf(api, repo_id, blob_path, record, self.password)
                    for r_id in repos_touched:
                        api = self.acc_mgr.get_api_for_repo(r_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else HfApi(token=self.token)
                        repo_files = {k: v for k, v in m.get("files", {}).items() if v.get("repo_id") == r_id}
                        # Master Index Only mode: canonical index updated via sync_master_index_to_cloud
                        self.sync_master_index_to_cloud()
                except Exception as ex:
                    logger.warning("Background folder tag HF sync warning: %s", ex)

            threading.Thread(target=_bg_sync_hf, args=(matched_keys,), daemon=True).start()

        resp_body = json.dumps({
            "success": True,
            "folder": folder,
            "action": action,
            "tag": tag,
            "updated_count": updated_count
        }).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_rename_tag(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            old_tag = str(req_data.get("old_tag", "")).strip()
            new_tag = str(req_data.get("new_tag", "")).strip()
        except Exception:
            self.send_error(400, "Invalid JSON payload")
            return

        if not old_tag or not new_tag:
            self.send_error(400, "Both 'old_tag' and 'new_tag' are required")
            return

        manifest = load_manifest()
        files = manifest.get("files", {})
        old_lower = old_tag.lower()
        updated_keys = []

        for k, finfo in files.items():
            meta = finfo.get("metadata") or {}
            raw_tags = meta.get("tags") or []
            if isinstance(raw_tags, str):
                raw_tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
            elif not isinstance(raw_tags, list):
                raw_tags = []

            has_match = any(str(t).strip().lower() == old_lower for t in raw_tags)
            if not has_match:
                continue

            new_tags = []
            seen = set()
            for t in raw_tags:
                tag_str = str(t).strip()
                if not tag_str:
                    continue
                if tag_str.lower() == old_lower:
                    tag_str = new_tag
                if tag_str.lower() not in seen:
                    seen.add(tag_str.lower())
                    new_tags.append(tag_str)

            meta["tags"] = new_tags
            finfo["metadata"] = meta
            finfo["verifier"] = crypto_engine.create_verifier(self.password)
            try:
                metadata_helper.write_nfo_files("jellyfin_library", k, meta)
                for alias in finfo.get("aliases", []):
                    alias_key = str(Path(k).parent / alias) if str(Path(k).parent) != "." else alias
                    metadata_helper.write_nfo_files("jellyfin_library", alias_key, meta)
            except Exception as e:
                logger.warning("Error writing NFO for %s: %s", k, e)

            updated_keys.append(k)

        if updated_keys:
            save_manifest(manifest)
            activity_tracker.tracker.add_event(
                "info",
                "Tag Renamed",
                f"Renamed tag '{old_tag}' to '{new_tag}' across {len(updated_keys)} video(s)"
            )

            def _bg_sync_rename(keys):
                try:
                    m = load_manifest()
                    repos_touched = set()
                    for item_key in keys:
                        f = m.get("files", {}).get(item_key)
                        if not f:
                            continue
                        repo_id = f.get("repo_id", f"{self.hf_username}/{self.default_repo}")
                        repos_touched.add(repo_id)
                        api = self.acc_mgr.get_api_for_repo(repo_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else HfApi(token=self.token)
                        blob_path = f.get("blob_path") or f.get("remote_path")
                        if blob_path:
                            record = dict(f)
                            record["full_key"] = item_key
                            metadata_helper.sync_item_metadata_to_hf(api, repo_id, blob_path, record, self.password)
                    for r_id in repos_touched:
                        api = self.acc_mgr.get_api_for_repo(r_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else HfApi(token=self.token)
                        repo_files = {k: v for k, v in m.get("files", {}).items() if v.get("repo_id") == r_id}
                        # Master Index Only mode: canonical index updated via sync_master_index_to_cloud
                        self.sync_master_index_to_cloud()
                except Exception as ex:
                    logger.warning("Background tag rename HF sync warning: %s", ex)

            threading.Thread(target=_bg_sync_rename, args=(updated_keys,), daemon=True).start()

        resp = {
            "success": True,
            "old_tag": old_tag,
            "new_tag": new_tag,
            "updated_count": len(updated_keys),
            "message": f"Successfully renamed tag '{old_tag}' to '{new_tag}' across {len(updated_keys)} video(s)."
        }
        resp_body = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_merge_tags(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            source_tags = req_data.get("source_tags", [])
            target_tag = str(req_data.get("target_tag", "")).strip()
        except Exception:
            self.send_error(400, "Invalid JSON payload")
            return

        if not isinstance(source_tags, list) or not source_tags or not target_tag:
            self.send_error(400, "'source_tags' (non-empty list) and 'target_tag' are required")
            return

        src_lowers = {str(t).strip().lower() for t in source_tags if str(t).strip()}
        if not src_lowers:
            self.send_error(400, "No valid source tags provided")
            return

        manifest = load_manifest()
        files = manifest.get("files", {})
        updated_keys = []

        for k, finfo in files.items():
            meta = finfo.get("metadata") or {}
            raw_tags = meta.get("tags") or []
            if isinstance(raw_tags, str):
                raw_tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
            elif not isinstance(raw_tags, list):
                raw_tags = []

            has_match = any(str(t).strip().lower() in src_lowers for t in raw_tags)
            if not has_match:
                continue

            new_tags = []
            seen = set()
            for t in raw_tags:
                tag_str = str(t).strip()
                if not tag_str or tag_str.lower() in src_lowers:
                    continue
                if tag_str.lower() not in seen:
                    seen.add(tag_str.lower())
                    new_tags.append(tag_str)

            if target_tag.lower() not in seen:
                seen.add(target_tag.lower())
                new_tags.append(target_tag)

            meta["tags"] = new_tags
            finfo["metadata"] = meta
            finfo["verifier"] = crypto_engine.create_verifier(self.password)
            try:
                metadata_helper.write_nfo_files("jellyfin_library", k, meta)
                for alias in finfo.get("aliases", []):
                    alias_key = str(Path(k).parent / alias) if str(Path(k).parent) != "." else alias
                    metadata_helper.write_nfo_files("jellyfin_library", alias_key, meta)
            except Exception as e:
                logger.warning("Error writing NFO for %s: %s", k, e)

            updated_keys.append(k)

        if updated_keys:
            save_manifest(manifest)
            activity_tracker.tracker.add_event(
                "info",
                "Tags Merged",
                f"Merged {len(src_lowers)} tag(s) into '{target_tag}' across {len(updated_keys)} video(s)"
            )

            def _bg_sync_merge(keys):
                try:
                    m = load_manifest()
                    repos_touched = set()
                    for item_key in keys:
                        f = m.get("files", {}).get(item_key)
                        if not f:
                            continue
                        repo_id = f.get("repo_id", f"{self.hf_username}/{self.default_repo}")
                        repos_touched.add(repo_id)
                        api = self.acc_mgr.get_api_for_repo(repo_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else HfApi(token=self.token)
                        blob_path = f.get("blob_path") or f.get("remote_path")
                        if blob_path:
                            record = dict(f)
                            record["full_key"] = item_key
                            metadata_helper.sync_item_metadata_to_hf(api, repo_id, blob_path, record, self.password)
                    for r_id in repos_touched:
                        api = self.acc_mgr.get_api_for_repo(r_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else HfApi(token=self.token)
                        repo_files = {k: v for k, v in m.get("files", {}).items() if v.get("repo_id") == r_id}
                        # Master Index Only mode: canonical index updated via sync_master_index_to_cloud
                        self.sync_master_index_to_cloud()
                except Exception as ex:
                    logger.warning("Background tag merge HF sync warning: %s", ex)

            threading.Thread(target=_bg_sync_merge, args=(updated_keys,), daemon=True).start()

        resp = {
            "success": True,
            "source_tags": source_tags,
            "target_tag": target_tag,
            "updated_count": len(updated_keys),
            "message": f"Successfully merged {len(source_tags)} tag(s) into '{target_tag}' across {len(updated_keys)} video(s)."
        }
        resp_body = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_rename_folder(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            folder_path = str(req_data.get("folder_path", "")).strip().replace("\\", "/").strip("/")
            new_name = str(req_data.get("new_name", "")).strip().replace("\\", "/").strip("/")
        except Exception:
            self.send_error(400, "Invalid JSON payload")
            return

        if not folder_path or not new_name:
            self.send_error(400, "Both 'folder_path' and 'new_name' are required")
            return

        clean_new_leaf = new_name.split("/")[-1].strip()
        if not clean_new_leaf:
            self.send_error(400, "Invalid new folder name")
            return

        if "/" in folder_path:
            parent_path = folder_path.rsplit("/", 1)[0]
            new_folder_path = f"{parent_path}/{clean_new_leaf}"
            old_leaf_name = folder_path.rsplit("/", 1)[1]
        else:
            new_folder_path = clean_new_leaf
            old_leaf_name = folder_path

        if folder_path == new_folder_path:
            resp_body = json.dumps({"success": True, "message": "Folder name unchanged", "old_folder": folder_path, "new_folder": new_folder_path, "updated_count": 0}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(resp_body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(resp_body)
            return

        manifest = load_manifest()
        files = manifest.get("files", {})
        prefix = folder_path + "/"

        matched_keys = [k for k in list(files.keys()) if k == folder_path or k.replace("\\", "/").startswith(prefix)]
        if not matched_keys:
            self.send_error(404, f"No files found under folder '{folder_path}'")
            return

        import shutil
        if account_manager.is_jellyfin_sync_enabled():
            old_lib_dir = Path("jellyfin_library") / folder_path
            new_lib_dir = Path("jellyfin_library") / new_folder_path
            if old_lib_dir.exists() and old_lib_dir.is_dir():
                new_lib_dir.parent.mkdir(parents=True, exist_ok=True)
                if not new_lib_dir.exists():
                    try:
                        old_lib_dir.rename(new_lib_dir)
                    except Exception:
                        shutil.move(str(old_lib_dir), str(new_lib_dir))
                else:
                    for item in old_lib_dir.glob("**/*"):
                        if item.is_file():
                            rel = item.relative_to(old_lib_dir)
                            dest = new_lib_dir / rel
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            if dest.exists():
                                p = dest
                                stem = p.stem
                                suffix = p.suffix
                                counter = 1
                                cand = p.parent / f"{stem} ({counter}){suffix}"
                                while cand.exists():
                                    counter += 1
                                    cand = p.parent / f"{stem} ({counter}){suffix}"
                                dest = cand
                            try:
                                shutil.move(str(item), str(dest))
                            except Exception as ex:
                                logger.warning("Error moving library file %s to %s: %s", item, dest, ex)
                    shutil.rmtree(str(old_lib_dir), ignore_errors=True)

            if new_lib_dir.exists():
                for strm_file in new_lib_dir.glob("**/*.strm"):
                    try:
                        txt = strm_file.read_text(encoding="utf-8")
                        txt = txt.replace(f"/stream/{folder_path}/", f"/stream/{new_folder_path}/")
                        strm_file.write_text(txt, encoding="utf-8")
                    except Exception as ex:
                        logger.warning("Error updating strm file %s: %s", strm_file, ex)

        old_sync_dir = Path("videos_to_sync") / folder_path
        new_sync_dir = Path("videos_to_sync") / new_folder_path
        if old_sync_dir.exists() and old_sync_dir.is_dir():
            new_sync_dir.parent.mkdir(parents=True, exist_ok=True)
            try:
                if not new_sync_dir.exists():
                    old_sync_dir.rename(new_sync_dir)
                else:
                    for item in old_sync_dir.glob("**/*"):
                        if item.is_file():
                            rel = item.relative_to(old_sync_dir)
                            dest = new_sync_dir / rel
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            if dest.exists():
                                p = dest
                                stem = p.stem
                                suffix = p.suffix
                                counter = 1
                                cand = p.parent / f"{stem} ({counter}){suffix}"
                                while cand.exists():
                                    counter += 1
                                    cand = p.parent / f"{stem} ({counter}){suffix}"
                                dest = cand
                            try:
                                shutil.move(str(item), str(dest))
                            except Exception as ex:
                                logger.warning("Error moving sync file %s to %s: %s", item, dest, ex)
                    shutil.rmtree(str(old_sync_dir), ignore_errors=True)
            except Exception as ex:
                logger.warning("Error moving sync dir: %s", ex)

        new_keys_created = []
        for old_k in matched_keys:
            finfo = files.pop(old_k)
            if old_k == folder_path:
                new_k = new_folder_path
            else:
                new_k = new_folder_path + old_k[len(folder_path):]

            # Collision check: if another video already exists at new_k, suffix with (1), (2), etc.
            if new_k in files and files[new_k].get("blob_path") != finfo.get("blob_path"):
                p = Path(new_k)
                stem = p.stem
                suffix = p.suffix
                parent = str(p.parent).replace("\\", "/")
                parent_prefix = f"{parent}/" if parent and parent != "." else ""
                counter = 1
                cand = f"{parent_prefix}{stem} ({counter}){suffix}"
                while cand in files:
                    counter += 1
                    cand = f"{parent_prefix}{stem} ({counter}){suffix}"
                new_k = cand

            old_fns = finfo.get("file_names") or []
            new_fns = []
            for fn in old_fns:
                if fn == old_k or fn.startswith(prefix):
                    new_fn = new_folder_path + fn[len(folder_path):]
                    new_fns.append(new_fn)
                else:
                    new_fns.append(fn)
            if new_k not in new_fns:
                new_fns.append(new_k)
            finfo["file_names"] = new_fns

            meta = finfo.get("metadata") or {}
            genres = meta.get("genres") or []
            if isinstance(genres, list):
                new_genres = []
                for g in genres:
                    g_clean = str(g).strip()
                    if g_clean.lower() == old_leaf_name.lower() or g_clean.lower() == folder_path.lower():
                        new_genres.append(clean_new_leaf)
                    else:
                        new_genres.append(g_clean)
                meta["genres"] = new_genres
                finfo["metadata"] = meta

            finfo["verifier"] = crypto_engine.create_verifier(self.password)
            files[new_k] = finfo
            FILE_CACHE.pop(old_k, None)
            FILE_CACHE[new_k] = finfo
            new_keys_created.append(new_k)

            try:
                metadata_helper.write_nfo_files("jellyfin_library", new_k, meta)
                if account_manager.is_jellyfin_sync_enabled():
                    strm_p = Path("jellyfin_library") / f"{new_k}.strm"
                    strm_p.parent.mkdir(parents=True, exist_ok=True)
                    port = self.server.server_port if hasattr(self.server, "server_port") else 8080
                    strm_p.write_text(f"http://127.0.0.1:{port}/stream/{quote(new_k)}\n", encoding="utf-8")
            except Exception as e:
                logger.warning("Error rewriting NFO/strm for %s: %s", new_k, e)

        save_manifest(manifest)
        activity_tracker.tracker.add_event(
            "info",
            "Folder Renamed",
            f"Renamed folder '{folder_path}' to '{new_folder_path}' ({len(matched_keys)} video(s))"
        )

        def _bg_sync_folder(new_keys_list):
            try:
                m = load_manifest()
                items_by_repo = {}
                for item_key in new_keys_list:
                    f = m.get("files", {}).get(item_key)
                    if not f:
                        continue
                    repo_id = f.get("repo_id", f"{self.hf_username}/{self.default_repo}")
                    blob_path = f.get("blob_path") or f.get("remote_path")
                    if blob_path:
                        record = dict(f)
                        record["full_key"] = item_key
                        items_by_repo.setdefault(repo_id, []).append((blob_path, record))

                for r_id, items in items_by_repo.items():
                    api = self.acc_mgr.get_api_for_repo(r_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else HfApi(token=self.token)
                    committed = metadata_helper.sync_bulk_metadata_to_hf(api, r_id, items, self.password)
                    logger.info("Atomic folder rename committed %d metadata sidecars to %s", committed, r_id)

                self.sync_master_index_to_cloud()
            except Exception as ex:
                logger.warning("Background folder rename HF sync warning: %s", ex)

        threading.Thread(target=_bg_sync_folder, args=(new_keys_created,), daemon=True).start()

        resp = {
            "success": True,
            "old_folder": folder_path,
            "new_folder": new_folder_path,
            "new_leaf": clean_new_leaf,
            "updated_count": len(matched_keys),
            "message": f"Successfully renamed folder '{folder_path}' to '{new_folder_path}' across {len(matched_keys)} video(s)."
        }
        resp_body = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_power_rename(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
        except Exception:
            self.send_error(400, "Invalid JSON payload")
            return

        folder_path = str(req_data.get("folder_path", "")).strip().replace("\\", "/").strip("/")
        renames = req_data.get("renames")  # list of {"old_key": str, "new_key": str}

        # If explicit renames not provided, generate from parameters
        if not renames:
            base_name = str(req_data.get("base_name", "")).strip()
            if not base_name:
                self.send_error(400, "'renames' list or 'base_name' is required")
                return
            start_num = int(req_data.get("start_num", 1))
            padding = int(req_data.get("padding", 1))
            separator = str(req_data.get("separator", " "))
            format_style = str(req_data.get("format_style", "space"))
            include_subfolders = bool(req_data.get("include_subfolders", True))
            sort_by = str(req_data.get("sort_by", "name_asc"))

            manifest = load_manifest()
            files = manifest.get("files", {})
            prefix = folder_path + "/" if folder_path else ""

            candidate_keys = []
            for k in files.keys():
                k_clean = k.replace("\\", "/")
                if prefix:
                    if include_subfolders:
                        if k_clean.startswith(prefix):
                            candidate_keys.append(k)
                    else:
                        if k_clean.startswith(prefix) and "/" not in k_clean[len(prefix):]:
                            candidate_keys.append(k)
                else:
                    if include_subfolders or "/" not in k_clean:
                        candidate_keys.append(k)

            # Natural Sort candidate keys
            if sort_by == "name_desc":
                candidate_keys.sort(key=lambda x: Path(x).name.lower(), reverse=True)
            elif sort_by == "date_asc":
                candidate_keys.sort(key=lambda x: float(files.get(x, {}).get("uploaded_at", 0) or 0))
            elif sort_by == "date_desc":
                candidate_keys.sort(key=lambda x: float(files.get(x, {}).get("uploaded_at", 0) or 0), reverse=True)
            else:
                def natural_sort_key(s):
                    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', Path(s).name)]
                candidate_keys.sort(key=natural_sort_key)

            renames = []
            for idx, old_k in enumerate(candidate_keys):
                cur_num = start_num + idx
                num_str = str(cur_num).zfill(padding) if padding > 1 else str(cur_num)
                parent_dir = str(Path(old_k).parent).replace("\\", "/")
                parent_dir = "" if parent_dir == "." else parent_dir
                ext = Path(old_k).suffix or ".mp4"

                if format_style == "hyphen":
                    new_filename = f"{base_name} - {num_str}{ext}"
                elif format_style == "underscore":
                    new_filename = f"{base_name}_{num_str}{ext}"
                elif format_style == "parentheses":
                    new_filename = f"{base_name} ({num_str}){ext}"
                elif format_style == "dot":
                    new_filename = f"{base_name}.{num_str}{ext}"
                else:
                    new_filename = f"{base_name} {num_str}{ext}"

                new_k = f"{parent_dir}/{new_filename}".lstrip("/") if parent_dir else new_filename
                renames.append({"old_key": old_k, "new_key": new_k})

        if not renames:
            self.send_error(400, "No files found to rename")
            return

        manifest = load_manifest()
        files = manifest.get("files", {})
        port = self.server.server_port if hasattr(self.server, "server_port") else 8080
        jelly_enabled = account_manager.is_jellyfin_sync_enabled()

        applied_renames = []
        sync_items_by_repo = {}

        for item in renames:
            old_k = item.get("old_key")
            new_k = item.get("new_key")
            if not old_k or not new_k or old_k == new_k:
                continue
            if old_k not in files:
                continue

            finfo = files.pop(old_k)
            finfo["full_key"] = new_k

            fns = finfo.get("file_names") or []
            new_fns = []
            for fn in fns:
                if fn == old_k:
                    new_fns.append(new_k)
                else:
                    new_fns.append(fn)
            if new_k not in new_fns:
                new_fns.append(new_k)
            finfo["file_names"] = new_fns

            clean_meta = finfo.get("metadata") or {}
            clean_meta["title"] = Path(new_k).stem
            finfo["metadata"] = clean_meta
            finfo["verifier"] = crypto_engine.create_verifier(self.password)
            finfo["updated_at"] = time.time()

            files[new_k] = finfo
            FILE_CACHE.pop(old_k, None)
            FILE_CACHE[new_k] = finfo

            media_hash = finfo.get("media_hash") or finfo.get("content_hash")
            if media_hash and "media_hashes" in manifest:
                manifest["media_hashes"][media_hash] = finfo

            try:
                local_old = Path("videos_to_sync") / old_k
                if local_old.exists():
                    local_new = Path("videos_to_sync") / new_k
                    local_new.parent.mkdir(parents=True, exist_ok=True)
                    local_old.rename(local_new)
                    old_stem = local_old.stem
                    for srt_cand in local_old.parent.glob(f"{old_stem}*.srt"):
                        new_srt_name = srt_cand.name.replace(old_stem, Path(new_k).stem, 1)
                        srt_cand.rename(srt_cand.parent / new_srt_name)
            except Exception as e:
                logger.warning("Error moving local sync file %s: %s", old_k, e)

            if jelly_enabled:
                try:
                    old_strm = Path("jellyfin_library") / f"{old_k}.strm"
                    if old_strm.exists():
                        try: old_strm.unlink()
                        except Exception: pass
                    metadata_helper.delete_nfo_files("jellyfin_library", old_k)

                    new_strm = Path("jellyfin_library") / f"{new_k}.strm"
                    new_strm.parent.mkdir(parents=True, exist_ok=True)
                    strm_url = f"http://127.0.0.1:{port}/stream/{quote(new_k)}"
                    new_strm.write_text(strm_url, encoding="utf-8")
                    metadata_helper.write_nfo_files("jellyfin_library", new_k, clean_meta, media_info=finfo.get("media_info"))
                except Exception as ex:
                    logger.warning("Error syncing Jellyfin for %s: %s", new_k, ex)

            repo_id = finfo.get("repo_id", f"{self.hf_username}/{self.default_repo}")
            blob_path = finfo.get("blob_path") or finfo.get("remote_path")
            if blob_path:
                sync_items_by_repo.setdefault(repo_id, []).append((blob_path, dict(finfo)))

            applied_renames.append({"old_key": old_k, "new_key": new_k})

        save_manifest(manifest)

        activity_tracker.tracker.add_event(
            "info",
            "Power Rename Completed",
            f"Power Renamed {len(applied_renames)} video(s) in '{folder_path or 'Vault'}' (Atomic HF Commit queued)"
        )

        def _bg_sync_power_rename(items_by_repo, cur_manifest):
            try:
                for r_id, items in items_by_repo.items():
                    api = self.acc_mgr.get_api_for_repo(r_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else HfApi(token=self.token)
                    committed = metadata_helper.sync_bulk_metadata_to_hf(api, r_id, items, self.password)
                    logger.info("Atomic bulk rename committed %d metadata sidecars to %s", committed, r_id)

                master_repo = self.acc_mgr.get_master_repo_id() if hasattr(self, "acc_mgr") and self.acc_mgr else (os.getenv("REPO_ID") or f"{self.hf_username}/{self.default_repo}")
                m_api = self.acc_mgr.get_api_for_repo(master_repo) if hasattr(self, "acc_mgr") and self.acc_mgr else HfApi(token=self.token)
                from core import hf_rate_limiter
                hf_rate_limiter.debounced_syncer.queue_master_sync(
                    m_api,
                    master_repo,
                    cur_manifest,
                    self.password,
                    metadata_helper.sync_master_manifest_to_hf
                )
            except Exception as ex:
                logger.warning("Background power rename commit error: %s", ex)

        threading.Thread(target=_bg_sync_power_rename, args=(sync_items_by_repo, manifest), daemon=True).start()

        resp_body = json.dumps({
            "success": True,
            "renamed_count": len(applied_renames),
            "folder_path": folder_path,
            "renames": applied_renames
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_purge_local_data(self):
        """
        Purges all local cache files (encrypted_cache, storyboards, temp files, activity logs)
        and deletes local upload_manifest.json, forcing it to recreate afresh from Hugging Face
        Master Index (vault_index.enc) on reload, and restarts the server process.
        """
        logger.info("[Purge] Initiating full local data purge and cloud recreation...")
        purged_cache_count = 0
        ec = Path("encrypted_cache")
        if ec.exists():
            for f in ec.iterdir():
                if f.is_file() and f.name != ".gitkeep":
                    try:
                        f.unlink()
                        purged_cache_count += 1
                    except Exception:
                        pass

        purged_storyboard_count = 0
        sb = Path("storyboards")
        if sb.exists():
            for f in sb.glob("**/*"):
                if f.is_file() and f.name != ".gitkeep":
                    try:
                        f.unlink()
                        purged_storyboard_count += 1
                    except Exception:
                        pass
            for d in list(sb.glob("**/*"))[::-1]:
                if d.is_dir() and not any(d.iterdir()):
                    try:
                        d.rmdir()
                    except Exception:
                        pass

        # Clean videos_to_sync companion files & root temp files
        vts = Path("videos_to_sync")
        if vts.exists():
            for f in vts.glob("**/*.companion*"):
                try:
                    f.unlink()
                except Exception:
                    pass
        for p in Path(".").glob("temp_*"):
            try:
                p.unlink()
            except Exception:
                pass
        for p in Path(".").glob("*.tmp"):
            try:
                p.unlink()
            except Exception:
                pass
        for b in Path(".").glob("upload_manifest.json.safety*"):
            try:
                b.unlink()
            except Exception:
                pass

        # Reset in-memory cache
        FILE_CACHE.clear()
        if hasattr(self, "stream_cache") and self.stream_cache:
            try:
                self.stream_cache.clear_cache()
            except Exception:
                pass

        # Reset live_activity.json
        try:
            activity_tracker.tracker.reset()
        except Exception:
            pass

        # Recreate manifest from Hugging Face Master Repository
        recreated_count = 0
        port = self.server.server_port if hasattr(self.server, "server_port") else 8080
        acc_mgr = self.acc_mgr if hasattr(self, "acc_mgr") and self.acc_mgr else account_manager.AccountManager()
        fresh_manifest = {"files": {}, "media_hashes": {}, "tombstones": {}}
        try:
            scan_res = acc_mgr.scan_and_merge_all_accounts(
                fresh_manifest,
                library_dir="jellyfin_library",
                proxy_host=f"127.0.0.1:{port}"
            )
            save_manifest(fresh_manifest)
            recreated_count = scan_res.get("discovered_count", len(fresh_manifest.get("files", {})))
            logger.info("[Purge] Rebuilt fresh manifest with %d videos from cloud vault", recreated_count)
        except Exception as ex:
            logger.warning("[Purge] Warning recreating manifest from cloud: %s", ex)

        resp = {
            "success": True,
            "purged_cache_files": purged_cache_count,
            "purged_storyboard_files": purged_storyboard_count,
            "recreated_files_count": recreated_count,
            "message": f"Local data purged. Rebuilt {recreated_count} video(s) from cloud vault. Server restarting..."
        }
        resp_body = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)
        try:
            self.wfile.flush()
        except Exception:
            pass

        # Schedule asynchronous server restart
        def _restart_server():
            time.sleep(1.0)
            logger.info("[Purge] Restarting server process...")
            try:
                self.server.server_close()
            except Exception:
                pass
            import subprocess
            subprocess.Popen([sys.executable] + sys.argv)
            os._exit(0)

        threading.Thread(target=_restart_server, daemon=True).start()

    def handle_delete_tag_category(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            item_type = str(req_data.get("type", "tag")).strip().lower()
            name = str(req_data.get("name", "")).strip()
        except Exception:
            self.send_error(400, "Invalid JSON payload")
            return

        if not name:
            self.send_error(400, "Missing 'name' parameter")
            return

        is_genre = item_type in ("genre", "category", "cat")
        field_name = "genres" if is_genre else "tags"
        target_lower = name.lower()

        manifest = load_manifest()
        files = manifest.get("files", {})
        updated_keys = []

        for k, finfo in files.items():
            meta = finfo.get("metadata") or {}
            raw_list = meta.get(field_name) or []
            if isinstance(raw_list, str):
                raw_list = [x.strip() for x in raw_list.split(",") if x.strip()]
            elif not isinstance(raw_list, list):
                raw_list = []

            has_match = any(str(x).strip().lower() == target_lower for x in raw_list)
            if not has_match:
                continue

            new_list = [str(x).strip() for x in raw_list if str(x).strip().lower() != target_lower]
            meta[field_name] = new_list
            finfo["metadata"] = meta
            finfo["verifier"] = crypto_engine.create_verifier(self.password)
            try:
                metadata_helper.write_nfo_files("jellyfin_library", k, meta)
                for alias in finfo.get("aliases", []):
                    alias_key = str(Path(k).parent / alias) if str(Path(k).parent) != "." else alias
                    metadata_helper.write_nfo_files("jellyfin_library", alias_key, meta)
            except Exception as e:
                logger.warning("Error writing NFO for %s: %s", k, e)

            updated_keys.append(k)

        if updated_keys:
            save_manifest(manifest)
            activity_tracker.tracker.add_event(
                "info",
                f"{'Category' if is_genre else 'Tag'} Deleted",
                f"Removed {'category' if is_genre else 'tag'} '{name}' across {len(updated_keys)} video(s)"
            )

            def _bg_sync(keys):
                try:
                    m = load_manifest()
                    repos_touched = set()
                    for item_key in keys:
                        f = m.get("files", {}).get(item_key)
                        if not f:
                            continue
                        repo_id = f.get("repo_id", f"{self.hf_username}/{self.default_repo}")
                        repos_touched.add(repo_id)
                        api = self.acc_mgr.get_api_for_repo(repo_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else HfApi(token=self.token)
                        blob_path = f.get("blob_path") or f.get("remote_path")
                        if blob_path:
                            record = dict(f)
                            record["full_key"] = item_key
                            metadata_helper.sync_item_metadata_to_hf(api, repo_id, blob_path, record, self.password)
                    for r_id in repos_touched:
                        api = self.acc_mgr.get_api_for_repo(r_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else HfApi(token=self.token)
                        repo_files = {k: v for k, v in m.get("files", {}).items() if v.get("repo_id") == r_id}
                        # Master Index Only mode: canonical index updated via sync_master_index_to_cloud
                        self.sync_master_index_to_cloud()
                except Exception as ex:
                    logger.warning("Background delete HF sync warning: %s", ex)

            threading.Thread(target=_bg_sync, args=(updated_keys,), daemon=True).start()

        resp = {
            "success": True,
            "type": "genre" if is_genre else "tag",
            "name": name,
            "updated_count": len(updated_keys),
            "message": f"Successfully deleted {'category' if is_genre else 'tag'} '{name}' from {len(updated_keys)} video(s)."
        }
        resp_body = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_rename_tag_category(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            item_type = str(req_data.get("type", "genre")).strip().lower()
            old_name = str(req_data.get("old_name", "")).strip()
            new_name = str(req_data.get("new_name", "")).strip()
        except Exception:
            self.send_error(400, "Invalid JSON payload")
            return

        if not old_name or not new_name:
            self.send_error(400, "Both 'old_name' and 'new_name' are required")
            return

        is_genre = item_type in ("genre", "category", "cat")
        field_name = "genres" if is_genre else "tags"
        old_lower = old_name.lower()

        manifest = load_manifest()
        files = manifest.get("files", {})
        updated_keys = []

        for k, finfo in files.items():
            meta = finfo.get("metadata") or {}
            raw_list = meta.get(field_name) or []
            if isinstance(raw_list, str):
                raw_list = [x.strip() for x in raw_list.split(",") if x.strip()]
            elif not isinstance(raw_list, list):
                raw_list = []

            has_match = any(str(x).strip().lower() == old_lower for x in raw_list)
            if not has_match:
                continue

            new_list = []
            seen = set()
            for x in raw_list:
                s = str(x).strip()
                if not s:
                    continue
                if s.lower() == old_lower:
                    s = new_name
                if s.lower() not in seen:
                    seen.add(s.lower())
                    new_list.append(s)

            meta[field_name] = new_list
            finfo["metadata"] = meta
            finfo["verifier"] = crypto_engine.create_verifier(self.password)
            try:
                metadata_helper.write_nfo_files("jellyfin_library", k, meta)
                for alias in finfo.get("aliases", []):
                    alias_key = str(Path(k).parent / alias) if str(Path(k).parent) != "." else alias
                    metadata_helper.write_nfo_files("jellyfin_library", alias_key, meta)
            except Exception as e:
                logger.warning("Error writing NFO for %s: %s", k, e)
            updated_keys.append(k)

        if updated_keys:
            save_manifest(manifest)
            activity_tracker.tracker.add_event(
                "info",
                f"{'Category' if is_genre else 'Tag'} Renamed",
                f"Renamed {'category' if is_genre else 'tag'} '{old_name}' to '{new_name}' across {len(updated_keys)} video(s)"
            )

            def _bg_sync(keys):
                try:
                    m = load_manifest()
                    repos_touched = set()
                    for item_key in keys:
                        f = m.get("files", {}).get(item_key)
                        if not f:
                            continue
                        repo_id = f.get("repo_id", f"{self.hf_username}/{self.default_repo}")
                        repos_touched.add(repo_id)
                        api = self.acc_mgr.get_api_for_repo(repo_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else HfApi(token=self.token)
                        blob_path = f.get("blob_path") or f.get("remote_path")
                        if blob_path:
                            record = dict(f)
                            record["full_key"] = item_key
                            metadata_helper.sync_item_metadata_to_hf(api, repo_id, blob_path, record, self.password)
                    for r_id in repos_touched:
                        api = self.acc_mgr.get_api_for_repo(r_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else HfApi(token=self.token)
                        repo_files = {k: v for k, v in m.get("files", {}).items() if v.get("repo_id") == r_id}
                        # Master Index Only mode: canonical index updated via sync_master_index_to_cloud
                        self.sync_master_index_to_cloud()
                except Exception as ex:
                    logger.warning("Background rename HF sync warning: %s", ex)

            threading.Thread(target=_bg_sync, args=(updated_keys,), daemon=True).start()

        resp = {
            "success": True,
            "type": "genre" if is_genre else "tag",
            "old_name": old_name,
            "new_name": new_name,
            "updated_count": len(updated_keys),
            "message": f"Successfully renamed {'category' if is_genre else 'tag'} '{old_name}' to '{new_name}' across {len(updated_keys)} video(s)."
        }
        resp_body = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_open_sync_folder(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else "{}"
        try:
            req_data = json.loads(body) if body else {}
            rel_folder = str(req_data.get("folder_path", "")).strip().replace("\\", "/")
        except Exception:
            rel_folder = ""

        # Normalize relative path to avoid path traversal
        clean_parts = [p for p in rel_folder.split("/") if p and p not in (".", "..")]
        clean_rel = os.path.join(*clean_parts) if clean_parts else ""

        sync_base = os.path.abspath("videos_to_sync")
        target_dir = os.path.normpath(os.path.join(sync_base, clean_rel))

        # Security check: must reside inside sync_base
        if not target_dir.startswith(sync_base):
            target_dir = sync_base

        try:
            os.makedirs(target_dir, exist_ok=True)
            if os.name == "nt":
                try:
                    os.startfile(target_dir)
                except Exception:
                    pass
                try:
                    subprocess.Popen(["explorer.exe", target_dir])
                except Exception:
                    pass
            else:
                try:
                    subprocess.Popen(["xdg-open", target_dir])
                except Exception:
                    pass

            rel_display = os.path.relpath(target_dir, os.getcwd()).replace("\\", "/")
            resp_body = json.dumps({
                "success": True,
                "path": target_dir,
                "rel_path": rel_display,
                "message": f"Opened sync folder: {rel_display}"
            }).encode("utf-8")
        except Exception as e:
            resp_body = json.dumps({"success": False, "error": str(e)}).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_merge_videos(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            keep_key = str(req_data.get("keep_key", "")).strip()
            discard_keys = req_data.get("discard_keys", [])
            delete_remote = bool(req_data.get("delete_remote", True))
            target_title = str(req_data.get("target_title", "")).strip()
        except Exception:
            self.send_error(400, "Invalid JSON payload")
            return

        if not keep_key:
            self.send_error(400, "Missing 'keep_key' in request")
            return

        if not isinstance(discard_keys, list) or not discard_keys:
            self.send_error(400, "Missing or empty 'discard_keys' list")
            return

        manifest = load_manifest()
        files = manifest.get("files", {})
        if keep_key not in files:
            self.send_error(404, f"Keep key '{keep_key}' not found in manifest")
            return

        keep_record = files[keep_key]
        keep_record.setdefault("extra_media_hashes", [])
        keep_record.setdefault("aliases", [])
        keep_record.setdefault("file_names", [keep_key])
        if target_title:
            keep_record.setdefault("metadata", {})
            keep_record["metadata"]["title"] = target_title

        existing_hashes = set()
        for h in keep_record.get("extra_media_hashes", []):
            val = h.get("hash") if isinstance(h, dict) else h
            if val:
                existing_hashes.add(val)
        primary_hash = keep_record.get("media_hash") or keep_record.get("content_hash")
        if primary_hash:
            existing_hashes.add(primary_hash)

        merged_hashes = []
        discarded_info_list = []

        for disc_key in discard_keys:
            if disc_key == keep_key:
                continue
            if disc_key not in files:
                continue

            disc_record = files[disc_key]
            disc_hash = disc_record.get("media_hash") or disc_record.get("content_hash")
            disc_name = Path(disc_key).name
            res_label = (disc_record.get("media_info") or {}).get("resolution_label") or (disc_record.get("metadata") or {}).get("resolution") or ""
            size_bytes = disc_record.get("orig_size") or 0

            # 0. Carry over genres and tags from discarded video
            disc_meta = disc_record.get("metadata", {})
            keep_meta = keep_record.setdefault("metadata", {})
            for g in disc_meta.get("genres", []):
                if g and g not in keep_meta.setdefault("genres", []):
                    keep_meta["genres"].append(g)
            for t in disc_meta.get("tags", []):
                if t and t not in keep_meta.setdefault("tags", []):
                    keep_meta["tags"].append(t)

            # 1. Add discarded video's media hash to keep_record["extra_media_hashes"]
            if disc_hash and disc_hash not in existing_hashes:
                entry = {
                    "hash": disc_hash,
                    "source_key": disc_key,
                    "source_name": disc_name,
                    "merged_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "resolution": res_label,
                    "orig_size": size_bytes,
                    "repo_id": disc_record.get("repo_id", "")
                }
                keep_record["extra_media_hashes"].append(entry)
                existing_hashes.add(disc_hash)
                merged_hashes.append(disc_hash)

            # 2. Carry over any hashes previously merged into the discarded record
            for prev_h in disc_record.get("extra_media_hashes", []):
                val = prev_h.get("hash") if isinstance(prev_h, dict) else prev_h
                if val and val not in existing_hashes:
                    entry = prev_h if isinstance(prev_h, dict) else {
                        "hash": val,
                        "source_key": disc_key,
                        "source_name": disc_name,
                        "merged_at": time.strftime("%Y-%m-%d %H:%M:%S")
                    }
                    keep_record["extra_media_hashes"].append(entry)
                    existing_hashes.add(val)
                    merged_hashes.append(val)

            # 3. Add filename and aliases to keep_record
            if disc_name and disc_name not in keep_record["aliases"] and disc_name != Path(keep_key).name:
                keep_record["aliases"].append(disc_name)
            for a in disc_record.get("aliases", []):
                if a and a not in keep_record["aliases"] and a != Path(keep_key).name:
                    keep_record["aliases"].append(a)

            # Also maintain file_names list
            if disc_key not in keep_record["file_names"]:
                keep_record["file_names"].append(disc_key)

            # 4. Remove physical files in jellyfin_library for discarded video
            library_dir = "jellyfin_library"
            try:
                disc_strm = os.path.join(library_dir, f"{disc_key}.strm")
                if os.path.exists(disc_strm):
                    os.remove(disc_strm)
                for nfo_suffix in [".nfo", ".strm.nfo"]:
                    disc_nfo = os.path.join(library_dir, f"{disc_key}{nfo_suffix}")
                    if os.path.exists(disc_nfo):
                        os.remove(disc_nfo)
                disc_thumb = os.path.join(library_dir, f"{Path(disc_key).stem}-thumb.jpg")
                if os.path.exists(disc_thumb):
                    os.remove(disc_thumb)
            except Exception as e:
                logger.warning("Error cleaning local library files for discarded %s: %s", disc_key, e)

            discarded_info_list.append({
                "key": disc_key,
                "repo_id": disc_record.get("repo_id"),
                "blob_path": disc_record.get("blob_path"),
                "audio": disc_record.get("audio", {}).get("blob_path"),
                "storyboard": disc_record.get("storyboard", {}).get("blob_path"),
                "screens": disc_record.get("screens", {}).get("blob_path"),
            })

            # 5. Remove discarded key from manifest
            del files[disc_key]

        # 6. Register all merged hashes in manifest["media_hashes"] pointing to keep_record
        if "media_hashes" not in manifest:
            manifest["media_hashes"] = {}
        if primary_hash:
            manifest["media_hashes"][primary_hash] = keep_record
        for mh in merged_hashes:
            manifest["media_hashes"][mh] = keep_record

        # 7. Update verifier and save manifest
        keep_record["verifier"] = crypto_engine.create_verifier(self.password)
        files[keep_key] = keep_record
        save_manifest(manifest)

        # 8. Re-write Jellyfin NFO files for keep_record
        try:
            metadata_helper.write_nfo_files(library_dir, keep_key, keep_record.get("metadata", {}))
        except Exception as e:
            logger.warning("Error updating NFO for %s after merge: %s", keep_key, e)

        # 9. Background thread: Sync keep_record metadata to HF & Delete remote discarded blobs
        def _bg_merge_cloud(keep_k, keep_rec, disc_list, do_del_remote):
            try:
                repo_id = keep_rec.get("repo_id", f"{self.hf_username}/{self.default_repo}")
                if hasattr(self, "acc_mgr") and self.acc_mgr and not self.acc_mgr.can_write_to_repo(repo_id):
                    logger.info("Skipping HF cloud merge sync for %s: Token lacks write permissions.", repo_id)
                else:
                    api = self.acc_mgr.get_api_for_repo(repo_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else HfApi(token=self.token)
                    blob_path = keep_rec.get("blob_path") or keep_rec.get("remote_path")
                    if blob_path:
                        metadata_helper.sync_item_metadata_to_hf(api, repo_id, blob_path, keep_rec, self.password)

                    # Sync repo index for keep_record
                    m = load_manifest()
                    repo_files = {k: v for k, v in m.get("files", {}).items() if v.get("repo_id") == repo_id}
                    # Master Index Only mode: canonical index updated via sync_master_index_to_cloud
                    self.sync_master_index_to_cloud()

                # Delete remote discarded blobs if requested
                if do_del_remote:
                    for disc_item in disc_list:
                        d_repo = disc_item.get("repo_id")
                        if not d_repo:
                            continue
                        if hasattr(self, "acc_mgr") and self.acc_mgr and not self.acc_mgr.can_write_to_repo(d_repo):
                            logger.info("Skipping HF cloud delete for discarded merge item in %s: Token lacks write permissions.", d_repo)
                            continue
                        d_api = self.acc_mgr.get_api_for_repo(d_repo) if (hasattr(self, "acc_mgr") and self.acc_mgr) else HfApi(token=self.token)
                        blobs_to_delete = [
                            disc_item.get("blob_path"),
                            disc_item.get("audio"),
                            disc_item.get("storyboard"),
                            disc_item.get("screens"),
                        ]
                        if disc_item.get("blob_path"):
                            blobs_to_delete.append(disc_item.get("blob_path").replace(".enc", ".meta.enc"))

                        for b in blobs_to_delete:
                            if b:
                                try:
                                    d_api.delete_file(path_in_repo=b, repo_id=d_repo, repo_type="dataset")
                                except Exception:
                                    pass

                        # Update repo manifest for discarded repo if different from keep_repo
                        if d_repo != repo_id:
                            d_repo_files = {k: v for k, v in m.get("files", {}).items() if v.get("repo_id") == d_repo}
                            # Master Index Only mode: canonical index updated via sync_master_index_to_cloud
                            self.sync_master_index_to_cloud()
            except Exception as ex:
                logger.warning("Background cloud merge sync warning: %s", ex)

        threading.Thread(target=_bg_merge_cloud, args=(keep_key, dict(keep_record), discarded_info_list, delete_remote), daemon=True).start()

        activity_tracker.tracker.add_event(
            "info",
            "Videos Merged",
            f"Merged {len(discarded_info_list)} duplicate(s) into '{Path(keep_key).name}'. Retained {len(merged_hashes)} sparse hash(es)."
        )

        resp = {
            "success": True,
            "keep_key": keep_key,
            "merged_count": len(discarded_info_list),
            "merged_hashes": merged_hashes,
            "extra_media_hashes": keep_record.get("extra_media_hashes", []),
            "message": f"Successfully merged {len(discarded_info_list)} duplicate video(s) into '{Path(keep_key).name}'."
        }
        resp_body = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_unlink_video_hash(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            key = str(req_data.get("key", "")).strip()
            target_hash = str(req_data.get("hash", "")).strip()
        except Exception:
            self.send_error(400, "Invalid JSON payload")
            return

        if not key or not target_hash:
            self.send_error(400, "Both 'key' and 'hash' are required")
            return

        manifest = load_manifest()
        files = manifest.get("files", {})
        if key not in files:
            self.send_error(404, f"Video key '{key}' not found in manifest")
            return

        record = files[key]
        extra_hashes = record.get("extra_media_hashes", [])
        
        # Remove target hash
        new_extra = []
        removed = False
        for h in extra_hashes:
            val = h.get("hash") if isinstance(h, dict) else h
            if val == target_hash:
                removed = True
            else:
                new_extra.append(h)

        record["extra_media_hashes"] = new_extra

        # Remove from manifest["media_hashes"] if it points to this record
        if "media_hashes" in manifest and target_hash in manifest["media_hashes"]:
            del manifest["media_hashes"][target_hash]

        record["verifier"] = crypto_engine.create_verifier(self.password)
        files[key] = record
        save_manifest(manifest)

        # Background sync updated metadata to HF
        def _bg_sync(k, rec):
            try:
                repo_id = rec.get("repo_id", f"{self.hf_username}/{self.default_repo}")
                api = self.acc_mgr.get_api_for_repo(repo_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else HfApi(token=self.token)
                blob_path = rec.get("blob_path") or rec.get("remote_path")
                if blob_path:
                    metadata_helper.sync_item_metadata_to_hf(api, repo_id, blob_path, rec, self.password)
            except Exception as ex:
                logger.warning("Background unlink hash sync warning: %s", ex)

        threading.Thread(target=_bg_sync, args=(key, dict(record)), daemon=True).start()

        activity_tracker.tracker.add_event(
            "info",
            "Hash Unlinked",
            f"Unlinked fingerprint '{target_hash[:16]}...' from '{Path(key).name}'."
        )

        resp = {
            "success": True,
            "removed": removed,
            "key": key,
            "hash": target_hash,
            "extra_media_hashes": new_extra,
            "message": f"Successfully unlinked hash '{target_hash[:16]}...'. This video file can now be synced independently again."
        }
        resp_body = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_remove_alias(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            key = req_data.get("key")
            alias_to_remove = str(req_data.get("alias", "")).strip()
        except Exception:
            self.send_error(400, "Invalid JSON payload")
            return

        if not key or not alias_to_remove:
            self.send_error(400, "Missing 'key' or 'alias' in request")
            return

        manifest = load_manifest()
        if key not in manifest.get("files", {}):
            found_key = None
            for k in manifest.get("files", {}):
                if k == key or Path(k).name == Path(key).name or k.endswith("/" + Path(key).name):
                    found_key = k
                    break
            if found_key:
                key = found_key
            else:
                self.send_error(404, f"Video key '{key}' not found in manifest")
                return

        rec = manifest["files"][key]
        blob_path = rec.get("blob_path") or rec.get("remote_path")
        media_hash = rec.get("media_hash") or rec.get("content_hash")
        repo_id = rec.get("repo_id", f"{self.hf_username}/{self.default_repo}")

        # Find all keys referencing this blob
        related_keys = [
            k for k, v in manifest.get("files", {}).items()
            if (blob_path and v.get("blob_path") == blob_path) or (media_hash and (v.get("media_hash") == media_hash or v.get("content_hash") == media_hash))
        ]
        if key not in related_keys:
            related_keys.append(key)

        current_aliases = list(rec.get("aliases") or [Path(key).name])
        current_file_names = list(rec.get("file_names") or [key])

        alias_basename = Path(alias_to_remove).name
        new_aliases = [a for a in current_aliases if a != alias_to_remove and a != alias_basename]
        new_file_names = [fn for fn in current_file_names if fn != alias_to_remove and Path(fn).name != alias_basename]

        if not new_aliases:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({
                "success": False,
                "message": "Cannot remove the only remaining filename. To completely delete this video, click 'Delete' on the dashboard."
            }).encode("utf-8"))
            return

        # If key itself is being removed, designate a surviving alias key
        keys_to_pop = [
            k for k in related_keys
            if k == alias_to_remove or Path(k).name == alias_basename or k.endswith("/" + alias_basename)
        ]

        if key in keys_to_pop or Path(key).name == alias_basename:
            surviving_key = None
            for fn in new_file_names:
                if fn in manifest["files"] and fn not in keys_to_pop:
                    surviving_key = fn
                    break
            if not surviving_key and new_file_names:
                surviving_key = new_file_names[0]
            if not surviving_key and new_aliases:
                parent_dir = Path(key).parent.as_posix()
                surviving_key = f"{parent_dir}/{new_aliases[0]}".replace("\\", "/").lstrip("/")

            if surviving_key and surviving_key != key:
                surv_rec = dict(rec)
                surv_rec["full_key"] = surviving_key
                surv_rec["aliases"] = list(new_aliases)
                surv_rec["file_names"] = list(new_file_names)
                manifest["files"][surviving_key] = surv_rec
                key = surviving_key

        # Remove popped alias keys
        for k in keys_to_pop:
            if k != key:
                manifest["files"].pop(k, None)

        # Update remaining records
        for k in list(manifest.get("files", {}).keys()):
            v = manifest["files"][k]
            if (blob_path and v.get("blob_path") == blob_path) or (media_hash and (v.get("media_hash") == media_hash or v.get("content_hash") == media_hash)):
                v["aliases"] = list(new_aliases)
                v["file_names"] = list(new_file_names)
                if "metadata" in v and v["metadata"]:
                    v["metadata"]["aliases"] = list(new_aliases)
                    v["metadata"]["file_names"] = list(new_file_names)

        if media_hash and "media_hashes" in manifest:
            manifest["media_hashes"][media_hash] = manifest["files"][key]

        save_manifest(manifest)

        # Delete local .strm and .nfo files for the removed alias if Jellyfin sync enabled
        if account_manager.is_jellyfin_sync_enabled():
            for strm_candidate in Path("jellyfin_library").glob("**/*.strm"):
                if strm_candidate.name in (f"{alias_basename}.strm", f"{alias_to_remove}.strm"):
                    try:
                        strm_candidate.unlink()
                    except Exception as e:
                        print(f"[!] Warning deleting alias strm: {e}")

            metadata_helper.delete_nfo_files("jellyfin_library", alias_to_remove)
            metadata_helper.delete_nfo_files("jellyfin_library", alias_basename)
            for nfo_candidate in Path("jellyfin_library").glob(f"**/*{alias_basename}*.nfo"):
                try:
                    nfo_candidate.unlink()
                except Exception:
                    pass

            # Re-write .nfo files for remaining filenames so Jellyfin no longer lists removed alias
            for fn in new_file_names:
                clean_rec = manifest["files"].get(fn) or manifest["files"][key]
                metadata_helper.write_nfo_files("jellyfin_library", fn, clean_rec.get("metadata", {}))

        # Sync updated metadata to Hugging Face
        api = self.acc_mgr.get_api_for_repo(repo_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else HfApi(token=self.token)
        if blob_path:
            try:
                record = dict(manifest["files"][key])
                record["full_key"] = key
                metadata_helper.sync_item_metadata_to_hf(api, repo_id, blob_path, record, self.password)
            except Exception as e:
                print(f"[!] Warning syncing updated aliases to HF: {e}")

        activity_tracker.tracker.add_event(
            "info",
            f"Filename Removed: {alias_basename}",
            f"Removed unsuitable filename from '{key}'. Remaining names: {', '.join(new_aliases)}",
            account=rec.get("account", self.hf_username)
        )

        resp_body = json.dumps({
            "success": True,
            "key": key,
            "removed_alias": alias_to_remove,
            "aliases": new_aliases,
            "file_names": new_file_names,
            "message": f"Successfully removed unsuitable filename '{alias_basename}'."
        }).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)


    def handle_add_alias(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            key = req_data.get("key")
            new_alias = str(req_data.get("alias", "")).strip()
        except Exception:
            self.send_error(400, "Invalid JSON payload")
            return

        if not key or not new_alias:
            self.send_error(400, "Missing 'key' or 'alias' in request")
            return

        manifest = load_manifest()
        if key not in manifest.get("files", {}):
            found_key = None
            for k in manifest.get("files", {}):
                if k == key or Path(k).name == Path(key).name or k.endswith("/" + Path(key).name):
                    found_key = k
                    break
            if found_key:
                key = found_key
            else:
                self.send_error(404, f"Video key '{key}' not found in manifest")
                return

        rec = manifest["files"][key]
        blob_path = rec.get("blob_path") or rec.get("remote_path")
        media_hash = rec.get("media_hash") or rec.get("content_hash")
        repo_id = rec.get("repo_id", f"{self.hf_username}/{self.default_repo}")

        # Ensure valid extension
        orig_ext = Path(key).suffix or ".mp4"
        if not Path(new_alias).suffix:
            new_alias += orig_ext

        new_basename = Path(new_alias).name
        current_aliases = list(rec.get("aliases") or [Path(key).name])
        current_file_names = list(rec.get("file_names") or [key])

        if new_basename not in current_aliases:
            current_aliases.append(new_basename)
        if new_alias not in current_file_names:
            current_file_names.append(new_alias)

        # Create alias entry in manifest["files"]
        parent_dir = Path(key).parent.as_posix()
        alias_key = f"{parent_dir}/{new_basename}".replace("\\", "/").lstrip("/")
        alias_rec = dict(rec)
        alias_rec["full_key"] = alias_key
        alias_rec["file_names"] = current_file_names
        alias_rec["aliases"] = current_aliases
        manifest["files"][alias_key] = alias_rec

        # Update all related records
        for k in list(manifest.get("files", {}).keys()):
            v = manifest["files"][k]
            if (blob_path and v.get("blob_path") == blob_path) or (media_hash and (v.get("media_hash") == media_hash or v.get("content_hash") == media_hash)):
                v["aliases"] = list(current_aliases)
                v["file_names"] = list(current_file_names)
                if "metadata" in v and v["metadata"]:
                    v["metadata"]["aliases"] = list(current_aliases)
                    v["metadata"]["file_names"] = list(current_file_names)

        if media_hash and "media_hashes" in manifest:
            manifest["media_hashes"][media_hash] = manifest["files"][key]

        save_manifest(manifest)

        # Create .strm and .nfo files for new alias if Jellyfin sync enabled
        if account_manager.is_jellyfin_sync_enabled():
            strm_path = Path("jellyfin_library") / f"{alias_key}.strm"
            strm_path.parent.mkdir(parents=True, exist_ok=True)
            with open(strm_path, "w", encoding="utf-8") as sf:
                sf.write(f"http://127.0.0.1:8080/stream/{alias_key}\n")

            # Write .nfo files
            meta = rec.get("metadata") or metadata_helper.extract_default_metadata(key)
            meta["aliases"] = current_aliases
            meta["file_names"] = current_file_names
            for fn in current_file_names:
                metadata_helper.write_nfo_files("jellyfin_library", fn, meta)

        # Sync to Hugging Face
        api = self.acc_mgr.get_api_for_repo(repo_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else HfApi(token=self.token)
        if blob_path:
            try:
                record = dict(manifest["files"][key])
                record["full_key"] = key
                metadata_helper.sync_item_metadata_to_hf(api, repo_id, blob_path, record, self.password)
            except Exception as e:
                print(f"[!] Warning syncing updated aliases to HF: {e}")

        activity_tracker.tracker.add_event(
            "info",
            f"Filename Added: {new_basename}",
            f"Added suitable filename to '{key}'. Active names: {', '.join(current_aliases)}",
            account=rec.get("account", self.hf_username)
        )

        resp_body = json.dumps({
            "success": True,
            "key": key,
            "added_alias": new_basename,
            "aliases": current_aliases,
            "file_names": current_file_names,
            "message": f"Successfully added filename '{new_basename}'."
        }).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def _send_stream_chunk(self, data_dict: dict):
        try:
            line = json.dumps(data_dict) + "\n"
            raw = line.encode("utf-8")
            chunk = f"{len(raw):X}\r\n".encode("ascii") + raw + b"\r\n"
            self.wfile.write(chunk)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _finish_stream_chunked(self):
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def handle_resync_stream(self):
        """
        Streaming NDJSON endpoint for Resync All Accounts with real-time live terminal output
        and strict zero-partial Master Index integrity verification.
        """
        import time, threading, zlib
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        stream_lock = threading.Lock()
        def send_chunk(data: dict):
            with stream_lock:
                self._send_stream_chunk(data)

        def log_msg(msg: str, stage: str = "running", percent: int = None, **kwargs):
            payload = {
                "timestamp": time.strftime("%H:%M:%S"),
                "stage": stage,
                "message": msg
            }
            if percent is not None:
                payload["percent"] = percent
            payload.update(kwargs)
            send_chunk(payload)

        t_overall_start = time.time()

        # Step 1: Initialization & Integrity Banner
        log_msg("🚀 Initiating Cloud Resync across all connected Hugging Face accounts...", stage="init", percent=3)
        log_msg("🛡️ STRICT INTEGRITY PROTECTION ACTIVE: All repository queries must succeed 100%.", stage="init", percent=5)
        log_msg("   If ANY repository fails or times out, the Master Index will NOT be modified.", stage="init", percent=6)

        if not hasattr(self, "acc_mgr") or not self.acc_mgr:
            self.acc_mgr = account_manager.AccountManager()

        accounts = self.acc_mgr.get_all_accounts()
        if not accounts:
            log_msg("❌ ERROR: No Hugging Face accounts configured!", stage="error", percent=10)
            log_msg("🛑 Master Index NOT modified. Resync aborted.", stage="aborted", percent=10, abort=True)
            send_chunk({"stage": "failed", "success": False, "percent": 10, "message": "No accounts configured."})
            self._finish_stream_chunked()
            return

        master_repo = self.acc_mgr.get_master_repo_id()
        log_msg(f"👑 Canonical Master Repository: {master_repo}", stage="info", percent=8)

        # Step 2: Validate Account Credentials & Permissions
        log_msg(f"🔑 Validating {len(accounts)} Hugging Face account(s)...", stage="auth", percent=10)
        auth_failures = []
        for acc in accounts:
            u = acc["username"]
            if not acc.get("enabled", True):
                log_msg(f"⏭️ Account '{u}' is disabled in settings. Skipping.", stage="auth")
                continue
            try:
                who = acc["api"].whoami()
                u_type = who.get("type", "user")
                can_w = acc.get("can_write", True)
                log_msg(f"   ✅ Account '{u}' ({u_type}) verified successfully (Write: {can_w})", stage="auth")
            except Exception as e:
                err_str = str(e)
                auth_failures.append({"user": u, "error": err_str})
                log_msg(f"   ❌ Account '{u}' authentication failed: {err_str}", stage="auth_error")

        if auth_failures:
            log_msg("=" * 60, stage="error")
            log_msg("🛑 CRITICAL SAFETY ABORT: One or more Hugging Face account tokens failed authentication!", stage="error")
            for af in auth_failures:
                log_msg(f"   ❌ User: {af['user']} -> {af['error']}", stage="error")
            log_msg("🛡️ Master Index Protection: master vault_index.enc and local manifest were NOT modified.", stage="error")
            log_msg("=" * 60, stage="error")
            send_chunk({"stage": "failed", "success": False, "percent": 15, "message": "Account authentication failed. Master Index was untouched.", "auth_failures": auth_failures})
            self._finish_stream_chunked()
            return

        # Step 3: Discover Target Dataset Repositories
        log_msg("🔍 Discovering dataset repositories across all enabled accounts...", stage="discovery", percent=15)
        repo_targets = []
        discovery_failures = []
        for acc in accounts:
            if not acc.get("enabled", True):
                continue
            u = acc["username"]
            token = acc["token"]
            try:
                ds_list = list(acc["api"].list_datasets(author=u))
                u_repos = []
                for ds in ds_list:
                    if ds.id != master_repo:
                        repo_targets.append((ds.id, token, u))
                        u_repos.append(ds.id)
                log_msg(f"📦 Account '{u}': Found {len(u_repos)} data repository/repositories ({', '.join([r.split('/')[-1] for r in u_repos]) if u_repos else 'None'})", stage="discovery", percent=20)
            except Exception as de:
                err_str = str(de)
                discovery_failures.append({"user": u, "error": err_str})
                log_msg(f"   ❌ Failed to discover repositories for '{u}': {err_str}", stage="discovery_error")

        if discovery_failures:
            log_msg("=" * 60, stage="error")
            log_msg("🛑 CRITICAL SAFETY ABORT: Repository discovery failed on one or more accounts!", stage="error")
            for df in discovery_failures:
                log_msg(f"   ❌ User: {df['user']} -> {df['error']}", stage="error")
            log_msg("🛡️ Master Index was NOT modified to prevent data loss.", stage="error")
            log_msg("=" * 60, stage="error")
            send_chunk({"stage": "failed", "success": False, "percent": 22, "message": "Repository discovery failed. Master Index was untouched.", "discovery_failures": discovery_failures})
            self._finish_stream_chunked()
            return

        total_repos = len(repo_targets)
        log_msg(f"📊 Target Repositories: {total_repos} repositories queued for parallel sidecar scanning.", stage="discovery", percent=25, total_repos=total_repos)

        if total_repos == 0:
            log_msg("ℹ️ No folder repositories found. Checking Master Index directly...", stage="discovery", percent=30)

        # Step 4: Dispatch Parallel Workers with Live Progress
        workers_count = max(1, min(total_repos, 4))
        log_msg(f"⚡ Spawning {workers_count} bounded worker(s) (GIT_LFS_SKIP_SMUDGE=1, 80ms staggered dispatch)...", stage="dispatch", percent=28)
        log_msg("💡 Skipping all multi-GB media blobs: fetching only encrypted lightweight .meta.enc sidecars.", stage="dispatch", percent=30)

        completed_count = 0
        total_sidecars = 0

        def on_worker_progress(event_type, data):
            nonlocal completed_count, total_sidecars
            wid = data.get("worker_id", 1)
            r_id = data.get("repo_id", "")
            r_short = r_id.split("/")[-1] if "/" in r_id else r_id

            if event_type == "worker_start":
                log_msg(f"[Worker-{wid}] 🚀 Connecting to '{r_short}' ({data.get('user')})...", stage="worker_start")
            elif event_type == "worker_success":
                st = data.get("stats", {})
                cnt = data.get("count", 0)
                total_sidecars += cnt
                completed_count += 1
                curr_pct = int(30 + (completed_count / max(1, total_repos)) * 45)
                source_lbl = "vault_index" if st.get("source") == "vault_index" else f"Git clone {st.get('clone_time', 0)}s"
                log_msg(
                    f"[Worker-{wid}] ✅ '{r_short}' SUCCESS: {cnt} sidecar(s) verified in {st.get('elapsed', 0)}s ({source_lbl}) | Status: 200 OK",
                    stage="worker_success",
                    percent=curr_pct,
                    completed_repos=completed_count,
                    total_repos=total_repos,
                    total_sidecars=total_sidecars
                )
            elif event_type == "worker_error":
                completed_count += 1
                curr_pct = int(30 + (completed_count / max(1, total_repos)) * 45)
                err = data.get("error", "Unknown error")
                log_msg(
                    f"[Worker-{wid}] ❌ FAILED on '{r_short}': {err}",
                    stage="worker_error",
                    percent=curr_pct,
                    completed_repos=completed_count,
                    total_repos=total_repos,
                    error=err
                )

        t_fetch_start = time.time()
        parallel_records, fetch_errors, repo_stats = self.acc_mgr.batch_fetch_all_repos_parallel(
            repo_targets, max_workers=workers_count, progress_callback=on_worker_progress
        )
        t_fetch_elapsed = round(time.time() - t_fetch_start, 2)
        log_msg(f"⚡ Parallel scan finished in {t_fetch_elapsed}s. Sidecars retrieved: {len(parallel_records)} | Repos with errors: {len(fetch_errors)}", stage="scan_done", percent=76)

        # Step 5: STRICT VERIFICATION GATE
        if fetch_errors or len(repo_stats) < total_repos:
            log_msg("=" * 60, stage="error")
            log_msg(f"🛑 CRITICAL SAFETY ABORT: {len(fetch_errors)} repository query/queries failed!", stage="error")
            for fe in fetch_errors:
                log_msg(f"   ❌ Repo: {fe['repo_id']} -> Error: {fe['error']}", stage="error")
            log_msg("🛡️ STRICT ALL-OR-NOTHING POLICY TRIGGERED:", stage="error")
            log_msg("   Because not all repositories could be verified, the Master Index was NOT modified.", stage="error")
            log_msg("   Local manifest was NOT modified. Zero partial data written.", stage="error")
            log_msg("   This guarantees no video records are accidentally deleted or lost.", stage="error")
            log_msg("=" * 60, stage="error")
            send_chunk({
                "stage": "failed",
                "success": False,
                "percent": 78,
                "message": f"Resync aborted: {len(fetch_errors)} repo(s) failed. Master Index was preserved.",
                "fetch_errors": fetch_errors
            })
            self._finish_stream_chunked()
            return

        log_msg(f"🛡️ 100% Repository Verification PASSED! All {total_repos}/{total_repos} repositories succeeded with 0 errors.", stage="verified", percent=78)

        # Step 6: Tombstones & Local Manifest Merging
        log_msg("🧹 Loading local manifest and checking tombstone filters...", stage="merge", percent=80)
        manifest = load_manifest()
        tombstones = manifest.get("tombstones", {})
        log_msg(f"🔍 Checking tombstones: {len(tombstones)} active deletion tombstone(s) found.", stage="merge", percent=82)

        files = manifest.setdefault("files", {})
        media_hashes = manifest.setdefault("media_hashes", {})
        new_discovered = 0
        updated_count = 0
        tombstoned_ignored = 0

        for k, rec in parallel_records.items():
            if k in tombstones:
                tombstoned_ignored += 1
                continue
            if k not in files:
                files[k] = rec
                new_discovered += 1
            else:
                r_ts = float(rec.get("updated_at") or 0.0)
                l_ts = float(files[k].get("updated_at") or 0.0)
                if r_ts > l_ts:
                    files[k] = rec
                    updated_count += 1
            m_hash = rec.get("media_hash")
            if m_hash:
                media_hashes[m_hash] = rec

        if tombstoned_ignored > 0:
            log_msg(f"🗑️ Respected {tombstoned_ignored} tombstoned deletion(s) (permanently deleted files will not resurrect).", stage="merge", percent=85)
        log_msg(f"📝 Manifest merged: {new_discovered} new video(s) added, {updated_count} updated, {len(files)} total active video(s).", stage="merge", percent=87)

        save_manifest(manifest)
        log_msg("💾 upload_manifest.json saved to disk safely.", stage="merge", percent=89)

        # Step 7: Master Index Encryption & Upload
        log_msg(f"🔐 Building unified Master Index for '{master_repo}'...", stage="master_index", percent=90)
        try:
            master_api = self.acc_mgr.get_api_for_repo(master_repo)
            master_pw = self.acc_mgr.get_password_for_repo(master_repo)
            metadata_helper.sync_master_manifest_to_hf(master_api, master_repo, manifest, master_pw)
            log_msg(f"✅ Canonical Master Index successfully committed and synced to '{master_repo}'!", stage="master_index", percent=96)
        except Exception as me:
            err_m = str(me)
            log_msg(f"⚠️ Warning: Failed to upload Master Index to cloud: {err_m}", stage="master_index_error")

        # Step 8: Update Jellyfin Library pointers if enabled
        if account_manager.is_jellyfin_sync_enabled():
            log_msg("📺 Refreshing local Jellyfin library pointers (.strm / .nfo)...", stage="jellyfin", percent=98)
            try:
                for k, rec in files.items():
                    if rec.get("status") == "uploaded":
                        strm_p = Path("jellyfin_library") / f"{k}.strm"
                        strm_p.parent.mkdir(parents=True, exist_ok=True)
                        strm_p.write_text(f"http://127.0.0.1:8080/stream/{k}\n", encoding="utf-8")
                        meta = rec.get("metadata") or metadata_helper.extract_default_metadata(k)
                        metadata_helper.write_nfo_files("jellyfin_library", k, meta, media_info=rec.get("media_info"))
            except Exception as je:
                log_msg(f"⚠️ Jellyfin strm generation notice: {je}", stage="jellyfin")

        t_overall_total = round(time.time() - t_overall_start, 2)
        activity_tracker.tracker.add_event(
            "success",
            "Full Cloud Resync Complete",
            f"100% verified {total_repos} repos across {len(accounts)} accounts. Rebuilt Master Index ({len(files)} active videos) in {t_overall_total}s."
        )
        activity_tracker.tracker.notify_manifest_updated()

        log_msg("=" * 60, stage="complete")
        log_msg(f"🎉 RESYNC COMPLETE: Rebuilt Master Index with {len(files)} active video(s) across {total_repos} repositories in {t_overall_total}s!", stage="complete", percent=100)
        log_msg("=" * 60, stage="complete")

        send_chunk({
            "stage": "complete",
            "success": True,
            "percent": 100,
            "message": f"Successfully resynced {len(files)} video(s) across {total_repos} repository/repositories in {t_overall_total}s.",
            "total_files": len(files),
            "new_discovered": new_discovered,
            "updated_count": updated_count,
            "total_repos": total_repos,
            "total_accounts": len(accounts),
            "elapsed_seconds": t_overall_total
        })
        self._finish_stream_chunked()

    def purge_video_entry(self, key_to_delete: str, manifest: dict, force_cloud_delete: bool = False, sync_repo_index: bool = True) -> dict:
        """
        Exhaustively and permanently purges a video from A to Z across all systems:
        - Local storyboards cache (jpg, json, thumb, screens, audio)
        - Local Jellyfin library (.strm, .nfo, subtitles)
        - Memory cache & manifest entries
        - All alias, filename, and hash references across the entire manifest
        - Remote Hugging Face video blob and all sidecars (.meta.enc, .audio.m4a.enc, .storyboard.jpg.enc, .screens.jpg.enc, subtitles)
        - Remote repository index (vault_index.enc) immediately re-synced
        - Automatic deletion of empty HF vault datasets
        - Activity tracker notification and live logging
        """
        info = manifest.get("files", {}).get(key_to_delete, {})
        repo_id = info.get("repo_id", f"{self.hf_username}/{self.default_repo}")
        blob_path = info.get("blob_path") or info.get("remote_path")
        media_hash = info.get("media_hash") or info.get("content_hash") or info.get("fingerprint")
        safe_id = info.get("safe_id") or storyboard_helper.get_safe_id(key_to_delete)

        api = self.acc_mgr.get_api_for_repo(repo_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else HfApi(token=self.token)
        can_write = self.acc_mgr.can_write_to_repo(repo_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else True

        # 1. Clean local storyboards and audio cache
        if safe_id:
            for ext in (".jpg", ".json", "_thumb.jpg", "_screens.jpg"):
                sp = os.path.join("storyboards", f"{safe_id}{ext}")
                if os.path.exists(sp):
                    try:
                        os.remove(sp)
                    except Exception:
                        pass
            audio_sp = os.path.join("storyboards", "audio", f"{safe_id}.m4a")
            if os.path.exists(audio_sp):
                try:
                    os.remove(audio_sp)
                except Exception:
                    pass

        # 2. Clean Jellyfin library files (.strm, .nfo, subs) if Jellyfin sync enabled
        if account_manager.is_jellyfin_sync_enabled():
            strm_path = Path("jellyfin_library") / f"{key_to_delete}.strm"
            if strm_path.exists():
                try:
                    strm_path.unlink()
                except Exception:
                    pass
            metadata_helper.delete_nfo_files("jellyfin_library", key_to_delete)

        # 3. Clean memory cache and manifest file entry + add to tombstones
        FILE_CACHE.pop(key_to_delete, None)
        manifest.get("files", {}).pop(key_to_delete, None)
        manifest.setdefault("tombstones", {})[key_to_delete] = time.time()

        # 4. Clean all references from other file entries (aliases, file_names)
        base_del = Path(key_to_delete).name
        for ok, other_rec in manifest.get("files", {}).items():
            if "file_names" in other_rec and key_to_delete in other_rec["file_names"]:
                other_rec["file_names"].remove(key_to_delete)
            if "aliases" in other_rec and base_del in other_rec["aliases"]:
                other_rec["aliases"].remove(base_del)
            if "metadata" in other_rec and other_rec["metadata"]:
                other_rec["metadata"]["file_names"] = other_rec.get("file_names", [])
                other_rec["metadata"]["aliases"] = other_rec.get("aliases", [])

        # 5. Clean all hash table entries
        all_hashes = set()
        if media_hash:
            all_hashes.add(media_hash)
        for eh in info.get("extra_media_hashes", []):
            eh_val = eh.get("hash") if isinstance(eh, dict) else str(eh)
            if eh_val:
                all_hashes.add(eh_val)

        for h in all_hashes:
            for table in ("media_hashes", "content_hashes", "fingerprints"):
                if table in manifest and h in manifest[table]:
                    other_owner = [k for k, v in manifest["files"].items() if v.get("media_hash") == h or v.get("content_hash") == h]
                    if other_owner:
                        manifest[table][h] = manifest["files"][other_owner[0]]
                    else:
                        manifest[table].pop(h, None)

        # 6. Check if any remaining file in manifest still uses this blob_path
        other_files_using_blob = [
            k for k, v in manifest.get("files", {}).items()
            if v.get("blob_path") == blob_path
        ]

        hf_deleted = False
        if (force_cloud_delete or not other_files_using_blob) and blob_path and can_write:
            try:
                metadata_helper.delete_video_and_sidecars_from_hf(api, repo_id, info)
                hf_deleted = True
            except Exception as e:
                logger.warning("Error deleting blob %s from HF: %s", blob_path, e)

        # 7. Immediately re-sync vault_index.enc on Hugging Face so no ghost index remains
        if sync_repo_index and can_write:
            try:
                remaining_repo_files = {k: v for k, v in manifest.get("files", {}).items() if v.get("repo_id") == repo_id}
                if not remaining_repo_files and repo_id.split("/")[-1].startswith("vault-"):
                    api.delete_repo(repo_id=repo_id, repo_type="dataset")
                    logger.info("Deleted empty HF vault repository %s", repo_id)
                else:
                    pass  # Master Index Only mode: canonical index updated via sync_master_index_to_cloud
            except Exception as e:
                logger.warning("Could not sync repo manifest after delete for %s: %s", repo_id, e)

        # Always sync canonical master index so deleted files are removed from the master index!
        self.sync_master_index_to_cloud()

        return {"success": True, "hf_deleted": hf_deleted, "repo_id": repo_id}


    def handle_delete_folder(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            folder_prefix = req_data.get("folder", "").strip("/").replace("\\", "/")
        except Exception:
            self.send_error(400, "Invalid JSON payload")
            return

        if not folder_prefix:
            self.send_error(400, "Missing 'folder' parameter")
            return

        manifest = load_manifest()
        matching_keys = [
            k for k in manifest["files"].keys()
            if k == folder_prefix or k.startswith(folder_prefix + "/")
        ]

        if not matching_keys:
            self.send_error(404, f"No files found under folder '{folder_prefix}'")
            return

        total_files = len(matching_keys)
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        self._send_stream_chunk({
            "stage": "init",
            "percent": 2,
            "current": 0,
            "total": total_files,
            "folder": folder_prefix,
            "message": f"Found {total_files} video(s) in '{folder_prefix}'. Initiating permanent purge...",
            "timestamp": time.strftime("%H:%M:%S")
        })

        deleted_count = 0
        affected_repos = {}

        for idx, key in enumerate(matching_keys, 1):
            base_name = Path(key).name
            pct_start = int(3 + ((idx - 1) / total_files) * 80)
            self._send_stream_chunk({
                "stage": "purging",
                "percent": pct_start,
                "current": idx,
                "total": total_files,
                "key": key,
                "message": f"[{idx}/{total_files}] Purging cloud blob & sidecars: {base_name}",
                "timestamp": time.strftime("%H:%M:%S")
            })

            info = manifest.get("files", {}).get(key, {})
            repo_id = info.get("repo_id", f"{self.hf_username}/{self.default_repo}")
            api = self.acc_mgr.get_api_for_repo(repo_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else HfApi(token=self.token)
            can_write = self.acc_mgr.can_write_to_repo(repo_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else True
            if repo_id not in affected_repos:
                affected_repos[repo_id] = (api, can_write)

            try:
                self.purge_video_entry(key, manifest, force_cloud_delete=True, sync_repo_index=False)
                deleted_count += 1
            except Exception as pe:
                logger.warning("Error purging %s: %s", key, pe)

            pct_done = int(3 + (idx / total_files) * 80)
            self._send_stream_chunk({
                "stage": "item_deleted",
                "percent": pct_done,
                "current": idx,
                "total": total_files,
                "key": key,
                "message": f"[{idx}/{total_files}] Successfully deleted {base_name}",
                "timestamp": time.strftime("%H:%M:%S")
            })

        # Sync vault_index.enc once per affected repository
        if affected_repos:
            self._send_stream_chunk({
                "stage": "syncing_index",
                "percent": 86,
                "current": total_files,
                "total": total_files,
                "message": f"Synchronizing remote vault index for {len(affected_repos)} repo(s)...",
                "timestamp": time.strftime("%H:%M:%S")
            })
            for repo_id, (api, can_write) in affected_repos.items():
                if can_write:
                    try:
                        remaining_repo_files = {k: v for k, v in manifest.get("files", {}).items() if v.get("repo_id") == repo_id}
                        if not remaining_repo_files and repo_id.split("/")[-1].startswith("vault-"):
                            api.delete_repo(repo_id=repo_id, repo_type="dataset")
                            logger.info("Deleted empty HF vault repository %s", repo_id)
                        else:
                            pass
                            # Master Index Only mode: canonical index updated via sync_master_index_to_cloud
                    except Exception as e:
                        logger.warning("Could not sync repo manifest after batch delete for %s: %s", repo_id, e)

            # Always sync master index after batch delete
            self.sync_master_index_to_cloud()

        # Clean local library folder and save manifest
        self._send_stream_chunk({
            "stage": "cleaning_library",
            "percent": 94,
            "current": total_files,
            "total": total_files,
            "message": "Cleaning local Jellyfin library pointers & empty folders...",
            "timestamp": time.strftime("%H:%M:%S")
        })

        local_folder_path = Path("jellyfin_library") / folder_prefix
        if local_folder_path.exists():
            try:
                shutil.rmtree(str(local_folder_path), ignore_errors=True)
            except Exception:
                pass

        remove_empty_folders("jellyfin_library")
        save_manifest(manifest)

        # Notify activity tracker and broadcast event
        activity_tracker.tracker.notify_manifest_updated()
        activity_tracker.tracker.add_event("success", "Folder Deleted", f"Permanently purged folder '{folder_prefix}' ({deleted_count} video(s)) from Hugging Face and library")

        self._send_stream_chunk({
            "stage": "complete",
            "success": True,
            "percent": 100,
            "current": deleted_count,
            "total": total_files,
            "folder": folder_prefix,
            "deleted_count": deleted_count,
            "message": f"Successfully deleted folder '{folder_prefix}' ({deleted_count} video(s)) from Hugging Face and Jellyfin library.",
            "timestamp": time.strftime("%H:%M:%S")
        })
        self._finish_stream_chunked()


    def handle_delete(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            key_to_delete = req_data.get("key")
        except Exception:
            self.send_error(400, "Invalid JSON payload")
            return

        if not key_to_delete:
            self.send_error(400, "Missing 'key' in request")
            return

        manifest = load_manifest()
        if key_to_delete not in manifest["files"]:
            self.send_error(404, "Video key not found in manifest")
            return

        base_name = Path(key_to_delete).name
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        self._send_stream_chunk({
            "stage": "init",
            "percent": 10,
            "current": 0,
            "total": 1,
            "key": key_to_delete,
            "message": f"Initiating permanent deletion of {base_name}...",
            "timestamp": time.strftime("%H:%M:%S")
        })

        self._send_stream_chunk({
            "stage": "purging",
            "percent": 45,
            "current": 1,
            "total": 1,
            "key": key_to_delete,
            "message": f"Purging remote encrypted blob, sidecars, and local Jellyfin files...",
            "timestamp": time.strftime("%H:%M:%S")
        })

        res = self.purge_video_entry(key_to_delete, manifest, force_cloud_delete=True, sync_repo_index=True)
        remove_empty_folders("jellyfin_library")
        save_manifest(manifest)

        # Notify activity tracker and broadcast event
        activity_tracker.tracker.notify_manifest_updated()
        activity_tracker.tracker.add_event("success", "Video Deleted", f"Permanently purged {key_to_delete} from Hugging Face and library")

        self._send_stream_chunk({
            "stage": "complete",
            "success": True,
            "percent": 100,
            "current": 1,
            "total": 1,
            "key": key_to_delete,
            "hf_deleted": res.get("hf_deleted", False),
            "message": f"Successfully deleted {base_name} from Hugging Face and Jellyfin library.",
            "timestamp": time.strftime("%H:%M:%S")
        })
        self._finish_stream_chunked()


    def handle_delete_skipped(self):
        """
        Deletes all skipped / indecryptable files from Hugging Face
        and removes them from upload_manifest.json.
        """
        manifest = load_manifest()
        deleted_count = 0

        keys_to_delete = []
        for key, info in manifest["files"].items():
            verifier = info.get("verifier")
            if verifier and not crypto_engine.verify_password(verifier, self.password):
                keys_to_delete.append(key)

        for key in keys_to_delete:
            info = manifest["files"].get(key, {})
            repo_id = info.get("repo_id", f"{self.hf_username}/{self.default_repo}")
            blob_path = info.get("blob_path") or info.get("remote_path")
            media_hash = info.get("media_hash") or info.get("content_hash")
            api = self.acc_mgr.get_api_for_repo(repo_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else HfApi(token=self.token)

            if blob_path and hasattr(self, "acc_mgr") and self.acc_mgr and not self.acc_mgr.can_write_to_repo(repo_id):
                logger.info("Skipping HF cloud deletion for %s: Token lacks write permissions.", repo_id)
            elif blob_path:
                try:
                    metadata_helper.delete_video_and_sidecars_from_hf(api, repo_id, info)
                except Exception as e:
                    print(f"[!] Warning deleting from HF ({blob_path}): {e}")

            if account_manager.is_jellyfin_sync_enabled():
                metadata_helper.delete_nfo_files("jellyfin_library", key)
                strm_path = Path("jellyfin_library") / f"{key}.strm"
                if strm_path.exists():
                    try:
                        strm_path.unlink()
                    except Exception:
                        pass

            FILE_CACHE.pop(key, None)
            manifest["files"].pop(key, None)
            if media_hash and "media_hashes" in manifest:
                manifest["media_hashes"].pop(media_hash, None)

            deleted_count += 1

        remove_empty_folders("jellyfin_library")
        save_manifest(manifest)

        resp_body = json.dumps({
            "success": True,
            "deleted_count": deleted_count,
            "message": f"Successfully deleted {deleted_count} obsolete/skipped file(s) from Hugging Face and local library."
        }).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)


    def handle_api_storyboard(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        raw_key = qs.get("key", [""])[0]
        key = unquote(unquote(raw_key))
        duration = float(qs.get("duration", [0])[0] or 0)

        if not key:
            safe_id_arg = qs.get("id", [""])[0]
            if safe_id_arg:
                resolved = self.resolve_key_from_id_or_path(safe_id_arg)
                if resolved and resolved != safe_id_arg:
                    key = resolved
                else:
                    try:
                        files_data, _, _, _ = self.get_files_data()
                        for f in files_data:
                            if f.get("safe_id") == safe_id_arg:
                                key = f.get("key")
                                break
                    except Exception:
                        pass

        if not key:
            self.send_error(400, "Missing key parameter")
            return

        safe_id = storyboard_helper.get_safe_id(key)
        meta = storyboard_helper.get_storyboard_meta(key, password=self.password)
        if not meta.get("available"):
            port = self.server.server_address[1] if hasattr(self.server, "server_address") else 8080
            storyboard_helper.trigger_priority_storyboard(key, duration=duration, port=port)

        resp_body = json.dumps(meta).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_api_storyboard_image(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        safe_id = qs.get("id", [""])[0]
        if not safe_id:
            key = qs.get("key", [""])[0]
            if key:
                safe_id = storyboard_helper.get_safe_id(key)

        img_path = storyboard_helper.get_storyboard_image_path(safe_id) if safe_id else None
        if (not img_path or not os.path.exists(img_path)) and safe_id:
            resolved_key = self.resolve_key_from_id_or_path(safe_id)
            if resolved_key and resolved_key != safe_id:
                canonical_safe_id = storyboard_helper.get_safe_id(resolved_key)
                if canonical_safe_id:
                    cand_img = storyboard_helper.get_storyboard_image_path(canonical_safe_id)
                    if cand_img and os.path.exists(cand_img):
                        safe_id = canonical_safe_id
                        img_path = cand_img

        if not img_path or not os.path.exists(img_path):
            self.send_error(404, "Storyboard image not found")
            return

        mtime = int(os.path.getmtime(img_path)) if os.path.exists(img_path) else int(time.time())
        etag = f'"{safe_id}_{mtime}"'
        if_none_match = self.headers.get("If-None-Match")
        if if_none_match and if_none_match == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return

        data = storyboard_helper.read_decrypted_bytes(img_path, self.password)
        if not data:
            self.send_error(500, "Failed to decrypt image")
            return

        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Disposition", "inline")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def handle_api_screens_image(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        safe_id = qs.get("id", [""])[0]
        key = qs.get("key", [""])[0]
        if not safe_id and key:
            safe_id = storyboard_helper.get_safe_id(key)
        elif safe_id:
            resolved_key = self.resolve_key_from_id_or_path(safe_id)
            if resolved_key and resolved_key != safe_id:
                canonical_safe_id = storyboard_helper.get_safe_id(resolved_key)
                if canonical_safe_id:
                    c_path = storyboard_helper.get_poster_thumb_path(canonical_safe_id)
                    if c_path and os.path.exists(c_path):
                        safe_id = canonical_safe_id

        img_path = screens_helper.get_screens_path(safe_id) if safe_id else None

        # 1. Check local search folders if not in storyboards/screens
        if not img_path or not os.path.exists(img_path):
            manifest = load_manifest()
            for k, info in manifest.get("files", {}).items():
                if storyboard_helper.get_safe_id(k) == safe_id or k == key:
                    matched = screens_helper.find_matching_screens_file(Path(k))
                    if matched and matched.is_file():
                        screens_helper.process_screens_image(matched, safe_id)
                        img_path = screens_helper.get_screens_path(safe_id)
                    break

        # 2. Check cloud Hugging Face dataset
        if not img_path or not os.path.exists(img_path):
            manifest = load_manifest()
            target_info = None
            for k, info in manifest.get("files", {}).items():
                if storyboard_helper.get_safe_id(k) == safe_id or k == key:
                    target_info = info
                    break

            screens_info = (target_info or {}).get("screens")
            if screens_info and screens_info.get("blob_path"):
                try:
                    repo_id = screens_info.get("repo_id")
                    blob_path = screens_info.get("blob_path")
                    api = self.acc_mgr.get_api_for_repo(repo_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else HfApi(token=self.token)
                    local_downloaded = api.hf_hub_download(repo_id=repo_id, filename=blob_path, repo_type="dataset")
                    temp_plain = os.path.join(screens_helper.SCREENS_DIR, f"{safe_id}.jpg")
                    crypto_engine.decrypt_file(local_downloaded, temp_plain, self.password)
                    screens_helper.process_screens_image(Path(temp_plain), safe_id, password=self.password)
                    img_path = screens_helper.get_screens_path(safe_id)
                except Exception as e:
                    logger.warning("Could not restore cloud screens for image %s: %s", safe_id, e)

        if not img_path or not os.path.exists(img_path):
            self.send_error(404, "Screens image not found")
            return

        mtime = int(os.path.getmtime(img_path)) if os.path.exists(img_path) else int(time.time())
        etag = f'"{safe_id}_{mtime}"'
        if_none_match = self.headers.get("If-None-Match")
        if if_none_match and if_none_match == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return

        data = storyboard_helper.read_decrypted_bytes(img_path, self.password)
        if not data:
            self.send_error(500, "Failed to decrypt image")
            return

        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Disposition", "inline")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def handle_api_screens_thumb(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        safe_id = qs.get("id", [""])[0]
        key = qs.get("key", [""])[0]
        if not safe_id and key:
            safe_id = storyboard_helper.get_safe_id(key)

        # 1. Primary: Thumbnail sliced from generated 100-frame storyboard
        thumb_path = storyboard_helper.get_poster_thumb_path(safe_id) if safe_id else None
        if (not thumb_path or not os.path.exists(thumb_path)) and safe_id:
            resolved_key = self.resolve_key_from_id_or_path(safe_id)
            if resolved_key and resolved_key != safe_id:
                canonical_safe_id = storyboard_helper.get_safe_id(resolved_key)
                if canonical_safe_id:
                    c_path = storyboard_helper.get_poster_thumb_path(canonical_safe_id)
                    if c_path and os.path.exists(c_path):
                        safe_id = canonical_safe_id
                        thumb_path = c_path
        if not thumb_path or not os.path.exists(thumb_path):
            sb_img = storyboard_helper.get_storyboard_image_path(safe_id) if safe_id else None
            if sb_img and os.path.exists(sb_img):
                try:
                    storyboard_helper.slice_poster_thumbnail(safe_id, overwrite=False, password=self.password)
                    thumb_path = storyboard_helper.get_poster_thumb_path(safe_id, password=self.password)
                except Exception:
                    pass

        # 2. Legacy fallback: screens_thumb if already present
        if not thumb_path or not os.path.exists(thumb_path):
            thumb_path = screens_helper.get_screens_thumb_path(safe_id) if safe_id else None

        if not thumb_path or not os.path.exists(thumb_path):
            sb_img = storyboard_helper.get_storyboard_image_path(safe_id) if safe_id else None
            if sb_img and os.path.exists(sb_img):
                try:
                    storyboard_helper.slice_poster_thumbnail(safe_id, overwrite=False, password=self.password)
                    thumb_path = storyboard_helper.get_poster_thumb_path(safe_id, password=self.password)
                except Exception:
                    pass

        if not thumb_path or not os.path.exists(thumb_path):
            if safe_id:
                manifest = load_manifest()
                for k, v in manifest.get("files", {}).items():
                    if storyboard_helper.get_safe_id(k) == safe_id:
                        port = self.server.server_address[1] if hasattr(self.server, "server_address") else 8080
                        storyboard_helper.trigger_priority_storyboard(k, duration=0, port=port)
                        break
            self.send_error(404, "Thumbnail not found")
            return

        mtime = int(os.path.getmtime(thumb_path)) if os.path.exists(thumb_path) else int(time.time())
        etag = f'"{safe_id}_{mtime}"'
        if_none_match = self.headers.get("If-None-Match")
        if if_none_match and if_none_match == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return

        data = storyboard_helper.read_decrypted_bytes(thumb_path, self.password)
        if not data:
            self.send_error(500, "Failed to decrypt thumbnail")
            return

        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Disposition", "inline")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def handle_api_activity(self):
        state = activity_tracker.tracker.get_state()
        resp = json.dumps(state).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp)

    def handle_api_accounts(self):
        manifest = load_manifest()
        summary = self.acc_mgr.get_accounts_summary(manifest) if hasattr(self, "acc_mgr") and self.acc_mgr else []
        primary = self.acc_mgr.primary_username if hasattr(self, "acc_mgr") and self.acc_mgr else self.hf_username
        resp = json.dumps({
            "accounts": summary,
            "primary_account": primary
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp)

    def handle_accounts_add(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            token = req_data.get("token", "").strip()
            is_upload = bool(req_data.get("is_upload", False))
            password = req_data.get("password", "").strip() or None
        except Exception:
            self.send_error(400, "Invalid JSON payload")
            return

        if not token:
            self.send_error(400, "Missing 'token' in request")
            return

        try:
            res = self.acc_mgr.add_account(token=token, is_upload=is_upload, password=password)
            if is_upload:
                self.token = self.acc_mgr.upload_token
                self.hf_username = self.acc_mgr.primary_username

            acc_user = res["username"]
            desc_role = "Designated as Primary Upload Account" if is_upload else "Added as Read/Scan/Stream Account"
            activity_tracker.tracker.add_event(
                "info",
                f"Account Connected: {acc_user}",
                f"{desc_role} (Key: {'Custom' if res.get('has_custom_key') else 'Master'})",
                account=acc_user
            )

            # Perform discovery and library merge in background daemon so UI connects instantly without lag
            def _async_bg_scan():
                try:
                    manifest = load_manifest()
                    scan_res = self.acc_mgr.scan_and_merge_all_accounts(manifest, library_dir="jellyfin_library")
                    save_manifest(manifest)
                    activity_tracker.tracker.add_event(
                        "info",
                        f"Scan Complete: {acc_user}",
                        f"Discovered {scan_res.get('discovered_count', 0)} files ({scan_res.get('skipped_count', 0)} skipped)",
                        account=acc_user
                    )
                except Exception as ex:
                    print(f"[!] Error in background scan for {acc_user}: {ex}")

            threading.Thread(target=_async_bg_scan, daemon=True).start()

            resp_body = json.dumps({
                "success": True,
                "account": res,
                "discovered": 0,
                "message": res.get("message") or f"Successfully connected '{acc_user}'!"
            }).encode("utf-8")

        except Exception as e:
            resp_body = json.dumps({
                "success": False,
                "message": str(e)
            }).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_accounts_update_key(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            username = req_data.get("username", "").strip()
            password = req_data.get("password", "").strip()
        except Exception:
            self.send_error(400, "Invalid JSON payload")
            return

        if not username:
            self.send_error(400, "Missing 'username'")
            return

        try:
            res = self.acc_mgr.update_account_password(username, password)
            if res.get("discarded_count", 0) > 0:
                self.sync_master_index_to_cloud(async_mode=True)
            activity_tracker.tracker.add_event(
                "info",
                f"Encryption Key Updated: {username}",
                f"{'Custom key set' if res.get('has_custom_key') else 'Reset to master key'}. {res.get('discarded_count', 0)} undecryptable file(s) discarded.",
                account=username
            )
            activity_tracker.tracker.notify_manifest_updated()
            resp_body = json.dumps(res).encode("utf-8")
        except Exception as e:
            resp_body = json.dumps({"success": False, "message": str(e)}).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_api_undecryptable_files(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        force_refresh = qs.get("refresh", ["0"])[0] in ("1", "true")
        try:
            res = self.acc_mgr.scan_undecryptable_cloud_files(force_refresh=force_refresh)
            resp_body = json.dumps({"success": True, **res}).encode("utf-8")
        except Exception as e:
            resp_body = json.dumps({"success": False, "error": str(e), "items": [], "empty_repos": []}).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_delete_undecryptable_files(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            items = req_data.get("items", [])
            res = self.acc_mgr.delete_bulk_cloud_files(items)
            activity_tracker.tracker.add_event(
                "warning",
                "Purged Undecryptable Cloud Files",
                res.get("message", "Deleted undecryptable files from Hugging Face.")
            )
            resp_body = json.dumps(res).encode("utf-8")
        except Exception as e:
            resp_body = json.dumps({"success": False, "error": str(e)}).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_delete_undecryptable_repo(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            req_data = json.loads(body)
            repo_id = req_data.get("repo_id", "").strip()
            if not repo_id:
                raise ValueError("Missing 'repo_id'")
            res = self.acc_mgr.delete_cloud_repo(repo_id)
            activity_tracker.tracker.add_event(
                "warning",
                f"Deleted Cloud Dataset: {repo_id}",
                res.get("message", f"Deleted repository {repo_id} from Hugging Face.")
            )
            resp_body = json.dumps(res).encode("utf-8")
        except Exception as e:
            resp_body = json.dumps({"success": False, "error": str(e)}).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_staging_status(self):
        status = staging_manager.get_staging_status()
        resp = json.dumps(status).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp)

    def handle_staging_push(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            data = json.loads(body)
        except Exception:
            data = {}
        repo_folder = data.get("repo_folder")
        if not repo_folder:
            self.send_error(400, "Missing repo_folder parameter")
            return
        success, msg = staging_manager.push_repo_async(repo_folder, library_dir="jellyfin_library")
        resp = json.dumps({"success": success, "message": msg}).encode("utf-8")
        self.send_response(200 if success else 400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp)

    def handle_staging_push_all(self):
        status = staging_manager.get_staging_status()
        launched = 0
        for r in status.get("ready_repos", []):
            success, _ = staging_manager.push_repo_async(r["repo_folder"], library_dir="jellyfin_library")
            if success:
                launched += 1
        resp = json.dumps({"success": True, "launched_count": launched}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp)

    def handle_staging_dismiss(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            req_data = json.loads(body)
            rf = req_data.get("repo_folder", "").strip()
            if rf:
                staging_manager.dismiss_staged_repo(rf)
            resp = json.dumps({"success": True}).encode("utf-8")
        except Exception as e:
            resp = json.dumps({"success": False, "error": str(e)}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp)

    def handle_staging_discard(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            data = json.loads(body)
        except Exception:
            data = {}
        repo_folder = data.get("repo_folder")
        if not repo_folder:
            self.send_error(400, "Missing repo_folder parameter")
            return
        success, msg = staging_manager.discard_staged_repo(repo_folder)
        resp = json.dumps({"success": success, "message": msg}).encode("utf-8")
        self.send_response(200 if success else 400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp)

    def handle_staging_revert(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            data = json.loads(body)
        except Exception:
            data = {}
        repo_folder = data.get("repo_folder")
        if not repo_folder:
            self.send_error(400, "Missing repo_folder parameter")
            return
        success, msg = staging_manager.revert_and_ignore_staged_repo(repo_folder)
        resp = json.dumps({"success": success, "message": msg}).encode("utf-8")
        self.send_response(200 if success else 400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp)

    def handle_staging_unignore(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            data = json.loads(body)
        except Exception:
            data = {}
        repo_folder = data.get("repo_folder")
        if not repo_folder:
            self.send_error(400, "Missing repo_folder parameter")
            return
        success, msg = staging_manager.unignore_staged_repo(repo_folder)
        resp = json.dumps({"success": success, "message": msg}).encode("utf-8")
        self.send_response(200 if success else 400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp)

    def handle_staging_pause(self):
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
        repo_folder = data.get("repo_folder", "")
        if repo_folder:
            staging_manager.pause_repo_push(repo_folder)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "paused", "repo_folder": repo_folder}).encode("utf-8"))

    def handle_staging_pause_staging(self):
        staging_manager.pause_staging()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "staging_paused"}).encode("utf-8"))

    def handle_staging_resume(self):
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
        repo_folder = data.get("repo_folder", "")
        if repo_folder:
            staging_manager.resume_repo_push(repo_folder)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "resumed", "repo_folder": repo_folder}).encode("utf-8"))

    def handle_staging_resume_staging(self):
        staging_manager.resume_staging()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "staging_resumed"}).encode("utf-8"))

    def handle_staging_cancel_queued(self):
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length).decode("utf-8")) if length > 0 else {}
        repo_folder = data.get("repo_folder", "")
        if repo_folder:
            staging_manager.cancel_queued_push(repo_folder)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "canceled", "repo_folder": repo_folder}).encode("utf-8"))

    def handle_auto_cleanup_undecryptable_repos(self):
        try:
            acc_mgr = getattr(self, "acc_mgr", None) or account_manager.AccountManager()
            cleaned = acc_mgr.auto_cleanup_obsolete_repos(max_idle_hours=24.0)
            msg = f"Cleaned up {len(cleaned)} obsolete repository(s) inactive for 24+ hours." if cleaned else "No empty repositories have reached 24+ hours of inactivity yet."
            resp_body = json.dumps({
                "success": True,
                "cleaned_count": len(cleaned),
                "cleaned_repos": cleaned,
                "message": msg
            }).encode("utf-8")
        except Exception as e:
            resp_body = json.dumps({"success": False, "error": str(e)}).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)

    def handle_static_file(self, path):
        rel = os.path.normpath(path.lstrip("/")).replace("\\", "/")
        if rel.startswith("..") or "/../" in rel:
            self.send_error(403, "Access denied")
            return
        static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
        sub_path = rel[7:] if rel.startswith("static/") else rel
        full_path = os.path.join(static_dir, sub_path.replace("/", os.sep))
        if not os.path.isfile(full_path):
            self.send_error(404, "File not found")
            return
        mime = "text/plain"
        if full_path.endswith(".css"):
            mime = "text/css; charset=utf-8"
        elif full_path.endswith(".js"):
            mime = "application/javascript; charset=utf-8"
        elif full_path.endswith(".png"):
            mime = "image/png"
        elif full_path.endswith((".jpg", ".jpeg")):
            mime = "image/jpeg"
        elif full_path.endswith(".svg"):
            mime = "image/svg+xml"
        elif full_path.endswith(".woff2"):
            mime = "font/woff2"
        try:
            with open(full_path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            self.send_error(500, f"Error reading static file: {e}")

    def do_GET(self):
        cloud_watcher.notify_client_active()
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path.startswith("/static/"):
            self.handle_static_file(path)
        elif path in ("/", "/index.html"):
            self.handle_dashboard()
        elif path in ("/discover", "/discover.html"):
            self.handle_discover()
        elif path == "/api/files":
            self.handle_api_files()
        elif path == "/api/jellyfin_sync":
            self.handle_get_jellyfin_sync()
        elif path in ("/api/resync-stream", "/api/recheck-cloud"):
            self.handle_resync_stream()
        elif path == "/api/restore-hf":
            self.handle_restore_hf()
        elif path == "/api/jellyfin_sync/toggle":
            self.handle_toggle_jellyfin_sync()
        elif path == "/api/cloud_sync/trigger":
            self.handle_cloud_sync_trigger()
        elif path == "/api/accounts":
            self.handle_api_accounts()
        elif path == "/api/undecryptable-files":
            self.handle_api_undecryptable_files()
        elif path == "/api/activity":
            self.handle_api_activity()
        elif path == "/api/storyboard":
            self.handle_api_storyboard()
        elif path == "/api/storyboard/image":
            self.handle_api_storyboard_image()
        elif path == "/api/screens/thumb":
            self.handle_api_screens_thumb()
        elif path == "/api/screens/image":
            self.handle_api_screens_image()
        elif path == "/api/staging/status":
            self.handle_staging_status()

        elif path == "/api/cache/stats":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            stats = stream_cache.get_overall_stats()
            self.wfile.write(json.dumps(stats).encode("utf-8"))
            return
        elif path == "/api/test/screens100":
            test_path = "test_screens_100_Variant_4K_384x216_q2.jpg"
            if os.path.exists(test_path):
                with open(test_path, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
        elif path.startswith("/stream/subtitle"):
            self.handle_stream_subtitle()
        elif path.startswith("/stream/audio/"):
            key_after = path[len("/stream/audio/"):]
            self.handle_stream_audio(key_after)
        elif path.startswith("/stream/play/"):
            raw_id = path[len("/stream/play/"):].strip("/")
            resolved_key = self.resolve_key_from_id_or_path(raw_id)
            print(f'[DEBUG_STREAM] /stream/play: raw_id={raw_id} -> resolved_key={resolved_key}', flush=True)
            self.handle_stream(resolved_key)
        elif path.startswith("/stream/"):
            path_after_stream = path[len("/stream/"):]
            self.handle_stream(path_after_stream)
        else:
            self.send_error(404, "Not Found")

    def get_files_data(self):
        manifest = load_manifest()
        files_data = []
        skipped_files = []

        all_accounts = [a["username"] for a in self.acc_mgr.get_all_accounts()] if (hasattr(self, "acc_mgr") and self.acc_mgr) else [self.hf_username]
        primary_account = self.acc_mgr.primary_username if (hasattr(self, "acc_mgr") and self.acc_mgr) else self.hf_username

        seen_blobs = {}
        for key, info in manifest.get("files", {}).items():
            if info.get("status") == "uploaded":
                if info.get("is_alias_of"):
                    continue

                repo = info.get("repo_id", f"{self.hf_username}/{self.default_repo}")
                blob = info.get("blob_path") or info.get("remote_path", "unknown")
                media_hash = info.get("media_hash") or info.get("content_hash")
                dedup_id = (repo, blob) if (blob and blob != "unknown") else (media_hash or key)

                orig_name = Path(key).name
                file_names = list(info.get("file_names") or [key])
                aliases = [a for a in (info.get("aliases") or []) if a != orig_name and a.lower() != orig_name.lower()]

                if dedup_id in seen_blobs:
                    existing_item = files_data[seen_blobs[dedup_id]]
                    exist_name = Path(existing_item["key"]).name
                    for a in aliases:
                        if a not in existing_item["aliases"] and a != exist_name and a.lower() != exist_name.lower():
                            existing_item["aliases"].append(a)
                    for fn in file_names:
                        if fn not in existing_item["file_names"]:
                            existing_item["file_names"].append(fn)
                            fn_base = Path(fn).name
                            if fn_base != exist_name and fn_base.lower() != exist_name.lower() and fn_base not in existing_item["aliases"]:
                                existing_item["aliases"].append(fn_base)
                    continue

                verifier = info.get("verifier")
                cand_keys = self.acc_mgr.get_candidate_keys_for_repo(repo) if (hasattr(self, "acc_mgr") and self.acc_mgr) else [self.password]
                can_decrypt = bool(crypto_engine.find_working_key_for_verifier(cand_keys, verifier)) if verifier else True
                if not can_decrypt:
                    continue

                size_bytes = info.get("orig_size", 0)
                hf_url = f"https://huggingface.co/datasets/{repo}/blob/main/{blob}" if (blob and blob != "unknown") else f"https://huggingface.co/datasets/{repo}"
                metadata = info.get("metadata") or metadata_helper.extract_default_metadata(key)
                if key.endswith(".encname"):
                    orig_file_name = Path(key).name
                    cur_title = metadata.get("title", "")
                    if not cur_title or not cur_title.endswith(".encname"):
                        metadata["title"] = orig_file_name
                if metadata.get("overview", "").startswith("Encrypted vault stream:"):
                    metadata["overview"] = ""

                account_owner = info.get("account") or (repo.split("/")[0] if "/" in repo else self.hf_username)

                media_info = info.get("media_info")
                dur_sec = 0
                duration_str = ""
                if media_info and media_info.get("duration_sec"):
                    dur_sec = media_info.get("duration_sec", 0)
                    duration_str = media_info.get("duration_str") or format_duration(dur_sec)
                else:
                    sb_meta = storyboard_helper.get_storyboard_meta(key)
                    if sb_meta and sb_meta.get("duration"):
                        raw_sb_dur = float(sb_meta.get("duration", 0))
                        # Ignore 180.0 default fallback placeholder from storyboard generator
                        if abs(raw_sb_dur - 180.0) > 0.05:
                            dur_sec = raw_sb_dur
                            duration_str = format_duration(dur_sec)

                safe_id = storyboard_helper.get_safe_id(key)
                has_screens_flag = bool(info.get("screens")) or screens_helper.has_screens(safe_id)
                has_sb_flag = bool(info.get("storyboard")) or storyboard_helper.has_storyboard(key)
                sb_v = storyboard_helper.get_storyboard_version(safe_id)

                audio_entry = info.get("audio")
                audio_sz = (audio_entry.get("orig_size") or audio_entry.get("size_bytes")) if audio_entry else None
                if not audio_sz:
                    cached_p = os.path.join("storyboards", "audio", f"{safe_id}.m4a")
                    if os.path.exists(cached_p):
                        audio_sz = os.path.getsize(cached_p)
                if audio_sz and audio_sz > 0:
                    audio_size_str = f"{audio_sz / (1024 * 1024):.1f} MB"
                elif dur_sec and dur_sec > 0:
                    audio_size_str = f"~{(dur_sec * 16.3 * 1024) / (1024 * 1024):.1f} MB"
                else:
                    audio_size_str = "Audio"
                audio_codec = (media_info.get("audio_codec") if media_info else None) or "AAC"

                item = {
                    "key": key,
                    "safe_id": safe_id,
                    "has_storyboard": has_sb_flag,
                    "sb_version": sb_v,
                    "has_screens": has_screens_flag,
                    "screens_url": f"/api/screens/image?id={safe_id}&v={sb_v}" if sb_v else f"/api/screens/image?id={safe_id}",
                    "screens_thumb_url": f"/api/screens/thumb?id={safe_id}&v={sb_v}" if sb_v else f"/api/screens/thumb?id={safe_id}",
                    "duration_sec": dur_sec,
                    "duration_str": duration_str,
                    "account": account_owner,
                    "repo": repo,
                    "blob": blob,
                    "hf_url": hf_url,
                    "size_bytes": size_bytes,
                    "size_str": f"{size_bytes / (1024 * 1024):.2f} MB",
                    "audio_size_str": audio_size_str,
                    "audio_codec": audio_codec,
                    "uploaded_at": info.get("uploaded_at", "N/A"),
                    "file_names": file_names,
                    "aliases": aliases,
                    "metadata": metadata,
                    "media_info": media_info,
                    "subtitles": info.get("subtitles") or [],
                    "has_audio": bool(info.get("audio")),
                    "audio_url": f"/stream/audio/{key}"
                }

                if can_decrypt:
                    seen_blobs[dedup_id] = len(files_data)
                    c_info = stream_cache.get_cache_info(safe_id, total_size=size_bytes, chunk_size=crypto_engine.DEFAULT_CHUNK_SIZE)
                    item["cache_pct"] = c_info["percentage"]
                    item["cached_bytes"] = c_info["cached_bytes"]
                    item["cached_size_str"] = c_info["cached_size_str"]
                    item["is_cached"] = c_info["cached"]
                    item["pinned_offline"] = c_info["pinned_offline"]
                    item["is_downloading"] = c_info["is_downloading"]
                    files_data.append(item)
                else:
                    item["reason"] = "Cannot decrypt with current master password (encrypted with an older or different key)"
                    skipped_files.append(item)

        return files_data, skipped_files, all_accounts, primary_account

    def handle_api_files(self):
        files_data, skipped_files, all_accounts, primary_account = self.get_files_data()
        body = json.dumps({
            "files": files_data,
            "skipped": skipped_files,
            "skipped_count": len(skipped_files),
            "accounts": all_accounts,
            "primary_account": primary_account
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def handle_discover(self):
        from discover_template import render_discover_page
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        staged_repo = qs.get("staged_repo", [None])[0]
        html = render_discover_page(self, staged_repo=staged_repo)
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def handle_dashboard(self):
        manifest = load_manifest()
        files_data = []
        skipped_files = []
        total_bytes = 0
        repos_set = set()

        seen_blobs = {}
        for key, info in manifest.get("files", {}).items():
            if info.get("status") == "uploaded":
                if info.get("is_alias_of"):
                    continue

                repo = info.get("repo_id", f"{self.hf_username}/{self.default_repo}")
                blob = info.get("blob_path") or info.get("remote_path", "unknown")
                media_hash = info.get("media_hash") or info.get("content_hash")
                dedup_id = (repo, blob) if (blob and blob != "unknown") else (media_hash or key)

                orig_name = Path(key).name
                file_names = list(info.get("file_names") or [key])
                aliases = [a for a in (info.get("aliases") or []) if a != orig_name and a.lower() != orig_name.lower()]

                if dedup_id in seen_blobs:
                    existing_item = files_data[seen_blobs[dedup_id]]
                    exist_name = Path(existing_item["key"]).name
                    for a in aliases:
                        if a not in existing_item["aliases"] and a != exist_name and a.lower() != exist_name.lower():
                            existing_item["aliases"].append(a)
                    for fn in file_names:
                        if fn not in existing_item["file_names"]:
                            existing_item["file_names"].append(fn)
                            fn_base = Path(fn).name
                            if fn_base != exist_name and fn_base.lower() != exist_name.lower() and fn_base not in existing_item["aliases"]:
                                existing_item["aliases"].append(fn_base)
                    continue

                size_bytes = info.get("orig_size", 0)
                hf_url = f"https://huggingface.co/datasets/{repo}/blob/main/{blob}" if (blob and blob != "unknown") else f"https://huggingface.co/datasets/{repo}"
                metadata = info.get("metadata") or metadata_helper.extract_default_metadata(key)
                if key.endswith(".encname"):
                    orig_file_name = Path(key).name
                    cur_title = metadata.get("title", "")
                    if not cur_title or not cur_title.endswith(".encname"):
                        metadata["title"] = orig_file_name
                if metadata.get("overview", "").startswith("Encrypted vault stream:"):
                    metadata["overview"] = ""

                verifier = info.get("verifier")
                cand_keys = self.acc_mgr.get_candidate_keys_for_repo(repo) if (hasattr(self, "acc_mgr") and self.acc_mgr) else [self.password]
                can_decrypt = bool(crypto_engine.find_working_key_for_verifier(cand_keys, verifier)) if verifier else True
                if not can_decrypt:
                    continue

                account_owner = info.get("account") or (repo.split("/")[0] if "/" in repo else self.hf_username)

                media_info = info.get("media_info")
                dur_sec = 0
                duration_str = ""
                if media_info and media_info.get("duration_sec"):
                    dur_sec = media_info.get("duration_sec", 0)
                    duration_str = media_info.get("duration_str") or format_duration(dur_sec)
                else:
                    sb_meta = storyboard_helper.get_storyboard_meta(key)
                    if sb_meta and sb_meta.get("duration"):
                        raw_sb_dur = float(sb_meta.get("duration", 0))
                        # Ignore 180.0 default fallback placeholder from storyboard generator
                        if abs(raw_sb_dur - 180.0) > 0.05:
                            dur_sec = raw_sb_dur
                            duration_str = format_duration(dur_sec)

                safe_id = storyboard_helper.get_safe_id(key)
                has_screens_flag = bool(info.get("screens")) or screens_helper.has_screens(safe_id)
                has_sb_flag = bool(info.get("storyboard")) or storyboard_helper.has_storyboard(key)
                sb_v = storyboard_helper.get_storyboard_version(safe_id)

                audio_entry = info.get("audio")
                audio_sz = (audio_entry.get("orig_size") or audio_entry.get("size_bytes")) if audio_entry else None
                if not audio_sz:
                    cached_p = os.path.join("storyboards", "audio", f"{safe_id}.m4a")
                    if os.path.exists(cached_p):
                        audio_sz = os.path.getsize(cached_p)
                if audio_sz and audio_sz > 0:
                    audio_size_str = f"{audio_sz / (1024 * 1024):.1f} MB"
                elif dur_sec and dur_sec > 0:
                    audio_size_str = f"~{(dur_sec * 16.3 * 1024) / (1024 * 1024):.1f} MB"
                else:
                    audio_size_str = "Audio"
                audio_codec = (media_info.get("audio_codec") if media_info else None) or "AAC"

                item = {
                    "key": key,
                    "safe_id": safe_id,
                    "has_storyboard": has_sb_flag,
                    "sb_version": sb_v,
                    "has_screens": has_screens_flag,
                    "screens_url": f"/api/screens/image?id={safe_id}&v={sb_v}" if sb_v else f"/api/screens/image?id={safe_id}",
                    "screens_thumb_url": f"/api/screens/thumb?id={safe_id}&v={sb_v}" if sb_v else f"/api/screens/thumb?id={safe_id}",
                    "duration_sec": dur_sec,
                    "duration_str": duration_str,
                    "account": account_owner,
                    "repo": repo,
                    "blob": blob,
                    "hf_url": hf_url,
                    "size_bytes": size_bytes,
                    "size_str": f"{size_bytes / (1024 * 1024):.2f} MB",
                    "audio_size_str": audio_size_str,
                    "audio_codec": audio_codec,
                    "uploaded_at": info.get("uploaded_at", "N/A"),
                    "file_names": file_names,
                    "aliases": aliases,
                    "metadata": metadata,
                    "media_info": media_info,
                    "subtitles": info.get("subtitles") or [],
                    "has_audio": bool(info.get("audio")),
                    "audio_url": f"/stream/audio/{key}"
                }

                if can_decrypt:
                    seen_blobs[dedup_id] = len(files_data)
                    total_bytes += size_bytes
                    c_info = stream_cache.get_cache_info(safe_id, total_size=size_bytes, chunk_size=crypto_engine.DEFAULT_CHUNK_SIZE)
                    item["cache_pct"] = c_info["percentage"]
                    item["cached_bytes"] = c_info["cached_bytes"]
                    item["cached_size_str"] = c_info["cached_size_str"]
                    item["is_cached"] = c_info["cached"]
                    item["pinned_offline"] = c_info["pinned_offline"]
                    item["is_downloading"] = c_info["is_downloading"]
                    repos_set.add(repo)
                    files_data.append(item)
                else:
                    item["reason"] = "Cannot decrypt with current password (encrypted with an older or different key)"
                    skipped_files.append(item)

                    # Suppress indecryptable stream & NFO from Jellyfin
                    if account_manager.is_jellyfin_sync_enabled():
                        strm_path = Path("jellyfin_library") / f"{key}.strm"
                        if strm_path.exists():
                            try:
                                strm_path.unlink()
                            except Exception:
                                pass
                        metadata_helper.delete_nfo_files("jellyfin_library", key)

        files_json_str = json.dumps(files_data)
        skipped_json_str = json.dumps(skipped_files)
        if total_bytes >= 1024 * 1024 * 1024 * 1024:
            total_size_str = f"{total_bytes / (1024**4):.2f} TB"
        elif total_bytes >= 1024 * 1024 * 1024:
            total_size_str = f"{total_bytes / (1024**3):.2f} GB"
        elif total_bytes >= 1024 * 1024:
            total_size_str = f"{total_bytes / (1024**2):.2f} MB"
        elif total_bytes >= 1024:
            total_size_str = f"{total_bytes / 1024:.1f} KB"
        else:
            total_size_str = f"{total_bytes} B"
        total_size_mb = total_bytes / (1024 * 1024)

        c_stats = stream_cache.get_overall_stats()
        tot_c_bytes = c_stats.get("total_cached_bytes", 0)
        if tot_c_bytes >= 1024 * 1024 * 1024:
            total_cached_mb_str = f"{tot_c_bytes / (1024 * 1024 * 1024):.1f} GB"
        elif tot_c_bytes >= 1024 * 1024:
            total_cached_mb_str = f"{tot_c_bytes / (1024 * 1024):.1f} MB"
        elif tot_c_bytes >= 1024:
            total_cached_mb_str = f"{tot_c_bytes / 1024:.0f} KB"
        else:
            total_cached_mb_str = f"{tot_c_bytes} B"

        skipped_count = len(skipped_files)

        all_accounts = [a["username"] for a in self.acc_mgr.get_all_accounts()] if (hasattr(self, "acc_mgr") and self.acc_mgr) else [self.hf_username]
        primary_user = self.acc_mgr.primary_username if (hasattr(self, "acc_mgr") and self.acc_mgr) else self.hf_username
        total_accounts = len(all_accounts)
        is_jelly_on = account_manager.is_jellyfin_sync_enabled()
        jelly_badge_cls = "is-on" if is_jelly_on else "is-off"
        jelly_badge_text = "ON" if is_jelly_on else "OFF"

        skipped_stat_html = ""
        skipped_banner_html = ""

        if skipped_count > 0:
            skipped_stat_html = f"""
            <div class="stat-card" id="statSkippedCard" style="border-color: rgba(245, 158, 11, 0.4); background: rgba(245, 158, 11, 0.08); cursor: pointer;" onclick="openSkippedModal()">
                <div class="stat-label" style="color: #fbbf24;">&#128274; Skipped (Locked)</div>
                <div class="stat-value" id="statSkippedVal" style="color: #fbbf24;">{skipped_count}</div>
            </div>"""

            skipped_banner_html = f"""
            <div class="skipped-banner" id="skippedBanner">
                <div class="skipped-banner-left">
                    <span style="font-size: 1.4rem;">&#9888;</span>
                    <div>
                        <div style="font-weight: 600; color: #fbbf24; font-size: 0.95rem;">
                            {skipped_count} video(s) skipped — cannot decrypt with current master password
                        </div>
                        <div style="font-size: 0.82rem; color: #d1d5db; margin-top: 2px;">
                            These files were encrypted with an older or different password. They are hidden from your Jellyfin library.
                        </div>
                    </div>
                </div>
                <button onclick="openSkippedModal()" class="tool-btn" style="border-color: #f59e0b; color: #fbbf24; background: rgba(245, 158, 11, 0.15);">
                    &#128269; Review Skipped ({skipped_count})
                </button>
            </div>"""

        from web.templates import dashboard_template
        html = dashboard_template.render_dashboard(
            primary_user=primary_user,
            total_accounts=total_accounts,
            total_repos=len(repos_set),
            total_files=len(files_data),
            total_cached_mb_str=total_cached_mb_str,
            total_size_str=total_size_str,
            jelly_badge_cls=jelly_badge_cls,
            jelly_badge_text=jelly_badge_text,
            skipped_stat_html=skipped_stat_html,
            skipped_banner_html=skipped_banner_html,
            files_json_str=files_json_str,
            skipped_json_str=skipped_json_str,
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html.encode("utf-8"))))
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def handle_stream_subtitle(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        key = qs.get("key", [""])[0]
        if not key and len(parsed.path) > len("/stream/subtitle/"):
            key = unquote(parsed.path[len("/stream/subtitle/"):])
        lang = qs.get("lang", [""])[0]

        clean_key = key.strip("/").replace("\\", "/")
        manifest = load_manifest()
        file_info = manifest.get("files", {}).get(clean_key)
        if not file_info:
            for k, info in manifest.get("files", {}).items():
                if clean_key in (info.get("file_names") or []) or Path(clean_key).name in (info.get("aliases") or []):
                    file_info = info
                    clean_key = k
                    break

        if not file_info:
            self.send_error(404, f"Video {key} not found")
            return

        subtitles = file_info.get("subtitles") or []
        if not subtitles:
            empty_vtt = "WEBVTT\n\n".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/vtt; charset=utf-8")
            self.send_header("Content-Length", str(len(empty_vtt)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(empty_vtt)
            return

        target_sub = None
        if lang:
            for s in subtitles:
                if s.get("lang") == lang:
                    target_sub = s
                    break
        if not target_sub:
            target_sub = subtitles[0]

        sub_blob = target_sub.get("blob_path")
        repo_id = target_sub.get("repo_id") or file_info.get("repo_id")
        repo_token = self.acc_mgr.get_token_for_repo(repo_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else self.token

        headers = {}
        if repo_token:
            headers["Authorization"] = f"Bearer {repo_token}"

        hf_url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{sub_blob}"
        try:
            resp = self.hf_session.get(hf_url, headers=headers, timeout=15)
            if resp.status_code != 200:
                empty_vtt = "WEBVTT\n\n".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/vtt; charset=utf-8")
                self.send_header("Content-Length", str(len(empty_vtt)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(empty_vtt)
                return

            enc_bytes = resp.content
            cand_keys = self.acc_mgr.get_candidate_keys_for_repo(repo_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else [self.password]
            matched_pw, dec_bytes = crypto_engine.find_working_key_for_bytes(cand_keys, enc_bytes)
            if not dec_bytes:
                raise ValueError("Unable to decrypt subtitle with any candidate key")
            sub_text = companion_helper.decode_subtitle_bytes(dec_bytes)
            vtt_text = companion_helper.srt_to_webvtt(sub_text)
            body = vtt_text.encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "text/vtt; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            sys.stderr.write(f"[!] Subtitle stream error: {e}\n")
            empty_vtt = "WEBVTT\n\n".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/vtt; charset=utf-8")
            self.send_header("Content-Length", str(len(empty_vtt)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(empty_vtt)

    def extract_and_upload_companion_audio(self, clean_key: str, file_info: dict) -> dict:
        """
        Extracts companion audio on-the-fly via the local proxy stream,
        encrypts it with AES-256-GCM, uploads it to Hugging Face,
        and saves it to upload_manifest.json.
        """
        try:
            repo_id = file_info.get("repo_id")
            blob_path = file_info.get("blob_path") or file_info.get("remote_path")
            if not repo_id or not blob_path:
                return None

            import imageio_ffmpeg
            import subprocess
            from urllib.parse import quote
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            port = self.server.server_port if hasattr(self.server, "server_port") else 8080
            stream_url = f"http://127.0.0.1:{port}/stream/{quote(clean_key)}?full=1&no_cache=1"
            safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", clean_key)
            tmp_m4a = f"temp_audio_{safe_id}.m4a"

            # 1. Fast copy
            cmd = [ffmpeg, "-y", "-i", stream_url, "-vn", "-c:a", "copy", tmp_m4a]
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
            if not os.path.exists(tmp_m4a) or os.path.getsize(tmp_m4a) < 500:
                # 2. AAC encode fallback
                cmd = [ffmpeg, "-y", "-i", stream_url, "-vn", "-c:a", "aac", "-b:a", "192k", tmp_m4a]
                subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)

            if not os.path.exists(tmp_m4a) or os.path.getsize(tmp_m4a) < 500:
                if os.path.exists(tmp_m4a):
                    os.remove(tmp_m4a)
                return None

            orig_size = os.path.getsize(tmp_m4a)
            tmp_enc = f"{tmp_m4a}.enc"
            enc_size = crypto_engine.encrypt_file(tmp_m4a, tmp_enc, self.password)

            a_blob = blob_path.replace(".enc", ".audio.m4a.enc")
            repo_token = self.acc_mgr.get_token_for_repo(repo_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else self.token
            api = HfApi(token=repo_token)
            api.upload_file(
                path_or_fileobj=tmp_enc,
                path_in_repo=a_blob,
                repo_id=repo_id,
                repo_type="dataset",
                commit_message=commit_helper.get_human_commit_message("upload")
            )

            audio_record = {
                "blob_path": a_blob,
                "repo_id": repo_id,
                "account": file_info.get("account") or (repo_id.split("/")[0] if "/" in repo_id else self.hf_username),
                "orig_size": orig_size,
                "size_bytes": orig_size,
                "enc_size": enc_size,
                "format": "m4a",
                "verifier": crypto_engine.create_verifier(self.password)
            }

            manifest = load_manifest()
            if clean_key in manifest.get("files", {}):
                manifest["files"][clean_key]["audio"] = audio_record
                save_manifest(manifest)

            file_info["audio"] = audio_record

            if os.path.exists(tmp_m4a):
                os.remove(tmp_m4a)
            if os.path.exists(tmp_enc):
                os.remove(tmp_enc)

            sys.stdout.write(f"[+] Companion Audio extracted & uploaded: {clean_key} ({orig_size / (1024*1024):.2f} MB)\n")
            return audio_record
        except Exception as e:
            sys.stderr.write(f"[!] Companion audio extraction error for {clean_key}: {e}\n")
            return None

    def handle_stream_audio(self, path_after_stream: str):
        clean_key = path_after_stream.strip("/").replace("\\", "/")
        manifest = load_manifest()
        file_info = manifest.get("files", {}).get(clean_key)
        if not file_info:
            for k, info in manifest.get("files", {}).items():
                if clean_key in (info.get("file_names") or []) or Path(clean_key).name in (info.get("aliases") or []):
                    file_info = info
                    clean_key = k
                    break

        if not file_info:
            self.send_error(404, f"File {clean_key} not found")
            return

        verifier = file_info.get("verifier")
        if verifier and not crypto_engine.verify_password(verifier, self.password):
            self.send_error(403, "Forbidden: Invalid master password")
            return

        audio_info = file_info.get("audio")
        if audio_info and audio_info.get("blob_path"):
            a_blob = audio_info["blob_path"]
            a_repo = audio_info.get("repo_id") or file_info.get("repo_id")
            self.handle_stream(path_after_stream=clean_key, custom_content_type="audio/mp4", repo_id=a_repo, blob_path=a_blob)
            return

        # Check local cache first for instant streaming / download without network lag
        safe_id = storyboard_helper.get_safe_id(clean_key)
        local_dir = os.path.join("storyboards", "audio")
        os.makedirs(local_dir, exist_ok=True)
        local_m4a = os.path.join(local_dir, f"{safe_id}.m4a")

        if not os.path.exists(local_m4a) or os.path.getsize(local_m4a) < 1000:
            import imageio_ffmpeg
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            port = self.server.server_port if hasattr(self.server, "server_port") else 8080
            stream_url = f"http://127.0.0.1:{port}/stream/{quote(clean_key)}?full=1&no_cache=1"
            cmd = [ffmpeg, "-y", "-i", stream_url, "-vn", "-c:a", "copy", local_m4a]
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
            if not os.path.exists(local_m4a) or os.path.getsize(local_m4a) < 1000:
                cmd = [ffmpeg, "-y", "-i", stream_url, "-vn", "-c:a", "aac", "-b:a", "192k", local_m4a]
                subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)

        if os.path.exists(local_m4a) and os.path.getsize(local_m4a) >= 1000:
            sz = os.path.getsize(local_m4a)
            fname = f"{Path(clean_key).stem}.m4a"

            range_header = self.headers.get("Range")
            if range_header:
                suffix_m = re.match(r"^bytes=-(\d+)$", range_header.strip())
                range_m = re.match(r"^bytes=(\d+)-(\d*)$", range_header.strip())
                if suffix_m:
                    suffix_len = int(suffix_m.group(1))
                    start = max(0, sz - suffix_len)
                    end = sz - 1
                    match = True
                elif range_m:
                    start = int(range_m.group(1))
                    end = int(range_m.group(2)) if range_m.group(2) else sz - 1
                    end = min(sz - 1, end)
                    match = True
                else:
                    match = False
                if match:
                    length = end - start + 1
                    self.send_response(206)
                    self.send_header("Content-Type", "audio/mp4")
                    self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
                    self.send_header("Content-Range", f"bytes {start}-{end}/{sz}")
                    self.send_header("Content-Length", str(length))
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    with open(local_m4a, "rb") as f_in:
                        f_in.seek(start)
                        rem = length
                        while rem > 0:
                            chunk = f_in.read(min(rem, 256 * 1024))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            rem -= len(chunk)
                    return

            self.send_response(200)
            self.send_header("Content-Type", "audio/mp4")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", str(sz))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with open(local_m4a, "rb") as f_in:
                while True:
                    chunk = f_in.read(256 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            self.wfile.flush()

            # Schedule background HF upload
            def _bg_upload_audio(ck, fi):
                try:
                    self.extract_and_upload_companion_audio(ck, fi)
                except Exception as e:
                    logger.warning("Background audio upload error: %s", e)
            threading.Thread(target=_bg_upload_audio, args=(clean_key, file_info), daemon=True).start()
            return

        self.send_error(500, "Failed to stream companion audio")

    def handle_stream(self, path_after_stream: str, custom_content_type: str = None, repo_id: str = None, blob_path: str = None):
        print(f'[DEBUG_STREAM] handle_stream: {path_after_stream}', flush=True)
        clean_key = path_after_stream.strip("/").replace("\\", "/")
        manifest = load_manifest()
        if clean_key in manifest.get("files", {}):
            verifier = manifest["files"][clean_key].get("verifier")
            if verifier and not crypto_engine.verify_password(verifier, self.password):
                self.send_error(403, "Forbidden: Video cannot be decrypted with current master password.")
                return

        meta = self.get_file_metadata(path_after_stream, repo_id=repo_id, blob_path=blob_path, custom_content_type=custom_content_type)
        if not meta:
            # Ghost file detected (blob/repo was deleted on HF) -> auto-prune from local manifest and master index
            try:
                if clean_key in manifest.get("files", {}):
                    manifest["files"].pop(clean_key, None)
                    manifest.setdefault("tombstones", {})[clean_key] = time.time()
                    save_manifest(manifest)
                    self.sync_master_index_to_cloud()
                    activity_tracker.tracker.notify_manifest_updated()
            except Exception:
                pass
            self.send_error(404, f"File {path_after_stream} not found in Hugging Face dataset")
            return

        hdr = meta["header"]
        total_size = hdr["total_size"]
        chunk_size = hdr["chunk_size"]
        aesgcm = meta["aesgcm"]
        hf_url = meta["url"]
        cache_id = meta.get("cache_id") or storyboard_helper.get_safe_id(clean_key)

        if custom_content_type:
            content_type = custom_content_type
        else:
            mime, _ = mimetypes.guess_type(path_after_stream)
            content_type = mime or "video/mp4"

        repo_token = meta.get("repo_token") or (self.acc_mgr.get_token_for_repo(meta.get("repo_id")) if (hasattr(self, "acc_mgr") and self.acc_mgr) else self.token)

        def read_exact_bytes(raw_stream, num_bytes):
            buf = bytearray()
            while len(buf) < num_bytes:
                part = raw_stream.read(num_bytes - len(buf))
                if not part:
                    break
                buf.extend(part)
            return bytes(buf)

        range_header = self.headers.get("Range")
        if range_header:
            suffix_match = re.match(r"^bytes=-(\d+)$", range_header.strip())
            range_match = re.match(r"^bytes=(\d+)-(\d*)$", range_header.strip())
            if suffix_match:
                suffix_len = int(suffix_match.group(1))
                if suffix_len <= 0:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total_size}")
                    self.end_headers()
                    return
                start = max(0, total_size - suffix_len)
                end = total_size - 1
            elif range_match:
                start = int(range_match.group(1))
                if start >= total_size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{total_size}")
                    self.end_headers()
                    return

                STREAM_BURST = 4 * 1024 * 1024
                is_audio_stream = bool(custom_content_type and "audio" in custom_content_type) or "/stream/audio/" in self.path
                is_full_stream = ('full=1' in self.path or 'no_burst=1' in self.path or is_audio_stream or 'download' in self.path)
                if range_match.group(2):
                    end = min(total_size - 1, int(range_match.group(2)))
                elif is_full_stream:
                    end = total_size - 1
                else:
                    end = min(total_size - 1, start + STREAM_BURST - 1)
            else:
                self.send_error(400, "Invalid Range Header")
                return

            range_info = crypto_engine.calculate_byte_range_chunks(total_size, chunk_size, start, end)
            req_len = range_info["req_length"]
            first_chunk = range_info["first_chunk"]
            last_chunk = range_info["last_chunk"]

            is_no_cache = ("no_cache=1" in self.path) or (self.headers.get("X-Internal-No-Cache") == "1")
            is_pinned_offline = (stream_cache.is_pinned_offline(cache_id) or stream_cache.is_downloading_offline(cache_id)) and not is_no_cache

            # Query the encrypted chunk cache for contiguous spans (cached vs missing)
            spans = stream_cache.get_chunk_spans(cache_id, first_chunk, last_chunk, total_size, chunk_size)

            # Send 206 Partial Content response headers
            self.send_response(206)
            self.send_header("Content-Type", content_type)
            if custom_content_type and "audio" in custom_content_type:
                self.send_header("Content-Disposition", f'attachment; filename="{Path(clean_key).stem}.m4a"')
            else:
                self.send_header("Content-Disposition", "inline")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Range", f"bytes {start}-{end}/{total_size}")
            self.send_header("Content-Length", str(req_len))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            try:
                for is_cached, span_start, span_end in spans:
                    if is_cached:
                        # 100% Cache hit: read encrypted chunks directly from local disk (ZERO cloud download!)
                        for c_idx in range(span_start, span_end + 1):
                            chunk_cipher = stream_cache.read_chunk_ciphertext(cache_id, c_idx, total_size, chunk_size)
                            if not chunk_cipher:
                                break
                            nonce = crypto_engine.get_chunk_nonce(hdr["base_nonce"], c_idx)
                            try:
                                decrypted_block = aesgcm.decrypt(nonce, chunk_cipher, None)
                            except Exception:
                                decrypted_block = b"\x00" * max(0, min(chunk_size, total_size - c_idx * chunk_size))

                            chunk_start_plain = c_idx * chunk_size
                            slice_start = max(0, start - chunk_start_plain)
                            slice_end = min(len(decrypted_block), end - chunk_start_plain + 1)
                            if slice_start < slice_end:
                                self.wfile.write(decrypted_block[slice_start:slice_end])
                                self.wfile.flush()
                    else:
                        # Cache miss: fetch ONLY missing chunks from Hugging Face and dynamically save to local encrypted cache!
                        span_range = crypto_engine.calculate_byte_range_chunks(
                            total_size, chunk_size, span_start * chunk_size, min(total_size - 1, (span_end + 1) * chunk_size - 1)
                        )
                        hf_headers = {}
                        if repo_token:
                            hf_headers["Authorization"] = f"Bearer {repo_token}"
                        hf_headers["Range"] = f"bytes={span_range['enc_start']}-{span_range['enc_end']}"

                        resp = None
                        try:
                            resp = self.hf_session.get(hf_url, headers=hf_headers, stream=True, timeout=25)
                            if resp.status_code not in (200, 206):
                                break
                            stream_raw = resp.raw

                            for c_idx in range(span_start, span_end + 1):
                                c_len = stream_cache.get_chunk_cipher_length(total_size, chunk_size, c_idx)
                                chunk_cipher = read_exact_bytes(stream_raw, c_len)
                                if not chunk_cipher or len(chunk_cipher) < c_len:
                                    break

                                nonce = crypto_engine.get_chunk_nonce(hdr["base_nonce"], c_idx)
                                try:
                                    decrypted_block = aesgcm.decrypt(nonce, chunk_cipher, None)
                                except Exception:
                                    # Fallback gracefully for damaged remainder tails so stream is never aborted
                                    decrypted_block = b"\x00" * max(0, min(chunk_size, total_size - c_idx * chunk_size))

                                # Save verified encrypted chunk dynamically to local sparse cache ONLY if pinned offline
                                if is_pinned_offline:
                                    stream_cache.write_chunk_ciphertext(cache_id, c_idx, chunk_cipher, total_size, chunk_size)

                                chunk_start_plain = c_idx * chunk_size
                                slice_start = max(0, start - chunk_start_plain)
                                slice_end = min(len(decrypted_block), end - chunk_start_plain + 1)
                                if slice_start < slice_end:
                                    self.wfile.write(decrypted_block[slice_start:slice_end])
                                    self.wfile.flush()
                        finally:
                            if resp is not None:
                                try:
                                    resp.close()
                                except Exception:
                                    pass

            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:
                return

        else:
            # Non-range request: stream full file using chunk spans (local cache first, cloud for missing)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            if custom_content_type and "audio" in custom_content_type:
                self.send_header("Content-Disposition", f'attachment; filename="{Path(clean_key).stem}.m4a"')
            else:
                self.send_header("Content-Disposition", "inline")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(total_size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            total_chunks = (total_size + chunk_size - 1) // chunk_size
            is_no_cache = ("no_cache=1" in self.path) or (self.headers.get("X-Internal-No-Cache") == "1")
            is_pinned_offline = (stream_cache.is_pinned_offline(cache_id) or stream_cache.is_downloading_offline(cache_id)) and not is_no_cache
            spans = stream_cache.get_chunk_spans(cache_id, 0, total_chunks - 1, total_size, chunk_size)

            try:
                for is_cached, span_start, span_end in spans:
                    if is_cached:
                        for c_idx in range(span_start, span_end + 1):
                            chunk_cipher = stream_cache.read_chunk_ciphertext(cache_id, c_idx, total_size, chunk_size)
                            if not chunk_cipher:
                                break
                            nonce = crypto_engine.get_chunk_nonce(hdr["base_nonce"], c_idx)
                            decrypted = aesgcm.decrypt(nonce, chunk_cipher, None)
                            self.wfile.write(decrypted)
                            self.wfile.flush()
                    else:
                        span_range = crypto_engine.calculate_byte_range_chunks(
                            total_size, chunk_size, span_start * chunk_size, min(total_size - 1, (span_end + 1) * chunk_size - 1)
                        )
                        hf_headers = {}
                        if repo_token:
                            hf_headers["Authorization"] = f"Bearer {repo_token}"
                        hf_headers["Range"] = f"bytes={span_range['enc_start']}-{span_range['enc_end']}"

                        resp = self.hf_session.get(hf_url, headers=hf_headers, stream=True, timeout=30)
                        if resp.status_code not in (200, 206):
                            break
                        stream_raw = resp.raw

                        for c_idx in range(span_start, span_end + 1):
                            c_len = stream_cache.get_chunk_cipher_length(total_size, chunk_size, c_idx)
                            chunk_cipher = read_exact_bytes(stream_raw, c_len)
                            if not chunk_cipher or len(chunk_cipher) < c_len:
                                break

                            nonce = crypto_engine.get_chunk_nonce(hdr["base_nonce"], c_idx)
                            decrypted = aesgcm.decrypt(nonce, chunk_cipher, None)
                            if is_pinned_offline:
                                stream_cache.write_chunk_ciphertext(cache_id, c_idx, chunk_cipher, total_size, chunk_size)
                            self.wfile.write(decrypted)
                            self.wfile.flush()

            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:
                return


def run_server(port=8080, password=None):
    acc_mgr = account_manager.AccountManager()
    upload_acc = acc_mgr.get_upload_account()
    if not upload_acc:
        print("[!] Error: No valid Hugging Face upload account found. Check env.txt.")
        sys.exit(1)

    user = upload_acc["username"]
    token = upload_acc["token"]
    password = password or acc_mgr.password

    if not password:
        print("[!] Error: Master encryption password not found.")
        sys.exit(1)

    InteractiveJellyfinStreamHandler.acc_mgr = acc_mgr
    InteractiveJellyfinStreamHandler.hf_username = user
    InteractiveJellyfinStreamHandler.token = token
    InteractiveJellyfinStreamHandler.password = password
    InteractiveJellyfinStreamHandler.name_key = crypto_engine.derive_name_key(password)
    InteractiveJellyfinStreamHandler.hf_session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=50, pool_maxsize=100, max_retries=3, pool_block=False)
    InteractiveJellyfinStreamHandler.hf_session.mount("https://", adapter)
    InteractiveJellyfinStreamHandler.hf_session.mount("http://", adapter)

    def _prewarm_daemon():
        try:
            m = load_manifest()
            if not m.get("files") and acc_mgr:
                print("[*] Empty manifest detected. Auto-syncing from connected Hugging Face account(s)...")
                try:
                    res = acc_mgr.scan_and_merge_all_accounts(m, library_dir="jellyfin_library", proxy_host=f"127.0.0.1:{port}")
                    save_manifest(m)
                    print(f"[*] Cloud restore completed: {res.get('discovered_count', 0)} files restored into manifest.")
                except Exception as ex:
                    print(f"[!] Note: Automatic cloud scan error: {ex}")
            f_items = list(m.get("files", {}).items())
            def _pw(item):
                k, info = item
                r = info.get("repo_id")
                b = info.get("blob_path")
                if not r or not b or b in FILE_CACHE:
                    return
                t = acc_mgr.get_token_for_repo(r)
                u = f"https://huggingface.co/datasets/{r}/resolve/main/{b}"
                h = {"Range": f"bytes=0-{crypto_engine.HEADER_SIZE - 1}"}
                if t:
                    h["Authorization"] = f"Bearer {t}"
                try:
                    resp = InteractiveJellyfinStreamHandler.hf_session.get(u, headers=h, timeout=12)
                    if resp.status_code in (200, 206):
                        content_bytes = resp.content
                        resp.close()
                        hdr = crypto_engine.read_header(content_bytes)
                        key = crypto_engine.derive_key(password, hdr["salt"])
                        aes = AESGCM(key)
                        meta = {"header": hdr, "key": key, "aesgcm": aes, "url": u, "repo_id": r, "repo_token": t}
                        FILE_CACHE[k] = meta
                        FILE_CACHE[b] = meta
                        clean_k = k.strip("/").replace("\\", "/")
                        FILE_CACHE[clean_k] = meta
                    else:
                        resp.close()
                except Exception:
                    pass

            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=6) as ex:
                ex.map(_pw, f_items)
            print(f"[*] Pre-warmed {len(f_items)} stream headers into memory for instant playback.")
        except Exception as e:
            print(f"[!] Warning prewarming headers: {e}")

    threading.Thread(target=_prewarm_daemon, daemon=True).start()
    # Lightweight Range-seeking storyboard pre-warming (pulls only ~3 MB keyframes per video)
    storyboard_helper.prewarm_all_storyboards(port=port)
    # Start Cloud Commit Watcher for real-time multi-server synchronization
    cloud_watcher.get_cloud_watcher().start()

    server_address = ("0.0.0.0", port)
    httpd = ThreadedHTTPServer(server_address, InteractiveJellyfinStreamHandler)

    all_accounts = [a["username"] for a in acc_mgr.get_all_accounts()]
    print("=" * 72)
    print("  HFSYNC: Interactive Tree View & Multi-Account Streaming Bridge")
    print("=" * 72)
    print(f"Primary Upload User : {user} [Primary] (All new uploads)")
    print(f"Connected Accounts  : {', '.join(all_accounts)} ({len(all_accounts)} total)")
    print(f"Streaming URL       : http://127.0.0.1:{port}")
    print(f"Web Dashboard       : http://127.0.0.1:{port}/")
    print(f"Features            : Multi-Account, Categories, Tags, NFO Sync, In-Browser Play")
    print("=" * 72)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping streaming proxy.")
        httpd.server_close()


def main():
    import argparse
    default_port = int(os.environ.get("PORT", 8080))
    parser = argparse.ArgumentParser(description="Interactive Tree View Jellyfin Streaming Proxy")
    parser.add_argument("--port", "-p", type=int, default=default_port, help=f"Port to listen on (default: {default_port})")
    parser.add_argument("--password", help="Master encryption password")
    args = parser.parse_args()

    run_server(port=args.port, password=args.password)


if __name__ == "__main__":
    main()
