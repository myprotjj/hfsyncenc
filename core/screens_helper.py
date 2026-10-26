import os
import re
import hashlib
from pathlib import Path
import io
from PIL import Image
from core import storyboard_helper

SCREENS_DIR = os.path.join("storyboards", "screens")
THUMBS_DIR = os.path.join("storyboards", "thumbs")

os.makedirs(SCREENS_DIR, exist_ok=True)
os.makedirs(THUMBS_DIR, exist_ok=True)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def clean_key(key: str) -> str:
    if not key:
        return ""
    return str(key).strip().replace("\\", "/").strip("/")


def get_safe_id(key: str) -> str:
    try:
        from core import storyboard_helper
        return storyboard_helper.get_safe_id(key)
    except Exception:
        ck = clean_key(key)
        return hashlib.sha256(ck.encode("utf-8")).hexdigest()[:24]


SCREEN_FOLDER_NAMES = {"screens", "screen", "thumbs", "thumb", "covers", "cover", "posters", "poster", "artwork", "storyboards"}


def find_matching_screens_file(video_path: Path, base_dir: Path = None) -> Path:
    """
    Finds an accompanying screens / contact sheet or poster image.
    Supports patterns like:
      - Video.mp4.jpg / Video.mp4.png / Video.mp4.webp (same name with mp4.jpg format)
      - Video.screens.jpg / Video.screens.png / Video.screens.webp
      - Video-screens.jpg / Video_screens.jpg / Video screens.jpg
      - Video.jpg / Video.jpeg / Video.png / Video.webp
      - Video_thumb.jpg / Video-thumb.jpg / Video.thumb.jpg
      - Video_poster.jpg / Video-poster.jpg
      - Video.mp4_screens.jpg / Video.mp4.screens.jpg
      - In same folder, subfolder (e.g. Screens/, Thumbs/), or ancestor Screens/ folders.
      - screens.jpg / poster.jpg / thumb.jpg (if it's the only video in folder)
    """
    if not video_path:
        return None

    parent = video_path.parent
    v_stem = video_path.stem.lower()
    v_name = video_path.name.lower()

    # Build pattern stems: check both full filename (e.g. video.mp4 -> video.mp4.jpg) and stem (video -> video.jpg)
    screens_patterns = [
        v_name,  # e.g. Video.mp4 -> Video.mp4.jpg
        v_stem,  # e.g. Video.mp4 -> Video.jpg
    ]

    suffixes = [
        ".screens", "-screens", "_screens", " screens",
        ".screen", "_screen", "-screen",
        "_thumb", "-thumb", ".thumb",
        "_poster", "-poster", ".poster",
        "_cover", "-cover", ".cover",
        "_preview", "-preview", ".preview"
    ]
    for sfx in suffixes:
        screens_patterns.append(f"{v_stem}{sfx}")
        screens_patterns.append(f"{v_name}{sfx}")

    # Build list of directories to inspect
    search_dirs = []
    if parent.exists():
        search_dirs.append(parent)
        for s in parent.iterdir():
            if s.is_dir() and s.name.lower() in SCREEN_FOLDER_NAMES:
                search_dirs.append(s)

    # Walk up ancestor directories towards base_dir to find parallel/ancestor screen folders (e.g. Root/Screens/Subdir/video.mp4.jpg)
    if base_dir and base_dir.exists():
        cur = parent
        while cur and cur != base_dir and base_dir in cur.parents:
            anc = cur.parent
            for sname in SCREEN_FOLDER_NAMES:
                sdir = anc / sname
                if sdir.is_dir():
                    try:
                        rel_sub = parent.relative_to(anc)
                        sub_sdir = sdir / rel_sub
                        if sub_sdir.is_dir() and sub_sdir not in search_dirs:
                            search_dirs.append(sub_sdir)
                    except Exception:
                        pass
                    if sdir not in search_dirs:
                        search_dirs.append(sdir)
            cur = anc

    # 1. Search candidate directories for pattern matches
    for sdir in search_dirs:
        for p in screens_patterns:
            for ext in IMAGE_EXTS:
                candidate = sdir / f"{p}{ext}"
                if candidate.is_file():
                    return candidate
            for f in sdir.iterdir():
                if f.is_file() and f.suffix.lower() in IMAGE_EXTS and f.stem.lower() == p:
                    return f

    # 2. Check for generic screens.jpg / poster.jpg if single video in folder
    if parent.exists():
        video_count = sum(1 for f in parent.iterdir() if f.is_file() and f.suffix.lower() in {
            ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".flv", ".ts", ".m4v"
        })
        if video_count <= 1:
            for generic in ["screens", "screen", "poster", "thumb", "cover", "preview", "folder"]:
                for ext in IMAGE_EXTS:
                    candidate = parent / f"{generic}{ext}"
                    if candidate.is_file():
                        return candidate
                    for sdir in search_dirs:
                        cand_sub = sdir / f"{generic}{ext}"
                        if cand_sub.is_file():
                            return cand_sub

    return None


def process_screens_image(
    screen_path: Path,
    safe_id: str,
    library_dir: str = "jellyfin_library",
    video_rel_path: str = "",
    password: str = None
) -> dict:
    """
    Processes a discovered screens/contact sheet image:
      1. Saves the full-res contact sheet to storyboards/screens/<safe_id>.jpg.
      2. Crops or creates a clean 16:9 primary video cover into storyboards/thumbs/<safe_id>.jpg.
      3. Copies thumb to jellyfin_library if path provided.
    Returns metadata dict.
    """
    if not screen_path or not os.path.exists(screen_path):
        return None

    out_screens_jpg = os.path.join(SCREENS_DIR, f"{safe_id}.jpg")
    out_thumb_jpg = os.path.join(THUMBS_DIR, f"{safe_id}.jpg")

    try:
        with Image.open(str(screen_path)) as img:
            # Convert to RGB (handles RGBA, Palette, CMYK, etc.)
            if img.mode != "RGB":
                img = img.convert("RGB")

            orig_w, orig_h = img.size

            # 1. Save full contact sheet image (encrypted with AES-256-GCM)
            screen_buf = io.BytesIO()
            if orig_w > 2560:
                scale_factor = 2560.0 / orig_w
                new_h = int(orig_h * scale_factor)
                save_img = img.resize((2560, new_h), Image.Resampling.LANCZOS)
                save_img.save(screen_buf, "JPEG", quality=92, optimize=True)
                screen_bytes = screen_buf.getvalue()
            elif Path(screen_path).suffix.lower() in [".jpg", ".jpeg"] and img.mode == "RGB":
                with open(str(screen_path), "rb") as f_in:
                    screen_bytes = f_in.read()
            else:
                img.save(screen_buf, "JPEG", quality=92, optimize=True)
                screen_bytes = screen_buf.getvalue()
            storyboard_helper.write_encrypted_bytes(out_screens_jpg, screen_bytes, password)

            # 2. Crop or fit a crisp 16:9 thumbnail for the video card & Jellyfin
            sname = Path(screen_path).name.lower()
            is_explicit_cover = any(x in sname for x in ["thumb", "poster", "cover", "preview", "front", "folder"])
            aspect = orig_w / float(orig_h) if orig_h else 1.0

            # Determine if this image is a multi-tile contact sheet grid or a single cover image
            # Note: Single widescreen screenshots (aspect >= 1.4) should NEVER be sliced into a 4x4 grid!
            is_contact_sheet = False
            if not is_explicit_cover:
                if abs(aspect - (16.0 / 9.0)) < 0.04 and orig_w >= 1600:
                    # 10x10 storyboard grid (100 frames)
                    is_contact_sheet = True
                elif (orig_h >= orig_w * 1.15) or (aspect < 1.4 and any(x in sname for x in ["screens", "sheet"]) and orig_h > 1200):
                    # Multi-row contact sheet with top metadata header
                    is_contact_sheet = True

            if is_contact_sheet:
                if abs(aspect - (16.0 / 9.0)) < 0.04 and orig_w >= 1600:
                    # 10x10 grid with no header: pick frame 3
                    cols, rows = 10, 10
                    tile_w = orig_w // cols
                    tile_h = orig_h // rows
                    col_idx = 2
                    row_idx = 0
                    crop_x1 = col_idx * tile_w
                    crop_y1 = row_idx * tile_h
                    crop_x2 = crop_x1 + tile_w
                    crop_y2 = crop_y1 + tile_h
                else:
                    # Typical external contact sheet with top metadata header
                    cols = 4 if orig_w >= 1200 else 3
                    rows = 4 if orig_h >= 1400 else 3
                    header_h = int(orig_h * 0.08)
                    tile_w = orig_w // cols
                    tile_h = (orig_h - header_h) // rows
                    # Pick tile (col 1, row 0) or center tile to avoid black opening frames
                    crop_x1 = min(tile_w, max(0, orig_w - tile_w))
                    crop_y1 = header_h
                    crop_x2 = crop_x1 + tile_w
                    crop_y2 = header_h + tile_h

                if crop_x2 > crop_x1 and crop_y2 > crop_y1:
                    tile_crop = img.crop((crop_x1, crop_y1, crop_x2, crop_y2))
                    thumb_img = tile_crop.resize((480, 270), Image.Resampling.LANCZOS)
                else:
                    thumb_img = img.resize((480, 270), Image.Resampling.LANCZOS)
            else:
                # Single cover, poster, or landscape screenshot:
                # Center crop & scale preserving full image composition!
                target_ratio = 16.0 / 9.0
                if aspect > target_ratio:
                    new_w = int(orig_h * target_ratio)
                    offset_x = (orig_w - new_w) // 2
                    crop_box = (offset_x, 0, offset_x + new_w, orig_h)
                    cover_crop = img.crop(crop_box)
                elif aspect < target_ratio:
                    new_h = int(orig_w / target_ratio)
                    offset_y = (orig_h - new_h) // 3  # center-high to capture faces/titles
                    if offset_y < 0:
                        offset_y = 0
                    if offset_y + new_h > orig_h:
                        new_h = orig_h - offset_y
                    crop_box = (0, offset_y, orig_w, offset_y + new_h)
                    cover_crop = img.crop(crop_box)
                else:
                    cover_crop = img

                thumb_img = cover_crop.resize((480, 270), Image.Resampling.LANCZOS)

            thumb_buf = io.BytesIO()
            thumb_img.save(thumb_buf, "JPEG", quality=92, optimize=True)
            thumb_bytes = thumb_buf.getvalue()
            storyboard_helper.write_encrypted_bytes(out_thumb_jpg, thumb_bytes, password)

            # 3. Copy thumb to Jellyfin library if available (plain JPEG for Jellyfin)
            if library_dir and video_rel_path:
                try:
                    jf_parent = Path(library_dir) / Path(video_rel_path).parent
                    jf_stem = Path(video_rel_path).stem
                    jf_parent.mkdir(parents=True, exist_ok=True)
                    jf_thumb = jf_parent / f"{jf_stem}-thumb.jpg"
                    with open(jf_thumb, "wb") as f_jf:
                        f_jf.write(thumb_bytes)
                except Exception:
                    pass

            return {
                "available": True,
                "safe_id": safe_id,
                "orig_width": orig_w,
                "orig_height": orig_h,
                "screens_file": out_screens_jpg,
                "thumb_file": out_thumb_jpg,
                "screens_url": f"/api/screens/image?id={safe_id}",
                "thumb_url": f"/api/screens/thumb?id={safe_id}"
            }

    except Exception as e:
        print(f"[!] Error processing screens image {screen_path}: {e}")
        return None


def get_screens_path(safe_id: str) -> str:
    p = os.path.join(SCREENS_DIR, f"{safe_id}.jpg")
    return p if os.path.exists(p) else None


def get_screens_thumb_path(safe_id: str) -> str:
    p = os.path.join(THUMBS_DIR, f"{safe_id}.jpg")
    return p if os.path.exists(p) else None


def has_screens(safe_id: str) -> bool:
    if get_screens_path(safe_id) or get_screens_thumb_path(safe_id):
        return True
    try:
        from core import storyboard_helper
        mf = storyboard_helper.get_manifest_data()
        for k, v in mf.get("files", {}).items():
            if v.get("safe_id") == safe_id or storyboard_helper.get_safe_id(k) == safe_id:
                return bool(v.get("screens"))
    except Exception:
        pass
    return False
