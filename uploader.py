import os
import sys
import json
import time
import argparse
from pathlib import Path
from huggingface_hub import HfApi
import crypto_engine
import commit_helper

# Common video and media extensions
SUPPORTED_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm",
    ".flv", ".ts", ".m4v", ".srt", ".vtt", ".nfo"
}

MANIFEST_FILE = "upload_manifest.json"


def load_env(env_path="env.txt"):
    """Load key-value settings from env.txt if available."""
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
    """Loads upload history manifest."""
    if os.path.exists(MANIFEST_FILE):
        try:
            with open(MANIFEST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_manifest(manifest):
    """Saves upload history manifest atomically."""
    temp_path = f"{MANIFEST_FILE}.tmp"
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    os.replace(temp_path, MANIFEST_FILE)


def scan_media_files(directory):
    """Recursively scans directory for supported media files."""
    media_files = []
    base_dir = Path(directory).resolve()
    for root, _, files in os.walk(base_dir):
        for file in files:
            file_path = Path(root) / file
            if file_path.suffix.lower() in SUPPORTED_EXTENSIONS:
                # Do not scan our temporary files or .enc files
                if not file.endswith(".tmp.enc") and not file.endswith(".enc"):
                    rel_path = file_path.relative_to(base_dir)
                    media_files.append((file_path, str(rel_path).replace("\\", "/")))
    return media_files


def sync_folder(
    input_dir: str,
    repo_id: str,
    token: str,
    password: str,
    delete_local: bool = True,
    dry_run: bool = False
):
    print("=" * 65)
    print("  HFSYNC: Encrypted Video Uploader to Hugging Face")
    print("=" * 65)
    print(f"Target Repo     : https://huggingface.co/datasets/{repo_id}")
    print(f"Input Directory : {input_dir}")
    print(f"Auto-Delete     : {'YES (one-by-one after upload)' if delete_local else 'NO (keep local files)'}")
    print(f"Dry Run Mode    : {'ENABLED' if dry_run else 'DISABLED'}")
    print("=" * 65)

    api = HfApi(token=token)

    # Ensure repository exists as public dataset
    try:
        api.create_repo(repo_id=repo_id, repo_type="dataset", private=False, exist_ok=True)
    except Exception as e:
        print(f"[!] Warning checking/creating repository: {e}")

    media_files = scan_media_files(input_dir)
    if not media_files:
        print("[i] No media files found to upload in target directory.")
        return

    print(f"[+] Discovered {len(media_files)} media file(s).")

    manifest = load_manifest()
    success_count = 0
    fail_count = 0

    for idx, (abs_path, rel_path) in enumerate(media_files, 1):
        file_size = os.path.getsize(abs_path)
        size_mb = file_size / (1024 * 1024)
        remote_enc_name = f"{rel_path}.enc"

        print(f"\n[{idx}/{len(media_files)}] Processing: {rel_path} ({size_mb:.2f} MB)")

        if rel_path in manifest and manifest[rel_path].get("status") == "uploaded":
            print(f"    -> Already marked as uploaded in manifest. Skipping.")
            continue

        if dry_run:
            print(f"    [DRY RUN] Would encrypt -> upload {remote_enc_name} -> delete {abs_path}")
            continue

        temp_enc_file = f"{abs_path}.tmp.enc"

        try:
            # 1. Encrypt chunk-by-chunk using AES-256-GCM
            t0 = time.time()
            print(f"    1. Encrypting (AES-256-GCM, 64KB blocks)...", end="", flush=True)
            crypto_engine.encrypt_file(str(abs_path), temp_enc_file, password)
            enc_size = os.path.getsize(temp_enc_file)
            enc_time = time.time() - t0
            print(f" Done ({enc_time:.2f}s, enc size: {enc_size / (1024 * 1024):.2f} MB)")

            # 2. Upload encrypted file to HF dataset
            print(f"    2. Uploading to HF dataset ({remote_enc_name})...")
            commit_info = api.upload_file(
                path_or_fileobj=temp_enc_file,
                path_in_repo=remote_enc_name,
                repo_id=repo_id,
                repo_type="dataset",
                commit_message=commit_helper.get_human_commit_message("upload")
            )
            print(f"       Upload verified: {commit_info}")

            # 3. Clean up temp encrypted file
            if os.path.exists(temp_enc_file):
                os.remove(temp_enc_file)

            # 4. Safely delete the original local file to free disk space
            if delete_local:
                os.remove(abs_path)
                print(f"    3. Successfully deleted local plaintext file: {abs_path}")
            else:
                print(f"    3. Kept local plaintext file (--no-delete active).")

            # 5. Record to manifest
            manifest[rel_path] = {
                "remote_path": remote_enc_name,
                "orig_size": file_size,
                "enc_size": enc_size,
                "uploaded_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "status": "uploaded"
            }
            save_manifest(manifest)
            success_count += 1

        except Exception as e:
            print(f"\n[!] Error processing {rel_path}: {e}")
            fail_count += 1
            if os.path.exists(temp_enc_file):
                try:
                    os.remove(temp_enc_file)
                except Exception:
                    pass

    print("\n" + "=" * 65)
    print(f"Sync complete. Uploaded: {success_count}, Failed: {fail_count}, Total: {len(media_files)}")
    print("=" * 65)


def main():
    env_cfg = load_env()

    parser = argparse.ArgumentParser(description="Upload encrypted video files to Hugging Face dataset and delete locally.")
    parser.add_argument("--input-dir", "-i", default="videos_to_sync", help="Input directory containing videos to upload (default: videos_to_sync)")
    parser.add_argument("--repo-id", "-r", default=env_cfg.get("REPO_ID", "myprotjj/encrypted-media-vault"), help="HF Dataset Repository ID")
    parser.add_argument("--token", "-t", default=env_cfg.get("HF_TOKEN"), help="Hugging Face API token")
    parser.add_argument("--password", "-p", default=env_cfg.get("ENCRYPTION_PASSWORD"), help="Master encryption password")
    parser.add_argument("--no-delete", action="store_true", help="Do not delete local plaintext files after upload")
    parser.add_argument("--dry-run", action="store_true", help="Simulate actions without encrypting, uploading, or deleting")

    args = parser.parse_args()

    if not args.token:
        print("[!] Error: HF_TOKEN not provided in arguments or env.txt")
        sys.exit(1)
    if not args.password:
        print("[!] Error: ENCRYPTION_PASSWORD not provided in arguments or env.txt")
        sys.exit(1)
    if not os.path.exists(args.input_dir):
        os.makedirs(args.input_dir, exist_ok=True)
        print(f"[i] Created empty input directory: {args.input_dir}. Place your videos there and re-run.")
        sys.exit(0)

    sync_folder(
        input_dir=args.input_dir,
        repo_id=args.repo_id,
        token=args.token,
        password=args.password,
        delete_local=not args.no_delete,
        dry_run=args.dry_run
    )


if __name__ == "__main__":
    main()
