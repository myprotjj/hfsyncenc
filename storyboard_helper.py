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



STORYBOARD_CONFIG_FILE = os.path.join(STORYBOARD_DIR, "storyboard_config.json")





def get_storyboard_config() -> dict:

    """Returns storyboard generator configuration with defaults."""

    cfg = {

        "engine": "pyav",                     # "pyav" (Fast In-Memory 100 True Frames) or "legacy" (FFmpeg CLI 100 True Frames)

        "timestamp_position": "bottom-right", # "bottom-right" (default) or "bottom-left"

        "show_timestamps": True,

        "cols": 10,

        "rows": 10,

        "total_frames": 100

    }

    if os.path.exists(STORYBOARD_CONFIG_FILE):

        try:

            raw = read_decrypted_bytes(STORYBOARD_CONFIG_FILE)

            if raw:

                data = json.loads(raw.decode("utf-8"))

                if isinstance(data, dict):

                    cfg.update(data)

        except Exception:

            pass

    return cfg





def set_storyboard_config(updates: dict) -> dict:

    """Updates storyboard generator configuration."""

    cfg = get_storyboard_config()

    if isinstance(updates, dict):

        cfg.update(updates)

    try:

        with open(STORYBOARD_CONFIG_FILE, "w", encoding="utf-8") as f:

            json.dump(cfg, f, indent=2)

    except Exception as e:

        sys.stderr.write(f"[!] Error saving storyboard config: {e}\n")

    return cfg





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







def get_optimal_worker_threads() -> int:

    """

    Dynamically detects host processor logical cores and calculates ideal concurrency.

    ALWAYS leaves at least 1-2 cores free for Windows desktop, OS, and user apps.

    """

    cores = os.cpu_count() or 4

    # On 4-core CPU: use at most 2 threads, leaving 2 cores (50%) free for OS and UI!

    return max(1, min(cores - 1, 3))





def get_optimal_ffmpeg_threads() -> int:

    """

    Dynamically detects host processor logical cores for FFmpeg decoding.

    Caps FFmpeg decode threads so background video encoding never freezes desktop UI.

    """

    cores = os.cpu_count() or 4

    if cores <= 2:

        return 1

    elif cores <= 4:

        return 2

    else:

        return 3





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

    """Transparently reads file and decrypts if encrypted with HFMETA01 (unpeels multi-layer encryption if present)."""

    if not file_path or not os.path.exists(file_path):

        return b""

    try:

        with open(file_path, "rb") as f:

            data = f.read()

        depth = 0

        while data.startswith(STORYBOARD_MAGIC) and depth < 3:

            dec = decrypt_bytes_sb(data, password)

            if not dec or dec == data:

                break

            data = dec

            depth += 1

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

            if f.endswith(".tmp.jpg") or f.endswith(".tmp") or f.endswith(".tmp_enc") or f == "storyboard_config.json":

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





_MANIFEST_CACHE = None

_MANIFEST_CACHE_MTIME = 0

_SAFE_ID_CACHE = {}





def get_manifest_data() -> dict:

    global _MANIFEST_CACHE, _MANIFEST_CACHE_MTIME, _SAFE_ID_CACHE

    try:

        if os.path.exists("upload_manifest.json"):

            mtime = os.path.getmtime("upload_manifest.json")

            if _MANIFEST_CACHE is not None and mtime == _MANIFEST_CACHE_MTIME:

                return _MANIFEST_CACHE

            with open("upload_manifest.json", "r", encoding="utf-8") as f_mf:

                _MANIFEST_CACHE = json.load(f_mf)

                _MANIFEST_CACHE_MTIME = mtime

                _SAFE_ID_CACHE.clear()

                return _MANIFEST_CACHE

    except Exception:

        pass

    return {"files": {}}





def get_safe_id(key: str) -> str:

    """Generates a permanent content-based safe hash for a video (media_hash[:24]) or key hash fallback."""

    if not key:

        return ""

    ck = clean_key(key)

    if ck in _SAFE_ID_CACHE:

        return _SAFE_ID_CACHE[ck]

    try:

        manifest = get_manifest_data()

        files = manifest.get("files", {})

        info = files.get(ck)

        if not info:

            for k, v in files.items():

                all_names = (v.get("file_names") or []) + (v.get("aliases") or []) + (v.get("metadata", {}).get("file_names") or [])

                rel = v.get("rel_in_repo") or ""

                folder = v.get("repo_folder") or ""

                combo = f"{folder}/{rel}".strip("/")

                if ck in all_names or ck == rel or ck == combo:

                    info = v

                    break



        if not info:

            try:

                import staging_manager

                stg = staging_manager.load_staging_manifest()

                for rf, rdata in stg.get("repositories", {}).items():

                    for f in rdata.get("files", []):

                        rec = f.get("record", {})

                        fk = f.get("full_key") or f.get("orig_filename") or ""

                        all_names = [fk, Path(fk).name, f.get("orig_filename", "")]

                        if ck in all_names or Path(ck).name == Path(fk).name:

                            info = rec

                            break

                    if info:

                        break

            except Exception:

                pass



        if not info:

            try:

                import staging_manager

                stg = staging_manager.load_staging_manifest()

                for rf, rdata in stg.get("repositories", {}).items():

                    for f in rdata.get("files", []):

                        rec = f.get("record", {})

                        fk = f.get("full_key") or f.get("orig_filename") or ""

                        all_names = [fk, Path(fk).name, f.get("orig_filename", "")]

                        if ck in all_names or Path(ck).name == Path(fk).name:

                            info = rec

                            break

                    if info:

                        break

            except Exception:

                pass

        if info:

            m_hash = info.get("media_hash") or info.get("content_hash")

            if m_hash:

                res = m_hash[:24]

                _SAFE_ID_CACHE[ck] = res

                return res

            if info.get("safe_id"):

                res = info.get("safe_id")

                _SAFE_ID_CACHE[ck] = res

                return res

    except Exception:

        pass

    res = hashlib.sha256(ck.encode("utf-8")).hexdigest()[:24]

    _SAFE_ID_CACHE[ck] = res

    return res





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





def add_timestamps_to_storyboard(jpg_path: str, duration: float, cols: int = 10, rows: int = 10, password: str = None, position: str = None, sparse_hash: str = None) -> bool:

    """

    Overlays crisp, modern translucent timestamp badges (e.g. 00:05:24) in the bottom-left

    corner of each tile in the 100-frame storyboard. Supports both plain temporary files

    and encrypted files on disk. Zero performance cost (<0.1s total).

    """

    if not os.path.exists(jpg_path):

        return False

    try:

        from PIL import Image, ImageDraw, ImageFont



        cfg = get_storyboard_config()

        if not cfg.get("show_timestamps", True):

            return True

        pos = position or cfg.get("timestamp_position", "bottom-right")



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



            font_size = 15 if tw >= 300 else 11

            font = None

            font_paths = [

                "C:/Windows/Fonts/segoeuib.ttf",

                "C:/Windows/Fonts/arialbd.ttf",

                "C:/Windows/Fonts/arial.ttf",

                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

            ]

            for fp in font_paths:

                if os.path.exists(fp):

                    try:

                        font = ImageFont.truetype(fp, font_size)

                        break

                    except Exception:

                        pass

            if not font:

                font = ImageFont.load_default()



            pad_x = 7 if tw >= 300 else 5

            pad_y = 4 if tw >= 300 else 3

            margin_x = 10 if tw >= 300 else 6

            margin_y = 18 if tw >= 300 else 12  # A little up from the bottom edge



            for idx in range(total_frames):

                c = idx % cols

                r = idx // cols

                x0 = c * tw

                y0 = r * th



                t_sec = int(round((idx / float(max(1, total_frames - 1))) * dur))

                m, s = divmod(t_sec, 60)

                h, m = divmod(m, 60)

                time_str = f"{h:02d}:{m:02d}:{s:02d}" if dur >= 3600 else f"{m:02d}:{s:02d}"



                bbox = draw.textbbox((0, 0), time_str, font=font)

                text_w = bbox[2] - bbox[0]

                text_h = bbox[3] - bbox[1]



                badge_w = text_w + pad_x * 2

                badge_h = text_h + pad_y * 2



                if pos == "bottom-right":

                    bx2 = x0 + tw - margin_x

                    by2 = y0 + th - margin_y

                    bx1 = bx2 - badge_w

                    by1 = by2 - badge_h

                else:  # bottom-left

                    bx1 = x0 + margin_x

                    by2 = y0 + th - margin_y

                    bx2 = bx1 + badge_w

                    by1 = by2 - badge_h



                # Crisp modern pill styling: semi-opaque dark slate with subtle light border

                draw.rounded_rectangle([bx1, by1, bx2, by2], radius=4, fill=(12, 17, 28, 220), outline=(255, 255, 255, 60))

                draw.text((bx1 + pad_x, by1 + pad_y - 1), time_str, fill=(255, 255, 255, 255), font=font)



            # Overlay unobtrusive, high-definition sparse hash fingerprint on Tile 0 (top-left of full sheet)

            if sparse_hash:

                try:

                    hash_font = None

                    mono_paths = [

                        "C:/Windows/Fonts/consola.ttf",

                        "C:/Windows/Fonts/consolab.ttf",

                        "C:/Windows/Fonts/courbd.ttf",

                        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

                    ]

                    for mp in mono_paths:

                        if os.path.exists(mp):

                            try:

                                hash_font = ImageFont.truetype(mp, 9 if tw >= 300 else 7)

                                break

                            except Exception:

                                pass

                    if not hash_font:

                        hash_font = font



                    hash_str = str(sparse_hash).strip()

                    label = "HASH: "

                    lbl_box = draw.textbbox((0, 0), label, font=hash_font)

                    lbl_w = lbl_box[2] - lbl_box[0]

                    h_box = draw.textbbox((0, 0), hash_str, font=hash_font)

                    h_w = h_box[2] - h_box[0]

                    h_h = max(lbl_box[3] - lbl_box[1], h_box[3] - h_box[1])



                    pad_hx = 6

                    pad_hy = 3

                    hx1 = margin_x

                    hy1 = 8 if tw >= 300 else 5

                    hx2 = hx1 + lbl_w + h_w + pad_hx * 2

                    hy2 = hy1 + h_h + pad_hy * 2



                    draw.rounded_rectangle([hx1, hy1, hx2, hy2], radius=4, fill=(10, 14, 23, 215), outline=(255, 255, 255, 55))

                    draw.text((hx1 + pad_hx, hy1 + pad_hy - 1), label, fill=(56, 189, 248, 255), font=hash_font)

                    draw.text((hx1 + pad_hx + lbl_w, hy1 + pad_hy - 1), hash_str, fill=(248, 250, 252, 255), font=hash_font)

                except Exception as e_h:

                    sys.stderr.write(f"[!] Warning stamping sparse hash on storyboard: {e_h}\n")



            out = Image.alpha_composite(im, overlay).convert("RGB")

            comment_bytes = f"SPARSE_HASH:{sparse_hash}".encode("utf-8") if sparse_hash else None

            save_kwargs = {"quality": 90, "optimize": True}

            if comment_bytes:

                save_kwargs["comment"] = comment_bytes



            if is_plain:

                out.save(jpg_path, "JPEG", **save_kwargs)

            else:

                out_buf = io.BytesIO()

                save_kwargs["quality"] = 92

                out.save(out_buf, "JPEG", **save_kwargs)

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

        cmd = [ffmpeg_exe, "-hide_banner", "-i", input_source]

        run_kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True, "timeout": 8}

        if sys.platform == "win32":

            run_kwargs["creationflags"] = 0x00004000

        p = subprocess.run(cmd, **run_kwargs)

        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", p.stderr)

        if m:

            hours = int(m.group(1))

            minutes = int(m.group(2))

            seconds = float(m.group(3))

            return hours * 3600 + minutes * 60 + seconds

    except Exception:

        pass

    return 0.0





def generate_storyboard_pyav(

    local_path: str,

    temp_jpg: str,

    duration: float,

    cols: int = 10,

    rows: int = 10,

    thumb_width: int = 384,

    thumb_height: int = 216

) -> bool:

    """

    Direct in-memory C-binding extraction (via PyAV / libavcodec).

    Extracts 100 genuine unique frames at ~71ms per seek in memory with zero duplicate tiles.

    """

    try:

        import av

        from PIL import Image



        total_frames = cols * rows

        timestamps = [round((i + 0.5) * (duration / total_frames), 2) for i in range(total_frames)]



        container = av.open(local_path)

        video_stream = container.streams.video[0]

        video_stream.codec_context.thread_type = "AUTO"

        video_stream.codec_context.thread_count = get_optimal_ffmpeg_threads()



        frames_extracted = []

        for ts in timestamps:

            target_pts = int(ts / video_stream.time_base)

            container.seek(target_pts, backward=True, any_frame=False, stream=video_stream)

            for frame in container.decode(video_stream):

                thumb = frame.to_image().resize((thumb_width, thumb_height), Image.Resampling.BILINEAR)

                frames_extracted.append(thumb)

                break



        container.close()



        if not frames_extracted:

            return False



        first_valid = frames_extracted[0]

        while len(frames_extracted) < total_frames:

            frames_extracted.append(first_valid)



        sheet = Image.new("RGB", (thumb_width * cols, thumb_height * rows), color="black")

        for idx in range(total_frames):

            c = idx % cols

            r = idx // cols

            sheet.paste(frames_extracted[idx], (c * thumb_width, r * thumb_height))



        sheet.save(temp_jpg, "JPEG", quality=85)

        return os.path.exists(temp_jpg) and os.path.getsize(temp_jpg) > 1000

    except Exception as e:

        sys.stderr.write(f"[!] PyAV generation fallback error: {e}\n")

        return False





def generate_storyboard_legacy(

    input_source: str,

    temp_jpg: str,

    duration: float,

    cols: int = 10,

    rows: int = 10,

    thumb_width: int = 384,

    thumb_height: int = 216,

    is_local: bool = True,

    safe_id: str = "tmp"

) -> bool:

    """

    Legacy generation method: runs FFmpeg CLI subprocesses to seek 100 keyframes across

    worker threads to fill the 100-frame (10x10) grid with genuine unique frames (zero replication).

    """

    try:

        import concurrent.futures

        from PIL import Image



        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

        total_frames = cols * rows

        timestamps = [round((i + 0.5) * (duration / total_frames), 2) for i in range(total_frames)]

        worker_threads = get_optimal_worker_threads()



        def _fetch_tile(item):

            idx, ts = item

            tmp_tile = os.path.join(STORYBOARD_DIR, f"tmp_t_{safe_id}_{idx}_{os.getpid()}.jpg")

            cmd_tile = [

                ffmpeg_exe, "-y",

                "-skip_frame", "nokey",

                "-ss", str(ts),

                "-i", input_source,

                "-frames:v", "1",

                "-vf", f"scale={thumb_width}:{thumb_height}:flags=fast_bilinear,format=yuvj420p",

                "-q:v", "3",

                tmp_tile

            ]

            try:

                run_kwargs = {"capture_output": True, "timeout": 14}

                if sys.platform == "win32":

                    run_kwargs["creationflags"] = 0x00004000

                subprocess.run(cmd_tile, **run_kwargs)

                if os.path.exists(tmp_tile) and os.path.getsize(tmp_tile) > 500:

                    with Image.open(tmp_tile) as im:

                        im_rgb = im.convert("RGB")

                    try: os.remove(tmp_tile)

                    except Exception: pass

                    return (idx, im_rgb)

            except Exception:

                pass

            if os.path.exists(tmp_tile):

                try: os.remove(tmp_tile)

                except Exception: pass

            return (idx, None)



        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_threads) as ex:

            results = dict(ex.map(_fetch_tile, enumerate(timestamps)))



        valid_frames = [im for im in results.values() if im is not None]

        if len(valid_frames) >= 1:

            first_valid = valid_frames[0]

            sheet = Image.new("RGB", (thumb_width * cols, thumb_height * rows), color="black")

            for tile_idx in range(total_frames):

                frame_img = results.get(tile_idx) or first_valid

                col_i = tile_idx % cols

                row_i = tile_idx // cols

                sheet.paste(frame_img, (col_i * thumb_width, row_i * thumb_height))



            sheet.save(temp_jpg, "JPEG", quality=85)

            return True

        else:

            # Fallback to single-pass linear scan

            ffmpeg_threads = str(get_optimal_ffmpeg_threads())

            fps_val = (total_frames + 0.5) / max(5.0, duration)

            filter_str = (

                f"fps={fps_val:.4f},"

                f"scale={thumb_width}:{thumb_height}:flags=fast_bilinear:force_original_aspect_ratio=decrease,"

                f"pad={thumb_width}:{thumb_height}:(ow-iw)/2:(oh-ih)/2:black,"

                f"format=yuvj420p,"

                f"tile={cols}x{rows}"

            )

            cmd = [

                ffmpeg_exe, "-y",

                "-threads", ffmpeg_threads,

                "-strict", "-1",

                "-i", input_source,

                "-vf", filter_str,

                "-frames:v", "1",

                "-q:v", "2",

                temp_jpg

            ]

            run_kwargs = {"capture_output": True, "text": True, "timeout": 180}

            if sys.platform == "win32":

                run_kwargs["creationflags"] = 0x00004000

            res = subprocess.run(cmd, **run_kwargs)

            return res.returncode == 0 and os.path.exists(temp_jpg) and os.path.getsize(temp_jpg) > 1000

    except Exception as e:

        sys.stderr.write(f"[!] Legacy generation fallback error: {e}\n")

        return False





def generate_storyboard(key: str, duration: float = 0, port: int = 8080, local_path: str = None, overwrite: bool = False, password: str = None, safe_id: str = None, engine: str = None, sparse_hash: str = None) -> bool:

    ck = clean_key(key)

    if not safe_id:

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

        input_source = None

        if local_path and os.path.exists(local_path):

            input_source = local_path

        else:

            encoded_key = quote(ck, safe="/")

            input_source = f"http://127.0.0.1:{port}/stream/{encoded_key}?full=1&no_cache=1"



        cols = 10

        rows = 10

        total_frames = cols * rows  # Always 100 frames

        thumb_width = 384

        thumb_height = 216



        dur = float(duration) if duration and duration > 5 else 0.0

        if dur <= 5:

            dur = probe_duration(input_source)

        if dur <= 5:

            dur = 180.0



        temp_jpg = os.path.join(STORYBOARD_DIR, f"tmp_{safe_id}_{os.getpid()}_{int(time.time()*1000)}.tmp.jpg")



        cfg = get_storyboard_config()

        chosen_engine = (engine or cfg.get("engine", "pyav")).lower()



        cmd_success = False



        # METHOD 1: Fast In-Memory PyAV Direct Engine (100 True Unique Frames)

        if chosen_engine == "pyav" and local_path and os.path.exists(local_path):

            cmd_success = generate_storyboard_pyav(

                local_path=local_path,

                temp_jpg=temp_jpg,

                duration=dur,

                cols=cols,

                rows=rows,

                thumb_width=thumb_width,

                thumb_height=thumb_height

            )



        # METHOD 2: Legacy FFmpeg CLI Engine (100 True Unique Frames via FFmpeg CLI keyframes) or remote fallback

        if not cmd_success:

            cmd_success = generate_storyboard_legacy(

                input_source=input_source,

                temp_jpg=temp_jpg,

                duration=dur,

                cols=cols,

                rows=rows,

                thumb_width=thumb_width,

                thumb_height=thumb_height,

                is_local=bool(local_path and os.path.exists(local_path)),

                safe_id=safe_id

            )



        # Resolve sparse hash

        resolved_sparse_hash = sparse_hash

        if not resolved_sparse_hash and local_path and os.path.exists(local_path):

            try:

                import crypto_engine

                resolved_sparse_hash = crypto_engine.get_sparse_media_hash(local_path)

            except Exception:

                pass

        if not resolved_sparse_hash and safe_id and len(safe_id) == 64:

            resolved_sparse_hash = safe_id



        if cmd_success and os.path.exists(temp_jpg) and os.path.getsize(temp_jpg) > 1000:

            # 1. Overlay crisp bottom-right timestamps and HD sparse hash on Tile 0

            try:

                add_timestamps_to_storyboard(temp_jpg, duration=dur, cols=cols, rows=rows, password=password, sparse_hash=resolved_sparse_hash)

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

                "interval": round(dur / total_frames, 2),

                "engine": "pyav" if (chosen_engine == "pyav" and local_path) else "legacy",

                "timestamp_position": cfg.get("timestamp_position", "bottom-right"),

                "sparse_hash": resolved_sparse_hash or safe_id

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

    """

    Lightweight background pre-warmer that uses fast HTTP Range seeking (HTTP 206)

    to quietly generate storyboards for any library items that don't have them yet.

    Only pulls ~3 MB of keyframes per video with zero full video downloading!

    """

    def _worker():

        try:

            time.sleep(5.0)  # brief startup delay

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

                    dur = info.get("media_info", {}).get("duration_sec", 0)

                    time.sleep(2.0)  # polite pause between videos

                    generate_storyboard(ck, duration=dur, port=port)

        except Exception:

            pass



    threading.Thread(target=_worker, daemon=True).start()





# Automatically ensure all existing storyboards, thumbs, and screens on disk are encrypted with AES-256-GCM

try:

    migrate_encrypt_all_storyboards()

except Exception:

    pass

