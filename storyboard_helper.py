import os
import sys
import re
import io
import json
import time
import hashlib
import threading
import subprocess
from urllib.parse import quote, unquote
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import imageio_ffmpeg
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import crypto_engine

STORYBOARD_DIR = "storyboards"
THUMBS_DIR = os.path.join(STORYBOARD_DIR, "thumbs")
GENERATING_LOCK = threading.Lock()
ACTIVE_GENERATIONS = set()

# Deterministic 32-byte salt for sub-millisecond key derivation caching
STORYBOARD_MAGIC = b"HFMETA01"
STORYBOARD_SALT = b"HFSYNC_SB_SALT_v1_00000000000000"
_KEY_CACHE = {}

# Priority pool for the currently watched video (takes immediate priority)
PRIORITY_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="PriorityStoryboard")

os.makedirs(STORYBOARD_DIR, exist_ok=True)
os.makedirs(THUMBS_DIR, exist_ok=True)


def get_default_password() -> str:
    """Retrieves vault master password from env.txt or system environment."""
    try:
        import account_manager
        cfg = account_manager.load_env_config()
        pwd = cfg.get("ENCRYPTION_PASSWORD", "")
        if pwd:
            return pwd
    except Exception:
        pass
    return os.environ.get("ENCRYPTION_PASSWORD", "")


def get_sb_aesgcm(password: str = None) -> AESGCM:
    """Returns cached AESGCM instance for ultra-fast in-memory operations."""
    pwd = password or get_default_password()
    if not pwd:
        return None
    if pwd not in _KEY_CACHE:
        key = crypto_engine.derive_key(pwd, STORYBOARD_SALT)
        _KEY_CACHE[pwd] = AESGCM(key)
    return _KEY_CACHE[pwd]


def encrypt_bytes_sb(data: bytes, password: str = None) -> bytes:
    """Encrypts arbitrary byte payload using AES-256-GCM in HFMETA01 format."""
    pwd = password or get_default_password()
    if not pwd:
        return data
    aesgcm = get_sb_aesgcm(pwd)
    if not aesgcm:
        return data
    nonce = os.urandom(12)
    ct = aesgcm.encrypt(nonce, data, None)
    return STORYBOARD_MAGIC + STORYBOARD_SALT + nonce + ct


def decrypt_bytes_sb(payload: bytes, password: str = None) -> bytes:
    """Decrypts HFMETA01 payload in-memory with sub-millisecond cached AESGCM."""
    if not payload or len(payload) < 68 or not payload.startswith(STORYBOARD_MAGIC):
        return payload
    pwd = password or get_default_password()
    if not pwd:
        return payload
    salt = payload[8:40]
    nonce = payload[40:52]
    ciphertext = payload[52:]
    if salt == STORYBOARD_SALT:
        aesgcm = get_sb_aesgcm(pwd)
        if aesgcm:
            return aesgcm.decrypt(nonce, ciphertext, None)
    return crypto_engine.decrypt_bytes(payload, pwd)


def read_decrypted_bytes(file_path: str, password: str = None) -> bytes:
    """Transparently reads file and decrypts if encrypted with HFMETA01."""
    if not file_path or not os.path.exists(file_path):
        return b""
    try:
        with open(file_path, "rb") as f:
            data = f.read()
        if data.startswith(STORYBOARD_MAGIC):
            return decrypt_bytes_sb(data, password)
        return data
    except Exception as e:
        sys.stderr.write(f"[!] Error reading/decrypting {file_path}: {e}\n")
        return b""


def write_encrypted_bytes(file_path: str, data: bytes, password: str = None):
    """Encrypts and atomically writes data to file on disk."""
    pwd = password or get_default_password()
    enc_data = encrypt_bytes_sb(data, pwd) if pwd else data
    temp_path = f"{file_path}.tmp_enc"
    with open(temp_path, "wb") as f:
        f.write(enc_data)
    os.replace(temp_path, file_path)


def migrate_encrypt_all_storyboards(password: str = None) -> int:
    """
    Scans STORYBOARD_DIR (including thumbs and screens) and encrypts any
    unencrypted .jpg and .json files in-place using AES-256-GCM.
    Returns the count of newly encrypted files.
    """
    pwd = password or get_default_password()
    if not pwd:
        return 0
    migrated_count = 0
    for root, dirs, files in os.walk(STORYBOARD_DIR):
        for f in files:
            if f.endswith(".tmp.jpg") or f.endswith(".tmp") or f.endswith(".tmp_enc"):
                continue
            if f.endswith(".jpg") or f.endswith(".json"):
                full_path = os.path.join(root, f)
                try:
                    with open(full_path, "rb") as fp:
                        header = fp.read(8)
                    if header != STORYBOARD_MAGIC:
                        with open(full_path, "rb") as fp:
                            raw_data = fp.read()
                        if raw_data:
                            write_encrypted_bytes(full_path, raw_data, pwd)
                            migrated_count += 1
                except Exception as e:
                    sys.stderr.write(f"[!] Migration error on {full_path}: {e}\n")
    if migrated_count > 0:
        sys.stderr.write(f"[*] Zero-knowledge migration complete: encrypted {migrated_count} storyboard/thumb files on disk.\n")
    return migrated_count


def clean_key(key: str) -> str:
    """Consistently unquotes and cleans path separators."""
    if not key:
        return ""
    # Multiple unquote passes in case of double-URL encoding
    k = str(key).strip().replace("\\", "/")
    k = unquote(unquote(k))
    return k


def get_safe_id(key: str) -> str:
    """Generates a deterministic filesystem-safe hash for a key."""
    ck = clean_key(key)
    return hashlib.sha256(ck.encode("utf-8")).hexdigest()[:24]


def update_storyboard_duration(key: str, real_duration: float, password: str = None) -> bool:
    """Updates existing storyboard metadata JSON with the true probed duration."""
    if not real_duration or real_duration <= 0:
        return False
    ck = clean_key(key)
    safe_id = get_safe_id(ck)
    json_path = os.path.join(STORYBOARD_DIR, f"{safe_id}.json")
    if os.path.exists(json_path):
        try:
            raw = read_decrypted_bytes(json_path, password)
            data = json.loads(raw.decode("utf-8"))
            data["duration"] = round(real_duration, 2)
            if data.get("total_frames"):
                data["interval"] = round(real_duration / data["total_frames"], 3)
            write_encrypted_bytes(json_path, json.dumps(data, indent=2).encode("utf-8"), password)
            return True
        except Exception:
            pass
    return False


def get_storyboard_meta(key: str, password: str = None) -> dict:
    ck = clean_key(key)
    safe_id = get_safe_id(ck)
    jpg_path = os.path.join(STORYBOARD_DIR, f"{safe_id}.jpg")
    json_path = os.path.join(STORYBOARD_DIR, f"{safe_id}.json")

    if os.path.exists(jpg_path) and os.path.exists(json_path):
        try:
            raw = read_decrypted_bytes(json_path, password)
            data = json.loads(raw.decode("utf-8"))
            data["available"] = True
            mtime = int(os.path.getmtime(jpg_path)) if os.path.exists(jpg_path) else int(time.time())
            data["sprite_url"] = f"/api/storyboard/image?id={safe_id}&v={mtime}"
            data["mtime"] = mtime
            return data
        except Exception:
            pass

    with GENERATING_LOCK:
        is_generating = safe_id in ACTIVE_GENERATIONS

    return {
        "available": False,
        "status": "generating" if is_generating else "not_generated",
        "safe_id": safe_id,
        "key": ck
    }


def get_storyboard_image_path(safe_id: str) -> str:
    clean_id = clean_key(safe_id)
    jpg_path = os.path.join(STORYBOARD_DIR, f"{clean_id}.jpg")
    if os.path.exists(jpg_path):
        return jpg_path
    return None


def get_storyboard_version(safe_id_or_key: str) -> int:
    """
    Returns the modification timestamp of the storyboard image as an integer version tag.
    Returns 0 if no storyboard exists.
    """
    try:
        sid = str(safe_id_or_key).strip()
        if len(sid) != 24 or not all(c in "0123456789abcdefABCDEF" for c in sid):
            sid = get_safe_id(clean_key(sid))
        jpg_path = os.path.join(STORYBOARD_DIR, f"{sid}.jpg")
        if os.path.exists(jpg_path):
            return int(os.path.getmtime(jpg_path))
    except Exception:
        pass
    return 0


def get_poster_thumb_path(safe_id: str, password: str = None) -> str:
    clean_id = clean_key(safe_id)
    thumb_path = os.path.join(THUMBS_DIR, f"{clean_id}.jpg")
    if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 100:
        return thumb_path
    return slice_poster_thumbnail(clean_id, password=password)


def _slice_thumb_from_image(img, safe_id: str, overwrite: bool = False, password: str = None) -> str:
    """Internal helper to slice a 480x270 thumbnail from a PIL Image and save encrypted."""
    clean_id = clean_key(safe_id)
    out_thumb = os.path.join(THUMBS_DIR, f"{clean_id}.jpg")
    if not overwrite and os.path.exists(out_thumb) and os.path.getsize(out_thumb) > 100:
        return out_thumb

    try:
        from PIL import Image, ImageStat
        w, h = img.size
        cols, rows = 10, 10
        tw = w // cols
        th = h // rows

        candidate_frames = [2, 3, 4, 5, 6, 8, 10, 12, 15]
        best_frame = 3
        best_score = -1.0

        for f_idx in candidate_frames:
            c = f_idx % cols
            r = f_idx // cols
            tile_box = (c * tw, r * th, (c + 1) * tw, (r + 1) * th)
            crop_sample = img.crop(tile_box)
            stat = ImageStat.Stat(crop_sample)
            mean_b = sum(stat.mean) / len(stat.mean)
            var_b = sum(stat.var) / len(stat.var) if stat.var else 0.0

            if mean_b < 20 or mean_b > 245:
                score = 0.0
            else:
                score = var_b * (1.0 - abs(mean_b - 128.0) / 128.0)

            if score > best_score:
                best_score = score
                best_frame = f_idx

        best_c = best_frame % cols
        best_r = best_frame // cols
        crop = img.crop((best_c * tw, best_r * th, (best_c + 1) * tw, (best_r + 1) * th))
        thumb = crop.resize((480, 270), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        thumb.save(buf, "JPEG", quality=92, optimize=True)
        write_encrypted_bytes(out_thumb, buf.getvalue(), password)
        return out_thumb
    except Exception as e:
        sys.stderr.write(f"[!] Error slicing poster thumbnail for {clean_id}: {e}\n")
        return None


def slice_poster_thumbnail(safe_id: str, overwrite: bool = False, password: str = None) -> str:
    """
    Slices a razor-sharp, pixel-perfect 16:9 poster thumbnail (480x270)
    from the 100-frame storyboard. Reads encrypted storyboard and saves encrypted thumbnail.
    """
    clean_id = clean_key(safe_id)
    out_thumb = os.path.join(THUMBS_DIR, f"{clean_id}.jpg")

    if not overwrite and os.path.exists(out_thumb) and os.path.getsize(out_thumb) > 100:
        return out_thumb

    sb_jpg = os.path.join(STORYBOARD_DIR, f"{clean_id}.jpg")
    if not os.path.exists(sb_jpg):
        return None

    try:
        from PIL import Image
        raw_sb = read_decrypted_bytes(sb_jpg, password)
        if not raw_sb:
            return None
        with Image.open(io.BytesIO(raw_sb)) as img:
            return _slice_thumb_from_image(img, clean_id, overwrite=overwrite, password=password)
    except Exception as e:
        sys.stderr.write(f"[!] Error slicing poster thumbnail for {clean_id}: {e}\n")
        return None


def add_timestamps_to_storyboard(jpg_path: str, duration: float, cols: int = 10, rows: int = 10, password: str = None) -> bool:
    """
    Overlays professional translucent timestamp badges (e.g. 00:05:24) in the bottom-right
    corner of each tile in the 100-frame storyboard. Supports both plain temporary files
    and encrypted files on disk.
    """
    if not os.path.exists(jpg_path):
        return False
    try:
        from PIL import Image, ImageDraw, ImageFont

        dur = float(duration) if duration and duration > 0 else 180.0
        total_frames = cols * rows

        is_plain = False
        with open(jpg_path, "rb") as f_check:
            magic = f_check.read(8)
        if magic != STORYBOARD_MAGIC:
            is_plain = True

        if is_plain:
            base_im = Image.open(jpg_path)
        else:
            raw_bytes = read_decrypted_bytes(jpg_path, password)
            if not raw_bytes:
                return False
            base_im = Image.open(io.BytesIO(raw_bytes))

        with base_im:
            im = base_im.convert("RGBA")
            overlay = Image.new("RGBA", im.size, (255, 255, 255, 0))
            draw = ImageDraw.Draw(overlay)

            tw = im.width // cols
            th = im.height // rows

            font = None
            font_paths = [
                "C:/Windows/Fonts/arialbd.ttf",
                "C:/Windows/Fonts/arial.ttf",
                "C:/Windows/Fonts/segoeui.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            ]
            for fp in font_paths:
                if os.path.exists(fp):
                    try:
                        font = ImageFont.truetype(fp, 18)
                        break
                    except Exception:
                        pass
            if not font:
                font = ImageFont.load_default()

            for idx in range(total_frames):
                c = idx % cols
                r = idx // cols
                x0 = c * tw
                y0 = r * th

                t_sec = int(round((idx / float(total_frames)) * dur))
                m, s = divmod(t_sec, 60)
                h, m = divmod(m, 60)
                time_str = f"{h:02d}:{m:02d}:{s:02d}" if dur >= 3600 else f"{m:02d}:{s:02d}"

                bbox = draw.textbbox((0, 0), time_str, font=font)
                text_w = bbox[2] - bbox[0]
                text_h = bbox[3] - bbox[1]

                pad_x = 7
                pad_y = 3
                badge_w = text_w + pad_x * 2
                badge_h = text_h + pad_y * 2

                bx2 = x0 + tw - 8
                by2 = y0 + th - 8
                bx1 = bx2 - badge_w
                by1 = by2 - badge_h

                draw.rounded_rectangle([bx1, by1, bx2, by2], radius=4, fill=(0, 0, 0, 195), outline=(255, 255, 255, 50))
                draw.text((bx1 + pad_x, by1 + pad_y - 1), time_str, fill=(255, 255, 255, 245), font=font)

            out = Image.alpha_composite(im, overlay).convert("RGB")
            if is_plain and jpg_path.endswith(".tmp.jpg"):
                out.save(jpg_path, "JPEG", quality=92, optimize=True)
            else:
                out_buf = io.BytesIO()
                out.save(out_buf, "JPEG", quality=92, optimize=True)
                write_encrypted_bytes(jpg_path, out_buf.getvalue(), password)
            return True
    except Exception as e:
        sys.stderr.write(f"[!] Warning overlaying timestamps on {jpg_path}: {e}\n")
        return False


def has_storyboard(key: str) -> bool:
    ck = clean_key(key)
    safe_id = get_safe_id(ck)
    jpg_path = os.path.join(STORYBOARD_DIR, f"{safe_id}.jpg")
    json_path = os.path.join(STORYBOARD_DIR, f"{safe_id}.json")
    return os.path.exists(jpg_path) and os.path.exists(json_path)


def probe_duration(input_source: str) -> float:
    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [ffmpeg_exe, "-i", input_source]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=8)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", p.stderr)
        if m:
            hours = int(m.group(1))
            minutes = int(m.group(2))
            seconds = float(m.group(3))
            return hours * 3600 + minutes * 60 + seconds
    except Exception:
        pass
    return 0.0


def generate_storyboard(key: str, duration: float = 0, port: int = 8080, local_path: str = None, overwrite: bool = False, password: str = None) -> bool:
    ck = clean_key(key)
    safe_id = get_safe_id(ck)

    jpg_path = os.path.join(STORYBOARD_DIR, f"{safe_id}.jpg")
    json_path = os.path.join(STORYBOARD_DIR, f"{safe_id}.json")

    if not overwrite and os.path.exists(jpg_path) and os.path.exists(json_path):
        return True

    with GENERATING_LOCK:
        if safe_id in ACTIVE_GENERATIONS:
            return False
        ACTIVE_GENERATIONS.add(safe_id)

    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        input_source = None

        if local_path and os.path.exists(local_path):
            input_source = local_path
        else:
            # Fallback to streaming proxy endpoint with clean quoting and no_cache flag to never pollute stream cache
            encoded_key = quote(ck, safe="/")
            input_source = f"http://127.0.0.1:{port}/stream/{encoded_key}?full=1&no_cache=1"

        cols = 10
        rows = 10
        total_frames = cols * rows  # 100 frames
        thumb_width = 384
        thumb_height = 216

        dur = float(duration) if duration and duration > 5 else 0.0
        if dur <= 5:
            dur = probe_duration(input_source)
        if dur <= 5:
            dur = 180.0
            
        fps_val = (total_frames + 0.5) / max(5.0, dur)

        temp_jpg = f"{jpg_path}.tmp.jpg"
        filter_str = (
            f"fps={fps_val:.4f},"
            f"scale={thumb_width}:{thumb_height}:flags=lanczos:force_original_aspect_ratio=decrease,"
            f"pad={thumb_width}:{thumb_height}:(ow-iw)/2:(oh-ih)/2:black,"
            f"tile={cols}x{rows}"
        )

        cmd = [
            ffmpeg_exe,
            "-y",
            "-skip_frame", "nokey",
            "-i", input_source,
            "-vf", filter_str,
            "-frames:v", "1",
            "-q:v", "2",
            temp_jpg
        ]

        res = subprocess.run(cmd, capture_output=True, text=True, timeout=50)
        if res.returncode == 0 and os.path.exists(temp_jpg) and os.path.getsize(temp_jpg) > 1000:
            # 1. Overlay timestamps directly on the plain temp_jpg before encrypting
            try:
                add_timestamps_to_storyboard(temp_jpg, duration=dur, cols=cols, rows=rows, password=password)
            except Exception:
                pass

            # 2. Slice thumbnail from the plain temp_jpg directly into out_thumb (encrypted)
            try:
                from PIL import Image
                with Image.open(temp_jpg) as temp_im:
                    _slice_thumb_from_image(temp_im, safe_id, overwrite=True, password=password)
            except Exception:
                pass

            # 3. Read final temp_jpg, encrypt and save to jpg_path
            with open(temp_jpg, "rb") as f_tmp:
                final_sb_bytes = f_tmp.read()
            write_encrypted_bytes(jpg_path, final_sb_bytes, password)

            # 4. Remove plain temporary file immediately
            try:
                os.remove(temp_jpg)
            except Exception:
                pass

            # 5. Encrypt and write metadata JSON
            meta = {
                "key": ck,
                "safe_id": safe_id,
                "cols": cols,
                "rows": rows,
                "thumb_width": thumb_width,
                "thumb_height": thumb_height,
                "total_frames": total_frames,
                "duration": dur,
                "interval": round(dur / total_frames, 2)
            }
            write_encrypted_bytes(json_path, json.dumps(meta, indent=2).encode("utf-8"), password)

            try:
                import activity_tracker
                activity_tracker.tracker.notify_manifest_updated()
            except Exception:
                pass
            return True
        else:
            if os.path.exists(temp_jpg):
                try:
                    os.remove(temp_jpg)
                except Exception:
                    pass
            return False
    except Exception as e:
        sys.stderr.write(f"[!] Warning generating storyboard for {ck}: {e}\n")
        return False
    finally:
        with GENERATING_LOCK:
            ACTIVE_GENERATIONS.discard(safe_id)


def trigger_priority_storyboard(key: str, duration: float = 0, port: int = 8080, local_path: str = None):
    """Submits the storyboard generation to the PRIORITY pool for the currently active/watched video."""
    ck = clean_key(key)
    safe_id = get_safe_id(ck)

    jpg_path = os.path.join(STORYBOARD_DIR, f"{safe_id}.jpg")
    if os.path.exists(jpg_path):
        return
    PRIORITY_POOL.submit(
        generate_storyboard,
        key=ck,
        duration=duration,
        port=port,
        local_path=local_path
    )


def trigger_background_storyboard(key: str, duration: float = 0, port: int = 8080, local_path: str = None):
    trigger_priority_storyboard(key=key, duration=duration, port=port, local_path=local_path)


def prewarm_all_storyboards(manifest_path="upload_manifest.json", port: int = 8080):
    """Background pre-warmer that quietly generates storyboards for all existing library items."""
    def _worker():
        try:
            if not os.path.exists(manifest_path):
                return
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            files = list(manifest.get("files", {}).items())
            for k, info in files:
                ck = clean_key(k)
                safe_id = get_safe_id(ck)
                jpg_path = os.path.join(STORYBOARD_DIR, f"{safe_id}.jpg")
                if not os.path.exists(jpg_path):
                    time.sleep(1.2)
                    generate_storyboard(ck, duration=0, port=port)
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()


# Automatically ensure all existing storyboards, thumbs, and screens on disk are encrypted with AES-256-GCM
try:
    migrate_encrypt_all_storyboards()
except Exception:
    pass
