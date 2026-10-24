import os
import re
import sys
import subprocess
from pathlib import Path
import imageio_ffmpeg

LANGUAGE_MAP = {
    "en": "English", "eng": "English",
    "es": "Spanish", "spa": "Spanish",
    "fr": "French", "fra": "French",
    "de": "German", "ger": "German", "deu": "German",
    "it": "Italian", "ita": "Italian",
    "pt": "Portuguese", "por": "Portuguese",
    "ru": "Russian", "rus": "Russian",
    "ja": "Japanese", "jpn": "Japanese",
    "ko": "Korean", "kor": "Korean",
    "zh": "Chinese", "chi": "Chinese", "zho": "Chinese",
    "hi": "Hindi", "hin": "Hindi",
    "ar": "Arabic", "ara": "Arabic",
    "nl": "Dutch", "dut": "Dutch",
    "sv": "Swedish", "swe": "Swedish",
    "pl": "Polish", "pol": "Polish",
    "tr": "Turkish", "tur": "Turkish",
}


def detect_subtitle_lang(filename: str):
    """Detects (lang_code, lang_name) from filename like 'Movie.en.srt' or 'Movie.spa.srt'."""
    stem = Path(filename).stem
    if "." in stem:
        tag = stem.rsplit(".", 1)[1].lower()
        if tag in LANGUAGE_MAP:
            return tag, LANGUAGE_MAP[tag]
    return "en", "English"


def srt_to_webvtt(srt_content: str) -> str:
    """Converts standard SubRip (.srt) text to WebVTT format for HTML5 <track>."""
    if not srt_content:
        return "WEBVTT\n\n"
    content = srt_content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    # Replace comma in timestamps: 00:01:23,456 -> 00:01:23.456
    vtt_text = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", content)
    vtt_clean = vtt_text.strip()
    if not vtt_clean.startswith("WEBVTT"):
        vtt_clean = "WEBVTT\n\n" + vtt_clean
    return vtt_clean + "\n"


def decode_subtitle_bytes(raw_bytes: bytes) -> str:
    """Safely decodes raw subtitle bytes trying utf-8-sig, utf-8, latin-1, cp1252."""
    for enc in ["utf-8-sig", "utf-8", "cp1252", "latin-1"]:
        try:
            return raw_bytes.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode("utf-8", errors="replace")


def extract_companion_audio(video_path: str, output_audio_path: str) -> bool:
    """
    Extracts companion audio from video file.
    Tries lossless stream copy (-c:a copy) first (<0.5s).
    Falls back to high-quality universal AAC (192kbps) if stream-copy fails or format is non-standard.
    """
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    try:
        # Fast stream-copy first
        cmd_copy = [ffmpeg, "-y", "-i", video_path, "-vn", "-c:a", "copy", output_audio_path]
        res = subprocess.run(cmd_copy, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if res.returncode == 0 and os.path.exists(output_audio_path) and os.path.getsize(output_audio_path) > 500:
            return True
    except Exception:
        pass

    try:
        # Fallback: re-encode audio to universal high-compatibility AAC
        cmd_aac = [ffmpeg, "-y", "-i", video_path, "-vn", "-c:a", "aac", "-b:a", "192k", output_audio_path]
        res2 = subprocess.run(cmd_aac, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        return res2.returncode == 0 and os.path.exists(output_audio_path) and os.path.getsize(output_audio_path) > 500
    except Exception:
        return False


def sync_jellyfin_subtitle(library_dir: str, video_key: str, srt_content: str, lang: str = "en") -> str:
    """
    Saves decrypted .srt file alongside .strm file in Jellyfin library directory.
    Example: jellyfin_library/LOLAH-VIBE/Movie.en.srt
    """
    try:
        parts = Path(video_key).parts
        if not parts:
            return ""
        
        # Build path matching Jellyfin library structure
        lib_path = Path(library_dir)
        folder = lib_path.joinpath(*parts[:-1]) if len(parts) > 1 else lib_path
        folder.mkdir(parents=True, exist_ok=True)
        
        video_stem = Path(parts[-1]).stem
        # Generate both Movie.en.srt and Movie.srt (if en/default)
        target_name = f"{video_stem}.{lang}.srt" if lang and lang != "en" else f"{video_stem}.srt"
        dest_file = folder / target_name
        dest_file.write_text(srt_content, encoding="utf-8")
        
        # Also create language-tagged version if default
        if lang == "en":
            dest_lang = folder / f"{video_stem}.en.srt"
            dest_lang.write_text(srt_content, encoding="utf-8")
            
        return str(dest_file)
    except Exception as e:
        sys.stderr.write(f"[!] Error syncing subtitle to Jellyfin: {e}\n")
        return ""
