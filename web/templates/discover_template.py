"""
HFSYNC Discover Feed: 100% Explorer Card & Video Player Engine
Served at /discover
Features:
- Exact 100% replication of Explorer Card:
  - HD sprite sheet moving thumbnail preview on hover (auto-cycle + horizontal scrub)
  - Interactive scrub bar with live frame seeking
  - Screens button hover contact sheet overlay
  - Cache pills, cache track buffer, resume badge & bar, size badge with audio size
  - Title row with offline toggle, mark for merge, and action arrow
  - Subtitles, plot overview, all tags and categories from renderCardBadgesHtml
  - In-thumbnail screenshot (SS) gallery overlay on arrow click (0% card push/shift)
- Exact 100% replication of Explorer YouTube Player (playerBox):
  - Custom scrub bar with floating storyboard thumbnail preview
  - Play/pause, 10s seek, speed menu, volume slider, CC menu, theater mode, PiP, fullscreen
  - Auto-docking draggable floating mini player on scroll (S/M/L sizes)
  - /stream/play/{safeId} streaming (avoids IDM capture)
- Modals: screensModal (high-res lightbox) & metadataModal (tag editing)
- Continuous 4-column infinite scroll feed (24 initial -> +16 on scroll)
- Interactive tag filtering (clicking ANY tag/category adds to filters) & complex sorting (with Random Shuffle)
- Slide-out Watch Later & Watch History drawer
"""

import json
from pathlib import Path
from typing import Any, Dict, List


def render_discover_page(handler, staged_repo: str = None) -> str:
    staged_banner_html = ""
    if staged_repo:
        try:
            import staging_manager
            import metadata_helper
            import storyboard_helper
            import screens_helper
            from pathlib import Path
            stg = staging_manager.load_staging_manifest()
            rdata = stg.get("repositories", {}).get(staged_repo, {})
            staged_items = []
            for f in rdata.get("files", []):
                rec = f.get("record", {})
                fk = f.get("full_key") or f.get("orig_filename") or ""
                base = Path(fk).name
                sz = f.get("orig_size", 0)
                media_info = rec.get("media_info") or {}
                dur_sec = media_info.get("duration_sec", 0)
                dur_str = media_info.get("duration_str") or (f"{int(dur_sec)//60}:{int(dur_sec)%60:02d}" if dur_sec else "")
                safe_id = rec.get("safe_id") or rec.get("storyboard", {}).get("safe_id") or storyboard_helper.get_safe_id(fk)
                has_sb = bool(rec.get("storyboard")) or storyboard_helper.has_storyboard(fk)
                has_screens = has_sb or bool(rec.get("screens")) or screens_helper.has_screens(safe_id)
                sb_v = storyboard_helper.get_storyboard_version(safe_id)
                audio_entry = rec.get("audio") or {}
                audio_sz = audio_entry.get("orig_size") or audio_entry.get("enc_size") or 0
                audio_size_str = f"{audio_sz / (1024 * 1024):.1f} MB" if audio_sz > 0 else "Audio"
                audio_codec = media_info.get("audio_codec") or "AAC"
                meta = rec.get("metadata") or metadata_helper.extract_default_metadata(fk)
                if not meta.get("title"):
                    meta["title"] = base
                staged_items.append({
                    "key": fk,
                    "safe_id": safe_id,
                    "orig_name": base,
                    "file_names": [base],
                    "aliases": [],
                    "size_bytes": sz,
                    "size_str": f"{sz / (1024 * 1024):.2f} MB",
                    "size_formatted": staging_manager._format_size(sz),
                    "repo_id": rec.get("repo_id") or rdata.get("repo_id", ""),
                    "account": rec.get("account") or rdata.get("account", ""),
                    "blob": f.get("blob_path", ""),
                    "blob_path": f.get("blob_path", ""),
                    "metadata": meta,
                    "media_info": media_info,
                    "duration_sec": dur_sec,
                    "duration_str": dur_str,
                    "has_storyboard": has_sb,
                    "sb_version": sb_v,
                    "has_screens": has_screens,
                    "screens_url": f"/api/screens/image?id={safe_id}&v={sb_v}" if sb_v else f"/api/screens/image?id={safe_id}",
                    "screens_thumb_url": f"/api/screens/thumb?id={safe_id}&v={sb_v}" if sb_v else f"/api/screens/thumb?id={safe_id}",
                    "audio": audio_entry,
                    "audio_size_str": audio_size_str,
                    "audio_codec": audio_codec,
                    "verifier": rec.get("verifier", ""),
                    "can_decrypt": True,
                    "cache_pct": 100,
                    "cached_size_str": staging_manager._format_size(sz),
                    "is_staged": True,
                    "staged_repo": staged_repo
                })
            if staged_items:
                manifest_data = staged_items
                all_accounts = [rdata.get("account", "")] if rdata.get("account") else []
                primary_account = rdata.get("account", "")

                staged_banner_html = f'''
                <div style="background: rgba(16, 185, 129, 0.12); border: 1px solid rgba(16, 185, 129, 0.4); border-radius: 12px; padding: 14px 20px; margin: 16px 24px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px;">
                    <div style="display: flex; align-items: center; gap: 12px;">
                        <span style="font-size: 1.8rem;">📦</span>
                        <div>
                            <div style="font-size: 1.05rem; font-weight: 700; color: #34d399;">
                                Staged Vault Content: <span style="font-family: monospace; color: #f1f5f9;">{staged_repo}</span>
                            </div>
                            <div style="font-size: 0.8rem; color: #94a3b8; margin-top: 2px;">
                                Showing {len(manifest_data)} pre-encrypted file(s) &bull; 100% verified on disk &bull; Not yet committed to cloud
                            </div>
                        </div>
                    </div>
                    <div style="display: flex; gap: 8px; align-items: center;">
                        <a href="/discover" style="background: rgba(255,255,255,0.08); color: #e2e8f0; text-decoration: none; padding: 6px 14px; border-radius: 6px; font-size: 0.82rem; font-weight: 600;">
                            &larr; View All Uploaded Videos
                        </a>
                        <button onclick="pushStagedRepo('{staged_repo}')" style="background: #059669; border: 1px solid #10b981; color: #fff; padding: 6px 14px; border-radius: 6px; font-size: 0.82rem; font-weight: 600; cursor: pointer;">
                            🚀 Push This Vault (1 Commit)
                        </button>
                    </div>
                </div>
                '''
            else:
                all_vault_files, _, all_accs, prim_acc = handler.get_files_data()
                target_repo_id = rdata.get("repo_id", "")
                pushed_items = [
                    f for f in all_vault_files
                    if f.get("repo_folder") == staged_repo
                    or f.get("key", "").startswith(staged_repo + "/")
                    or (target_repo_id and f.get("repo") == target_repo_id)
                ]
                if pushed_items:
                    manifest_data = pushed_items
                    all_accounts = [rdata.get("account", "")] if rdata.get("account") else all_accs
                    primary_account = rdata.get("account", "") or prim_acc
                    hf_repo_url = f"https://huggingface.co/datasets/{target_repo_id}" if target_repo_id else ""
                    hf_btn = f'<a href="{hf_repo_url}" target="_blank" rel="noopener noreferrer" style="background: #059669; border: 1px solid #10b981; color: #fff; text-decoration: none; padding: 6px 14px; border-radius: 6px; font-size: 0.82rem; font-weight: 600;">📦 Open HF Repo ↗</a>' if target_repo_id else ""
                    staged_banner_html = f'''
                    <div style="background: rgba(16, 185, 129, 0.15); border: 1px solid rgba(16, 185, 129, 0.4); border-radius: 12px; padding: 14px 20px; margin: 16px 24px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 12px;">
                        <div style="display: flex; align-items: center; gap: 12px;">
                            <span style="font-size: 1.8rem; color: #34d399;">✓</span>
                            <div>
                                <div style="font-size: 1.05rem; font-weight: 700; color: #34d399;">
                                    Committed Vault Content: <span style="font-family: monospace; color: #f1f5f9;">{staged_repo}</span>
                                </div>
                                <div style="font-size: 0.8rem; color: #94a3b8; margin-top: 2px;">
                                    Showing all {len(manifest_data)} video(s) committed to Hugging Face ({target_repo_id or "Cloud"}) &bull; 100% Streamable
                                </div>
                            </div>
                        </div>
                        <div style="display: flex; gap: 8px; align-items: center;">
                            <a href="/discover" style="background: rgba(255,255,255,0.08); color: #e2e8f0; text-decoration: none; padding: 6px 14px; border-radius: 6px; font-size: 0.82rem; font-weight: 600;">
                                &larr; View All Vault Videos
                            </a>
                            <a href="/?folder={staged_repo}" style="background: rgba(255,255,255,0.08); color: #e2e8f0; text-decoration: none; padding: 6px 14px; border-radius: 6px; font-size: 0.82rem; font-weight: 600;">
                                📁 Open in Explorer
                            </a>
                            {hf_btn}
                        </div>
                    </div>
                    '''
                else:
                    manifest_data, skipped_files, all_accounts, primary_account = handler.get_files_data()
        except Exception as ex_stg:
            print(f"[!] Error loading staged repo for discover: {ex_stg}")
            manifest_data, skipped_files, all_accounts, primary_account = handler.get_files_data()
    else:
        manifest_data, skipped_files, all_accounts, primary_account = handler.get_files_data()

    files_json = json.dumps(manifest_data)
    accounts_json = json.dumps(all_accounts)
    primary_acc_str = json.dumps(primary_account)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>HFSYNC &bull; Discover Feed</title>
    <link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>✨</text></svg>">
    <style>
        :root {{
            --bg-base: #0f0f0f;
            --bg-surface: #181818;
            --bg-card: #181818;
            --bg-card-hover: #222222;
            --border-subtle: #272727;
            --border-hover: #3f3f46;
            --text-primary: #f4f4f5;
            --text-secondary: #a1a1aa;
            --text-muted: #71717a;
            --primary: #38bdf8;
            --accent: #10b981;
            --panel-border: rgba(255, 255, 255, 0.08);
            --pill-bg: #272727;
            --pill-text: #e4e4e7;
        }}

        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}

        body {{
            background-color: var(--bg-base);
            color: var(--text-primary);
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            min-height: 100vh;
            overflow-x: hidden;
            display: flex;
            flex-direction: column;
        }}

        /* Scrollbar */
        ::-webkit-scrollbar {{ width: 8px; height: 8px; }}
        ::-webkit-scrollbar-track {{ background: #0f0f0f; }}
        ::-webkit-scrollbar-thumb {{ background: #272727; border-radius: 4px; }}
        ::-webkit-scrollbar-thumb:hover {{ background: #3f3f46; }}

        /* Top Sticky Header & Filter Bar */
        .discover-header {{
            position: sticky;
            top: 0;
            z-index: 1000;
            background: rgba(15, 15, 15, 0.94);
            backdrop-filter: blur(16px);
            -webkit-backdrop-filter: blur(16px);
            border-bottom: 1px solid var(--border-subtle);
            padding: 12px 28px;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }}

        .header-top-row {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
        }}

        .brand-cluster {{
            display: flex;
            align-items: center;
            gap: 10px;
            flex-shrink: 0;
        }}

        .brand-title {{
            font-size: 1.15rem;
            font-weight: 700;
            color: #ffffff;
            display: flex;
            align-items: center;
            gap: 8px;
            text-decoration: none;
            letter-spacing: -0.01em;
        }}

        .brand-count-badge {{
            background: #272727;
            color: #a1a1aa;
            font-size: 0.76rem;
            padding: 2px 8px;
            border-radius: 9999px;
            font-weight: 600;
        }}

        .search-container {{
            flex: 1;
            max-width: 640px;
            position: relative;
            display: flex;
            align-items: center;
        }}

        .search-input {{
            width: 100%;
            background: #181818;
            border: 1px solid #2e2e2e;
            border-radius: 9999px;
            padding: 9px 40px 9px 38px;
            color: #ffffff;
            font-size: 0.9rem;
            outline: none;
            transition: all 0.2s ease;
        }}
        .search-input:focus {{
            border-color: #38bdf8;
            background: #202020;
            box-shadow: 0 0 0 2px rgba(56, 189, 248, 0.2);
        }}

        .search-icon-left {{
            position: absolute;
            left: 14px;
            color: #71717a;
            font-size: 0.85rem;
            pointer-events: none;
        }}

        .search-clear-btn {{
            position: absolute;
            right: 14px;
            background: transparent;
            border: none;
            color: #71717a;
            cursor: pointer;
            font-size: 1rem;
            display: none;
            padding: 2px;
        }}
        .search-clear-btn:hover {{ color: #ffffff; }}

        .search-shortcut-hint {{
            position: absolute;
            right: 14px;
            background: #272727;
            color: #71717a;
            font-size: 0.72rem;
            padding: 1px 6px;
            border-radius: 4px;
            pointer-events: none;
            font-weight: 600;
        }}

        .header-actions-cluster {{
            display: flex;
            align-items: center;
            gap: 10px;
            flex-shrink: 0;
        }}

        .flat-btn {{
            background: #272727;
            color: #f4f4f5;
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 8px;
            padding: 8px 14px;
            font-size: 0.82rem;
            font-weight: 600;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            transition: all 0.15s ease;
            text-decoration: none;
        }}
        .flat-btn:hover {{
            background: #333338;
            border-color: rgba(255, 255, 255, 0.18);
            color: #ffffff;
        }}

        .sort-select {{
            background: #272727;
            color: #f4f4f5;
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 8px;
            padding: 8px 12px;
            font-size: 0.82rem;
            font-weight: 600;
            outline: none;
            cursor: pointer;
        }}

        /* Filter Chips Row */
        .filter-chips-row {{
            display: flex;
            align-items: center;
            gap: 8px;
            overflow-x: auto;
            white-space: nowrap;
            padding-bottom: 2px;
            scrollbar-width: none;
        }}
        .filter-chips-row::-webkit-scrollbar {{ display: none; }}

        .filter-chip {{
            background: var(--pill-bg);
            color: var(--pill-text);
            border: 1px solid rgba(255, 255, 255, 0.06);
            border-radius: 9999px;
            padding: 5px 14px;
            font-size: 0.8rem;
            font-weight: 600;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            transition: all 0.15s ease;
            user-select: none;
            flex-shrink: 0;
        }}
        .filter-chip:hover {{ background: #333338; color: #ffffff; }}
        .filter-chip.is-active {{ background: #ffffff; color: #0f0f0f; border-color: #ffffff; }}

        .active-filter-badge {{
            background: #38bdf8;
            color: #0b1120;
            font-size: 0.78rem;
            font-weight: 700;
            padding: 4px 10px;
            border-radius: 9999px;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            cursor: pointer;
            flex-shrink: 0;
        }}
        .active-filter-badge:hover {{ background: #7dd3fc; }}
        .active-filter-badge .badge-remove {{ font-weight: 800; margin-left: 2px; }}

        .btn-clear-filters {{
            background: transparent;
            color: #ef4444;
            border: 1px dashed rgba(239, 68, 68, 0.4);
            border-radius: 9999px;
            padding: 4px 12px;
            font-size: 0.78rem;
            font-weight: 600;
            cursor: pointer;
            display: none;
            align-items: center;
            gap: 4px;
            flex-shrink: 0;
        }}
        .btn-clear-filters:hover {{ background: rgba(239, 68, 68, 0.1); }}

        /* Main Container */
        .discover-container {{
            max-width: 1720px;
            width: 100%;
            margin: 0 auto;
            padding: 24px 28px 60px 28px;
            flex: 1;
        }}

        /* 4-Column Continuous Video Feed */
        .video-grid {{
            display: grid;
            grid-template-columns: repeat(4, minmax(0, 1fr));
            gap: 22px;
            width: 100%;
        }}
        @media (max-width: 1440px) {{ .video-grid {{ grid-template-columns: repeat(3, minmax(0, 1fr)); }} }}
        @media (max-width: 1024px) {{ .video-grid {{ grid-template-columns: repeat(2, minmax(0, 1fr)); }} }}
        @media (max-width: 640px) {{
            .video-grid {{ grid-template-columns: 1fr; }}
            .discover-header {{ padding: 10px 16px; }}
            .discover-container {{ padding: 16px 16px 40px 16px; }}
        }}

        /* =========================================================================
           100% EXACT EXPLORER CARD CSS
           ========================================================================= */
        .video-card {{
            background: #181818;
            border: 1px solid var(--border-subtle);
            border-radius: 12px;
            overflow: hidden;
            display: flex;
            flex-direction: column;
            transition: transform 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease;
            position: relative;
        }}
        .video-card:hover {{
            transform: translateY(-2px);
            border-color: #3f3f46;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.6);
        }}
        .video-card.is-marked-for-merge {{
            border-color: #a855f7 !important;
            box-shadow: 0 0 15px rgba(168, 85, 247, 0.3) !important;
        }}

        /* 16:9 Thumbnail Box */
        .video-card-thumb {{
            position: relative;
            width: 100% !important;
            aspect-ratio: 16 / 9;
            background: #000;
            overflow: hidden;
            cursor: pointer;
            user-select: none;
            border-radius: 11px 11px 0 0 !important;
            border-bottom: 1px solid var(--border-subtle) !important;
        }}

        .thumb-canvas {{
            width: 100%;
            height: 100%;
            background-size: cover;
            background-position: center;
            background-repeat: no-repeat;
            transition: transform 0.25s ease;
        }}
        .video-card:hover .thumb-canvas {{
            transform: scale(1.02);
        }}

        .thumb-fallback {{
            width: 100%;
            height: 100%;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            background: #121212;
            color: #71717a;
            gap: 4px;
        }}
        .thumb-fallback-icon {{ font-size: 1.8rem; }}
        .thumb-fallback-txt {{ font-size: 0.7rem; font-weight: 700; letter-spacing: 0.5px; }}

        .thumb-screens-overlay {{
            position: absolute;
            inset: 0;
            background-size: contain;
            background-position: center;
            background-repeat: no-repeat;
            background-color: #000;
            z-index: 15;
            display: none;
        }}

        .thumb-scrub-bar {{
            position: absolute;
            bottom: 0;
            left: 0;
            right: 0;
            height: 5px;
            background: rgba(255, 255, 255, 0.15);
            z-index: 8;
            cursor: ew-resize;
        }}
        .thumb-scrub-fill {{
            height: 100%;
            width: 0%;
            background: #38bdf8;
            box-shadow: 0 0 6px rgba(56, 189, 248, 0.8);
        }}

        .thumb-overlay-play {{
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%) scale(0.85);
            width: 48px;
            height: 48px;
            background: rgba(0, 0, 0, 0.75);
            backdrop-filter: blur(8px);
            border: 1px solid rgba(255, 255, 255, 0.2);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #ffffff;
            opacity: 0;
            transition: all 0.18s cubic-bezier(0.2, 0, 0, 1);
            z-index: 6;
        }}
        .video-card-thumb:hover .thumb-overlay-play {{
            opacity: 1;
            transform: translate(-50%, -50%) scale(1);
        }}

        .thumb-screens-btn {{
            position: absolute;
            top: 8px;
            right: 8px;
            background: rgba(10, 15, 30, 0.9);
            border: 1px solid rgba(255, 255, 255, 0.2);
            color: #ffffff;
            font-size: 0.68rem;
            font-weight: 700;
            padding: 3px 7px;
            border-radius: 5px;
            cursor: pointer;
            z-index: 10;
            transition: all 0.15s ease;
        }}
        .thumb-screens-btn:hover {{
            background: #38bdf8;
            color: #0b1120;
            border-color: #38bdf8;
        }}

        .card-top-badges {{
            position: absolute;
            top: 6px;
            left: 6px;
            display: flex;
            flex-direction: column;
            align-items: flex-start;
            gap: 4px;
            z-index: 10;
            pointer-events: none;
        }}

        .card-cache-pill {{
            font-size: 0.65rem;
            font-weight: 700;
            padding: 2px 6px;
            border-radius: 4px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            display: inline-flex;
            align-items: center;
            gap: 4px;
        }}
        .card-cache-pill.is-pinned {{ background: rgba(16, 185, 129, 0.9); color: #fff; }}
        .card-cache-pill.is-downloading {{ background: rgba(245, 158, 11, 0.9); color: #000; }}
        .card-cache-pill.is-cached {{ background: rgba(56, 189, 248, 0.9); color: #0b1120; }}
        .card-cache-pill.is-cloud {{ background: rgba(0, 0, 0, 0.75); color: #a1a1aa; border: 1px solid rgba(255,255,255,0.1); }}

        .card-size-badge {{
            background: rgba(10, 15, 30, 0.95);
            backdrop-filter: blur(10px);
            color: #93c5fd;
            font-size: 0.65rem;
            font-weight: 700;
            font-family: monospace;
            padding: 2.5px 7px;
            border-radius: 5px;
            border: 1px solid rgba(255, 255, 255, 0.2);
            display: inline-flex;
            align-items: center;
            gap: 5px;
            line-height: 1.25;
        }}
        .card-size-badge .badge-vsize {{ color: #93c5fd; }}
        .card-size-badge .badge-sep {{ color: rgba(255, 255, 255, 0.4); }}
        .card-size-badge .badge-asize {{ color: #6ee7b7; }}

        .thumb-cache-track {{
            position: absolute;
            bottom: 0;
            left: 0;
            right: 0;
            height: 3px;
            background: rgba(255, 255, 255, 0.08);
            z-index: 4;
            pointer-events: none;
        }}
        .thumb-cache-fill {{
            height: 100%;
            background: #10b981;
            transition: width 0.3s ease;
        }}

        .card-resume-bar {{
            position: absolute;
            bottom: 0;
            left: 0;
            height: 3.5px;
            background: #ef4444;
            box-shadow: 0 0 6px rgba(239, 68, 68, 0.85);
            z-index: 5;
            pointer-events: none;
            transition: width 0.2s ease;
        }}
        .card-resume-badge {{
            position: absolute;
            bottom: 6px;
            left: 6px;
            background: rgba(220, 38, 38, 0.9);
            color: #ffffff;
            font-size: 0.62rem;
            font-weight: 700;
            font-family: monospace;
            padding: 1px 6px;
            border-radius: 4px;
            border: 1px solid rgba(255, 255, 255, 0.25);
            z-index: 6;
            pointer-events: none;
        }}
        .video-card-thumb:hover .card-resume-badge {{ opacity: 0; }}
        .video-card-thumb:hover .card-duration-badge,
        .video-card:hover .card-duration-badge {{
            opacity: 0 !important;
            visibility: hidden !important;
        }}

        .card-duration-badge {{
            position: absolute;
            bottom: 8px;
            right: 8px;
            background: rgba(0, 0, 0, 0.82);
            color: #ffffff;
            font-size: 0.72rem;
            font-weight: 700;
            padding: 2px 6px;
            border-radius: 4px;
            z-index: 7;
            letter-spacing: 0.02em;
        }}

        /* IN-THUMBNAIL SCREENSHOT (SS) OVERLAY: 0% CARD PUSH */
        .card-ss-overlay {{
            position: absolute;
            inset: 0;
            background: #121212;
            z-index: 25;
            display: flex;
            flex-direction: column;
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.2s cubic-bezier(0.2, 0, 0, 1);
            border-radius: 11px 11px 0 0;
        }}
        .card-ss-overlay.is-open {{
            opacity: 1;
            pointer-events: auto;
        }}
        .ss-overlay-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 8px 12px;
            background: #181818;
            border-bottom: 1px solid #272727;
            font-size: 0.78rem;
            font-weight: 600;
            color: #e4e4e7;
            flex-shrink: 0;
        }}
        .ss-close-btn {{
            background: transparent;
            border: none;
            color: #a1a1aa;
            cursor: pointer;
            font-size: 1.1rem;
            line-height: 1;
            padding: 0 4px;
        }}
        .ss-close-btn:hover {{ color: #ffffff; }}
        .ss-filmstrip-scroll {{
            flex: 1;
            overflow-x: auto;
            overflow-y: hidden;
            display: flex;
            gap: 8px;
            padding: 8px 10px;
            align-items: center;
            scroll-snap-type: x mandatory;
        }}
        .ss-frame-item {{
            height: 100%;
            aspect-ratio: 16 / 9;
            background: #000;
            border: 1px solid #272727;
            border-radius: 6px;
            background-size: cover;
            background-position: center;
            flex-shrink: 0;
            scroll-snap-align: start;
            cursor: pointer;
            position: relative;
            transition: transform 0.15s ease, border-color 0.15s ease;
        }}
        .ss-frame-item:hover {{
            transform: scale(1.04);
            border-color: #38bdf8;
        }}
        .ss-frame-time {{
            position: absolute;
            bottom: 4px;
            right: 4px;
            background: rgba(0, 0, 0, 0.8);
            color: #ffffff;
            font-size: 0.65rem;
            padding: 1px 4px;
            border-radius: 3px;
            font-weight: 600;
        }}

        /* Card Body */
        .video-card-body {{
            padding: 12px 14px 10px 14px;
            flex: 1;
            display: flex;
            flex-direction: column;
            gap: 6px;
            min-width: 0;
        }}

        .video-card-title-row {{
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 6px;
        }}
        .video-card-title {{
            font-size: 0.92rem;
            font-weight: 600;
            color: #f9fafb;
            line-height: 1.35;
            cursor: pointer;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
            transition: color 0.15s;
            flex: 1;
        }}
        .video-card-title:hover {{ color: var(--primary); }}

        .video-card-title-actions {{
            display: flex;
            align-items: center;
            gap: 4px;
            flex-shrink: 0;
        }}
        .card-offline-tag-btn, .card-mark-merge-pill, .card-action-arrow-btn {{
            width: 24px;
            height: 24px;
            border-radius: 5px;
            border: 1px solid rgba(255, 255, 255, 0.1);
            background: rgba(255, 255, 255, 0.04);
            color: #9ca3af;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            cursor: pointer;
            padding: 0;
            transition: all 0.18s ease;
        }}
        .card-offline-tag-btn:hover, .card-mark-merge-pill:hover, .card-action-arrow-btn:hover {{
            background: rgba(255, 255, 255, 0.12);
            color: #ffffff;
            border-color: rgba(255, 255, 255, 0.25);
        }}
        .card-offline-tag-btn.active {{ color: #10b981; border-color: rgba(16, 185, 129, 0.4); }}
        .card-action-arrow-btn.is-active {{
            background: rgba(56, 189, 248, 0.18);
            border-color: #38bdf8;
            color: #38bdf8;
        }}
        .card-action-arrow-btn.is-active .card-arrow-icon {{
            transform: rotate(180deg);
        }}

        .video-card-sub {{
            font-size: 0.72rem;
            color: #6b7280;
            font-family: monospace;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}
        .card-overview-snippet {{
            font-size: 0.75rem;
            color: #94a3b8;
            font-style: italic;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}

        /* All Card Badges & Pills */
        .video-card-badges {{
            display: flex;
            flex-wrap: wrap;
            gap: 4px;
            align-items: center;
            margin-top: 4px;
        }}
        .meta-pill, .genre-pill, .tag-pill, .account-pill {{
            font-size: 0.68rem;
            padding: 2px 7px;
            border-radius: 5px;
            cursor: pointer !important;
            transition: all 0.12s ease;
            user-select: none;
            display: inline-flex;
            align-items: center;
            gap: 4px;
            background: #272727;
            color: #e4e4e7;
            border: 1px solid rgba(255, 255, 255, 0.06);
        }}
        .meta-pill:hover, .genre-pill:hover, .tag-pill:hover, .account-pill:hover {{
            background: #333338;
            color: #ffffff;
            border-color: rgba(255, 255, 255, 0.2);
            transform: translateY(-1px);
        }}
        .genre-pill {{
            background: #1e293b;
            color: #93c5fd;
            border-color: rgba(147, 197, 253, 0.2);
            font-weight: 600;
        }}
        .account-pill.is-primary {{
            color: #c084fc;
        }}
        .sub-pill {{
            color: #38bdf8;
        }}
        .card-alias-cluster {{
            display: flex;
            flex-wrap: wrap;
            gap: 4px;
            margin-top: 4px;
        }}
        .alias-card-pill {{
            background: rgba(147, 51, 234, 0.12);
            border: 1px solid rgba(147, 51, 234, 0.3);
            color: #d8b4fe;
            font-size: 0.68rem;
            padding: 2px 6px;
            border-radius: 4px;
            display: inline-flex;
            align-items: center;
            gap: 4px;
        }}
        .btn-alias-direct-edit {{
            background: transparent;
            border: none;
            color: #c084fc;
            cursor: pointer;
            font-size: 0.66rem;
            padding: 1px 3px;
        }}

        /* Sliding Open/Collapse Bottom Action Bar */
        .video-card-actions {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 6px;
            padding: 0 12px;
            max-height: 0;
            opacity: 0;
            overflow: hidden;
            border-top: 1px solid transparent;
            background: rgba(0, 0, 0, 0.3);
            margin-top: auto;
            transition: max-height 0.25s cubic-bezier(0.4, 0, 0.2, 1),
                        opacity 0.2s ease,
                        padding 0.25s cubic-bezier(0.4, 0, 0.2, 1),
                        border-color 0.25s ease;
        }}
        .video-card.is-expanded .video-card-actions,
        .video-card-actions.is-open {{
            max-height: 48px;
            opacity: 1;
            padding: 8px 12px;
            border-top-color: rgba(255, 255, 255, 0.08);
        }}
        .card-action-group-left, .card-action-group-right {{
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .card-icon-btn {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 28px;
            height: 28px;
            border-radius: 6px;
            border: 1px solid rgba(255, 255, 255, 0.1);
            background: rgba(255, 255, 255, 0.04);
            color: #e5e7eb;
            font-size: 0.85rem;
            cursor: pointer;
            text-decoration: none;
            transition: all 0.15s ease;
        }}
        .card-icon-btn:hover {{
            background: rgba(255, 255, 255, 0.15);
            color: #ffffff;
            border-color: rgba(255, 255, 255, 0.25);
            transform: translateY(-1px);
        }}

        /* =========================================================================
           100% EXACT EXPLORER YOUTUBE PLAYER CSS (.yt-player-box)
           ========================================================================= */
        .yt-player-box {{
            background: #000000;
            border-radius: 14px;
            overflow: hidden;
            margin-bottom: 26px;
            box-shadow: 0 16px 50px rgba(0,0,0,0.9);
            border: 1px solid rgba(255,255,255,0.12);
            display: none;
            transition: max-width 0.3s ease, margin 0.3s ease;
        }}
        .yt-player-box.theater-mode {{
            max-width: 100% !important;
            border-radius: 0;
            margin-left: -28px;
            margin-right: -28px;
            border-left: none;
            border-right: none;
        }}
        .yt-container {{
            position: relative;
            width: 100%;
            background: #000;
            outline: none;
            aspect-ratio: 16 / 9;
            max-height: 75vh;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
            user-select: none;
        }}
        .yt-container video {{
            width: 100%;
            height: 100%;
            object-fit: contain;
            display: block;
        }}
        .yt-top-overlay {{
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            padding: 14px 18px;
            background: linear-gradient(to bottom, rgba(0,0,0,0.85) 0%, rgba(0,0,0,0.4) 60%, transparent 100%);
            display: flex;
            justify-content: space-between;
            align-items: center;
            z-index: 25;
            transition: opacity 0.25s ease;
        }}
        .yt-container.controls-hidden .yt-top-overlay,
        .yt-container.controls-hidden .yt-controls-overlay {{
            opacity: 0;
            pointer-events: none;
        }}
        .yt-top-title-group {{
            display: flex;
            align-items: center;
            gap: 12px;
            overflow: hidden;
        }}
        .yt-security-badge {{
            background: rgba(16, 185, 129, 0.15);
            border: 1px solid rgba(16, 185, 129, 0.35);
            color: #34d399;
            font-size: 0.72rem;
            font-weight: 700;
            padding: 3px 8px;
            border-radius: 6px;
            display: inline-flex;
            align-items: center;
            gap: 5px;
            text-transform: uppercase;
            flex-shrink: 0;
        }}
        .yt-live-dot {{
            width: 7px;
            height: 7px;
            border-radius: 50%;
            background: #10b981;
            box-shadow: 0 0 8px #10b981;
        }}
        .yt-video-title {{
            color: #f3f4f6;
            font-size: 0.95rem;
            font-weight: 600;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }}
        .yt-top-actions {{
            display: flex;
            align-items: center;
            gap: 8px;
            flex-shrink: 0;
        }}

        .yt-center-feedback {{
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%) scale(0.6);
            background: rgba(0, 0, 0, 0.7);
            color: #fff;
            width: 76px;
            height: 76px;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.8rem;
            pointer-events: none;
            opacity: 0;
            transition: transform 0.25s cubic-bezier(0.18, 0.89, 0.32, 1.28), opacity 0.25s ease;
            z-index: 20;
            backdrop-filter: blur(4px);
        }}
        .yt-center-feedback.show {{
            transform: translate(-50%, -50%) scale(1.15);
            opacity: 1;
        }}

        .yt-spinner {{
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            z-index: 18;
            pointer-events: none;
        }}
        .yt-spinner-ring {{
            width: 52px;
            height: 52px;
            border: 4px solid rgba(255, 255, 255, 0.2);
            border-top-color: #38bdf8;
            border-radius: 50%;
            animation: yt-spin 0.8s linear infinite;
        }}
        @keyframes yt-spin {{ to {{ transform: rotate(360deg); }} }}

        .yt-controls-overlay {{
            position: absolute;
            bottom: 0;
            left: 0;
            right: 0;
            background: linear-gradient(to top, rgba(0,0,0,0.92) 0%, rgba(0,0,0,0.6) 60%, transparent 100%);
            padding: 8px 16px 10px 16px;
            z-index: 25;
            transition: opacity 0.25s ease;
        }}
        .yt-scrub-container {{
            position: relative;
            width: 100%;
            height: 16px;
            display: flex;
            align-items: center;
            cursor: pointer;
            margin-bottom: 6px;
        }}
        .yt-scrub-track {{
            position: relative;
            width: 100%;
            height: 4px;
            background: rgba(255, 255, 255, 0.22);
            border-radius: 2px;
            transition: height 0.15s ease;
        }}
        .yt-scrub-container:hover .yt-scrub-track {{ height: 6px; }}
        .yt-scrub-buffered {{
            position: absolute;
            top: 0;
            left: 0;
            height: 100%;
            width: 0%;
            background: rgba(255, 255, 255, 0.45);
            border-radius: 2px;
            pointer-events: none;
        }}
        .yt-scrub-progress {{
            position: absolute;
            top: 0;
            left: 0;
            height: 100%;
            width: 0%;
            background: #38bdf8;
            border-radius: 2px;
            pointer-events: none;
        }}
        .yt-scrub-handle {{
            position: absolute;
            right: -6px;
            top: 50%;
            transform: translateY(-50%) scale(0);
            width: 13px;
            height: 13px;
            background: #38bdf8;
            border-radius: 50%;
            transition: transform 0.15s ease;
            box-shadow: 0 0 6px rgba(56,189,248,0.7);
        }}
        .yt-scrub-container:hover .yt-scrub-handle {{ transform: translateY(-50%) scale(1); }}

        .yt-scrub-tooltip {{
            position: absolute;
            bottom: 26px;
            transform: translateX(-50%);
            background: rgba(14, 14, 18, 0.96);
            border-radius: 8px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.85);
            border: 1px solid rgba(255,255,255,0.18);
            display: none;
            pointer-events: none;
            overflow: hidden;
            text-align: center;
            z-index: 50;
        }}
        .yt-thumb-preview {{
            width: 200px;
            height: 112px;
            background-color: #000;
            background-repeat: no-repeat;
            display: block;
        }}
        .yt-tooltip-time {{
            padding: 4px 8px;
            font-size: 0.76rem;
            font-weight: 700;
            color: #f3f4f6;
            background: rgba(0, 0, 0, 0.7);
            border-top: 1px solid rgba(255, 255, 255, 0.08);
        }}

        .yt-controls-row {{
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .yt-controls-left, .yt-controls-right {{
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .yt-btn {{
            background: transparent;
            border: none;
            color: #e5e7eb;
            cursor: pointer;
            padding: 6px;
            border-radius: 6px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            transition: all 0.15s;
            outline: none;
        }}
        .yt-btn:hover {{ color: #fff; background: rgba(255,255,255,0.12); }}
        .yt-btn-speed {{ font-size: 0.82rem; font-weight: 600; padding: 4px 8px; min-width: 36px; }}
        /* Volume Slider */
        .yt-volume-group {{
            display: flex;
            align-items: center;
            position: relative;
        }}
        .yt-volume-slider-wrap {{
            width: 0;
            overflow: hidden;
            transition: width 0.2s cubic-bezier(0.4, 0, 0.2, 1);
            display: flex;
            align-items: center;
        }}
        .yt-volume-group:hover .yt-volume-slider-wrap,
        .yt-volume-slider-wrap:focus-within {{
            width: 82px;
            margin-right: 10px;
        }}
        .yt-volume-slider {{
            width: 78px;
            height: 5px;
            -webkit-appearance: none;
            appearance: none;
            background: linear-gradient(to right, #38bdf8 0%, #38bdf8 100%, rgba(255,255,255,0.25) 100%, rgba(255,255,255,0.25) 100%);
            border-radius: 4px;
            outline: none;
            cursor: pointer;
            transition: background 0.1s ease;
        }}
        .yt-volume-slider::-webkit-slider-thumb {{
            -webkit-appearance: none;
            appearance: none;
            width: 14px;
            height: 14px;
            border-radius: 50%;
            background: #ffffff;
            border: 2px solid #38bdf8;
            box-shadow: 0 1px 6px rgba(0,0,0,0.4), 0 0 8px rgba(56, 189, 248, 0.8);
            cursor: pointer;
            transition: transform 0.15s ease, box-shadow 0.15s ease;
        }}
        .yt-volume-slider::-webkit-slider-thumb:hover {{
            transform: scale(1.25);
            box-shadow: 0 2px 10px rgba(0,0,0,0.6), 0 0 12px rgba(56, 189, 248, 1);
        }}
        .yt-volume-slider::-moz-range-thumb {{
            width: 14px;
            height: 14px;
            border-radius: 50%;
            background: #ffffff;
            border: 2px solid #38bdf8;
            box-shadow: 0 1px 6px rgba(0,0,0,0.4), 0 0 8px rgba(56, 189, 248, 0.8);
            cursor: pointer;
        }}
        .yt-container.is-floating-mini .yt-volume-group:hover .yt-volume-slider-wrap,
        .yt-container.is-floating-mini .yt-volume-slider-wrap:focus-within {{
            width: 65px !important;
            margin-right: 6px !important;
        }}
        .yt-container.is-floating-mini .yt-volume-slider {{
            width: 60px !important;
        }}
        .yt-container.is-floating-mini #videoPlayer {{
            cursor: pointer;
        }}
        .yt-time-display {{
            font-size: 0.8rem;
            color: #d1d5db;
            font-weight: 500;
            margin-left: 6px;
        }}

        /* Floating Mini Player (YouTube style) */
        .yt-container.is-floating-mini {{
            position: fixed !important;
            bottom: 24px;
            right: 24px;
            width: 480px;
            max-width: calc(100vw - 24px) !important;
            aspect-ratio: 16 / 9 !important;
            height: auto !important;
            max-height: 270px;
            z-index: 99999 !important;
            border-radius: 12px !important;
            box-shadow: 0 16px 45px rgba(0, 0, 0, 0.9), 0 0 0 1px rgba(255, 255, 255, 0.16) !important;
            overflow: hidden !important;
            backdrop-filter: blur(8px) !important;
        }}
        .yt-container.is-floating-mini.mini-size-s {{ width: 360px !important; max-height: 202px !important; }}
        .yt-container.is-floating-mini.mini-size-m {{ width: 500px !important; max-height: 281px !important; }}
        .yt-container.is-floating-mini.mini-size-l {{ width: 680px !important; max-height: 382px !important; }}
        .yt-container.is-floating-mini .yt-top-overlay {{ cursor: move; }}
        .yt-mini-size-controls {{ display: none; }}
        .yt-container.is-floating-mini .yt-mini-size-controls {{
            display: inline-flex !important;
            align-items: center;
            gap: 2px;
            background: rgba(0, 0, 0, 0.45);
            border-radius: 4px;
            padding: 1px 3px;
            border: 1px solid rgba(255, 255, 255, 0.15);
            margin-right: 4px;
        }}
        .yt-mini-size-btn {{
            background: transparent;
            border: none;
            color: #94a3b8;
            font-size: 0.68rem;
            font-weight: 700;
            padding: 1px 4px;
            cursor: pointer;
            border-radius: 2px;
        }}
        .yt-mini-size-btn.active {{ color: #ffffff; background: rgba(56, 189, 248, 0.3); }}

        /* Popovers: Speed & Subtitles */
        .yt-speed-menu, .yt-sub-menu {{
            position: absolute;
            bottom: 42px;
            right: 0;
            background: rgba(20, 20, 24, 0.96);
            backdrop-filter: blur(10px);
            border: 1px solid rgba(255,255,255,0.12);
            border-radius: 10px;
            padding: 6px 0;
            width: 150px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.8);
            z-index: 40;
        }}
        .yt-speed-title, .yt-sub-header {{
            padding: 6px 14px;
            font-size: 0.72rem;
            color: #9ca3af;
            text-transform: uppercase;
            border-bottom: 1px solid rgba(255,255,255,0.08);
            margin-bottom: 4px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .yt-speed-option, .yt-sub-option {{
            padding: 6px 14px;
            font-size: 0.82rem;
            color: #e5e7eb;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}
        .yt-speed-option:hover, .yt-sub-option:hover {{ background: rgba(255,255,255,0.1); color: #fff; }}
        .yt-speed-option.active, .yt-sub-option.active {{ color: #38bdf8; font-weight: 700; }}

        /* Stats for Nerds */
        .yt-stats-panel {{
            position: absolute;
            top: 56px;
            left: 18px;
            background: rgba(10, 10, 14, 0.88);
            backdrop-filter: blur(8px);
            border: 1px solid rgba(255,255,255,0.15);
            border-radius: 8px;
            padding: 10px 14px;
            color: #e5e7eb;
            font-size: 0.75rem;
            font-family: monospace;
            z-index: 35;
            max-width: 360px;
        }}
        .yt-stats-header {{
            display: flex;
            justify-content: space-between;
            font-weight: 700;
            color: #38bdf8;
            border-bottom: 1px solid rgba(255,255,255,0.1);
            padding-bottom: 4px;
            margin-bottom: 8px;
        }}
        .yt-stats-grid {{
            display: grid;
            grid-template-columns: 130px 1fr;
            row-gap: 4px;
            column-gap: 8px;
        }}

        /* Modals: Screens Lightbox & Metadata Edit */
        .modal-overlay {{
            position: fixed;
            inset: 0;
            background: rgba(0, 0, 0, 0.8);
            backdrop-filter: blur(8px);
            z-index: 10000;
            display: none;
            align-items: center;
            justify-content: center;
            padding: 20px;
        }}
        .modal {{
            background: #181818;
            border: 1px solid #272727;
            border-radius: 12px;
            box-shadow: 0 20px 60px rgba(0, 0, 0, 0.9);
            width: 100%;
            max-width: 680px;
            max-height: 90vh;
            overflow-y: auto;
            padding: 22px;
        }}
        .modal-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 16px;
            border-bottom: 1px solid #272727;
            padding-bottom: 12px;
        }}
        .form-group {{ margin-bottom: 14px; }}
        .form-label {{ display: block; font-size: 0.8rem; font-weight: 600; color: #a1a1aa; margin-bottom: 5px; }}
        .form-input {{
            width: 100%;
            background: #121212;
            border: 1px solid #272727;
            border-radius: 6px;
            padding: 8px 12px;
            color: #ffffff;
            font-size: 0.85rem;
            outline: none;
        }}
        .form-input:focus {{ border-color: #38bdf8; }}
        .modal-actions {{
            display: flex;
            justify-content: flex-end;
            gap: 10px;
            margin-top: 20px;
            border-top: 1px solid #272727;
            padding-top: 14px;
        }}

        /* Slide-Out Drawer: Watch Later & History */
        .drawer-clipped-tab {{
            position: fixed;
            right: 0;
            top: 50%;
            transform: translateY(-50%);
            background: #181818;
            border: 1px solid #272727;
            border-right: none;
            border-radius: 8px 0 0 8px;
            padding: 12px 10px;
            color: #e4e4e7;
            font-size: 0.78rem;
            font-weight: 600;
            cursor: pointer;
            z-index: 900;
            box-shadow: -4px 0 20px rgba(0, 0, 0, 0.5);
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 6px;
            writing-mode: vertical-rl;
            text-orientation: mixed;
            transition: all 0.15s ease;
        }}
        .drawer-clipped-tab:hover {{ background: #272727; color: #ffffff; }}
        .drawer-tab-badge {{
            writing-mode: horizontal-tb;
            background: #38bdf8;
            color: #0b1120;
            font-size: 0.68rem;
            padding: 2px 5px;
            border-radius: 9999px;
            font-weight: 700;
        }}

        .drawer-overlay {{
            position: fixed;
            inset: 0;
            background: rgba(0, 0, 0, 0.6);
            backdrop-filter: blur(4px);
            z-index: 2000;
            display: none;
            opacity: 0;
            transition: opacity 0.25s ease;
        }}
        .drawer-overlay.is-visible {{ display: block; opacity: 1; }}

        .drawer-panel {{
            position: fixed;
            top: 0;
            right: 0;
            bottom: 0;
            width: 400px;
            max-width: 90vw;
            background: #121212;
            border-left: 1px solid #272727;
            z-index: 2001;
            display: flex;
            flex-direction: column;
            transform: translateX(100%);
            transition: transform 0.25s cubic-bezier(0.2, 0, 0, 1);
            box-shadow: -10px 0 40px rgba(0, 0, 0, 0.8);
        }}
        .drawer-panel.is-open {{ transform: translateX(0); }}
        .drawer-header {{
            padding: 16px 20px;
            border-bottom: 1px solid #272727;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}
        .drawer-tabs {{
            display: flex;
            gap: 8px;
            padding: 12px 20px;
            border-bottom: 1px solid #272727;
        }}
        .drawer-tab-btn {{
            background: transparent;
            border: 1px solid transparent;
            color: #71717a;
            padding: 6px 14px;
            border-radius: 6px;
            font-size: 0.82rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.15s ease;
        }}
        .drawer-tab-btn.active {{
            background: #272727;
            color: #ffffff;
            border-color: rgba(255, 255, 255, 0.08);
        }}
        .drawer-list {{
            flex: 1;
            overflow-y: auto;
            padding: 14px 20px;
            display: flex;
            flex-direction: column;
            gap: 12px;
        }}
        .drawer-item {{
            display: flex;
            gap: 12px;
            background: #181818;
            border: 1px solid #272727;
            border-radius: 8px;
            padding: 8px;
            cursor: pointer;
            position: relative;
            transition: background 0.15s ease, border-color 0.15s ease;
        }}
        .drawer-item:hover {{ background: #222222; border-color: #3f3f46; }}
        .drawer-item-thumb {{
            width: 100px;
            aspect-ratio: 16 / 9;
            background-size: cover;
            background-position: center;
            border-radius: 5px;
            flex-shrink: 0;
            position: relative;
            background-color: #000;
        }}
        .drawer-item-info {{ flex: 1; min-width: 0; display: flex; flex-direction: column; justify-content: space-between; }}
        .drawer-item-title {{
            font-size: 0.82rem;
            font-weight: 600;
            color: #f4f4f5;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }}
        .drawer-item-sub {{ font-size: 0.72rem; color: #71717a; }}
        .drawer-item-remove {{
            position: absolute;
            top: 6px;
            right: 6px;
            background: transparent;
            border: none;
            color: #71717a;
            cursor: pointer;
        }}
        .drawer-item-remove:hover {{ color: #ef4444; }}

        /* Toast notification */
        .discover-toast {{
            position: fixed;
            bottom: 24px;
            left: 50%;
            transform: translateX(-50%) translateY(20px);
            background: #272727;
            color: #ffffff;
            border: 1px solid #3f3f46;
            padding: 8px 16px;
            border-radius: 9999px;
            font-size: 0.82rem;
            font-weight: 600;
            z-index: 4000;
            box-shadow: 0 8px 25px rgba(0, 0, 0, 0.6);
            opacity: 0;
            pointer-events: none;
            transition: all 0.25s cubic-bezier(0.2, 0, 0, 1);
        }}
        .discover-toast.show {{
            opacity: 1;
            transform: translateX(-50%) translateY(0);
        }}

        .feed-footer {{
            text-align: center;
            padding: 40px 20px;
            color: #71717a;
            font-size: 0.86rem;
        }}
        .feed-spinner {{
            width: 28px;
            height: 28px;
            border: 3px solid rgba(255, 255, 255, 0.1);
            border-top-color: #38bdf8;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            margin: 0 auto 12px auto;
        }}
        @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    </style>
</head>
<body>

    <!-- Sticky Top Navigation & Filter Bar -->
    <header class="discover-header">
        <div class="header-top-row">
            <div class="brand-cluster">
                <a href="/discover" class="brand-title">
                    <span>✨</span> HFSYNC Discover
                </a>
                <span class="brand-count-badge" id="totalCountBadge">{len(manifest_data)} Videos</span>
            </div>

            <div class="search-container">
                <span class="search-icon-left">🔍</span>
                <input type="text" id="discoverSearchInput" class="search-input" placeholder="Search videos, categories, tags, codecs, resolution..." autocomplete="off">
                <button type="button" id="searchClearBtn" class="search-clear-btn" onclick="clearSearch()">&times;</button>
                <span class="search-shortcut-hint" id="shortcutHint">/</span>
            </div>

            <div class="header-actions-cluster">
                <select id="sortSelect" class="sort-select" onchange="handleSortChange(this.value)">
                    <option value="newest">🕒 Newest Added</option>
                    <option value="oldest">🕒 Oldest Added</option>
                    <option value="name_asc">🔤 Title (A &rarr; Z)</option>
                    <option value="name_desc">🔤 Title (Z &rarr; A)</option>
                    <option value="duration_desc">⏱️ Duration (Longest)</option>
                    <option value="duration_asc">⏱️ Duration (Shortest)</option>
                    <option value="size_desc">💾 Size (Largest)</option>
                    <option value="size_asc">💾 Size (Smallest)</option>
                    <option value="rating_desc">⭐ Rating (Highest)</option>
                    <option value="random">🔀 Random Shuffle</option>
                </select>

                <a href="/" class="flat-btn" title="Switch back to Folder Tree Explorer">
                    <span>📁</span> Vault Explorer
                </a>
                <a href="/#stagingPushSection" id="stagedQueueBadge" class="flat-btn" style="display: none; background: #065f46; color: #6ee7b7; border-color: #059669; font-weight: 700;" title="Batches staged and ready for 1-commit push">
                    <span>🚀</span> <span id="stagedQueueCount">0</span> Ready to Push
                </a>
            </div>
        </div>

        <!-- Scrollable Filter Chips Row -->
        <div class="filter-chips-row" id="filterChipsRow">
            <div class="filter-chip is-active" data-filter="all" onclick="setPresetFilter('all', this)">All</div>
            <div class="filter-chip" data-filter="4k" onclick="setPresetFilter('4k', this)">✨ 4K UHD</div>
            <div class="filter-chip" data-filter="1080p" onclick="setPresetFilter('1080p', this)">📺 1080p HD</div>
            <div class="filter-chip" data-filter="720p" onclick="setPresetFilter('720p', this)">📺 720p</div>
            <div class="filter-chip" data-filter="short" onclick="setPresetFilter('short', this)">⏱️ Short (&lt;15m)</div>
            <div class="filter-chip" data-filter="long" onclick="setPresetFilter('long', this)">🎬 Feature (&gt;30m)</div>
            <div class="filter-chip" data-filter="offline" onclick="setPresetFilter('offline', this)">📌 Offline Ready</div>
            <div class="filter-chip" data-filter="cached" onclick="setPresetFilter('cached', this)">💾 Cached</div>

            <!-- Active Custom Filter Badges rendered dynamically here -->
            <div id="activeFiltersContainer" style="display: inline-flex; gap: 6px; align-items: center;"></div>
            <button type="button" id="btnClearAllFilters" class="btn-clear-filters" onclick="clearAllFilters()">
                &times; Clear All
            </button>
        </div>
    </header>

    <!-- Main Container -->
    <main class="discover-container">
        <!-- Exact 100% Explorer YouTube Player Box (Auto-Docks on Scroll) -->
        <div id="playerBox" class="yt-player-box">
            <div id="ytContainer" class="yt-container" tabindex="0" ondblclick="if (isFloatingMini) expandFromFloatingMini();">
                <!-- Video Element -->
                <video id="videoPlayer" playsinline preload="metadata"></video>

                <!-- Center Animated Feedback -->
                <div id="ytCenterFeedback" class="yt-center-feedback">▶</div>

                <!-- Buffering Spinner -->
                <div id="ytSpinner" class="yt-spinner" style="display: none;">
                    <div class="yt-spinner-ring"></div>
                </div>

                <!-- Top Title & Controls Overlay -->
                <div class="yt-top-overlay">
                    <div class="yt-top-title-group">
                        <span class="yt-security-badge"><span class="yt-live-dot"></span> Decrypted Stream</span>
                        <span id="playerTitle" class="yt-video-title">Now Playing</span>
                    </div>
                    <div class="yt-top-actions">
                        <div class="yt-mini-size-controls" title="Change Popup Player Size">
                            <button type="button" class="yt-mini-size-btn" data-size="s" onclick="setMiniPlayerSize('s', event)" title="Small (360px)">S</button>
                            <button type="button" class="yt-mini-size-btn active" data-size="m" onclick="setMiniPlayerSize('m', event)" title="Medium (500px)">M</button>
                            <button type="button" class="yt-mini-size-btn" data-size="l" onclick="setMiniPlayerSize('l', event)" title="Large (680px)">L</button>
                        </div>
                        <button type="button" class="yt-btn yt-mini-expand-btn" onclick="expandFromFloatingMini()" title="Expand to Main View">
                            <svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/></svg>
                        </button>
                        <button type="button" class="yt-btn" onclick="toggleStatsForNerds()" title="Stats for Nerds (s)">
                            <svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M19 3H5c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm-2 10h-4v4h-2v-4H7v-2h4V7h2v4h4v2z"/></svg>
                        </button>
                        <button type="button" class="yt-btn" onclick="toggleTheaterMode()" title="Theater Mode (t)">
                            <svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M19 6H5c-1.1 0-2 .9-2 2v8c0 1.1.9 2 2 2h14c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2zm0 10H5V8h14v8z"/></svg>
                        </button>
                        <button type="button" class="yt-btn" onclick="closePlayer()" style="font-size: 1.3rem; line-height: 1; padding: 2px 8px;" title="Close Player (Esc)">&times;</button>
                    </div>
                </div>

                <!-- Stats for Nerds Panel -->
                <div id="ytStatsPanel" class="yt-stats-panel" style="display: none;">
                    <div class="yt-stats-header">
                        <span>Decrypted Stream Stats</span>
                        <span style="cursor: pointer;" onclick="toggleStatsForNerds()">&times;</span>
                    </div>
                    <div class="yt-stats-grid">
                        <div>Video Resolution:</div><div id="ytStatRes">-</div>
                        <div>Decryption Engine:</div><div>AES-256-GCM (64KB blocks)</div>
                        <div>Stream Position:</div><div id="ytStatPos">-</div>
                        <div>Buffer Health:</div><div id="ytStatBuffer">-</div>
                        <div>Playback Rate:</div><div id="ytStatSpeed">1x</div>
                        <div>Decrypted Blob:</div><div id="ytStatBlob" style="font-size: 0.7rem; word-break: break-all;">-</div>
                    </div>
                </div>

                <!-- Bottom Controls Overlay -->
                <div id="ytControls" class="yt-controls-overlay">
                    <!-- Progress / Scrub Bar with Floating Storyboard Thumbnail -->
                    <div id="ytScrubContainer" class="yt-scrub-container">
                        <div class="yt-scrub-track">
                            <div id="ytScrubBuffered" class="yt-scrub-buffered"></div>
                            <div id="ytScrubProgress" class="yt-scrub-progress">
                                <div class="yt-scrub-handle"></div>
                            </div>
                        </div>
                        <div id="ytScrubTooltip" class="yt-scrub-tooltip">
                            <div id="ytThumbPreview" class="yt-thumb-preview" style="display: none;"></div>
                            <div id="ytTooltipTime" class="yt-tooltip-time">00:00</div>
                        </div>
                    </div>

                    <!-- Control Buttons Row -->
                    <div class="yt-controls-row">
                        <div class="yt-controls-left">
                            <button type="button" id="ytPlayPauseBtn" class="yt-btn" onclick="togglePlayPause()" title="Play/Pause (k / Space)">
                                <svg id="ytPlayIcon" viewBox="0 0 24 24" width="22" height="22"><path fill="currentColor" d="M8 5v14l11-7z"/></svg>
                                <svg id="ytPauseIcon" viewBox="0 0 24 24" width="22" height="22" style="display: none;"><path fill="currentColor" d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>
                            </button>
                            <button type="button" class="yt-btn" onclick="seekRelative(-10)" title="Rewind 10s (j / Left Arrow)">
                                <svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M12.5 8c-2.65 0-5.05.99-6.9 2.6L2 7v9h9l-3.62-3.62c1.39-1.2 3.16-1.88 5.12-1.88 3.54 0 6.55 2.31 7.6 5.5l2.37-.78C21.08 11.03 17.15 8 12.5 8z"/></svg>
                            </button>
                            <button type="button" class="yt-btn" onclick="seekRelative(10)" title="Fast Forward 10s (l / Right Arrow)">
                                <svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M11.5 8c2.65 0 5.05.99 6.9 2.6L22 7v9h-9l3.62-3.62c-1.39-1.2-3.16-1.88-5.12-1.88-3.54 0-6.55 2.31-7.6 5.5l-2.37-.78C2.92 11.03 6.85 8 11.5 8z"/></svg>
                            </button>
                            <div class="yt-volume-group">
                                <button type="button" id="ytMuteBtn" class="yt-btn" onclick="toggleMute()" title="Mute (m)">
                                    <svg id="ytVolHigh" viewBox="0 0 24 24" width="22" height="22"><path fill="currentColor" d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z"/></svg>
                                    <svg id="ytVolMute" viewBox="0 0 24 24" width="22" height="22" style="display: none;"><path fill="currentColor" d="M16.5 12c0-1.77-1.02-3.29-2.5-4.03v2.21l2.45 2.45c.03-.2.05-.41.05-.63zm2.5 0c0 .94-.2 1.82-.54 2.64l1.51 1.51C20.63 14.91 21 13.5 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3L3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06c1.38-.31 2.63-.95 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4L9.91 6.09 12 8.18V4z"/></svg>
                                </button>
                                <div class="yt-volume-slider-wrap">
                                    <input type="range" id="ytVolumeSlider" class="yt-volume-slider" min="0" max="1" step="0.01" value="1">
                                </div>
                            </div>
                            <div class="yt-time-display">
                                <span id="ytCurTime">00:00</span> / <span id="ytDurTime">00:00</span>
                            </div>
                        </div>

                        <div class="yt-controls-right">
                            <div class="yt-speed-container">
                                <button type="button" id="ytSpeedBtn" class="yt-btn yt-btn-speed" onclick="toggleSpeedMenu()" title="Playback Speed">1x</button>
                                <div id="ytSpeedMenu" class="yt-speed-menu" style="display: none;">
                                    <div class="yt-speed-title">Playback Speed</div>
                                    <div class="yt-speed-option" onclick="setSpeed(0.5)">0.5x</div>
                                    <div class="yt-speed-option" onclick="setSpeed(0.75)">0.75x</div>
                                    <div class="yt-speed-option active" onclick="setSpeed(1)">Normal</div>
                                    <div class="yt-speed-option" onclick="setSpeed(1.25)">1.25x</div>
                                    <div class="yt-speed-option" onclick="setSpeed(1.5)">1.5x</div>
                                    <div class="yt-speed-option" onclick="setSpeed(2)">2x</div>
                                </div>
                            </div>

                            <button type="button" class="yt-btn" onclick="togglePiP()" title="Picture-in-Picture (p)">
                                <svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M19 7h-8v6h8V7zm2-4H3c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h18c1.1 0 2-.9 2-2V5c0-1.1-.9-2-2-2zm0 16.01H3V4.99h18v14.02z"/></svg>
                            </button>

                            <button type="button" class="yt-btn" onclick="toggleFullscreen()" title="Fullscreen (f)">
                                <svg id="ytFsEnter" viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z"/></svg>
                                <svg id="ytFsExit" viewBox="0 0 24 24" width="20" height="20" style="display: none;"><path fill="currentColor" d="M5 16h3v3h2v-5H5v2zm3-8H5v2h5V5H8v3zm6 11h2v-3h3v-2h-5v5zm2-11V5h-2v5h5V8h-3z"/></svg>
                            </button>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        {staged_banner_html}

        <!-- 4-Column Continuous Video Feed -->
        <div id="videoGrid" class="video-grid">
            <!-- Cards rendered dynamically via continuous infinite scroll -->
        </div>

        <div id="feedFooter" class="feed-footer">
            <div id="feedSpinner" class="feed-spinner" style="display: none;"></div>
            <span id="feedFooterText">Loading videos...</span>
        </div>
    </main>

    <!-- Slide-Out Drawer Button (Right Edge) -->
    <div class="drawer-clipped-tab" onclick="toggleWatchDrawer()" title="View Watch Later & Watch History">
        <span>Watchlist &amp; History</span>
        <span class="drawer-tab-badge" id="drawerTabCountBadge">0</span>
    </div>

    <!-- Slide-Out Drawer Panel -->
    <div id="drawerOverlay" class="drawer-overlay" onclick="closeWatchDrawer()"></div>
    <div id="drawerPanel" class="drawer-panel">
        <div class="drawer-header">
            <div style="font-weight: 700; font-size: 1rem; color: #ffffff; display: flex; align-items: center; gap: 8px;">
                <span>📑</span> Watchlist &amp; History
            </div>
            <button type="button" onclick="closeWatchDrawer()" class="ss-close-btn">&times;</button>
        </div>

        <div class="drawer-tabs">
            <button type="button" id="btnTabWatchLater" class="drawer-tab-btn active" onclick="switchDrawerTab('later')">
                ⏱️ Watch Later (<span id="countWatchLater">0</span>)
            </button>
            <button type="button" id="btnTabHistory" class="drawer-tab-btn" onclick="switchDrawerTab('history')">
                🕒 History (<span id="countHistory">0</span>)
            </button>
            <button type="button" onclick="clearCurrentDrawerTab()" style="margin-left: auto; background: transparent; border: none; color: #ef4444; font-size: 0.74rem; cursor: pointer;">
                Clear
            </button>
        </div>

        <div id="drawerItemsList" class="drawer-list"></div>
    </div>

    <!-- Screens Contact Sheet Lightbox Modal (Exact 100% Explorer Replication) -->
    <div id="screensModal" class="modal-overlay" onclick="closeScreensModal(event)">
        <div class="modal" style="max-width: 94vw; width: 1280px; max-height: 94vh; display: flex; flex-direction: column; padding: 16px; background: rgba(15, 20, 32, 0.98); border: 1px solid rgba(255, 255, 255, 0.12);" onclick="event.stopPropagation()">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; border-bottom: 1px solid rgba(255,255,255,0.08); padding-bottom: 8px;">
                <div style="display: flex; align-items: center; gap: 8px; min-width: 0;">
                    <span style="font-size: 1.2rem;">&#128444;&#65039;</span>
                    <h3 id="screensModalTitle" style="margin: 0; font-size: 1rem; color: #f3f4f6; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 800px;">Contact Sheet Preview</h3>
                </div>
                <div style="display: flex; gap: 8px; align-items: center; flex-shrink: 0;">
                    <a id="screensModalRawBtn" href="#" target="_blank" rel="noopener noreferrer" class="flat-btn" style="padding: 4px 10px; font-size: 0.75rem;" title="Open raw image in new tab">&#8599; View Raw</a>
                    <a id="screensModalDownloadBtn" href="#" download class="flat-btn" style="padding: 4px 10px; font-size: 0.75rem;" title="Download image">&#128229; Download</a>
                    <button type="button" class="flat-btn" style="padding: 4px 10px; font-size: 0.75rem;" onclick="closeScreensModal()">✕ Close</button>
                </div>
            </div>
            <div style="flex: 1; overflow: auto; text-align: center; background: rgba(0,0,0,0.5); border-radius: 6px; padding: 8px;">
                <img id="screensModalImg" src="" alt="Video Contact Sheet" style="max-width: 100%; height: auto; border-radius: 4px; box-shadow: 0 4px 20px rgba(0,0,0,0.6);" loading="lazy">
            </div>
        </div>
    </div>

    <!-- Metadata & Tags Edit Modal (Exact 100% Explorer Replication) -->
    <div id="metadataModal" class="modal-overlay" onclick="if(event.target === this) closeMetadataModal()">
        <div class="modal">
            <div class="modal-header">
                <h3 style="color: #38bdf8; margin: 0; font-size: 1.1rem;">🏷️ Edit Metadata &amp; Tags</h3>
                <button type="button" onclick="closeMetadataModal()" class="ss-close-btn">&times;</button>
            </div>
            <div class="modal-body">
                <input type="hidden" id="editKey">
                <div class="form-group">
                    <label class="form-label">Video Title</label>
                    <input type="text" id="editTitle" class="form-input" placeholder="Clean video title">
                </div>
                <div style="display: flex; gap: 12px;">
                    <div class="form-group" style="flex: 1;">
                        <label class="form-label">Year</label>
                        <input type="text" id="editYear" class="form-input" placeholder="e.g. 2024">
                    </div>
                    <div class="form-group" style="flex: 1;">
                        <label class="form-label">Rating</label>
                        <input type="text" id="editRating" class="form-input" placeholder="e.g. 8.5">
                    </div>
                </div>
                <div class="form-group">
                    <label class="form-label">Categories (Comma-separated)</label>
                    <input type="text" id="editGenres" class="form-input" placeholder="Action, Drama, LGF 3">
                </div>
                <div class="form-group">
                    <label class="form-label">Tags (Comma-separated)</label>
                    <input type="text" id="editTags" class="form-input" placeholder="4K, 1080p, Short Form, H.264">
                </div>
                <div class="form-group">
                    <label class="form-label">Overview / Plot</label>
                    <textarea id="editOverview" class="form-input" rows="3" placeholder="Plot description..."></textarea>
                </div>
            </div>
            <div class="modal-actions">
                <button type="button" onclick="closeMetadataModal()" class="flat-btn">Cancel</button>
                <button type="button" onclick="saveMetadata()" class="flat-btn btn-accent" style="background: #2563eb; color: #fff; border-color: #3b82f6;">Save Changes</button>
            </div>
        </div>
    </div>

    <!-- Toast Notification -->
    <div id="discoverToast" class="discover-toast">Notification</div>

    <script>
        const allFiles = {files_json};
        const allAccounts = {accounts_json};
        const primaryAccount = {primary_acc_str};

        /* State Engine */
        let activeQuery = '';
        let activeSort = 'newest';
        let activeFilterTags = new Set();
        let filteredFiles = [...allFiles];
        let renderedCount = 0;
        const BATCH_SIZE_INITIAL = 24;
        const BATCH_SIZE_SUBSEQUENT = 16;
        let isLoadingMore = false;
        let markedForMergeKeys = new Set();
        let hiddenTagsSet = new Set();
        let userClosedPlayer = false;
        let isFloatingMini = false;
        let miniPlayerSize = 'm';
        let ytPlayerEventsBound = false;
        let isScrubDragging = false;
        let lastPosSaveTime = 0;
        let activeHoveredThumb = null;
        let thumbCycleInterval = null;
        let thumbManualScrubTimeout = null;
        let thumbSequenceIdx = 0;
        const THUMB_SAMPLE_FRAMES = [2, 6, 10, 14, 18, 22, 26, 30, 34, 38, 42, 46, 50, 54, 58, 62, 66, 70, 74, 78, 82, 86, 90, 94, 98];

        /* Storyboard Cache Registry */
        const storyboardCache = {{}};
        function registerStoryboard(safeId, key, version) {{
            if (!safeId) return;
            const vTag = version || Date.now();
            const entry = {{
                available: true,
                safe_id: safeId,
                key: key || '',
                cols: 10,
                rows: 10,
                thumb_width: 384,
                thumb_height: 216,
                total_frames: 100,
                vTag: vTag,
                sprite_url: `/api/storyboard/image?id=${{safeId}}&v=${{vTag}}`
            }};
            storyboardCache[safeId] = entry;
            if (key) storyboardCache[key] = entry;
        }}

        function syncStoryboardCache(filesList) {{
            (filesList || []).forEach(f => {{
                if ((f.has_storyboard || f.has_screens) && f.safe_id) {{
                    const v = f.sb_version || f.uploaded_at || Date.now();
                    registerStoryboard(f.safe_id, f.key, v);
                }}
            }});
        }}
        syncStoryboardCache(allFiles);

        /* Watch Later & History Stores */
        let watchLaterKeys = new Set(JSON.parse(localStorage.getItem('hfsync_discover_watch_later') || '[]'));
        let watchHistoryList = JSON.parse(localStorage.getItem('hfsync_discover_history') || '[]');

        function saveWatchLater() {{
            localStorage.setItem('hfsync_discover_watch_later', JSON.stringify([...watchLaterKeys]));
            updateWatchDrawerBadges();
        }}
        function saveWatchHistory() {{
            localStorage.setItem('hfsync_discover_history', JSON.stringify(watchHistoryList.slice(0, 80)));
            updateWatchDrawerBadges();
        }}
        function recordWatchHistory(file, timeSec) {{
            if (!file) return;
            watchHistoryList = watchHistoryList.filter(h => h.key !== file.key);
            watchHistoryList.unshift({{
                key: file.key,
                safe_id: file.safe_id || file.key,
                title: file.metadata?.title || file.key.split('/').pop(),
                duration_sec: file.duration_sec || 0,
                duration_str: file.duration_str || '',
                time_sec: Math.round(timeSec),
                watched_at: new Date().toLocaleTimeString([], {{ hour: '2-digit', minute: '2-digit' }})
            }});
            saveWatchHistory();
        }}

        /* Toast helper */
        let toastTimeout = null;
        function showToast(msg) {{
            const toast = document.getElementById('discoverToast');
            if (!toast) return;
            toast.textContent = msg;
            toast.classList.add('show');
            clearTimeout(toastTimeout);
            toastTimeout = setTimeout(() => toast.classList.remove('show'), 2400);
        }}

        /* Format Helpers */
        function escapeHtml(str) {{
            return String(str || '').replace(/[&<>"']/g, function(m) {{
                return {{ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }}[m];
            }});
        }}

        function formatTime(sec) {{
            if (!sec || isNaN(sec)) return '0:00';
            const s = Math.floor(sec);
            const m = Math.floor(s / 60);
            const remS = s % 60;
            const h = Math.floor(m / 60);
            const remM = m % 60;
            if (h > 0) {{
                return `${{h}}:${{remM.toString().padStart(2, '0')}}:${{remS.toString().padStart(2, '0')}}`;
            }}
            return `${{m}}:${{remS.toString().padStart(2, '0')}}`;
        }}

        function cleanTitleExtension(filename) {{
            if (!filename) return '';
            let s = String(filename).trim();
            if (s.toLowerCase().endsWith('.encname')) return s;
            return s.replace(/\.(mp4|mkv|avi|mov|flv|webm|ts|m4a|wmv|strm)$/i, '');
        }}

        /* Resume Position Storage */
        function getSavedVideoPosition(safeId) {{
            if (!safeId) return null;
            try {{
                const raw = localStorage.getItem('hfsync_pos_' + safeId);
                return raw ? JSON.parse(raw) : null;
            }} catch (e) {{ return null; }}
        }}
        function saveVideoPosition(safeId, time, duration) {{
            if (!safeId || time <= 0) return;
            try {{
                localStorage.setItem('hfsync_pos_' + safeId, JSON.stringify({{ time, duration, updated: Date.now() }}));
            }} catch (e) {{}}
        }}
        function clearVideoPosition(safeId) {{
            if (!safeId) return;
            try {{ localStorage.removeItem('hfsync_pos_' + safeId); }} catch (e) {{}}
        }}

        /* =========================================================================
           100% EXACT EXPLORER CARD BADGES HTML (renderCardBadgesHtml)
           ========================================================================= */
        function renderCardBadgesHtml(f) {{
            const meta = f.metadata || {{}};
            const genres = meta.genres || [];
            const tags = meta.tags || [];

            let badgesHtml = '';
            const accountOwner = f.account || primaryAccount;
            const isPrimary = (accountOwner === primaryAccount);
            badgesHtml += `<span class="account-pill ${{isPrimary ? 'is-primary' : ''}}" onclick="event.stopPropagation(); addActiveFilter('acc', '${{escapeHtml(accountOwner)}}')" title="Click to filter by account: ${{escapeHtml(accountOwner)}}">${{isPrimary ? '&#128081;' : '&#128100;'}} ${{escapeHtml(accountOwner)}}</span>`;

            if (f.cache_pct && f.cache_pct > 0) {{
                const pct = Math.round(f.cache_pct);
                const isFull = pct >= 99;
                badgesHtml += `<span class="meta-pill" title="Encrypted Local Cache: ${{pct}}% cached on disk">&#128190; ${{isFull ? 'Cached' : pct + '% Cached'}}</span>`;
            }}

            if (meta.year) badgesHtml += `<span class="meta-pill">&#128197; ${{escapeHtml(meta.year)}}</span>`;
            if (meta.rating) badgesHtml += `<span class="meta-pill">&#11088; ${{escapeHtml(meta.rating)}}</span>`;

            genres.forEach(g => {{
                badgesHtml += `<span class="meta-pill genre-pill" onclick="event.stopPropagation(); addActiveFilter('cat', '${{escapeHtml(g)}}')" title="Category: ${{escapeHtml(g)}} (Click to filter)">&#128193; CAT: ${{escapeHtml(g)}}</span>`;
            }});

            tags.forEach(t => {{
                const tl = String(t).trim().toLowerCase();
                let icon = '&#127991;';
                if (['4k', '2160p', 'uhd'].includes(tl)) icon = '&#10024;';
                else if (['1080p', 'full hd', 'fhd', '720p', 'hd'].includes(tl)) icon = '&#128250;';
                else if (['h.264', 'h.265', 'av1', 'hevc'].includes(tl)) icon = '&#127916;';
                else if (tl.includes('< 1m') || tl.includes('1-5m') || tl.includes('5-15m') || tl.includes('short')) icon = '&#9201;';
                badgesHtml += `<span class="meta-pill tag-pill" onclick="event.stopPropagation(); addActiveFilter('tag', '${{escapeHtml(t)}}')" title="Tag: ${{escapeHtml(t)}} (Click to filter)">${{icon}} ${{escapeHtml(t)}}</span>`;
            }});

            const subs = f.subtitles || [];
            if (subs.length > 0) {{
                const subLangs = subs.map(s => (s.lang || 'cc').toUpperCase()).join(', ');
                badgesHtml += `<span class="meta-pill sub-pill" title="Subtitles: ${{escapeHtml(subLangs)}}">&#128172; ${{escapeHtml(subLangs)}}</span>`;
            }}

            return badgesHtml;
        }}

        /* =========================================================================
           100% EXACT EXPLORER CARD RENDERER (renderCard)
           ========================================================================= */
        function renderCard(f, nodeName) {{
            const meta = f.metadata || {{}};
            const title = meta.title || nodeName;
            const badgesHtml = renderCardBadgesHtml(f);

            const isEnc = nodeName.toLowerCase().endsWith('.encname');
            const cleanNode = isEnc ? nodeName.trim().toLowerCase() : nodeName.replace(/\\.[^/.]+$/, '').trim().toLowerCase();
            const cleanTitle = title.trim().toLowerCase();
            const strippedNode = cleanNode.replace(/\\s*\\(\\d+\\)$/, '').trim();
            const showSubtitle = (cleanNode !== cleanTitle && strippedNode !== cleanTitle && nodeName.trim().toLowerCase() !== cleanTitle);
            const subtitleHtml = showSubtitle ? `<div class="video-card-sub" title="${{escapeHtml(nodeName)}}">${{escapeHtml(nodeName)}}</div>` : '';

            const ov = (meta.overview || "").trim();
            const showOv = (ov && !ov.startsWith('Encrypted vault stream:') && ov.toLowerCase() !== cleanTitle);
            const overviewHtml = showOv ? `<div class="card-overview-snippet" title="${{escapeHtml(ov)}}"><span style="color: #38bdf8; font-style: normal; margin-right: 4px;">&#128221; Plot:</span>${{escapeHtml(ov)}}</div>` : '';

            const safeId = f.safe_id || '';
            const hasStoryboard = !!f.has_storyboard;
            const hasScreens = !!f.has_screens;
            const canViewScreens = hasScreens || hasStoryboard;
            let thumbBgStyle = '';
            let fallbackHtml = '';
            if ((hasStoryboard || hasScreens) && safeId) {{
                const vTag = f.sb_version || (f.uploaded_at ? encodeURIComponent(f.uploaded_at) : '1');
                thumbBgStyle = `background-image: url('/api/screens/thumb?id=${{safeId}}&v=${{vTag}}'); background-size: cover; background-position: center;`;
            }} else {{
                fallbackHtml = '<div class="thumb-fallback"><span class="thumb-fallback-icon">&#127916;</span><span class="thumb-fallback-txt">PREVIEW</span></div>';
            }}

            const durationStr = f.duration_str || (f.duration_sec ? formatTime(f.duration_sec) : '');
            const durationHtml = durationStr ? `<div class="card-duration-badge">${{escapeHtml(durationStr)}}</div>` : '';

            const savedPos = getSavedVideoPosition(safeId);
            let resumeBarHtml = '';
            let resumePillHtml = '';
            if (savedPos && savedPos.time > 5 && savedPos.duration > 0) {{
                const pct = Math.min(100, Math.round((savedPos.time / savedPos.duration) * 100));
                resumeBarHtml = `<div class="card-resume-bar" style="width: ${{pct}}%;"></div>`;
                resumePillHtml = `<div class="card-resume-badge">⏱️ ${{formatTime(savedPos.time)}}</div>`;
            }}

            const audioSizeStr = f.audio_size_str || 'Audio';
            const cachePct = Math.round(f.cache_pct || 0);
            const cachedSizeStr = f.cached_size_str || '0 B';
            const isPinned = !!f.pinned_offline;
            const isDownloading = !!f.is_downloading;
            const isFullyOffline = isPinned && cachePct >= 99.5;

            let cachePillHtml = '';
            if (isFullyOffline) {{
                cachePillHtml = `<div class="card-cache-pill is-pinned" id="cachePill_${{safeId}}"><span class="cache-pill-icon">📌</span> Offline Ready</div>`;
            }} else if (isDownloading) {{
                cachePillHtml = `<div class="card-cache-pill is-downloading" id="cachePill_${{safeId}}">⏳ Downloading ${{cachePct}}%</div>`;
            }} else if (cachePct > 0) {{
                cachePillHtml = `<div class="card-cache-pill is-cached" id="cachePill_${{safeId}}">💾 ${{cachePct}}% Cached</div>`;
            }} else {{
                cachePillHtml = `<div class="card-cache-pill is-cloud" id="cachePill_${{safeId}}">☁️ Cloud Stream</div>`;
            }}

            const cacheTrackHtml = `<div class="thumb-cache-track" title="Local Cache: ${{cachePct}}%"><div class="thumb-cache-fill" id="cacheFill_${{safeId}}" style="width: ${{Math.min(100, cachePct)}}%;"></div></div>`;
            const isMarked = markedForMergeKeys.has(f.key);

            return `
            <div class="video-card ${{isMarked ? 'is-marked-for-merge' : ''}}" data-key="${{escapeHtml(f.key)}}" id="card_${{safeId}}">
                <!-- 16:9 Thumbnail Box with Moving Preview & In-Thumbnail SS Overlay -->
                <div class="video-card-thumb" 
                     data-key="${{escapeHtml(f.key)}}" 
                     data-safeid="${{safeId}}"
                     data-duration="${{f.duration_sec || 0}}"
                     onclick="handleCardThumbClick(this, event)"
                     onmouseenter="handleThumbEnter(this)"
                     onmouseleave="handleThumbLeave(this)"
                     onmousemove="handleThumbMove(this, event)"
                     title="Click to play / resume (click bottom blue line to seek)">
                    
                    <div class="thumb-canvas" id="thumb-canvas-${{safeId}}" style="${{thumbBgStyle}}">
                        ${{fallbackHtml}}
                    </div>
                    <div class="thumb-screens-overlay" id="screens-overlay-${{safeId}}"></div>
                    <div class="thumb-scrub-bar" title="Click blue scrub bar to seek">
                        <div class="thumb-scrub-fill"></div>
                    </div>
                    <div class="thumb-overlay-play">
                        <svg viewBox="0 0 24 24" fill="currentColor" width="24" height="24"><path d="M8 5v14l11-7z"/></svg>
                    </div>

                    ${{canViewScreens ? `<button type="button" class="thumb-screens-btn" onmouseenter="handleScreensBtnEnter(this, event)" onmousemove="event.stopPropagation()" onmouseleave="handleScreensBtnLeave(this, event)" onclick="openScreensModal('${{safeId}}', '${{escapeHtml(title)}}', event)" title="Hover to view screens &bull; Click for high-res sheet">&#128444;&#65039; Screens</button>` : ''}}

                    <div class="card-top-badges">
                        ${{cachePillHtml}}
                        <div class="card-size-badge" title="Video size: ${{escapeHtml(f.size_str)}} | Audio: ${{escapeHtml(audioSizeStr)}}">
                            <span class="badge-vsize">💾 ${{escapeHtml(f.size_str)}}</span>
                            <span class="badge-sep">•</span>
                            <span class="badge-asize">🎵 ${{escapeHtml(audioSizeStr)}}</span>
                        </div>
                    </div>

                    ${{cacheTrackHtml}}
                    ${{resumePillHtml}}
                    ${{durationHtml}}
                    ${{resumeBarHtml}}

                    <!-- In-Thumbnail Screenshot (SS) Overlay: 0% Card Height Push -->
                    <div class="card-ss-overlay" id="ssOverlay_${{safeId}}" onclick="event.stopPropagation()">
                        <div class="ss-overlay-header">
                            <span>🖼️ Preview Screenshots</span>
                            <button type="button" class="ss-close-btn" onclick="toggleCardSS('${{safeId}}', event)">&times;</button>
                        </div>
                        <div class="ss-filmstrip-scroll" id="ssStrip_${{safeId}}">
                            <span style="font-size: 0.72rem; color: #71717a; padding: 10px;">Loading screenshots...</span>
                        </div>
                    </div>
                </div>
                
                <div class="video-card-body">
                    <div class="video-card-title-row">
                        <div class="video-card-title" onclick="playVideo('/stream/${{escapeHtml(f.key)}}', '${{escapeHtml(title)}}', '${{escapeHtml(f.blob_path || '')}}')" title="Click to play: ${{escapeHtml(title)}}">
                            ${{escapeHtml(title)}}
                        </div>
                        <div class="video-card-title-actions">
                            <button type="button" class="card-offline-tag-btn ${{isFullyOffline ? 'active' : ''}}" id="tagOffline_${{safeId}}" onclick="toggleMakeOffline('${{safeId}}', event)" title="${{isFullyOffline ? '📌 Offline Ready. Click to unpin.' : '📥 Make offline available (Download locally)'}}">
                                ${{isFullyOffline ? '📌' : '📥'}}
                            </button>
                            <button type="button" class="card-action-arrow-btn" id="btnSS_${{safeId}}" onclick="toggleCardSS('${{safeId}}', event)" title="View in-thumbnail screenshots gallery">
                                <span class="card-arrow-icon">&#9662;</span>
                            </button>
                        </div>
                    </div>
                    ${{subtitleHtml}}
                    ${{overviewHtml}}
                    <div class="video-card-badges">
                        ${{badgesHtml}}
                    </div>
                </div>

                <div class="video-card-actions is-open">
                    <div class="card-action-group-left">
                        <button type="button" class="card-icon-btn btn-card-offline" id="btnOffline_${{safeId}}" onclick="toggleMakeOffline('${{safeId}}', event)" title="Toggle Offline Availability">
                            ${{isFullyOffline ? '📌' : '📥'}}
                        </button>
                        <a href="/stream/${{escapeHtml(f.key)}}" target="_blank" class="card-icon-btn btn-card-link" title="Direct Stream URL">&#128279;</a>
                        ${{canViewScreens ? `<button type="button" class="card-icon-btn" onclick="openScreensModal('${{safeId}}', '${{escapeHtml(title)}}', event)" title="High-Res Contact Sheet">&#128444;&#65039;</button>` : ''}}
                        <a href="/stream/audio/${{escapeHtml(f.key)}}" download="${{escapeHtml(title)}}.m4a" class="card-icon-btn" title="Download Audio (.m4a)">&#127925;</a>
                        <button type="button" class="card-icon-btn" onclick="toggleWatchLater('${{escapeHtml(f.key)}}', event)" title="Add to Watch Later">⏱️</button>
                    </div>
                    <div class="card-action-group-right">
                        <button onclick="promptEditMetadata('${{escapeHtml(f.key)}}')" class="card-icon-btn btn-card-edit" title="Edit Metadata &amp; Tags">&#127991;</button>
                    </div>
                </div>
            </div>`;
        }}

        /* =========================================================================
           100% EXACT MOVING THUMBNAIL PREVIEW ENGINE
           ========================================================================= */
        function setThumbCardFrame(card, frameIndex) {{
            if (!card) return;
            const canvas = card.querySelector('.thumb-canvas');
            if (!canvas) return;
            
            const w = Math.round(canvas.clientWidth || card.clientWidth || 240);
            const h = Math.round(canvas.clientHeight || card.clientHeight || 135);
            const col = frameIndex % 10;
            const row = Math.floor(frameIndex / 10);
            
            canvas.style.backgroundSize = `${{10 * w}}px ${{10 * h}}px`;
            canvas.style.backgroundPosition = `-${{col * w}}px -${{row * h}}px`;
            
            const fill = card.querySelector('.thumb-scrub-fill');
            if (fill) fill.style.width = `${{((frameIndex + 1) / 100) * 100}}%`;
        }}

        function startThumbAutoCycle(card, startFrame = 0) {{
            clearInterval(thumbCycleInterval);
            let currentFrame = Math.max(0, Math.min(99, Math.floor(startFrame || 0)));
            
            thumbCycleInterval = setInterval(() => {{
                if (!activeHoveredThumb || activeHoveredThumb !== card) {{
                    clearInterval(thumbCycleInterval);
                    return;
                }}
                setThumbCardFrame(card, currentFrame);
                currentFrame = (currentFrame >= 99) ? 0 : currentFrame + 1;
            }}, 200); // 5 fps: 1000ms / 5 = 200ms per frame (smooth cinematic trailer pace without flashing)
        }}

        function applyStoryboardToCard(card, safeId, spriteUrl, vTag) {{
            if (!card) return;
            const canvas = card.querySelector('.thumb-canvas');
            if (canvas) {{
                const thumbUrl = `/api/screens/thumb?id=${{safeId}}&v=${{vTag || Date.now()}}`;
                canvas.dataset.staticBg = `url('${{thumbUrl}}')`;
                const fb = card.querySelector('.thumb-fallback');
                if (fb) fb.remove();
            }}
        }}

        async function handleThumbEnter(card) {{
            activeHoveredThumb = card;
            const key = card.dataset.key;
            const safeId = card.dataset.safeid;
            const canvas = card.querySelector('.thumb-canvas');
            if (!canvas) return;

            if (!canvas.dataset.staticBg || canvas.dataset.staticBg === 'none' || canvas.dataset.staticBg === '') {{
                canvas.dataset.staticBg = canvas.style.backgroundImage || `url('/api/screens/thumb?id=${{safeId}}')`;
            }}

            const sb = storyboardCache[safeId] || storyboardCache[key];
            if (sb && sb.available && sb.sprite_url) {{
                canvas.style.backgroundImage = `url('${{sb.sprite_url}}')`;
                const fb = card.querySelector('.thumb-fallback');
                if (fb) fb.remove();
                startThumbAutoCycle(card, 0);
                return;
            }}

            try {{
                const res = await fetch(`/api/storyboard?key=${{encodeURIComponent(key)}}`);
                if (!res.ok) return;
                const data = await res.json();
                if (data.available && data.sprite_url) {{
                    registerStoryboard(data.safe_id || safeId, key);
                    applyStoryboardToCard(card, safeId, data.sprite_url);
                    if (activeHoveredThumb === card) {{
                        canvas.style.backgroundImage = `url('${{data.sprite_url}}')`;
                        startThumbAutoCycle(card, 0);
                    }}
                }}
            }} catch (err) {{}}
        }}

        function handleThumbMove(card, event) {{
            if (!card) return;
            if (card.dataset.screensHover === '1') return;
            const canvas = card.querySelector('.thumb-canvas');
            if (!canvas) return;

            const safeId = card.dataset.safeid;
            const key = card.dataset.key;
            const sb = storyboardCache[safeId] || storyboardCache[key];
            const spriteUrl = (sb && sb.sprite_url) ? sb.sprite_url : `/api/storyboard/image?id=${{safeId}}`;

            if (safeId && (!canvas.style.backgroundImage || !canvas.style.backgroundImage.includes('/api/storyboard/image'))) {{
                canvas.style.backgroundImage = `url('${{spriteUrl}}')`;
            }}

            clearInterval(thumbCycleInterval);
            clearTimeout(thumbManualScrubTimeout);

            const rect = card.getBoundingClientRect();
            const pct = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
            const frameIndex = Math.min(99, Math.floor(pct * 100));
            
            card.dataset.lastScrubPct = pct;
            setThumbCardFrame(card, frameIndex);

            thumbManualScrubTimeout = setTimeout(() => {{
                if (activeHoveredThumb === card) {{
                    startThumbAutoCycle(card, frameIndex);
                }}
            }}, 600);
        }}

        function handleThumbLeave(card) {{
            clearInterval(thumbCycleInterval);
            clearTimeout(thumbManualScrubTimeout);
            if (activeHoveredThumb === card) {{
                activeHoveredThumb = null;
            }}
            const canvas = card.querySelector('.thumb-canvas');
            if (canvas) {{
                if (canvas.dataset.staticBg && canvas.dataset.staticBg !== 'none') {{
                    canvas.style.backgroundImage = canvas.dataset.staticBg;
                }} else {{
                    const safeId = card.dataset.safeid;
                    if (safeId) canvas.style.backgroundImage = `url('/api/screens/thumb?id=${{safeId}}')`;
                }}
                canvas.style.backgroundSize = 'cover';
                canvas.style.backgroundPosition = 'center';
            }}
            const fill = card.querySelector('.thumb-scrub-fill');
            if (fill) fill.style.width = '0%';
        }}

        function handleScreensBtnEnter(btn, event) {{
            if (event) event.stopPropagation();
            const wrap = btn.closest('.video-card-thumb');
            if (!wrap) return;
            const safeId = wrap.dataset.safeid;
            wrap.dataset.screensHover = '1';
            clearInterval(thumbCycleInterval);

            const overlay = wrap.querySelector('.thumb-screens-overlay');
            if (!overlay) return;

            const fileObj = (allFiles || []).find(f => f.safe_id === safeId);
            const sb = storyboardCache[safeId];
            const url = (sb && sb.sprite_url) ? sb.sprite_url : `/api/screens/image?id=${{encodeURIComponent(safeId)}}`;
            overlay.style.backgroundImage = `url('${{url}}')`;
            overlay.style.display = 'block';
        }}

        function handleScreensBtnLeave(btn, event) {{
            if (event) event.stopPropagation();
            const wrap = btn.closest('.video-card-thumb');
            if (!wrap) return;
            wrap.dataset.screensHover = '';
            const overlay = wrap.querySelector('.thumb-screens-overlay');
            if (overlay) overlay.style.display = 'none';
        }}

        function handleCardThumbClick(card, event) {{
            if (!card) return;
            if (event && (event.target.closest('button') || event.target.closest('a'))) return;
            const key = card.dataset.key;
            const safeId = card.dataset.safeid;
            const fileObj = (allFiles || []).find(f => f.key === key || f.safe_id === safeId);
            const title = fileObj ? (fileObj.metadata?.title || key) : key;
            const blobPath = fileObj ? (fileObj.blob_path || '') : '';
            const dur = fileObj ? (fileObj.duration_sec || 0) : 0;

            const rect = card.getBoundingClientRect();
            const clickedOnScrubBar = Boolean(event && event.target && event.target.closest && event.target.closest('.thumb-scrub-bar'))
                || (event && typeof event.clientY === 'number' && (event.clientY >= rect.bottom - 18));

            let seekTime = 0;
            const savedPos = getSavedVideoPosition(safeId);

            if (clickedOnScrubBar && event && typeof event.clientX === 'number') {{
                const pct = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
                seekTime = Math.max(0, Math.floor(pct * dur));
            }} else if (savedPos && savedPos.time > 5) {{
                seekTime = savedPos.time;
            }}
            playVideo('/stream/' + key, title, blobPath, seekTime);
        }}

        /* In-Thumbnail Screenshot (SS) Overlay Toggle */
        function toggleCardSS(safeId, event) {{
            if (event) event.stopPropagation();
            const overlay = document.getElementById(`ssOverlay_${{safeId}}`);
            const btn = document.getElementById(`btnSS_${{safeId}}`);
            if (!overlay) return;

            const isOpen = overlay.classList.contains('is-open');
            if (isOpen) {{
                overlay.classList.remove('is-open');
                if (btn) btn.classList.remove('is-active');
            }} else {{
                document.querySelectorAll('.card-ss-overlay.is-open').forEach(el => el.classList.remove('is-open'));
                document.querySelectorAll('.card-action-arrow-btn.is-active').forEach(b => b.classList.remove('is-active'));

                overlay.classList.add('is-open');
                if (btn) btn.classList.add('is-active');
                loadScreenshotFrames(safeId);
            }}
        }}

        async function loadScreenshotFrames(safeId) {{
            const strip = document.getElementById(`ssStrip_${{safeId}}`);
            if (!strip || strip.dataset.loaded === 'true') return;

            const fileObj = (allFiles || []).find(f => f.safe_id === safeId);
            const fileKey = fileObj ? fileObj.key : '';

            try {{
                const res = await fetch(`/api/storyboard?key=${{encodeURIComponent(fileKey)}}&id=${{encodeURIComponent(safeId)}}`);
                if (!res.ok) throw new Error('Not available');
                const data = await res.json();
                if (!data.available || !data.sprite_url) throw new Error('Storyboard not generated');

                const totalFrames = data.total_frames || 100;
                const dur = data.duration || (fileObj ? (fileObj.duration_sec || 60) : 60);
                const step = dur / totalFrames;

                // Pick ~25 evenly spaced frames from 0 to totalFrames - 1
                const frameIndices = [];
                const sampleCount = 25;
                for (let s = 0; s < sampleCount; s++) {{
                    const idx = Math.min(totalFrames - 1, Math.round((s / (sampleCount - 1)) * (totalFrames - 1)));
                    if (!frameIndices.includes(idx)) frameIndices.push(idx);
                }}

                let html = '';
                frameIndices.forEach(i => {{
                    const frameSec = i * step;
                    const col = i % 10;
                    const row = Math.floor(i / 10);
                    const posX = (col / 9) * 100;
                    const posY = (row / 9) * 100;
                    html += `
                    <div class="ss-frame-item" 
                         style="background-image: url('${{data.sprite_url}}'); background-size: 1000% 1000%; background-position: ${{posX}}% ${{posY}}%;"
                         onclick="playVideoWithSeek('${{safeId}}', ${{Math.round(frameSec)}})"
                         title="Play from ${{formatTime(frameSec)}}">
                        <span class="ss-frame-time">${{formatTime(frameSec)}}</span>
                    </div>`;
                }});
                strip.innerHTML = html || '<span style="font-size: 0.72rem; color: #71717a; padding: 10px;">No preview frames available.</span>';
                strip.dataset.loaded = 'true';
            }} catch (e) {{
                if (fileObj && (fileObj.has_screens || fileObj.screens_url)) {{
                    strip.innerHTML = `
                    <div style="display: flex; align-items: center; justify-content: center; width: 100%; height: 100%; padding: 8px;">
                        <button type="button" class="btn-card-action" onclick="openScreensModal('${{safeId}}', '${{escapeHtml(fileObj.metadata?.title || fileObj.key)}}', event)" style="background: rgba(56,189,248,0.15); border: 1px solid rgba(56,189,248,0.4); color: #38bdf8; padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 0.75rem; font-weight: 500;">
                            🖼️ View Full Contact Sheet Lightbox
                        </button>
                    </div>`;
                }} else {{
                    strip.innerHTML = `
                    <div style="display: flex; flex-direction: column; align-items: center; justify-content: center; width: 100%; height: 100%; padding: 10px; color: #a1a1aa; font-size: 0.75rem; text-align: center;">
                        <span>⏳ Generating previews...</span>
                        <span style="font-size: 0.68rem; color: #71717a; margin-top: 4px;">Hover card to prioritize storyboard</span>
                    </div>`;
                }}
            }}
        }}

        /* =========================================================================
           100% EXACT EXPLORER YOUTUBE PLAYER CONTROLLER (playVideo)
           ========================================================================= */
        let currentBlobId = "";
        let currentVideoKey = "";
        let currentPlayingSafeId = "";
        let activeStoryboard = null;

        function playVideo(url, title, blobPath, startTime) {{
            const box = document.getElementById('playerBox');
            const player = document.getElementById('videoPlayer');
            const playerTitle = document.getElementById('playerTitle');
            const container = document.getElementById('ytContainer');

            if (player) {{
                try {{
                    player.pause();
                    player.removeAttribute('src');
                    player.load();
                }} catch(e) {{}}
            }}

            userClosedPlayer = false;
            currentBlobId = blobPath || url;
            let rawKey = String(url || '');
            if (rawKey.includes('/stream/play/')) rawKey = rawKey.substring(rawKey.indexOf('/stream/play/') + 13);
            else if (rawKey.includes('/stream/')) rawKey = rawKey.substring(rawKey.indexOf('/stream/') + 8);
            try {{ rawKey = decodeURIComponent(rawKey); }} catch (e) {{}}
            
            const fileObj = (allFiles || []).find(f => f.key === rawKey || f.safe_id === rawKey || (f.file_names && f.file_names.includes(rawKey)));
            currentVideoKey = fileObj ? fileObj.key : rawKey;
            const safeId = fileObj ? fileObj.safe_id : (currentPlayingSafeId || currentVideoKey);
            currentPlayingSafeId = safeId;

            playerTitle.textContent = title || "Decrypted Stream";
            box.style.display = 'block';

            const isFileH265 = (fileObj && fileObj.media_info && String(fileObj.media_info.video_codec || '').toLowerCase().includes('265')) ||
                               (fileObj && (fileObj.tags || []).includes('H.265'));
            const initialStreamUrl = isFileH265 ? ('/stream/transcode/' + encodeURIComponent(safeId)) : ('/stream/play/' + encodeURIComponent(safeId));

            // Use /stream/play/ or /stream/transcode/ (avoids IDM capture)
            player.onerror = function() {{
                if (player.src && player.src.includes('/stream/play/') && currentVideoKey) {{
                    player.src = '/stream/' + encodeURIComponent(currentVideoKey);
                    player.play().catch(() => {{}});
                }}
            }};
            player.src = initialStreamUrl;

            if (window._h265CheckTimer) clearTimeout(window._h265CheckTimer);
            const onPlayH265Check = function() {{
                if (window._h265CheckTimer) clearTimeout(window._h265CheckTimer);
                window._h265CheckTimer = setTimeout(() => {{
                    if (!player.paused && player.videoWidth === 0 && !player.src.includes('/stream/transcode/')) {{
                        console.warn("H.265 video decoding unsupported in browser. Auto-switching to Live H.264 Transcode...");
                        const curTime = player.currentTime || 0;
                        player.src = '/stream/transcode/' + encodeURIComponent(safeId) + (curTime > 2 ? '?t=' + Math.floor(curTime) : '');
                        player.play().catch(() => {{}});
                    }}
                }}, 1300);
            }};
            player.removeEventListener('playing', player._h265CheckHandler);
            player._h265CheckHandler = onPlayH265Check;
            player.addEventListener('playing', onPlayH265Check);

            let targetSeek = 0;
            if (typeof startTime === 'number' && startTime > 0) {{
                targetSeek = startTime;
            }} else {{
                const savedPos = getSavedVideoPosition(safeId);
                if (savedPos && savedPos.time > 5) targetSeek = savedPos.time;
            }}

            if (targetSeek > 0) {{
                let seekDone = false;
                const applySeek = () => {{
                    if (seekDone) return;
                    try {{
                        player.currentTime = targetSeek;
                        seekDone = true;
                    }} catch (err) {{}}
                }};
                if (player.readyState >= 1) applySeek();
                else {{
                    player.addEventListener('loadedmetadata', applySeek, {{ once: true }});
                    player.addEventListener('canplay', applySeek, {{ once: true }});
                }}
            }}

            player.play().catch(() => {{}});
            initYtPlayerEvents();
            loadStoryboard(url, player.duration);
            initFloatingScrollObserver();
            initMiniPlayerDraggable();

            // Smooth scroll up to player if user clicked card from top
            if (window.scrollY < 300) {{
                window.scrollTo({{ top: 0, behavior: 'smooth' }});
            }}
        }}

        function playVideoWithSeek(safeId, seekSec) {{
            const f = allFiles.find(item => item.safe_id === safeId);
            if (!f) return;
            playVideo('/stream/' + f.key, f.metadata?.title || f.key, f.blob_path || '', seekSec);
        }}

        function closePlayer() {{
            const closingKey = currentVideoKey;
            const closingSafeId = currentPlayingSafeId;
            if (closingSafeId) captureCurrentVideoPosition();
            userClosedPlayer = true;
            deactivateFloatingMiniPlayer();

            const player = document.getElementById('videoPlayer');
            const box = document.getElementById('playerBox');
            if (player) {{
                try {{
                    player.pause();
                    player.removeAttribute('src');
                    player.load();
                }} catch(e) {{}}
            }}
            if (box) box.style.display = 'none';

            if (closingSafeId) {{
                fetch('/api/cache/stop_playback', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ safe_id: closingSafeId, key: closingKey }})
                }}).catch(() => {{}});
            }}
        }}

        function captureCurrentVideoPosition() {{
            const player = document.getElementById('videoPlayer');
            const safeId = currentPlayingSafeId;
            if (!player || !safeId || isNaN(player.currentTime) || player.currentTime <= 0) return;
            saveVideoPosition(safeId, player.currentTime, player.duration || 0);
            const fileObj = (allFiles || []).find(f => f.safe_id === safeId);
            if (fileObj) recordWatchHistory(fileObj, player.currentTime);
        }}

        function togglePlayPause() {{
            const player = document.getElementById('videoPlayer');
            if (!player) return;
            if (player.paused) player.play();
            else player.pause();
            flashCenterFeedback(player.paused ? '⏸' : '▶');
        }}

        function seekRelative(sec) {{
            const player = document.getElementById('videoPlayer');
            if (!player || !player.duration) return;
            player.currentTime = Math.max(0, Math.min(player.duration, player.currentTime + sec));
            flashCenterFeedback(sec > 0 ? `+${{sec}}s` : `${{sec}}s`);
        }}

        function toggleMute() {{
            const player = document.getElementById('videoPlayer');
            if (!player) return;
            player.muted = !player.muted;
            updateVolumeUI();
        }}

        function updateVolumeUI() {{
            const player = document.getElementById('videoPlayer');
            const volHigh = document.getElementById('ytVolHigh');
            const volMute = document.getElementById('ytVolMute');
            const slider = document.getElementById('ytVolumeSlider');
            if (!player) return;
            const isMuted = player.muted || player.volume === 0;
            if (volHigh) volHigh.style.display = isMuted ? 'none' : 'block';
            if (volMute) volMute.style.display = isMuted ? 'block' : 'none';
            if (slider) {{
                const sliderVal = isMuted ? 0 : Math.sqrt(player.volume);
                slider.value = sliderVal;
                const pct = Math.round(sliderVal * 100);
                slider.style.background = `linear-gradient(to right, #38bdf8 0%, #38bdf8 ${{pct}}%, rgba(255,255,255,0.25) ${{pct}}%, rgba(255,255,255,0.25) 100%)`;
            }}
        }}

        function setSpeed(rate) {{
            const player = document.getElementById('videoPlayer');
            const btn = document.getElementById('ytSpeedBtn');
            if (player) player.playbackRate = rate;
            if (btn) btn.textContent = rate === 1 ? '1x' : `${{rate}}x`;
            document.querySelectorAll('.yt-speed-option').forEach(opt => {{
                opt.classList.toggle('active', opt.textContent.trim() === (rate === 1 ? 'Normal' : `${{rate}}x`));
            }});
            const menu = document.getElementById('ytSpeedMenu');
            if (menu) menu.style.display = 'none';
            flashCenterFeedback(`${{rate}}x Speed`);
        }}

        function toggleSpeedMenu() {{
            const menu = document.getElementById('ytSpeedMenu');
            if (menu) menu.style.display = menu.style.display === 'block' ? 'none' : 'block';
        }}

        function toggleFullscreen() {{
            const container = document.getElementById('ytContainer');
            if (!container) return;
            if (!document.fullscreenElement) container.requestFullscreen().catch(() => {{}});
            else document.exitFullscreen().catch(() => {{}});
        }}

        function toggleTheaterMode() {{
            const box = document.getElementById('playerBox');
            if (box) box.classList.toggle('theater-mode');
        }}

        function togglePiP() {{
            const player = document.getElementById('videoPlayer');
            if (!player) return;
            if (document.pictureInPictureElement) document.exitPictureInPicture().catch(() => {{}});
            else if (player.requestPictureInPicture) player.requestPictureInPicture().catch(() => {{}});
        }}

        function toggleStatsForNerds() {{
            const panel = document.getElementById('ytStatsPanel');
            if (panel) panel.style.display = panel.style.display === 'block' ? 'none' : 'block';
        }}

        function flashCenterFeedback(icon) {{
            const fb = document.getElementById('ytCenterFeedback');
            if (!fb) return;
            fb.textContent = icon;
            fb.classList.remove('show');
            void fb.offsetWidth;
            fb.classList.add('show');
            setTimeout(() => fb.classList.remove('show'), 400);
        }}

        /* Floating Mini Player (Auto on scroll & S/M/L sizes) */
        function activateFloatingMiniPlayer() {{
            const container = document.getElementById('ytContainer');
            if (!container || isFloatingMini) return;
            isFloatingMini = true;
            container.classList.add('is-floating-mini');
            setMiniPlayerSize(miniPlayerSize);
        }}

        function deactivateFloatingMiniPlayer() {{
            const container = document.getElementById('ytContainer');
            if (!container || !isFloatingMini) return;
            isFloatingMini = false;
            container.classList.remove('is-floating-mini', 'mini-size-s', 'mini-size-m', 'mini-size-l');
            container.style.transform = '';
            container.style.left = '';
            container.style.top = '';
        }}

        function expandFromFloatingMini() {{
            deactivateFloatingMiniPlayer();
            window.scrollTo({{ top: 0, behavior: 'smooth' }});
        }}

        function setMiniPlayerSize(size, event) {{
            if (event) event.stopPropagation();
            miniPlayerSize = size;
            const container = document.getElementById('ytContainer');
            if (!container) return;
            container.classList.remove('mini-size-s', 'mini-size-m', 'mini-size-l');
            container.classList.add(`mini-size-${{size}}`);
            document.querySelectorAll('.yt-mini-size-btn').forEach(b => {{
                b.classList.toggle('active', b.getAttribute('data-size') === size);
            }});
        }}

        function initFloatingScrollObserver() {{
            window.addEventListener('scroll', () => {{
                const box = document.getElementById('playerBox');
                if (!box || box.style.display === 'none' || userClosedPlayer) return;
                const rect = box.getBoundingClientRect();
                const isFarDown = (window.scrollY > 400 || rect.bottom < 50);

                if (isFarDown && !isFloatingMini) {{
                    activateFloatingMiniPlayer();
                }} else if (!isFarDown && isFloatingMini && window.scrollY < 200) {{
                    deactivateFloatingMiniPlayer();
                }}
            }}, {{ passive: true }});
        }}

        function initMiniPlayerDraggable() {{
            const container = document.getElementById('ytContainer');
            const topOverlay = container ? container.querySelector('.yt-top-overlay') : null;
            if (!topOverlay || topOverlay.dataset.dragInit === '1') return;
            topOverlay.dataset.dragInit = '1';

            let isDragging = false;
            let hasDragged = false;
            let startX, startY, initLeft, initTop;

            topOverlay.addEventListener('mousedown', (e) => {{
                if (!isFloatingMini || e.target.closest('button')) return;
                isDragging = true;
                hasDragged = false;
                const rect = container.getBoundingClientRect();
                startX = e.clientX;
                startY = e.clientY;
                initLeft = rect.left;
                initTop = rect.top;
                container.style.right = 'auto';
                container.style.bottom = 'auto';
                container.style.left = `${{initLeft}}px`;
                container.style.top = `${{initTop}}px`;
            }});

            window.addEventListener('mousemove', (e) => {{
                if (!isDragging || !isFloatingMini) return;
                const dx = e.clientX - startX;
                const dy = e.clientY - startY;
                if (Math.abs(dx) > 3 || Math.abs(dy) > 3) {{
                    hasDragged = true;
                }}
                container.style.left = `${{initLeft + dx}}px`;
                container.style.top = `${{initTop + dy}}px`;
            }});

            window.addEventListener('mouseup', () => {{ isDragging = false; }});

            topOverlay.addEventListener('click', (e) => {{
                if (e.target.closest('button') || e.target.closest('.yt-mini-size-controls')) return;
                if (isFloatingMini && !hasDragged) {{
                    togglePlayPause();
                }}
            }});
        }}

        async function loadStoryboard(streamUrl, duration) {{
            const safeId = currentPlayingSafeId;
            const thumbElem = document.getElementById('ytThumbPreview');
            if (!safeId || !thumbElem) return;

            // 1. Instant check from pre-populated storyboardCache
            const cachedSb = storyboardCache[safeId] || (currentVideoKey ? storyboardCache[currentVideoKey] : null);
            if (cachedSb && cachedSb.available && cachedSb.sprite_url) {{
                activeStoryboard = cachedSb;
                thumbElem.style.backgroundImage = `url('${{cachedSb.sprite_url}}')`;
                return;
            }}

            try {{
                const res = await fetch(`/api/storyboard?key=${{encodeURIComponent(currentVideoKey || '')}}&id=${{encodeURIComponent(safeId)}}`);
                if (!res.ok) return;
                const data = await res.json();
                if (data.available && data.sprite_url) {{
                    activeStoryboard = data;
                    registerStoryboard(data.safe_id || safeId, currentVideoKey, data.mtime);
                    thumbElem.style.backgroundImage = `url('${{data.sprite_url}}')`;
                }}
            }} catch (e) {{}}
        }}

        function initYtPlayerEvents() {{
            if (ytPlayerEventsBound) return;
            ytPlayerEventsBound = true;

            const player = document.getElementById('videoPlayer');
            const playIcon = document.getElementById('ytPlayIcon');
            const pauseIcon = document.getElementById('ytPauseIcon');
            const curTime = document.getElementById('ytCurTime');
            const durTime = document.getElementById('ytDurTime');
            const scrubProgress = document.getElementById('ytScrubProgress');
            const scrubBuffered = document.getElementById('ytScrubBuffered');
            const scrubContainer = document.getElementById('ytScrubContainer');
            const scrubTooltip = document.getElementById('ytScrubTooltip');
            const volSlider = document.getElementById('ytVolumeSlider');
            const spinner = document.getElementById('ytSpinner');

            player.addEventListener('play', () => {{
                if (playIcon) playIcon.style.display = 'none';
                if (pauseIcon) pauseIcon.style.display = 'block';
            }});
            player.addEventListener('pause', () => {{
                if (playIcon) playIcon.style.display = 'block';
                if (pauseIcon) pauseIcon.style.display = 'none';
                captureCurrentVideoPosition();
            }});
            player.addEventListener('waiting', () => {{ if (spinner) spinner.style.display = 'block'; }});
            player.addEventListener('playing', () => {{ if (spinner) spinner.style.display = 'none'; }});
            player.addEventListener('canplay', () => {{ if (spinner) spinner.style.display = 'none'; }});

            player.addEventListener('timeupdate', () => {{
                if (!player.duration) return;
                const pct = (player.currentTime / player.duration) * 100;
                if (scrubProgress && !isScrubDragging) scrubProgress.style.width = `${{pct}}%`;
                if (curTime) curTime.textContent = formatTime(player.currentTime);
                if (durTime) durTime.textContent = formatTime(player.duration);

                if (player.buffered && player.buffered.length > 0 && scrubBuffered) {{
                    const bufEnd = player.buffered.end(player.buffered.length - 1);
                    scrubBuffered.style.width = `${{Math.min(100, (bufEnd / player.duration) * 100)}}%`;
                }}

                const now = Date.now();
                if (now - lastPosSaveTime > 1500) {{
                    lastPosSaveTime = now;
                    captureCurrentVideoPosition();
                }}
            }});

            // Click directly on videoPlayer to toggle play/pause
            player.addEventListener('click', (e) => {{
                if (e.target.closest('.yt-controls-overlay') || e.target.closest('.yt-mini-size-controls')) return;
                togglePlayPause();
            }});

            // Volume control with quadratic auditory power curve & mouse wheel
            function changeDiscoverVolumeBy(delta) {{
                const isMuted = player.muted || player.volume === 0;
                let curSliderVal = isMuted ? 0 : Math.sqrt(player.volume);
                curSliderVal = Math.min(1, Math.max(0, curSliderVal + delta));
                player.volume = Math.min(1, Math.max(0, Math.pow(curSliderVal, 2)));
                player.muted = (curSliderVal === 0);
                updateVolumeUI();
                const pct = Math.round(curSliderVal * 100);
                flashCenterFeedback(player.muted || pct === 0 ? '🔇 Muted' : `🔊 ${{pct}}%`);
            }}

            function handleDiscoverVolumeWheel(e) {{
                e.preventDefault();
                e.stopPropagation();
                const step = 0.04;
                const delta = e.deltaY < 0 ? step : -step;
                changeDiscoverVolumeBy(delta);
            }}

            if (volSlider) {{
                volSlider.addEventListener('input', (e) => {{
                    const sliderVal = parseFloat(e.target.value);
                    player.volume = Math.min(1, Math.max(0, Math.pow(sliderVal, 2)));
                    player.muted = (sliderVal === 0);
                    updateVolumeUI();
                }});
                volSlider.addEventListener('wheel', handleDiscoverVolumeWheel, {{ passive: false }});
            }}

            const volGroup = document.querySelector('.yt-volume-group');
            if (volGroup) {{
                volGroup.addEventListener('wheel', handleDiscoverVolumeWheel, {{ passive: false }});
            }}

            const muteBtn = document.getElementById('ytMuteBtn');
            if (muteBtn) {{
                muteBtn.addEventListener('wheel', handleDiscoverVolumeWheel, {{ passive: false }});
            }}

            const playerContainer = document.getElementById('ytContainer');
            if (playerContainer) {{
                playerContainer.addEventListener('wheel', (e) => {{
                    if (isFloatingMini) {{
                        handleDiscoverVolumeWheel(e);
                    }} else if (e.target.closest('.yt-volume-group') || e.target.closest('.yt-volume-slider-wrap')) {{
                        handleDiscoverVolumeWheel(e);
                    }}
                }}, {{ passive: false }});
            }}

            // Scrub Bar Scrubbing & Tooltip
            function scrubAtEvent(e) {{
                const rect = scrubContainer.getBoundingClientRect();
                const pos = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
                if (player.duration) player.currentTime = pos * player.duration;
            }}

            scrubContainer.addEventListener('mousedown', (e) => {{
                isScrubDragging = true;
                scrubAtEvent(e);
            }});
            window.addEventListener('mousemove', (e) => {{ if (isScrubDragging) scrubAtEvent(e); }});
            window.addEventListener('mouseup', () => {{ if (isScrubDragging) {{ isScrubDragging = false; captureCurrentVideoPosition(); }} }});

            scrubContainer.addEventListener('mousemove', (e) => {{
                const rect = scrubContainer.getBoundingClientRect();
                const pos = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
                if (player.duration && scrubTooltip) {{
                    scrubTooltip.style.display = 'block';
                    const tooltipHalf = 104;
                    const mouseX = e.clientX - rect.left;
                    scrubTooltip.style.left = `${{Math.max(tooltipHalf, Math.min(rect.width - tooltipHalf, mouseX))}}px`;

                    const hoverTime = pos * player.duration;
                    const timeEl = document.getElementById('ytTooltipTime');
                    if (timeEl) timeEl.textContent = formatTime(hoverTime);

                    const thumbEl = document.getElementById('ytThumbPreview');
                    if (activeStoryboard && thumbEl) {{
                        thumbEl.style.display = 'block';
                        const frameIdx = Math.min(99, Math.floor(pos * 100));
                        const col = frameIdx % 10;
                        const row = Math.floor(frameIdx / 10);
                        thumbEl.style.backgroundSize = '2000px 1120px';
                        thumbEl.style.backgroundPosition = `-${{col * 200}}px -${{row * 112}}px`;
                    }}
                }}
            }});
            scrubContainer.addEventListener('mouseleave', () => {{
                if (scrubTooltip) scrubTooltip.style.display = 'none';
            }});
        }}

        /* Full Keyboard Shortcuts */
        document.addEventListener('keydown', (e) => {{
            if (['input', 'textarea'].includes(document.activeElement.tagName.toLowerCase())) return;
            const player = document.getElementById('videoPlayer');
            const code = e.code;

            if (code === 'Space' || e.key === 'k') {{ e.preventDefault(); togglePlayPause(); }}
            else if (code === 'ArrowLeft' || e.key === 'j') {{ e.preventDefault(); seekRelative(-10); }}
            else if (code === 'ArrowRight' || e.key === 'l') {{ e.preventDefault(); seekRelative(10); }}
            else if (e.key === 'm') {{ e.preventDefault(); toggleMute(); }}
            else if (e.key === 'f') {{ e.preventDefault(); toggleFullscreen(); }}
            else if (e.key === 't') {{ e.preventDefault(); toggleTheaterMode(); }}
            else if (e.key === 'p') {{ e.preventDefault(); togglePiP(); }}
            else if (e.key === 's') {{ e.preventDefault(); toggleStatsForNerds(); }}
            else if (e.key === '/') {{
                e.preventDefault();
                const searchInput = document.getElementById('discoverSearchInput');
                if (searchInput) searchInput.focus();
            }} else if (e.key === 'Escape') {{
                closePlayer();
                closeScreensModal();
                closeMetadataModal();
                closeWatchDrawer();
                document.querySelectorAll('.card-ss-overlay.is-open').forEach(el => el.classList.remove('is-open'));
                document.querySelectorAll('.card-action-arrow-btn.is-active').forEach(b => b.classList.remove('is-active'));
            }}
        }});

        /* =========================================================================
           SCREENS LIGHTBOX & METADATA EDIT MODALS
           ========================================================================= */
        function openScreensModal(safeId, title, event) {{
            if (event) event.stopPropagation();
            const modal = document.getElementById('screensModal');
            const img = document.getElementById('screensModalImg');
            const titleEl = document.getElementById('screensModalTitle');
            const rawBtn = document.getElementById('screensModalRawBtn');
            const dlBtn = document.getElementById('screensModalDownloadBtn');
            if (!modal || !img) return;

            const fileObj = (allFiles || []).find(f => f.safe_id === safeId);
            const sb = storyboardCache[safeId];
            const url = (sb && sb.sprite_url) ? sb.sprite_url : `/api/storyboard/image?id=${{encodeURIComponent(safeId)}}`;

            img.src = url;
            if (titleEl) titleEl.textContent = title ? '🖼️ ' + title : '100-Screen Contact Sheet';
            if (rawBtn) rawBtn.href = url;
            if (dlBtn) {{
                dlBtn.href = url;
                dlBtn.download = (title || safeId) + '_screens.jpg';
            }}
            modal.style.display = 'flex';
        }}

        function closeScreensModal(event) {{
            if (event && event.target && event.target.closest && event.target.closest('.modal')) return;
            const modal = document.getElementById('screensModal');
            if (modal) modal.style.display = 'none';
        }}

        function promptEditMetadata(key) {{
            const file = allFiles.find(f => f.key === key);
            if (!file) return;
            const meta = file.metadata || {{}};

            document.getElementById('editKey').value = key;
            document.getElementById('editTitle').value = meta.title || "";
            document.getElementById('editYear').value = meta.year || "";
            document.getElementById('editRating').value = meta.rating || "";
            document.getElementById('editGenres').value = (meta.genres || []).join(", ");
            document.getElementById('editTags').value = (meta.tags || []).join(", ");
            document.getElementById('editOverview').value = meta.overview || meta.plot || "";

            document.getElementById('metadataModal').style.display = 'flex';
        }}

        function closeMetadataModal() {{
            document.getElementById('metadataModal').style.display = 'none';
        }}

        async function saveMetadata() {{
            const key = document.getElementById('editKey').value;
            const file = allFiles.find(f => f.key === key);
            if (!file) return;

            const title = document.getElementById('editTitle').value.trim();
            const year = document.getElementById('editYear').value.trim();
            const rating = document.getElementById('editRating').value.trim();
            const genres = document.getElementById('editGenres').value.split(',').map(s => s.trim()).filter(Boolean);
            const tags = document.getElementById('editTags').value.split(',').map(s => s.trim()).filter(Boolean);
            const overview = document.getElementById('editOverview').value.trim();

            const updatedMeta = {{
                title: title || file.metadata?.title || key.split('/').pop(),
                year, rating, genres, tags, overview
            }};

            try {{
                const res = await fetch('/api/metadata', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ key, metadata: updatedMeta }})
                }});
                const data = await res.json();
                if (data.success) {{
                    file.metadata = updatedMeta;
                    closeMetadataModal();
                    computeFilteredFiles();
                    resetAndRenderGrid();
                    showToast('✅ Metadata updated & synced to NFO!');
                }} else {{
                    showToast('⚠️ Failed: ' + (data.message || 'Error'));
                }}
            }} catch (e) {{
                showToast('⚠️ Save error: ' + e);
            }}
        }}

        /* Make Offline & Cache Polling */
        async function toggleMakeOffline(safeId, event) {{
            if (event) event.stopPropagation();
            const fileObj = (allFiles || []).find(f => f.safe_id === safeId);
            if (!fileObj) return;

            const nextPinned = !fileObj.pinned_offline;
            fileObj.pinned_offline = nextPinned;
            fileObj.is_downloading = nextPinned && ((fileObj.cache_pct || 0) < 99.5);

            showToast(nextPinned ? '📌 Queued for offline download!' : '⏱️ Offline pin removed.');

            try {{
                await fetch('/api/cache/offline', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ safe_id: safeId, enabled: nextPinned }})
                }});
                computeFilteredFiles();
                resetAndRenderGrid();
            }} catch (e) {{}}
        }}

        /* =========================================================================
           CONTINUOUS INFINITE SCROLL & FILTERING ENGINE
           ========================================================================= */
        function computeFilteredFiles() {{
            const q = activeQuery.toLowerCase().trim();

            filteredFiles = allFiles.filter(f => {{
                const meta = f.metadata || {{}};
                const title = (meta.title || f.key.split('/').pop() || '').toLowerCase();
                const genres = (meta.genres || []).map(g => g.toLowerCase());
                const tags = (meta.tags || []).map(t => t.toLowerCase());
                const acc = (f.account || '').toLowerCase();

                if (q) {{
                    const matchTitle = title.includes(q);
                    const matchGenre = genres.some(g => g.includes(q));
                    const matchTag = tags.some(t => t.includes(q));
                    const matchAcc = acc.includes(q);
                    if (!matchTitle && !matchGenre && !matchTag && !matchAcc) return false;
                }}

                for (const filter of activeFilterTags) {{
                    if (filter.startsWith('cat:')) {{
                        if (!genres.includes(filter.substring(4).toLowerCase())) return false;
                    }} else if (filter.startsWith('tag:')) {{
                        if (!tags.includes(filter.substring(4).toLowerCase())) return false;
                    }} else if (filter.startsWith('acc:')) {{
                        if (acc !== filter.substring(4).toLowerCase()) return false;
                    }} else if (filter === 'preset:4k') {{
                        if (!tags.some(t => ['4k', '2160p', 'uhd'].includes(t))) return false;
                    }} else if (filter === 'preset:1080p') {{
                        if (!tags.some(t => ['1080p', 'full hd', 'fhd'].includes(t))) return false;
                    }} else if (filter === 'preset:720p') {{
                        if (!tags.some(t => ['720p', 'hd ready', 'hd'].includes(t))) return false;
                    }} else if (filter === 'preset:short') {{
                        if ((f.duration_sec || 0) >= 900) return false;
                    }} else if (filter === 'preset:long') {{
                        if ((f.duration_sec || 0) < 1800) return false;
                    }} else if (filter === 'preset:offline') {{
                        if (!f.pinned_offline) return false;
                    }} else if (filter === 'preset:cached') {{
                        if (!f.cache_pct || f.cache_pct < 1) return false;
                    }}
                }}
                return true;
            }});

            applySorting();
        }}

        function applySorting() {{
            if (activeSort === 'newest') {{
                filteredFiles.sort((a, b) => (b.uploaded_at || '').localeCompare(a.uploaded_at || ''));
            }} else if (activeSort === 'oldest') {{
                filteredFiles.sort((a, b) => (a.uploaded_at || '').localeCompare(b.uploaded_at || ''));
            }} else if (activeSort === 'name_asc') {{
                filteredFiles.sort((a, b) => (a.metadata?.title || a.key).localeCompare(b.metadata?.title || b.key));
            }} else if (activeSort === 'name_desc') {{
                filteredFiles.sort((a, b) => (b.metadata?.title || b.key).localeCompare(a.metadata?.title || a.key));
            }} else if (activeSort === 'duration_desc') {{
                filteredFiles.sort((a, b) => (b.duration_sec || 0) - (a.duration_sec || 0));
            }} else if (activeSort === 'duration_asc') {{
                filteredFiles.sort((a, b) => (a.duration_sec || 0) - (b.duration_sec || 0));
            }} else if (activeSort === 'size_desc') {{
                filteredFiles.sort((a, b) => (b.size_bytes || 0) - (a.size_bytes || 0));
            }} else if (activeSort === 'size_asc') {{
                filteredFiles.sort((a, b) => (a.size_bytes || 0) - (b.size_bytes || 0));
            }} else if (activeSort === 'rating_desc') {{
                filteredFiles.sort((a, b) => parseFloat(b.metadata?.rating || 0) - parseFloat(a.metadata?.rating || 0));
            }} else if (activeSort === 'random') {{
                for (let i = filteredFiles.length - 1; i > 0; i--) {{
                    const j = Math.floor(Math.random() * (i + 1));
                    [filteredFiles[i], filteredFiles[j]] = [filteredFiles[j], filteredFiles[i]];
                }}
            }}
        }}

        function handleSortChange(sortVal) {{
            activeSort = sortVal;
            computeFilteredFiles();
            resetAndRenderGrid();
        }}

        function addActiveFilter(tagType, value) {{
            const key = `${{tagType}}:${{value}}`;
            activeFilterTags.add(key);
            updateFilterChipsUI();
            computeFilteredFiles();
            resetAndRenderGrid();
            showToast(`Filter: ${{value}}`);
        }}

        function removeActiveFilter(key) {{
            activeFilterTags.delete(key);
            updateFilterChipsUI();
            computeFilteredFiles();
            resetAndRenderGrid();
        }}

        function clearAllFilters() {{
            activeFilterTags.clear();
            activeQuery = '';
            const searchInput = document.getElementById('discoverSearchInput');
            if (searchInput) searchInput.value = '';
            document.querySelectorAll('.filter-chip').forEach(c => {{
                c.classList.toggle('is-active', c.getAttribute('data-filter') === 'all');
            }});
            updateFilterChipsUI();
            computeFilteredFiles();
            resetAndRenderGrid();
        }}

        function setPresetFilter(presetName, chipEl) {{
            document.querySelectorAll('.filter-chip').forEach(c => c.classList.remove('is-active'));
            if (chipEl) chipEl.classList.add('is-active');

            activeFilterTags.clear();
            if (presetName !== 'all') activeFilterTags.add(`preset:${{presetName}}`);
            updateFilterChipsUI();
            computeFilteredFiles();
            resetAndRenderGrid();
        }}

        function updateFilterChipsUI() {{
            const container = document.getElementById('activeFiltersContainer');
            const clearBtn = document.getElementById('btnClearAllFilters');
            if (!container) return;

            const badges = [];
            activeFilterTags.forEach(key => {{
                if (key.startsWith('preset:')) return;
                let label = key;
                if (key.startsWith('cat:')) label = `📁 ${{key.substring(4)}}`;
                else if (key.startsWith('tag:')) label = `🏷️ ${{key.substring(4)}}`;
                else if (key.startsWith('acc:')) label = `👤 ${{key.substring(4)}}`;

                badges.push(`
                <span class="active-filter-badge" onclick="removeActiveFilter('${{escapeHtml(key)}}')" title="Remove filter">
                    <span>${{escapeHtml(label)}}</span>
                    <span class="badge-remove">&times;</span>
                </span>`);
            }});

            container.innerHTML = badges.join('');
            if (clearBtn) clearBtn.style.display = (activeFilterTags.size > 0 || activeQuery) ? 'inline-flex' : 'none';
        }}

        /* Search input listeners */
        const searchInput = document.getElementById('discoverSearchInput');
        const searchClear = document.getElementById('searchClearBtn');
        const shortcutHint = document.getElementById('shortcutHint');

        if (searchInput) {{
            let debounceTimer = null;
            searchInput.addEventListener('input', (e) => {{
                const val = e.target.value;
                if (searchClear) searchClear.style.display = val ? 'inline-block' : 'none';
                if (shortcutHint) shortcutHint.style.display = val ? 'none' : 'inline-block';
                clearTimeout(debounceTimer);
                debounceTimer = setTimeout(() => {{
                    activeQuery = val;
                    computeFilteredFiles();
                    resetAndRenderGrid();
                }}, 150);
            }});
        }}

        function clearSearch() {{
            if (searchInput) {{
                searchInput.value = '';
                searchInput.focus();
            }}
            if (searchClear) searchClear.style.display = 'none';
            if (shortcutHint) shortcutHint.style.display = 'inline-block';
            activeQuery = '';
            computeFilteredFiles();
            resetAndRenderGrid();
        }}

        /* Infinite Scroll Renderer */
        function resetAndRenderGrid() {{
            const grid = document.getElementById('videoGrid');
            if (!grid) return;
            grid.innerHTML = '';
            renderedCount = 0;
            renderNextBatch(BATCH_SIZE_INITIAL);
        }}

        function renderNextBatch(batchSize) {{
            const grid = document.getElementById('videoGrid');
            const footerText = document.getElementById('feedFooterText');
            const spinner = document.getElementById('feedSpinner');
            if (!grid) return;

            const nextFiles = filteredFiles.slice(renderedCount, renderedCount + batchSize);
            if (nextFiles.length === 0 && renderedCount === 0) {{
                grid.innerHTML = '<div style="grid-column: 1 / -1; text-align: center; padding: 80px 20px; color: #71717a;">No videos match your search/filter criteria.</div>';
                if (footerText) footerText.textContent = '';
                if (spinner) spinner.style.display = 'none';
                return;
            }}

            const cardsHtml = nextFiles.map(f => {{
                const nodeName = f.key.split('/').pop();
                return renderCard(f, nodeName);
            }}).join('');

            grid.insertAdjacentHTML('beforeend', cardsHtml);
            renderedCount += nextFiles.length;

            if (footerText) {{
                if (renderedCount >= filteredFiles.length) {{
                    footerText.textContent = `Showing all ${{filteredFiles.length}} videos &bull; Continuous feed complete`;
                    if (spinner) spinner.style.display = 'none';
                }} else {{
                    footerText.textContent = `Showing ${{renderedCount}} of ${{filteredFiles.length}} videos`;
                }}
            }}
        }}

        window.addEventListener('scroll', () => {{
            if (isLoadingMore) return;
            if (renderedCount >= filteredFiles.length) return;

            const scrollPos = window.innerHeight + window.scrollY;
            const threshold = document.body.offsetHeight - 500;

            if (scrollPos >= threshold) {{
                isLoadingMore = true;
                const spinner = document.getElementById('feedSpinner');
                if (spinner) spinner.style.display = 'block';

                setTimeout(() => {{
                    renderNextBatch(BATCH_SIZE_SUBSEQUENT);
                    isLoadingMore = false;
                }}, 150);
            }}
        }});

        /* Watch Later & History Drawer */
        let currentDrawerTab = 'later';

        function toggleWatchDrawer() {{
            const overlay = document.getElementById('drawerOverlay');
            const panel = document.getElementById('drawerPanel');
            if (!panel) return;

            const isOpen = panel.classList.contains('is-open');
            if (isOpen) {{
                closeWatchDrawer();
            }} else {{
                renderDrawerItems();
                if (overlay) overlay.classList.add('is-visible');
                panel.classList.add('is-open');
            }}
        }}

        function closeWatchDrawer() {{
            const overlay = document.getElementById('drawerOverlay');
            const panel = document.getElementById('drawerPanel');
            if (overlay) overlay.classList.remove('is-visible');
            if (panel) panel.classList.remove('is-open');
        }}

        function switchDrawerTab(tab) {{
            currentDrawerTab = tab;
            const btnLater = document.getElementById('btnTabWatchLater');
            const btnHist = document.getElementById('btnTabHistory');
            if (btnLater) btnLater.classList.toggle('active', tab === 'later');
            if (btnHist) btnHist.classList.toggle('active', tab === 'history');
            renderDrawerItems();
        }}

        function renderDrawerItems() {{
            const list = document.getElementById('drawerItemsList');
            if (!list) return;
            updateWatchDrawerBadges();

            if (currentDrawerTab === 'later') {{
                const queuedFiles = allFiles.filter(f => watchLaterKeys.has(f.key));
                if (queuedFiles.length === 0) {{
                    list.innerHTML = '<div style="text-align: center; color: #71717a; padding: 40px 10px; font-size: 0.82rem;">Your Watch Later queue is empty.<br>Click ⏱️ on any video to add.</div>';
                    return;
                }}
                list.innerHTML = queuedFiles.map(f => {{
                    const title = f.metadata?.title || f.key.split('/').pop();
                    const safeId = f.safe_id || '';
                    const thumbUrl = safeId ? `/api/screens/thumb?id=${{safeId}}` : '';
                    return `
                    <div class="drawer-item" onclick="playVideo('/stream/${{f.key}}', '${{escapeHtml(title)}}', '${{f.blob_path || ''}}')">
                        <div class="drawer-item-thumb" style="background-image: url('${{thumbUrl}}');"></div>
                        <div class="drawer-item-info">
                            <div class="drawer-item-title" title="${{escapeHtml(title)}}">${{escapeHtml(title)}}</div>
                            <div class="drawer-item-sub">${{f.duration_str || ''}} &bull; ${{f.size_str || ''}}</div>
                        </div>
                        <button type="button" class="drawer-item-remove" onclick="removeWatchLater('${{escapeHtml(f.key)}}', event)">&times;</button>
                    </div>`;
                }}).join('');
            }} else {{
                if (watchHistoryList.length === 0) {{
                    list.innerHTML = '<div style="text-align: center; color: #71717a; padding: 40px 10px; font-size: 0.82rem;">No watch history yet.<br>Videos you play appear here.</div>';
                    return;
                }}
                list.innerHTML = watchHistoryList.map(h => {{
                    const thumbUrl = h.safe_id ? `/api/screens/thumb?id=${{h.safe_id}}` : '';
                    const progressPct = h.duration_sec > 0 ? Math.min(100, Math.round((h.time_sec / h.duration_sec) * 100)) : 0;
                    return `
                    <div class="drawer-item" onclick="playVideoWithSeek('${{h.safe_id}}', ${{h.time_sec}})">
                        <div class="drawer-item-thumb" style="background-image: url('${{thumbUrl}}');">
                            <div style="position: absolute; bottom: 0; left: 0; right: 0; height: 3px; background: rgba(255,255,255,0.2);">
                                <div style="height: 100%; width: ${{progressPct}}%; background: #38bdf8;"></div>
                            </div>
                        </div>
                        <div class="drawer-item-info">
                            <div class="drawer-item-title" title="${{escapeHtml(h.title)}}">${{escapeHtml(h.title)}}</div>
                            <div class="drawer-item-sub">Resume at ${{formatTime(h.time_sec)}} / ${{h.duration_str || formatTime(h.duration_sec)}}</div>
                        </div>
                    </div>`;
                }}).join('');
            }}
        }}

        function toggleWatchLater(key, event) {{
            if (event) event.stopPropagation();
            if (watchLaterKeys.has(key)) {{
                watchLaterKeys.delete(key);
                showToast('Removed from Watch Later');
            }} else {{
                watchLaterKeys.add(key);
                showToast('Added to Watch Later');
            }}
            saveWatchLater();
            if (currentDrawerTab === 'later') renderDrawerItems();
        }}

        function removeWatchLater(key, event) {{
            if (event) event.stopPropagation();
            watchLaterKeys.delete(key);
            saveWatchLater();
            renderDrawerItems();
        }}

        function clearCurrentDrawerTab() {{
            if (currentDrawerTab === 'later') {{
                watchLaterKeys.clear();
                saveWatchLater();
                showToast('Watch Later cleared');
            }} else {{
                watchHistoryList = [];
                saveWatchHistory();
                showToast('Watch History cleared');
            }}
            renderDrawerItems();
        }}

        function updateWatchDrawerBadges() {{
            const badge = document.getElementById('drawerTabCountBadge');
            const countLater = document.getElementById('countWatchLater');
            const countHist = document.getElementById('countHistory');
            const total = watchLaterKeys.size + watchHistoryList.length;

            if (badge) badge.textContent = total;
            if (countLater) countLater.textContent = watchLaterKeys.size;
            if (countHist) countHist.textContent = watchHistoryList.length;
        }}

        /* Staging queue badge check */
        async function checkStagingPushQueue() {{
            try {{
                const res = await fetch('/api/staging/status');
                if (!res.ok) return;
                const d = await res.json();
                const badge = document.getElementById('stagedQueueBadge');
                const countSpan = document.getElementById('stagedQueueCount');
                if (badge && countSpan) {{
                    const count = d.total_ready_count || (d.ready_repos ? d.ready_repos.length : 0);
                    if (count > 0) {{
                        countSpan.textContent = count + ' Batch' + (count > 1 ? 'es' : '');
                        badge.style.display = 'inline-flex';
                    }} else {{
                        badge.style.display = 'none';
                    }}
                }}
            }} catch (e) {{}}
        }}

        async function pushStagedRepo(repoFolder) {{
            if (!confirm('Are you sure you want to push "' + repoFolder + '" to Hugging Face in 1 atomic commit?')) return;
            try {{
                const res = await fetch('/api/staging/push', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ repo_folder: repoFolder }})
                }});
                const data = await res.json();
                if (data.success) {{
                    alert('1-Commit push launched for ' + repoFolder + '! Redirecting to dashboard...');
                    window.location.href = '/?tab=staging';
                }} else {{
                    alert('Push failed: ' + (data.message || 'Unknown error'));
                }}
            }} catch (err) {{
                alert('Network error launching push: ' + err);
            }}
        }}

        function initAccountFilterChips() {{
            const row = document.getElementById('filterChipsRow');
            if (!row || !allAccounts || allAccounts.length <= 1) return;
            const container = document.createElement('div');
            container.style.display = 'inline-flex';
            container.style.gap = '6px';
            container.style.alignItems = 'center';
            container.style.borderLeft = '1px solid rgba(255, 255, 255, 0.12)';
            container.style.paddingLeft = '8px';
            container.style.marginLeft = '4px';

            allAccounts.forEach(acc => {{
                if (!acc) return;
                const count = allFiles.filter(f => (f.account || primaryAccount) === acc).length;
                if (count === 0) return;
                const chip = document.createElement('div');
                chip.className = 'filter-chip';
                chip.setAttribute('data-account', acc);
                chip.innerHTML = `&#128100; ${{escapeHtml(acc)}} <span style="font-size:0.75rem;opacity:0.7;">(${{count}})</span>`;
                chip.onclick = () => {{
                    if (activeFilterTags.has(`acc:${{acc}}`)) {{
                        removeActiveFilter(`acc:${{acc}}`);
                        chip.classList.remove('is-active');
                    }} else {{
                        addActiveFilter('acc', acc);
                        chip.classList.add('is-active');
                    }}
                }};
                container.appendChild(chip);
            }});
            const activeCont = document.getElementById('activeFiltersContainer');
            if (activeCont) {{
                row.insertBefore(container, activeCont);
            }}
        }}

        /* Boot up */
        initAccountFilterChips();
        computeFilteredFiles();
        resetAndRenderGrid();
        updateWatchDrawerBadges();
        checkStagingPushQueue();
        setInterval(checkStagingPushQueue, 3500);
    </script>
</body>
</html>"""
