import os
import io
import json
import re
import requests
import xml.sax.saxutils as saxutils
from pathlib import Path
from huggingface_hub import HfApi
import crypto_engine
import commit_helper


def extract_default_metadata(key_path: str) -> dict:
    """
    Extracts sensible initial metadata from key path and folder structure.
    Example: Action-Collection/2024/TopGun/flight.mp4
      -> Title: Flight
      -> Year: 2024
      -> Genre: Action
      -> Tags: ['Streamable']
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

    return {
        "title": clean_title,
        "year": year,
        "genres": genres,
        "tags": ["Streamable"],
        "rating": "7.5",
        "overview": "",
        "file_names": [key_path],
        "aliases": []
    }


def generate_nfo_xml(metadata: dict) -> str:
    """
    Generates standard Jellyfin / Kodi / Emby compliant XML (.nfo)
    supporting titles, years, plot, rating, genres, and tags.
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

    lines.append('</movie>\n')
    return "\n".join(lines)


def write_nfo_files(library_dir: str, key_path: str, metadata: dict):
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

    xml_content = generate_nfo_xml(metadata)

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


