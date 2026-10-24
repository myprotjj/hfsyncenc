import os
import re
import hashlib
from pathlib import Path
from PIL import Image

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
    video_rel_path: str = ""
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

            # 1. Save full contact sheet image
            # If extremely large (e.g. > 3000px wide), downscale slightly for smooth web viewing
            save_img = img
            if orig_w > 2560:
                scale_factor = 2560.0 / orig_w
                new_h = int(orig_h * scale_factor)
                save_img = img.resize((2560, new_h), Image.Resampling.LANCZOS)

            save_img.save(out_screens_jpg, "JPEG", quality=88, optimize=True)

            # 2. Crop a crisp 16:9 thumbnail for the video card & Jellyfin
            # Check if this is a multi-tile contact sheet or a single poster
            is_contact_sheet = (orig_h >= orig_w) or (orig_h > 1000 and orig_w > 1000)

            if is_contact_sheet:
                # Check if it's a 10x10 storyboard (16:9 aspect ratio, e.g. 3840x2160)
                aspect = orig_w / float(orig_h) if orig_h else 1.0
                if abs(aspect - (16.0 / 9.0)) < 0.04 and orig_w >= 1600:
                    # 10x10 grid with no header
                    cols = 10
                    rows = 10
                    header_h = 0
                    tile_w = orig_w // cols
                    tile_h = orig_h // rows
                    # Pick frame 2 or 3 (col 2, row 0)
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
                    # Pick Tile (col 0, row 0) or (col 1, row 0)
                    crop_x1 = 0
                    crop_y1 = header_h
                    crop_x2 = tile_w
                    crop_y2 = header_h + tile_h

                # Ensure positive bounds
                if crop_x2 > crop_x1 and crop_y2 > crop_y1:
                    tile_crop = img.crop((crop_x1, crop_y1, crop_x2, crop_y2))
                    # Resize to standard 480x270 (16:9) thumbnail
                    tile_crop = tile_crop.resize((480, 270), Image.Resampling.LANCZOS)
                    tile_crop.save(out_thumb_jpg, "JPEG", quality=92, optimize=True)
                else:
                    thumb_img = img.resize((480, 270), Image.Resampling.LANCZOS)
                    thumb_img.save(out_thumb_jpg, "JPEG", quality=92, optimize=True)
            else:
                # Single landscape frame or banner
                thumb_img = img.resize((480, 270), Image.Resampling.LANCZOS)
                thumb_img.save(out_thumb_jpg, "JPEG", quality=92, optimize=True)

            # 3. Copy thumb to Jellyfin library if available
            if library_dir and video_rel_path:
                try:
                    jf_parent = Path(library_dir) / Path(video_rel_path).parent
                    jf_stem = Path(video_rel_path).stem
                    jf_parent.mkdir(parents=True, exist_ok=True)
                    jf_thumb = jf_parent / f"{jf_stem}-thumb.jpg"
                    if os.path.exists(out_thumb_jpg):
                        import shutil
                        shutil.copyfile(out_thumb_jpg, str(jf_thumb))
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
    return bool(get_screens_path(safe_id))
