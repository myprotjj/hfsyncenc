import os
import sys
import re
import json
import time
import hashlib
import threading
import subprocess
from urllib.parse import quote, unquote
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import imageio_ffmpeg

STORYBOARD_DIR = "storyboards"
THUMBS_DIR = os.path.join(STORYBOARD_DIR, "thumbs")
GENERATING_LOCK = threading.Lock()
ACTIVE_GENERATIONS = set()

# Priority pool for the currently watched video (takes immediate priority)
PRIORITY_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="PriorityStoryboard")

os.makedirs(STORYBOARD_DIR, exist_ok=True)
os.makedirs(THUMBS_DIR, exist_ok=True)


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


def update_storyboard_duration(key: str, real_duration: float) -> bool:
    """Updates existing storyboard metadata JSON with the true probed duration."""
    if not real_duration or real_duration <= 0:
        return False
    ck = clean_key(key)
    safe_id = get_safe_id(ck)
    json_path = os.path.join(STORYBOARD_DIR, f"{safe_id}.json")
    if os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["duration"] = round(real_duration, 2)
            if data.get("total_frames"):
                data["interval"] = round(real_duration / data["total_frames"], 3)
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            return True
        except Exception:
            pass
    return False


def get_storyboard_meta(key: str) -> dict:
    ck = clean_key(key)
    safe_id = get_safe_id(ck)
    jpg_path = os.path.join(STORYBOARD_DIR, f"{safe_id}.jpg")
    json_path = os.path.join(STORYBOARD_DIR, f"{safe_id}.json")

    if os.path.exists(jpg_path) and os.path.exists(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
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


def get_poster_thumb_path(safe_id: str) -> str:
    clean_id = clean_key(safe_id)
    thumb_path = os.path.join(THUMBS_DIR, f"{clean_id}.jpg")
    if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 1000:
        return thumb_path
    return slice_poster_thumbnail(clean_id)


def slice_poster_thumbnail(safe_id: str, overwrite: bool = False) -> str:
    """
    Slices a razor-sharp, pixel-perfect 16:9 poster thumbnail (480x270)
    from the 100-frame storyboard. Analyzes early candidate frames (2..12)
    to select a high-contrast, vivid scene rather than an intro fade or black screen.
    """
    clean_id = clean_key(safe_id)
    out_thumb = os.path.join(THUMBS_DIR, f"{clean_id}.jpg")
    if not overwrite and os.path.exists(out_thumb) and os.path.getsize(out_thumb) > 1000:
        return out_thumb

    sb_jpg = os.path.join(STORYBOARD_DIR, f"{clean_id}.jpg")
    if not os.path.exists(sb_jpg):
        return None

    try:
        from PIL import Image, ImageStat
        with Image.open(sb_jpg) as img:
            w, h = img.size
            cols, rows = 10, 10
            tw = w // cols
            th = h // rows

            # Candidate frame indices: avoid frame 0-1 (often dark/intro), check 2..15
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
                    # Prefer frames with good variance and mid-range brightness
                    score = var_b * (1.0 - abs(mean_b - 128.0) / 128.0)

                if score > best_score:
                    best_score = score
                    best_frame = f_idx

            best_c = best_frame % cols
            best_r = best_frame // cols
            crop = img.crop((best_c * tw, best_r * th, (best_c + 1) * tw, (best_r + 1) * th))
            # Resize with highest quality Lanczos filter to 480x270
            thumb = crop.resize((480, 270), Image.Resampling.LANCZOS)
            thumb.save(out_thumb, "JPEG", quality=92, optimize=True)
            return out_thumb
    except Exception as e:
        sys.stderr.write(f"[!] Error slicing poster thumbnail for {clean_id}: {e}\n")
        return None


def add_timestamps_to_storyboard(jpg_path: str, duration: float, cols: int = 10, rows: int = 10) -> bool:
    """
    Overlays professional translucent timestamp badges (e.g. 00:05:24) in the bottom-right
    corner of each tile in the 100-frame storyboard, matching movie contact sheet screenshots.
    """
    if not os.path.exists(jpg_path):
        return False
    try:
        from PIL import Image, ImageDraw, ImageFont

        dur = float(duration) if duration and duration > 0 else 180.0
        total_frames = cols * rows

        with Image.open(jpg_path) as base_im:
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
            out.save(jpg_path, "JPEG", quality=92, optimize=True)
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


def generate_storyboard(key: str, duration: float = 0, port: int = 8080, local_path: str = None, overwrite: bool = False) -> bool:
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
            # Fallback to streaming proxy endpoint with clean quoting
            encoded_key = quote(ck, safe="/")
            input_source = f"http://127.0.0.1:{port}/stream/{encoded_key}?full=1"

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
            os.replace(temp_jpg, jpg_path)
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
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
            try:
                slice_poster_thumbnail(safe_id, overwrite=True)
            except Exception:
                pass
            try:
                add_timestamps_to_storyboard(jpg_path, duration=dur, cols=cols, rows=rows)
            except Exception:
                pass
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
