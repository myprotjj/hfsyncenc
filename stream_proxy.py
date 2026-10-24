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
        elif path == "/api/sync-hf":
            self.handle_sync_hf()
        elif path == "/api/restore-hf":
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
        elif path == "/api/scan-tech-tags":
            self.handle_scan_tech_tags()
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
                    if not has_res or not rec.get("media_info"):
                        url = f"http://127.0.0.1:{port}/stream/{quote(k)}"
                        try:
                            mi = metadata_helper.probe_media_file(url, timeout=12)
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

        with open(img_path, "rb") as f:
            data = f.read()

        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
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
        elif path == "/api/accounts":
            self.handle_api_accounts()
        elif path == "/api/activity":
            self.handle_api_activity()
        elif path == "/api/storyboard":
            self.handle_api_storyboard()
        elif path == "/api/storyboard/image":
            self.handle_api_storyboard_image()
        elif path.startswith("/stream/subtitle"):
            self.handle_stream_subtitle()
        elif path.startswith("/stream/audio/"):
            key_after = path[len("/stream/audio/"):]
            self.handle_stream_audio(key_after)
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

                sb_meta = storyboard_helper.get_storyboard_meta(key)
                dur_sec = sb_meta.get("duration", 0) if sb_meta else 0
                duration_str = format_duration(dur_sec)

                item = {
                    "key": key,
                    "safe_id": storyboard_helper.get_safe_id(key),
                    "has_storyboard": storyboard_helper.has_storyboard(key),
                    "duration_sec": dur_sec,
                    "duration_str": duration_str,
                    "account": account_owner,
                    "repo": repo,
                    "blob": blob,
                    "hf_url": hf_url,
                    "size_bytes": size_bytes,
                    "size_str": f"{size_bytes / (1024 * 1024):.2f} MB",
                    "uploaded_at": info.get("uploaded_at", "N/A"),
                    "file_names": file_names,
                    "aliases": aliases,
                    "metadata": metadata,
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

                sb_meta = storyboard_helper.get_storyboard_meta(key)
                dur_sec = sb_meta.get("duration", 0) if sb_meta else 0
                duration_str = format_duration(dur_sec)

                item = {
                    "key": key,
                    "safe_id": storyboard_helper.get_safe_id(key),
                    "has_storyboard": storyboard_helper.has_storyboard(key),
                    "duration_sec": dur_sec,
                    "duration_str": duration_str,
                    "account": account_owner,
                    "repo": repo,
                    "blob": blob,
                    "hf_url": hf_url,
                    "size_bytes": size_bytes,
                    "size_str": f"{size_bytes / (1024 * 1024):.2f} MB",
                    "uploaded_at": info.get("uploaded_at", "N/A"),
                    "file_names": file_names,
                    "aliases": aliases,
                    "metadata": metadata,
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
        header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--panel-border);
            padding-bottom: 20px;
            margin-bottom: 24px;
        }}
        h1 {{ margin: 0; font-size: 1.6rem; color: var(--primary); display: flex; align-items: center; gap: 10px; }}
        .stats-bar {{
            display: flex;
            gap: 16px;
            flex-wrap: wrap;
            margin-bottom: 20px;
        }}
        .stat-card {{
            background: var(--panel);
            border: 1px solid var(--panel-border);
            padding: 12px 18px;
            border-radius: 8px;
            flex: 1;
            min-width: 180px;
        }}
        .stat-label {{ font-size: 0.8rem; color: var(--text-muted); text-transform: uppercase; font-weight: 600; }}
        .stat-value {{ font-size: 1.3rem; font-weight: bold; margin-top: 4px; color: #fff; }}
        
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
            width: 160px;
            height: 90px;
            background-color: #000;
            background-repeat: no-repeat;
            display: block;
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
            bottom: 24px !important;
            right: 24px !important;
            width: 380px !important;
            max-width: calc(100vw - 48px) !important;
            aspect-ratio: 16 / 9 !important;
            height: auto !important;
            max-height: 240px !important;
            z-index: 99999 !important;
            border-radius: 12px !important;
            box-shadow: 0 16px 45px rgba(0, 0, 0, 0.9), 0 0 0 1px rgba(255, 255, 255, 0.16) !important;
            overflow: hidden !important;
            animation: yt-float-in 0.3s cubic-bezier(0.16, 1, 0.3, 1) forwards !important;
            backdrop-filter: blur(8px) !important;
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
            background-size: 1000% 500%;
            background-position: 22.2222% 0%; /* Frame 2 default poster */
            display: flex;
            align-items: center;
            justify-content: center;
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
            height: 3px;
            background: rgba(255, 255, 255, 0.2);
            opacity: 0;
            transition: opacity 0.15s;
            pointer-events: none;
            z-index: 3;
        }}
        .video-thumb-card:hover .thumb-scrub-bar {{
            opacity: 1;
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
                background-size: 1000% 500%;
                background-position: 22.2222% 0%;
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
            right: 6px;
            background: rgba(0, 0, 0, 0.82);
            color: #93c5fd;
            font-size: 0.65rem;
            font-weight: 600;
            font-family: monospace;
            padding: 2px 6px;
            border-radius: 4px;
            pointer-events: none;
            border: 1px solid rgba(255, 255, 255, 0.15);
            z-index: 2;
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
        .genre-pill {{
            background: rgba(56, 189, 248, 0.12);
            color: #38bdf8;
            border: 1px solid rgba(56, 189, 248, 0.28);
        }}
        .tag-pill {{
            background: rgba(16, 185, 129, 0.12);
            color: #34d399;
            border: 1px solid rgba(16, 185, 129, 0.28);
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
            padding: 24px;
            border-radius: 12px;
            max-width: 520px;
            width: 100%;
            box-shadow: 0 20px 40px rgba(0,0,0,0.85);
            max-height: 90vh;
            overflow-y: auto;
        }}
        .modal h3 {{ margin-top: 0; color: #ef4444; }}
        .modal p {{ color: var(--text-muted); font-size: 0.92rem; line-height: 1.5; }}
        .modal-actions {{ display: flex; justify-content: flex-end; gap: 10px; margin-top: 20px; }}

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
        <header>
            <h1>&#128274; HFSYNC Encrypted Media Vault</h1>
            <div style="display: flex; align-items: center; gap: 12px;">
                <button onclick="scanAllTechTags()" id="btnScanTech" class="tool-btn" style="border-color: rgba(56, 189, 248, 0.4); color: #38bdf8; background: rgba(56, 189, 248, 0.12);" title="Extract resolution (4K, 1080p, 720p, SD), audio, and video codecs for all existing videos">
                    &#127916; Scan Tech Specs
                </button>
                <button onclick="openAccountsModal()" class="tool-btn" style="border-color: rgba(168, 85, 247, 0.4); color: #c084fc; background: rgba(168, 85, 247, 0.12);">
                    &#128101; Accounts (<span id="headerAccCount">{total_accounts}</span>)
                </button>
                <div style="font-size: 0.9rem; color: var(--accent); font-weight: 500;">
                    &#9679; Streaming Bridge Active (Port 8080)
                </div>
            </div>
        </header>

        <div class="stats-bar">
            <div class="stat-card">
                <div class="stat-label">Playable Videos</div>
                <div class="stat-value" id="statVideos">{len(files_data)}</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Cloud Storage Size</div>
                <div class="stat-value" id="statSize">{total_size_mb:.2f} MB</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Primary Upload User</div>
                <div class="stat-value" style="font-size: 1.1rem; color: var(--primary);">&#128081; {primary_user}</div>
            </div>
            <div class="stat-card" style="cursor: pointer;" onclick="openAccountsModal()" title="Manage Hugging Face accounts">
                <div class="stat-label">&#128101; Connected Accounts</div>
                <div class="stat-value" id="statAccounts">{total_accounts} <span style="font-size: 0.78rem; font-weight: normal; color: var(--text-muted);">&#9881;&#65039; Manage</span></div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Target Repositories</div>
                <div class="stat-value" id="statRepos">{len(repos_set)}</div>
            </div>
            {skipped_stat_html}
        </div>

        {skipped_banner_html}

        <!-- Live Realtime Processing & Activity Pipeline -->
        <div id="liveActivityBox" class="live-activity-box">
            <div class="live-activity-header">
                <div class="live-activity-left">
                    <span id="liveStatusPulse" class="status-pulse-dot pulse-idle"></span>
                    <span id="liveStatusTitle" style="font-weight: 600; font-size: 0.95rem;">Watcher Service Active &bull; Idle</span>
                    <span id="liveStatusSub" style="font-size: 0.8rem; color: var(--text-muted);">Waiting for videos in videos_to_sync/</span>
                </div>
                <div style="display: flex; align-items: center; gap: 8px;">
                    <span id="liveActiveBadge" class="live-active-badge" style="display: none;">0 Active</span>
                    <button onclick="toggleActivityLog()" id="btnToggleLog" class="tool-btn btn-sm">&#128220; Activity Log (<span id="eventCountBadge">0</span>)</button>
                </div>
            </div>

            <!-- Active Processing Tasks Grid -->
            <div id="activeTasksContainer" class="active-tasks-grid" style="display: none;">
                <!-- Populated dynamically via JS -->
            </div>

            <!-- Expandable Live Activity Log / Console -->
            <div id="activityLogPanel" class="activity-log-panel" style="display: none;">
                <div class="activity-log-header">
                    <span>Recent Pipeline Events</span>
                    <button onclick="clearActivityEvents()" class="tool-btn btn-xs" style="font-size: 0.72rem;">Clear Log</button>
                </div>
                <div id="activityEventsList" class="activity-events-list">
                    <!-- Events stream here -->
                </div>
            </div>
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

        <div class="toolbar">
            <input type="text" id="searchInput" class="search-input" placeholder="&#128269; Search by title, tag, genre, account, folder..." oninput="filterTree()">
            <select id="accountFilter" class="filter-select" onchange="filterTree()">
                <option value="">👥 All Accounts</option>
            </select>
            <select id="genreFilter" class="filter-select" onchange="filterTree()">
                <option value="">📁 All Categories / Genres</option>
            </select>
            <select id="tagFilter" class="filter-select" onchange="filterTree()">
                <option value="">🏷️ All Tags</option>
            </select>
            <button onclick="resetFilters()" class="tool-btn">&#128260; Reset</button>
            <button onclick="syncMetadataToHf()" id="syncHfBtn" class="tool-btn" title="Encrypt and sync all metadata to Hugging Face">&#9729;&#65039; Sync to HF</button>
            <button onclick="expandAll()" class="tool-btn">&#9660; Expand All</button>
            <button onclick="collapseAll()" class="tool-btn">&#9654; Collapse All</button>
            <div class="view-toggle-group">
                <button onclick="setViewMode('card')" id="btnViewCard" class="view-toggle-btn" title="Switch to Cards Grid View">&#127183; Cards</button>
                <button onclick="setViewMode('list')" id="btnViewList" class="view-toggle-btn" title="Switch to List Row View">&#9776; List</button>
            </div>
        </div>

        <div class="tree-container" id="treeContainer">
            <!-- Tree populated dynamically via JS -->
        </div>
    </div>

    <!-- Metadata & Tags Edit Modal -->
    <div id="metadataModal" class="modal-overlay">
        <div class="modal">
            <h3 style="color: var(--primary); margin-bottom: 4px;">&#127991; Edit Jellyfin Metadata &amp; Tags</h3>
            <p style="margin-top: 0; margin-bottom: 16px; font-size: 0.84rem;">
                Changes update <code>upload_manifest.json</code> and standard Jellyfin <code>.nfo</code> XML files.
            </p>

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
                    <label class="form-label">Movie / Video Title</label>
                    <input type="text" id="editTitle" class="form-input" placeholder="Title">
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
                <label class="form-label">Categories / Genres (comma-separated)</label>
                <input type="text" id="editGenres" class="form-input" placeholder="Action, Sci-Fi, Adventure">
                <div class="quick-chips">
                    <span class="quick-chip" onclick="addGenreChip('Action')">+ Action</span>
                    <span class="quick-chip" onclick="addGenreChip('Sci-Fi')">+ Sci-Fi</span>
                    <span class="quick-chip" onclick="addGenreChip('Comedy')">+ Comedy</span>
                    <span class="quick-chip" onclick="addGenreChip('Drama')">+ Drama</span>
                    <span class="quick-chip" onclick="addGenreChip('Horror')">+ Horror</span>
                    <span class="quick-chip" onclick="addGenreChip('Thriller')">+ Thriller</span>
                    <span class="quick-chip" onclick="addGenreChip('Animation')">+ Animation</span>
                    <span class="quick-chip" onclick="addGenreChip('Anime')">+ Anime</span>
                    <span class="quick-chip" onclick="addGenreChip('Documentary')">+ Documentary</span>
                </div>
            </div>

            <div class="form-group">
                <label class="form-label">Tags (comma-separated)</label>
                <input type="text" id="editTags" class="form-input" placeholder="4K, Favorites, Streamable">
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

            <div class="modal-actions">
                <button onclick="closeMetadataModal()" class="tool-btn">Cancel</button>
                <button id="saveMetadataBtn" onclick="saveMetadata()" class="btn btn-play" style="padding: 8px 18px;">&#128190; Save to Jellyfin</button>
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

    <div id="toast" class="toast"></div>

    <script>
        let allFiles = {files_json_str};
        let skippedFiles = {skipped_json_str};
        let primaryAccount = "{primary_user}";
        let deleteTarget = {{ type: null, key: null, folder: null, count: 0 }};
        let editingKey = null;

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
            currentViewMode = mode;
            try {{
                localStorage.setItem('hfsync_view_mode', mode);
            }} catch (e) {{}}
            updateViewToggleButtons();
            filterTree();
        }}

        function toggleCardActions(btn, event) {{
            if (event) event.stopPropagation();
            const card = btn.closest('.video-card');
            if (!card) return;
            const actionsBar = card.querySelector('.video-card-actions');
            const arrowBtn = card.querySelector('.card-action-arrow-btn') || btn;
            if (!actionsBar) return;

            const isOpen = actionsBar.classList.contains('is-open');
            if (isOpen) {{
                actionsBar.classList.remove('is-open');
                arrowBtn.classList.remove('is-active');
            }} else {{
                actionsBar.classList.add('is-open');
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
                badgesHtml += `<span class="meta-pill genre-pill">&#127916; ${{escapeHtml(g)}}</span>`;
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
                badgesHtml += `<span class="meta-pill ${{cls}}">${{icon}} ${{escapeHtml(t)}}</span>`;
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
            const accountBadge = `<span class="account-pill ${{isPrimary ? 'is-primary' : ''}}">${{isPrimary ? '&#128081;' : '&#128100;'}} ${{escapeHtml(accountOwner)}}</span>`;

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
            let thumbBgStyle = '';
            let fallbackHtml = '';
            if (hasStoryboard && safeId) {{
                thumbBgStyle = "background-image: url('/api/storyboard/image?id=" + safeId + "'); background-size: 1000% 500%; background-position: 22.2222% 0%;";
            }} else {{
                fallbackHtml = '<div class="thumb-fallback"><span class="thumb-fallback-icon">&#127916;</span><span class="thumb-fallback-txt">PREVIEW</span></div>';
            }}

            const durationStr = f.duration_str || (f.duration_sec ? formatTime(f.duration_sec) : '');
            const durationHtml = durationStr ? `<div class="card-duration-badge">${{escapeHtml(durationStr)}}</div>` : '';

            return `
            <div class="video-card" data-key="${{escapeHtml(f.key)}}">
                <div class="video-card-thumb video-thumb-card" 
                     data-key="${{escapeHtml(f.key)}}" 
                     data-safeid="${{safeId}}"
                     onclick="playVideo('/stream/${{escapeHtml(f.key)}}', '${{escapeHtml(title)}}', '${{escapeHtml(f.blob_path || '')}}')"
                     onmouseenter="handleThumbEnter(this)"
                     onmouseleave="handleThumbLeave(this)"
                     onmousemove="handleThumbMove(this, event)"
                     title="Click to play, hover to preview screenshots">
                    <div class="thumb-canvas" id="thumb-canvas-${{safeId}}" style="${{thumbBgStyle}}">
                        ${{fallbackHtml}}
                    </div>
                    <div class="thumb-scrub-bar">
                        <div class="thumb-scrub-fill"></div>
                    </div>
                    <div class="thumb-overlay-play">
                        <svg viewBox="0 0 24 24" fill="currentColor" width="22" height="22"><path d="M8 5v14l11-7z"/></svg>
                    </div>
                    <div class="thumb-badge">${{hasStoryboard ? 'HD' : 'PREVIEW'}}</div>
                    <div class="card-size-badge">${{escapeHtml(f.size_str)}}</div>
                    ${{durationHtml}}
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
                badgesHtml += `<span class="meta-pill genre-pill">&#127916; ${{escapeHtml(g)}}</span>`;
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
                badgesHtml += `<span class="meta-pill ${{cls}}">${{icon}} ${{escapeHtml(t)}}</span>`;
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
            const accountBadge = `<span class="account-pill ${{isPrimary ? 'is-primary' : ''}}">${{isPrimary ? '&#128081;' : '&#128100;'}} ${{escapeHtml(accountOwner)}}</span>`;

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
            let thumbBgStyle = '';
            let fallbackHtml = '';
            if (hasStoryboard && safeId) {{
                thumbBgStyle = "background-image: url('/api/storyboard/image?id=" + safeId + "'); background-size: 1000% 500%; background-position: 22.2222% 0%;";
            }} else {{
                fallbackHtml = '<div class="thumb-fallback"><span class="thumb-fallback-icon">&#127916;</span><span class="thumb-fallback-txt">PREVIEW</span></div>';
            }}

            const durationStr = f.duration_str || (f.duration_sec ? formatTime(f.duration_sec) : '');
            const durationHtml = durationStr ? `<div class="card-duration-badge list-duration-badge">${{escapeHtml(durationStr)}}</div>` : '';

            return `
            <div class="file-row" data-key="${{escapeHtml(f.key)}}">
                <div class="video-thumb-card" 
                     data-key="${{escapeHtml(f.key)}}" 
                     data-safeid="${{safeId}}"
                     onclick="playVideo('/stream/${{escapeHtml(f.key)}}', '${{escapeHtml(title)}}', '${{escapeHtml(f.blob_path || '')}}')"
                     onmouseenter="handleThumbEnter(this)"
                     onmouseleave="handleThumbLeave(this)"
                     onmousemove="handleThumbMove(this, event)"
                     title="Click to play, hover to preview screenshots">
                    <div class="thumb-canvas" id="thumb-canvas-${{safeId}}" style="${{thumbBgStyle}}">
                        ${{fallbackHtml}}
                    </div>
                    <div class="thumb-scrub-bar">
                        <div class="thumb-scrub-fill"></div>
                    </div>
                    <div class="thumb-overlay-play">
                        <svg viewBox="0 0 24 24" fill="currentColor" width="16" height="16"><path d="M8 5v14l11-7z"/></svg>
                    </div>
                    <div class="thumb-badge">${{hasStoryboard ? 'HD' : 'PREVIEW'}}</div>
                    ${{durationHtml}}
                </div>
                <div style="flex: 1; min-width: 0;">
                    <div style="display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap;">
                        <span class="file-name" style="cursor: pointer;" onclick="playVideo('/stream/${{escapeHtml(f.key)}}', '${{escapeHtml(title)}}', '${{escapeHtml(f.blob_path || '')}}')" title="Click to play: ${{escapeHtml(title)}}">${{escapeHtml(title)}}</span>
                        ${{subtitleHtml}}
                    </div>
                    ${{overviewHtml}}
                    <div class="meta-badges">
                        ${{accountBadge}}
                        <span class="file-meta">${{f.size_str}}</span>
                        ${{badgesHtml}}
                        ${{aliasBadge}}
                        ${{subBadge}}
                    </div>
                </div>
                <div class="file-actions">
                    <a href="/stream/${{f.key}}" target="_blank" class="btn btn-link" title="Direct Stream URL">&#128279; Direct</a>
                    <a href="/stream/audio/${{f.key}}" download="${{escapeHtml(title)}}.m4a" target="_blank" class="btn btn-link btn-card-audio" title="Download / Stream Lightweight Audio (.m4a)">&#127925; Audio</a>
                    <a href="${{f.hf_url}}" target="_blank" class="btn btn-hf" title="Open file on Hugging Face">&#129303; View on HF</a>
                    <button onclick="promptEditMetadata('${{escapeHtml(f.key)}}')" class="btn btn-edit" title="Edit tags, categories, and Jellyfin metadata">&#127991; Edit Tags</button>
                    <button onclick="promptDeleteFile('${{escapeHtml(f.key)}}')" class="btn btn-delete" title="Delete video permanently">&#128465; Delete</button>
                </div>
            </div>`;
        }}

        function renderTree(node, depth = 0, currentPath = "") {{
            if (node.type === "file") {{
                return currentViewMode === 'card' ? renderCard(node.data, node.name) : renderListRow(node.data, node.name);
            }}

            let totalDescendantFiles = countFiles(node);
            if (totalDescendantFiles === 0 && depth > 0) return "";

            let html = "";
            const folderPath = currentPath ? `${{currentPath}}/${{node.name}}` : (depth > 0 ? node.name : "");

            if (depth > 0) {{
                html += `
                <div class="folder-group" data-folder="${{escapeHtml(folderPath)}}">
                    <div class="folder-header">
                        <div class="folder-left" onclick="toggleFolder(this.parentElement)">
                            <span class="folder-arrow open">&#9654;</span>
                            <span class="folder-icon">&#128193;</span>
                            <span>${{escapeHtml(node.name)}}</span>
                            <span class="folder-count">${{totalDescendantFiles}} video(s)</span>
                        </div>
                        <div class="folder-actions">
                            <button onclick="event.stopPropagation(); promptDeleteFolder('${{escapeHtml(folderPath)}}', ${{totalDescendantFiles}})" class="btn btn-delete btn-sm" title="Delete entire folder and all contained videos">&#128465; Delete Folder</button>
                        </div>
                    </div>
                    <div class="folder-children">`;
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

        function populateFilterDropdowns() {{
            const accountSelect = document.getElementById('accountFilter');
            const genreSelect = document.getElementById('genreFilter');
            const tagSelect = document.getElementById('tagFilter');

            const currAccount = accountSelect ? accountSelect.value : '';
            const currGenre = genreSelect.value;
            const currTag = tagSelect.value;

            const accountSet = new Set();
            const genreSet = new Set();
            const tagSet = new Set();

            allFiles.forEach(f => {{
                accountSet.add(f.account || primaryAccount);
                const meta = f.metadata || {{}};
                (meta.genres || []).forEach(g => {{ if (g && String(g).trim()) genreSet.add(String(g).trim()); }});
                (meta.tags || []).forEach(t => {{ if (t && String(t).trim()) tagSet.add(String(t).trim()); }});
            }});

            if (accountSelect) {{
                accountSelect.innerHTML = '<option value="">👥 All Accounts</option>';
                Array.from(accountSet).sort().forEach(acc => {{
                    const opt = document.createElement('option');
                    opt.value = acc;
                    opt.textContent = (acc === primaryAccount ? '👑 ' : '👤 ') + acc;
                    if (acc === currAccount) opt.selected = true;
                    accountSelect.appendChild(opt);
                }});
            }}

            genreSelect.innerHTML = '<option value="">📁 All Categories / Genres</option>';
            Array.from(genreSet).sort().forEach(g => {{
                const opt = document.createElement('option');
                opt.value = g;
                opt.textContent = g;
                if (g === currGenre) opt.selected = true;
                genreSelect.appendChild(opt);
            }});

            tagSelect.innerHTML = '<option value="">🏷️ All Tags</option>';
            Array.from(tagSet).sort().forEach(t => {{
                const opt = document.createElement('option');
                opt.value = t;
                opt.textContent = t;
                if (t === currTag) opt.selected = true;
                tagSelect.appendChild(opt);
            }});
        }}

        function refreshTreeDisplay() {{
            updateViewToggleButtons();
            updateViewToggleButtons();
            const container = document.getElementById('treeContainer');
            if (allFiles.length === 0) {{
                container.innerHTML = '<div class="empty-state">No playable videos found with current encryption password. Drop videos into <code>videos_to_sync/</code> to upload!</div>';
                return;
            }}
            const tree = buildTree(allFiles);
            container.innerHTML = renderTree(tree, 0);
        }}

        function toggleFolder(header) {{
            const arrow = header.querySelector('.folder-arrow');
            const children = header.nextElementSibling;
            if (arrow && children) {{
                arrow.classList.toggle('open');
                children.classList.toggle('collapsed');
            }}
        }}

        function expandAll() {{
            document.querySelectorAll('.folder-arrow').forEach(a => a.classList.add('open'));
            document.querySelectorAll('.folder-children').forEach(c => c.classList.remove('collapsed'));
        }}

        function collapseAll() {{
            document.querySelectorAll('.folder-arrow').forEach(a => a.classList.remove('open'));
            document.querySelectorAll('.folder-children').forEach(c => c.classList.add('collapsed'));
        }}

        function filterTree() {{
            const q = document.getElementById('searchInput').value.toLowerCase().trim();
            const selAccount = document.getElementById('accountFilter') ? document.getElementById('accountFilter').value : '';
            const selGenre = document.getElementById('genreFilter').value;
            const selTag = document.getElementById('tagFilter').value;

            if (!q && !selAccount && !selGenre && !selTag) {{
                refreshTreeDisplay();
                return;
            }}

            const filtered = allFiles.filter(f => {{
                const meta = f.metadata || {{}};
                const title = (meta.title || "").toLowerCase();
                const overview = (meta.overview || "").toLowerCase();
                const year = String(meta.year || "").toLowerCase();
                const key = f.key.toLowerCase();
                const account = (f.account || primaryAccount).toLowerCase();
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
                               account.includes(q) ||
                               genresLower.some(g => g.includes(q)) ||
                               tagsLower.some(t => t.includes(q)) ||
                               aliases.some(a => a.includes(q)) ||
                               fileNames.some(fn => fn.includes(q));
                }}

                let matchesAccount = true;
                if (selAccount) {{
                    matchesAccount = ((f.account || primaryAccount) === selAccount);
                }}

                let matchesGenre = true;
                if (selGenre) {{
                    matchesGenre = genres.includes(selGenre);
                }}

                let matchesTag = true;
                if (selTag) {{
                    matchesTag = tags.includes(selTag);
                }}

                return matchesQ && matchesAccount && matchesGenre && matchesTag;
            }});

            updateViewToggleButtons();
            const container = document.getElementById('treeContainer');
            if (filtered.length === 0) {{
                container.innerHTML = '<div class="empty-state">No videos match your active filter criteria. <br><button onclick="resetFilters()" class="btn btn-link" style="margin-top: 12px;">Reset Filters</button></div>';
                return;
            }}
            const tree = buildTree(filtered);
            container.innerHTML = renderTree(tree, 0);
            expandAll();
        }}

        function resetFilters() {{
            document.getElementById('searchInput').value = "";
            if (document.getElementById('accountFilter')) {{
                document.getElementById('accountFilter').value = "";
            }}
            document.getElementById('genreFilter').value = "";
            document.getElementById('tagFilter').value = "";
            filterTree();
        }}

        /* Interactive Video Thumbnail Moving Preview Engine */
        let thumbCycleInterval = null;
        let activeHoveredThumb = null;
        let thumbManualScrubTimeout = null;
        let thumbSequenceIdx = 0;

        // 17 sample frames across the 50-frame storyboard (steps of 3 frames)
        const THUMB_SAMPLE_FRAMES = [
            1, 4, 7, 10, 13, 16, 19, 22, 25, 28, 31, 34, 37, 40, 43, 46, 49
        ];

        function setThumbCardFrame(card, frameIndex) {{
            if (!card) return;
            const canvas = card.querySelector('.thumb-canvas');
            if (!canvas) return;
            
            const col = frameIndex % 10;
            const row = Math.floor(frameIndex / 10);
            const posX = (col / 9) * 100;
            const posY = (row / 4) * 100;
            
            canvas.style.backgroundPosition = `${{posX}}% ${{posY}}%`;
            
            const fill = card.querySelector('.thumb-scrub-fill');
            if (fill) {{
                fill.style.width = `${{((frameIndex + 1) / 50) * 100}}%`;
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

            const hasBg = canvas.style.backgroundImage && canvas.style.backgroundImage !== 'none' && canvas.style.backgroundImage !== '';
            
            if (hasBg) {{
                startThumbAutoCycle(card, 0);
            }} else {{
                const fallback = card.querySelector('.thumb-fallback-txt');
                if (fallback) fallback.textContent = 'Loading...';

                try {{
                    const res = await fetch(`/api/storyboard?key=${{encodeURIComponent(key)}}`);
                    if (!res.ok) return;
                    const data = await res.json();
                    
                    if (data.available && data.sprite_url) {{
                        const img = new Image();
                        img.src = data.sprite_url;
                        img.onload = () => {{
                            canvas.style.backgroundImage = `url('${{data.sprite_url}}')`;
                            const fb = card.querySelector('.thumb-fallback');
                            if (fb) fb.remove();
                            if (activeHoveredThumb === card) {{
                                startThumbAutoCycle(card, 0);
                            }} else {{
                                setThumbCardFrame(card, 2);
                            }}
                        }};
                    }} else {{
                        let pollCount = 0;
                        const poll = setInterval(async () => {{
                            pollCount++;
                            if (pollCount > 15 || activeHoveredThumb !== card) {{
                                clearInterval(poll);
                                return;
                            }}
                            try {{
                                const r2 = await fetch(`/api/storyboard?key=${{encodeURIComponent(key)}}`);
                                if (r2.ok) {{
                                    const d2 = await r2.json();
                                    if (d2.available && d2.sprite_url) {{
                                        clearInterval(poll);
                                        const img = new Image();
                                        img.src = d2.sprite_url;
                                        img.onload = () => {{
                                            canvas.style.backgroundImage = `url('${{d2.sprite_url}}')`;
                                            const fb = card.querySelector('.thumb-fallback');
                                            if (fb) fb.remove();
                                            if (activeHoveredThumb === card) {{
                                                startThumbAutoCycle(card, 0);
                                            }} else {{
                                                setThumbCardFrame(card, 2);
                                            }}
                                        }};
                                    }}
                                }}
                            }} catch (e) {{}}
                        }}, 800);
                    }}
                }} catch (err) {{}}
            }}
        }}

        function handleThumbMove(card, event) {{
            if (!card) return;
            const canvas = card.querySelector('.thumb-canvas');
            if (!canvas || !canvas.style.backgroundImage || canvas.style.backgroundImage === 'none') return;

            clearInterval(thumbCycleInterval);
            clearTimeout(thumbManualScrubTimeout);

            const rect = card.getBoundingClientRect();
            const pct = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
            const frameIndex = Math.min(49, Math.floor(pct * 50));
            
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
            setThumbCardFrame(card, 2);
            const fill = card.querySelector('.thumb-scrub-fill');
            if (fill) fill.style.width = '0%';
        }}

        /* Advanced YouTube-Style Video Player Controller */
        let currentBlobId = "";
        let activeStoryboard = null;
        let currentStoryboardKey = null;

        async function loadStoryboard(streamUrl, duration) {{
            if (!streamUrl) return;
            const raw = streamUrl.replace(/.*\/stream\//, '');
            const key = decodeURIComponent(raw);
            currentStoryboardKey = key;
            activeStoryboard = null;
            const thumbElem = document.getElementById('ytThumbPreview');
            if (thumbElem) thumbElem.style.display = 'none';

            try {{
                const durParam = (duration && duration > 0) ? `&duration=${{Math.round(duration)}}` : '';
                const res = await fetch(`/api/storyboard?key=${{encodeURIComponent(key)}}${{durParam}}`);
                if (!res.ok) return;
                const data = await res.json();
                if (currentStoryboardKey !== key) return;

                if (data.available && data.sprite_url) {{
                    const img = new Image();
                    img.src = data.sprite_url;
                    img.onload = () => {{
                        if (currentStoryboardKey === key) {{
                            activeStoryboard = data;
                            if (thumbElem) {{
                                thumbElem.style.backgroundImage = `url('${{data.sprite_url}}')`;
                            }}
                        }}
                    }};
                }} else {{
                    // Watching priority poll: check every 1s for up to 6 times
                    let attempts = 0;
                    const pollInterval = setInterval(async () => {{
                        attempts++;
                        if (currentStoryboardKey !== key || activeStoryboard || attempts > 6) {{
                            clearInterval(pollInterval);
                            return;
                        }}
                        try {{
                            const r2 = await fetch(`/api/storyboard?key=${{encodeURIComponent(key)}}`);
                            if (r2.ok) {{
                                const d2 = await r2.json();
                                if (d2.available && d2.sprite_url && currentStoryboardKey === key) {{
                                    clearInterval(pollInterval);
                                    const img = new Image();
                                    img.src = d2.sprite_url;
                                    img.onload = () => {{
                                        if (currentStoryboardKey === key) {{
                                            activeStoryboard = d2;
                                            if (thumbElem) {{
                                                thumbElem.style.backgroundImage = `url('${{d2.sprite_url}}')`;
                                            }}
                                        }}
                                    }};
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
        }}

        function deactivateFloatingMiniPlayer() {{
            const box = document.getElementById('playerBox');
            const container = document.getElementById('ytContainer');
            if (!box || !container || !isFloatingMini) return;

            container.classList.remove('is-floating-mini');
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

        function playVideo(url, title, blobPath) {{
            const box = document.getElementById('playerBox');
            const player = document.getElementById('videoPlayer');
            const playerTitle = document.getElementById('playerTitle');
            const container = document.getElementById('ytContainer');

            userClosedPlayer = false;

            currentBlobId = blobPath || url;
            currentVideoKey = (url.startsWith('/stream/') ? url.substring('/stream/'.length) : url);
            try {{ currentVideoKey = decodeURIComponent(currentVideoKey); }} catch (e) {{}}
            playerTitle.textContent = title || "Decrypted Stream";
            box.style.display = 'block';
            resetAudioMode();
            updateSubtitleTracks(currentVideoKey);

            // Check if user is scrolled down away from main player
            const rect = box.getBoundingClientRect();
            const isScrolledDown = (window.scrollY > 180 || rect.bottom < 80 || isFloatingMini);

            if (isScrolledDown) {{
                activateFloatingMiniPlayer();
            }} else {{
                deactivateFloatingMiniPlayer();
            }}

            player.src = url;
            player.play().catch(() => {{}});

            if (!isScrolledDown && container) {{
                container.focus();
            }}

            initYtPlayerEvents();
            loadStoryboard(url, player.duration);
            initFloatingScrollObserver();
            resetControlsTimer();
        }}

        function closePlayer() {{
            userClosedPlayer = true;
            deactivateFloatingMiniPlayer();
            const box = document.getElementById('playerBox');
            const player = document.getElementById('videoPlayer');
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
            flashCenterFeedback(deltaSec > 0 ? `+${{deltaSec}}s` : `${{deltaSec}}s`);
        }}

        function toggleMute() {{
            const player = document.getElementById('videoPlayer');
            player.muted = !player.muted;
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
                resetControlsTimer();
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

            // Time & Buffer Tracking
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
                updateStatsForNerds();
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
                }}
            }});

            scrubContainer.addEventListener('mousemove', (e) => {{
                const rect = scrubContainer.getBoundingClientRect();
                const pos = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
                if (player.duration && scrubTooltip) {{
                    scrubTooltip.style.display = 'block';

                    // Clamp horizontally so 160px preview thumbnail stays strictly within player bounds
                    const tooltipHalfWidth = 84;
                    const mouseX = e.clientX - rect.left;
                    const clampedX = Math.max(tooltipHalfWidth, Math.min(rect.width - tooltipHalfWidth, mouseX));
                    scrubTooltip.style.left = `${{clampedX}}px`;

                    const hoverTime = pos * player.duration;
                    const timeElem = document.getElementById('ytTooltipTime');
                    if (timeElem) timeElem.textContent = formatTime(hoverTime);

                    const thumbElem = document.getElementById('ytThumbPreview');
                    if (activeStoryboard && thumbElem) {{
                        thumbElem.style.display = 'block';
                        const totalFrames = activeStoryboard.total_frames || 50;
                        const cols = activeStoryboard.cols || 10;
                        const rows = activeStoryboard.rows || 5;
                        const w = activeStoryboard.thumb_width || 160;
                        const h = activeStoryboard.thumb_height || 90;

                        const frameIdx = Math.min(totalFrames - 1, Math.max(0, Math.floor((hoverTime / player.duration) * totalFrames)));
                        const col = frameIdx % cols;
                        const row = Math.floor(frameIdx / cols);

                        thumbElem.style.backgroundImage = `url('${{activeStoryboard.sprite_url}}')`;
                        thumbElem.style.backgroundSize = `${{cols * w}}px ${{rows * h}}px`;
                        thumbElem.style.backgroundPosition = `-${{col * w}}px -${{row * h}}px`;
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

            renderEditSubtitles(key, file.subtitles || []);
            resetSubUploadForm();

            document.getElementById('metadataModal').style.display = 'flex';
        }}



        /* Subtitles & Audio Mode Engine */
        let currentVideoKey = '';
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
        }}

        function setSubtitleTrack(lang) {{
            const player = document.getElementById('videoPlayer');
            const menu = document.getElementById('ytSubMenu');
            const topMenu = document.getElementById('ytTopSubMenu');
            currentSubLang = lang;

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
            listEl.innerHTML = aliasArr.map(a => `
                <span class="alias-pill">
                    &#128206; <span>${{escapeHtml(a)}}</span>
                    <button type="button" class="alias-del-btn" onclick="promptRemoveAlias(event, '${{escapeHtml(fileKey)}}', '${{escapeHtml(a)}}')" title="Remove this unsuitable filename">&times;</button>
                </span>
            `).join('');
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

        function clearActivityEvents() {{
            const list = document.getElementById('activityEventsList');
            if (list) list.innerHTML = '<div style="color: #6b7280; font-style: italic;">Log cleared.</div>';
            const badge = document.getElementById('eventCountBadge');
            if (badge) badge.textContent = '0';
        }}

        async function pollActivity() {{
            try {{
                const res = await fetch('/api/activity');
                if (!res.ok) return;
                const data = await res.json();
                renderActivity(data);
            }} catch (err) {{
                // Silently ignore network blips in background polling
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

            // Render Active Task Cards
            if (activeTasks.length > 0) {{
                if (tasksContainer) {{
                    tasksContainer.style.display = 'flex';
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
            }} else {{
                if (tasksContainer) tasksContainer.style.display = 'none';
            }}

            // Render Events Stream
            if (eventsList && events.length > 0) {{
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
                    allFiles = data.files || [];
                    skippedFiles = data.skipped || [];
                    primaryAccount = data.primary_account || primaryAccount;
                    populateFilterDropdowns();
                    filterTree();
                    updateStats();
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
        populateFilterDropdowns();
        pollActivity();
        setInterval(pollActivity, 1000);
        refreshTreeDisplay();
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
            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            port = self.server.server_port if hasattr(self.server, "server_port") else 8080
            stream_url = f"http://127.0.0.1:{port}/stream/{clean_key}"
            safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", clean_key)
            tmp_m4a = f"temp_audio_{safe_id}.m4a"

            # 1. Fast copy
            cmd = [ffmpeg, "-y", "-i", stream_url, "-vn", "-c:a", "copy", tmp_m4a]
            subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
            if not os.path.exists(tmp_m4a) or os.path.getsize(tmp_m4a) < 500:
                # 2. AAC encode fallback
                cmd = [ffmpeg, "-y", "-i", stream_url, "-vn", "-c:a", "aac", "-b:a", "192k", tmp_m4a]
                subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)

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
        if not audio_info or not audio_info.get("blob_path"):
            audio_info = self.extract_and_upload_companion_audio(clean_key, file_info)

        if audio_info and audio_info.get("blob_path"):
            a_blob = audio_info["blob_path"]
            a_repo = audio_info.get("repo_id") or file_info.get("repo_id")
            self.handle_stream(path_after_stream=clean_key, custom_content_type="audio/mp4", repo_id=a_repo, blob_path=a_blob)
            return

        self.handle_stream(path_after_stream=clean_key, custom_content_type="audio/mp4")

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
            is_full_stream = ('full=1' in self.path or 'no_burst=1' in self.path)
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
                    self.send_header("Content-Disposition", f'inline; filename="{Path(clean_key).stem}.m4a"')
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
                self.send_header("Content-Disposition", f'inline; filename="{Path(clean_key).stem}.m4a"')
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
