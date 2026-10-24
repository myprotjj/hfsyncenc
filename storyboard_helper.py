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
GENERATING_LOCK = threading.Lock()
ACTIVE_GENERATIONS = set()

# Priority pool for the currently watched video (takes immediate priority)
PRIORITY_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="PriorityStoryboard")

os.makedirs(STORYBOARD_DIR, exist_ok=True)


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
            data["sprite_url"] = f"/api/storyboard/image?id={safe_id}"
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
        rows = 5
        total_frames = cols * rows  # 50 frames

        dur = float(duration) if duration and duration > 5 else 0.0
        if dur <= 5:
            dur = probe_duration(input_source)
        if dur <= 5:
            dur = 180.0
            
        fps_val = (total_frames + 0.5) / max(5.0, dur)

        temp_jpg = f"{jpg_path}.tmp.jpg"
        filter_str = (
            f"fps={fps_val:.4f},"
            f"scale=160:90:force_original_aspect_ratio=decrease,"
            f"pad=160:90:(ow-iw)/2:(oh-ih)/2:black,"
            f"tile={cols}x{rows}"
        )

        cmd = [
            ffmpeg_exe,
            "-y",
            "-skip_frame", "nokey",
            "-i", input_source,
            "-vf", filter_str,
            "-frames:v", "1",
            "-q:v", "4",
            temp_jpg
        ]

        res = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
        if res.returncode == 0 and os.path.exists(temp_jpg) and os.path.getsize(temp_jpg) > 1000:
            os.replace(temp_jpg, jpg_path)
            meta = {
                "key": ck,
                "safe_id": safe_id,
                "cols": cols,
                "rows": rows,
                "thumb_width": 160,
                "thumb_height": 90,
                "total_frames": total_frames,
                "duration": dur,
                "interval": round(dur / total_frames, 2)
            }
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
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
