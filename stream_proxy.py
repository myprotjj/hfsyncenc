import os
import sys
import json
import shutil
import mimetypes
import re
import requests
import threading
from urllib.parse import urlparse, unquote, parse_qs, quote
import storyboard_helper
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from pathlib import Path
from huggingface_hub import HfApi
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import crypto_engine
import commit_helper
import metadata_helper
import account_manager
import activity_tracker
import companion_helper
import screens_helper

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


def load_manifest(manifest_path="upload_manifest.json"):
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "files" in data:
                    return data
                return {"files": data, "media_hashes": {}}
        except Exception:
            return {"files": {}, "media_hashes": {}}
    return {"files": {}, "media_hashes": {}}


def save_manifest(manifest, manifest_path="upload_manifest.json"):
    temp_path = f"{manifest_path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    os.replace(temp_path, manifest_path)


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

    def resolve_remote_blob(self, path_after_stream: str):
        manifest = load_manifest()
        clean_key = path_after_stream.strip("/").replace("\\", "/")

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
        # 1. Exact key match
        if clean in manifest.get("files", {}):
            return clean
        # 2. Match by safe_id or file_names or aliases
        for k, v in manifest.get("files", {}).items():
            if storyboard_helper.get_safe_id(k) == clean:
                return k
            if clean in (v.get("file_names") or []) or clean in (v.get("aliases") or []):
                return k
        return clean

    def get_file_metadata(self, path_after_stream: str, repo_id: str = None, blob_path: str = None):
        is_custom_blob = bool(repo_id and blob_path)
        if is_custom_blob:
            cache_key = f"{repo_id}:{blob_path}"
            if cache_key in FILE_CACHE:
                return FILE_CACHE[cache_key]
            if blob_path in FILE_CACHE:
                return FILE_CACHE[blob_path]
        else:
            clean_key = path_after_stream.strip("/").replace(b"\\".decode("ascii"), "/")
            if clean_key in FILE_CACHE:
                return FILE_CACHE[clean_key]
            if path_after_stream in FILE_CACHE:
                return FILE_CACHE[path_after_stream]
            repo_id, blob_path = self.resolve_remote_blob(path_after_stream)
            cache_key = f"{repo_id}:{blob_path}"
            if cache_key in FILE_CACHE:
                return FILE_CACHE[cache_key]

        hf_url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{blob_path}"

        headers = {}
        repo_token = self.acc_mgr.get_token_for_repo(repo_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else self.token
        if repo_token:
            headers["Authorization"] = f"Bearer {repo_token}"
        headers["Range"] = f"bytes=0-{crypto_engine.HEADER_SIZE - 1}"

        try:
            resp = self.hf_session.get(hf_url, headers=headers, allow_redirects=True, timeout=15)
        except Exception as e:
            print(f"[!] Network error fetching from {hf_url}: {e}")
            return None

        if resp.status_code not in (200, 206):
            fallback_url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{path_after_stream.split('/', 1)[-1]}.enc"
            try:
                resp = self.hf_session.get(fallback_url, headers=headers, allow_redirects=True, timeout=15)
                if resp.status_code in (200, 206):
                    hf_url = fallback_url
                else:
                    return None
            except Exception:
                return None

        try:
            hdr = crypto_engine.read_header(resp.content)
            key = crypto_engine.derive_key(self.password, hdr["salt"])
            aesgcm = AESGCM(key)
        except Exception as e:
            print(f"[!] Header parsing error: {e}")
            return None

        metadata = {
            "header": hdr,
            "key": key,
            "aesgcm": aesgcm,
            "url": hf_url,
            "repo_id": repo_id,
            "repo_token": repo_token
        }
        FILE_CACHE[cache_key] = metadata
        if blob_path:
            FILE_CACHE[blob_path] = metadata
        if not is_custom_blob:
            clean_key = path_after_stream.strip("/").replace(b"\\".decode("ascii"), "/")
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
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/api/delete":
            self.handle_delete()
        elif path == "/api/delete-folder":
            self.handle_delete_folder()
        elif path == "/api/metadata":
            self.handle_update_metadata()
        elif path == "/api/folder-tags":
            self.handle_folder_tags()
        elif path == "/api/sync-hf":
            self.handle_sync_hf()
        elif path in ("/api/restore-hf", "/api/recheck-cloud"):
            self.handle_restore_hf()
        elif path == "/api/delete-skipped":
            self.handle_delete_skipped()
        elif path == "/api/accounts/add":
            self.handle_accounts_add()
        elif path == "/api/accounts/toggle":
            self.handle_accounts_toggle()
        elif path == "/api/accounts/set-primary":
            self.handle_accounts_set_primary()
        elif path == "/api/accounts/delete":
            self.handle_accounts_delete()
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
                                metadata_helper.sync_repo_manifest_to_hf(acc["api"], r_id, r_files, self.password)
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
        manifest = load_manifest()
        if hasattr(self, "acc_mgr") and self.acc_mgr:
            res = self.acc_mgr.scan_and_merge_all_accounts(manifest, library_dir="jellyfin_library")
            save_manifest(manifest)
            recovered_count = res.get("discovered_count", 0)
            skipped_count = res.get("skipped_count", 0)
            msg = f"Restored {recovered_count} item(s) across {res.get('total_accounts', 1)} HF account(s) ({skipped_count} skipped/incompatible)."
        else:
            api = HfApi(token=self.token)
            res = metadata_helper.restore_library_from_hf(
                api=api,
                password=self.password,
                hf_username=self.hf_username,
                library_dir="jellyfin_library",
                manifest_path="upload_manifest.json"
            )
            recovered_count = res.get("recovered_count", 0)
            skipped_count = res.get("skipped_count", 0)
            msg = f"Restored {recovered_count} item(s) from HF ({skipped_count} skipped/incompatible)."

        resp_body = json.dumps({
            "success": True,
            "recovered_count": recovered_count,
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
                api = self.acc_mgr.get_api_for_repo(repo_id)
                for k, rec in r_files.items():
                    blob_path = rec.get("blob_path") or rec.get("remote_path")
                    if blob_path:
                        rec_copy = dict(rec)
                        rec_copy["full_key"] = k
                        metadata_helper.sync_item_metadata_to_hf(api, repo_id, blob_path, rec_copy, self.password)
                        count += 1
                metadata_helper.sync_repo_manifest_to_hf(api, repo_id, r_files, self.password)
        else:
            api = HfApi(token=self.token)
            count = metadata_helper.sync_all_manifest_metadata_to_hf(
                api=api,
                manifest=manifest,
                password=self.password,
                hf_username=self.hf_username,
                default_repo=self.default_repo
            )

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
        except Exception:
            self.send_error(400, "Invalid JSON payload")
            return

        if not key:
            self.send_error(400, "Missing 'key' in request")
            return

        manifest = load_manifest()
        if key not in manifest["files"]:
            self.send_error(404, f"Video key '{key}' not found in manifest")
            return

        title = str(meta_in.get("title", "")).strip() or Path(key).stem.replace("_", " ").title()
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

        # Update manifest & record verifier
        manifest["files"][key]["metadata"] = clean_meta
        manifest["files"][key]["verifier"] = crypto_engine.create_verifier(self.password)
        save_manifest(manifest)

        # Write Jellyfin NFO files (.nfo, .mp4.nfo, .strm.nfo)
        metadata_helper.write_nfo_files("jellyfin_library", key, clean_meta)

        # Sync encrypted metadata to Hugging Face
        file_info = manifest["files"][key]
        repo_id = file_info.get("repo_id", f"{self.hf_username}/{self.default_repo}")
        api = self.acc_mgr.get_api_for_repo(repo_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else HfApi(token=self.token)
        blob_path = file_info.get("blob_path") or file_info.get("remote_path")
        if blob_path:
            record = dict(file_info)
            record["full_key"] = key
            metadata_helper.sync_item_metadata_to_hf(api, repo_id, blob_path, record, self.password)
            repo_files = {k: v for k, v in manifest["files"].items() if v.get("repo_id") == repo_id}
            metadata_helper.sync_repo_manifest_to_hf(api, repo_id, repo_files, self.password)

        resp_body = json.dumps({
            "success": True,
            "key": key,
            "metadata": clean_meta,
            "message": "Metadata updated, saved to Jellyfin .nfo, and synced encrypted to Hugging Face."
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
                        metadata_helper.sync_repo_manifest_to_hf(api, r_id, repo_files, self.password)
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
                        metadata_helper.sync_repo_manifest_to_hf(api, r_id, repo_files, self.password)
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
                        metadata_helper.sync_repo_manifest_to_hf(api, r_id, repo_files, self.password)
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

        # Delete local .strm files for the removed alias
        for strm_candidate in Path("jellyfin_library").glob("**/*.strm"):
            if strm_candidate.name in (f"{alias_basename}.strm", f"{alias_to_remove}.strm"):
                try:
                    strm_candidate.unlink()
                except Exception as e:
                    print(f"[!] Warning deleting alias strm: {e}")

        # Delete local .nfo files for the removed alias
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

        # Create .strm file for new alias
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

        deleted_count = 0

        for key in matching_keys:
            info = manifest["files"].get(key, {})
            repo_id = info.get("repo_id", f"{self.hf_username}/{self.default_repo}")
            blob_path = info.get("blob_path") or info.get("remote_path")
            media_hash = info.get("media_hash") or info.get("content_hash") or info.get("fingerprint")
            api = self.acc_mgr.get_api_for_repo(repo_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else HfApi(token=self.token)

            if blob_path:
                try:
                    api.delete_file(
                        path_in_repo=blob_path,
                        repo_id=repo_id,
                        repo_type="dataset",
                        commit_message=commit_helper.get_human_commit_message("delete")
                    )
                except Exception as e:
                    print(f"[!] Warning deleting from HF ({blob_path}): {e}")

                metadata_helper.delete_item_metadata_from_hf(api, repo_id, blob_path)

            strm_path = Path("jellyfin_library") / f"{key}.strm"
            if strm_path.exists():
                try:
                    strm_path.unlink()
                except Exception:
                    pass

            metadata_helper.delete_nfo_files("jellyfin_library", key)

            FILE_CACHE.pop(key, None)
            manifest["files"].pop(key, None)
            if media_hash:
                for table in ("media_hashes", "content_hashes", "fingerprints"):
                    if table in manifest and media_hash in manifest[table]:
                        manifest[table].pop(media_hash, None)

            deleted_count += 1

        local_folder_path = Path("jellyfin_library") / folder_prefix
        if local_folder_path.exists():
            try:
                shutil.rmtree(str(local_folder_path), ignore_errors=True)
            except Exception:
                pass

        remove_empty_folders("jellyfin_library")
        save_manifest(manifest)

        resp_body = json.dumps({
            "success": True,
            "folder": folder_prefix,
            "deleted_count": deleted_count,
            "message": f"Successfully deleted folder '{folder_prefix}' ({deleted_count} video(s)) from Hugging Face and Jellyfin library."
        }).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)


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

        info = manifest["files"][key_to_delete]
        repo_id = info.get("repo_id", f"{self.hf_username}/{self.default_repo}")
        blob_path = info.get("blob_path") or info.get("remote_path")
        media_hash = info.get("media_hash") or info.get("content_hash") or info.get("fingerprint")

        api = self.acc_mgr.get_api_for_repo(repo_id) if (hasattr(self, "acc_mgr") and self.acc_mgr) else HfApi(token=self.token)

        # Check if other video entries share this blob_path or media_hash
        other_files_using_blob = [
            k for k, v in manifest["files"].items()
            if k != key_to_delete and (v.get("blob_path") == blob_path or (media_hash and (v.get("media_hash") == media_hash or v.get("content_hash") == media_hash)))
        ]

        hf_deleted = False
        if other_files_using_blob:
            # Multiple names for this video: delete only this filename alias
            base_del = Path(key_to_delete).name
            for ok in other_files_using_blob:
                other_rec = manifest["files"][ok]
                if "file_names" in other_rec and key_to_delete in other_rec["file_names"]:
                    other_rec["file_names"].remove(key_to_delete)
                if "aliases" in other_rec and base_del in other_rec["aliases"]:
                    other_rec["aliases"].remove(base_del)
                if "metadata" in other_rec and other_rec["metadata"]:
                    other_rec["metadata"]["file_names"] = other_rec.get("file_names", [])
                    other_rec["metadata"]["aliases"] = other_rec.get("aliases", [])
                metadata_helper.write_nfo_files("jellyfin_library", ok, other_rec.get("metadata", {}))

            if media_hash and "media_hashes" in manifest:
                manifest["media_hashes"][media_hash] = manifest["files"][other_files_using_blob[0]]
        else:
            # Last reference: delete cloud blob on Hugging Face
            try:
                if blob_path:
                    api.delete_file(
                        path_in_repo=blob_path,
                        repo_id=repo_id,
                        repo_type="dataset",
                        commit_message=commit_helper.get_human_commit_message("delete")
                    )
                    metadata_helper.delete_item_metadata_from_hf(api, repo_id, blob_path)
                hf_deleted = True
            except Exception as e:
                print(f"[!] Warning deleting from HF ({blob_path}): {e}")

            if media_hash:
                if "media_hashes" in manifest and media_hash in manifest["media_hashes"]:
                    manifest["media_hashes"].pop(media_hash, None)
                if "content_hashes" in manifest and media_hash in manifest["content_hashes"]:
                    manifest["content_hashes"].pop(media_hash, None)
                if "fingerprints" in manifest and media_hash in manifest["fingerprints"]:
                    manifest["fingerprints"].pop(media_hash, None)

        metadata_helper.delete_nfo_files("jellyfin_library", key_to_delete)
        strm_path = Path("jellyfin_library") / f"{key_to_delete}.strm"
        if strm_path.exists():
            try:
                strm_path.unlink()
                remove_empty_folders("jellyfin_library")
            except Exception as e:
                print(f"[!] Warning deleting .strm file: {e}")

        FILE_CACHE.pop(key_to_delete, None)
        manifest["files"].pop(key_to_delete, None)

        save_manifest(manifest)

        resp_body = json.dumps({
            "success": True,
            "key": key_to_delete,
            "hf_deleted": hf_deleted,
            "message": f"Successfully deleted {key_to_delete} from Hugging Face and Jellyfin library."
        }).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp_body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(resp_body)


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

            if blob_path:
                try:
                    api.delete_file(
                        path_in_repo=blob_path,
                        repo_id=repo_id,
                        repo_type="dataset",
                        commit_message=commit_helper.get_human_commit_message("delete")
                    )
                    metadata_helper.delete_item_metadata_from_hf(api, repo_id, blob_path)
                except Exception as e:
                    print(f"[!] Warning deleting from HF ({blob_path}): {e}")

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
            self.send_error(400, "Missing key parameter")
            return

        meta = storyboard_helper.get_storyboard_meta(key)
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

        with open(img_path, "rb") as f:
            data = f.read()

        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
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

        img_path = screens_helper.get_screens_path(safe_id) if safe_id else None
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
                    acc_owner = screens_info.get("account")
                    accounts = get_all_accounts()
                    token = None
                    for a in accounts:
                        if a.get("username") == acc_owner:
                            token = a.get("token")
                            break
                    if not token and accounts:
                        token = accounts[0].get("token")

                    api = HfApi(token=token)
                    local_downloaded = api.hf_hub_download(repo_id=repo_id, filename=blob_path, repo_type="dataset")
                    temp_plain = os.path.join(screens_helper.SCREENS_DIR, f"{safe_id}.jpg")
                    crypto_engine.decrypt_file(local_downloaded, temp_plain, ENCRYPTION_PASSWORD)
                    screens_helper.process_screens_image(Path(temp_plain), safe_id)
                    img_path = temp_plain
                except Exception as e:
                    print(f"[!] Warning on-demand restoring screens {safe_id}: {e}")

        if not img_path or not os.path.exists(img_path):
            img_path = storyboard_helper.get_storyboard_image_path(safe_id) if safe_id else None

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

        with open(img_path, "rb") as f:
            data = f.read()

        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
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

        thumb_path = screens_helper.get_screens_thumb_path(safe_id) if safe_id else None
        if not thumb_path or not os.path.exists(thumb_path):
            # Check if we can slice or fetch poster thumb from storyboard
            thumb_path = storyboard_helper.get_poster_thumb_path(safe_id) if safe_id else None

        if not thumb_path or not os.path.exists(thumb_path):
            screens_path = screens_helper.get_screens_path(safe_id) if safe_id else None
            if screens_path and os.path.exists(screens_path):
                screens_helper.process_screens_image(Path(screens_path), safe_id)
                thumb_path = screens_helper.get_screens_thumb_path(safe_id)

        if not thumb_path or not os.path.exists(thumb_path):
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

        with open(thumb_path, "rb") as f:
            data = f.read()

        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
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
        except Exception:
            self.send_error(400, "Invalid JSON payload")
            return

        if not token:
            self.send_error(400, "Missing 'token' in request")
            return

        try:
            res = self.acc_mgr.add_account(token=token, is_upload=is_upload)
            if is_upload:
                self.token = self.acc_mgr.upload_token
                self.hf_username = self.acc_mgr.primary_username

            acc_user = res["username"]
            desc_role = "Designated as Primary Upload Account" if is_upload else "Added as Read/Scan/Stream Account"
            activity_tracker.tracker.add_event(
                "info",
                f"Account Connected: {acc_user}",
                f"{desc_role} (Scanning repositories in background)",
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
                "message": f"Successfully connected '{acc_user}'! Scanning vault repositories in background."
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

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path in ("/", "/index.html"):
            self.handle_dashboard()
        elif path == "/api/files":
            self.handle_api_files()
        elif path in ("/api/restore-hf", "/api/recheck-cloud"):
            self.handle_restore_hf()
        elif path == "/api/accounts":
            self.handle_api_accounts()
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
            self.handle_stream(resolved_key)
        elif path.startswith("/stream/"):
            path_after_stream = path[len("/stream/"):]
            self.handle_stream(path_after_stream)
        else:
            self.send_error(404, "Not Found")

    def handle_api_files(self):
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
                can_decrypt = crypto_engine.verify_password(verifier, self.password) if verifier else True

                size_bytes = info.get("orig_size", 0)
                hf_url = f"https://huggingface.co/datasets/{repo}/blob/main/{blob}" if (blob and blob != "unknown") else f"https://huggingface.co/datasets/{repo}"
                metadata = info.get("metadata") or metadata_helper.extract_default_metadata(key)
                if metadata.get("overview", "").startswith("Encrypted vault stream:"):
                    metadata["overview"] = ""
                account_owner = info.get("account") or (repo.split("/")[0] if "/" in repo else self.hf_username)

                media_info = info.get("media_info")
                sb_meta = storyboard_helper.get_storyboard_meta(key)
                dur_sec = 0
                duration_str = ""
                if media_info and media_info.get("duration_sec"):
                    dur_sec = media_info.get("duration_sec", 0)
                    duration_str = media_info.get("duration_str") or format_duration(dur_sec)
                elif sb_meta and sb_meta.get("duration"):
                    raw_sb_dur = float(sb_meta.get("duration", 0))
                    # Ignore 180.0 default fallback placeholder from storyboard generator
                    if abs(raw_sb_dur - 180.0) > 0.05:
                        dur_sec = raw_sb_dur
                        duration_str = format_duration(dur_sec)

                safe_id = storyboard_helper.get_safe_id(key)
                has_screens_flag = bool(info.get("screens")) or screens_helper.has_screens(safe_id)
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
                    "has_storyboard": storyboard_helper.has_storyboard(key),
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
                    files_data.append(item)
                else:
                    item["reason"] = "Cannot decrypt with current master password (encrypted with an older or different key)"
                    skipped_files.append(item)

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
                if metadata.get("overview", "").startswith("Encrypted vault stream:"):
                    metadata["overview"] = ""

                verifier = info.get("verifier")
                can_decrypt = crypto_engine.verify_password(verifier, self.password) if verifier else True

                account_owner = info.get("account") or (repo.split("/")[0] if "/" in repo else self.hf_username)

                media_info = info.get("media_info")
                sb_meta = storyboard_helper.get_storyboard_meta(key)
                dur_sec = 0
                duration_str = ""
                if media_info and media_info.get("duration_sec"):
                    dur_sec = media_info.get("duration_sec", 0)
                    duration_str = media_info.get("duration_str") or format_duration(dur_sec)
                elif sb_meta and sb_meta.get("duration"):
                    raw_sb_dur = float(sb_meta.get("duration", 0))
                    # Ignore 180.0 default fallback placeholder from storyboard generator
                    if abs(raw_sb_dur - 180.0) > 0.05:
                        dur_sec = raw_sb_dur
                        duration_str = format_duration(dur_sec)

                safe_id = storyboard_helper.get_safe_id(key)
                has_screens_flag = bool(info.get("screens")) or screens_helper.has_screens(safe_id)
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
                    "has_storyboard": storyboard_helper.has_storyboard(key),
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
                    repos_set.add(repo)
                    files_data.append(item)
                else:
                    item["reason"] = "Cannot decrypt with current password (encrypted with an older or different key)"
                    skipped_files.append(item)

                    # Suppress indecryptable stream & NFO from Jellyfin
                    strm_path = Path("jellyfin_library") / f"{key}.strm"
                    if strm_path.exists():
                        try:
                            strm_path.unlink()
                        except Exception:
                            pass
                    metadata_helper.delete_nfo_files("jellyfin_library", key)

        files_json_str = json.dumps(files_data)
        skipped_json_str = json.dumps(skipped_files)
        total_size_mb = total_bytes / (1024 * 1024)
        skipped_count = len(skipped_files)

        all_accounts = [a["username"] for a in self.acc_mgr.get_all_accounts()] if (hasattr(self, "acc_mgr") and self.acc_mgr) else [self.hf_username]
        primary_user = self.acc_mgr.primary_username if (hasattr(self, "acc_mgr") and self.acc_mgr) else self.hf_username
        total_accounts = len(all_accounts)

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

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>HFSYNC - Encrypted Media Vault</title>
    <style>
        :root {{
            --bg: #090d16;
            --panel: #111827;
            --panel-border: #1f2937;
            --primary: #38bdf8;
            --primary-hover: #0284c7;
            --danger: #ef4444;
            --danger-hover: #dc2626;
            --text: #f9fafb;
            --text-muted: #9ca3af;
            --accent: #10b981;
            --purple: #a855f7;
            --amber: #f59e0b;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background: var(--bg);
            color: var(--text);
            margin: 0;
            padding: 24px;
        }}
        .container {{ max-width: 1140px; margin: 0 auto; }}
        header, .vault-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--panel-border);
            padding: 4px 0 12px 0;
            margin-bottom: 10px;
            flex-wrap: wrap;
            gap: 12px;
        }}
        .vault-brand-cluster {{
            display: flex;
            align-items: center;
            gap: 12px;
            flex-wrap: wrap;
        }}
        .vault-brand-title {{
            margin: 0;
            font-size: 1.32rem;
            color: var(--primary);
            display: inline-flex;
            align-items: center;
            gap: 8px;
            font-weight: 800;
            letter-spacing: -0.3px;
        }}
        .vault-actions-cluster {{
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        }}

        /* Status Pills (Always-On-Top Indicators) */
        .status-pill {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 3px 10px;
            border-radius: 9999px;
            font-size: 0.74rem;
            font-weight: 600;
            letter-spacing: 0.2px;
            transition: all 0.25s ease;
            white-space: nowrap;
        }}
        .status-pill.status-online {{
            background: rgba(16, 185, 129, 0.14);
            border: 1px solid rgba(16, 185, 129, 0.35);
            color: #34d399;
        }}
        .status-pill.status-offline {{
            background: rgba(239, 68, 68, 0.2);
            border: 1px solid rgba(239, 68, 68, 0.6);
            color: #fca5a5;
            animation: pulse-alert 1.2s infinite alternate;
        }}
        .status-pill.status-idle {{
            background: rgba(16, 185, 129, 0.12);
            border: 1px solid rgba(16, 185, 129, 0.3);
            color: #6ee7b7;
        }}
        .status-pill.status-active {{
            background: rgba(56, 189, 248, 0.2);
            border: 1px solid rgba(56, 189, 248, 0.55);
            color: #7dd3fc;
            animation: pulse-sync 1.5s infinite alternate;
        }}
        .status-pill.status-attention {{
            background: rgba(245, 158, 11, 0.2);
            border: 1px solid rgba(245, 158, 11, 0.6);
            color: #fcd34d;
            animation: pulse-alert 1.2s infinite alternate;
        }}
        @keyframes pulse-sync {{
            from {{ box-shadow: 0 0 4px rgba(56, 189, 248, 0.3); }}
            to {{ box-shadow: 0 0 12px rgba(56, 189, 248, 0.75); }}
        }}
        @keyframes pulse-alert {{
            from {{ box-shadow: 0 0 4px rgba(239, 68, 68, 0.3); }}
            to {{ box-shadow: 0 0 14px rgba(239, 68, 68, 0.85); transform: scale(1.02); }}
        }}
        .pill-dot {{
            width: 7px;
            height: 7px;
            border-radius: 50%;
            display: inline-block;
        }}
        .status-online .pill-dot, .status-idle .pill-dot {{ background: #10b981; box-shadow: 0 0 6px #10b981; }}
        .status-offline .pill-dot {{ background: #ef4444; box-shadow: 0 0 6px #ef4444; }}
        .status-active .pill-dot {{ background: #38bdf8; box-shadow: 0 0 6px #38bdf8; }}
        .status-attention .pill-dot {{ background: #f59e0b; box-shadow: 0 0 6px #f59e0b; }}

        /* Minimalist Metrics Strip (Saves massive vertical space) */
        .metrics-strip {{
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
            margin-bottom: 12px;
            padding: 6px 12px;
            background: rgba(17, 24, 39, 0.65);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 8px;
            backdrop-filter: blur(8px);
        }}
        .metric-chip {{
            display: inline-flex;
            align-items: center;
            gap: 5px;
            font-size: 0.78rem;
            color: #9ca3af;
            padding: 2px 7px;
            border-radius: 6px;
            transition: background 0.15s;
        }}
        .metric-chip.clickable {{
            cursor: pointer;
        }}
        .metric-chip.clickable:hover {{
            background: rgba(255, 255, 255, 0.06);
            color: #fff;
        }}
        .metric-val {{
            font-weight: 700;
            color: #f3f4f6;
            font-size: 0.86rem;
        }}
        .metric-divider {{
            color: rgba(255, 255, 255, 0.15);
            font-size: 0.8rem;
        }}

        /* Tree-Level Folder Tags Cluster */
        .folder-tags-cluster {{
            display: inline-flex;
            align-items: center;
            gap: 5px;
            margin-left: 10px;
            flex-wrap: wrap;
        }}
        .tree-tag-chip {{
            display: inline-flex;
            align-items: center;
            gap: 4px;
            padding: 1px 7px;
            background: rgba(147, 51, 234, 0.18);
            border: 1px solid rgba(147, 51, 234, 0.4);
            border-radius: 4px;
            color: #d8b4fe;
            font-size: 0.72rem;
            font-weight: 600;
            transition: all 0.15s ease;
        }}
        .tree-tag-chip:hover {{
            background: rgba(147, 51, 234, 0.3);
            border-color: #a855f7;
        }}
        .tree-tag-remove {{
            cursor: pointer;
            color: #fca5a5;
            font-weight: bold;
            margin-left: 2px;
            padding: 0 2px;
            border-radius: 2px;
        }}
        .tree-tag-remove:hover {{
            background: rgba(239, 68, 68, 0.3);
            color: #fff;
        }}
        .btn-tree-add-tag {{
            background: rgba(255, 255, 255, 0.06);
            border: 1px dashed rgba(255, 255, 255, 0.25);
            border-radius: 4px;
            color: #9ca3af;
            font-size: 0.68rem;
            font-weight: 600;
            padding: 1px 6px;
            cursor: pointer;
            transition: all 0.15s;
        }}
        .btn-tree-add-tag:hover {{
            background: rgba(56, 189, 248, 0.15);
            border-color: #38bdf8;
            color: #38bdf8;
        }}

        /* Hide Tags Mode */
        .cards-hide-tags .video-card:not(.is-expanded) .video-card-badges,
        .cards-hide-tags .file-row:not(.is-expanded) .row-badges {{
            display: none !important;
        }}
        .video-card.is-expanded .video-card-badges {{
            display: flex !important;
            padding-top: 6px;
            border-top: 1px dashed rgba(255, 255, 255, 0.1);
            margin-top: 6px;
        }}

        /* Per-tag hide system (Hide any tag from all cards, visible on expand, filterable) */
        .video-card:not(.is-expanded) .is-hidden-tag,
        .file-row:not(.is-expanded) .is-hidden-tag {{
            display: none !important;
        }}
        .video-card.is-expanded .is-hidden-tag,
        .file-row.is-expanded .is-hidden-tag {{
            display: inline-flex !important;
            opacity: 0.72;
            border: 1px dashed rgba(245, 158, 11, 0.5) !important;
            background: rgba(245, 158, 11, 0.08) !important;
        }}
        .pill-hide-trigger {{
            font-size: 0.65rem;
            margin-left: 3px;
            cursor: pointer;
            opacity: 0;
            transition: opacity 0.15s, transform 0.15s;
            padding: 0 2px;
            border-radius: 2px;
        }}
        .meta-pill:hover .pill-hide-trigger,
        .is-hidden-tag .pill-hide-trigger {{
            opacity: 0.9;
        }}
        .pill-hide-trigger:hover {{
            opacity: 1;
            transform: scale(1.25);
        }}
        .btn-tag-hide-toggle {{
            font-size: 0.68rem;
            padding: 2px 7px;
            border-radius: 4px;
            border: 1px solid rgba(255, 255, 255, 0.14);
            background: rgba(255, 255, 255, 0.06);
            color: #9ca3af;
            cursor: pointer;
            transition: all 0.15s ease;
            white-space: nowrap;
        }}
        .btn-tag-hide-toggle:hover {{
            border-color: rgba(56, 189, 248, 0.5);
            color: #38bdf8;
            background: rgba(56, 189, 248, 0.15);
        }}
        .btn-tag-hide-toggle.is-hidden {{
            border-color: rgba(245, 158, 11, 0.5);
            color: #fbbf24;
            background: rgba(245, 158, 11, 0.15);
        }}
        .popover-item {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 6px;
            padding: 3px 6px;
            border-radius: 4px;
        }}
        .popover-item-left {{
            display: flex;
            align-items: center;
            gap: 7px;
            cursor: pointer;
            flex: 1;
            min-width: 0;
        }}
        .popover-item.is-tag-hidden {{
            background: rgba(245, 158, 11, 0.05);
        }}
        .popover-hidden-badge {{
            font-size: 0.72rem;
            padding: 2px 8px;
            background: rgba(245, 158, 11, 0.18);
            border: 1px solid rgba(245, 158, 11, 0.4);
            color: #fbbf24;
            border-radius: 4px;
            cursor: pointer;
            white-space: nowrap;
        }}
        .popover-hidden-badge:hover {{
            background: rgba(245, 158, 11, 0.3);
            color: #fff;
        }}

        /* Dedicated Screens Hover Overlay (Zero conflict with storyboard scrubbing!) */
        .thumb-screens-overlay {{
            position: absolute;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background-color: #0b0f19;
            background-size: contain;
            background-position: center;
            background-repeat: no-repeat;
            z-index: 4;
            display: none;
            pointer-events: none;
            border-radius: 6px;
        }}
        .sticky-status-cluster {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            margin-left: auto;
        }}
        
        .skipped-banner {{
            background: rgba(245, 158, 11, 0.1);
            border: 1px solid rgba(245, 158, 11, 0.35);
            border-radius: 10px;
            padding: 14px 18px;
            margin-bottom: 18px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
        }}
        .skipped-banner-left {{
            display: flex;
            align-items: center;
            gap: 12px;
        }}
        .skipped-card {{
            background: #090d16;
            border: 1px solid var(--panel-border);
            border-radius: 8px;
            padding: 12px 16px;
            margin-bottom: 10px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
        }}
        .skipped-name {{
            font-weight: 600;
            color: #f3f4f6;
            font-size: 0.9rem;
        }}
        .skipped-reason {{
            color: #f59e0b;
            font-size: 0.78rem;
            margin-top: 3px;
        }}

        /* Advanced Modern YouTube-Style Video Player */
        .yt-player-box {{
            background: #0a0a0c;
            border-radius: 14px;
            overflow: hidden;
            margin-bottom: 24px;
            box-shadow: 0 16px 40px rgba(0,0,0,0.85);
            border: 1px solid rgba(255,255,255,0.08);
            display: none;
            transition: max-width 0.3s ease, margin 0.3s ease;
        }}
        .yt-player-box.theater-mode {{
            max-width: 100% !important;
            border-radius: 0;
            margin-left: -20px;
            margin-right: -20px;
            border-left: none;
            border-right: none;
        }}
        .yt-container {{
            position: relative;
            width: 100%;
            background: #000;
            outline: none;
            aspect-ratio: 16 / 9;
            max-height: 75vh;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
            user-select: none;
        }}
        .yt-container.hide-cursor {{
            cursor: none;
        }}
        .yt-container video {{
            width: 100%;
            height: 100%;
            object-fit: contain;
            display: block;
        }}

        /* Top Overlay */
        .yt-top-overlay {{
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            padding: 14px 18px;
            background: linear-gradient(to bottom, rgba(0,0,0,0.82) 0%, rgba(0,0,0,0.4) 60%, transparent 100%);
            display: flex;
            justify-content: space-between;
            align-items: center;
            z-index: 25;
            transition: opacity 0.25s ease;
            pointer-events: auto;
        }}
        .yt-top-title-group {{
            display: flex;
            align-items: center;
            gap: 12px;
            overflow: hidden;
        }}
        .yt-security-badge {{
            background: rgba(16, 185, 129, 0.15);
            border: 1px solid rgba(16, 185, 129, 0.35);
            color: #34d399;
            font-size: 0.72rem;
            font-weight: 700;
            padding: 3px 8px;
            border-radius: 6px;
            display: inline-flex;
            align-items: center;
            gap: 5px;
            letter-spacing: 0.5px;
            text-transform: uppercase;
            flex-shrink: 0;
        }}
        .yt-live-dot {{
            width: 7px;
            height: 7px;
            border-radius: 50%;
            background: #10b981;
            box-shadow: 0 0 8px #10b981;
        }}
        .yt-video-title {{
            color: #f3f4f6;
            font-size: 0.95rem;
            font-weight: 600;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            text-shadow: 0 1px 4px rgba(0,0,0,0.8);
        }}
        .yt-top-actions {{
            display: flex;
            align-items: center;
            gap: 8px;
            flex-shrink: 0;
        }}

        /* Center Animation Feedback & Spinner */
        .yt-center-feedback {{
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%) scale(0.6);
            background: rgba(0, 0, 0, 0.68);
            color: #fff;
            width: 76px;
            height: 76px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.8rem;
            pointer-events: none;
            opacity: 0;
            transition: transform 0.25s cubic-bezier(0.18, 0.89, 0.32, 1.28), opacity 0.25s ease;
            z-index: 20;
            backdrop-filter: blur(4px);
        }}
        .yt-center-feedback.show {{
            transform: translate(-50%, -50%) scale(1.15);
            opacity: 1;
        }}
        .yt-spinner {{
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            z-index: 18;
            pointer-events: none;
        }}
        .yt-spinner-ring {{
            width: 52px;
            height: 52px;
            border: 4px solid rgba(255, 255, 255, 0.2);
            border-top-color: #ff0000;
            border-radius: 50%;
            animation: yt-spin 0.8s linear infinite;
        }}
        @keyframes yt-spin {{
            to {{ transform: rotate(360deg); }}
        }}

        /* Bottom Controls Overlay */
        .yt-controls-overlay {{
            position: absolute;
            bottom: 0;
            left: 0;
            right: 0;
            background: linear-gradient(to top, rgba(0,0,0,0.92) 0%, rgba(0,0,0,0.6) 60%, transparent 100%);
            padding: 8px 16px 10px 16px;
            z-index: 25;
            transition: opacity 0.25s ease;
            pointer-events: auto;
        }}
        .yt-container.controls-hidden .yt-top-overlay,
        .yt-container.controls-hidden .yt-controls-overlay {{
            opacity: 0;
            pointer-events: none;
        }}

        /* Scrub / Progress Bar */
        .yt-scrub-container {{
            position: relative;
            width: 100%;
            height: 16px;
            display: flex;
            align-items: center;
            cursor: pointer;
            margin-bottom: 6px;
        }}
        .yt-scrub-track {{
            position: relative;
            width: 100%;
            height: 4px;
            background: rgba(255, 255, 255, 0.22);
            border-radius: 2px;
            transition: height 0.15s ease;
        }}
        .yt-scrub-container:hover .yt-scrub-track {{
            height: 6px;
        }}
        .yt-scrub-buffered {{
            position: absolute;
            top: 0;
            left: 0;
            height: 100%;
            width: 0%;
            background: rgba(255, 255, 255, 0.45);
            border-radius: 2px;
            pointer-events: none;
        }}
        .yt-scrub-progress {{
            position: absolute;
            top: 0;
            left: 0;
            height: 100%;
            width: 0%;
            background: #ff0000;
            border-radius: 2px;
            pointer-events: none;
        }}
        .yt-scrub-handle {{
            position: absolute;
            right: -6px;
            top: 50%;
            transform: translateY(-50%) scale(0);
            width: 13px;
            height: 13px;
            background: #ff0000;
            border-radius: 50%;
            transition: transform 0.15s ease;
            box-shadow: 0 0 6px rgba(255,0,0,0.7);
        }}
        .yt-scrub-container:hover .yt-scrub-handle,
        .yt-scrub-container.is-dragging .yt-scrub-handle {{
            transform: translateY(-50%) scale(1);
        }}
        .yt-scrub-tooltip {{
            position: absolute;
            bottom: 26px;
            transform: translateX(-50%);
            background: rgba(14, 14, 18, 0.96);
            border-radius: 8px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.85);
            border: 1px solid rgba(255,255,255,0.18);
            display: none;
            pointer-events: none;
            overflow: hidden;
            text-align: center;
            z-index: 50;
            transition: opacity 0.1s ease;
        }}
        .yt-thumb-preview {{
            width: 200px;
            height: 112px;
            background-color: #000;
            background-repeat: no-repeat;
            display: block;
            image-rendering: -webkit-optimize-contrast;
            image-rendering: high-quality;
        }}
        .yt-tooltip-time {{
            padding: 4px 8px;
            font-size: 0.76rem;
            font-weight: 700;
            color: #f3f4f6;
            background: rgba(0, 0, 0, 0.7);
            border-top: 1px solid rgba(255, 255, 255, 0.08);
            letter-spacing: 0.5px;
        }}

        /* Controls Row */
        .yt-controls-row {{
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .yt-controls-left, .yt-controls-right {{
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .yt-btn {{
            background: transparent;
            border: none;
            color: #e5e7eb;
            cursor: pointer;
            padding: 6px;
            border-radius: 6px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            transition: color 0.15s, transform 0.1s, background 0.15s;
            outline: none;
        }}
        .yt-btn:hover {{
            color: #fff;
            background: rgba(255,255,255,0.12);
        }}
        .yt-btn:active {{
            transform: scale(0.92);
        }}
        .yt-btn-speed {{
            font-size: 0.82rem;
            font-weight: 600;
            padding: 4px 8px;
            min-width: 36px;
        }}
        .yt-btn-close {{
            font-size: 1.3rem;
            line-height: 1;
            padding: 2px 8px;
            color: #9ca3af;
        }}
        .yt-btn-close:hover {{
            color: #ef4444;
            background: rgba(239, 68, 68, 0.15);
        }}

        /* Volume Slider */
        .yt-volume-group {{
            display: flex;
            align-items: center;
        }}
        .yt-volume-slider-wrap {{
            width: 0;
            overflow: hidden;
            transition: width 0.2s cubic-bezier(0.4, 0, 0.2, 1);
            display: flex;
            align-items: center;
        }}
        .yt-volume-group:hover .yt-volume-slider-wrap,
        .yt-volume-slider-wrap:focus-within {{
            width: 64px;
            margin-right: 8px;
        }}
        .yt-volume-slider {{
            width: 60px;
            height: 4px;
            -webkit-appearance: none;
            appearance: none;
            background: rgba(255,255,255,0.3);
            border-radius: 2px;
            outline: none;
            cursor: pointer;
        }}
        .yt-volume-slider::-webkit-slider-thumb {{
            -webkit-appearance: none;
            width: 12px;
            height: 12px;
            border-radius: 50%;
            background: #fff;
            cursor: pointer;
        }}
        .yt-time-display {{
            font-size: 0.8rem;
            color: #d1d5db;
            font-weight: 500;
            margin-left: 6px;
            letter-spacing: 0.3px;
        }}

        /* Speed Popover Menu */
        .yt-speed-container {{
            position: relative;
        }}
        .yt-speed-menu {{
            position: absolute;
            bottom: 42px;
            right: 0;
            background: rgba(20, 20, 24, 0.96);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255,255,255,0.12);
            border-radius: 10px;
            padding: 6px 0;
            width: 140px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.8);
            z-index: 40;
        }}
        .yt-speed-title {{
            padding: 6px 14px;
            font-size: 0.72rem;
            color: #9ca3af;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            border-bottom: 1px solid rgba(255,255,255,0.08);
            margin-bottom: 4px;
        }}
        .yt-speed-option {{
            padding: 6px 14px;
            font-size: 0.82rem;
            color: #e5e7eb;
            cursor: pointer;
            transition: background 0.15s, color 0.15s;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}
        .yt-speed-option:hover {{
            background: rgba(255,255,255,0.1);
            color: #fff;
        }}
        .yt-speed-option.active {{
            color: #ff0000;
            font-weight: 700;
        }}
        .yt-speed-option.active::after {{
            content: "✓";
            font-size: 0.8rem;
        }}

        /* Stats for Nerds */
        .yt-stats-panel {{
            position: absolute;
            top: 56px;
            left: 18px;
            background: rgba(10, 10, 14, 0.88);
            backdrop-filter: blur(8px);
            border: 1px solid rgba(255,255,255,0.15);
            border-radius: 8px;
            padding: 10px 14px;
            color: #e5e7eb;
            font-size: 0.75rem;
            font-family: monospace;
            z-index: 35;
            max-width: 360px;
            box-shadow: 0 8px 24px rgba(0,0,0,0.7);
        }}
        .yt-stats-header {{
            display: flex;
            justify-content: space-between;
            font-weight: 700;
            color: #38bdf8;
            border-bottom: 1px solid rgba(255,255,255,0.1);
            padding-bottom: 4px;
            margin-bottom: 8px;
        }}
        .yt-stats-grid {{
            display: grid;
            grid-template-columns: 130px 1fr;
            row-gap: 4px;
            column-gap: 8px;
        }}

        /* Auto-Activating Floating Popup Mini-Player */
        .yt-container.is-floating-mini {{
            position: fixed !important;
            bottom: 24px;
            right: 24px;
            width: 480px;
            max-width: calc(100vw - 24px) !important;
            aspect-ratio: 16 / 9 !important;
            height: auto !important;
            max-height: 270px;
            z-index: 99999 !important;
            border-radius: 12px !important;
            box-shadow: 0 16px 45px rgba(0, 0, 0, 0.9), 0 0 0 1px rgba(255, 255, 255, 0.16) !important;
            overflow: hidden !important;
            animation: yt-float-in 0.3s cubic-bezier(0.16, 1, 0.3, 1) forwards !important;
            backdrop-filter: blur(8px) !important;
            touch-action: none;
        }}
        .yt-container.is-floating-mini.mini-size-s {{
            width: 360px !important;
            max-height: 202px !important;
        }}
        .yt-container.is-floating-mini.mini-size-m {{
            width: 500px !important;
            max-height: 281px !important;
        }}
        .yt-container.is-floating-mini.mini-size-l {{
            width: 680px !important;
            max-height: 382px !important;
        }}
        .yt-container.is-floating-mini .yt-top-overlay {{
            cursor: move;
            user-select: none;
        }}
        .yt-mini-size-controls {{
            display: none;
        }}
        .yt-container.is-floating-mini .yt-mini-size-controls {{
            display: inline-flex !important;
            align-items: center;
            gap: 2px;
            background: rgba(0, 0, 0, 0.45);
            border-radius: 4px;
            padding: 1px 3px;
            border: 1px solid rgba(255, 255, 255, 0.15);
            margin-right: 4px;
        }}
        .yt-mini-size-btn {{
            background: transparent;
            border: none;
            color: #94a3b8;
            font-size: 0.68rem;
            font-weight: 700;
            padding: 1px 6px;
            border-radius: 3px;
            cursor: pointer;
            transition: all 0.15s ease;
        }}
        .yt-mini-size-btn:hover {{
            color: #fff;
            background: rgba(255, 255, 255, 0.2);
        }}
        .yt-mini-size-btn.active {{
            color: #040711;
            background: var(--primary);
            box-shadow: 0 0 6px rgba(56, 189, 248, 0.6);
        }}
        @keyframes yt-float-in {{
            from {{ transform: translateY(30px) scale(0.92); opacity: 0; }}
            to {{ transform: translateY(0) scale(1); opacity: 1; }}
        }}
        .yt-container.is-floating-mini .yt-top-overlay {{
            padding: 8px 12px !important;
        }}
        .yt-container.is-floating-mini .yt-security-badge {{
            display: none !important;
        }}
        .yt-container.is-floating-mini .yt-video-title {{
            font-size: 0.8rem !important;
            max-width: 190px !important;
        }}
        .yt-container.is-floating-mini .yt-controls-overlay {{
            padding: 4px 10px 8px 10px !important;
        }}
        .yt-container.is-floating-mini .yt-speed-container,
        .yt-container.is-floating-mini .yt-sub-container,
        .yt-container.is-floating-mini button[title*="Stats"],
        .yt-container.is-floating-mini button[title*="Theater"] {{
            display: none !important;
        }}
        .yt-container.is-floating-mini .yt-volume-group {{
            display: inline-flex !important;
            align-items: center;
            cursor: pointer;
        }}
        .yt-container.is-floating-mini .yt-volume-slider-wrap {{
            width: 0;
            overflow: hidden;
            transition: width 0.2s cubic-bezier(0.4, 0, 0.2, 1);
        }}
        .yt-container.is-floating-mini .yt-volume-group:hover .yt-volume-slider-wrap,
        .yt-container.is-floating-mini .yt-volume-slider-wrap:focus-within {{
            width: 48px !important;
        }}
        .yt-container.is-floating-mini .yt-volume-slider {{
            width: 48px !important;
        }}
        .yt-container.is-floating-mini .yt-btn {{
            padding: 4px !important;
        }}
        .yt-container.is-floating-mini .yt-time-display {{
            font-size: 0.72rem !important;
        }}
        .yt-mini-expand-btn {{
            display: none;
        }}
        .yt-container.is-floating-mini .yt-mini-expand-btn {{
            display: inline-flex !important;
            background: rgba(255, 255, 255, 0.15) !important;
            border-radius: 4px !important;
            color: #fff !important;
        }}
        .yt-container.is-floating-mini .yt-mini-expand-btn:hover {{
            background: var(--primary) !important;
            color: #040711 !important;
            box-shadow: 0 0 8px rgba(56, 189, 248, 0.6) !important;
        }}

        .sticky-filter-bar {{
            position: sticky;
            top: 0;
            z-index: 100;
            background: rgba(11, 15, 25, 0.94);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            padding: 12px 0 8px 0;
            margin-bottom: 14px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
            box-shadow: 0 6px 20px rgba(0, 0, 0, 0.5);
            transition: all 0.2s ease;
        }}
        .sticky-filter-bar .toolbar {{
            margin-bottom: 0;
        }}
        .sticky-filter-bar .active-chips-bar {{
            margin-top: 8px;
            margin-bottom: 0;
        }}
        .filter-summary-count {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 6px 14px;
            background: rgba(99, 102, 241, 0.18);
            border: 1px solid rgba(99, 102, 241, 0.45);
            border-radius: 9999px;
            color: #a5b4fc;
            font-size: 0.82rem;
            font-weight: 600;
            white-space: nowrap;
        }}
        .filter-count-badge {{
            display: inline-flex;
            align-items: center;
            gap: 4px;
            padding: 3px 10px;
            background: rgba(99, 102, 241, 0.25);
            border: 1px solid rgba(99, 102, 241, 0.5);
            border-radius: 9999px;
            color: #c7d2fe;
            font-size: 0.76rem;
            font-weight: 700;
            margin-right: 6px;
        }}
        .folder-count.is-matching-filtered {{
            background: rgba(99, 102, 241, 0.25) !important;
            color: #c7d2fe !important;
            border: 1px solid rgba(99, 102, 241, 0.5) !important;
            font-weight: 700 !important;
        }}

        .toolbar {{
            display: flex;
            gap: 10px;
            align-items: center;
            margin-bottom: 16px;
            flex-wrap: wrap;
        }}
        .search-input {{
            flex: 1;
            min-width: 240px;
            padding: 10px 14px;
            background: var(--panel);
            border: 1px solid var(--panel-border);
            border-radius: 8px;
            color: #fff;
            font-size: 0.92rem;
            outline: none;
        }}
        .search-input:focus {{ border-color: var(--primary); }}
        
        .filter-select {{
            padding: 10px 14px;
            background: var(--panel);
            border: 1px solid var(--panel-border);
            border-radius: 8px;
            color: #f3f4f6;
            font-size: 0.9rem;
            outline: none;
            cursor: pointer;
        }}
        .filter-select:focus {{ border-color: var(--primary); }}

        .tool-btn {{
            padding: 10px 14px;
            background: var(--panel);
            border: 1px solid var(--panel-border);
            color: var(--text-muted);
            border-radius: 8px;
            cursor: pointer;
            font-size: 0.88rem;
            font-weight: 500;
            transition: all 0.2s;
            white-space: nowrap;
        }}
        .tool-btn:hover {{ color: #fff; border-color: #4b5563; }}

        /* Tree view structure */
        .tree-container {{
            background: var(--panel);
            border: 1px solid var(--panel-border);
            border-radius: 10px;
            padding: 16px;
            min-height: 200px;
        }}
        .tree-node {{ margin-left: 20px; }}
        .folder-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 8px 10px;
            border-radius: 6px;
            font-weight: 600;
            color: #e5e7eb;
            transition: background 0.15s;
        }}
        .folder-header:hover {{ background: rgba(255,255,255,0.04); }}
        .folder-left {{
            display: flex;
            align-items: center;
            gap: 8px;
            cursor: pointer;
            user-select: none;
            flex: 1;
        }}
        .folder-actions {{
            display: flex;
            gap: 8px;
            align-items: center;
        }}
        .btn-sm {{
            padding: 4px 10px;
            font-size: 0.78rem;
        }}
        .folder-icon {{ color: #fbbf24; font-size: 1.1rem; }}
        .folder-arrow {{ font-size: 0.8rem; color: var(--text-muted); width: 14px; display: inline-block; transition: transform 0.2s; }}
        .folder-arrow.open {{ transform: rotate(90deg); }}
        .folder-count {{ font-size: 0.8rem; color: var(--text-muted); font-weight: normal; margin-left: 6px; }}
        .folder-children {{ margin-left: 12px; border-left: 1px dashed #374151; padding-left: 8px; }}
        .folder-children.collapsed {{ display: none; }}
        .is-filter-hidden {{ display: none !important; }}
        .video-card, .file-row {{ transition: opacity 0.15s ease; }}

        .folder-group {{
            margin-bottom: 8px;
            transition: all 0.25s ease;
        }}
        .folder-group.is-playing-group {{
            border: 1px solid rgba(56, 189, 248, 0.4);
            background: rgba(56, 189, 248, 0.03);
            border-radius: 8px;
            box-shadow: 0 4px 16px rgba(0, 0, 0, 0.4);
            margin-bottom: 14px;
        }}
        .folder-group.is-playing-group > .folder-header {{
            background: rgba(56, 189, 248, 0.09);
            border-bottom: 1px solid rgba(56, 189, 248, 0.25);
            border-radius: 7px 7px 0 0;
        }}
        .folder-playing-badge {{
            display: inline-flex;
            align-items: center;
            gap: 4px;
            background: #0284c7;
            color: #fff;
            font-size: 0.7rem;
            font-weight: 700;
            padding: 2px 7px;
            border-radius: 4px;
            margin-left: 8px;
            animation: pulseNowPlaying 2s infinite ease-in-out;
        }}
        .video-card.is-now-playing {{
            border-color: #38bdf8 !important;
            box-shadow: 0 0 16px rgba(56, 189, 248, 0.4) !important;
            background: rgba(56, 189, 248, 0.08) !important;
            position: relative;
        }}
        .file-row.is-now-playing {{
            border-color: #38bdf8 !important;
            background: rgba(56, 189, 248, 0.12) !important;
            box-shadow: 0 0 12px rgba(56, 189, 248, 0.3) !important;
        }}
        .now-playing-badge {{
            position: absolute;
            top: 6px;
            left: 6px;
            background: rgba(2, 132, 199, 0.95);
            color: #fff;
            font-size: 0.7rem;
            font-weight: 700;
            padding: 2px 7px;
            border-radius: 4px;
            z-index: 6;
            display: flex;
            align-items: center;
            gap: 4px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.5);
            animation: pulseNowPlaying 2s infinite ease-in-out;
        }}
        @keyframes pulseNowPlaying {{
            0%, 100% {{ opacity: 1; transform: scale(1); }}
            50% {{ opacity: 0.85; transform: scale(1.02); }}
        }}

        /* Advanced Multi-Select Filter Toolbar */
        .multi-filter-group {{
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        }}
        .popover-wrapper {{
            position: relative;
            display: inline-block;
        }}
        .filter-pill-btn {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 8px 12px;
            background: var(--panel);
            border: 1px solid var(--panel-border);
            border-radius: 8px;
            color: #e5e7eb;
            font-size: 0.84rem;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s ease;
            user-select: none;
            white-space: nowrap;
        }}
        .filter-pill-btn:hover {{
            border-color: #4b5563;
            background: rgba(255, 255, 255, 0.05);
            color: #fff;
        }}
        .filter-pill-btn.is-active {{
            background: rgba(56, 189, 248, 0.15);
            border-color: #38bdf8;
            color: #38bdf8;
        }}
        #genrePillBtn {{
            background: rgba(245, 158, 11, 0.12) !important;
            border-color: rgba(245, 158, 11, 0.45) !important;
            color: #fbbf24 !important;
            font-weight: 600;
        }}
        #genrePillBtn:hover, #genrePillBtn.is-active {{
            background: rgba(245, 158, 11, 0.25) !important;
            border-color: #f59e0b !important;
            color: #fde68a !important;
        }}
        #genrePillBtn .pill-badge {{
            background: #f59e0b !important;
            color: #1c1917 !important;
        }}
        #tagPillBtn {{
            background: rgba(56, 189, 248, 0.12) !important;
            border-color: rgba(56, 189, 248, 0.4) !important;
            color: #38bdf8 !important;
            font-weight: 600;
        }}
        #tagPillBtn:hover, #tagPillBtn.is-active {{
            background: rgba(56, 189, 248, 0.22) !important;
            border-color: #0284c7 !important;
            color: #7dd3fc !important;
        }}
        #tagPillBtn .pill-badge {{
            background: #0284c7 !important;
            color: #ffffff !important;
        }}
        .pill-badge {{
            background: #38bdf8;
            color: #0b0f19;
            font-size: 0.72rem;
            font-weight: 700;
            padding: 1px 6px;
            border-radius: 10px;
            margin-left: 2px;
        }}
        .filter-popover {{
            position: absolute;
            top: calc(100% + 6px);
            left: 0;
            min-width: 260px;
            max-width: 340px;
            background: #111827;
            border: 1px solid #374151;
            border-radius: 10px;
            box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.7), 0 8px 10px -6px rgba(0, 0, 0, 0.7);
            z-index: 1000;
            padding: 12px;
            backdrop-filter: blur(16px);
        }}
        .popover-header {{
            display: flex;
            flex-direction: column;
            gap: 8px;
            margin-bottom: 8px;
            padding-bottom: 8px;
            border-bottom: 1px solid #1f2937;
        }}
        .popover-search {{
            width: 100%;
            padding: 6px 10px;
            background: #1f2937;
            border: 1px solid #374151;
            border-radius: 6px;
            color: #fff;
            font-size: 0.8rem;
            outline: none;
            box-sizing: border-box;
        }}
        .popover-search:focus {{
            border-color: #38bdf8;
        }}
        .popover-actions {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 6px;
        }}
        .popover-actions button {{
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid rgba(255, 255, 255, 0.12);
            color: #d1d5db;
            font-size: 0.75rem;
            font-weight: 600;
            cursor: pointer;
            padding: 3px 9px;
            border-radius: 5px;
            transition: all 0.15s ease;
        }}
        .popover-actions button:hover {{
            color: #fff;
            background: rgba(56, 189, 248, 0.25);
            border-color: #38bdf8;
        }}
        .popover-list {{
            max-height: 220px;
            overflow-y: auto;
            display: flex;
            flex-direction: column;
            gap: 2px;
        }}
        .popover-item {{
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 6px 8px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 0.82rem;
            color: #d1d5db;
            transition: background 0.15s;
            user-select: none;
        }}
        .popover-item:hover {{
            background: rgba(255, 255, 255, 0.06);
            color: #fff;
        }}
        .popover-item input[type="checkbox"] {{
            accent-color: #38bdf8;
            cursor: pointer;
            width: 15px;
            height: 15px;
        }}
        .popover-item-count {{
            margin-left: auto;
            font-size: 0.72rem;
            color: #6b7280;
        }}
        .active-chips-bar {{
            display: flex;
            align-items: center;
            gap: 6px;
            flex-wrap: wrap;
            margin: 8px 0 12px 0;
            padding: 6px 12px;
            background: rgba(255, 255, 255, 0.02);
            border-radius: 8px;
            border: 1px dashed rgba(255, 255, 255, 0.1);
        }}
        .filter-chip {{
            display: inline-flex;
            align-items: center;
            gap: 5px;
            padding: 3px 8px;
            background: rgba(56, 189, 248, 0.15);
            border: 1px solid rgba(56, 189, 248, 0.4);
            border-radius: 12px;
            color: #38bdf8;
            font-size: 0.75rem;
            font-weight: 500;
        }}
        .filter-chip-remove {{
            cursor: pointer;
            font-size: 0.85rem;
            line-height: 1;
            opacity: 0.7;
        }}
        .filter-chip-remove:hover {{
            opacity: 1;
            color: #ef4444;
        }}
        .clear-all-chip {{
            cursor: pointer;
            color: #9ca3af;
            font-size: 0.75rem;
            text-decoration: underline;
            margin-left: 6px;
        }}
        .clear-all-chip:hover {{
            color: #f87171;
        }}

        .file-row {{
            display: flex;
            align-items: center;
            padding: 10px 14px;
            margin: 6px 0;
            border-radius: 8px;
            background: rgba(255,255,255,0.02);
            border: 1px solid rgba(255,255,255,0.05);
            gap: 14px;
            transition: background 0.15s;
        }}
        .file-row:hover {{ background: rgba(255,255,255,0.05); }}
        
        /* YouTube / Video site style animated preview thumbnail card */
        .video-thumb-card {{
            position: relative;
            width: 120px;
            height: 68px;
            border-radius: 6px;
            overflow: hidden;
            background: #040711;
            border: 1px solid rgba(255, 255, 255, 0.12);
            flex-shrink: 0;
            cursor: pointer;
            user-select: none;
            transition: transform 0.15s ease, border-color 0.15s ease, box-shadow 0.15s ease;
        }}
        .video-thumb-card:hover {{
            border-color: var(--primary);
            box-shadow: 0 4px 16px rgba(56, 189, 248, 0.35);
            transform: translateY(-1px);
        }}
        .thumb-canvas {{
            position: absolute;
            inset: 0;
            background-repeat: no-repeat;
            background-color: #0b0f19;
            background-size: cover;
            background-position: center;
            display: flex;
            align-items: center;
            justify-content: center;
            image-rendering: -webkit-optimize-contrast;
            image-rendering: high-quality;
        }}
        .thumb-fallback {{
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            gap: 2px;
            color: #64748b;
            font-size: 0.65rem;
            font-family: monospace;
            font-weight: 600;
            text-align: center;
            width: 100%;
            height: 100%;
            background: linear-gradient(135deg, #090d16 0%, #171f30 100%);
        }}
        .thumb-fallback-icon {{
            font-size: 1.2rem;
            opacity: 0.8;
        }}
        .thumb-scrub-bar {{
            position: absolute;
            bottom: 0;
            left: 0;
            width: 100%;
            height: 6px;
            background: rgba(255, 255, 255, 0.25);
            opacity: 0;
            transition: opacity 0.15s, height 0.15s;
            pointer-events: auto;
            cursor: pointer;
            z-index: 5;
        }}
        .card-thumb-wrap:hover .thumb-scrub-bar,
        .list-thumb-wrap:hover .thumb-scrub-bar,
        .video-thumb-card:hover .thumb-scrub-bar {{
            opacity: 1;
        }}
        .thumb-scrub-bar:hover {{
            height: 10px;
        }}
        .thumb-scrub-fill {{
            height: 100%;
            width: 0%;
            background: var(--primary);
            box-shadow: 0 0 6px var(--primary);
            transition: width 0.05s linear;
        }}
        .thumb-overlay-play {{
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%) scale(0.85);
            background: rgba(0, 0, 0, 0.65);
            color: #fff;
            border-radius: 50%;
            width: 28px;
            height: 28px;
            display: flex;
            align-items: center;
            justify-content: center;
            opacity: 0;
            transition: opacity 0.18s ease, transform 0.18s ease;
            pointer-events: none;
            border: 1px solid rgba(255, 255, 255, 0.35);
            z-index: 2;
        }}
        .video-thumb-card:hover .thumb-overlay-play {{
            opacity: 0.85;
            transform: translate(-50%, -50%) scale(1);
        }}
        .thumb-badge {{
            position: absolute;
            bottom: 5px;
            left: 5px;
            background: rgba(0, 0, 0, 0.78);
            color: #94a3b8;
            font-size: 0.6rem;
            font-weight: 700;
            padding: 1px 4px;
            border-radius: 3px;
            font-family: monospace;
            pointer-events: none;
            z-index: 2;
            letter-spacing: 0.5px;
            border: 1px solid rgba(255, 255, 255, 0.12);
            transition: opacity 0.15s;
        }}
        .video-thumb-card:hover .thumb-badge {{
            opacity: 0;
        }}
        .thumb-screens-btn {{
            position: absolute;
            top: 5px;
            right: 5px;
            background: rgba(15, 23, 42, 0.85);
            backdrop-filter: blur(4px);
            color: #38bdf8;
            border: 1px solid rgba(56, 189, 248, 0.4);
            font-size: 0.65rem;
            font-weight: 700;
            padding: 2px 6px;
            border-radius: 4px;
            cursor: pointer;
            z-index: 10;
            display: inline-flex;
            align-items: center;
            gap: 3px;
            transition: all 0.15s ease;
            box-shadow: 0 2px 6px rgba(0,0,0,0.5);
        }}
        .thumb-screens-btn:hover {{
            background: #0284c7;
            color: #ffffff;
            border-color: #38bdf8;
            transform: scale(1.05);
        }}
        .card-duration-badge {{
            position: absolute;
            bottom: 5px;
            right: 5px;
            background: rgba(0, 0, 0, 0.88);
            color: #f9fafb;
            font-size: 0.72rem;
            font-weight: 700;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            padding: 2px 5px;
            border-radius: 4px;
            pointer-events: none;
            letter-spacing: 0.5px;
            border: 1px solid rgba(255, 255, 255, 0.18);
            box-shadow: 0 2px 4px rgba(0, 0, 0, 0.6);
            z-index: 3;
            line-height: 1;
        }}
        .list-duration-badge {{
            bottom: 3px;
            right: 3px;
            font-size: 0.62rem;
            padding: 1px 4px;
            border-radius: 3px;
        }}
        @media (max-width: 640px) {{
            .video-thumb-card {{
                width: 90px;
                height: 51px;
            }}
            .thumb-canvas {{
                background-size: cover;
                background-position: center;
            }}
        }}

        /* View Mode Switcher */
        .view-toggle-group {{
            display: inline-flex;
            align-items: center;
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--panel-border);
            border-radius: 6px;
            padding: 2px;
            gap: 2px;
            margin-left: auto;
        }}
        .view-toggle-btn {{
            padding: 5px 12px;
            font-size: 0.8rem;
            font-weight: 600;
            border-radius: 4px;
            border: none;
            background: transparent;
            color: var(--text-muted);
            cursor: pointer;
            transition: all 0.15s ease;
            display: inline-flex;
            align-items: center;
            gap: 5px;
        }}
        .view-toggle-btn:hover {{
            color: #fff;
            background: rgba(255, 255, 255, 0.08);
        }}
        .view-toggle-btn.active {{
            background: var(--primary);
            color: #040711;
            font-weight: 700;
        }}

        /* Responsive Cards Grid View */
        .folder-cards-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
            gap: 16px;
            padding: 8px 0 16px 0;
        }}
        .folder-list-rows {{
            display: flex;
            flex-direction: column;
            gap: 6px;
            padding: 4px 0 10px 0;
        }}
        .video-card {{
            background: #111827;
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 10px;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            transition: transform 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease;
            position: relative;
        }}
        .video-card:hover {{
            transform: translateY(-3px);
            border-color: var(--primary);
            box-shadow: 0 10px 24px rgba(0, 0, 0, 0.5), 0 0 14px rgba(56, 189, 248, 0.22);
        }}
        .video-card-thumb {{
            position: relative;
            width: 100% !important;
            height: auto !important;
            aspect-ratio: 16 / 9;
            background: #000;
            overflow: hidden;
            cursor: pointer;
            user-select: none;
            border-radius: 9px 9px 0 0 !important;
            border: none !important;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08) !important;
        }}
        .card-size-badge {{
            position: absolute;
            top: 6px;
            left: 6px;
            background: rgba(0, 0, 0, 0.85);
            backdrop-filter: blur(4px);
            color: #93c5fd;
            font-size: 0.65rem;
            font-weight: 600;
            font-family: monospace;
            padding: 2px 7px;
            border-radius: 4px;
            pointer-events: none;
            border: 1px solid rgba(255, 255, 255, 0.18);
            z-index: 3;
            display: inline-flex;
            align-items: center;
            gap: 4px;
            box-shadow: 0 2px 6px rgba(0, 0, 0, 0.6);
            transition: opacity 0.15s ease;
        }}
        .card-size-badge .badge-asize {{
            color: #a7f3d0;
        }}
        .card-resume-bar {{
            position: absolute;
            bottom: 0;
            left: 0;
            height: 3.5px;
            background: #ef4444;
            box-shadow: 0 0 6px rgba(239, 68, 68, 0.85);
            z-index: 4;
            pointer-events: none;
            transition: width 0.2s ease;
        }}
        .card-resume-badge {{
            position: absolute;
            bottom: 6px;
            left: 6px;
            background: rgba(220, 38, 38, 0.9);
            backdrop-filter: blur(4px);
            color: #ffffff;
            font-size: 0.62rem;
            font-weight: 700;
            font-family: monospace;
            padding: 1px 6px;
            border-radius: 4px;
            pointer-events: none;
            border: 1px solid rgba(255, 255, 255, 0.25);
            z-index: 3;
            box-shadow: 0 2px 6px rgba(0, 0, 0, 0.7);
            transition: opacity 0.15s ease;
        }}
        .video-thumb-card:hover .card-resume-badge {{
            opacity: 0;
        }}
        .meta-pill, .genre-pill, .tag-pill, .account-pill {{
            cursor: pointer !important;
            transition: transform 0.12s ease, filter 0.12s ease;
        }}
        .meta-pill:hover, .genre-pill:hover, .tag-pill:hover, .account-pill:hover {{
            filter: brightness(1.25) !important;
            transform: scale(1.05) !important;
        }}
        .video-card-body {{
            padding: 10px 12px 8px 12px;
            flex: 1;
            display: flex;
            flex-direction: column;
            gap: 6px;
            min-width: 0;
        }}
        .video-card-title {{
            font-size: 0.92rem;
            font-weight: 600;
            color: #f9fafb;
            line-height: 1.35;
            cursor: pointer;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
            transition: color 0.15s;
        }}
        .video-card-title:hover {{
            color: var(--primary);
        }}
        .video-card-sub {{
            font-size: 0.72rem;
            color: #6b7280;
            font-family: monospace;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}
        .card-overview-snippet {{
            font-size: 0.75rem;
            color: #94a3b8;
            font-style: italic;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}
        .video-card-badges {{
            display: flex;
            flex-wrap: wrap;
            gap: 4px;
            align-items: center;
            margin-top: 2px;
        }}
        .video-card-badges .meta-pill,
        .video-card-badges .account-pill,
        .video-card-badges .alias-pill {{
            font-size: 0.66rem;
            padding: 1px 5px;
        }}
        /* Title Row with subtle Action Arrow */
        .video-card-title-row {{
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 6px;
        }}
        .video-card-title-row .video-card-title {{
            flex: 1;
            min-width: 0;
        }}
        .card-action-arrow-btn {{
            flex-shrink: 0;
            width: 22px;
            height: 22px;
            border-radius: 5px;
            border: 1px solid rgba(255, 255, 255, 0.1);
            background: rgba(255, 255, 255, 0.04);
            color: #9ca3af;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            padding: 0;
            margin-top: 1px;
            transition: all 0.18s ease;
        }}
        .card-action-arrow-btn:hover {{
            background: rgba(255, 255, 255, 0.12);
            color: var(--primary);
            border-color: rgba(56, 189, 248, 0.4);
            transform: translateY(-1px);
        }}
        .card-action-arrow-btn .card-arrow-icon {{
            font-size: 0.72rem;
            transition: transform 0.25s cubic-bezier(0.4, 0, 0.2, 1);
            display: inline-block;
            line-height: 1;
        }}
        .card-action-arrow-btn.is-active {{
            background: rgba(56, 189, 248, 0.15);
            border-color: var(--primary);
            color: var(--primary);
        }}
        .card-action-arrow-btn.is-active .card-arrow-icon {{
            transform: rotate(180deg);
        }}

        /* Sliding Open/Collapse Bottom Action Bar */
        .video-card-actions {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 6px;
            padding: 0 10px;
            max-height: 0;
            opacity: 0;
            overflow: hidden;
            border-top: 1px solid transparent;
            background: rgba(0, 0, 0, 0.22);
            margin-top: auto;
            transition: max-height 0.25s cubic-bezier(0.4, 0, 0.2, 1),
                        opacity 0.2s ease,
                        padding 0.25s cubic-bezier(0.4, 0, 0.2, 1),
                        border-color 0.25s ease;
        }}
        .video-card-actions.is-open {{
            max-height: 48px;
            opacity: 1;
            padding: 7px 10px;
            border-top-color: rgba(255, 255, 255, 0.06);
        }}
        .card-action-group-left, .card-action-group-right {{
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .card-icon-btn {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 28px;
            height: 28px;
            border-radius: 6px;
            border: 1px solid rgba(255, 255, 255, 0.1);
            background: rgba(255, 255, 255, 0.04);
            color: #e5e7eb;
            font-size: 0.85rem;
            cursor: pointer;
            text-decoration: none;
            transition: all 0.15s ease;
            box-sizing: border-box;
        }}
        .card-icon-btn:hover {{
            background: rgba(255, 255, 255, 0.12);
            border-color: rgba(255, 255, 255, 0.25);
            transform: translateY(-1px);
        }}
        .btn-card-link:hover {{
            background: rgba(16, 185, 129, 0.2);
            border-color: var(--accent);
            color: var(--accent);
        }}
        .btn-card-hf:hover {{
            background: rgba(255, 172, 51, 0.2);
            border-color: #ffac33;
            color: #ffac33;
        }}
        .btn-card-edit:hover {{
            background: rgba(168, 85, 247, 0.2);
            border-color: var(--purple);
            color: #c084fc;
        }}
        .btn-card-delete:hover {{
            background: rgba(239, 68, 68, 0.2);
            border-color: var(--danger);
            color: #fca5a5;
        }}
        .file-icon {{ color: var(--primary); font-size: 1.1rem; flex-shrink: 0; }}
        .file-name {{ font-weight: 600; font-size: 0.95rem; color: #f9fafb; }}
        .file-meta {{
            font-size: 0.75rem;
            color: var(--text-muted);
            font-family: monospace;
            background: #090d16;
            padding: 2px 7px;
            border-radius: 4px;
        }}
        .file-overview-snippet {{
            font-size: 0.8rem;
            color: #94a3b8;
            font-style: italic;
            margin-top: 2px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 580px;
        }}

        .meta-badges {{
            display: flex;
            flex-wrap: wrap;
            gap: 6px;
            align-items: center;
            margin-top: 5px;
        }}
        .meta-pill {{
            font-size: 0.72rem;
            font-weight: 600;
            padding: 2px 7px;
            border-radius: 4px;
            display: inline-flex;
            align-items: center;
            gap: 3px;
        }}
        /* Visual Distinction: Category (Warm Amber Folder Tab) vs Tag (Cool Cyan Oval Pill) */
        .meta-pill.genre-pill, .genre-pill, .category-badge {{
            background: linear-gradient(135deg, rgba(245, 158, 11, 0.22), rgba(217, 119, 6, 0.32)) !important;
            color: #fde68a !important;
            border: 1px solid rgba(245, 158, 11, 0.65) !important;
            border-radius: 3px 9px 9px 3px !important;
            font-weight: 700 !important;
            font-size: 0.70rem !important;
            letter-spacing: 0.3px;
            padding: 2px 8px !important;
            box-shadow: 0 1px 3px rgba(245, 158, 11, 0.2);
            display: inline-flex;
            align-items: center;
            gap: 4px;
        }}
        .cat-pill-prefix {{
            font-size: 0.62rem;
            font-weight: 800;
            color: #fbbf24;
            opacity: 0.95;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        .cat-pill-name {{
            font-weight: 700;
            color: #fffbeb;
        }}
        .meta-pill.tag-pill, .tag-pill {{
            background: rgba(14, 165, 233, 0.12) !important;
            color: #7dd3fc !important;
            border: 1px solid rgba(56, 189, 248, 0.35) !important;
            border-radius: 9999px !important;
            font-weight: 500 !important;
            font-size: 0.70rem !important;
            padding: 2px 8px !important;
            display: inline-flex;
            align-items: center;
            gap: 3px;
        }}
        .btn-use-alias-title {{
            background: rgba(147, 51, 234, 0.14);
            border: 1px solid rgba(147, 51, 234, 0.4);
            color: #c084fc;
            border-radius: 4px;
            font-size: 0.75rem;
            padding: 3px 8px;
            cursor: pointer;
            transition: all 0.15s ease;
            text-align: left;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            max-width: 100%;
        }}
        .btn-use-alias-title:hover {{
            background: rgba(147, 51, 234, 0.28);
            border-color: #a855f7;
            color: #e9d5ff;
            transform: translateY(-1px);
        }}
        .btn-use-alias-title.is-active-title {{
            background: rgba(16, 185, 129, 0.18);
            border-color: rgba(16, 185, 129, 0.5);
            color: #34d399;
        }}
        .alias-use-title-btn {{
            background: transparent;
            border: 1px solid rgba(167, 139, 250, 0.35);
            color: #c084fc;
            font-size: 0.68rem;
            cursor: pointer;
            margin-left: 5px;
            padding: 1px 5px;
            border-radius: 3px;
            transition: all 0.15s ease;
        }}
        .alias-use-title-btn:hover {{
            background: rgba(167, 139, 250, 0.25);
            color: #fff;
            border-color: #a78bfa;
        }}
        .btn-tag-edit-mini {{
            background: transparent;
            border: none;
            color: #9ca3af;
            cursor: pointer;
            font-size: 0.85rem;
            padding: 2px 5px;
            border-radius: 3px;
            transition: all 0.15s ease;
            line-height: 1;
        }}
        .btn-tag-edit-mini:hover {{
            color: #38bdf8;
            background: rgba(56, 189, 248, 0.15);
        }}
        .tree-tag-edit {{
            cursor: pointer;
            font-size: 0.72rem;
            color: #94a3b8;
            margin-left: 3px;
            margin-right: 2px;
            transition: color 0.15s ease;
        }}
        .tree-tag-edit:hover {{
            color: #38bdf8;
        }}
        .btn-merge-tags-popover {{
            background: rgba(168, 85, 247, 0.15) !important;
            border: 1px solid rgba(168, 85, 247, 0.4) !important;
            color: #c084fc !important;
            border-radius: 4px;
            padding: 2px 8px;
            font-size: 0.72rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.15s ease;
        }}
        .btn-merge-tags-popover:hover {{
            background: rgba(168, 85, 247, 0.3) !important;
            color: #f3e8ff !important;
        }}
        .year-pill {{
            background: rgba(168, 85, 247, 0.12);
            color: #c084fc;
            border: 1px solid rgba(168, 85, 247, 0.28);
        }}
        .rating-pill {{
            background: rgba(245, 158, 11, 0.12);
            color: #fbbf24;
            border: 1px solid rgba(245, 158, 11, 0.28);
        }}
        .res-4k-pill {{
            background: rgba(168, 85, 247, 0.22);
            color: #f0abfc;
            border: 1px solid rgba(216, 70, 239, 0.55);
            font-weight: 700;
            box-shadow: 0 0 6px rgba(216, 70, 239, 0.25);
        }}
        .res-1080p-pill {{
            background: rgba(59, 130, 246, 0.22);
            color: #93c5fd;
            border: 1px solid rgba(59, 130, 246, 0.55);
            font-weight: 600;
        }}
        .res-720p-pill {{
            background: rgba(14, 165, 233, 0.2);
            color: #7dd3fc;
            border: 1px solid rgba(14, 165, 233, 0.5);
            font-weight: 600;
        }}
        .res-sd-pill {{
            background: rgba(107, 114, 128, 0.2);
            color: #cbd5e1;
            border: 1px solid rgba(107, 114, 128, 0.45);
        }}
        .hdr-pill {{
            background: rgba(245, 158, 11, 0.22);
            color: #fde68a;
            border: 1px solid rgba(245, 158, 11, 0.55);
            font-weight: 700;
        }}
        .surround-pill {{
            background: rgba(20, 184, 166, 0.2);
            color: #5eead4;
            border: 1px solid rgba(20, 184, 166, 0.5);
            font-weight: 600;
        }}
        .codec-pill {{
            background: rgba(99, 102, 241, 0.18);
            color: #a5b4fc;
            border: 1px solid rgba(99, 102, 241, 0.45);
            font-weight: 500;
        }}
        .dur-short-pill {{
            background: rgba(34, 197, 94, 0.16);
            color: #86efac;
            border: 1px solid rgba(34, 197, 94, 0.42);
            font-weight: 500;
        }}
        .dur-mid-pill {{
            background: rgba(56, 189, 248, 0.16);
            color: #7dd3fc;
            border: 1px solid rgba(56, 189, 248, 0.42);
            font-weight: 500;
        }}
        .dur-long-pill {{
            background: rgba(168, 85, 247, 0.18);
            color: #d8b4fe;
            border: 1px solid rgba(168, 85, 247, 0.45);
            font-weight: 600;
        }}
        .dur-epic-pill {{
            background: rgba(239, 68, 68, 0.22);
            color: #fca5a5;
            border: 1px solid rgba(239, 68, 68, 0.55);
            font-weight: 700;
            box-shadow: 0 0 6px rgba(239, 68, 68, 0.25);
        }}
        .cutscene-pill {{
            background: rgba(245, 158, 11, 0.2);
            color: #fde68a;
            border: 1px solid rgba(245, 158, 11, 0.5);
            font-weight: 600;
        }}
        .trailer-pill {{
            background: rgba(249, 115, 22, 0.2);
            color: #fdba74;
            border: 1px solid rgba(249, 115, 22, 0.5);
            font-weight: 600;
        }}
        .gameplay-pill {{
            background: rgba(16, 185, 129, 0.2);
            color: #6ee7b7;
            border: 1px solid rgba(16, 185, 129, 0.45);
            font-weight: 600;
        }}
        .music-pill {{
            background: rgba(236, 72, 153, 0.2);
            color: #f472b6;
            border: 1px solid rgba(236, 72, 153, 0.45);
            font-weight: 600;
        }}
        .vertical-pill {{
            background: rgba(139, 92, 246, 0.2);
            color: #c4b5fd;
            border: 1px solid rgba(139, 92, 246, 0.45);
            font-weight: 600;
        }}
        .alias-pill {{
            font-size: 0.74rem;
            font-weight: 500;
            padding: 3px 8px;
            border-radius: 5px;
            background: rgba(147, 51, 234, 0.14);
            color: #d8b4fe;
            border: 1px solid rgba(147, 51, 234, 0.32);
            display: inline-flex;
            align-items: center;
            gap: 5px;
        }}
        .alias-del-btn {{
            background: transparent;
            border: none;
            color: #f87171;
            font-size: 1.05rem;
            line-height: 1;
            cursor: pointer;
            padding: 0 3px;
            margin-left: 2px;
            border-radius: 3px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            transition: all 0.15s ease;
        }}
        .alias-del-btn:hover {{
            background: rgba(239, 68, 68, 0.25);
            color: #ef4444;
            transform: scale(1.15);
        }}
        .account-pill {{
            font-size: 0.72rem;
            font-weight: 600;
            padding: 2px 8px;
            border-radius: 9999px;
            background: rgba(236, 72, 153, 0.12);
            color: #f472b6;
            border: 1px solid rgba(236, 72, 153, 0.28);
            display: inline-flex;
            align-items: center;
            gap: 4px;
        }}
        .account-pill.is-primary {{
            background: rgba(168, 85, 247, 0.15);
            color: #c084fc;
            border-color: rgba(168, 85, 247, 0.35);
        }}

        .file-actions {{ display: flex; gap: 6px; flex-shrink: 0; }}
        .btn {{
            padding: 6px 11px;
            border-radius: 6px;
            border: none;
            cursor: pointer;
            font-size: 0.82rem;
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            gap: 5px;
            text-decoration: none;
            transition: opacity 0.2s;
            white-space: nowrap;
        }}
        .btn:hover {{ opacity: 0.9; }}
        .btn-play {{ background: var(--primary-hover); color: #fff; }}
        .btn-link {{ background: #1f2937; color: var(--text-muted); }}
        .btn-hf {{ background: rgba(251, 191, 36, 0.15); color: #fbbf24; border: 1px solid rgba(251, 191, 36, 0.3); }}
        .btn-hf:hover {{ background: #f59e0b; color: #000; }}
        .btn-edit {{ background: rgba(168, 85, 247, 0.15); color: #c084fc; border: 1px solid rgba(168, 85, 247, 0.3); }}
        .btn-edit:hover {{ background: #9333ea; color: #fff; }}
        .btn-delete {{ background: rgba(239, 68, 68, 0.15); color: #f87171; border: 1px solid rgba(239, 68, 68, 0.3); }}
        .btn-delete:hover {{ background: var(--danger); color: #fff; }}

        .empty-state {{
            text-align: center;
            padding: 48px;
            color: var(--text-muted);
            font-size: 1rem;
        }}
        .modal-overlay {{
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.75);
            display: none;
            justify-content: center;
            align-items: center;
            z-index: 1000;
            padding: 16px;
        }}
        .modal {{
            background: var(--panel);
            border: 1px solid var(--panel-border);
            padding: 20px 24px;
            border-radius: 12px;
            max-width: 560px;
            width: 100%;
            box-shadow: 0 20px 40px rgba(0,0,0,0.85);
            max-height: 88vh;
            display: flex;
            flex-direction: column;
            position: relative;
            box-sizing: border-box;
        }}
        .modal-header {{
            margin-bottom: 12px;
            padding-bottom: 8px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
        }}
        .modal-body {{
            overflow-y: auto;
            flex: 1;
            padding-right: 6px;
            margin-right: -4px;
        }}
        .modal h3 {{ margin-top: 0; color: #ef4444; }}
        .modal p {{ color: var(--text-muted); font-size: 0.92rem; line-height: 1.5; }}
        .modal-actions {{
            display: flex;
            justify-content: flex-end;
            align-items: center;
            gap: 10px;
            margin-top: 14px;
            padding-top: 12px;
            border-top: 1px solid var(--panel-border);
            background: var(--panel);
            position: sticky;
            bottom: 0;
            z-index: 20;
        }}

        .form-group {{ margin-bottom: 14px; }}
        .form-label {{
            display: block;
            font-size: 0.78rem;
            font-weight: 600;
            color: var(--text-muted);
            margin-bottom: 5px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        .form-input, .form-textarea {{
            width: 100%;
            padding: 9px 12px;
            background: #090d16;
            border: 1px solid var(--panel-border);
            border-radius: 6px;
            color: #fff;
            font-size: 0.9rem;
            outline: none;
            box-sizing: border-box;
        }}
        .form-input:focus, .form-textarea:focus {{ border-color: var(--primary); }}
        .form-row {{ display: flex; gap: 12px; }}
        .form-row .form-group {{ flex: 1; }}

        .quick-chips {{
            display: flex;
            flex-wrap: wrap;
            gap: 5px;
            margin-top: 6px;
        }}
        .quick-chip {{
            font-size: 0.72rem;
            padding: 2px 7px;
            background: #1f2937;
            border-radius: 4px;
            color: #9ca3af;
            cursor: pointer;
            user-select: none;
            transition: all 0.15s;
        }}
        .quick-chip:hover {{
            background: var(--primary);
            color: #000;
        }}

        .toast {{
            position: fixed;
            bottom: 24px;
            right: 24px;
            background: #10b981;
            color: #000;
            font-weight: 600;
            padding: 12px 20px;
            border-radius: 8px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.5);
            display: none;
            z-index: 2000;
            font-size: 0.9rem;
        }}

        /* Live Activity and Realtime Progress */
        .live-activity-box {{
            background: linear-gradient(180deg, #111827 0%, #0d131f 100%);
            border: 1px solid rgba(56, 189, 248, 0.25);
            border-radius: 10px;
            padding: 14px 18px;
            margin-bottom: 20px;
            box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
            transition: all 0.3s;
        }}
        .live-activity-box.is-active {{
            border-color: rgba(56, 189, 248, 0.7);
            box-shadow: 0 0 25px rgba(56, 189, 248, 0.2);
        }}
        .live-activity-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 12px;
        }}
        .live-activity-left {{
            display: flex;
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
        }}
        .status-pulse-dot {{
            width: 10px;
            height: 10px;
            border-radius: 50%;
            display: inline-block;
        }}
        .pulse-idle {{
            background: #10b981;
            box-shadow: 0 0 8px rgba(16, 185, 129, 0.7);
        }}
        .pulse-active {{
            background: #38bdf8;
            box-shadow: 0 0 10px rgba(56, 189, 248, 0.9);
            animation: pulse-anim 1.5s infinite;
        }}
        @keyframes pulse-anim {{
            0% {{ transform: scale(0.95); box-shadow: 0 0 0 0 rgba(56, 189, 248, 0.7); }}
            70% {{ transform: scale(1.15); box-shadow: 0 0 0 8px rgba(56, 189, 248, 0); }}
            100% {{ transform: scale(0.95); box-shadow: 0 0 0 0 rgba(56, 189, 248, 0); }}
        }}
        .live-active-badge {{
            font-size: 0.75rem;
            font-weight: 700;
            padding: 3px 8px;
            border-radius: 9999px;
            background: rgba(56, 189, 248, 0.2);
            color: #38bdf8;
            border: 1px solid rgba(56, 189, 248, 0.4);
        }}
        .active-tasks-grid {{
            display: flex;
            flex-direction: column;
            gap: 10px;
            margin-top: 14px;
            border-top: 1px solid var(--panel-border);
            padding-top: 12px;
        }}
        .active-task-card {{
            background: rgba(0, 0, 0, 0.35);
            border: 1px solid var(--panel-border);
            border-radius: 8px;
            padding: 10px 14px;
        }}
        .task-card-top {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 6px;
        }}
        .task-file-name {{
            font-weight: 600;
            font-size: 0.92rem;
            color: #f9fafb;
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .task-progress-track {{
            width: 100%;
            height: 8px;
            background: #1f2937;
            border-radius: 9999px;
            overflow: hidden;
            margin: 6px 0;
        }}
        .task-progress-fill {{
            height: 100%;
            background: linear-gradient(90deg, #38bdf8, #818cf8);
            border-radius: 9999px;
            transition: width 0.4s ease;
        }}
        .task-card-bottom {{
            display: flex;
            justify-content: space-between;
            font-size: 0.78rem;
            color: var(--text-muted);
        }}

        /* Activity Console / Log */
        .activity-log-panel {{
            margin-top: 12px;
            background: #06090e;
            border: 1px solid var(--panel-border);
            border-radius: 8px;
            padding: 10px 14px;
        }}
        .activity-log-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 0.78rem;
            color: var(--text-muted);
            text-transform: uppercase;
            font-weight: 600;
            margin-bottom: 8px;
        }}
        .activity-events-list {{
            max-height: 160px;
            overflow-y: auto;
            font-family: monospace;
            font-size: 0.8rem;
            display: flex;
            flex-direction: column;
            gap: 6px;
        }}
        .activity-event-row {{
            display: flex;
            align-items: baseline;
            gap: 8px;
            line-height: 1.4;
        }}
        .event-time {{
            color: #6b7280;
            font-size: 0.75rem;
            flex-shrink: 0;
        }}
        .event-badge {{
            font-size: 0.7rem;
            padding: 1px 5px;
            border-radius: 4px;
            font-weight: 600;
            text-transform: uppercase;
            flex-shrink: 0;
        }}
        .badge-success {{ background: rgba(16, 185, 129, 0.2); color: #34d399; }}
        .badge-upload {{ background: rgba(56, 189, 248, 0.2); color: #38bdf8; }}
        .badge-skip {{ background: rgba(245, 158, 11, 0.2); color: #fbbf24; }}
        .badge-error {{ background: rgba(239, 68, 68, 0.2); color: #f87171; }}
        .badge-info {{ background: rgba(168, 85, 247, 0.2); color: #c084fc; }}

        /* Accounts Modal Styling */
        .account-card {{
            background: #090d16;
            border: 1px solid var(--panel-border);
            border-radius: 8px;
            padding: 12px 14px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 12px;
            transition: border-color 0.2s;
        }}
        .account-card:hover {{
            border-color: #374151;
        }}
        .account-card.is-primary {{
            border-color: rgba(168, 85, 247, 0.45);
            background: rgba(168, 85, 247, 0.05);
        }}
        .account-card.is-disabled {{
            opacity: 0.55;
            border-style: dashed;
        }}
        .acc-left {{
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .acc-avatar {{
            width: 36px;
            height: 36px;
            border-radius: 8px;
            background: #1f2937;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.1rem;
        }}
        .acc-name {{
            font-weight: 600;
            font-size: 0.95rem;
            color: #f9fafb;
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .acc-token {{
            font-size: 0.75rem;
            color: var(--text-muted);
            font-family: monospace;
        }}
        .acc-actions {{
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .btn-xs {{
            padding: 4px 8px;
            font-size: 0.75rem;
        }}


        .btn-card-audio {{
            color: #4ade80 !important;
            border-color: rgba(34, 197, 94, 0.4) !important;
            background: rgba(34, 197, 94, 0.08) !important;
        }}
        .btn-card-audio:hover {{
            color: #86efac !important;
            border-color: #22c55e !important;
            background: rgba(34, 197, 94, 0.2) !important;
            box-shadow: 0 0 10px rgba(34, 197, 94, 0.3);
        }}
        .sub-pill {{
            background: rgba(59, 130, 246, 0.15);
            color: #93c5fd;
            border: 1px solid rgba(59, 130, 246, 0.35);
        }}
        .yt-sub-container {{
            position: relative;
            display: inline-flex;
            align-items: center;
        }}
        .yt-cc-btn {{
            position: relative;
            font-weight: bold;
        }}
        .yt-cc-btn.active {{
            color: var(--primary) !important;
            border-bottom: 2px solid var(--primary) !important;
        }}
        .yt-sub-menu {{
            position: absolute;
            bottom: calc(100% + 10px);
            right: 0;
            background: rgba(15, 23, 42, 0.96);
            backdrop-filter: blur(12px);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 8px 0;
            min-width: 175px;
            z-index: 100;
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.6);
        }}
        .yt-top-sub-menu {{
            bottom: auto !important;
            top: calc(100% + 6px) !important;
            right: 0 !important;
        }}
        .yt-top-cc-btn {{
            font-size: 0.85rem;
            color: #cbd5e1;
            padding: 4px 6px;
            border-radius: 4px;
            transition: all 0.2s ease;
        }}
        .yt-top-cc-btn:hover {{
            color: #fff;
            background: rgba(255, 255, 255, 0.12);
        }}
        .yt-top-cc-btn.active {{
            color: var(--primary) !important;
            background: rgba(56, 189, 248, 0.16) !important;
            border-bottom: 2px solid var(--primary) !important;
        }}
        .yt-sub-header {{
            padding: 4px 12px 6px;
            font-size: 0.75rem;
            font-weight: 700;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
            margin-bottom: 4px;
        }}
        .yt-sub-option {{
            padding: 6px 14px;
            font-size: 0.82rem;
            color: #e2e8f0;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: space-between;
            transition: background 0.15s ease;
        }}
        .yt-sub-option:hover {{
            background: rgba(255, 255, 255, 0.1);
            color: #fff;
        }}
        .yt-sub-option.active {{
            color: var(--primary);
            font-weight: 600;
        }}
        .yt-sub-option.active::after {{
            content: "\2713";
            color: var(--primary);
            font-weight: bold;
            font-size: 0.85rem;
        }}
        .yt-audio-mode-btn.active {{
            color: #4ade80 !important;
            background: rgba(34, 197, 94, 0.2) !important;
            border: 1px solid rgba(34, 197, 94, 0.5) !important;
        }}
        .yt-audio-visualizer {{
            position: absolute;
            inset: 0;
            background: radial-gradient(circle at center, #1e293b 0%, #0b0f19 100%);
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            z-index: 5;
            pointer-events: none;
        }}
        .yt-audio-icon {{
            font-size: 3rem;
            margin-bottom: 12px;
            animation: pulseIcon 2s infinite ease-in-out;
        }}
        .yt-audio-title {{
            font-size: 1.1rem;
            font-weight: 600;
            color: #f1f5f9;
            margin-bottom: 16px;
            max-width: 80%;
            text-align: center;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}
        .yt-audio-bars {{
            display: flex;
            align-items: flex-end;
            gap: 5px;
            height: 48px;
            padding-bottom: 4px;
        }}
        .yt-audio-bars .bar {{
            width: 6px;
            background: linear-gradient(180deg, #4ade80 0%, #059669 100%);
            border-radius: 3px;
            height: 12px;
            animation: bounceBar 1.2s infinite ease-in-out;
        }}
        .yt-audio-bars .bar:nth-child(1) {{ animation-delay: 0.1s; height: 18px; }}
        .yt-audio-bars .bar:nth-child(2) {{ animation-delay: 0.3s; height: 32px; }}
        .yt-audio-bars .bar:nth-child(3) {{ animation-delay: 0.5s; height: 42px; }}
        .yt-audio-bars .bar:nth-child(4) {{ animation-delay: 0.2s; height: 26px; }}
        .yt-audio-bars .bar:nth-child(5) {{ animation-delay: 0.4s; height: 38px; }}
        .yt-audio-bars .bar:nth-child(6) {{ animation-delay: 0.6s; height: 20px; }}
        .yt-audio-bars .bar:nth-child(7) {{ animation-delay: 0.35s; height: 34px; }}
        .yt-audio-bars .bar:nth-child(8) {{ animation-delay: 0.15s; height: 14px; }}
        @keyframes bounceBar {{
            0%, 100% {{ transform: scaleY(0.4); opacity: 0.6; }}
            50% {{ transform: scaleY(1.1); opacity: 1; }}
        }}
        @keyframes pulseIcon {{
            0%, 100% {{ transform: scale(1); filter: drop-shadow(0 0 4px rgba(74, 222, 128, 0.4)); }}
            50% {{ transform: scale(1.1); filter: drop-shadow(0 0 16px rgba(74, 222, 128, 0.8)); }}
        }}
        .sub-track-item {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 6px;
            padding: 6px 10px;
            font-size: 0.82rem;
        }}
        .sub-track-lang {{
            font-weight: 600;
            color: #60a5fa;
            text-transform: uppercase;
            font-size: 0.75rem;
            background: rgba(59, 130, 246, 0.12);
            padding: 2px 6px;
            border-radius: 4px;
        }}
        ::cue {{
            background-color: rgba(0, 0, 0, 0.75);
            color: #ffffff;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            font-size: 1.15rem;
            line-height: 1.4;
            text-shadow: 0 1px 2px rgba(0,0,0,0.8);
            border-radius: 4px;
            padding: 2px 8px;
        }}

    </style>
</head>
<body>
    <div class="container">
        <header class="vault-header">
            <div class="vault-brand-cluster">
                <span class="vault-brand-title">&#128274; HFSYNC Vault</span>
                <span id="hdrServerStatus" class="status-pill status-online" title="Streaming Proxy Server Status">
                    <span class="pill-dot"></span> Port 8080 Active
                </span>
                <span id="hdrWatcherStatus" class="status-pill status-idle" title="Directory Watcher Service Status">
                    <span class="pill-dot"></span> Watcher: Idle
                </span>
            </div>
            <div class="vault-actions-cluster">
                <button onclick="recheckCloudSync()" id="btnRecheckCloud" class="tool-btn" style="border-color: rgba(56, 189, 248, 0.4); color: #38bdf8; background: rgba(56, 189, 248, 0.12);" title="Recheck Hugging Face for new cloud uploads and sync">
                    &#128260; Recheck Cloud
                </button>
                <button onclick="scanAllTechTags()" id="btnScanTech" class="tool-btn" title="Extract resolution, audio, and video codecs">
                    &#127916; Scan Tech Specs
                </button>
                <button onclick="openAccountsModal()" class="tool-btn" style="border-color: rgba(168, 85, 247, 0.4); color: #c084fc; background: rgba(168, 85, 247, 0.12);" title="Manage connected Hugging Face accounts">
                    &#128101; Accounts (<span id="headerAccCount">{total_accounts}</span>)
                </button>
                <button onclick="toggleActivityLog()" id="btnToggleLog" class="tool-btn" title="View live pipeline activity log">
                    &#128220; Activity (<span id="eventCountBadge">0</span>)
                </button>
            </div>
        </header>

        <!-- Minimalist Metrics Strip (Saves over 150px height!) -->
        <div class="metrics-strip">
            <div class="metric-chip" title="Playable decrypted videos">
                <span>&#127916;</span>
                <span class="metric-val" id="statVideos">{len(files_data)}</span>
                <span>Videos</span>
            </div>
            <span class="metric-divider">&bull;</span>
            <div class="metric-chip" title="Total cloud storage across datasets">
                <span>&#9729;&#65039;</span>
                <span class="metric-val" id="statSize">{total_size_mb:.2f} MB</span>
                <span>Storage</span>
            </div>
            <span class="metric-divider">&bull;</span>
            <div class="metric-chip" title="Primary account for new uploads">
                <span>&#128081;</span>
                <span class="metric-val" style="color: var(--primary);">{primary_user}</span>
                <span>Primary</span>
            </div>
            <span class="metric-divider">&bull;</span>
            <div class="metric-chip clickable" onclick="openAccountsModal()" title="Connected Hugging Face accounts (Click to manage)">
                <span>&#128101;</span>
                <span class="metric-val" id="statAccounts">{total_accounts}</span>
                <span>Accounts <span style="font-size: 0.72rem; color: var(--text-muted);">&#9881;&#65039;</span></span>
            </div>
            <span class="metric-divider">&bull;</span>
            <div class="metric-chip" title="Target repositories on Hugging Face">
                <span>&#128230;</span>
                <span class="metric-val" id="statRepos">{len(repos_set)}</span>
                <span>Repos</span>
            </div>
            {skipped_stat_html}
        </div>

        {skipped_banner_html}

        <!-- Active Processing Tasks Grid (Appears only during active syncing) -->
        <div id="activeTasksContainer" class="active-tasks-grid" style="display: none;"></div>

        <!-- Expandable Live Activity Log / Console (Appears only when toggled) -->
        <div id="activityLogPanel" class="activity-log-panel" style="display: none;">
            <div class="activity-log-header">
                <span>Recent Pipeline Events</span>
                <button onclick="clearActivityEvents()" class="tool-btn btn-xs" style="font-size: 0.72rem;">Clear Log</button>
            </div>
            <div id="activityEventsList" class="activity-events-list"></div>
        </div>


        <div id="playerBox" class="yt-player-box">
            <div id="ytContainer" class="yt-container" tabindex="0" ondblclick="if (isFloatingMini) expandFromFloatingMini();">
                <!-- Main Video Element -->
                <video id="videoPlayer" playsinline preload="metadata"></video>

                <!-- Center Animated Feedback -->
                <div id="ytCenterFeedback" class="yt-center-feedback">▶</div>

                <!-- Audio Mode Visualizer Overlay -->
                <div id="ytAudioVisualizer" class="yt-audio-visualizer" style="display: none;">
                    <div class="yt-audio-icon">&#127925;</div>
                    <div id="ytAudioTrackTitle" class="yt-audio-title">Audio Mode Active</div>
                    <div class="yt-audio-bars">
                        <div class="bar"></div>
                        <div class="bar"></div>
                        <div class="bar"></div>
                        <div class="bar"></div>
                        <div class="bar"></div>
                        <div class="bar"></div>
                        <div class="bar"></div>
                        <div class="bar"></div>
                    </div>
                    <div style="font-size: 0.78rem; color: var(--text-muted); margin-top: 8px;">
                        Bandwidth saving companion stream &bull; AES-256-GCM
                    </div>
                </div>

                <!-- Buffering Spinner -->
                <div id="ytSpinner" class="yt-spinner" style="display: none;">
                    <div class="yt-spinner-ring"></div>
                </div>

                <!-- Top Title & Controls Overlay -->
                <div class="yt-top-overlay">
                    <div class="yt-top-title-group">
                        <span class="yt-security-badge"><span class="yt-live-dot"></span> Decrypted Stream</span>
                        <span id="playerTitle" class="yt-video-title">Now Playing</span>
                    </div>
                    <div class="yt-top-actions">
                        <div class="yt-mini-size-controls" title="Change Popup Player Size">
                            <button type="button" class="yt-mini-size-btn" data-size="s" onclick="setMiniPlayerSize('s', event)" title="Small (360px)">S</button>
                            <button type="button" class="yt-mini-size-btn active" data-size="m" onclick="setMiniPlayerSize('m', event)" title="Medium (500px)">M</button>
                            <button type="button" class="yt-mini-size-btn" data-size="l" onclick="setMiniPlayerSize('l', event)" title="Large (680px)">L</button>
                        </div>
                        <button type="button" class="yt-btn yt-btn-icon yt-mini-expand-btn" onclick="expandFromFloatingMini()" title="Expand to Main View">
                            <svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/></svg>
                        </button>
                        <button type="button" id="ytAudioModeBtn" class="yt-btn yt-btn-icon yt-audio-mode-btn" onclick="toggleAudioMode()" title="Toggle Audio Mode (Lightweight stream / visualizer)">
                            &#127925;
                        </button>
                        <!-- Top CC Button & Menu Wrap -->
                        <div class="yt-top-cc-wrap" style="position: relative; display: inline-flex;">
                            <button type="button" id="ytTopCcBtn" class="yt-btn yt-btn-icon yt-cc-btn yt-top-cc-btn" onclick="handleCcButtonClick(event, this)" oncontextmenu="openSubtitlesMenuFromBtn(event, this); return false;" title="Subtitles / Closed Captions (c) - Right-click for menu">
                                <svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M19 4H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2zm-8 7H9.5v-.5h-2v3h2V13H11v1a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1v-4a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v1zm7 0h-1.5v-.5h-2v3h2V13H18v1a1 1 0 0 1-1 1h-3a1 1 0 0 1-1-1v-4a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v1z"/></svg>
                            </button>
                            <div id="ytTopSubMenu" class="yt-sub-menu yt-top-sub-menu" style="display: none;">
                                <div class="yt-sub-header">
                                    <span>Subtitles / CC</span>
                                    <button type="button" class="tool-btn btn-xs" onclick="openUploadSubModalFromPlayer()" style="font-size: 0.68rem; padding: 2px 6px;">+ Add .srt</button>
                                </div>
                                <div id="ytTopSubTrackList" class="yt-sub-tracks">
                                    <div class="yt-sub-option active" onclick="setSubtitleTrack('off')">Off</div>
                                </div>
                            </div>
                        </div>
                        <button type="button" class="yt-btn yt-btn-icon" onclick="toggleStatsForNerds()" title="Stats for Nerds (S)">
                            <svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-2 10h-4v4h-2v-4H7v-2h4V7h2v4h4v2z"/></svg>
                        </button>
                        <button type="button" class="yt-btn yt-btn-icon" onclick="toggleTheaterMode()" title="Theater Mode (T)">
                            <svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M19 6H5c-1.1 0-2 .9-2 2v8c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2zm0 10H5V8h14v8z"/></svg>
                        </button>
                        <button type="button" class="yt-btn yt-btn-close" onclick="closePlayer()" title="Close Player (Esc)">&times;</button>
                    </div>
                </div>

                <!-- Stats for Nerds Panel -->
                <div id="ytStatsPanel" class="yt-stats-panel" style="display: none;">
                    <div class="yt-stats-header">
                        <span>Decrypted Stream Stats</span>
                        <span style="cursor: pointer;" onclick="toggleStatsForNerds()">&times;</span>
                    </div>
                    <div class="yt-stats-grid">
                        <div>Video Resolution:</div><div id="ytStatRes">-</div>
                        <div>Decryption Engine:</div><div>AES-256-GCM (64KB blocks)</div>
                        <div>Stream Position:</div><div id="ytStatPos">-</div>
                        <div>Buffer Health:</div><div id="ytStatBuffer">-</div>
                        <div>Playback Rate:</div><div id="ytStatSpeed">1x</div>
                        <div>Decrypted Blob:</div><div id="ytStatBlob" style="font-size: 0.7rem; word-break: break-all;">-</div>
                    </div>
                </div>

                <!-- Bottom Controls Overlay -->
                <div id="ytControls" class="yt-controls-overlay">
                    <!-- Progress / Scrub Bar -->
                    <div id="ytScrubContainer" class="yt-scrub-container">
                        <div class="yt-scrub-track">
                            <div id="ytScrubBuffered" class="yt-scrub-buffered"></div>
                            <div id="ytScrubProgress" class="yt-scrub-progress">
                                <div class="yt-scrub-handle"></div>
                            </div>
                        </div>
                        <div id="ytScrubTooltip" class="yt-scrub-tooltip">
                            <div id="ytThumbPreview" class="yt-thumb-preview" style="display: none;"></div>
                            <div id="ytTooltipTime" class="yt-tooltip-time">00:00</div>
                        </div>
                    </div>

                    <!-- Control Buttons Row -->
                    <div class="yt-controls-row">
                        <div class="yt-controls-left">
                            <button type="button" id="ytPlayPauseBtn" class="yt-btn yt-btn-icon" onclick="togglePlayPause()" title="Play/Pause (k / Space)">
                                <svg id="ytPlayIcon" viewBox="0 0 24 24" width="22" height="22"><path fill="currentColor" d="M8 5v14l11-7z"/></svg>
                                <svg id="ytPauseIcon" viewBox="0 0 24 24" width="22" height="22" style="display: none;"><path fill="currentColor" d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>
                            </button>
                            <button type="button" class="yt-btn yt-btn-icon" onclick="seekRelative(-10)" title="Rewind 10s (j / Left Arrow)">
                                <svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M12.5 8c-2.65 0-5.05.99-6.9 2.6L2 7v9h9l-3.62-3.62c1.39-1.2 3.16-1.88 5.12-1.88 3.54 0 6.55 2.31 7.6 5.5l2.37-.78C21.08 11.03 17.15 8 12.5 8z"/><text x="12" y="19" font-size="7" font-weight="bold" fill="currentColor" text-anchor="middle">10</text></svg>
                            </button>
                            <button type="button" class="yt-btn yt-btn-icon" onclick="seekRelative(10)" title="Fast Forward 10s (l / Right Arrow)">
                                <svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M11.5 8c2.65 0 5.05.99 6.9 2.6L22 7v9h-9l3.62-3.62c-1.39-1.2-3.16-1.88-5.12-1.88-3.54 0-6.55 2.31-7.6 5.5l-2.37-.78C2.92 11.03 6.85 8 11.5 8z"/><text x="12" y="19" font-size="7" font-weight="bold" fill="currentColor" text-anchor="middle">10</text></svg>
                            </button>
                            <div class="yt-volume-group">
                                <button type="button" id="ytMuteBtn" class="yt-btn yt-btn-icon" onclick="toggleMute()" title="Mute (m)">
                                    <svg id="ytVolHigh" viewBox="0 0 24 24" width="22" height="22"><path fill="currentColor" d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z"/></svg>
                                    <svg id="ytVolMute" viewBox="0 0 24 24" width="22" height="22" style="display: none;"><path fill="currentColor" d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51C20.63 14.91 21 13.5 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3L3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06c1.38-.31 2.63-.95 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4L9.91 6.09 12 8.18V4z"/></svg>
                                </button>
                                <div class="yt-volume-slider-wrap">
                                    <input type="range" id="ytVolumeSlider" class="yt-volume-slider" min="0" max="1" step="0.05" value="1">
                                </div>
                            </div>
                            <div class="yt-time-display">
                                <span id="ytCurTime">00:00</span> / <span id="ytDurTime">00:00</span>
                            </div>
                        </div>

                        <div class="yt-controls-right">
                            <div class="yt-speed-container">
                                <button type="button" id="ytSpeedBtn" class="yt-btn yt-btn-speed" onclick="toggleSpeedMenu()" title="Playback Speed">1x</button>
                                <div id="ytSpeedMenu" class="yt-speed-menu" style="display: none;">
                                    <div class="yt-speed-title">Playback Speed</div>
                                    <div class="yt-speed-option" onclick="setSpeed(0.25)">0.25x</div>
                                    <div class="yt-speed-option" onclick="setSpeed(0.5)">0.5x</div>
                                    <div class="yt-speed-option" onclick="setSpeed(0.75)">0.75x</div>
                                    <div class="yt-speed-option active" onclick="setSpeed(1)">Normal</div>
                                    <div class="yt-speed-option" onclick="setSpeed(1.25)">1.25x</div>
                                    <div class="yt-speed-option" onclick="setSpeed(1.5)">1.5x</div>
                                    <div class="yt-speed-option" onclick="setSpeed(1.75)">1.75x</div>
                                    <div class="yt-speed-option" onclick="setSpeed(2)">2x</div>
                                </div>
                            </div>

                            <!-- Subtitles / CC Menu Container -->
                            <div class="yt-sub-container">
                                <button type="button" id="ytCcBtn" class="yt-btn yt-btn-icon yt-cc-btn" onclick="toggleSubtitlesMenu()" title="Subtitles / Closed Captions (c)">
                                    <svg viewBox="0 0 24 24" width="22" height="22"><path fill="currentColor" d="M19 4H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2zm-8 7H9.5v-.5h-2v3h2V13H11v1a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1v-4a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v1zm7 0h-1.5v-.5h-2v3h2V13H18v1a1 1 0 0 1-1 1h-3a1 1 0 0 1-1-1v-4a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1v1z"/></svg>
                                </button>
                                <div id="ytSubMenu" class="yt-sub-menu" style="display: none;">
                                    <div class="yt-sub-header">
                                        <span>Subtitles / CC</span>
                                        <button type="button" class="tool-btn btn-xs" onclick="openUploadSubModalFromPlayer()" style="font-size: 0.68rem; padding: 2px 6px;">+ Add .srt</button>
                                    </div>
                                    <div id="ytSubTrackList" class="yt-sub-tracks">
                                        <div class="yt-sub-option active" onclick="setSubtitleTrack('off')">Off</div>
                                    </div>
                                </div>
                            </div>

                            <button type="button" class="yt-btn yt-btn-icon" onclick="togglePiP()" title="Picture-in-Picture (p)">
                                <svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M19 7h-8v6h8V7zm2-4H3c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h18c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 16.01H3V4.99h18v14.02z"/></svg>
                            </button>

                            <button type="button" class="yt-btn yt-btn-icon" onclick="toggleTheaterMode()" title="Theater Mode (t)">
                                <svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M19 6H5c-1.1 0-2 .9-2 2v8c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2zm0 10H5V8h14v8z"/></svg>
                            </button>

                            <button type="button" id="ytFullscreenBtn" class="yt-btn yt-btn-icon" onclick="toggleFullscreen()" title="Fullscreen (f)">
                                <svg id="ytFsEnter" viewBox="0 0 24 24" width="22" height="22"><path fill="currentColor" d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/></svg>
                                <svg id="ytFsExit" viewBox="0 0 24 24" width="22" height="22" style="display: none;"><path fill="currentColor" d="M5 16h3v3h2v-5H5v2zm3-8H5v2h5V5H8v3zm6 11h2v-3h3v-2h-5v5zm2-11V5h-2v5h5V8h-3z"/></svg>
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <div class="sticky-filter-bar" id="stickyFilterBar">
            <div class="toolbar">
                <input type="text" id="searchInput" class="search-input" placeholder="&#128269; Search by title, tag, genre, account, folder..." oninput="filterTree()">
                
                <div class="multi-filter-group">
                    <!-- Multi-select Tags Popover -->
                    <div class="popover-wrapper" id="tagPopoverWrapper">
                        <button type="button" class="filter-pill-btn" id="tagPillBtn" onclick="toggleFilterPopover('tagPopover', event)" title="Filter by tags (resolution, duration, content type)">
                            <span class="pill-icon">&#127991;</span>
                            <span class="pill-label">Tags</span>
                            <span class="pill-badge" id="tagCountBadge" style="display: none;">0</span>
                            <span class="pill-arrow">&#9662;</span>
                        </button>
                        <div class="filter-popover" id="tagPopover" style="display: none;">
                            <div class="popover-header">
                                <div style="display: flex; gap: 6px; align-items: center; margin-bottom: 6px;">
                                    <input type="text" class="popover-search" placeholder="Filter tags..." oninput="filterPopoverList('tagPopover', this.value)" style="margin-bottom: 0;">
                                    <span id="popoverHiddenTagsCount" class="popover-hidden-badge" style="display: none;" onclick="unhideAllTags(event)" title="Click to unhide all hidden tags"></span>
                                </div>
                                <div class="popover-actions">
                                    <button type="button" onclick="selectAllFilter('tag')">Select All</button>
                                    <button type="button" onclick="clearFilter('tag')">Clear</button>
                                    <button type="button" class="btn-merge-tags-popover" onclick="openMergeTagsModal(event)" title="Merge multiple tags into one unified tag">&#128256; Merge</button>
                                    <button type="button" onclick="closeAllPopovers()" style="color:#38bdf8;font-weight:700;">Done</button>
                                </div>
                            </div>
                            <div class="popover-list" id="tagPopoverList"></div>
                        </div>
                    </div>

                    <!-- Multi-select Categories Popover -->
                    <div class="popover-wrapper" id="genrePopoverWrapper">
                        <button type="button" class="filter-pill-btn" id="genrePillBtn" onclick="toggleFilterPopover('genrePopover', event)" title="Filter by category or genre">
                            <span class="pill-icon">&#128193;</span>
                            <span class="pill-label">Categories</span>
                            <span class="pill-badge" id="genreCountBadge" style="display: none;">0</span>
                            <span class="pill-arrow">&#9662;</span>
                        </button>
                        <div class="filter-popover" id="genrePopover" style="display: none;">
                            <div class="popover-header">
                                <input type="text" class="popover-search" placeholder="Filter categories..." oninput="filterPopoverList('genrePopover', this.value)">
                                <div class="popover-actions">
                                    <button type="button" onclick="selectAllFilter('genre')">Select All</button>
                                    <button type="button" onclick="clearFilter('genre')">Clear</button>
                                    <button type="button" onclick="closeAllPopovers()" style="color:#38bdf8;font-weight:700;">Done</button>
                                </div>
                            </div>
                            <div class="popover-list" id="genrePopoverList"></div>
                        </div>
                    </div>

                    <!-- Multi-select Accounts Popover -->
                    <div class="popover-wrapper" id="accountPopoverWrapper">
                        <button type="button" class="filter-pill-btn" id="accountPillBtn" onclick="toggleFilterPopover('accountPopover', event)" title="Filter by Hugging Face account">
                            <span class="pill-icon">&#128100;</span>
                            <span class="pill-label">Accounts</span>
                            <span class="pill-badge" id="accountCountBadge" style="display: none;">0</span>
                            <span class="pill-arrow">&#9662;</span>
                        </button>
                        <div class="filter-popover" id="accountPopover" style="display: none;">
                            <div class="popover-header">
                                <input type="text" class="popover-search" placeholder="Filter accounts..." oninput="filterPopoverList('accountPopover', this.value)">
                                <div class="popover-actions">
                                    <button type="button" onclick="selectAllFilter('account')">Select All</button>
                                    <button type="button" onclick="clearFilter('account')">Clear</button>
                                    <button type="button" onclick="closeAllPopovers()" style="color:#38bdf8;font-weight:700;">Done</button>
                                </div>
                            </div>
                            <div class="popover-list" id="accountPopoverList"></div>
                        </div>
                    </div>

                    <button onclick="resetAllFilters()" id="btnResetFilters" class="tool-btn" style="display: none;" title="Reset all active filters and search">&#128260; Reset</button>
                    <span id="filterSummaryCount" class="filter-summary-count" style="display: none;"></span>
                </div>

                <button onclick="toggleHideTags()" id="btnHideTags" class="tool-btn" title="Toggle tag badges visibility on video cards">&#127991; Tags: <span id="tagsStateLabel">Shown</span></button>
                <button onclick="syncMetadataToHf()" id="syncHfBtn" class="tool-btn" title="Encrypt and sync all metadata to Hugging Face">&#9729;&#65039; Sync to HF</button>
                <button onclick="expandAll()" class="tool-btn">&#9660; Expand All</button>
                <button onclick="collapseAll()" class="tool-btn">&#9654; Collapse All</button>
                <div class="view-toggle-group">
                    <button onclick="setViewMode('card')" id="btnViewCard" class="view-toggle-btn" title="Switch to Cards Grid View">&#127183; Cards</button>
                    <button onclick="setViewMode('list')" id="btnViewList" class="view-toggle-btn" title="Switch to List Row View">&#9776; List</button>
                </div>

                <div class="sticky-status-cluster">
                    <span id="stickyServerStatus" class="status-pill status-online" title="Streaming Proxy Server Status">
                        <span class="pill-dot"></span> 8080
                    </span>
                    <span id="stickyWatcherStatus" class="status-pill status-idle" title="Directory Watcher Service Status">
                        <span class="pill-dot"></span> Watcher: Idle
                    </span>
                </div>
            </div>

            <div id="activeFilterChipsBar" class="active-chips-bar" style="display: none;"></div>
        </div>

        <div class="tree-container" id="treeContainer">
            <!-- Tree populated dynamically via JS -->
        </div>
    </div>

    <!-- Metadata & Tags Edit Modal -->
    <div id="metadataModal" class="modal-overlay">
        <div class="modal">
            <div class="modal-header">
                <h3 style="color: var(--primary); margin: 0 0 4px 0;">&#127991; Edit Jellyfin Metadata &amp; Tags</h3>
                <span style="font-size: 0.8rem; color: #9ca3af;">
                    Changes update <code>upload_manifest.json</code> and standard Jellyfin <code>.nfo</code> XML files.
                </span>
            </div>
            <div class="modal-body">
                <input type="hidden" id="editKey">

            <div id="editAliasesBox" style="margin-bottom: 16px; background: rgba(147, 51, 234, 0.08); border: 1px solid rgba(147, 51, 234, 0.28); border-radius: 8px; padding: 12px 14px; display: none;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                    <div style="font-size: 0.78rem; font-weight: 600; color: #c084fc; text-transform: uppercase; letter-spacing: 0.5px;">
                        &#128206; Associated File Names &amp; Aliases
                    </div>
                    <span style="font-size: 0.72rem; color: #9ca3af;">Click &times; to remove unsuitable names</span>
                </div>
                <div id="editAliasesList" style="display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px;"></div>
                
                <div style="display: flex; gap: 6px; align-items: center; border-top: 1px solid rgba(147, 51, 234, 0.15); padding-top: 8px;">
                    <input type="text" id="newAliasInput" class="form-input form-input-sm" placeholder="Add suitable filename (e.g. Inception (2010).mp4)" style="flex: 1; font-size: 0.8rem; padding: 5px 8px;">
                    <button type="button" class="btn btn-hf btn-xs" onclick="submitAddAlias()" style="padding: 5px 12px; white-space: nowrap;">+ Add Name</button>
                </div>
            </div>

            <div class="form-row">
                <div class="form-group" style="flex: 2;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                        <label class="form-label" style="margin-bottom: 0;">Movie / Video Title</label>
                        <button type="button" class="tool-btn btn-xs" onclick="cleanCurrentEditTitle()" title="Remove file extensions and fix underscores from current title" style="padding: 2px 7px; font-size: 0.70rem;">
                            &#129529; Clean Title
                        </button>
                    </div>
                    <input type="text" id="editTitle" class="form-input" placeholder="Title">
                    <div id="aliasTitlePickerBox" style="margin-top: 6px; display: none;">
                        <div style="font-size: 0.72rem; color: #c084fc; font-weight: 600; margin-bottom: 4px;">
                            &#128161; Available Names (click to replace title &bull; fixes extension &bull; editable):
                        </div>
                        <div id="aliasTitlePickerList" style="display: flex; flex-wrap: wrap; gap: 4px;"></div>
                    </div>
                </div>
                <div class="form-group" style="flex: 1;">
                    <label class="form-label">Year</label>
                    <input type="text" id="editYear" class="form-input" placeholder="e.g. 2024">
                </div>
                <div class="form-group" style="flex: 1;">
                    <label class="form-label">Rating</label>
                    <input type="text" id="editRating" class="form-input" placeholder="e.g. 8.5">
                </div>
            </div>

            <div class="form-group">
                <label class="form-label" style="color: #fbbf24; font-weight: 700;">&#128193; Categories / Folder Genres (comma-separated)</label>
                <input type="text" id="editGenres" class="form-input" placeholder="Action, Sci-Fi, Adventure" style="border-color: rgba(245, 158, 11, 0.4);">
                <div class="quick-chips">
                    <span class="quick-chip" style="background: rgba(245, 158, 11, 0.15); border-color: rgba(245, 158, 11, 0.4); color: #fbbf24;" onclick="addGenreChip('Action')">+ Action</span>
                    <span class="quick-chip" style="background: rgba(245, 158, 11, 0.15); border-color: rgba(245, 158, 11, 0.4); color: #fbbf24;" onclick="addGenreChip('Sci-Fi')">+ Sci-Fi</span>
                    <span class="quick-chip" style="background: rgba(245, 158, 11, 0.15); border-color: rgba(245, 158, 11, 0.4); color: #fbbf24;" onclick="addGenreChip('Comedy')">+ Comedy</span>
                    <span class="quick-chip" style="background: rgba(245, 158, 11, 0.15); border-color: rgba(245, 158, 11, 0.4); color: #fbbf24;" onclick="addGenreChip('Drama')">+ Drama</span>
                    <span class="quick-chip" style="background: rgba(245, 158, 11, 0.15); border-color: rgba(245, 158, 11, 0.4); color: #fbbf24;" onclick="addGenreChip('Horror')">+ Horror</span>
                    <span class="quick-chip" style="background: rgba(245, 158, 11, 0.15); border-color: rgba(245, 158, 11, 0.4); color: #fbbf24;" onclick="addGenreChip('Thriller')">+ Thriller</span>
                    <span class="quick-chip" style="background: rgba(245, 158, 11, 0.15); border-color: rgba(245, 158, 11, 0.4); color: #fbbf24;" onclick="addGenreChip('Animation')">+ Animation</span>
                    <span class="quick-chip" style="background: rgba(245, 158, 11, 0.15); border-color: rgba(245, 158, 11, 0.4); color: #fbbf24;" onclick="addGenreChip('Anime')">+ Anime</span>
                    <span class="quick-chip" style="background: rgba(245, 158, 11, 0.15); border-color: rgba(245, 158, 11, 0.4); color: #fbbf24;" onclick="addGenreChip('Documentary')">+ Documentary</span>
                </div>
            </div>

            <div class="form-group">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                    <label class="form-label" style="margin-bottom: 0; color: #38bdf8; font-weight: 700;">&#127991; Tags (comma-separated)</label>
                    <button type="button" class="tool-btn btn-xs" onclick="openMergeTagsModal(event)" style="color: #c084fc; border-color: rgba(168, 85, 247, 0.4); padding: 2px 7px; font-size: 0.70rem;" title="Merge multiple tags into one tag name">
                        &#128256; Merge Tags
                    </button>
                </div>
                <input type="text" id="editTags" class="form-input" placeholder="4K, Favorites, Streamable" style="border-color: rgba(56, 189, 248, 0.4);">
                <div class="quick-chips">
                    <span class="quick-chip" onclick="addTagChip('Favorites')">+ Favorites</span>
                    <span class="quick-chip" onclick="addTagChip('4K')">+ 4K</span>
                    <span class="quick-chip" onclick="addTagChip('1080p')">+ 1080p</span>
                    <span class="quick-chip" onclick="addTagChip('HDR')">+ HDR</span>
                    <span class="quick-chip" onclick="addTagChip('Streamable')">+ Streamable</span>
                    <span class="quick-chip" onclick="addTagChip('Must-Watch')">+ Must-Watch</span>
                    <span class="quick-chip" onclick="addTagChip('Family')">+ Family</span>
                </div>
            </div>

            <div class="form-group">
                <label class="form-label">Overview / Storyline</label>
                <textarea id="editOverview" class="form-textarea" rows="3" placeholder="Brief storyline or plot summary..."></textarea>
            </div>

            <!-- Subtitles & Closed Captions Section -->
            <div id="editSubtitlesSection" style="margin-top: 14px; background: rgba(59, 130, 246, 0.08); border: 1px solid rgba(59, 130, 246, 0.28); border-radius: 8px; padding: 12px 14px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                    <div style="font-size: 0.78rem; font-weight: 600; color: #60a5fa; text-transform: uppercase; letter-spacing: 0.5px;">
                        &#128172; Subtitles &amp; Closed Captions
                    </div>
                    <span style="font-size: 0.72rem; color: #9ca3af;">Encrypted on HF &bull; Synced to Jellyfin</span>
                </div>
                <div id="editSubtitlesList" style="display: flex; flex-direction: column; gap: 6px; margin-bottom: 10px;">
                    <!-- Subtitle items rendered dynamically -->
                </div>
                
                <div style="display: flex; gap: 6px; align-items: center; border-top: 1px solid rgba(59, 130, 246, 0.15); padding-top: 8px; flex-wrap: wrap;">
                    <input type="file" id="modalSubFileInput" accept=".srt,.vtt" style="display: none;" onchange="handleModalSubFilePicked(this)">
                    <button type="button" class="tool-btn btn-xs" onclick="document.getElementById('modalSubFileInput').click()" style="padding: 5px 10px;">
                        &#128194; Choose .srt / .vtt File
                    </button>
                    <span id="modalSubFileName" style="font-size: 0.75rem; color: var(--text-muted); flex: 1; min-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">No file chosen</span>
                    <select id="modalSubLangSelect" class="filter-select" style="font-size: 0.75rem; padding: 4px 8px; height: 28px;">
                        <option value="en">English (en)</option>
                        <option value="es">Spanish (es)</option>
                        <option value="fr">French (fr)</option>
                        <option value="de">German (de)</option>
                        <option value="it">Italian (it)</option>
                        <option value="pt">Portuguese (pt)</option>
                        <option value="ru">Russian (ru)</option>
                        <option value="hi">Hindi (hi)</option>
                        <option value="zh">Chinese (zh)</option>
                        <option value="ja">Japanese (ja)</option>
                        <option value="ko">Korean (ko)</option>
                        <option value="ar">Arabic (ar)</option>
                        <option value="other">Other / Custom</option>
                    </select>
                    <button type="button" id="btnUploadModalSub" class="btn btn-hf btn-xs" onclick="submitUploadSubtitle()" style="padding: 5px 12px; white-space: nowrap;" disabled>+ Upload Subtitle</button>
                </div>
            </div>
            </div> <!-- End of modal-body -->

            <div class="modal-actions">
                <button onclick="closeMetadataModal()" class="tool-btn">Cancel</button>
                <button id="saveMetadataBtn" onclick="saveMetadata()" class="btn btn-play" style="padding: 8px 18px;">&#128190; Save to Jellyfin</button>
            </div>
        </div>
    </div>

    <!-- Merge Tags Modal -->
    <div id="mergeTagsModal" class="modal-overlay" style="display: none;">
        <div class="modal" style="max-width: 520px;">
            <div class="modal-header">
                <h3 style="color: #c084fc; margin: 0 0 4px 0;">&#128256; Merge Multiple Tags</h3>
                <span style="font-size: 0.8rem; color: #9ca3af;">
                    Combine multiple tags (e.g. <code>movie</code> and <code>videos</code>) into a single unified tag (e.g. <code>playable</code>). All matching videos are updated immediately.
                </span>
            </div>
            <div class="modal-body">
                <div class="form-group">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px;">
                        <label class="form-label" style="margin-bottom: 0;">1. Select Tags to Merge</label>
                        <span id="mergeSelectedCount" style="color: #c084fc; font-weight: 600; font-size: 0.75rem;">0 selected</span>
                    </div>
                    <input type="text" id="mergeTagSearch" class="form-input form-input-sm" placeholder="Filter tags to select..." oninput="filterMergeTagList(this.value)" style="margin-bottom: 6px; font-size: 0.8rem; padding: 5px 8px;">
                    <div id="mergeTagList" style="max-height: 180px; overflow-y: auto; background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.1); border-radius: 6px; padding: 6px;"></div>
                </div>

                <div class="form-group" style="margin-top: 14px;">
                    <label class="form-label">2. Target Merged Tag Name</label>
                    <input type="text" id="mergeTargetTagInput" class="form-input" placeholder="e.g. playable" oninput="updateMergeBtnState()">
                    <div id="mergeTargetSuggestions" style="display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px;"></div>
                </div>
            </div>
            <div class="modal-actions">
                <button type="button" onclick="closeMergeTagsModal()" class="tool-btn">Cancel</button>
                <button type="button" id="btnSubmitMergeTags" onclick="submitMergeTags()" class="btn btn-play" style="background: #9333ea; border-color: #a855f7; padding: 8px 18px;" disabled>
                    &#128256; Merge Tags
                </button>
            </div>
        </div>
    </div>

    <!-- Skipped / Incompatible Content Modal -->
    <div id="skippedModal" class="modal-overlay">
        <div class="modal" style="max-width: 640px;">
            <h3 style="color: #f59e0b; margin-bottom: 6px;">&#128274; Skipped Content (Password Mismatch)</h3>
            <p style="margin-top: 0; margin-bottom: 16px; font-size: 0.85rem; color: var(--text-muted); line-height: 1.5;">
                These files exist in your Hugging Face vault or manifest, but <b>cannot be decrypted</b> using your current <code>ENCRYPTION_PASSWORD</code>. To prevent playback errors, they are skipped and hidden from your Jellyfin library.
            </p>

            <div id="skippedListContainer" style="max-height: 380px; overflow-y: auto; margin-bottom: 16px;">
                <!-- Populated dynamically via JS -->
            </div>

            <div class="modal-actions" style="justify-content: space-between;">
                <button id="deleteAllSkippedBtn" onclick="deleteAllSkippedFiles()" class="btn btn-delete">&#128465; Delete All Skipped from HF</button>
                <button onclick="closeSkippedModal()" class="tool-btn">Close</button>
            </div>
        </div>
    </div>

    <!-- Delete Confirmation Modal -->
    <div id="deleteModal" class="modal-overlay">
        <div class="modal">
            <h3 id="modalTitle">&#9888; Delete Content Permanently</h3>
            <p id="deleteMessage">Are you sure you want to delete this content from Hugging Face and your Jellyfin library?</p>
            <div class="modal-actions">
                <button onclick="closeDeleteModal()" class="tool-btn">Cancel</button>
                <button id="confirmDeleteBtn" class="btn btn-delete" style="padding: 8px 16px;">Delete Permanently</button>
            </div>
        </div>
    </div>

        <!-- Accounts Management Modal -->
    <div id="accountsModal" class="modal-overlay">
        <div class="modal" style="max-width: 680px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
                <h3 style="color: var(--primary); margin: 0; display: flex; align-items: center; gap: 8px;">
                    &#128101; Hugging Face Accounts
                </h3>
                <button onclick="closeAccountsModal()" class="tool-btn btn-xs" style="font-size: 1.1rem; padding: 2px 8px;">&times;</button>
            </div>
            <p style="margin-top: 0; margin-bottom: 16px; font-size: 0.84rem; color: var(--text-muted); line-height: 1.5;">
                Connect multiple Hugging Face accounts to distribute storage. All accounts share your master encryption password. New video uploads always route to your <b>Primary Upload Account (&#128081;)</b>.
            </p>

            <!-- Accounts List -->
            <div id="accountsListContainer" style="margin-bottom: 20px; display: flex; flex-direction: column; gap: 10px; max-height: 320px; overflow-y: auto;">
                <!-- Populated dynamically via JS -->
            </div>

            <!-- Add New Account Form -->
            <div style="background: rgba(255,255,255,0.03); border: 1px solid var(--panel-border); border-radius: 8px; padding: 14px;">
                <div style="font-weight: 600; font-size: 0.9rem; color: #fff; margin-bottom: 8px; display: flex; align-items: center; gap: 6px;">
                    &#10133; Connect New Hugging Face Account
                </div>
                <div class="form-group" style="margin-bottom: 10px;">
                    <label class="form-label">Hugging Face Access Token</label>
                    <input type="password" id="newAccountToken" class="form-input" placeholder="hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" autocomplete="off">
                </div>
                <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px;">
                    <label style="display: flex; align-items: center; gap: 8px; font-size: 0.84rem; cursor: pointer; color: var(--text);">
                        <input type="checkbox" id="chkSetPrimary" style="cursor: pointer;">
                        Set as Primary Upload Account (&#128081; All new uploads)
                    </label>
                    <button id="btnAddAccount" onclick="submitAddAccount()" class="btn btn-play" style="padding: 7px 16px;">
                        &#129303; Validate &amp; Connect
                    </button>
                </div>
            </div>

            <div class="modal-actions" style="margin-top: 18px;">
                <button onclick="closeAccountsModal()" class="tool-btn">Close</button>
            </div>
        </div>
    </div>

    <!-- Delete Account Confirmation Modal -->
    <div id="deleteAccountModal" class="modal-overlay" style="z-index: 1100;">
        <div class="modal" style="max-width: 480px;">
            <h3 style="color: var(--danger); margin-top: 0;">&#9888; Remove Hugging Face Account</h3>
            <p id="deleteAccountPrompt" style="color: var(--text); font-size: 0.92rem; line-height: 1.5;"></p>
            <div style="margin: 14px 0; background: rgba(239, 68, 68, 0.08); border: 1px solid rgba(239, 68, 68, 0.2); border-radius: 6px; padding: 10px 12px;">
                <label style="display: flex; align-items: flex-start; gap: 8px; font-size: 0.84rem; cursor: pointer; color: #fca5a5;">
                    <input type="checkbox" id="chkPurgeManifest" style="margin-top: 2px;">
                    <span>Also remove local stream pointers (<code>.strm</code>) for videos hosted on this account</span>
                </label>
            </div>
            <div class="modal-actions">
                <button onclick="closeDeleteAccountModal()" class="tool-btn">Cancel</button>
                <button id="btnConfirmDeleteAccount" class="btn btn-delete" style="padding: 8px 16px;">Remove Account</button>
            </div>
        </div>
    </div>

    <!-- Screens Contact Sheet Lightbox Modal -->
    <div id="screensModal" class="modal-overlay" onclick="closeScreensModal(event)" style="z-index: 1200; display: none;">
        <div class="modal" style="max-width: 94vw; width: 1280px; max-height: 94vh; display: flex; flex-direction: column; padding: 16px; background: rgba(15, 20, 32, 0.98); border: 1px solid rgba(255, 255, 255, 0.12); box-shadow: 0 10px 40px rgba(0, 0, 0, 0.85);" onclick="event.stopPropagation()">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; border-bottom: 1px solid rgba(255,255,255,0.08); padding-bottom: 8px;">
                <div style="display: flex; align-items: center; gap: 8px; min-width: 0;">
                    <span style="font-size: 1.2rem;">&#128444;&#65039;</span>
                    <h3 id="screensModalTitle" style="margin: 0; font-size: 1rem; color: #f3f4f6; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 800px;">Contact Sheet Preview</h3>
                </div>
                <div style="display: flex; gap: 8px; align-items: center; flex-shrink: 0;">
                    <a id="screensModalDownloadBtn" href="#" target="_blank" download class="btn btn-secondary btn-xs" title="Open full resolution image in new tab">&#8599; Full Size</a>
                    <button type="button" class="btn btn-secondary btn-xs" onclick="closeScreensModal()">✕ Close</button>
                </div>
            </div>
            <div style="flex: 1; overflow: auto; text-align: center; background: rgba(0,0,0,0.5); border-radius: 6px; padding: 8px;">
                <img id="screensModalImg" src="" alt="Video Contact Sheet" style="max-width: 100%; height: auto; border-radius: 4px; box-shadow: 0 4px 20px rgba(0,0,0,0.6);" loading="lazy">
            </div>
        </div>
    </div>

    <div id="toast" class="toast"></div>

    <script>
        function openScreensModal(safeId, title, event) {{
            if (event) event.stopPropagation();
            const modal = document.getElementById('screensModal');
            const img = document.getElementById('screensModalImg');
            const titleEl = document.getElementById('screensModalTitle');
            const dlBtn = document.getElementById('screensModalDownloadBtn');
            if (!modal || !img) return;

            const fileObj = (allFiles || []).find(f => (f.safe_id === safeId));
            const sb = storyboardCache[safeId];
            let url = '';
            if (sb && sb.sprite_url) {{
                url = sb.sprite_url;
            }} else if (fileObj && fileObj.has_storyboard) {{
                const v = fileObj.sb_version || Date.now();
                url = `/api/storyboard/image?id=${{encodeURIComponent(safeId)}}&v=${{v}}`;
            }} else {{
                url = `/api/screens/image?id=${{encodeURIComponent(safeId)}}&v=${{Date.now()}}`;
            }}

            img.src = url;
            if (titleEl) titleEl.textContent = title ? '🖼️ ' + title : '100-Screen Contact Sheet Preview';
            if (dlBtn) dlBtn.href = url;
            modal.style.display = 'flex';
        }}

        function closeScreensModal(event) {{
            if (event && event.target && event.target.closest && event.target.closest('.modal')) return;
            const modal = document.getElementById('screensModal');
            if (modal) modal.style.display = 'none';
        }}

        let hiddenTagsSet = new Set(JSON.parse(localStorage.getItem('hfsync_hidden_tags') || '[]'));

        function isTagHidden(tag) {{
            return hiddenTagsSet.has(tag);
        }}

        function toggleHideTag(tag, event) {{
            if (event) event.stopPropagation();
            const cleanTag = String(tag).trim();
            if (hiddenTagsSet.has(cleanTag)) {{
                hiddenTagsSet.delete(cleanTag);
                flashCenterFeedback(`👁️ Unhidden: "${{cleanTag}}" is now visible on cards`);
            }} else {{
                hiddenTagsSet.add(cleanTag);
                flashCenterFeedback(`🙈 Hidden: "${{cleanTag}}" is now hidden on card faces`);
            }}
            try {{
                localStorage.setItem('hfsync_hidden_tags', JSON.stringify(Array.from(hiddenTagsSet)));
            }} catch(e) {{}}
            populateFilterDropdowns();
            refreshTreeDisplay();
        }}

        function unhideAllTags(event) {{
            if (event) event.stopPropagation();
            hiddenTagsSet.clear();
            try {{ localStorage.setItem('hfsync_hidden_tags', '[]'); }} catch(e) {{}}
            populateFilterDropdowns();
            refreshTreeDisplay();
            flashCenterFeedback('👁️ All tags are now visible on cards');
        }}

        function handleScreensBtnEnter(btn, event) {{
            if (event) event.stopPropagation();
            const wrap = btn.closest('.card-thumb-wrap, .video-thumb-card, .list-thumb-wrap');
            if (!wrap) return;
            const safeId = wrap.dataset.safeid;
            if (!safeId) return;

            wrap.dataset.screensHover = '1';
            clearInterval(thumbCycleInterval);
            clearTimeout(thumbManualScrubTimeout);

            const overlay = wrap.querySelector('.thumb-screens-overlay');
            if (!overlay) return;

            const fileObj = (allFiles || []).find(f => (f.safe_id === safeId));
            const sb = storyboardCache[safeId];
            let screensUrl = '';
            if (sb && sb.sprite_url) {{
                screensUrl = sb.sprite_url;
            }} else if (fileObj && fileObj.has_storyboard) {{
                const v = fileObj.sb_version || Date.now();
                screensUrl = `/api/storyboard/image?id=${{encodeURIComponent(safeId)}}&v=${{v}}`;
            }} else {{
                screensUrl = `/api/screens/image?id=${{encodeURIComponent(safeId)}}&v=${{Date.now()}}`;
            }}

            overlay.style.backgroundImage = `url('${{screensUrl}}')`;
            overlay.style.display = 'block';
        }}

        function handleScreensBtnLeave(btn, event) {{
            if (event) event.stopPropagation();
            const wrap = btn.closest('.card-thumb-wrap, .video-thumb-card, .list-thumb-wrap');
            if (!wrap) return;
            wrap.dataset.screensHover = '';
            const overlay = wrap.querySelector('.thumb-screens-overlay');
            if (overlay) {{
                overlay.style.display = 'none';
            }}
        }}

        function closeAllPopovers() {{
            document.querySelectorAll('.filter-popover').forEach(p => p.style.display = 'none');
        }}

        let hideCardTags = (localStorage.getItem('hfsync_hide_card_tags') === '1');

        function applyHideTagsState() {{
            const btn = document.getElementById('btnHideTags');
            const label = document.getElementById('tagsStateLabel');
            const container = document.getElementById('treeContainer');
            if (hideCardTags) {{
                if (container) container.classList.add('cards-hide-tags');
                if (label) label.textContent = 'Hidden';
                if (btn) {{
                    btn.classList.add('is-active-filter');
                    btn.title = 'Tags are hidden on cards (Click card arrow ▼ to see tags, or click here to show all)';
                }}
            }} else {{
                if (container) container.classList.remove('cards-hide-tags');
                if (label) label.textContent = 'Shown';
                if (btn) {{
                    btn.classList.remove('is-active-filter');
                    btn.title = 'Tags are visible on cards (Click here to hide)';
                }}
            }}
        }}

        function toggleHideTags() {{
            hideCardTags = !hideCardTags;
            try {{ localStorage.setItem('hfsync_hide_card_tags', hideCardTags ? '1' : '0'); }} catch (e) {{}}
            applyHideTagsState();
        }}

        async function promptAddTagToFolder(folderPath, event) {{
            if (event) event.stopPropagation();
            const tag = prompt(`Enter tag to add to all videos in "${{folderPath}}":`);
            if (!tag || !tag.trim()) return;
            const cleanTag = tag.trim();

            try {{
                const res = await fetch('/api/folder-tags', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ folder: folderPath, action: 'add', tag: cleanTag }})
                }});
                const data = await res.json();
                if (data.success) {{
                    const prefix = folderPath + '/';
                    allFiles.forEach(f => {{
                        if (f.key.startsWith(prefix) || f.key === folderPath) {{
                            if (!f.metadata) f.metadata = {{}};
                            if (!f.metadata.tags) f.metadata.tags = [];
                            if (!f.metadata.tags.includes(cleanTag)) f.metadata.tags.push(cleanTag);
                        }}
                    }});
                    populateFilterDropdowns();
                    refreshTreeDisplay();
                    flashCenterFeedback(`+ Added "${{cleanTag}}" to ${{data.updated_count}} videos`);
                }}
            }} catch (err) {{
                alert('Failed to add folder tag: ' + err.message);
            }}
        }}

        async function removeTagFromFolder(folderPath, tag, event) {{
            if (event) event.stopPropagation();
            if (!confirm(`Remove tag "${{tag}}" from all videos in folder "${{folderPath}}"?`)) return;

            try {{
                const res = await fetch('/api/folder-tags', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ folder: folderPath, action: 'remove', tag: tag }})
                }});
                const data = await res.json();
                if (data.success) {{
                    const prefix = folderPath + '/';
                    allFiles.forEach(f => {{
                        if (f.key.startsWith(prefix) || f.key === folderPath) {{
                            if (f.metadata && f.metadata.tags) {{
                                f.metadata.tags = f.metadata.tags.filter(t => t !== tag);
                            }}
                        }}
                    }});
                    populateFilterDropdowns();
                    refreshTreeDisplay();
                    flashCenterFeedback(`- Removed "${{tag}}" from ${{folderPath}}`);
                }}
            }} catch (err) {{
                alert('Failed to remove folder tag: ' + err.message);
            }}
        }}

        async function recheckCloudSync() {{
            const btn = document.getElementById('btnRecheckCloud');
            const origHtml = btn ? btn.innerHTML : '&#128260; Recheck Cloud';
            if (btn) {{
                btn.disabled = true;
                btn.innerHTML = '&#128260; Scanning Cloud...';
            }}
            try {{
                const res = await fetch('/api/recheck-cloud', {{ method: 'POST' }});
                if (!res.ok) throw new Error('HTTP ' + res.status);
                const data = await res.json();
                const recovered = data.recovered_count || 0;
                const skipped = data.skipped_count || 0;
                let msg = recovered > 0 ? `+ Found ${{recovered}} new video(s) from cloud!` : `Cloud check: Vault is up to date`;
                if (skipped > 0) msg += ` (${{skipped}} skipped)`;
                flashCenterFeedback(msg);
                await reloadFilesList();
            }} catch (err) {{
                flashCenterFeedback('Cloud check error: ' + err.message);
            }} finally {{
                if (btn) {{
                    btn.disabled = false;
                    btn.innerHTML = origHtml;
                }}
            }}
        }}

        function updateSystemStatusIndicators(serverOnline, watcherState, watcherMsg) {{
            const sPills = [document.getElementById('hdrServerStatus'), document.getElementById('stickyServerStatus')];
            const wPills = [document.getElementById('hdrWatcherStatus'), document.getElementById('stickyWatcherStatus')];

            sPills.forEach(p => {{
                if (!p) return;
                if (serverOnline) {{
                    p.className = 'status-pill status-online';
                    p.innerHTML = '<span class="pill-dot"></span> Port 8080 Active';
                    p.title = 'Streaming Proxy Server Connected';
                }} else {{
                    p.className = 'status-pill status-offline';
                    p.innerHTML = '<span class="pill-dot"></span> ⚠️ Server Disconnected!';
                    p.title = 'Streaming Proxy Server is offline or unreachable!';
                }}
            }});

            wPills.forEach(p => {{
                if (!p) return;
                if (watcherState === 'active') {{
                    p.className = 'status-pill status-active';
                    p.innerHTML = `<span class="pill-dot"></span> ⚡ ${{escapeHtml(watcherMsg || 'Syncing...')}}`;
                    p.title = 'Watcher Service is actively syncing files to vault';
                }} else if (watcherState === 'attention') {{
                    p.className = 'status-pill status-attention';
                    p.innerHTML = `<span class="pill-dot"></span> ⚠️ ${{escapeHtml(watcherMsg || 'Attention')}}`;
                    p.title = watcherMsg || 'Watcher alert';
                }} else {{
                    p.className = 'status-pill status-idle';
                    p.innerHTML = '<span class="pill-dot"></span> Watcher: Idle';
                    p.title = 'Watcher Service is running and waiting for videos';
                }}
            }});
        }}

        let allFiles = {files_json_str};
        let skippedFiles = {skipped_json_str};
        let primaryAccount = "{primary_user}";
        let deleteTarget = {{ type: null, key: null, folder: null, count: 0 }};
        let editingKey = null;

        const storyboardCache = {{}};
        function registerStoryboard(safeId, key, version) {{
            if (!safeId) return;
            const vTag = version || Date.now();
            const entry = {{
                available: true,
                safe_id: safeId,
                key: key || '',
                cols: 10,
                rows: 10,
                thumb_width: 384,
                thumb_height: 216,
                total_frames: 100,
                vTag: vTag,
                sprite_url: `/api/storyboard/image?id=${{safeId}}&v=${{vTag}}`
            }};
            storyboardCache[safeId] = entry;
            if (key) storyboardCache[key] = entry;
        }}

        function syncStoryboardCache(filesList) {{
            (filesList || []).forEach(f => {{
                if ((f.has_storyboard || f.has_screens) && f.safe_id) {{
                    const v = f.sb_version || f.uploaded_at || Date.now();
                    registerStoryboard(f.safe_id, f.key, v);
                }}
            }});
        }}
        syncStoryboardCache(allFiles);

        function buildTree(files) {{
            const root = {{ name: "root", type: "folder", children: {{}} }};

            files.forEach(f => {{
                const parts = f.key.split('/');
                let curr = root;

                for (let i = 0; i < parts.length; i++) {{
                    const part = parts[i];
                    const isLast = (i === parts.length - 1);

                    if (isLast) {{
                        curr.children[part] = {{
                            name: part,
                            type: "file",
                            data: f
                        }};
                    }} else {{
                        if (!curr.children[part]) {{
                            curr.children[part] = {{
                                name: part,
                                type: "folder",
                                children: {{}}
                            }};
                        }}
                        curr = curr.children[part];
                    }}
                }}
            }});

            return root;
        }}

        let currentViewMode = localStorage.getItem('hfsync_view_mode') || 'card';

        function updateViewToggleButtons() {{
            const btnCard = document.getElementById('btnViewCard');
            const btnList = document.getElementById('btnViewList');
            if (btnCard) btnCard.classList.toggle('active', currentViewMode === 'card');
            if (btnList) btnList.classList.toggle('active', currentViewMode === 'list');
        }}

        function setViewMode(mode) {{
            if (currentViewMode === mode) return;
            currentViewMode = mode;
            try {{
                localStorage.setItem('hfsync_view_mode', mode);
            }} catch (e) {{}}
            updateViewToggleButtons();
            refreshTreeDisplay();
        }}

        function toggleCardActions(btn, event) {{
            if (event) event.stopPropagation();
            const card = btn.closest('.video-card, .file-row');
            if (!card) return;
            const actionsBar = card.querySelector('.video-card-actions, .file-row-actions');
            const arrowBtn = card.querySelector('.card-action-arrow-btn') || btn;
            if (!actionsBar) return;

            const isOpen = actionsBar.classList.contains('is-open');
            if (isOpen) {{
                actionsBar.classList.remove('is-open');
                card.classList.remove('is-expanded');
                arrowBtn.classList.remove('is-active');
            }} else {{
                actionsBar.classList.add('is-open');
                card.classList.add('is-expanded');
                arrowBtn.classList.add('is-active');
            }}
        }}

        function renderCard(f, nodeName) {{
            const meta = f.metadata || {{}};
            const title = meta.title || nodeName;
            const genres = meta.genres || [];
            const tags = meta.tags || [];

            let badgesHtml = '';
            if (meta.year) badgesHtml += `<span class="meta-pill year-pill">&#128197; ${{escapeHtml(meta.year)}}</span>`;
            if (meta.rating) badgesHtml += `<span class="meta-pill rating-pill">&#11088; ${{escapeHtml(meta.rating)}}</span>`;
            genres.forEach(g => {{
                badgesHtml += `<span class="meta-pill genre-pill" onclick="event.stopPropagation(); addQuickFilter('genre', '${{escapeHtml(g)}}')" title="Category / Folder: ${{escapeHtml(g)}} (Click to filter)"><span class="cat-pill-prefix">&#128193; CAT:</span><span class="cat-pill-name">${{escapeHtml(g)}}</span></span>`;
            }});
            tags.forEach(t => {{
                const tl = String(t).trim().toLowerCase();
                let cls = 'tag-pill';
                let icon = '&#127991;';
                if (tl === '4k' || tl === '2160p' || tl === 'uhd') {{ cls = 'res-4k-pill'; icon = '&#10024;'; }}
                else if (tl === '1440p' || tl === '2k' || tl === 'qhd') {{ cls = 'res-1080p-pill'; icon = '&#128250;'; }}
                else if (tl === '1080p' || tl === 'full hd' || tl === 'fhd') {{ cls = 'res-1080p-pill'; icon = '&#128250;'; }}
                else if (tl === '720p' || tl === 'hd ready' || tl === 'hd') {{ cls = 'res-720p-pill'; icon = '&#128250;'; }}
                else if (tl === '480p' || tl === 'sd') {{ cls = 'res-sd-pill'; icon = '&#128249;'; }}
                else if (tl === 'hdr' || tl === 'hdr10' || tl === 'dolby vision' || tl === 'hlg') {{ cls = 'hdr-pill'; icon = '&#127775;'; }}
                else if (tl === '5.1' || tl === '5.1 surround' || tl === '7.1' || tl === '7.1 surround' || tl === 'atmos') {{ cls = 'surround-pill'; icon = '&#128266;'; }}
                else if (['h.264', 'h.265', 'av1', 'vp9', 'hevc', 'mpeg-4'].includes(tl)) {{ cls = 'codec-pill'; icon = '&#127916;'; }}
                else if (tl.includes('< 1m') || tl.includes('1-5m') || tl === 'micro clip' || tl === 'short clip') {{ cls = 'dur-short-pill'; icon = '&#9201;'; }}
                else if (tl.includes('5-15m') || tl.includes('15-30m') || tl === 'scene' || tl === 'short form') {{ cls = 'dur-mid-pill'; icon = '&#9201;'; }}
                else if (tl.includes('30-60m') || tl === 'mid-length' || tl === 'episode') {{ cls = 'dur-mid-pill'; icon = '&#128250;'; }}
                else if (tl.includes('1-2h') || tl.includes('2-3h') || tl === 'feature film' || tl === 'extended feature') {{ cls = 'dur-long-pill'; icon = '&#127916;'; }}
                else if (tl.includes('3h+') || tl.includes('4h+') || tl.includes('epic') || tl.includes('marathon')) {{ cls = 'dur-epic-pill'; icon = '&#128293;'; }}
                else if (tl === 'cutscene' || tl === 'cinematic') {{ cls = 'cutscene-pill'; icon = '&#127918;'; }}
                else if (tl === 'trailer' || tl === 'teaser') {{ cls = 'trailer-pill'; icon = '&#127916;'; }}
                else if (tl === 'gameplay') {{ cls = 'gameplay-pill'; icon = '&#128377;'; }}
                else if (tl === 'music video') {{ cls = 'music-pill'; icon = '&#127925;'; }}
                else if (tl.includes('vertical') || tl.includes('mobile')) {{ cls = 'vertical-pill'; icon = '&#128241;'; }}
                const isHidden = hiddenTagsSet.has(t);
                badgesHtml += `
                <span class="meta-pill ${{cls}} ${{isHidden ? 'is-hidden-tag' : ''}}" title="${{isHidden ? `Hidden tag: ${{escapeHtml(t)}} (Click 👁️ to unhide)` : `Tag: ${{escapeHtml(t)}} (Click to filter, or click 🙈 to hide)`}}">
                    <span class="pill-label-txt" onclick="event.stopPropagation(); addQuickFilter('tag', '${{escapeHtml(t)}}')">${{icon}} ${{escapeHtml(t)}}</span>
                    <span class="pill-hide-trigger" onclick="toggleHideTag('${{escapeHtml(t)}}', event)" title="${{isHidden ? 'Unhide this tag on all cards' : 'Hide this tag across all cards'}}">${{isHidden ? '&#128065;&#65039;' : '&#128584;'}}</span>
                </span>`;
            }});

            const cleanNode = nodeName.replace(/\.[^/.]+$/, '').trim().toLowerCase();
            const cleanTitle = title.trim().toLowerCase();
            const showSubtitle = (cleanNode !== cleanTitle && nodeName.trim().toLowerCase() !== cleanTitle);
            const subtitleHtml = showSubtitle ? `<div class="video-card-sub" title="${{escapeHtml(nodeName)}}">${{escapeHtml(nodeName)}}</div>` : '';

            const ov = (meta.overview || "").trim();
            const showOv = (ov && !ov.startsWith('Encrypted vault stream:') && ov.toLowerCase() !== cleanTitle && ov.toLowerCase() !== cleanNode);
            const overviewHtml = showOv ? `<div class="card-overview-snippet" title="${{escapeHtml(ov)}}">${{escapeHtml(ov)}}</div>` : '';

            const accountOwner = f.account || primaryAccount;
            const isPrimary = (accountOwner === primaryAccount);
            const accountBadge = `<span class="account-pill ${{isPrimary ? 'is-primary' : ''}}" onclick="event.stopPropagation(); addQuickFilter('account', '${{escapeHtml(accountOwner)}}')" title="Click to filter by account: ${{escapeHtml(accountOwner)}}">${{isPrimary ? '&#128081;' : '&#128100;'}} ${{escapeHtml(accountOwner)}}</span>`;

            const aliases = f.aliases || [];
            const otherAliases = aliases.filter(a => {{
                if (!a) return false;
                const sa = String(a).trim().toLowerCase();
                const saNoExt = sa.replace(/\.[^/.]+$/, '');
                return sa !== nodeName.toLowerCase() && saNoExt !== cleanNode && sa !== cleanTitle && saNoExt !== cleanTitle;
            }});
            let aliasBadge = '';
            if (otherAliases.length > 0) {{
                aliasBadge = `<span class="alias-pill" onclick="event.stopPropagation(); promptEditMetadata('${{escapeHtml(f.key)}}')" style="cursor: pointer;" title="Associated file names: ${{escapeHtml(aliases.join(', '))}}. Click to edit.">&#128206; Also: ${{escapeHtml(otherAliases.slice(0, 1).join(', '))}}${{otherAliases.length > 1 ? ` (+${{otherAliases.length - 1}})` : ''}}</span>`;
            }}

            const subs = f.subtitles || [];
            let subBadge = '';
            if (subs.length > 0) {{
                const subLangs = subs.map(s => (s.lang || 'cc').toUpperCase()).join(', ');
                subBadge = `<span class="meta-pill sub-pill" title="Subtitles available: ${{escapeHtml(subLangs)}}">&#128172; ${{escapeHtml(subLangs)}}</span>`;
            }}

            const safeId = f.safe_id || '';
            const hasStoryboard = !!f.has_storyboard;
            const hasScreens = !!f.has_screens;
            const canViewScreens = hasScreens || hasStoryboard;
            let thumbBgStyle = '';
            let fallbackHtml = '';
            if ((hasStoryboard || hasScreens) && safeId) {{
                const vTag = f.sb_version || (f.uploaded_at ? encodeURIComponent(f.uploaded_at) : '1');
                thumbBgStyle = "background-image: url('/api/screens/thumb?id=" + safeId + "&v=" + vTag + "'); background-size: cover; background-position: center;";
            }} else {{
                fallbackHtml = '<div class="thumb-fallback"><span class="thumb-fallback-icon">&#127916;</span><span class="thumb-fallback-txt">PREVIEW</span></div>';
            }}

            const durationStr = f.duration_str || (f.duration_sec ? formatTime(f.duration_sec) : '');
            const durationHtml = durationStr ? `<div class="card-duration-badge">${{escapeHtml(durationStr)}}</div>` : '';

            const savedPos = getSavedVideoPosition(safeId);
            let resumeBarHtml = '';
            let resumePillHtml = '';
            if (savedPos && savedPos.time > 5 && savedPos.duration > 0) {{
                const pct = Math.min(100, Math.round((savedPos.time / savedPos.duration) * 100));
                resumeBarHtml = `<div class="card-resume-bar" style="width: ${{pct}}%;"></div>`;
                resumePillHtml = `<div class="card-resume-badge" title="Resume at ${{formatTime(savedPos.time)}} / ${{formatTime(savedPos.duration)}}">⏱️ ${{formatTime(savedPos.time)}}</div>`;
            }}
            const audioSizeStr = f.audio_size_str || 'Audio';

            return `
            <div class="video-card" data-key="${{escapeHtml(f.key)}}">
                <div class="video-card-thumb video-thumb-card" 
                     data-key="${{escapeHtml(f.key)}}" 
                     data-safeid="${{safeId}}"
                     data-duration="${{f.duration_sec || 0}}"
                     onclick="handleCardThumbClick(this, event)"
                     onmouseenter="handleThumbEnter(this)"
                     onmouseleave="handleThumbLeave(this)"
                     onmousemove="handleThumbMove(this, event)"
                     title="Click to play from scrub position">
                    <div class="thumb-canvas" id="thumb-canvas-${{safeId}}" style="${{thumbBgStyle}}">
                        ${{fallbackHtml}}
                    </div>
                    <div class="thumb-screens-overlay" id="screens-overlay-${{safeId}}"></div>
                    <div class="thumb-scrub-bar">
                        <div class="thumb-scrub-fill"></div>
                    </div>
                    <div class="thumb-overlay-play">
                        <svg viewBox="0 0 24 24" fill="currentColor" width="22" height="22"><path d="M8 5v14l11-7z"/></svg>
                    </div>
                    ${{canViewScreens ? `<button type="button" class="thumb-screens-btn" onmouseenter="handleScreensBtnEnter(this, event)" onmousemove="event.stopPropagation()" onmouseleave="handleScreensBtnLeave(this, event)" onclick="openScreensModal('${{safeId}}', '${{escapeHtml(title)}}', event)" title="Hover to view screens on card &bull; Click to open full sheet">&#128444;&#65039; Screens</button>` : ''}}
                    <div class="card-size-badge" title="Video size: ${{escapeHtml(f.size_str)}}, Audio: ${{escapeHtml(audioSizeStr)}}">
                        <span class="badge-vsize">💾 ${{escapeHtml(f.size_str)}}</span>
                        <span class="badge-asize"> • 🎵 ${{escapeHtml(audioSizeStr)}}</span>
                    </div>
                    ${{resumePillHtml}}
                    ${{durationHtml}}
                    ${{resumeBarHtml}}
                </div>
                
                <div class="video-card-body">
                    <div class="video-card-title-row">
                        <div class="video-card-title" onclick="playVideo('/stream/${{escapeHtml(f.key)}}', '${{escapeHtml(title)}}', '${{escapeHtml(f.blob_path || '')}}')" title="Click to play: ${{escapeHtml(title)}}">
                            ${{escapeHtml(title)}}
                        </div>
                        <button type="button" class="card-action-arrow-btn" onclick="toggleCardActions(this, event)" title="Toggle actions (Direct, HF, Edit, Delete)">
                            <span class="card-arrow-icon">&#9662;</span>
                        </button>
                    </div>
                    ${{subtitleHtml}}
                    ${{overviewHtml}}
                    <div class="video-card-badges">
                        ${{accountBadge}}
                        ${{badgesHtml}}
                        ${{aliasBadge}}
                    </div>
                </div>

                <div class="video-card-actions">
                    <div class="card-action-group-left">
                        <a href="/stream/${{escapeHtml(f.key)}}" target="_blank" class="card-icon-btn btn-card-link" title="Direct Stream URL">&#128279;</a>
                        ${{canViewScreens ? `<button type="button" class="card-icon-btn" onclick="openScreensModal('${{safeId}}', '${{escapeHtml(title)}}', event)" title="View High-Res Contact Sheet Screenshots">&#128444;&#65039;</button>` : ''}}
                        <a href="/stream/audio/${{escapeHtml(f.key)}}" download="${{escapeHtml(title)}}.m4a" target="_blank" class="card-icon-btn btn-card-audio" title="Download / Stream Lightweight Audio (.m4a)">&#127925;</a>
                        <a href="${{escapeHtml(f.hf_url)}}" target="_blank" class="card-icon-btn btn-card-hf" title="Open dataset on Hugging Face">&#129303;</a>
                    </div>
                    <div class="card-action-group-right">
                        <button onclick="promptEditMetadata('${{escapeHtml(f.key)}}')" class="card-icon-btn btn-card-edit" title="Edit Tags, Categories &amp; Metadata">&#127991;</button>
                        <button onclick="promptDeleteFile('${{escapeHtml(f.key)}}')" class="card-icon-btn btn-card-delete" title="Delete Video Permanently">&#128465;</button>
                    </div>
                </div>
            </div>`;
        }}

        function renderListRow(f, nodeName) {{
            const meta = f.metadata || {{}};
            const title = meta.title || nodeName;
            const genres = meta.genres || [];
            const tags = meta.tags || [];

            let badgesHtml = '';
            if (meta.year) badgesHtml += `<span class="meta-pill year-pill">&#128197; ${{escapeHtml(meta.year)}}</span>`;
            if (meta.rating) badgesHtml += `<span class="meta-pill rating-pill">&#11088; ${{escapeHtml(meta.rating)}}</span>`;
            genres.forEach(g => {{
                badgesHtml += `<span class="meta-pill genre-pill" onclick="event.stopPropagation(); addQuickFilter('genre', '${{escapeHtml(g)}}')" title="Category / Folder: ${{escapeHtml(g)}} (Click to filter)"><span class="cat-pill-prefix">&#128193; CAT:</span><span class="cat-pill-name">${{escapeHtml(g)}}</span></span>`;
            }});
            tags.forEach(t => {{
                const tl = String(t).trim().toLowerCase();
                let cls = 'tag-pill';
                let icon = '&#127991;';
                if (tl === '4k' || tl === '2160p' || tl === 'uhd') {{ cls = 'res-4k-pill'; icon = '&#10024;'; }}
                else if (tl === '1440p' || tl === '2k' || tl === 'qhd') {{ cls = 'res-1080p-pill'; icon = '&#128250;'; }}
                else if (tl === '1080p' || tl === 'full hd' || tl === 'fhd') {{ cls = 'res-1080p-pill'; icon = '&#128250;'; }}
                else if (tl === '720p' || tl === 'hd ready' || tl === 'hd') {{ cls = 'res-720p-pill'; icon = '&#128250;'; }}
                else if (tl === '480p' || tl === 'sd') {{ cls = 'res-sd-pill'; icon = '&#128249;'; }}
                else if (tl === 'hdr' || tl === 'hdr10' || tl === 'dolby vision' || tl === 'hlg') {{ cls = 'hdr-pill'; icon = '&#127775;'; }}
                else if (tl === '5.1' || tl === '5.1 surround' || tl === '7.1' || tl === '7.1 surround' || tl === 'atmos') {{ cls = 'surround-pill'; icon = '&#128266;'; }}
                else if (['h.264', 'h.265', 'av1', 'vp9', 'hevc', 'mpeg-4'].includes(tl)) {{ cls = 'codec-pill'; icon = '&#127916;'; }}
                else if (tl.includes('< 1m') || tl.includes('1-5m') || tl === 'micro clip' || tl === 'short clip') {{ cls = 'dur-short-pill'; icon = '&#9201;'; }}
                else if (tl.includes('5-15m') || tl.includes('15-30m') || tl === 'scene' || tl === 'short form') {{ cls = 'dur-mid-pill'; icon = '&#9201;'; }}
                else if (tl.includes('30-60m') || tl === 'mid-length' || tl === 'episode') {{ cls = 'dur-mid-pill'; icon = '&#128250;'; }}
                else if (tl.includes('1-2h') || tl.includes('2-3h') || tl === 'feature film' || tl === 'extended feature') {{ cls = 'dur-long-pill'; icon = '&#127916;'; }}
                else if (tl.includes('3h+') || tl.includes('4h+') || tl.includes('epic') || tl.includes('marathon')) {{ cls = 'dur-epic-pill'; icon = '&#128293;'; }}
                else if (tl === 'cutscene' || tl === 'cinematic') {{ cls = 'cutscene-pill'; icon = '&#127918;'; }}
                else if (tl === 'trailer' || tl === 'teaser') {{ cls = 'trailer-pill'; icon = '&#127916;'; }}
                else if (tl === 'gameplay') {{ cls = 'gameplay-pill'; icon = '&#128377;'; }}
                else if (tl === 'music video') {{ cls = 'music-pill'; icon = '&#127925;'; }}
                else if (tl.includes('vertical') || tl.includes('mobile')) {{ cls = 'vertical-pill'; icon = '&#128241;'; }}
                const isHidden = hiddenTagsSet.has(t);
                badgesHtml += `
                <span class="meta-pill ${{cls}} ${{isHidden ? 'is-hidden-tag' : ''}}" title="${{isHidden ? `Hidden tag: ${{escapeHtml(t)}} (Click 👁️ to unhide)` : `Tag: ${{escapeHtml(t)}} (Click to filter, or click 🙈 to hide)`}}">
                    <span class="pill-label-txt" onclick="event.stopPropagation(); addQuickFilter('tag', '${{escapeHtml(t)}}')">${{icon}} ${{escapeHtml(t)}}</span>
                    <span class="pill-hide-trigger" onclick="toggleHideTag('${{escapeHtml(t)}}', event)" title="${{isHidden ? 'Unhide this tag on all cards' : 'Hide this tag across all cards'}}">${{isHidden ? '&#128065;&#65039;' : '&#128584;'}}</span>
                </span>`;
            }});

            const cleanNode = nodeName.replace(/\.[^/.]+$/, '').trim().toLowerCase();
            const cleanTitle = title.trim().toLowerCase();
            const showSubtitle = (cleanNode !== cleanTitle && nodeName.trim().toLowerCase() !== cleanTitle);
            const subtitleHtml = showSubtitle ? `<span style="font-size: 0.75rem; color: #6b7280; font-family: monospace;">(${{escapeHtml(nodeName)}})</span>` : '';

            const ov = (meta.overview || "").trim();
            const showOv = (ov && !ov.startsWith('Encrypted vault stream:') && ov.toLowerCase() !== cleanTitle && ov.toLowerCase() !== cleanNode);
            const overviewHtml = showOv ? `<div class="file-overview-snippet" title="${{escapeHtml(ov)}}">${{escapeHtml(ov)}}</div>` : '';

            const accountOwner = f.account || primaryAccount;
            const isPrimary = (accountOwner === primaryAccount);
            const accountBadge = `<span class="account-pill ${{isPrimary ? 'is-primary' : ''}}" onclick="event.stopPropagation(); addQuickFilter('account', '${{escapeHtml(accountOwner)}}')" title="Click to filter by account: ${{escapeHtml(accountOwner)}}">${{isPrimary ? '&#128081;' : '&#128100;'}} ${{escapeHtml(accountOwner)}}</span>`;

            const aliases = f.aliases || [];
            const otherAliases = aliases.filter(a => {{
                if (!a) return false;
                const sa = String(a).trim().toLowerCase();
                const saNoExt = sa.replace(/\.[^/.]+$/, '');
                return sa !== nodeName.toLowerCase() && saNoExt !== cleanNode && sa !== cleanTitle && saNoExt !== cleanTitle;
            }});
            let aliasBadge = '';
            if (otherAliases.length > 0) {{
                aliasBadge = `<span class="alias-pill" onclick="event.stopPropagation(); promptEditMetadata('${{escapeHtml(f.key)}}')" style="cursor: pointer;" title="Associated file names: ${{escapeHtml(aliases.join(', '))}}. Click to edit or remove filenames.">&#128206; Also: ${{escapeHtml(otherAliases.slice(0, 2).join(', '))}}${{otherAliases.length > 2 ? ` (+${{otherAliases.length - 2}})` : ''}} <span style="opacity: 0.7; font-size: 0.65rem;">&#9998;</span></span>`;
            }}

            const subs = f.subtitles || [];
            let subBadge = '';
            if (subs.length > 0) {{
                const subLangs = subs.map(s => (s.lang || 'cc').toUpperCase()).join(', ');
                subBadge = `<span class="meta-pill sub-pill" title="Subtitles available: ${{escapeHtml(subLangs)}}">&#128172; ${{escapeHtml(subLangs)}}</span>`;
            }}

            const safeId = f.safe_id || '';
            const hasStoryboard = !!f.has_storyboard;
            const hasScreens = !!f.has_screens;
            const canViewScreens = hasScreens || hasStoryboard;
            let thumbBgStyle = '';
            let fallbackHtml = '';
            if ((hasStoryboard || hasScreens) && safeId) {{
                const vTag = f.sb_version || (f.uploaded_at ? encodeURIComponent(f.uploaded_at) : '1');
                thumbBgStyle = "background-image: url('/api/screens/thumb?id=" + safeId + "&v=" + vTag + "'); background-size: cover; background-position: center;";
            }} else {{
                fallbackHtml = '<div class="thumb-fallback"><span class="thumb-fallback-icon">&#127916;</span><span class="thumb-fallback-txt">PREVIEW</span></div>';
            }}

            const durationStr = f.duration_str || (f.duration_sec ? formatTime(f.duration_sec) : '');
            const durationHtml = durationStr ? `<div class="card-duration-badge list-duration-badge">${{escapeHtml(durationStr)}}</div>` : '';

            const savedPos = getSavedVideoPosition(safeId);
            let resumeBarHtml = '';
            let resumePillHtml = '';
            if (savedPos && savedPos.time > 5 && savedPos.duration > 0) {{
                const pct = Math.min(100, Math.round((savedPos.time / savedPos.duration) * 100));
                resumeBarHtml = `<div class="card-resume-bar" style="width: ${{pct}}%;"></div>`;
                resumePillHtml = `<div class="card-resume-badge" title="Resume at ${{formatTime(savedPos.time)}} / ${{formatTime(savedPos.duration)}}">⏱️ ${{formatTime(savedPos.time)}}</div>`;
            }}
            const audioSizeStr = f.audio_size_str || 'Audio';

            return `
            <div class="file-row" data-key="${{escapeHtml(f.key)}}">
                <div class="video-thumb-card" 
                     data-key="${{escapeHtml(f.key)}}" 
                     data-safeid="${{safeId}}"
                     data-duration="${{f.duration_sec || 0}}"
                     onclick="handleCardThumbClick(this, event)"
                     onmouseenter="handleThumbEnter(this)"
                     onmouseleave="handleThumbLeave(this)"
                     onmousemove="handleThumbMove(this, event)"
                     title="Click to play from scrub position">
                    <div class="thumb-canvas" id="thumb-canvas-${{safeId}}" style="${{thumbBgStyle}}">
                        ${{fallbackHtml}}
                    </div>
                    <div class="thumb-screens-overlay" id="screens-overlay-list-${{safeId}}"></div>
                    <div class="thumb-scrub-bar">
                        <div class="thumb-scrub-fill"></div>
                    </div>
                    <div class="thumb-overlay-play">
                        <svg viewBox="0 0 24 24" fill="currentColor" width="16" height="16"><path d="M8 5v14l11-7z"/></svg>
                    </div>
                    ${{canViewScreens ? `<button type="button" class="thumb-screens-btn" onmouseenter="handleScreensBtnEnter(this, event)" onmousemove="event.stopPropagation()" onmouseleave="handleScreensBtnLeave(this, event)" onclick="openScreensModal('${{safeId}}', '${{escapeHtml(title)}}', event)" title="Hover to view screens &bull; Click to open full sheet">&#128444;&#65039; Screens</button>` : ''}}
                    ${{resumePillHtml}}
                    ${{durationHtml}}
                    ${{resumeBarHtml}}
                </div>
                <div style="flex: 1; min-width: 0;">
                    <div style="display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap;">
                        <span class="file-name" style="cursor: pointer;" onclick="playVideo('/stream/${{escapeHtml(f.key)}}', '${{escapeHtml(title)}}', '${{escapeHtml(f.blob_path || '')}}')" title="Click to play: ${{escapeHtml(title)}}">${{escapeHtml(title)}}</span>
                        ${{subtitleHtml}}
                    </div>
                    ${{overviewHtml}}
                    <div class="meta-badges">
                        ${{accountBadge}}
                        <span class="file-meta">💾 ${{f.size_str}} • 🎵 ${{audioSizeStr}}</span>
                        ${{badgesHtml}}
                        ${{aliasBadge}}
                        ${{subBadge}}
                    </div>
                </div>
                <div class="file-actions">
                    <a href="/stream/${{f.key}}" target="_blank" class="btn btn-link" title="Direct Stream URL">&#128279; Direct</a>
                    ${{canViewScreens ? `<button type="button" onclick="openScreensModal('${{safeId}}', '${{escapeHtml(title)}}', event)" class="btn btn-secondary btn-sm" title="View Full Contact Sheet Screenshots">&#128444;&#65039; Screens</button>` : ''}}
                    <a href="/stream/audio/${{f.key}}" download="${{escapeHtml(title)}}.m4a" target="_blank" class="btn btn-link btn-card-audio" title="Download / Stream Lightweight Audio (.m4a)">&#127925; Audio</a>
                    <a href="${{f.hf_url}}" target="_blank" class="btn btn-hf" title="Open file on Hugging Face">&#129303; View on HF</a>
                    <button onclick="promptEditMetadata('${{escapeHtml(f.key)}}')" class="btn btn-edit" title="Edit tags, categories, and Jellyfin metadata">&#127991; Edit Tags</button>
                    <button onclick="promptDeleteFile('${{escapeHtml(f.key)}}')" class="btn btn-delete" title="Delete video permanently">&#128465; Delete</button>
                </div>
            </div>`;
        }}

        function getFolderFiles(folderNode) {{
            let files = [];
            if (!folderNode) return files;
            if (folderNode.type === "file") return [folderNode.data];
            if (folderNode.children) {{
                Object.values(folderNode.children).forEach(child => {{
                    files = files.concat(getFolderFiles(child));
                }});
            }}
            return files;
        }}

        function renderTree(node, depth = 0, currentPath = "") {{
            if (node.type === "file") {{
                return currentViewMode === 'card' ? renderCard(node.data, node.name) : renderListRow(node.data, node.name);
            }}

            let totalDescendantFiles = countFiles(node);
            if (totalDescendantFiles === 0 && depth > 0) return "";

            let html = "";
            const folderPath = currentPath ? `${{currentPath}}/${{node.name}}` : (depth > 0 ? node.name : "");
            const isOpen = openedTreesQueue.includes(folderPath);

            if (depth > 0) {{
                const descendantFiles = getFolderFiles(node);
                let commonTags = [];
                if (descendantFiles.length > 0) {{
                    const firstTags = (descendantFiles[0].metadata?.tags || []);
                    commonTags = firstTags.filter(t => {{
                        return descendantFiles.every(f => (f.metadata?.tags || []).includes(t));
                    }});
                }}

                const treeTagsHtml = `
                <div class="folder-tags-cluster">
                    ${{commonTags.map(t => `
                        <span class="tree-tag-chip" title="Tag shared by all ${{descendantFiles.length}} videos in this folder">
                            &#127991; ${{escapeHtml(t)}}
                            <span class="tree-tag-edit" onclick="promptRenameTag('${{escapeHtml(t)}}', event)" title="Rename tag '${{escapeHtml(t)}}' across all videos">&#9998;</span>
                            <span class="tree-tag-remove" onclick="removeTagFromFolder('${{escapeHtml(folderPath)}}', '${{escapeHtml(t)}}', event)" title="Remove from this folder">&times;</span>
                        </span>
                    `).join('')}}
                    <button type="button" class="btn-tree-add-tag" onclick="promptAddTagToFolder('${{escapeHtml(folderPath)}}', event)" title="Add a tag to all videos in this folder in bulk">+ Tag</button>
                </div>`;

                html += `
                <div class="folder-group" data-folder="${{escapeHtml(folderPath)}}">
                    <div class="folder-header">
                        <div class="folder-left" onclick="toggleFolder(this.parentElement)">
                            <span class="folder-arrow ${{isOpen ? 'open' : ''}}">&#9654;</span>
                            <span class="folder-icon">&#128193;</span>
                            <span>${{escapeHtml(node.name)}}</span>
                            <span class="folder-count">${{totalDescendantFiles}} video(s)</span>
                            ${{treeTagsHtml}}
                        </div>
                        <div class="folder-actions">
                            <button onclick="event.stopPropagation(); promptDeleteFolder('${{escapeHtml(folderPath)}}', ${{totalDescendantFiles}})" class="btn btn-delete btn-sm" title="Delete entire folder and all contained videos">&#128465; Delete Folder</button>
                        </div>
                    </div>
                    <div class="folder-children ${{isOpen ? '' : 'collapsed'}}">`;
            }}

            const folderChildren = [];
            const fileChildren = [];

            Object.keys(node.children).sort((a, b) => a.localeCompare(b)).forEach(k => {{
                const child = node.children[k];
                if (child.type === "folder") {{
                    folderChildren.push(child);
                }} else {{
                    fileChildren.push(child);
                }}
            }});

            // Render sub-folders first
            folderChildren.forEach(child => {{
                html += renderTree(child, depth + 1, folderPath);
            }});

            // Render direct video files inside this folder/root
            if (fileChildren.length > 0) {{
                if (currentViewMode === 'card') {{
                    html += `<div class="folder-cards-grid">`;
                    fileChildren.forEach(child => {{
                        html += renderCard(child.data, child.name);
                    }});
                    html += `</div>`;
                }} else {{
                    html += `<div class="folder-list-rows">`;
                    fileChildren.forEach(child => {{
                        html += renderListRow(child.data, child.name);
                    }});
                    html += `</div>`;
                }}
            }}

            if (depth > 0) {{
                html += `</div></div>`;
            }}

            return html;
        }}

        function countFiles(folderNode) {{
            let count = 0;
            for (const k in folderNode.children) {{
                const child = folderNode.children[k];
                if (child.type === "file") count++;
                else count += countFiles(child);
            }}
            return count;
        }}

        let selectedTags = new Set();
        let selectedGenres = new Set();
        let selectedAccounts = new Set();

        function toggleFilterPopover(popoverId, event) {{
            if (event) event.stopPropagation();
            const popover = document.getElementById(popoverId);
            if (!popover) return;
            const isOpen = popover.style.display === 'block';
            document.querySelectorAll('.filter-popover').forEach(p => p.style.display = 'none');
            if (!isOpen) {{
                popover.style.display = 'block';
                const search = popover.querySelector('.popover-search');
                if (search) {{
                    search.value = '';
                    search.focus();
                    filterPopoverList(popoverId, '');
                }}
            }}
        }}

        function filterPopoverList(popoverId, query) {{
            const popover = document.getElementById(popoverId);
            if (!popover) return;
            const q = (query || '').toLowerCase().trim();
            popover.querySelectorAll('.popover-item').forEach(item => {{
                const text = item.getAttribute('data-val') || '';
                item.style.display = (!q || text.toLowerCase().includes(q)) ? 'flex' : 'none';
            }});
        }}

        function addQuickFilter(type, val) {{
            if (!val) return;
            const clean = String(val).trim();
            if (type === 'genre') {{
                selectedGenres.add(clean);
            }} else if (type === 'tag') {{
                selectedTags.add(clean);
            }} else if (type === 'account') {{
                selectedAccounts.add(clean);
            }}
            updateFilterBadges();
            populateFilterDropdowns();
            filterTree();
            showToast(`Filter added: ${{clean}}`);
        }}

        function toggleFilterSelection(type, val) {{
            let setObj = (type === 'tag') ? selectedTags : ((type === 'genre') ? selectedGenres : selectedAccounts);
            if (setObj.has(val)) {{
                setObj.delete(val);
            }} else {{
                setObj.add(val);
            }}
            updateFilterBadges();
            filterTree();
        }}

        function selectAllFilter(type) {{
            if (type === 'tag') {{
                allFiles.forEach(f => {{
                    (f.metadata?.tags || []).forEach(t => {{ if (t) selectedTags.add(String(t).trim()); }});
                }});
            }} else if (type === 'genre') {{
                allFiles.forEach(f => {{
                    (f.metadata?.genres || []).forEach(g => {{ if (g) selectedGenres.add(String(g).trim()); }});
                }});
            }} else if (type === 'account') {{
                allFiles.forEach(f => {{
                    selectedAccounts.add(f.account || primaryAccount);
                }});
            }}
            updateFilterBadges();
            populateFilterDropdowns();
            filterTree();
        }}

        function clearFilter(type) {{
            if (type === 'tag') selectedTags.clear();
            else if (type === 'genre') selectedGenres.clear();
            else if (type === 'account') selectedAccounts.clear();
            updateFilterBadges();
            populateFilterDropdowns();
            filterTree();
        }}

        function resetAllFilters() {{
            const searchInput = document.getElementById('searchInput');
            if (searchInput) searchInput.value = '';
            selectedTags.clear();
            selectedGenres.clear();
            selectedAccounts.clear();
            updateFilterBadges();
            populateFilterDropdowns();
            filterTree();
        }}

        function updateFilterBadges() {{
            const updatePill = (btnId, badgeId, setObj) => {{
                const btn = document.getElementById(btnId);
                const badge = document.getElementById(badgeId);
                if (!btn || !badge) return;
                if (setObj.size > 0) {{
                    btn.classList.add('is-active');
                    badge.style.display = 'inline-block';
                    badge.textContent = setObj.size;
                }} else {{
                    btn.classList.remove('is-active');
                    badge.style.display = 'none';
                    badge.textContent = '0';
                }}
            }};
            updatePill('tagPillBtn', 'tagCountBadge', selectedTags);
            updatePill('genrePillBtn', 'genreCountBadge', selectedGenres);
            updatePill('accountPillBtn', 'accountCountBadge', selectedAccounts);

            const q = document.getElementById('searchInput') ? document.getElementById('searchInput').value.trim() : '';
            const totalActive = selectedTags.size + selectedGenres.size + selectedAccounts.size;
            const resetBtn = document.getElementById('btnResetFilters');
            if (resetBtn) {{
                resetBtn.style.display = (q || totalActive > 0) ? 'inline-block' : 'none';
            }}

            renderActiveFilterChips();
        }}

        function renderActiveFilterChips(matchingCount) {{
            const bar = document.getElementById('activeFilterChipsBar');
            if (!bar) return;
            const totalActive = selectedTags.size + selectedGenres.size + selectedAccounts.size;
            const q = document.getElementById('searchInput') ? document.getElementById('searchInput').value.trim() : '';
            if (totalActive === 0 && !q) {{
                bar.style.display = 'none';
                bar.innerHTML = '';
                return;
            }}
            bar.style.display = 'flex';
            const mCount = (typeof matchingCount === 'number') ? matchingCount : (window.lastMatchingCount !== undefined ? window.lastMatchingCount : allFiles.length);
            let html = `<span class="filter-count-badge">🎯 ${{mCount}} of ${{allFiles.length}} matching</span><span style="font-size: 0.74rem; color: #9ca3af; margin-right: 4px;">Active Filters:</span>`;

            if (q) {{
                html += `<span class="filter-chip">&#128269; "${{escapeHtml(q)}}" <span class="filter-chip-remove" onclick="clearSearchInput()">&times;</span></span>`;
            }}
            selectedAccounts.forEach(acc => {{
                html += `<span class="filter-chip">&#128100; ${{escapeHtml(acc)}} <span class="filter-chip-remove" onclick="toggleFilterSelection('account', '${{escapeHtml(acc)}}')">&times;</span></span>`;
            }});
            selectedGenres.forEach(g => {{
                html += `<span class="filter-chip">&#128193; ${{escapeHtml(g)}} <span class="filter-chip-remove" onclick="toggleFilterSelection('genre', '${{escapeHtml(g)}}')">&times;</span></span>`;
            }});
            selectedTags.forEach(t => {{
                html += `<span class="filter-chip">&#127991; ${{escapeHtml(t)}} <span class="filter-chip-remove" onclick="toggleFilterSelection('tag', '${{escapeHtml(t)}}')">&times;</span></span>`;
            }});
            html += `<span class="clear-all-chip" onclick="resetAllFilters()">Clear All</span>`;
            bar.innerHTML = html;
        }}

        function clearSearchInput() {{
            const el = document.getElementById('searchInput');
            if (el) {{
                el.value = '';
                filterTree();
            }}
        }}

        function populateFilterDropdowns() {{
            const accountCounts = {{}};
            const genreCounts = {{}};
            const tagCounts = {{}};

            allFiles.forEach(f => {{
                const acc = f.account || primaryAccount;
                accountCounts[acc] = (accountCounts[acc] || 0) + 1;

                const meta = f.metadata || {{}};
                (meta.genres || []).forEach(g => {{
                    const strG = String(g).trim();
                    if (strG) genreCounts[strG] = (genreCounts[strG] || 0) + 1;
                }});
                (meta.tags || []).forEach(t => {{
                    const strT = String(t).trim();
                    if (strT) tagCounts[strT] = (tagCounts[strT] || 0) + 1;
                }});
            }});

            // Populate Tags
            const tagList = document.getElementById('tagPopoverList');
            if (tagList) {{
                tagList.innerHTML = Object.keys(tagCounts).sort((a, b) => a.localeCompare(b)).map(t => {{
                    const isChecked = selectedTags.has(t);
                    const isHidden = hiddenTagsSet.has(t);
                    return `
                    <div class="popover-item ${{isHidden ? 'is-tag-hidden' : ''}}" data-val="${{escapeHtml(t)}}">
                        <label class="popover-item-left">
                            <input type="checkbox" ${{isChecked ? 'checked' : ''}} onchange="toggleFilterSelection('tag', '${{escapeHtml(t)}}')">
                            <span style="${{isHidden ? 'color: #9ca3af; text-decoration: line-through;' : ''}}">${{escapeHtml(t)}}</span>
                            <span class="popover-item-count">(${{tagCounts[t]}})</span>
                        </label>
                        <div class="popover-item-actions" style="display: flex; gap: 4px; align-items: center;">
                            <button type="button" class="btn-tag-edit-mini" onclick="promptRenameTag('${{escapeHtml(t)}}', event)" title="Rename / edit tag '${{escapeHtml(t)}}' across all videos">&#9998;</button>
                            <button type="button" class="btn-tag-hide-toggle ${{isHidden ? 'is-hidden' : ''}}" onclick="toggleHideTag('${{escapeHtml(t)}}', event)" title="${{isHidden ? 'Tag is hidden on cards. Click to show on all cards' : 'Hide this tag across all video cards'}}">
                                ${{isHidden ? '&#128584; Hidden' : '&#128065;&#65039; Hide'}}
                            </button>
                        </div>
                    </div>`;
                }}).join('') || '<div style="color: #6b7280; font-size: 0.78rem; padding: 6px;">No tags found</div>';
            }}

            const hiddenBadge = document.getElementById('popoverHiddenTagsCount');
            if (hiddenBadge) {{
                if (hiddenTagsSet.size > 0) {{
                    hiddenBadge.style.display = 'inline-block';
                    hiddenBadge.textContent = `&#128584; ${{hiddenTagsSet.size}} Hidden (Unhide All)`;
                }} else {{
                    hiddenBadge.style.display = 'none';
                }}
            }}

            // Populate Categories
            const genreList = document.getElementById('genrePopoverList');
            if (genreList) {{
                genreList.innerHTML = Object.keys(genreCounts).sort((a, b) => a.localeCompare(b)).map(g => {{
                    const isChecked = selectedGenres.has(g);
                    return `
                    <label class="popover-item" data-val="${{escapeHtml(g)}}">
                        <input type="checkbox" ${{isChecked ? 'checked' : ''}} onchange="toggleFilterSelection('genre', '${{escapeHtml(g)}}')">
                        <span>${{escapeHtml(g)}}</span>
                        <span class="popover-item-count">(${{genreCounts[g]}})</span>
                    </label>`;
                }}).join('') || '<div style="color: #6b7280; font-size: 0.78rem; padding: 6px;">No categories found</div>';
            }}

            // Populate Accounts
            const accountList = document.getElementById('accountPopoverList');
            if (accountList) {{
                accountList.innerHTML = Object.keys(accountCounts).sort((a, b) => a.localeCompare(b)).map(acc => {{
                    const isChecked = selectedAccounts.has(acc);
                    const isPrimary = (acc === primaryAccount);
                    return `
                    <label class="popover-item" data-val="${{escapeHtml(acc)}}">
                        <input type="checkbox" ${{isChecked ? 'checked' : ''}} onchange="toggleFilterSelection('account', '${{escapeHtml(acc)}}')">
                        <span>${{isPrimary ? '&#128081; ' : '&#128100; '}}${{escapeHtml(acc)}}</span>
                        <span class="popover-item-count">(${{accountCounts[acc]}})</span>
                    </label>`;
                }}).join('') || '<div style="color: #6b7280; font-size: 0.78rem; padding: 6px;">No accounts found</div>';
            }}

            updateFilterBadges();
        }}

        /* Tree Open State, FIFO Queue (Max 3), and Position Restoration */
        const MAX_OPEN_TREES = 3;
        let openedTreesQueue = [];

        function loadOpenedTrees() {{
            try {{
                const saved = localStorage.getItem('hfsync_opened_trees');
                if (saved) {{
                    const parsed = JSON.parse(saved);
                    if (Array.isArray(parsed)) {{
                        openedTreesQueue = parsed.filter(p => typeof p === 'string' && p.trim()).slice(-MAX_OPEN_TREES);
                    }}
                }}
            }} catch (e) {{
                openedTreesQueue = [];
            }}
        }}

        function saveOpenedTrees() {{
            try {{
                localStorage.setItem('hfsync_opened_trees', JSON.stringify(openedTreesQueue));
            }} catch (e) {{}}
        }}

        function saveLastInteractedTree(folderPath) {{
            try {{
                localStorage.setItem('hfsync_last_tree', folderPath);
            }} catch (e) {{}}
        }}

        function findFolderGroup(folderPath) {{
            if (!folderPath) return null;
            const all = document.querySelectorAll('.folder-group');
            for (let i = 0; i < all.length; i++) {{
                if (all[i].getAttribute('data-folder') === folderPath) {{
                    return all[i];
                }}
            }}
            return null;
        }}

        function openFolderElement(folderGroup) {{
            if (!folderGroup) return;
            const header = folderGroup.querySelector('.folder-header');
            if (header) {{
                const arrow = header.querySelector('.folder-arrow');
                if (arrow) arrow.classList.add('open');
            }}
            const children = folderGroup.querySelector(':scope > .folder-children');
            if (children) children.classList.remove('collapsed');

            // Ensure ancestor folders are also open
            let parentGroup = folderGroup.parentElement ? folderGroup.parentElement.closest('.folder-group') : null;
            while (parentGroup) {{
                const pHeader = parentGroup.querySelector('.folder-header');
                if (pHeader) {{
                    const pArrow = pHeader.querySelector('.folder-arrow');
                    if (pArrow) pArrow.classList.add('open');
                }}
                const pChildren = parentGroup.querySelector(':scope > .folder-children');
                if (pChildren) pChildren.classList.remove('collapsed');
                parentGroup = parentGroup.parentElement ? parentGroup.parentElement.closest('.folder-group') : null;
            }}
        }}

        function closeFolderElement(folderGroup) {{
            if (!folderGroup) return;
            const header = folderGroup.querySelector('.folder-header');
            if (header) {{
                const arrow = header.querySelector('.folder-arrow');
                if (arrow) arrow.classList.remove('open');
            }}
            const children = folderGroup.querySelector(':scope > .folder-children');
            if (children) children.classList.add('collapsed');
        }}

        function applyOpenedTrees() {{
            const allFolders = document.querySelectorAll('.folder-group');
            allFolders.forEach(fg => {{
                const folderPath = fg.getAttribute('data-folder');
                if (openedTreesQueue.includes(folderPath)) {{
                    openFolderElement(fg);
                }} else {{
                    closeFolderElement(fg);
                }}
            }});
        }}

        // Track and persist scroll position
        let scrollDebounceTimer = null;
        window.addEventListener('scroll', () => {{
            if (scrollDebounceTimer) clearTimeout(scrollDebounceTimer);
            scrollDebounceTimer = setTimeout(() => {{
                try {{
                    localStorage.setItem('hfsync_scroll_pos', String(window.scrollY));
                }} catch (e) {{}}
            }}, 150);
        }}, {{ passive: true }});

        window.addEventListener('beforeunload', () => {{
            try {{
                localStorage.setItem('hfsync_scroll_pos', String(window.scrollY));
            }} catch (e) {{}}
        }});

        function restoreUserPosition() {{
            try {{
                const savedY = localStorage.getItem('hfsync_scroll_pos');
                const lastTree = localStorage.getItem('hfsync_last_tree');
                if (savedY !== null && !isNaN(parseFloat(savedY)) && parseFloat(savedY) > 0) {{
                    const y = parseFloat(savedY);
                    requestAnimationFrame(() => {{
                        window.scrollTo({{ top: y, behavior: 'instant' }});
                        setTimeout(() => {{
                            window.scrollTo({{ top: y, behavior: 'instant' }});
                        }}, 120);
                    }});
                }} else if (lastTree) {{
                    setTimeout(() => {{
                        const el = findFolderGroup(lastTree);
                        if (el) {{
                            el.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
                        }}
                    }}, 120);
                }}
            }} catch (e) {{}}
        }}

        function refreshTreeDisplay() {{
            updateViewToggleButtons();
            const container = document.getElementById('treeContainer');
            if (allFiles.length === 0) {{
                container.innerHTML = '<div class="empty-state">No playable videos found with current encryption password. Drop videos into <code>videos_to_sync/</code> to upload!</div>';
                return;
            }}
            const tree = buildTree(allFiles);
            container.innerHTML = renderTree(tree, 0);

            // Restore opened trees
            applyOpenedTrees();

            // Run in-place filtering
            filterTree();

            // Promote playing video if active
            if (currentVideoKey) {{
                highlightAndPromotePlayingVideo(currentVideoKey);
            }}
            applyHideTagsState();
        }}

        function toggleFolder(header) {{
            const folderGroup = header.closest('.folder-group');
            if (!folderGroup) return;
            const folderPath = folderGroup.getAttribute('data-folder');
            if (!folderPath) return;

            const arrow = header.querySelector('.folder-arrow');
            const isCurrentlyOpen = arrow ? arrow.classList.contains('open') : false;

            if (isCurrentlyOpen) {{
                // Close this folder
                closeFolderElement(folderGroup);
                openedTreesQueue = openedTreesQueue.filter(p => p !== folderPath);
                saveOpenedTrees();
            }} else {{
                // Open this folder
                openFolderElement(folderGroup);

                // Add to queue (remove first if already exists to push to most recent)
                openedTreesQueue = openedTreesQueue.filter(p => p !== folderPath);
                openedTreesQueue.push(folderPath);

                // FIFO: Max 3 trees can be open at one time; opening 4th will close earliest one
                while (openedTreesQueue.length > MAX_OPEN_TREES) {{
                    const oldestPath = openedTreesQueue.shift();
                    const oldestEl = findFolderGroup(oldestPath);
                    if (oldestEl) {{
                        closeFolderElement(oldestEl);
                    }}
                }}

                saveOpenedTrees();
                saveLastInteractedTree(folderPath);
            }}
        }}

        function expandAll() {{
            document.querySelectorAll('.folder-group').forEach(fg => openFolderElement(fg));
        }}

        function collapseAll() {{
            document.querySelectorAll('.folder-group').forEach(fg => closeFolderElement(fg));
            openedTreesQueue = [];
            saveOpenedTrees();
        }}

        function highlightAndPromotePlayingVideo(videoKey) {{
            if (!videoKey) return;
            const cleanKey = videoKey.replace(/^\/+/, '');
            const allCards = document.querySelectorAll('.video-card, .file-row');
            let targetCard = null;

            allCards.forEach(c => {{
                const k = (c.getAttribute('data-key') || '').replace(/^\/+/, '');
                if (k === cleanKey || k.toLowerCase() === cleanKey.toLowerCase()) {{
                    targetCard = c;
                    c.classList.add('is-now-playing');
                    if (!c.querySelector('.now-playing-badge')) {{
                        const badge = document.createElement('div');
                        badge.className = 'now-playing-badge';
                        badge.innerHTML = '&#9654; Playing';
                        const thumb = c.querySelector('.video-thumb-card');
                        if (thumb) thumb.appendChild(badge);
                    }}
                }} else {{
                    c.classList.remove('is-now-playing');
                    c.querySelector('.now-playing-badge')?.remove();
                }}
            }});

            if (targetCard) {{
                const folderGroup = targetCard.closest('.folder-group');
                const treeContainer = document.getElementById('treeContainer');

                if (folderGroup && treeContainer) {{
                    // Find root top folder under treeContainer
                    let topFolder = folderGroup;
                    while (topFolder.parentElement && topFolder.parentElement !== treeContainer && topFolder.parentElement.closest('.folder-group')) {{
                        topFolder = topFolder.parentElement.closest('.folder-group');
                    }}

                    // Root top folder remains in its natural tree position (do not reorder DOM)
                    document.querySelectorAll('.folder-group').forEach(fg => {{
                        fg.classList.remove('is-playing-group');
                        fg.querySelector('.folder-playing-badge')?.remove();
                    }});
                    topFolder.classList.add('is-playing-group');
                    const headerLeft = topFolder.querySelector('.folder-left');
                    if (headerLeft && !headerLeft.querySelector('.folder-playing-badge')) {{
                        const playPill = document.createElement('span');
                        playPill.className = 'folder-playing-badge';
                        playPill.innerHTML = '&#9654; Now Playing';
                        headerLeft.appendChild(playPill);
                    }}

                    const topPath = topFolder.getAttribute('data-folder');
                    if (topPath) {{
                        openFolderElement(topFolder);
                        if (!openedTreesQueue.includes(topPath)) {{
                            openedTreesQueue.push(topPath);
                            while (openedTreesQueue.length > MAX_OPEN_TREES) {{
                                const oldestPath = openedTreesQueue.shift();
                                const oldestEl = findFolderGroup(oldestPath);
                                if (oldestEl) closeFolderElement(oldestEl);
                            }}
                            saveOpenedTrees();
                        }}
                    }}
                    openFolderElement(folderGroup);
                }}
            }}
        }}

        function filterTree() {{
            const q = document.getElementById('searchInput') ? document.getElementById('searchInput').value.toLowerCase().trim() : '';
            const hasTags = selectedTags.size > 0;
            const hasGenres = selectedGenres.size > 0;
            const hasAccounts = selectedAccounts.size > 0;
            const hasFilters = Boolean(q || hasTags || hasGenres || hasAccounts);

            updateFilterBadges();

            const matchingFileKeys = new Set();
            allFiles.forEach(f => {{
                const meta = f.metadata || {{}};
                const title = (meta.title || "").toLowerCase();
                const overview = (meta.overview || "").toLowerCase();
                const year = String(meta.year || "").toLowerCase();
                const key = (f.key || "").toLowerCase();
                const account = f.account || primaryAccount;
                const genres = (meta.genres || []).map(g => String(g));
                const tags = (meta.tags || []).map(t => String(t));
                const genresLower = genres.map(g => g.toLowerCase());
                const tagsLower = tags.map(t => t.toLowerCase());

                const aliases = (f.aliases || []).map(a => String(a).toLowerCase());
                const fileNames = (f.file_names || []).map(fn => String(fn).toLowerCase());

                let matchesQ = true;
                if (q) {{
                    matchesQ = key.includes(q) ||
                               title.includes(q) ||
                               year.includes(q) ||
                               overview.includes(q) ||
                               account.toLowerCase().includes(q) ||
                               genresLower.some(g => g.includes(q)) ||
                               tagsLower.some(t => t.includes(q)) ||
                               aliases.some(a => a.includes(q)) ||
                               fileNames.some(fn => fn.includes(q));
                }}

                let matchesAccount = true;
                if (hasAccounts) {{
                    matchesAccount = selectedAccounts.has(account);
                }}

                let matchesGenre = true;
                if (hasGenres) {{
                    matchesGenre = genres.some(g => selectedGenres.has(g));
                }}

                let matchesTag = true;
                if (hasTags) {{
                    matchesTag = tags.some(t => selectedTags.has(t));
                }}

                if (matchesQ && matchesAccount && matchesGenre && matchesTag) {{
                    matchingFileKeys.add(f.key);
                }}
            }});

            // 1. Toggle visibility of all video cards and list rows in-place (no DOM rebuild!)
            const allItems = document.querySelectorAll('.video-card, .file-row');
            allItems.forEach(item => {{
                const key = item.getAttribute('data-key');
                if (matchingFileKeys.has(key)) {{
                    item.classList.remove('is-filter-hidden');
                }} else {{
                    item.classList.add('is-filter-hidden');
                }}
            }});

            // 2. Update folder groups visibility and count in-place
            const allFolderGroups = document.querySelectorAll('.folder-group');
            allFolderGroups.forEach(fg => {{
                const visibleCount = fg.querySelectorAll('.video-card:not(.is-filter-hidden), .file-row:not(.is-filter-hidden)').length;
                const totalInFolder = fg.querySelectorAll('.video-card, .file-row').length;
                const countBadge = fg.querySelector(':scope > .folder-header .folder-count');

                if (visibleCount > 0) {{
                    fg.classList.remove('is-filter-hidden');
                    if (countBadge) {{
                        if (hasFilters) {{
                            countBadge.textContent = `${{visibleCount}} of ${{totalInFolder}} matching`;
                            countBadge.classList.add('is-matching-filtered');
                        }} else {{
                            countBadge.textContent = `${{totalInFolder}} video(s)`;
                            countBadge.classList.remove('is-matching-filtered');
                        }}
                    }}
                }} else {{
                    fg.classList.add('is-filter-hidden');
                }}
            }});

            // Update matching results badges next to filter bar
            window.lastMatchingCount = matchingFileKeys.size;
            const summaryEl = document.getElementById('filterSummaryCount');
            if (summaryEl) {{
                if (hasFilters) {{
                    summaryEl.innerHTML = `🎯 <strong>${{matchingFileKeys.size}}</strong> of <strong>${{allFiles.length}}</strong> matching`;
                    summaryEl.style.display = 'inline-flex';
                }} else {{
                    summaryEl.style.display = 'none';
                }}
            }}
            renderActiveFilterChips(matchingFileKeys.size);

            // 3. Show/hide smooth Empty State Notice without wiping treeContainer
            const container = document.getElementById('treeContainer');
            let emptyEl = document.getElementById('filterEmptyNotice');
            if (!emptyEl && container) {{
                emptyEl = document.createElement('div');
                emptyEl.id = 'filterEmptyNotice';
                emptyEl.className = 'empty-state';
                emptyEl.innerHTML = 'No videos match your active filter criteria. <br><button onclick="resetAllFilters()" class="btn btn-link" style="margin-top: 12px;">Reset All Filters</button>';
                container.appendChild(emptyEl);
            }}

            if (emptyEl) {{
                if (matchingFileKeys.size === 0 && allFiles.length > 0) {{
                    emptyEl.style.display = 'block';
                }} else {{
                    emptyEl.style.display = 'none';
                }}
            }}
        }}

        // Close dropdown popovers on outside click
        document.addEventListener('click', function(e) {{
            if (!e.target.closest('.popover-wrapper')) {{
                document.querySelectorAll('.filter-popover').forEach(p => p.style.display = 'none');
            }}
        }});

        /* Interactive Video Thumbnail Moving Preview Engine */
        let thumbCycleInterval = null;
        let activeHoveredThumb = null;
        let thumbManualScrubTimeout = null;
        let thumbSequenceIdx = 0;

        // 25 sample frames across the 100-frame storyboard (steps of 4 frames)
        const THUMB_SAMPLE_FRAMES = [
            2, 6, 10, 14, 18, 22, 26, 30, 34, 38, 42, 46, 50, 54, 58, 62, 66, 70, 74, 78, 82, 86, 90, 94, 98
        ];

        function setThumbCardFrame(card, frameIndex) {{
            if (!card) return;
            const canvas = card.querySelector('.thumb-canvas');
            if (!canvas) return;
            
            const w = Math.round(canvas.clientWidth || card.clientWidth || 120);
            const h = Math.round(canvas.clientHeight || card.clientHeight || 68);

            const col = frameIndex % 10;
            const row = Math.floor(frameIndex / 10);
            
            canvas.style.backgroundSize = `${{10 * w}}px ${{10 * h}}px`;
            canvas.style.backgroundPosition = `-${{col * w}}px -${{row * h}}px`;
            
            const fill = card.querySelector('.thumb-scrub-fill');
            if (fill) {{
                fill.style.width = `${{((frameIndex + 1) / 100) * 100}}%`;
            }}
        }}

        function startThumbAutoCycle(card, startFromIndex = 0) {{
            clearInterval(thumbCycleInterval);
            thumbSequenceIdx = startFromIndex % THUMB_SAMPLE_FRAMES.length;
            
            thumbCycleInterval = setInterval(() => {{
                if (!activeHoveredThumb || activeHoveredThumb !== card) {{
                    clearInterval(thumbCycleInterval);
                    return;
                }}
                const frame = THUMB_SAMPLE_FRAMES[thumbSequenceIdx];
                setThumbCardFrame(card, frame);
                thumbSequenceIdx = (thumbSequenceIdx + 1) % THUMB_SAMPLE_FRAMES.length;
            }}, 260);
        }}

        async function handleThumbEnter(card) {{
            activeHoveredThumb = card;
            const key = card.dataset.key;
            const safeId = card.dataset.safeid;
            const canvas = card.querySelector('.thumb-canvas');
            if (!canvas) return;

            // Remember initial static poster background for instant revert on mouse leave
            if (!canvas.dataset.staticBg) {{
                canvas.dataset.staticBg = canvas.style.backgroundImage || '';
            }}

            // 1. Instant cache check: activate HD sprite sheet immediately with zero latency
            const sb = storyboardCache[safeId] || storyboardCache[key];
            if (sb && sb.available && sb.sprite_url) {{
                canvas.style.backgroundImage = `url('${{sb.sprite_url}}')`;
                const fb = card.querySelector('.thumb-fallback');
                if (fb) fb.remove();
                startThumbAutoCycle(card, 0);
                return;
            }}

            // 2. Fallback: on-demand generation and polling
            try {{
                const res = await fetch(`/api/storyboard?key=${{encodeURIComponent(key)}}`);
                if (!res.ok) return;
                const data = await res.json();
                
                if (data.available && data.sprite_url) {{
                    registerStoryboard(data.safe_id || safeId, key);
                    if (activeHoveredThumb === card) {{
                        canvas.style.backgroundImage = `url('${{data.sprite_url}}')`;
                        const fb = card.querySelector('.thumb-fallback');
                        if (fb) fb.remove();
                        startThumbAutoCycle(card, 0);
                    }}
                }} else {{
                    let pollCount = 0;
                    const poll = setInterval(async () => {{
                        pollCount++;
                        if (pollCount > 25 || activeHoveredThumb !== card) {{
                            clearInterval(poll);
                            return;
                        }}
                        try {{
                            const r2 = await fetch(`/api/storyboard?key=${{encodeURIComponent(key)}}`);
                            if (r2.ok) {{
                                const d2 = await r2.json();
                                if (d2.available && d2.sprite_url) {{
                                    clearInterval(poll);
                                    registerStoryboard(d2.safe_id || safeId, key);
                                    if (activeHoveredThumb === card) {{
                                        canvas.style.backgroundImage = `url('${{d2.sprite_url}}')`;
                                        const fb = card.querySelector('.thumb-fallback');
                                        if (fb) fb.remove();
                                        startThumbAutoCycle(card, 0);
                                    }}
                                }}
                            }}
                        }} catch (e) {{}}
                    }}, 800);
                }}
            }} catch (err) {{}}
        }}

        function handleThumbMove(card, event) {{
            if (!card) return;
            if (card.dataset.screensHover === '1') return;
            const canvas = card.querySelector('.thumb-canvas');
            if (!canvas) return;

            const safeId = card.dataset.safeid;
            const key = card.dataset.key;
            const sb = storyboardCache[safeId] || storyboardCache[key];
            const spriteUrl = (sb && sb.sprite_url) ? sb.sprite_url : `/api/storyboard/image?id=${{safeId}}`;

            if (safeId && (!canvas.style.backgroundImage || !canvas.style.backgroundImage.includes('/api/storyboard/image'))) {{
                canvas.style.backgroundImage = `url('${{spriteUrl}}')`;
            }}

            clearInterval(thumbCycleInterval);
            clearTimeout(thumbManualScrubTimeout);

            const rect = card.getBoundingClientRect();
            const pct = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
            const frameIndex = Math.min(99, Math.floor(pct * 100));
            
            card.dataset.lastScrubPct = pct;
            card.dataset.scrubbing = '1';
            setThumbCardFrame(card, frameIndex);

            thumbManualScrubTimeout = setTimeout(() => {{
                if (activeHoveredThumb === card) {{
                    let closestIdx = 0;
                    let minDiff = 999;
                    THUMB_SAMPLE_FRAMES.forEach((f, idx) => {{
                        const diff = Math.abs(f - frameIndex);
                        if (diff < minDiff) {{
                            minDiff = diff;
                            closestIdx = idx;
                        }}
                    }});
                    startThumbAutoCycle(card, closestIdx);
                }}
            }}, 800);
        }}

        function handleThumbLeave(card) {{
            clearInterval(thumbCycleInterval);
            clearTimeout(thumbManualScrubTimeout);
            if (activeHoveredThumb === card) {{
                activeHoveredThumb = null;
            }}
            delete card.dataset.scrubbing;
            const canvas = card.querySelector('.thumb-canvas');
            if (canvas) {{
                if (canvas.dataset.staticBg) {{
                    canvas.style.backgroundImage = canvas.dataset.staticBg;
                }}
                canvas.style.backgroundSize = 'cover';
                canvas.style.backgroundPosition = 'center';
            }}
            const fill = card.querySelector('.thumb-scrub-fill');
            if (fill) fill.style.width = '0%';
        }}

        /* Advanced YouTube-Style Video Player Controller */
        let currentBlobId = "";
        let currentVideoKey = "";
        let currentPlayingSafeId = "";
        let activeStoryboard = null;
        let currentStoryboardKey = null;

        async function loadStoryboard(streamUrl, duration) {{
            if (!streamUrl) return;
            const raw = streamUrl.replace(/.*\/stream\//, '');
            const key = decodeURIComponent(raw);
            currentStoryboardKey = key;
            const thumbElem = document.getElementById('ytThumbPreview');

            // 1. Instant lookup from frontend cache or allFiles
            const fileObj = (allFiles || []).find(f => f.key === key || (f.file_names && f.file_names.includes(key)));
            const safeId = fileObj ? fileObj.safe_id : (storyboardCache[key] ? storyboardCache[key].safe_id : null);

            if (safeId && storyboardCache[safeId]) {{
                activeStoryboard = storyboardCache[safeId];
                if (thumbElem) {{
                    thumbElem.style.backgroundImage = `url('${{activeStoryboard.sprite_url}}')`;
                }}
                return; // INSTANT! Zero network latency
            }} else if (fileObj && fileObj.has_storyboard && safeId) {{
                registerStoryboard(safeId, key);
                activeStoryboard = storyboardCache[safeId];
                if (thumbElem) {{
                    thumbElem.style.backgroundImage = `url('${{activeStoryboard.sprite_url}}')`;
                }}
                return; // INSTANT!
            }}

            activeStoryboard = null;
            if (thumbElem) thumbElem.style.display = 'none';

            // 2. Fallback to API if not pre-cached
            try {{
                const durParam = (duration && duration > 0) ? `&duration=${{Math.round(duration)}}` : '';
                const res = await fetch(`/api/storyboard?key=${{encodeURIComponent(key)}}${{durParam}}`);
                if (!res.ok) return;
                const data = await res.json();
                if (currentStoryboardKey !== key) return;

                if (data.available && data.sprite_url) {{
                    registerStoryboard(data.safe_id || safeId, key);
                    activeStoryboard = data;
                    if (thumbElem) {{
                        thumbElem.style.backgroundImage = `url('${{data.sprite_url}}')`;
                    }}
                }} else {{
                    let attempts = 0;
                    const pollInterval = setInterval(async () => {{
                        attempts++;
                        if (currentStoryboardKey !== key || activeStoryboard || attempts > 20) {{
                            clearInterval(pollInterval);
                            return;
                        }}
                        try {{
                            const r2 = await fetch(`/api/storyboard?key=${{encodeURIComponent(key)}}`);
                            if (r2.ok) {{
                                const d2 = await r2.json();
                                if (d2.available && d2.sprite_url && currentStoryboardKey === key) {{
                                    clearInterval(pollInterval);
                                    registerStoryboard(d2.safe_id || safeId, key);
                                    activeStoryboard = d2;
                                    if (thumbElem) {{
                                        thumbElem.style.backgroundImage = `url('${{d2.sprite_url}}')`;
                                    }}
                                }}
                            }}
                        }} catch (e) {{}}
                    }}, 1000);
                }}
            }} catch (err) {{}}
        }}
        let ytControlsTimeout = null;
        let isScrubDragging = false;
        let clickTimeout = null;
        let userClosedPlayer = true;
        let isFloatingMini = false;
        let scrollObserverBound = false;

        function initFloatingScrollObserver() {{
            if (scrollObserverBound) return;
            scrollObserverBound = true;

            window.addEventListener('scroll', () => {{
                const box = document.getElementById('playerBox');
                const player = document.getElementById('videoPlayer');
                if (userClosedPlayer || !box || box.style.display === 'none' || !player || !player.src) return;

                const rect = box.getBoundingClientRect();
                // When playerBox has scrolled out of view (its bottom is near/above top of viewport)
                if (rect.bottom < 60 && !isFloatingMini) {{
                    activateFloatingMiniPlayer();
                }} else if (rect.bottom >= 60 && isFloatingMini) {{
                    deactivateFloatingMiniPlayer();
                }}
            }}, {{ passive: true }});
        }}

        function savePlayerVolume(vol, muted) {{
            try {{
                localStorage.setItem('hfsync_player_vol', String(typeof vol === 'number' ? vol : 1.0));
                localStorage.setItem('hfsync_player_muted', muted ? '1' : '0');
            }} catch (e) {{}}
        }}

        function restorePlayerVolume() {{
            const player = document.getElementById('videoPlayer');
            if (!player) return;
            try {{
                const savedVol = localStorage.getItem('hfsync_player_vol');
                const savedMuted = localStorage.getItem('hfsync_player_muted');
                if (savedVol !== null && !isNaN(parseFloat(savedVol))) {{
                    player.volume = Math.max(0, Math.min(1, parseFloat(savedVol)));
                }}
                if (savedMuted !== null) {{
                    player.muted = (savedMuted === '1');
                }}
            }} catch (e) {{}}
            updateVolumeUI();
        }}

        let lastPosSaveTime = 0;
        function captureCurrentVideoPosition() {{
            const player = document.getElementById('videoPlayer');
            if (!player || !player.duration || player.duration < 10) return;
            const cur = player.currentTime;
            const dur = player.duration;
            const key = currentVideoKey;
            const fileObj = (allFiles || []).find(f => f.key === key || (f.file_names && f.file_names.includes(key)));
            const safeId = currentPlayingSafeId || (fileObj ? fileObj.safe_id : key);
            if (!safeId) return;

            if (cur > 3 && cur < dur - 5) {{
                saveVideoPosition(safeId, cur, dur);
            }} else if (cur >= dur - 5) {{
                clearVideoPosition(safeId);
            }}
        }}

        function saveVideoPosition(safeId, time, duration) {{
            if (!safeId) return;
            try {{
                localStorage.setItem('hfsync_pos_' + safeId, JSON.stringify({{
                    time: Math.floor(time),
                    duration: Math.floor(duration),
                    updated: Date.now()
                }}));
            }} catch (e) {{}}
            updateCardResumeIndicators(safeId, time, duration);
        }}

        function getSavedVideoPosition(safeId) {{
            if (!safeId) return null;
            try {{
                const raw = localStorage.getItem('hfsync_pos_' + safeId);
                if (raw) return JSON.parse(raw);
            }} catch (e) {{}}
            return null;
        }}

        function clearVideoPosition(safeId) {{
            if (!safeId) return;
            try {{
                localStorage.removeItem('hfsync_pos_' + safeId);
            }} catch (e) {{}}
            updateCardResumeIndicators(safeId, 0, 0);
        }}

        function updateCardResumeIndicators(safeId, time, duration) {{
            const cards = document.querySelectorAll(`[data-safeid="${{safeId}}"]`);
            cards.forEach(card => {{
                let resumeBar = card.querySelector('.card-resume-bar');
                let resumePill = card.querySelector('.card-resume-badge');
                if (time > 3 && duration > 0) {{
                    const pct = Math.min(100, Math.round((time / duration) * 100));
                    if (!resumeBar) {{
                        resumeBar = document.createElement('div');
                        resumeBar.className = 'card-resume-bar';
                        card.appendChild(resumeBar);
                    }}
                    resumeBar.style.width = pct + '%';
                    resumeBar.style.display = 'block';

                    if (!resumePill) {{
                        resumePill = document.createElement('div');
                        resumePill.className = 'card-resume-badge';
                        card.appendChild(resumePill);
                    }}
                    resumePill.textContent = '⏱️ ' + formatTime(time);
                    resumePill.title = `Resume at ${{formatTime(time)}} / ${{formatTime(duration)}} (${{pct}}%)`;
                    resumePill.style.display = 'block';
                }} else {{
                    if (resumeBar) resumeBar.style.display = 'none';
                    if (resumePill) resumePill.style.display = 'none';
                }}
            }});
        }}

        function setMiniPlayerSize(size, event) {{
            if (event) event.stopPropagation();
            const container = document.getElementById('ytContainer');
            if (!container) return;
            const valid = ['s', 'm', 'l'];
            const s = valid.includes(size) ? size : 'm';
            container.classList.remove('mini-size-s', 'mini-size-m', 'mini-size-l');
            container.classList.add('mini-size-' + s);
            try {{
                localStorage.setItem('hfsync_mini_size', s);
            }} catch (e) {{}}
            document.querySelectorAll('.yt-mini-size-btn').forEach(b => {{
                b.classList.toggle('active', b.dataset.size === s);
            }});
        }}

        function initMiniPlayerDraggable() {{
            const container = document.getElementById('ytContainer');
            const header = container ? container.querySelector('.yt-top-overlay') : null;
            if (!container || !header || container.dataset.draggableInited) return;
            container.dataset.draggableInited = "1";

            let isDragging = false;
            let startX = 0, startY = 0;
            let initialLeft = 0, initialTop = 0;

            header.addEventListener('pointerdown', (e) => {{
                if (!container.classList.contains('is-floating-mini')) return;
                if (e.target.closest('button') || e.target.closest('a') || e.target.closest('.yt-mini-size-controls')) return;

                isDragging = true;
                header.setPointerCapture(e.pointerId);
                const rect = container.getBoundingClientRect();
                initialLeft = rect.left;
                initialTop = rect.top;
                startX = e.clientX;
                startY = e.clientY;

                container.style.bottom = 'auto';
                container.style.right = 'auto';
                container.style.left = `${{initialLeft}}px`;
                container.style.top = `${{initialTop}}px`;
                container.style.userSelect = 'none';
            }});

            header.addEventListener('pointermove', (e) => {{
                if (!isDragging) return;
                const dx = e.clientX - startX;
                const dy = e.clientY - startY;
                const newLeft = Math.max(6, Math.min(window.innerWidth - container.offsetWidth - 6, initialLeft + dx));
                const newTop = Math.max(6, Math.min(window.innerHeight - container.offsetHeight - 6, initialTop + dy));
                container.style.left = `${{newLeft}}px`;
                container.style.top = `${{newTop}}px`;
            }});

            const endDrag = (e) => {{
                if (isDragging) {{
                    isDragging = false;
                    container.style.userSelect = '';
                    try {{ header.releasePointerCapture(e.pointerId); }} catch (err) {{}}
                }}
            }};
            header.addEventListener('pointerup', endDrag);
            header.addEventListener('pointercancel', endDrag);
        }}

        function handleCardThumbClick(card, event) {{
            if (!card) return;
            if (event && (event.target.closest('button') || event.target.closest('a'))) return;
            const key = card.dataset.key;
            const safeId = card.dataset.safeid;
            const fileObj = (allFiles || []).find(f => f.key === key || f.safe_id === safeId);
            const title = fileObj ? (fileObj.metadata?.title || key) : key;
            const blobPath = fileObj ? (fileObj.blob_path || '') : '';
            const dur = fileObj ? (fileObj.duration_sec || 0) : 0;

            const clickedScrubBar = Boolean(event && event.target && event.target.closest('.thumb-scrub-bar'));
            const isActivelyScrubbing = card.classList.contains('is-scrubbing') || (card.dataset.scrubbing === '1');

            let seekTime = 0;
            const savedPos = getSavedVideoPosition(safeId);

            if (clickedScrubBar) {{
                const scrubBar = card.querySelector('.thumb-scrub-bar') || card;
                const rect = scrubBar.getBoundingClientRect();
                const pct = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
                seekTime = Math.floor(pct * dur);
            }} else if (savedPos && savedPos.time > 5) {{
                seekTime = savedPos.time;
            }} else if (isActivelyScrubbing && event && event.clientX) {{
                const rect = card.getBoundingClientRect();
                const pct = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
                seekTime = Math.floor(pct * dur);
            }}
            playVideo('/stream/' + key, title, blobPath, seekTime);
        }}

        function activateFloatingMiniPlayer() {{
            const box = document.getElementById('playerBox');
            const container = document.getElementById('ytContainer');
            if (!box || !container || isFloatingMini) return;

            // Preserve placeholder space so document does not jump
            if (box.offsetHeight > 100) {{
                box.style.minHeight = box.offsetHeight + 'px';
            }} else {{
                box.style.minHeight = '380px';
            }}
            container.classList.add('is-floating-mini');
            isFloatingMini = true;

            // Always reset position to bottom right on every activation/open
            container.style.left = '';
            container.style.top = '';
            container.style.bottom = '24px';
            container.style.right = '24px';

            let savedSize = 'm';
            try {{ savedSize = localStorage.getItem('hfsync_mini_size') || 'm'; }} catch (e) {{}}
            setMiniPlayerSize(savedSize);
            initMiniPlayerDraggable();
        }}

        function deactivateFloatingMiniPlayer() {{
            const box = document.getElementById('playerBox');
            const container = document.getElementById('ytContainer');
            if (!box || !container || !isFloatingMini) return;

            container.classList.remove('is-floating-mini');
            container.style.left = '';
            container.style.top = '';
            container.style.bottom = '';
            container.style.right = '';
            box.style.minHeight = '';
            isFloatingMini = false;
        }}

        function expandFromFloatingMini() {{
            const box = document.getElementById('playerBox');
            if (box) {{
                deactivateFloatingMiniPlayer();
                box.scrollIntoView({{ behavior: 'smooth', block: 'start' }});
            }}
        }}

        function formatTime(sec) {{
            if (isNaN(sec) || sec < 0) return "00:00";
            const h = Math.floor(sec / 3600);
            const m = Math.floor((sec % 3600) / 60);
            const s = Math.floor(sec % 60);
            const pad = (n) => String(n).padStart(2, "0");
            if (h > 0) {{
                return `${{h}}:${{pad(m)}}:${{pad(s)}}`;
            }}
            return `${{pad(m)}}:${{pad(s)}}`;
        }}

        function flashCenterFeedback(text) {{
            const fb = document.getElementById('ytCenterFeedback');
            if (!fb) return;
            fb.textContent = text;
            fb.classList.remove('show');
            void fb.offsetWidth; // Force reflow
            fb.classList.add('show');
            setTimeout(() => fb.classList.remove('show'), 450);
        }}

        function playVideo(url, title, blobPath, startTime) {{
            const box = document.getElementById('playerBox');
            const player = document.getElementById('videoPlayer');
            const playerTitle = document.getElementById('playerTitle');
            const container = document.getElementById('ytContainer');

            userClosedPlayer = false;

            currentBlobId = blobPath || url;
            currentVideoKey = (url.startsWith('/stream/') ? url.substring('/stream/'.length) : url);
            try {{ currentVideoKey = decodeURIComponent(currentVideoKey); }} catch (e) {{}}
            
            const fileObj = (allFiles || []).find(f => f.key === currentVideoKey || (f.file_names && f.file_names.includes(currentVideoKey)));
            const safeId = fileObj ? fileObj.safe_id : currentVideoKey;
            currentPlayingSafeId = safeId;

            highlightAndPromotePlayingVideo(currentVideoKey);
            playerTitle.textContent = title || "Decrypted Stream";
            box.style.display = 'block';
            resetAudioMode();
            updateSubtitleTracks(currentVideoKey);
            restorePlayerVolume();

            // Check if user is scrolled down away from main player
            const rect = box.getBoundingClientRect();
            const isScrolledDown = (window.scrollY > 180 || rect.bottom < 80 || isFloatingMini);

            if (isScrolledDown) {{
                activateFloatingMiniPlayer();
            }} else {{
                deactivateFloatingMiniPlayer();
            }}

            // Use /stream/play/ (no .mp4 extension) to completely avoid IDM capture:
            const streamPlayUrl = '/stream/play/' + encodeURIComponent(safeId);
            player.src = streamPlayUrl;

            // Target resume position or scrub seek point:
            let targetSeek = 0;
            if (typeof startTime === 'number' && startTime > 0) {{
                targetSeek = startTime;
            }} else {{
                const savedPos = getSavedVideoPosition(safeId);
                if (savedPos && savedPos.time > 5) {{
                    targetSeek = savedPos.time;
                }}
            }}

            if (targetSeek > 0) {{
                const applySeek = () => {{
                    try {{
                        player.currentTime = targetSeek;
                        flashCenterFeedback(`Position: ${{formatTime(targetSeek)}}`);
                    }} catch (err) {{}}
                }};
                if (player.readyState >= 1) {{
                    applySeek();
                }} else {{
                    player.addEventListener('loadedmetadata', applySeek, {{ once: true }});
                }}
            }}

            player.play().catch(() => {{}});

            if (!isScrolledDown && container) {{
                container.focus();
            }}

            initYtPlayerEvents();
            loadStoryboard(url, player.duration);
            initFloatingScrollObserver();
            initMiniPlayerDraggable();
            resetControlsTimer();
        }}

        function closePlayer() {{
            captureCurrentVideoPosition();
            userClosedPlayer = true;
            deactivateFloatingMiniPlayer();
            const box = document.getElementById('playerBox');
            const player = document.getElementById('videoPlayer');
            const container = document.getElementById('ytContainer');
            if (container) {{
                container.style.left = '';
                container.style.top = '';
                container.style.bottom = '24px';
                container.style.right = '24px';
            }}
            if (document.fullscreenElement) {{
                document.exitFullscreen().catch(() => {{}});
            }}
            player.pause();
            player.src = "";
            resetAudioMode();
            setSubtitleTrack('off');
            const topMenu = document.getElementById('ytTopSubMenu');
            if (topMenu) topMenu.style.display = 'none';
            box.style.display = 'none';
            box.style.minHeight = '';
            box.classList.remove('theater-mode');
        }}

        function togglePlayPause() {{
            const player = document.getElementById('videoPlayer');
            if (player.paused || player.ended) {{
                player.play();
                flashCenterFeedback('▶');
            }} else {{
                player.pause();
                flashCenterFeedback('❚❚');
            }}
        }}

        function seekRelative(deltaSec) {{
            const player = document.getElementById('videoPlayer');
            if (!player.duration) return;
            player.currentTime = Math.max(0, Math.min(player.duration, player.currentTime + deltaSec));
            captureCurrentVideoPosition();
            flashCenterFeedback(deltaSec > 0 ? `+${{deltaSec}}s` : `${{deltaSec}}s`);
        }}

        window.addEventListener('beforeunload', () => {{
            captureCurrentVideoPosition();
        }});
        window.addEventListener('pagehide', () => {{
            captureCurrentVideoPosition();
        }});

        function toggleMute() {{
            const player = document.getElementById('videoPlayer');
            player.muted = !player.muted;
            savePlayerVolume(player.volume, player.muted);
            updateVolumeUI();
        }}

        function updateVolumeUI() {{
            const player = document.getElementById('videoPlayer');
            const slider = document.getElementById('ytVolumeSlider');
            const volHigh = document.getElementById('ytVolHigh');
            const volMute = document.getElementById('ytVolMute');

            if (slider) slider.value = player.muted ? 0 : player.volume;
            if (volHigh && volMute) {{
                if (player.muted || player.volume === 0) {{
                    volHigh.style.display = 'none';
                    volMute.style.display = 'block';
                }} else {{
                    volHigh.style.display = 'block';
                    volMute.style.display = 'none';
                }}
            }}
        }}

        function toggleSpeedMenu() {{
            const menu = document.getElementById('ytSpeedMenu');
            if (menu) menu.style.display = (menu.style.display === 'block') ? 'none' : 'block';
        }}

        function setSpeed(rate) {{
            const player = document.getElementById('videoPlayer');
            const btn = document.getElementById('ytSpeedBtn');
            const menu = document.getElementById('ytSpeedMenu');
            player.playbackRate = rate;
            if (btn) btn.textContent = rate === 1 ? '1x' : `${{rate}}x`;
            if (menu) {{
                menu.style.display = 'none';
                menu.querySelectorAll('.yt-speed-option').forEach(opt => {{
                    const txt = opt.textContent.trim().toLowerCase();
                    if ((rate === 1 && txt === 'normal') || txt === `${{rate}}x`) {{
                        opt.classList.add('active');
                    }} else {{
                        opt.classList.remove('active');
                    }}
                }});
            }}
            flashCenterFeedback(`${{rate}}x`);
        }}

        function toggleTheaterMode() {{
            const box = document.getElementById('playerBox');
            if (box) box.classList.toggle('theater-mode');
        }}

        function togglePiP() {{
            const player = document.getElementById('videoPlayer');
            if (document.pictureInPictureElement) {{
                document.exitPictureInPicture().catch(() => {{}});
            }} else if (player.requestPictureInPicture) {{
                player.requestPictureInPicture().catch(() => {{}});
            }}
        }}

        function toggleFullscreen() {{
            const container = document.getElementById('ytContainer');
            if (!document.fullscreenElement) {{
                if (container.requestFullscreen) container.requestFullscreen();
                else if (container.webkitRequestFullscreen) container.webkitRequestFullscreen();
            }} else {{
                if (document.exitFullscreen) document.exitFullscreen();
            }}
        }}

        function toggleStatsForNerds() {{
            const panel = document.getElementById('ytStatsPanel');
            if (panel) {{
                panel.style.display = (panel.style.display === 'block') ? 'none' : 'block';
                updateStatsForNerds();
            }}
        }}

        function updateStatsForNerds() {{
            const panel = document.getElementById('ytStatsPanel');
            if (!panel || panel.style.display !== 'block') return;
            const player = document.getElementById('videoPlayer');

            document.getElementById('ytStatRes').textContent = `${{player.videoWidth || 0}} x ${{player.videoHeight || 0}}`;
            document.getElementById('ytStatPos').textContent = `${{formatTime(player.currentTime)}} / ${{formatTime(player.duration)}}`;
            document.getElementById('ytStatSpeed').textContent = `${{player.playbackRate}}x`;
            document.getElementById('ytStatBlob').textContent = currentBlobId || "Encrypted HF Stream";

            let bufferedSec = 0;
            if (player.buffered && player.buffered.length > 0) {{
                for (let i = 0; i < player.buffered.length; i++) {{
                    if (player.currentTime >= player.buffered.start(i) && player.currentTime <= player.buffered.end(i)) {{
                        bufferedSec = (player.buffered.end(i) - player.currentTime);
                        break;
                    }}
                }}
            }}
            document.getElementById('ytStatBuffer').textContent = `${{bufferedSec.toFixed(2)}}s ahead`;
        }}

        function resetControlsTimer() {{
            const container = document.getElementById('ytContainer');
            const player = document.getElementById('videoPlayer');
            if (!container) return;

            container.classList.remove('controls-hidden', 'hide-cursor');
            clearTimeout(ytControlsTimeout);

            if (!player.paused && !player.ended) {{
                ytControlsTimeout = setTimeout(() => {{
                    container.classList.add('controls-hidden', 'hide-cursor');
                    const menu = document.getElementById('ytSpeedMenu');
                    if (menu) menu.style.display = 'none';
                }}, 2600);
            }}
        }}

        let ytPlayerEventsBound = false;
        function initYtPlayerEvents() {{
            if (ytPlayerEventsBound) return;
            ytPlayerEventsBound = true;

            const container = document.getElementById('ytContainer');
            const player = document.getElementById('videoPlayer');
            const playIcon = document.getElementById('ytPlayIcon');
            const pauseIcon = document.getElementById('ytPauseIcon');
            const curTimeElem = document.getElementById('ytCurTime');
            const durTimeElem = document.getElementById('ytDurTime');
            const scrubProgress = document.getElementById('ytScrubProgress');
            const scrubBuffered = document.getElementById('ytScrubBuffered');
            const scrubContainer = document.getElementById('ytScrubContainer');
            const scrubTooltip = document.getElementById('ytScrubTooltip');
            const spinner = document.getElementById('ytSpinner');
            const volSlider = document.getElementById('ytVolumeSlider');
            const fsEnter = document.getElementById('ytFsEnter');
            const fsExit = document.getElementById('ytFsExit');

            // Play / Pause Icons
            player.addEventListener('play', () => {{
                if (playIcon) playIcon.style.display = 'none';
                if (pauseIcon) pauseIcon.style.display = 'block';
                resetControlsTimer();
            }});
            player.addEventListener('pause', () => {{
                if (playIcon) playIcon.style.display = 'block';
                if (pauseIcon) pauseIcon.style.display = 'none';
                captureCurrentVideoPosition();
                resetControlsTimer();
            }});
            player.addEventListener('seeked', () => {{
                captureCurrentVideoPosition();
            }});

            // Buffering Spinner
            player.addEventListener('waiting', () => {{
                if (spinner) spinner.style.display = 'block';
            }});
            player.addEventListener('playing', () => {{
                if (spinner) spinner.style.display = 'none';
            }});
            player.addEventListener('canplay', () => {{
                if (spinner) spinner.style.display = 'none';
            }});

            restorePlayerVolume();

            // Time & Buffer Tracking & Resume Position Saving
            player.addEventListener('timeupdate', () => {{
                if (!player.duration) return;
                const pct = (player.currentTime / player.duration) * 100;
                if (scrubProgress && !isScrubDragging) scrubProgress.style.width = `${{pct}}%`;
                if (curTimeElem) curTimeElem.textContent = formatTime(player.currentTime);
                if (durTimeElem) durTimeElem.textContent = formatTime(player.duration);

                // Update buffered ranges
                if (player.buffered && player.buffered.length > 0 && scrubBuffered) {{
                    const bufEnd = player.buffered.end(player.buffered.length - 1);
                    const bufPct = (bufEnd / player.duration) * 100;
                    scrubBuffered.style.width = `${{Math.min(100, bufPct)}}%`;
                }}

                // Save watched progress for resume
                const now = Date.now();
                if (now - lastPosSaveTime > 1000 && player.duration > 10) {{
                    lastPosSaveTime = now;
                    captureCurrentVideoPosition();
                }}

                updateStatsForNerds();
            }});

            player.addEventListener('ended', () => {{
                const safeId = currentPlayingSafeId || (currentVideoKey ? ((allFiles || []).find(f => f.key === currentVideoKey)?.safe_id || currentVideoKey) : null);
                if (safeId) clearVideoPosition(safeId);
            }});

            player.addEventListener('loadedmetadata', () => {{
                if (durTimeElem) durTimeElem.textContent = formatTime(player.duration);
                updateStatsForNerds();
                loadStoryboard(player.src, player.duration);
            }});

            // Fullscreen UI sync
            document.addEventListener('fullscreenchange', () => {{
                const isFs = !!document.fullscreenElement;
                if (fsEnter && fsExit) {{
                    fsEnter.style.display = isFs ? 'none' : 'block';
                    fsExit.style.display = isFs ? 'block' : 'none';
                }}
            }});

            // Scrub Bar Scrubbing & Tooltip
            function scrubAtEvent(e) {{
                const rect = scrubContainer.getBoundingClientRect();
                const pos = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
                if (player.duration) {{
                    player.currentTime = pos * player.duration;
                }}
            }}

            scrubContainer.addEventListener('mousedown', (e) => {{
                isScrubDragging = true;
                scrubContainer.classList.add('is-dragging');
                scrubAtEvent(e);
            }});

            window.addEventListener('mousemove', (e) => {{
                if (isScrubDragging) {{
                    scrubAtEvent(e);
                }}
            }});

            window.addEventListener('mouseup', () => {{
                if (isScrubDragging) {{
                    isScrubDragging = false;
                    scrubContainer.classList.remove('is-dragging');
                    captureCurrentVideoPosition();
                }}
            }});

            scrubContainer.addEventListener('mousemove', (e) => {{
                const rect = scrubContainer.getBoundingClientRect();
                const pos = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
                if (player.duration && scrubTooltip) {{
                    scrubTooltip.style.display = 'block';

                    // Clamp horizontally so 200px preview thumbnail stays strictly within player bounds
                    const tooltipHalfWidth = 104;
                    const mouseX = e.clientX - rect.left;
                    const clampedX = Math.max(tooltipHalfWidth, Math.min(rect.width - tooltipHalfWidth, mouseX));
                    scrubTooltip.style.left = `${{clampedX}}px`;

                    const hoverTime = pos * player.duration;
                    const timeElem = document.getElementById('ytTooltipTime');
                    if (timeElem) timeElem.textContent = formatTime(hoverTime);

                    const thumbElem = document.getElementById('ytThumbPreview');
                    if (activeStoryboard && thumbElem) {{
                        thumbElem.style.display = 'block';
                        const totalFrames = activeStoryboard.total_frames || 100;
                        const cols = activeStoryboard.cols || 10;
                        const rows = activeStoryboard.rows || 10;
                        const boxW = 200;
                        const boxH = 112;

                        const frameIdx = Math.min(totalFrames - 1, Math.max(0, Math.floor((hoverTime / player.duration) * totalFrames)));
                        const col = frameIdx % cols;
                        const row = Math.floor(frameIdx / cols);

                        thumbElem.style.backgroundImage = `url('${{activeStoryboard.sprite_url}}')`;
                        thumbElem.style.backgroundSize = `${{cols * boxW}}px ${{rows * boxH}}px`;
                        thumbElem.style.backgroundPosition = `-${{col * boxW}}px -${{row * boxH}}px`;
                    }} else if (thumbElem) {{
                        thumbElem.style.display = 'none';
                    }}
                }}
            }});

            scrubContainer.addEventListener('mouseleave', () => {{
                if (scrubTooltip) scrubTooltip.style.display = 'none';
            }});

            // Volume Slider & Mouse Wheel Volume Control
            function changeVolumeBy(delta) {{
                let newVol = Math.max(0, Math.min(1, player.volume + delta));
                newVol = Math.round(newVol * 100) / 100;
                player.volume = newVol;
                if (newVol > 0 && player.muted) {{
                    player.muted = false;
                }} else if (newVol === 0) {{
                    player.muted = true;
                }}
                savePlayerVolume(player.volume, player.muted);
                updateVolumeUI();
                flashCenterFeedback(`Vol ${{Math.round(player.volume * 100)}}%`);
                resetControlsTimer();
            }}

            function handleVolumeWheel(e) {{
                e.preventDefault();
                e.stopPropagation();
                const step = 0.05;
                const delta = e.deltaY < 0 ? step : -step;
                changeVolumeBy(delta);
            }}

            if (volSlider) {{
                volSlider.addEventListener('input', (e) => {{
                    player.volume = parseFloat(e.target.value);
                    player.muted = false;
                    savePlayerVolume(player.volume, player.muted);
                    updateVolumeUI();
                }});
                volSlider.addEventListener('wheel', handleVolumeWheel, {{ passive: false }});
            }}

            const volGroup = container.querySelector('.yt-volume-group');
            if (volGroup) {{
                volGroup.addEventListener('wheel', handleVolumeWheel, {{ passive: false }});
            }}

            const muteBtn = document.getElementById('ytMuteBtn');
            if (muteBtn) {{
                muteBtn.addEventListener('wheel', handleVolumeWheel, {{ passive: false }});
            }}

            // Mouse wheel on the player container:
            // When in floating mini player mode: scrolling anywhere on the mini popup controls volume.
            // When in main player mode: scrolling over the volume controls adjusts volume.
            container.addEventListener('wheel', (e) => {{
                if (isFloatingMini) {{
                    handleVolumeWheel(e);
                }} else if (e.target.closest('.yt-volume-group') || e.target.closest('.yt-volume-slider-wrap')) {{
                    handleVolumeWheel(e);
                }}
            }}, {{ passive: false }});

            // Canvas Click / Double-Click Interactions
            player.addEventListener('click', (e) => {{
                if (clickTimeout) {{
                    clearTimeout(clickTimeout);
                    clickTimeout = null;
                    // Double-click detected: check left vs right half
                    const rect = player.getBoundingClientRect();
                    const x = e.clientX - rect.left;
                    if (x < rect.width / 2) {{
                        seekRelative(-10);
                    }} else {{
                        seekRelative(10);
                    }}
                }} else {{
                    clickTimeout = setTimeout(() => {{
                        clickTimeout = null;
                        togglePlayPause();
                    }}, 240);
                }}
            }});

            // Auto-hide controls on mouse idle
            container.addEventListener('mousemove', resetControlsTimer);
            container.addEventListener('click', resetControlsTimer);

            // YouTube Keyboard Shortcuts
            window.addEventListener('keydown', (e) => {{
                const box = document.getElementById('playerBox');
                if (!box || box.style.display === 'none') return;
                // Ignore shortcuts if user is typing in search or edit inputs
                const tag = (e.target.tagName || '').toLowerCase();
                if (tag === 'input' || tag === 'textarea' || tag === 'select') return;

                const key = e.key.toLowerCase();

                if (key === ' ' || key === 'k') {{
                    e.preventDefault();
                    togglePlayPause();
                }} else if (key === 'arrowleft') {{
                    e.preventDefault();
                    seekRelative(-5);
                }} else if (key === 'arrowright') {{
                    e.preventDefault();
                    seekRelative(5);
                }} else if (key === 'j') {{
                    e.preventDefault();
                    seekRelative(-10);
                }} else if (key === 'l') {{
                    e.preventDefault();
                    seekRelative(10);
                }} else if (key === 'arrowup') {{
                    e.preventDefault();
                    player.volume = Math.min(1, player.volume + 0.05);
                    player.muted = false;
                    updateVolumeUI();
                    flashCenterFeedback(`Vol ${{Math.round(player.volume * 100)}}%`);
                }} else if (key === 'arrowdown') {{
                    e.preventDefault();
                    player.volume = Math.max(0, player.volume - 0.05);
                    updateVolumeUI();
                    flashCenterFeedback(`Vol ${{Math.round(player.volume * 100)}}%`);
                }} else if (key === 'm') {{
                    e.preventDefault();
                    toggleMute();
                    flashCenterFeedback(player.muted ? 'Muted' : 'Unmuted');
                }} else if (key === 'c') {{
                    e.preventDefault();
                    toggleSubtitles();
                }} else if (key === 'f') {{
                    e.preventDefault();
                    toggleFullscreen();
                }} else if (key === 't') {{
                    e.preventDefault();
                    toggleTheaterMode();
                }} else if (key === 'p') {{
                    e.preventDefault();
                    togglePiP();
                }} else if (key === 's') {{
                    e.preventDefault();
                    toggleStatsForNerds();
                }} else if (key >= '0' && key <= '9') {{
                    e.preventDefault();
                    if (player.duration) {{
                        const targetPct = parseInt(key) / 10;
                        player.currentTime = targetPct * player.duration;
                        flashCenterFeedback(`${{targetPct * 100}}%`);
                    }}
                }} else if (key === '<' || key === ',') {{
                    e.preventDefault();
                    const rates = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2];
                    const idx = Math.max(0, rates.indexOf(player.playbackRate) - 1);
                    setSpeed(rates[idx]);
                }} else if (key === '>' || key === '.') {{
                    e.preventDefault();
                    const rates = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2];
                    const idx = Math.min(rates.length - 1, rates.indexOf(player.playbackRate) + 1);
                    setSpeed(rates[idx]);
                }} else if (key === 'escape') {{
                    const menu = document.getElementById('ytSpeedMenu');
                    const stats = document.getElementById('ytStatsPanel');
                    if (menu && menu.style.display === 'block') {{
                        menu.style.display = 'none';
                    }} else if (stats && stats.style.display === 'block') {{
                        stats.style.display = 'none';
                    }} else if (!document.fullscreenElement) {{
                        closePlayer();
                    }}
                }}
            }});
        }}

        /* Edit Metadata Functions */
        function cleanTitleExtension(str) {{
            if (!str) return '';
            let clean = String(str).trim();
            clean = clean.replace(/\.(mp4|mkv|avi|mov|flv|webm|ts|m4a|wmv|strm|nfo)$/i, '');
            clean = clean.replace(/\.(mp4|mkv|avi|mov|flv|webm|ts|m4a|wmv|strm|nfo)$/i, '');
            return clean.trim();
        }}

        function setEditTitle(name) {{
            const input = document.getElementById('editTitle');
            if (input) {{
                input.value = name;
                input.focus();
                input.select();
                input.style.borderColor = '#10b981';
                input.style.boxShadow = '0 0 10px rgba(16, 185, 129, 0.4)';
                setTimeout(() => {{
                    input.style.borderColor = '';
                    input.style.boxShadow = '';
                }}, 800);
                showToast(`Title set to: "${{name}}". Edit freely to fix typos.`, 'info');
                const key = document.getElementById('editKey').value;
                const file = allFiles.find(f => f.key === key);
                if (file) {{
                    updateCandidateTitlePicker(file, key);
                }}
            }}
        }}

        function cleanCurrentEditTitle() {{
            const input = document.getElementById('editTitle');
            if (input && input.value) {{
                let cleaned = cleanTitleExtension(input.value);
                cleaned = cleaned.replace(/[_]+/g, ' ').replace(/\s+/g, ' ').trim();
                input.value = cleaned;
                input.focus();
                showToast('Cleaned current title', 'info');
            }}
        }}

        function updateCandidateTitlePicker(file, key) {{
            const box = document.getElementById('aliasTitlePickerBox');
            const list = document.getElementById('aliasTitlePickerList');
            if (!box || !list) return;

            const meta = file.metadata || {{}};
            const candidateNames = [];
            const seenNames = new Set();
            function addCandidate(raw) {{
                if (!raw) return;
                const cleaned = cleanTitleExtension(raw);
                if (cleaned && !seenNames.has(cleaned.toLowerCase())) {{
                    seenNames.add(cleaned.toLowerCase());
                    candidateNames.push(cleaned);
                }}
            }}

            if (meta.title) addCandidate(meta.title);
            const keyStem = key.split('/').pop().split('\\').pop();
            addCandidate(keyStem);

            (file.aliases || []).forEach(a => addCandidate(a));
            (file.file_names || []).forEach(fn => addCandidate(fn));

            const curVal = (document.getElementById('editTitle').value || '').trim();
            if (candidateNames.length > 0) {{
                box.style.display = 'block';
                list.innerHTML = candidateNames.map(n => {{
                    const isCurrent = (n.toLowerCase() === curVal.toLowerCase());
                    return `
                    <button type="button" class="btn-use-alias-title ${{isCurrent ? 'is-active-title' : ''}}" 
                            onclick="setEditTitle('${{escapeHtml(n)}}')" 
                            title="Click to set this name as the title (without extension)">
                        ${{isCurrent ? '&#10003;' : '&#9998;'}} ${{escapeHtml(n)}}
                    </button>`;
                }}).join('');
            }} else {{
                box.style.display = 'none';
            }}
        }}

        async function promptRenameTag(oldTag, event) {{
            if (event) event.stopPropagation();
            const newTag = prompt(`Rename tag "${{oldTag}}" across all videos in your library to:`, oldTag);
            if (!newTag || newTag.trim() === '' || newTag.trim() === oldTag) return;
            const cleanNew = newTag.trim();

            try {{
                showToast(`Renaming tag "${{oldTag}}" to "${{cleanNew}}"...`, 'info');
                const resp = await fetch('/api/tags/rename', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ old_tag: oldTag, new_tag: cleanNew }})
                }});
                const res = await resp.json();
                if (res.success) {{
                    showToast(res.message || `Renamed tag to "${{cleanNew}}"`, 'success');
                    allFiles.forEach(f => {{
                        if (f.metadata && f.metadata.tags) {{
                            f.metadata.tags = f.metadata.tags.map(t => (t.toLowerCase() === oldTag.toLowerCase() ? cleanNew : t));
                            f.metadata.tags = Array.from(new Set(f.metadata.tags));
                        }}
                    }});
                    if (hiddenTagsSet.has(oldTag)) {{
                        hiddenTagsSet.delete(oldTag);
                        hiddenTagsSet.add(cleanNew);
                        saveHiddenTags();
                    }}
                    if (selectedTags.has(oldTag)) {{
                        selectedTags.delete(oldTag);
                        selectedTags.add(cleanNew);
                    }}
                    populateFilterDropdowns();
                    refreshTreeDisplay();
                }} else {{
                    alert(res.message || 'Error renaming tag');
                }}
            }} catch (e) {{
                alert('Rename tag failed: ' + e);
            }}
        }}

        let mergeSelectedTags = new Set();

        function openMergeTagsModal(event) {{
            if (event) event.stopPropagation();
            closeAllPopovers();
            mergeSelectedTags = new Set(selectedTags);
            renderMergeTagList();
            const input = document.getElementById('mergeTargetTagInput');
            if (input) input.value = '';
            updateMergeTargetSuggestions();
            updateMergeBtnState();
            document.getElementById('mergeTagsModal').style.display = 'flex';
        }}

        function closeMergeTagsModal() {{
            document.getElementById('mergeTagsModal').style.display = 'none';
        }}

        function renderMergeTagList(filterText = '') {{
            const container = document.getElementById('mergeTagList');
            if (!container) return;

            const tagCounts = {{}};
            allFiles.forEach(f => {{
                (f.metadata?.tags || []).forEach(t => {{
                    const st = String(t).trim();
                    if (st) tagCounts[st] = (tagCounts[st] || 0) + 1;
                }});
            }});

            const ft = filterText.toLowerCase().trim();
            const tags = Object.keys(tagCounts).sort((a, b) => a.localeCompare(b)).filter(t => {{
                return !ft || t.toLowerCase().includes(ft);
            }});

            if (tags.length === 0) {{
                container.innerHTML = '<div style="color: #6b7280; font-size: 0.78rem; padding: 8px;">No tags found</div>';
                return;
            }}

            container.innerHTML = tags.map(t => {{
                const checked = mergeSelectedTags.has(t);
                return `
                <label style="display: flex; align-items: center; justify-content: space-between; padding: 4px 8px; border-radius: 4px; cursor: pointer; user-select: none; ${{checked ? 'background: rgba(147, 51, 234, 0.22);' : ''}}">
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <input type="checkbox" ${{checked ? 'checked' : ''}} onchange="toggleMergeTagItem('${{escapeHtml(t)}}', this.checked)">
                        <span style="font-size: 0.82rem; color: #f3f4f6;">${{escapeHtml(t)}}</span>
                    </div>
                    <span style="font-size: 0.72rem; color: #9ca3af;">${{tagCounts[t]}} video(s)</span>
                </label>`;
            }}).join('');

            const countEl = document.getElementById('mergeSelectedCount');
            if (countEl) countEl.textContent = `${{mergeSelectedTags.size}} selected`;
        }}

        function filterMergeTagList(val) {{
            renderMergeTagList(val);
        }}

        function toggleMergeTagItem(tag, isChecked) {{
            if (isChecked) {{
                mergeSelectedTags.add(tag);
            }} else {{
                mergeSelectedTags.delete(tag);
            }}
            const countEl = document.getElementById('mergeSelectedCount');
            if (countEl) countEl.textContent = `${{mergeSelectedTags.size}} selected`;
            updateMergeTargetSuggestions();
            updateMergeBtnState();
        }}

        function updateMergeTargetSuggestions() {{
            const sugContainer = document.getElementById('mergeTargetSuggestions');
            if (!sugContainer) return;
            if (mergeSelectedTags.size === 0) {{
                sugContainer.innerHTML = '';
                return;
            }}
            sugContainer.innerHTML = Array.from(mergeSelectedTags).map(t => `
                <button type="button" class="tool-btn btn-xs" onclick="setMergeTargetName('${{escapeHtml(t)}}')" title="Use '${{escapeHtml(t)}}' as the target merged tag name" style="padding: 2px 8px; font-size: 0.72rem;">
                    Use "${{escapeHtml(t)}}"
                </button>
            `).join('');
        }}

        function setMergeTargetName(name) {{
            const input = document.getElementById('mergeTargetTagInput');
            if (input) {{
                input.value = name;
                updateMergeBtnState();
            }}
        }}

        function updateMergeBtnState() {{
            const btn = document.getElementById('btnSubmitMergeTags');
            const input = document.getElementById('mergeTargetTagInput');
            if (!btn || !input) return;
            const hasTarget = input.value.trim().length > 0;
            const hasSources = mergeSelectedTags.size >= 1;
            btn.disabled = !(hasTarget && hasSources);
        }}

        async function submitMergeTags() {{
            const input = document.getElementById('mergeTargetTagInput');
            const targetTag = input ? input.value.trim() : '';
            const sourceTags = Array.from(mergeSelectedTags);
            if (!targetTag || sourceTags.length === 0) return;

            if (!confirm(`Merge ${{sourceTags.length}} tag(s):\n\n[ ${{sourceTags.join(', ')}} ]\n\ninto single tag "${{targetTag}}"?\n\nThis will update all videos in upload_manifest.json and rewrite Jellyfin .nfo files.`)) {{
                return;
            }}

            try {{
                showToast(`Merging ${{sourceTags.length}} tags into "${{targetTag}}"...`, 'info');
                const resp = await fetch('/api/tags/merge', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ source_tags: sourceTags, target_tag: targetTag }})
                }});
                const res = await resp.json();
                if (res.success) {{
                    showToast(res.message || `Successfully merged tags into "${{targetTag}}"`, 'success');
                    closeMergeTagsModal();
                    const srcLowers = new Set(sourceTags.map(st => st.toLowerCase()));
                    allFiles.forEach(f => {{
                        if (f.metadata && f.metadata.tags) {{
                            const hadAny = f.metadata.tags.some(t => srcLowers.has(t.toLowerCase()));
                            if (hadAny) {{
                                f.metadata.tags = f.metadata.tags.filter(t => !srcLowers.has(t.toLowerCase()));
                                if (!f.metadata.tags.includes(targetTag)) {{
                                    f.metadata.tags.push(targetTag);
                                }}
                            }}
                        }}
                    }});
                    sourceTags.forEach(st => {{
                        hiddenTagsSet.delete(st);
                        selectedTags.delete(st);
                    }});
                    saveHiddenTags();
                    populateFilterDropdowns();
                    refreshTreeDisplay();
                }} else {{
                    alert(res.message || 'Error merging tags');
                }}
            }} catch (e) {{
                alert('Merge tags request failed: ' + e);
            }}
        }}

        function promptEditMetadata(key) {{
            const file = allFiles.find(f => f.key === key);
            if (!file) return;

            editingKey = key;
            const meta = file.metadata || {{}};

            document.getElementById('editKey').value = key;
            document.getElementById('editTitle').value = meta.title || "";
            document.getElementById('editYear').value = meta.year || "";
            document.getElementById('editRating').value = meta.rating || "";
            document.getElementById('editGenres').value = (meta.genres || []).join(", ");
            document.getElementById('editTags').value = (meta.tags || []).join(", ");
            document.getElementById('editOverview').value = meta.overview || meta.plot || "";

            const aliases = file.aliases || meta.aliases || [key];
            const aliasesBox = document.getElementById('editAliasesBox');
            if (aliasesBox) {{
                aliasesBox.style.display = 'block';
                renderEditAliases(key, aliases);
            }}

            updateCandidateTitlePicker(file, key);

            renderEditSubtitles(key, file.subtitles || []);
            resetSubUploadForm();

            document.getElementById('metadataModal').style.display = 'flex';
        }}



        /* Subtitles & Audio Mode Engine */
        let currentSubLang = 'off';
        let isAudioMode = false;
        let pickedSubFile = null;

        function updateSubtitleTracks(key) {{
            const player = document.getElementById('videoPlayer');
            const trackList = document.getElementById('ytSubTrackList');
            const topTrackList = document.getElementById('ytTopSubTrackList');
            const allCcBtns = document.querySelectorAll('.yt-cc-btn');
            if (!player) return;

            // Remove existing track elements
            const oldTracks = player.querySelectorAll('track');
            oldTracks.forEach(t => t.remove());

            const file = allFiles.find(f => f.key === key);
            const subs = (file && file.subtitles) ? file.subtitles : [];

            if (!subs || subs.length === 0) {{
                allCcBtns.forEach(btn => {{
                    btn.style.opacity = '0.4';
                    btn.title = 'No Subtitles Available (Click to add)';
                    btn.classList.remove('active');
                }});
                const emptyTracksHtml = `
                    <div class="yt-sub-option active" onclick="setSubtitleTrack('off')">Off</div>
                    <div style="padding: 6px 14px; font-size: 0.75rem; color: var(--text-muted); font-style: italic;">No tracks uploaded</div>
                `;
                if (trackList) trackList.innerHTML = emptyTracksHtml;
                if (topTrackList) topTrackList.innerHTML = emptyTracksHtml;
                return;
            }}

            allCcBtns.forEach(btn => {{
                btn.style.opacity = '1';
                btn.title = 'Subtitles / Closed Captions (c)';
            }});

            let tracksHtml = `<div class="yt-sub-option active" id="subOpt-off" onclick="setSubtitleTrack('off')">Off</div>`;

            subs.forEach(s => {{
                const lang = s.lang || 'en';
                const label = s.label || lang.toUpperCase();
                
                const track = document.createElement('track');
                track.kind = 'subtitles';
                track.label = label;
                track.srclang = lang;
                track.src = `/stream/subtitle?key=${{encodeURIComponent(key)}}&lang=${{encodeURIComponent(lang)}}`;
                track.default = false;
                player.appendChild(track);

                tracksHtml += `<div class="yt-sub-option" id="subOpt-${{lang}}" onclick="setSubtitleTrack('${{lang}}')">${{escapeHtml(label)}} (${{escapeHtml(lang)}})</div>`;
            }});

            if (trackList) {{
                trackList.innerHTML = tracksHtml;
            }}
            if (topTrackList) {{
                topTrackList.innerHTML = tracksHtml;
            }}

            let preferredLang = 'off';
            try {{
                preferredLang = localStorage.getItem('hfsync_sub_' + key) || localStorage.getItem('hfsync_sub_global') || 'off';
            }} catch (e) {{}}

            const matchingSub = subs.find(s => s.lang === preferredLang || (s.label && s.label.toLowerCase() === preferredLang.toLowerCase()));
            if (preferredLang !== 'off' && matchingSub) {{
                setSubtitleTrack(matchingSub.lang);
            }} else {{
                setSubtitleTrack('off');
            }}
        }}

        function setSubtitleTrack(lang) {{
            const player = document.getElementById('videoPlayer');
            const menu = document.getElementById('ytSubMenu');
            const topMenu = document.getElementById('ytTopSubMenu');
            currentSubLang = lang;

            if (currentVideoKey) {{
                try {{
                    localStorage.setItem('hfsync_sub_' + currentVideoKey, lang);
                    if (lang !== 'off') localStorage.setItem('hfsync_sub_global', lang);
                }} catch (e) {{}}
            }}

            if (player && player.textTracks) {{
                for (let i = 0; i < player.textTracks.length; i++) {{
                    const t = player.textTracks[i];
                    if (lang === 'off') {{
                        t.mode = 'disabled';
                    }} else if (t.language === lang || t.label.toLowerCase() === lang.toLowerCase()) {{
                        t.mode = 'showing';
                    }} else {{
                        t.mode = 'disabled';
                    }}
                }}
            }}

            document.querySelectorAll('.yt-cc-btn').forEach(btn => {{
                if (lang === 'off') {{
                    btn.classList.remove('active');
                }} else {{
                    btn.classList.add('active');
                }}
            }});

            if (menu) menu.style.display = 'none';
            if (topMenu) topMenu.style.display = 'none';

            document.querySelectorAll('.yt-sub-option').forEach(opt => {{
                opt.classList.remove('active');
            }});
            document.querySelectorAll(`#subOpt-${{lang}}`).forEach(opt => {{
                opt.classList.add('active');
            }});
            flashCenterFeedback(lang === 'off' ? 'Subtitles: Off' : `Subtitles: ${{lang.toUpperCase()}}`);
        }}

        function toggleSubtitlesMenu() {{
            const menu = document.getElementById('ytSubMenu');
            if (!menu) return;
            const speedMenu = document.getElementById('ytSpeedMenu');
            if (speedMenu) speedMenu.style.display = 'none';
            menu.style.display = (menu.style.display === 'block') ? 'none' : 'block';
        }}

        function toggleTopSubMenu() {{
            const topMenu = document.getElementById('ytTopSubMenu');
            if (!topMenu) return;
            const isVisible = (topMenu.style.display === 'block');
            topMenu.style.display = isVisible ? 'none' : 'block';
        }}

        function handleCcButtonClick(event, btn) {{
            if (event) event.stopPropagation();
            const file = allFiles.find(f => f.key === currentVideoKey);
            const subs = (file && file.subtitles) ? file.subtitles : [];

            // If no subtitles exist or multiple exist: show menu
            if (!subs || subs.length !== 1) {{
                toggleTopSubMenu();
                return;
            }}

            // If exactly 1 subtitle track exists: toggle ON / OFF directly
            toggleSubtitles();
        }}

        function openSubtitlesMenuFromBtn(event, btn) {{
            if (event) {{
                event.preventDefault();
                event.stopPropagation();
            }}
            toggleTopSubMenu();
        }}

        function toggleSubtitles() {{
            const player = document.getElementById('videoPlayer');
            if (!player || !player.textTracks || player.textTracks.length === 0) {{
                toggleTopSubMenu();
                return;
            }}
            if (currentSubLang === 'off') {{
                const firstLang = player.textTracks[0].language || 'en';
                setSubtitleTrack(firstLang);
            }} else {{
                setSubtitleTrack('off');
            }}
        }}

        function toggleAudioMode() {{
            const visualizer = document.getElementById('ytAudioVisualizer');
            const btn = document.getElementById('ytAudioModeBtn');
            const titleEl = document.getElementById('ytAudioTrackTitle');
            const playerTitle = document.getElementById('playerTitle');

            isAudioMode = !isAudioMode;
            if (isAudioMode) {{
                if (visualizer) visualizer.style.display = 'flex';
                if (btn) btn.classList.add('active');
                if (titleEl && playerTitle) titleEl.textContent = playerTitle.textContent;
                flashCenterFeedback('🎵 Audio Mode ON');
            }} else {{
                if (visualizer) visualizer.style.display = 'none';
                if (btn) btn.classList.remove('active');
                flashCenterFeedback('🎬 Video Mode ON');
            }}
        }}

        function resetAudioMode() {{
            isAudioMode = false;
            const visualizer = document.getElementById('ytAudioVisualizer');
            const btn = document.getElementById('ytAudioModeBtn');
            if (visualizer) visualizer.style.display = 'none';
            if (btn) btn.classList.remove('active');
        }}

        function openUploadSubModalFromPlayer() {{
            const menu = document.getElementById('ytSubMenu');
            if (menu) menu.style.display = 'none';
            if (currentVideoKey) {{
                promptEditMetadata(currentVideoKey);
                setTimeout(() => {{
                    const sec = document.getElementById('editSubtitlesSection');
                    if (sec) sec.scrollIntoView({{ behavior: 'smooth' }});
                }}, 100);
            }}
        }}

        function renderEditSubtitles(fileKey, subtitles) {{
            const listEl = document.getElementById('editSubtitlesList');
            if (!listEl) return;
            const subs = Array.isArray(subtitles) ? subtitles : [];
            if (subs.length === 0) {{
                listEl.innerHTML = '<span style="font-size: 0.78rem; color: #6b7280; font-style: italic;">No subtitle tracks uploaded</span>';
                return;
            }}
            listEl.innerHTML = subs.map(s => `
                <div class="sub-track-item">
                    <div style="display: flex; align-items: center; gap: 8px;">
                        <span class="sub-track-lang">${{escapeHtml(s.lang || 'cc')}}</span>
                        <span style="font-weight: 500; color: #e2e8f0;">${{escapeHtml(s.label || s.lang)}}</span>
                        <span style="font-size: 0.72rem; color: #9ca3af;">(${{s.orig_size ? (s.orig_size/1024).toFixed(1) + ' KB' : (s.size ? (s.size/1024).toFixed(1) + ' KB' : '')}})</span>
                    </div>
                    <div style="display: flex; align-items: center; gap: 6px;">
                        <a href="/stream/subtitle?key=${{encodeURIComponent(fileKey)}}&lang=${{encodeURIComponent(s.lang)}}" target="_blank" class="tool-btn btn-xs" style="text-decoration: none; padding: 2px 8px; font-size: 0.72rem;" title="Preview / Download WebVTT">&#128065; Preview</a>
                        <button type="button" class="btn btn-delete btn-xs" onclick="promptDeleteSubtitle(event, '${{escapeHtml(fileKey)}}', '${{escapeHtml(s.lang)}}')" style="padding: 2px 8px; font-size: 0.72rem;" title="Delete subtitle track">&times; Delete</button>
                    </div>
                </div>
            `).join('');
        }}

        function handleModalSubFilePicked(input) {{
            const file = input.files && input.files[0];
            const nameEl = document.getElementById('modalSubFileName');
            const btn = document.getElementById('btnUploadModalSub');
            const langSelect = document.getElementById('modalSubLangSelect');

            if (!file) {{
                pickedSubFile = null;
                if (nameEl) nameEl.textContent = 'No file chosen';
                if (btn) btn.disabled = true;
                return;
            }}

            pickedSubFile = file;
            if (nameEl) nameEl.textContent = `${{file.name}} (${{(file.size / 1024).toFixed(1)}} KB)`;
            if (btn) btn.disabled = false;

            const fname = file.name.toLowerCase();
            const parts = fname.split('.');
            if (parts.length >= 2) {{
                const possibleLang = parts[parts.length - 2];
                if (langSelect) {{
                    for (let opt of langSelect.options) {{
                        if (opt.value === possibleLang) {{
                            langSelect.value = possibleLang;
                            break;
                        }}
                    }}
                }}
            }}
        }}

        function resetSubUploadForm() {{
            pickedSubFile = null;
            const input = document.getElementById('modalSubFileInput');
            if (input) input.value = '';
            const nameEl = document.getElementById('modalSubFileName');
            if (nameEl) nameEl.textContent = 'No file chosen';
            const btn = document.getElementById('btnUploadModalSub');
            if (btn) {{
                btn.disabled = true;
                btn.textContent = '+ Upload Subtitle';
            }}
        }}

        async function submitUploadSubtitle() {{
            if (!pickedSubFile || !editingKey) return;
            const langSelect = document.getElementById('modalSubLangSelect');
            const lang = langSelect ? langSelect.value : 'en';
            const btn = document.getElementById('btnUploadModalSub');

            if (btn) {{
                btn.disabled = true;
                btn.textContent = 'Encrypting & Uploading...';
            }}

            try {{
                const textContent = await pickedSubFile.text();
                const resp = await fetch('/api/subtitles/upload', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        key: editingKey,
                        content: textContent,
                        lang: lang,
                        filename: pickedSubFile.name
                    }})
                }});
                const res = await resp.json();
                if (res.success) {{
                    showToast(res.message || 'Subtitle uploaded successfully!', 'success');
                    const file = allFiles.find(f => f.key === editingKey);
                    if (file) {{
                        file.subtitles = res.subtitles;
                    }}
                    renderEditSubtitles(editingKey, res.subtitles);
                    resetSubUploadForm();
                    updateSubtitleTracks(editingKey);
                    filterTree();
                }} else {{
                    alert('Upload failed: ' + (res.message || 'Unknown error'));
                }}
            }} catch (err) {{
                alert('Subtitle upload failed: ' + err);
            }} finally {{
                if (btn) {{
                    btn.disabled = false;
                    btn.textContent = '+ Upload Subtitle';
                }}
            }}
        }}

        async function promptDeleteSubtitle(event, fileKey, lang) {{
            if (event) event.stopPropagation();
            if (!confirm(`Delete ${{lang.toUpperCase()}} subtitle track for this video?\n\nThis will remove it from Hugging Face and your local Jellyfin library.`)) {{
                return;
            }}

            try {{
                const resp = await fetch('/api/subtitles/delete', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ key: fileKey, lang: lang }})
                }});
                const res = await resp.json();
                if (res.success) {{
                    showToast(res.message || 'Subtitle deleted', 'success');
                    const file = allFiles.find(f => f.key === fileKey);
                    if (file) {{
                        file.subtitles = res.subtitles;
                    }}
                    renderEditSubtitles(fileKey, res.subtitles);
                    updateSubtitleTracks(fileKey);
                    filterTree();
                }} else {{
                    alert('Delete failed: ' + (res.message || 'Unknown error'));
                }}
            }} catch (err) {{
                alert('Subtitle delete failed: ' + err);
            }}
        }}

        function renderEditAliases(fileKey, aliases) {{
            const listEl = document.getElementById('editAliasesList');
            if (!listEl) return;
            const aliasArr = Array.isArray(aliases) ? aliases : [];
            if (aliasArr.length === 0) {{
                listEl.innerHTML = '<span style="font-size: 0.78rem; color: #6b7280; font-style: italic;">No filenames registered</span>';
                return;
            }}
            listEl.innerHTML = aliasArr.map(a => {{
                const cleanName = cleanTitleExtension(a);
                return `
                <span class="alias-pill">
                    &#128206; <span>${{escapeHtml(a)}}</span>
                    <button type="button" class="alias-use-title-btn" onclick="setEditTitle('${{escapeHtml(cleanName)}}')" title="Set this filename as Title (without extension)">&#9998; Use as Title</button>
                    <button type="button" class="alias-del-btn" onclick="promptRemoveAlias(event, '${{escapeHtml(fileKey)}}', '${{escapeHtml(a)}}')" title="Remove this unsuitable filename">&times;</button>
                </span>`;
            }}).join('');
        }}

        let aliasActionInProgress = false;

        async function promptRemoveAlias(event, fileKey, aliasName) {{
            if (event) event.stopPropagation();
            if (aliasActionInProgress) return;

            const confirmed = confirm(`Remove unsuitable filename:\n\n"${{aliasName}}"\n\nThis will remove its Jellyfin pointer (.strm & .nfo). The video and any other filenames will be kept.`);
            if (!confirmed) return;

            aliasActionInProgress = true;
            try {{
                const resp = await fetch('/api/aliases/remove', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ key: fileKey, alias: aliasName }})
                }});
                const res = await resp.json();
                if (res.success) {{
                    showToast(res.message || `Removed filename ${{aliasName}}`, 'success');
                    if (FILE_CACHE && FILE_CACHE[fileKey]) {{
                        FILE_CACHE[fileKey].aliases = res.aliases;
                        FILE_CACHE[fileKey].file_names = res.file_names;
                    }}
                    renderEditAliases(res.key || fileKey, res.aliases);
                    refreshFiles();
                }} else {{
                    alert(res.message || 'Error removing filename');
                }}
            }} catch (e) {{
                alert('Request failed: ' + e);
            }} finally {{
                aliasActionInProgress = false;
            }}
        }}

        async function submitAddAlias() {{
            const input = document.getElementById('newAliasInput');
            if (!input || !editingKey || aliasActionInProgress) return;
            const newName = input.value.trim();
            if (!newName) {{
                input.focus();
                return;
            }}

            aliasActionInProgress = true;
            try {{
                const resp = await fetch('/api/aliases/add', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ key: editingKey, alias: newName }})
                }});
                const res = await resp.json();
                if (res.success) {{
                    input.value = '';
                    showToast(res.message || `Added filename ${{res.added_alias}}`, 'success');
                    if (FILE_CACHE && FILE_CACHE[editingKey]) {{
                        FILE_CACHE[editingKey].aliases = res.aliases;
                        FILE_CACHE[editingKey].file_names = res.file_names;
                    }}
                    renderEditAliases(res.key || editingKey, res.aliases);
                    refreshFiles();
                }} else {{
                    alert(res.message || 'Error adding filename');
                }}
            }} catch (e) {{
                alert('Request failed: ' + e);
            }} finally {{
                aliasActionInProgress = false;
            }}
        }}

        function closeMetadataModal() {{
            document.getElementById('metadataModal').style.display = 'none';
            editingKey = null;
        }}

        function addGenreChip(genre) {{
            const input = document.getElementById('editGenres');
            const existing = input.value.split(',').map(s => s.trim()).filter(Boolean);
            if (!existing.includes(genre)) {{
                existing.push(genre);
                input.value = existing.join(', ');
            }}
        }}

        function addTagChip(tag) {{
            const input = document.getElementById('editTags');
            const existing = input.value.split(',').map(s => s.trim()).filter(Boolean);
            if (!existing.includes(tag)) {{
                existing.push(tag);
                input.value = existing.join(', ');
            }}
        }}

        async function saveMetadata() {{
            if (!editingKey) return;
            const saveBtn = document.getElementById('saveMetadataBtn');
            const origText = saveBtn.textContent;
            saveBtn.textContent = "Saving to Jellyfin...";
            saveBtn.disabled = true;

            const title = document.getElementById('editTitle').value.trim();
            const year = document.getElementById('editYear').value.trim();
            const rating = document.getElementById('editRating').value.trim();
            const overview = document.getElementById('editOverview').value.trim();
            const genres = document.getElementById('editGenres').value.split(',').map(s => s.trim()).filter(Boolean);
            const tags = document.getElementById('editTags').value.split(',').map(s => s.trim()).filter(Boolean);

            const updatedMetadata = {{
                title: title,
                year: year,
                rating: rating,
                overview: overview,
                genres: genres,
                tags: tags
            }};

            try {{
                const res = await fetch('/api/metadata', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        key: editingKey,
                        metadata: updatedMetadata
                    }})
                }});
                const result = await res.json();
                if (result && result.success) {{
                    const f = allFiles.find(item => item.key === editingKey);
                    if (f) {{
                        f.metadata = result.metadata;
                    }}
                    populateFilterDropdowns();
                    filterTree();
                    closeMetadataModal();
                    showToast("✅ Jellyfin metadata & .nfo saved successfully!");
                }} else {{
                    alert("Error updating metadata: " + ((result && result.message) || "Unknown error"));
                }}
            }} catch (err) {{
                alert("Failed to connect to server: " + err);
            }} finally {{
                saveBtn.textContent = origText;
                saveBtn.disabled = false;
            }}
        }}

        async function syncMetadataToHf() {{
            const btn = document.getElementById('syncHfBtn');
            const origText = btn.innerHTML;
            btn.innerHTML = "&#9729;&#65039; Syncing...";
            btn.disabled = true;

            try {{
                const res = await fetch('/api/sync-hf', {{ method: 'POST' }});
                const result = await res.json();
                if (result && result.success) {{
                    showToast("☁️ " + result.message);
                }} else {{
                    alert("Error syncing metadata: " + ((result && result.message) || "Unknown error"));
                }}
            }} catch (err) {{
                alert("Failed to communicate with server: " + err);
            }} finally {{
                btn.innerHTML = origText;
                btn.disabled = false;
            }}
        }}

        /* Skipped / Incompatible Content Management */
        function openSkippedModal() {{
            renderSkippedList();
            document.getElementById('skippedModal').style.display = 'flex';
        }}

        function closeSkippedModal() {{
            document.getElementById('skippedModal').style.display = 'none';
        }}

        function renderSkippedList() {{
            const container = document.getElementById('skippedListContainer');
            if (skippedFiles.length === 0) {{
                container.innerHTML = '<div style="text-align:center; padding: 24px; color:#9ca3af;">All files in your vault can be decrypted with your current password! Zero skipped files.</div>';
                updateSkippedBanner();
                return;
            }}

            let html = '';
            skippedFiles.forEach(f => {{
                html += `
                <div class="skipped-card">
                    <div style="flex: 1; min-width: 0;">
                        <div class="skipped-name">${{escapeHtml(f.key)}}</div>
                        <div class="skipped-reason">&#9888; ${{escapeHtml(f.reason)}}</div>
                        <div style="font-size: 0.75rem; color: #6b7280; font-family: monospace; margin-top: 3px;">
                            Repo: ${{escapeHtml(f.repo)}} &bull; Size: ${{f.size_str}} &bull; Uploaded: ${{escapeHtml(f.uploaded_at)}}
                        </div>
                    </div>
                    <div style="display: flex; gap: 8px; flex-shrink: 0; align-items: center;">
                        <a href="${{f.hf_url}}" target="_blank" class="btn btn-hf btn-sm" title="View blob on HF">&#129303; View HF</a>
                        <button onclick="deleteSingleSkipped('${{escapeHtml(f.key)}}')" class="btn btn-delete btn-sm">&#128465; Delete</button>
                    </div>
                </div>`;
            }});
            container.innerHTML = html;
        }}

        async function deleteSingleSkipped(key) {{
            if (!confirm(`Permanently delete '${{key}}' from Hugging Face and manifest?`)) return;
            try {{
                const res = await fetch('/api/delete', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ key: key }})
                }});
                const result = await res.json();
                if (result && result.success) {{
                    skippedFiles = skippedFiles.filter(f => f.key !== key);
                    renderSkippedList();
                    updateSkippedBanner();
                    showToast("🗑️ Deleted skipped file from HF.");
                }} else {{
                    alert("Error: " + ((result && result.message) || "Failed to delete"));
                }}
            }} catch (err) {{
                alert("Error deleting: " + err);
            }}
        }}

        async function deleteAllSkippedFiles() {{
            if (skippedFiles.length === 0) return;
            if (!confirm(`Are you sure you want to permanently delete all ${{skippedFiles.length}} skipped file(s) from Hugging Face?\n\nThis will purge outdated blobs encrypted with older passwords and free cloud storage.`)) return;

            const btn = document.getElementById('deleteAllSkippedBtn');
            const origText = btn.textContent;
            btn.textContent = "Deleting from HF...";
            btn.disabled = true;

            try {{
                const res = await fetch('/api/delete-skipped', {{ method: 'POST' }});
                const result = await res.json();
                if (result && result.success) {{
                    skippedFiles = [];
                    renderSkippedList();
                    updateSkippedBanner();
                    closeSkippedModal();
                    showToast("🗑️ All skipped files deleted from Hugging Face.");
                }} else {{
                    alert("Error: " + ((result && result.message) || "Failed to delete"));
                }}
            }} catch (err) {{
                alert("Error: " + err);
            }} finally {{
                btn.textContent = origText;
                btn.disabled = false;
            }}
        }}

        function updateSkippedBanner() {{
            const banner = document.getElementById('skippedBanner');
            const statCard = document.getElementById('statSkippedCard');
            const statVal = document.getElementById('statSkippedVal');

            if (statVal) statVal.textContent = skippedFiles.length;
            if (skippedFiles.length === 0) {{
                if (banner) banner.style.display = 'none';
                if (statCard) statCard.style.display = 'none';
            }} else {{
                if (banner) banner.style.display = 'flex';
                if (statCard) statCard.style.display = 'block';
            }}
        }}

        function showToast(msg) {{
            const toast = document.getElementById('toast');
            toast.textContent = msg;
            toast.style.display = 'block';
            setTimeout(() => {{
                toast.style.display = 'none';
            }}, 3200);
        }}

        /* Delete Functions */
        function promptDeleteFile(key) {{
            deleteTarget = {{ type: 'file', key: key, folder: null, count: 1 }};
            document.getElementById('modalTitle').innerHTML = "&#9888; Delete Video File";
            document.getElementById('deleteMessage').innerHTML = 
                `Are you sure you want to permanently delete: <br><br><b>${{escapeHtml(key)}}</b><br><br>This will permanently remove the encrypted blob from Hugging Face and remove the stream pointer and NFO files from your Jellyfin library.`;
            document.getElementById('confirmDeleteBtn').textContent = "Delete Video";
            document.getElementById('deleteModal').style.display = 'flex';
        }}

        function promptDeleteFolder(folderPath, count) {{
            deleteTarget = {{ type: 'folder', key: null, folder: folderPath, count: count }};
            document.getElementById('modalTitle').innerHTML = "&#9888; Delete Entire Folder &amp; All Videos";
            document.getElementById('deleteMessage').innerHTML = 
                `Are you sure you want to permanently delete the entire folder:<br><br><span style="color:#f87171;font-size:1.1rem;">&#128193; <b>${{escapeHtml(folderPath)}}</b></span><br><br>Containing <b>${{count}}</b> video(s)?<br><br><small style="color:#9ca3af;">This will delete all ${{count}} encrypted blob(s) permanently from Hugging Face and remove all stream files from your Jellyfin library.</small>`;
            document.getElementById('confirmDeleteBtn').textContent = `Delete Entire Folder (${{count}} Videos)`;
            document.getElementById('deleteModal').style.display = 'flex';
        }}

        function closeDeleteModal() {{
            document.getElementById('deleteModal').style.display = 'none';
            deleteTarget = {{ type: null, key: null, folder: null, count: 0 }};
        }}

        document.getElementById('confirmDeleteBtn').addEventListener('click', async () => {{
            if (!deleteTarget.type) return;
            const btn = document.getElementById('confirmDeleteBtn');
            const originalText = btn.textContent;
            btn.textContent = "Deleting from Hugging Face...";
            btn.disabled = true;

            try {{
                let res, result;
                if (deleteTarget.type === 'file') {{
                    res = await fetch('/api/delete', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ key: deleteTarget.key }})
                    }});
                    result = await res.json();
                    if (result && result.success) {{
                        allFiles = allFiles.filter(f => f.key !== deleteTarget.key);
                    }}
                }} else if (deleteTarget.type === 'folder') {{
                    res = await fetch('/api/delete-folder', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{ folder: deleteTarget.folder }})
                    }});
                    result = await res.json();
                    if (result && result.success) {{
                        const prefix = deleteTarget.folder;
                        allFiles = allFiles.filter(f => f.key !== prefix && !f.key.startsWith(prefix + '/'));
                    }}
                }}

                if (result && result.success) {{
                    updateStats();
                    populateFilterDropdowns();
                    filterTree();
                    closeDeleteModal();
                    showToast("🗑️ Content deleted permanently.");
                }} else {{
                    alert("Error deleting: " + ((result && result.message) || "Unknown error"));
                }}
            }} catch (err) {{
                alert("Failed to communicate with server: " + err);
            }} finally {{
                btn.textContent = originalText;
                btn.disabled = false;
            }}
        }});

        function updateStats() {{
            document.getElementById('statVideos').textContent = allFiles.length;
            const bytes = allFiles.reduce((acc, f) => acc + f.size_bytes, 0);
            document.getElementById('statSize').textContent = (bytes / (1024 * 1024)).toFixed(2) + " MB";
            const repos = new Set(allFiles.map(f => f.repo));
            document.getElementById('statRepos').textContent = repos.size;
        }}

        function escapeHtml(str) {{
            return String(str).replace(/[&<>"']/g, function(m) {{
                return {{ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }}[m];
            }});
        }}


        /* Realtime Activity Tracker & Live Progress Polling */
        let activityLogVisible = false;
        let lastFinishedTaskCount = -1;
        let lastManifestVersion = -1;
        let wasProcessing = false;
        let accountToDelete = null;

        function toggleActivityLog() {{
            activityLogVisible = !activityLogVisible;
            const panel = document.getElementById('activityLogPanel');
            if (panel) panel.style.display = activityLogVisible ? 'block' : 'none';
        }}

        async function clearActivityEvents() {{
            const list = document.getElementById('activityEventsList');
            if (list) {{
                list.dataset.lastFp = 'empty';
                list.innerHTML = '<div style="color: #6b7280; font-style: italic; padding: 12px; text-align: center;">Log cleared.</div>';
            }}
            const badges = [document.getElementById('eventCountBadge'), document.getElementById('headerEventCountBadge')];
            badges.forEach(b => {{ if (b) b.textContent = '0'; }});

            try {{
                await fetch('/api/activity/clear', {{ method: 'POST' }});
            }} catch (e) {{}}
        }}

        async function pollActivity() {{
            try {{
                const res = await fetch('/api/activity');
                if (!res.ok) {{
                    updateSystemStatusIndicators(false, 'offline', 'Server Offline');
                    return;
                }}
                const data = await res.json();
                renderActivity(data);
            }} catch (err) {{
                updateSystemStatusIndicators(false, 'offline', 'Server Offline');
            }}
        }}

        function renderActivity(data) {{
            const box = document.getElementById('liveActivityBox');
            const pulse = document.getElementById('liveStatusPulse');
            const title = document.getElementById('liveStatusTitle');
            const sub = document.getElementById('liveStatusSub');
            const badge = document.getElementById('liveActiveBadge');
            const tasksContainer = document.getElementById('activeTasksContainer');
            const eventsList = document.getElementById('activityEventsList');
            const eventBadge = document.getElementById('eventCountBadge');

            const activeTasks = Object.values(data.active_tasks || {{}});
            const events = data.recent_events || [];

            if (eventBadge) eventBadge.textContent = events.length;

            const inFlightTasks = activeTasks.filter(t => !['completed', 'skipped_duplicate', 'error'].includes(t.stage));
            const batchTotal = data.batch_total || 0;
            const batchDone = data.batch_completed || 0;
            const isProcessing = inFlightTasks.length > 0 || (batchTotal > 0 && batchDone < batchTotal);

            // Update Dual Status Indicators (Sticky Header & Filter Bar)
            const errorTasks = activeTasks.filter(t => t.stage === 'error');
            if (errorTasks.length > 0) {{
                updateSystemStatusIndicators(true, 'attention', `Watcher Alert (${{errorTasks.length}} Error)`);
            }} else if (isProcessing) {{
                const countMsg = batchTotal > 0 ? `${{batchDone}}/${{batchTotal}} Syncing` : `${{inFlightTasks.length}} Syncing`;
                updateSystemStatusIndicators(true, 'active', countMsg);
            }} else {{
                updateSystemStatusIndicators(true, 'idle');
            }}

            if (isProcessing) {{
                if (box) box.classList.add('is-active');
                if (pulse) {{ pulse.className = 'status-pulse-dot pulse-active'; }}
                if (title) {{
                    if (batchTotal > 0) {{
                        const pct = Math.round((batchDone / batchTotal) * 100);
                        title.innerHTML = `⚡ Processing Batch: ${{batchDone}} of ${{batchTotal}} videos completed (${{pct}}%) &bull; ${{inFlightTasks.length}} in flight`;
                    }} else {{
                        title.innerHTML = `⚡ Processing ${{inFlightTasks.length}} video(s)...`;
                    }}
                }}
                if (sub) sub.textContent = inFlightTasks.map(t => t.filename).join(', ') || 'Syncing to cloud vault & Jellyfin...';
                if (badge) {{
                    badge.style.display = 'inline-block';
                    badge.textContent = batchTotal > 0 ? `${{batchDone}}/${{batchTotal}} Done` : `${{inFlightTasks.length}} In Progress`;
                }}
            }} else {{
                if (box) box.classList.remove('is-active');
                if (pulse) {{ pulse.className = 'status-pulse-dot pulse-idle'; }}
                if (title) {{
                    if (batchTotal > 0 && batchDone >= batchTotal) {{
                        title.innerHTML = `✅ Batch Complete: All ${{batchTotal}} videos processed &amp; live on Jellyfin!`;
                    }} else {{
                        title.innerHTML = 'Watcher Service Active &bull; Idle';
                    }}
                }}
                if (sub) sub.textContent = 'Waiting for videos in videos_to_sync/';
                if (badge) badge.style.display = 'none';
            }}

            // Render Active Task Cards without unnecessary DOM flicker
            if (activeTasks.length > 0) {{
                if (tasksContainer) {{
                    tasksContainer.style.display = 'flex';
                    const fp = activeTasks.map(t => `${{t.filename}}:${{t.stage}}:${{t.percent}}:${{t.speed_str || ''}}:${{t.elapsed_sec || ''}}`).join('|');
                    if (tasksContainer.dataset.lastFp !== fp) {{
                        tasksContainer.dataset.lastFp = fp;
                        tasksContainer.innerHTML = activeTasks.map(t => {{
                            let stageIcon = '🔄';
                            let stageColor = '#38bdf8';
                            if (t.stage === 'hashing') {{ stageIcon = '🔍'; stageColor = '#fbbf24'; }}
                            else if (t.stage === 'dup_check') {{ stageIcon = '👥'; stageColor = '#a855f7'; }}
                            else if (t.stage === 'encrypting') {{ stageIcon = '🔒'; stageColor = '#38bdf8'; }}
                            else if (t.stage === 'uploading') {{ stageIcon = '☁️'; stageColor = '#60a5fa'; }}
                            else if (t.stage === 'metadata') {{ stageIcon = '🏷️'; stageColor = '#34d399'; }}
                            else if (t.stage === 'completed') {{ stageIcon = '✅'; stageColor = '#10b981'; }}
                            else if (t.stage === 'skipped_duplicate') {{ stageIcon = '⏭️'; stageColor = '#f59e0b'; }}
                            else if (t.stage === 'paused') {{ stageIcon = '⏸️'; stageColor = '#f59e0b'; }}
                            else if (t.stage === 'error') {{ stageIcon = '❌'; stageColor = '#ef4444'; }}

                            return `
                            <div class="active-task-card">
                                <div class="task-card-top">
                                    <span class="task-file-name">
                                        <span>${{stageIcon}}</span>
                                        <span>${{escapeHtml(t.filename)}}</span>
                                        ${{t.size_str ? `<span style="font-size: 0.75rem; color: #9ca3af; font-weight: normal;">(${{t.size_str}})</span>` : ''}}
                                    </span>
                                    <span style="font-size: 0.75rem; font-weight: 600; color: ${{stageColor}};">
                                        ${{escapeHtml(t.stage_name || t.stage)}} &bull; ${{t.percent}}%
                                    </span>
                                </div>
                                <div class="task-progress-track">
                                    <div class="task-progress-fill" style="width: ${{t.percent}}%;"></div>
                                </div>
                                <div class="task-card-bottom">
                                    <span>${{escapeHtml(t.details || '')}}</span>
                                    <span>${{t.speed_str ? `⚡ ${{escapeHtml(t.speed_str)}} &bull; ` : ''}}${{t.elapsed_sec ? `${{t.elapsed_sec}}s` : ''}} ${{t.account ? `&bull; 👤 ${{escapeHtml(t.account)}}` : ''}}</span>
                                </div>
                            </div>`;
                        }}).join('');
                    }}
                }}
            }} else {{
                if (tasksContainer) {{
                    tasksContainer.style.display = 'none';
                    tasksContainer.dataset.lastFp = '';
                }}
            }}

            // Render Events Stream without unnecessary DOM flicker
            if (eventsList) {{
                if (events.length > 0) {{
                    const fp = events.map(e => `${{e.time}}:${{e.type}}:${{e.title}}:${{e.message}}`).join('|');
                    if (eventsList.dataset.lastFp !== fp) {{
                        eventsList.dataset.lastFp = fp;
                        eventsList.innerHTML = events.map(e => {{
                            let badgeClass = 'badge-info';
                            if (e.type === 'success') badgeClass = 'badge-success';
                            else if (e.type === 'upload') badgeClass = 'badge-upload';
                            else if (e.type === 'skip') badgeClass = 'badge-skip';
                            else if (e.type === 'error') badgeClass = 'badge-error';

                            return `
                            <div class="activity-event-row">
                                <span class="event-time">${{escapeHtml(e.time)}}</span>
                                <span class="event-badge ${{badgeClass}}">${{escapeHtml(e.type)}}</span>
                                <span style="font-weight: 600; color: #e5e7eb;">${{escapeHtml(e.title)}}</span>
                                <span style="color: #9ca3af;">${{escapeHtml(e.message)}}</span>
                                ${{e.speed ? `<span style="color: #38bdf8; font-size: 0.75rem;">(${{escapeHtml(e.speed)}})</span>` : ''}}
                            </div>`;
                        }}).join('');
                    }}
                }} else {{
                    if (eventsList.dataset.lastFp !== 'empty') {{
                        eventsList.dataset.lastFp = 'empty';
                        eventsList.innerHTML = '<div style="color: #6b7280; font-style: italic; padding: 12px; text-align: center;">No activity events logged.</div>';
                    }}
                }}
            }}

            // Monotonic reload tracking:
            const curCompleted = data.total_completed_count || 0;
            const curVersion = data.manifest_version || 0;

            let needReload = false;
            if (curCompleted > lastFinishedTaskCount) {{
                lastFinishedTaskCount = curCompleted;
                needReload = true;
            }}
            if (lastManifestVersion !== -1 && curVersion !== lastManifestVersion) {{
                lastManifestVersion = curVersion;
                needReload = true;
            }} else if (lastManifestVersion === -1) {{
                lastManifestVersion = curVersion;
            }}

            if (wasProcessing && !isProcessing) {{
                // Batch just transitioned to idle: guaranteed final reload
                needReload = true;
            }}
            wasProcessing = isProcessing;

            if (needReload) {{
                reloadFilesList();
            }}
        }}

        async function reloadFilesList() {{
            try {{
                const res = await fetch('/api/files');
                if (res.ok) {{
                    const data = await res.json();
                    const newFiles = data.files || [];
                    const oldKeys = (allFiles || []).map(f => f.key).sort().join('|');
                    const newKeys = newFiles.map(f => f.key).sort().join('|');

                    allFiles = newFiles;
                    syncStoryboardCache(newFiles);
                    skippedFiles = data.skipped || [];
                    primaryAccount = data.primary_account || primaryAccount;
                    populateFilterDropdowns();
                    updateStats();

                    if (oldKeys !== newKeys) {{
                        const curScrollY = window.scrollY;
                        refreshTreeDisplay();
                        window.scrollTo({{ top: curScrollY, behavior: 'instant' }});
                    }} else {{
                        filterTree();
                    }}
                }}
            }} catch (err) {{}}
        }}

        /* Technical Metadata & Tag Extraction */
        async function scanAllTechTags() {{
            const btn = document.getElementById('btnScanTech');
            const orig = btn ? btn.innerHTML : '';
            if (btn) {{
                btn.innerHTML = '&#9203; Scanning Specs...';
                btn.disabled = true;
            }}
            showToast('&#127916; Technical specs & resolution extraction started in background...');
            try {{
                const res = await fetch('/api/scan-tech-tags', {{ method: 'POST' }});
                const data = await res.json();
                if (data.success) {{
                    showToast('&#9989; Technical scan active! Watch live activity log below.');
                }} else {{
                    alert('Scan error: ' + (data.message || 'Failed'));
                }}
            }} catch (err) {{
                alert('Connection error: ' + err);
            }} finally {{
                setTimeout(() => {{
                    if (btn) {{
                        btn.innerHTML = orig;
                        btn.disabled = false;
                    }}
                }}, 4000);
            }}
        }}

        /* Accounts Management UI */
        async function openAccountsModal() {{
            document.getElementById('accountsModal').style.display = 'flex';
            await loadAccountsList();
        }}

        function closeAccountsModal() {{
            document.getElementById('accountsModal').style.display = 'none';
            document.getElementById('newAccountToken').value = '';
            document.getElementById('chkSetPrimary').checked = false;
        }}

        async function loadAccountsList() {{
            const container = document.getElementById('accountsListContainer');
            container.innerHTML = '<div style="color: #9ca3af; padding: 12px; text-align: center;">Loading accounts...</div>';

            try {{
                const res = await fetch('/api/accounts');
                if (!res.ok) throw new Error("Failed to fetch accounts");
                const data = await res.json();
                renderAccountsList(data.accounts || [], data.primary_account);
            }} catch (err) {{
                container.innerHTML = `<div style="color: #f87171; padding: 12px;">Error loading accounts: ${{err}}</div>`;
            }}
        }}

        function renderAccountsList(accounts, primaryUser) {{
            const container = document.getElementById('accountsListContainer');
            const headerCount = document.getElementById('headerAccCount');
            const statAccounts = document.getElementById('statAccounts');
            if (headerCount) headerCount.textContent = accounts.length;
            if (statAccounts) statAccounts.innerHTML = `${{accounts.length}} <span style="font-size: 0.78rem; font-weight: normal; color: var(--text-muted);">&#9881;&#65039; Manage</span>`;

            if (accounts.length === 0) {{
                container.innerHTML = '<div style="color: #9ca3af; padding: 12px;">No connected accounts found.</div>';
                return;
            }}

            container.innerHTML = accounts.map(acc => {{
                const isPrimary = acc.is_primary || (acc.username === primaryUser);
                const isEnabled = acc.enabled !== false;

                return `
                <div class="account-card ${{isPrimary ? 'is-primary' : ''}} ${{!isEnabled ? 'is-disabled' : ''}}">
                    <div class="acc-left">
                        <div class="acc-avatar">${{isPrimary ? '👑' : '👤'}}</div>
                        <div>
                            <div class="acc-name">
                                <span>${{escapeHtml(acc.username)}}</span>
                                ${{isPrimary ? '<span class="account-pill is-primary" style="font-size: 0.68rem;">👑 Primary Uploader</span>' : ''}}
                                ${{isEnabled ? '<span class="meta-pill genre-pill" style="font-size: 0.68rem;">Active</span>' : '<span class="meta-pill" style="font-size: 0.68rem; background: #374151; color: #9ca3af;">Disabled</span>'}}
                            </div>
                            <div class="acc-token">
                                Token: ${{escapeHtml(acc.token_masked)}} &bull; Hosted: ${{acc.video_count || 0}} videos (${{acc.size_str || '0 MB'}})
                            </div>
                        </div>
                    </div>
                    <div class="acc-actions">
                        ${{!isPrimary ? `
                            <button onclick="setPrimaryAccount('${{escapeHtml(acc.username)}}')" class="btn btn-link btn-xs" title="Make this account the primary uploader">
                                👑 Make Primary
                            </button>
                            <button onclick="toggleAccount('${{escapeHtml(acc.username)}}', ${{!isEnabled}})" class="btn ${{isEnabled ? 'btn-hf' : 'btn-play'}} btn-xs">
                                ${{isEnabled ? '⏸️ Disable' : '▶️ Enable'}}
                            </button>
                            <button onclick="promptDeleteAccount('${{escapeHtml(acc.username)}}')" class="btn btn-delete btn-xs" title="Remove account">
                                🗑️ Delete
                            </button>
                        ` : `
                            <span style="font-size: 0.75rem; color: #c084fc; font-weight: 600; padding: 4px 8px;">Upload Destination</span>
                        `}}
                    </div>
                </div>`;
            }}).join('');
        }}

        async function submitAddAccount() {{
            const tokenInput = document.getElementById('newAccountToken');
            const token = tokenInput.value.trim();
            const isUpload = document.getElementById('chkSetPrimary').checked;
            const btn = document.getElementById('btnAddAccount');

            if (!token) {{
                alert("Please enter a Hugging Face Access Token.");
                return;
            }}

            const origText = btn.innerHTML;
            btn.innerHTML = 'Connecting...';
            btn.disabled = true;

            try {{
                const res = await fetch('/api/accounts/add', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ token: token, is_upload: isUpload }})
                }});
                const result = await res.json();
                if (result.success) {{
                    showToast(`✅ Connected account: ${{result.account.username}}`);
                    tokenInput.value = '';
                    document.getElementById('chkSetPrimary').checked = false;
                    await loadAccountsList();
                    await reloadFilesList();
                }} else {{
                    alert("Failed to connect account: " + (result.message || "Unknown error"));
                }}
            }} catch (err) {{
                alert("Communication error: " + err);
            }} finally {{
                btn.innerHTML = origText;
                btn.disabled = false;
            }}
        }}

        async function toggleAccount(username, newEnabled) {{
            try {{
                const res = await fetch('/api/accounts/toggle', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ username: username, enabled: newEnabled }})
                }});
                const result = await res.json();
                if (result.success) {{
                    showToast(`Account '${{username}}' ${{newEnabled ? 'enabled' : 'disabled'}}.`);
                    await loadAccountsList();
                    await reloadFilesList();
                }} else {{
                    alert("Error updating account: " + (result.message || "Unknown error"));
                }}
            }} catch (err) {{
                alert("Failed to communicate: " + err);
            }}
        }}

        async function setPrimaryAccount(username) {{
            if (!confirm(`Switch designated Primary Upload Account to '${{username}}'?\nAll future video syncs will upload to this user's repos.`)) return;

            try {{
                const res = await fetch('/api/accounts/set-primary', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ username: username }})
                }});
                const result = await res.json();
                if (result.success) {{
                    showToast(`👑 Primary upload account switched to: ${{username}}`);
                    primaryAccount = username;
                    await loadAccountsList();
                    await reloadFilesList();
                }} else {{
                    alert("Error: " + (result.message || "Unknown error"));
                }}
            }} catch (err) {{
                alert("Failed to communicate: " + err);
            }}
        }}

        function promptDeleteAccount(username) {{
            accountToDelete = username;
            document.getElementById('deleteAccountPrompt').innerHTML = `Are you sure you want to remove Hugging Face account <b>${{escapeHtml(username)}}</b> from this system?<br><br>The watcher service will no longer scan or check this account for duplicates.`;
            document.getElementById('chkPurgeManifest').checked = false;
            document.getElementById('deleteAccountModal').style.display = 'flex';
        }}

        function closeDeleteAccountModal() {{
            document.getElementById('deleteAccountModal').style.display = 'none';
            accountToDelete = null;
        }}

        async function confirmDeleteAccount() {{
            if (!accountToDelete) return;
            const btn = document.getElementById('btnConfirmDeleteAccount');
            const removeManifest = document.getElementById('chkPurgeManifest').checked;
            const origText = btn.textContent;
            btn.textContent = 'Removing...';
            btn.disabled = true;

            try {{
                const res = await fetch('/api/accounts/delete', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ username: accountToDelete, remove_manifest: removeManifest }})
                }});
                const result = await res.json();
                if (result.success) {{
                    showToast(`🗑️ Account '${{accountToDelete}}' removed.`);
                    closeDeleteAccountModal();
                    await loadAccountsList();
                    await reloadFilesList();
                }} else {{
                    alert("Error removing account: " + (result.message || "Unknown error"));
                }}
            }} catch (err) {{
                alert("Communication error: " + err);
            }} finally {{
                btn.textContent = origText;
                btn.disabled = false;
            }}
        }}

        // Initial setup
        loadOpenedTrees();
        populateFilterDropdowns();
        pollActivity();
        setInterval(pollActivity, 1000);
        refreshTreeDisplay();
        restoreUserPosition();
        applyHideTagsState();
        updateSystemStatusIndicators(true, 'idle');
    </script>
</body>
</html>
"""
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
            dec_bytes = crypto_engine.decrypt_bytes(enc_bytes, self.password)
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
            stream_url = f"http://127.0.0.1:{port}/stream/{quote(clean_key)}?full=1"
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
            stream_url = f"http://127.0.0.1:{port}/stream/{quote(clean_key)}?full=1"
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
                match = re.match(r"bytes=(\d+)-(\d*)", range_header)
                if match:
                    start = int(match.group(1))
                    end = int(match.group(2)) if match.group(2) else sz - 1
                    end = min(sz - 1, end)
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
        clean_key = path_after_stream.strip("/").replace("\\", "/")
        manifest = load_manifest()
        if clean_key in manifest.get("files", {}):
            verifier = manifest["files"][clean_key].get("verifier")
            if verifier and not crypto_engine.verify_password(verifier, self.password):
                self.send_error(403, "Forbidden: Video cannot be decrypted with current master password.")
                return

        meta = self.get_file_metadata(path_after_stream, repo_id=repo_id, blob_path=blob_path)
        if not meta:
            self.send_error(404, f"File {path_after_stream} not found in Hugging Face dataset")
            return

        hdr = meta["header"]
        total_size = hdr["total_size"]
        chunk_size = hdr["chunk_size"]
        aesgcm = meta["aesgcm"]
        hf_url = meta["url"]

        if custom_content_type:
            content_type = custom_content_type
        else:
            mime, _ = mimetypes.guess_type(path_after_stream)
            content_type = mime or "video/mp4"

        repo_token = meta.get("repo_token") or (self.acc_mgr.get_token_for_repo(meta.get("repo_id")) if (hasattr(self, "acc_mgr") and self.acc_mgr) else self.token)

        range_header = self.headers.get("Range")
        if range_header:
            match = re.match(r"bytes=(\d+)-(\d*)", range_header)
            if not match:
                self.send_error(400, "Invalid Range Header")
                return

            start = int(match.group(1))
            if start >= total_size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total_size}")
                self.end_headers()
                return

            # Open-ended range (e.g. bytes=0- or bytes=X- without specified end byte):
            # Stream in responsive 4MB bursts so initial loading and seeking start in milliseconds!
            STREAM_BURST = 4 * 1024 * 1024
            is_audio_stream = bool(custom_content_type and "audio" in custom_content_type) or "/stream/audio/" in self.path
            is_full_stream = ('full=1' in self.path or 'no_burst=1' in self.path or is_audio_stream or 'download' in self.path)
            if match.group(2):
                end = min(total_size - 1, int(match.group(2)))
            elif is_full_stream:
                end = total_size - 1
            else:
                end = min(total_size - 1, start + STREAM_BURST - 1)

            range_info = crypto_engine.calculate_byte_range_chunks(total_size, chunk_size, start, end)
            req_len = range_info["req_length"]

            hf_headers = {}
            if repo_token:
                hf_headers["Authorization"] = f"Bearer {repo_token}"
            hf_headers["Range"] = f"bytes={range_info['enc_start']}-{range_info['enc_end']}"

            try:
                # Stream on-the-fly directly from Hugging Face
                resp = self.hf_session.get(hf_url, headers=hf_headers, stream=True, timeout=25)
                if resp.status_code not in (200, 206):
                    self.send_error(502, f"HF upstream error {resp.status_code}")
                    return

                # Send 206 headers IMMEDIATELY so client starts playback right away
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

                total_chunks = (total_size + chunk_size - 1) // chunk_size
                enc_chunk_payload = chunk_size + crypto_engine.TAG_SIZE
                stream_raw = resp.raw

                def read_exact_bytes(raw_stream, num_bytes):
                    buf = bytearray()
                    while len(buf) < num_bytes:
                        part = raw_stream.read(num_bytes - len(buf))
                        if not part:
                            break
                        buf.extend(part)
                    return bytes(buf)

                # Decrypt chunk-by-chunk on the fly and write immediately to socket
                for c_idx in range(range_info["first_chunk"], range_info["last_chunk"] + 1):
                    nonce = crypto_engine.get_chunk_nonce(hdr["base_nonce"], c_idx)
                    if c_idx == total_chunks - 1:
                        c_len = (total_size - c_idx * chunk_size) + crypto_engine.TAG_SIZE
                    else:
                        c_len = enc_chunk_payload

                    chunk_cipher = read_exact_bytes(stream_raw, c_len)
                    if not chunk_cipher:
                        break

                    decrypted_block = aesgcm.decrypt(nonce, chunk_cipher, None)

                    chunk_start_plain = c_idx * chunk_size
                    slice_start = max(0, start - chunk_start_plain)
                    slice_end = min(len(decrypted_block), end - chunk_start_plain + 1)

                    if slice_start < slice_end:
                        self.wfile.write(decrypted_block[slice_start:slice_end])
                        self.wfile.flush()

            except (BrokenPipeError, ConnectionResetError):
                return
            except Exception:
                return

        else:
            # Non-range request: stream full file progressively with on-the-fly decryption
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
            enc_chunk_payload = chunk_size + crypto_engine.TAG_SIZE
            total_enc_size = crypto_engine.HEADER_SIZE + total_size + (total_chunks * crypto_engine.TAG_SIZE)

            hf_headers = {"Range": f"bytes={crypto_engine.HEADER_SIZE}-{total_enc_size - 1}"}
            if repo_token:
                hf_headers["Authorization"] = f"Bearer {repo_token}"

            try:
                resp = self.hf_session.get(hf_url, headers=hf_headers, stream=True, timeout=30)
                stream_raw = resp.raw

                def read_exact_bytes(raw_stream, num_bytes):
                    buf = bytearray()
                    while len(buf) < num_bytes:
                        part = raw_stream.read(num_bytes - len(buf))
                        if not part:
                            break
                        buf.extend(part)
                    return bytes(buf)

                for c_idx in range(total_chunks):
                    nonce = crypto_engine.get_chunk_nonce(hdr["base_nonce"], c_idx)
                    if c_idx == total_chunks - 1:
                        c_len = (total_size - c_idx * chunk_size) + crypto_engine.TAG_SIZE
                    else:
                        c_len = enc_chunk_payload

                    chunk_cipher = read_exact_bytes(stream_raw, c_len)
                    if not chunk_cipher:
                        break
                    decrypted = aesgcm.decrypt(nonce, chunk_cipher, None)
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
                        hdr = crypto_engine.read_header(resp.content)
                        key = crypto_engine.derive_key(password, hdr["salt"])
                        aes = AESGCM(key)
                        meta = {"header": hdr, "key": key, "aesgcm": aes, "url": u, "repo_id": r, "repo_token": t}
                        FILE_CACHE[k] = meta
                        FILE_CACHE[b] = meta
                        clean_k = k.strip("/").replace("\\", "/")
                        FILE_CACHE[clean_k] = meta
                except Exception:
                    pass

            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=6) as ex:
                ex.map(_pw, f_items)
            print(f"[*] Pre-warmed {len(f_items)} stream headers into memory for instant playback.")
        except Exception as e:
            print(f"[!] Warning prewarming headers: {e}")

    threading.Thread(target=_prewarm_daemon, daemon=True).start()
    storyboard_helper.prewarm_all_storyboards(port=port)

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


if __name__ == "__main__":
    import argparse
    default_port = int(os.environ.get("PORT", 8080))
    parser = argparse.ArgumentParser(description="Interactive Tree View Jellyfin Streaming Proxy")
    parser.add_argument("--port", "-p", type=int, default=default_port, help=f"Port to listen on (default: {default_port})")
    parser.add_argument("--password", help="Master encryption password")
    args = parser.parse_args()

    run_server(port=args.port, password=args.password)
