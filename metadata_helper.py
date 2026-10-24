import os
import io
import json
import re
import subprocess
import requests
import xml.sax.saxutils as saxutils
from pathlib import Path
from huggingface_hub import HfApi
import imageio_ffmpeg
import crypto_engine
import commit_helper


def probe_media_file(source_path: str, timeout: int = 15) -> dict:
    """
    Extracts comprehensive technical media metadata using ffmpeg.
    Extracts: resolution, dimensions, aspect ratio, resolution category (4K, 1080p, 720p, SD),
    video/audio codecs, audio channels/layout (Stereo, 5.1, 7.1), HDR/SDR dynamic range,
    color depth (10-bit, 8-bit), FPS, duration, bitrate, and a standardized list of tags.
    Works seamlessly on both local unencrypted video files and streaming URLs.
    """
    info = {
        "width": None,
        "height": None,
        "resolution_label": "SD",
        "aspect_ratio": None,
        "duration_sec": 0.0,
        "duration_str": "",
        "video_codec": None,
        "video_profile": None,
        "fps": None,
        "pix_fmt": None,
        "color_depth": "8-bit",
        "dynamic_range": "SDR",
        "audio_codec": None,
        "audio_channels": None,
        "audio_layout": None,
        "bitrate_kbps": None,
        "tags": []
    }

    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [ffmpeg_exe, "-hide_banner", "-i", str(source_path)]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="ignore", timeout=timeout)
        out = res.stderr or ""

        # 1. Duration
        dur_m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", out)
        if dur_m:
            h, m, s = int(dur_m.group(1)), int(dur_m.group(2)), float(dur_m.group(3))
            sec = h * 3600 + m * 60 + s
            info["duration_sec"] = round(sec, 2)
            info["duration_str"] = f"{h}:{m:02d}:{int(s):02d}" if h > 0 else f"{m}:{int(s):02d}"
            if sec >= 3600:
                info["tags"].append("Feature Film")
            elif sec >= 900:
                info["tags"].append("Short Film")
            elif sec > 0:
                info["tags"].append("Clip")

        # 2. Overall Bitrate
        br_m = re.search(r"bitrate:\s*(\d+)\s*kb/s", out)
        if br_m:
            info["bitrate_kbps"] = int(br_m.group(1))

        # 3. Video Stream
        v_m = re.search(r"Stream #\d+:\d+.*?: Video:\s*([^\n\r]+)", out)
        if v_m:
            v_line = v_m.group(1)
            v_lower = v_line.lower()

            # Resolution & Aspect Ratio
            res_m = re.search(r"\b(\d{3,5})x(\d{3,5})\b", v_line)
            if res_m:
                w, h = int(res_m.group(1)), int(res_m.group(2))
                info["width"] = w
                info["height"] = h
                if h > 0:
                    ar = w / h
                    if 1.70 <= ar <= 1.85:
                        info["aspect_ratio"] = "16:9"
                    elif 2.25 <= ar <= 2.45:
                        info["aspect_ratio"] = "21:9"
                        info["tags"].append("21:9")
                    elif 1.30 <= ar <= 1.40:
                        info["aspect_ratio"] = "4:3"
                    else:
                        info["aspect_ratio"] = f"{ar:.2f}:1"

                max_dim = max(w, h)
                min_dim = min(w, h)
                if max_dim >= 3800 or min_dim >= 2100:
                    info["resolution_label"] = "4K (UHD)"
                    info["tags"].extend(["4K", "2160p"])
                elif max_dim >= 2500 or min_dim >= 1400:
                    info["resolution_label"] = "1440p (2K)"
                    info["tags"].extend(["1440p", "2K"])
                elif max_dim >= 1900 or min_dim >= 1000:
                    info["resolution_label"] = "1080p (Full HD)"
                    info["tags"].extend(["1080p", "Full HD"])
                elif max_dim >= 1200 or min_dim >= 700:
                    info["resolution_label"] = "720p (HD Ready)"
                    info["tags"].extend(["720p", "HD Ready"])
                elif max_dim >= 700 or min_dim >= 450:
                    info["resolution_label"] = "480p (SD)"
                    info["tags"].extend(["480p", "SD"])
                else:
                    info["resolution_label"] = "SD"
                    info["tags"].append("SD")

            # Video Codec
            if "hevc" in v_lower or "h265" in v_lower:
                info["video_codec"] = "H.265"
                info["tags"].append("H.265")
            elif "h264" in v_lower or "avc" in v_lower:
                info["video_codec"] = "H.264"
                info["tags"].append("H.264")
            elif "av1" in v_lower:
                info["video_codec"] = "AV1"
                info["tags"].append("AV1")
            elif "vp9" in v_lower:
                info["video_codec"] = "VP9"
                info["tags"].append("VP9")
            elif "mpeg4" in v_lower or "xvid" in v_lower:
                info["video_codec"] = "MPEG-4"
                info["tags"].append("MPEG-4")

            # Color depth (10-bit vs 8-bit)
            if any(p in v_lower for p in ("10le", "10be", "p010", "10-bit")):
                info["color_depth"] = "10-bit"
                info["tags"].append("10-bit")

            # Dynamic Range (HDR vs SDR)
            if "dovi" in v_lower or "dvhe" in v_lower:
                info["dynamic_range"] = "Dolby Vision"
                info["tags"].extend(["Dolby Vision", "HDR"])
            elif "smpte2084" in v_lower or "bt2020" in v_lower:
                info["dynamic_range"] = "HDR10"
                info["tags"].extend(["HDR10", "HDR"])
            elif "arib-std-b67" in v_lower:
                info["dynamic_range"] = "HLG"
                info["tags"].extend(["HLG", "HDR"])
            else:
                info["dynamic_range"] = "SDR"

            # FPS
            fps_m = re.search(r"\b(\d+(?:\.\d+)?)\s*(?:fps|tbr)\b", v_line)
            if fps_m:
                fps_val = float(fps_m.group(1))
                info["fps"] = round(fps_val, 2)
                if fps_val >= 50:
                    info["tags"].append("60fps")
                elif 23.5 <= fps_val <= 24.5:
                    info["tags"].append("24fps")

        # 4. Audio Stream
        a_m = re.search(r"Stream #\d+:\d+.*?: Audio:\s*([^\n\r]+)", out)
        if a_m:
            a_line = a_m.group(1)
            a_lower = a_line.lower()

            if "truehd" in a_lower:
                info["audio_codec"] = "Dolby TrueHD"
                info["tags"].append("TrueHD")
            elif "eac3" in a_lower:
                info["audio_codec"] = "Dolby Digital Plus"
                info["tags"].append("E-AC3")
                if "atmos" in a_lower:
                    info["tags"].append("Atmos")
            elif "ac3" in a_lower:
                info["audio_codec"] = "Dolby Digital"
                info["tags"].append("AC3")
            elif "dts" in a_lower:
                info["audio_codec"] = "DTS"
                info["tags"].append("DTS")
            elif "flac" in a_lower:
                info["audio_codec"] = "FLAC"
                info["tags"].append("FLAC")
            elif "aac" in a_lower:
                info["audio_codec"] = "AAC"
                info["tags"].append("AAC")
            elif "opus" in a_lower:
                info["audio_codec"] = "Opus"
                info["tags"].append("Opus")
            elif "mp3" in a_lower:
                info["audio_codec"] = "MP3"
                info["tags"].append("MP3")

            if "7.1" in a_lower:
                info["audio_channels"] = "7.1 Surround"
                info["tags"].append("7.1 Surround")
            elif "5.1" in a_lower:
                info["audio_channels"] = "5.1 Surround"
                info["tags"].append("5.1 Surround")
            elif "stereo" in a_lower:
                info["audio_channels"] = "Stereo"
                info["tags"].append("Stereo")
            elif "mono" in a_lower:
                info["audio_channels"] = "Mono"

    except Exception as e:
        pass

    # Deduplicate tags preserving order
    seen = set()
    info["tags"] = [t for t in info["tags"] if not (t.lower() in seen or seen.add(t.lower()))]
    return info


def extract_default_metadata(key_path: str, media_info: dict = None) -> dict:
    """
    Extracts sensible initial metadata from key path and folder structure,
    and merges technical resolution/codec tags from media_info if provided.
    """
    parts = key_path.replace("\\", "/").split("/")
    filename = parts[-1]
    stem = Path(filename).stem
    # Replace dots, underscores, dashes with space
    clean_title = re.sub(r"[_\.\-]+", " ", stem).strip().title()

    # Detect year (1900-2099)
    year = ""
    for part in parts:
        m = re.search(r"\b(19\d{2}|20\d{2})\b", part)
        if m:
            year = m.group(1)
            break

    # Detect genre/category from folder names
    genres = []
    category_hints = {
        "action": "Action",
        "comedy": "Comedy",
        "drama": "Drama",
        "scifi": "Sci-Fi",
        "sci-fi": "Sci-Fi",
        "horror": "Horror",
        "thriller": "Thriller",
        "animation": "Animation",
        "anime": "Anime",
        "documentary": "Documentary",
        "adventure": "Adventure",
        "fantasy": "Fantasy",
        "family": "Family",
        "crime": "Crime",
        "mystery": "Mystery"
    }

    for part in parts[:-1]:
        clean_part = part.lower().replace("-", " ").replace("_", " ")
        matched = False
        for k, v in category_hints.items():
            if k in clean_part:
                if v not in genres:
                    genres.append(v)
                matched = True
        if not matched and len(genres) == 0 and part:
            clean_folder = re.sub(r"(vault|collection|folder|dir|repo)", "", part, flags=re.I).strip(" -_")
            if clean_folder and clean_folder not in genres and not clean_folder.isdigit():
                genres.append(clean_folder.title())

    if not genres:
        genres = ["Movies"]

    tags = ["Streamable"]
    res_label = ""
    duration_str = ""
    if media_info:
        for t in media_info.get("tags", []):
            if t not in tags:
                tags.append(t)
        res_label = media_info.get("resolution_label", "")
        duration_str = media_info.get("duration_str", "")

    return {
        "title": clean_title,
        "year": year,
        "genres": genres,
        "tags": tags,
        "rating": "7.5",
        "overview": "",
        "resolution": res_label,
        "duration": duration_str,
        "file_names": [key_path],
        "aliases": []
    }


def generate_nfo_xml(metadata: dict, media_info: dict = None) -> str:
    """
    Generates standard Jellyfin / Kodi / Emby compliant XML (.nfo)
    supporting titles, years, plot, rating, genres, tags, and technical stream details.
    """
    title = saxutils.escape(str(metadata.get("title", "")))
    year = saxutils.escape(str(metadata.get("year", "")))
    plot = str(metadata.get("plot", "") or metadata.get("overview", "")).strip()
    if plot.startswith("Encrypted vault stream:") or plot.lower() == str(metadata.get("title", "")).strip().lower():
        plot = ""
    plot = saxutils.escape(plot)
    rating = saxutils.escape(str(metadata.get("rating", "")))

    genres = metadata.get("genres", [])
    if isinstance(genres, str):
        genres = [g.strip() for g in genres.split(",") if g.strip()]

    tags = metadata.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]

    lines = [
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>',
        '<movie>',
        f'    <title>{title}</title>',
        f'    <originaltitle>{title}</originaltitle>',
    ]

    if year:
        lines.append(f'    <year>{year}</year>')
    if plot:
        lines.append(f'    <plot>{plot}</plot>')
        lines.append(f'    <outline>{plot}</outline>')
    if rating:
        lines.append(f'    <rating>{rating}</rating>')

    for genre in genres:
        if genre:
            lines.append(f'    <genre>{saxutils.escape(str(genre))}</genre>')

    for tag in tags:
        if tag:
            lines.append(f'    <tag>{saxutils.escape(str(tag))}</tag>')

    # Keep and index all alternate file names / aliases for this video in Jellyfin
    aliases = metadata.get("aliases", [])
    if isinstance(aliases, str):
        aliases = [a.strip() for a in aliases.split(",") if a.strip()]

    file_names = metadata.get("file_names", [])
    if isinstance(file_names, str):
        file_names = [f.strip() for f in file_names.split(",") if f.strip()]

    all_alt_names = []
    for a in aliases:
        if a and a not in all_alt_names:
            all_alt_names.append(a)
    for fn in file_names:
        name_only = Path(str(fn)).name
        if name_only and name_only not in all_alt_names:
            all_alt_names.append(name_only)

    for alt in all_alt_names:
        if alt != title:
            lines.append(f'    <originaltitle>{saxutils.escape(str(alt))}</originaltitle>')
            lines.append(f'    <tag>Also: {saxutils.escape(str(alt))}</tag>')

    # Technical Media stream details for Jellyfin / Emby / Kodi
    mi = media_info or metadata.get("media_info")
    if mi:
        lines.append('    <fileinfo>')
        lines.append('        <streamdetails>')
        lines.append('            <video>')
        if mi.get("video_codec"):
            lines.append(f'                <codec>{saxutils.escape(str(mi["video_codec"]).lower())}</codec>')
        if mi.get("width"):
            lines.append(f'                <width>{mi["width"]}</width>')
        if mi.get("height"):
            lines.append(f'                <height>{mi["height"]}</height>')
        if mi.get("aspect_ratio"):
            lines.append(f'                <aspect>{saxutils.escape(str(mi["aspect_ratio"]))}</aspect>')
        if mi.get("duration_sec"):
            lines.append(f'                <durationinseconds>{int(mi["duration_sec"])}</durationinseconds>')
        lines.append('            </video>')
        if mi.get("audio_codec") or mi.get("audio_channels"):
            lines.append('            <audio>')
            if mi.get("audio_codec"):
                lines.append(f'                <codec>{saxutils.escape(str(mi["audio_codec"]).lower())}</codec>')
            if mi.get("audio_channels"):
                ch = 6 if "5.1" in str(mi["audio_channels"]) else (8 if "7.1" in str(mi["audio_channels"]) else (2 if "stereo" in str(mi["audio_channels"]).lower() else 1))
                lines.append(f'                <channels>{ch}</channels>')
            lines.append('            </audio>')
        lines.append('        </streamdetails>')
        lines.append('    </fileinfo>')

    lines.append('</movie>\n')
    return "\n".join(lines)


def write_nfo_files(library_dir: str, key_path: str, metadata: dict, media_info: dict = None):
    """
    Writes NFO files alongside the .strm in jellyfin_library.
    Writes:
      1. <name>.nfo (e.g. flight.mp4.nfo)
      2. <stem>.nfo (e.g. flight.nfo)
      3. <name>.strm.nfo (e.g. flight.mp4.strm.nfo)
    for 100% Jellyfin / Emby / Kodi compatibility.
    """
    base_dir = Path(library_dir)
    target_path = base_dir / key_path
    target_path.parent.mkdir(parents=True, exist_ok=True)

    xml_content = generate_nfo_xml(metadata, media_info=media_info)

    # 1. movie.mp4.nfo
    nfo1 = target_path.parent / f"{target_path.name}.nfo"
    with open(nfo1, "w", encoding="utf-8") as f:
        f.write(xml_content)

    # 2. movie.nfo
    clean_stem = target_path.stem
    nfo2 = target_path.parent / f"{clean_stem}.nfo"
    with open(nfo2, "w", encoding="utf-8") as f:
        f.write(xml_content)

    # 3. movie.mp4.strm.nfo
    nfo3 = target_path.parent / f"{target_path.name}.strm.nfo"
    with open(nfo3, "w", encoding="utf-8") as f:
        f.write(xml_content)


def delete_nfo_files(library_dir: str, key_path: str):
    """Deletes matching NFO files from library."""
    base_dir = Path(library_dir)
    target_path = base_dir / key_path

    for nfo_candidate in [
        target_path.parent / f"{target_path.name}.nfo",
        target_path.parent / f"{target_path.stem}.nfo",
        target_path.parent / f"{target_path.name}.strm.nfo"
    ]:
        if nfo_candidate.exists():
            try:
                nfo_candidate.unlink()
            except Exception:
                pass


def get_meta_blob_path(blob_path: str) -> str:
    """Computes sidecar path for encrypted metadata: blobs/xx/<hash>.meta.enc"""
    if blob_path.endswith(".enc"):
        return blob_path[:-4] + ".meta.enc"
    return f"{blob_path}.meta.enc"


def sync_item_metadata_to_hf(api: HfApi, repo_id: str, blob_path: str, record: dict, password: str):
    """
    Encrypts item metadata with AES-256-GCM and uploads as a sidecar
    blob on Hugging Face (e.g. blobs/xx/<hash>.meta.enc).
    Ensures ZERO plaintext leak of movie names, tags, categories, or folder paths.
    """
    if not blob_path:
        return False

    record["verifier"] = crypto_engine.create_verifier(password)
    meta_blob_path = get_meta_blob_path(blob_path)
    json_bytes = json.dumps(record).encode("utf-8")
    encrypted_bytes = crypto_engine.encrypt_bytes(json_bytes, password)

    try:
        api.upload_file(
            path_or_fileobj=io.BytesIO(encrypted_bytes),
            path_in_repo=meta_blob_path,
            repo_id=repo_id,
            repo_type="dataset",
            commit_message=commit_helper.get_human_commit_message("metadata")
        )
        return True
    except Exception as e:
        print(f"[!] Warning syncing metadata to HF ({repo_id}/{meta_blob_path}): {e}")
        return False


def delete_item_metadata_from_hf(api: HfApi, repo_id: str, blob_path: str):
    """Deletes the encrypted metadata sidecar from Hugging Face."""
    if not blob_path:
        return
    meta_blob_path = get_meta_blob_path(blob_path)
    try:
        api.delete_file(
            path_in_repo=meta_blob_path,
            repo_id=repo_id,
            repo_type="dataset",
            commit_message=commit_helper.get_human_commit_message("delete")
        )
    except Exception:
        pass


def sync_repo_manifest_to_hf(api: HfApi, repo_id: str, repo_files: dict, password: str):
    """
    Encrypts all file records belonging to a repository and uploads
    it as 'vault_index.enc' in the dataset root.
    """
    if not repo_files:
        return False
    for r in repo_files.values():
        if "verifier" not in r:
            r["verifier"] = crypto_engine.create_verifier(password)

    json_bytes = json.dumps(repo_files).encode("utf-8")
    encrypted_bytes = crypto_engine.encrypt_bytes(json_bytes, password)

    try:
        api.upload_file(
            path_or_fileobj=io.BytesIO(encrypted_bytes),
            path_in_repo="vault_index.enc",
            repo_id=repo_id,
            repo_type="dataset",
            commit_message=commit_helper.get_human_commit_message("metadata")
        )
        return True
    except Exception as e:
        print(f"[!] Warning syncing vault_index to HF ({repo_id}): {e}")
        return False


def sync_all_manifest_metadata_to_hf(api: HfApi, manifest: dict, password: str, hf_username: str, default_repo: str = "encrypted-media-vault"):
    """
    Syncs encrypted metadata for ALL files in manifest to their respective HF repos.
    Also creates per-repo 'vault_index.enc' files.
    """
    files_by_repo = {}
    synced_items = 0

    for key, info in manifest.get("files", {}).items():
        if info.get("status") == "uploaded":
            repo_id = info.get("repo_id", f"{hf_username}/{default_repo}")
            blob_path = info.get("blob_path") or info.get("remote_path")
            if blob_path:
                record = dict(info)
                record["full_key"] = key
                sync_item_metadata_to_hf(api, repo_id, blob_path, record, password)
                synced_items += 1

                if repo_id not in files_by_repo:
                    files_by_repo[repo_id] = {}
                files_by_repo[repo_id][key] = record

    # Push per-repo vault_index.enc
    for repo_id, r_files in files_by_repo.items():
        sync_repo_manifest_to_hf(api, repo_id, r_files, password)

    return synced_items


def restore_library_from_hf(api: HfApi, password: str, hf_username: str, library_dir="jellyfin_library", manifest_path="upload_manifest.json", proxy_host="127.0.0.1:8080"):
    """
    Recovers the entire Jellyfin library (manifest, .strm streams, and .nfo metadata)
    directly from Hugging Face by downloading and decrypting all vault indexes & sidecars.
    Any file that cannot be decrypted with the master password is skipped and logged.
    """
    manifest = {"files": {}, "media_hashes": {}}
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception:
            pass

    recovered_count = 0
    skipped_count = 0
    skipped_items = []
    token = api.token

    try:
        datasets = list(api.list_datasets(author=hf_username))
    except Exception as e:
        print(f"[!] Error listing HF datasets: {e}")
        return {"recovered_count": 0, "skipped_count": 0, "skipped_items": []}

    for ds in datasets:
        repo_id = ds.id
        try:
            files_in_repo = api.list_repo_files(repo_id=repo_id, repo_type="dataset")
        except Exception:
            continue

        repo_records = {}

        # 1. Try downloading vault_index.enc
        if "vault_index.enc" in files_in_repo:
            try:
                url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/vault_index.enc"
                resp = requests.get(url, headers={"Authorization": f"Bearer {token}"})
                if resp.status_code == 200:
                    dec = crypto_engine.decrypt_bytes(resp.content, password).decode("utf-8")
                    repo_records = json.loads(dec)
            except Exception as e:
                print(f"[!] Warning reading vault_index.enc for {repo_id}: {e}")

        # 2. Also check individual .meta.enc sidecars
        meta_files = [f for f in files_in_repo if f.endswith(".meta.enc")]
        for mf in meta_files:
            try:
                url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{mf}"
                resp = requests.get(url, headers={"Authorization": f"Bearer {token}"})
                if resp.status_code == 200:
                    dec = crypto_engine.decrypt_bytes(resp.content, password).decode("utf-8")
                    item = json.loads(dec)
                    k = item.get("full_key")
                    if k:
                        repo_records[k] = item
            except Exception as e:
                skipped_count += 1
                skipped_items.append({
                    "repo_id": repo_id,
                    "blob": mf,
                    "reason": "Failed to decrypt with current password"
                })

        # 3. Rebuild local files
        for key, rec in repo_records.items():
            # Verify password
            verifier = rec.get("verifier")
            if verifier and not crypto_engine.verify_password(verifier, password):
                skipped_count += 1
                skipped_items.append({
                    "key": key,
                    "repo_id": repo_id,
                    "blob": rec.get("blob_path"),
                    "reason": "Invalid encryption password"
                })
                continue

            manifest["files"][key] = rec
            media_hash = rec.get("media_hash")
            if media_hash:
                manifest["media_hashes"][media_hash] = rec

            # Recreate .strm
            strm_path = Path(library_dir) / f"{key}.strm"
            strm_path.parent.mkdir(parents=True, exist_ok=True)
            url = f"http://{proxy_host}/stream/{key}"
            with open(strm_path, "w", encoding="utf-8") as f:
                f.write(f"{url}\n")

            # Recreate .nfo files
            meta = rec.get("metadata") or extract_default_metadata(key)
            write_nfo_files(library_dir, key, meta)
            recovered_count += 1

    temp_path = f"{manifest_path}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    os.replace(temp_path, manifest_path)

    return {
        "recovered_count": recovered_count,
        "skipped_count": skipped_count,
        "skipped_items": skipped_items
    }


