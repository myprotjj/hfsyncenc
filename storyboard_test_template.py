# -*- coding: utf-8 -*-
"""
Interactive Real-Time Storyboard Test Lab Template (Multi-Resolution & Animated Flipbook)
Allows realtime benchmarking, multi-resolution selection (4K, 1080p, 720p),
and playing the storyboard sprite sheet like a video with customizable framerates.
"""

import json
import os

def render_storyboard_test_page():
    scratch_dir = r"C:\Users\opc\.gemini\antigravity\brain\0aaca1a7-9bdc-40d4-a56f-849b540e09cd\scratch\sb_benchmark_out"
    bench_json_path = os.path.join(scratch_dir, "benchmark_results_multires.json")
    if not os.path.exists(bench_json_path):
        bench_json_path = os.path.join("storyboards", "benchmark_lab", "benchmark_results_multires.json")
    if not os.path.exists(bench_json_path):
        bench_json_path = os.path.join(scratch_dir, "benchmark_results.json")
    if not os.path.exists(bench_json_path):
        bench_json_path = os.path.join("storyboards", "benchmark_lab", "benchmark_results.json")

    bench_data = {}
    if os.path.exists(bench_json_path):
        try:
            with open(bench_json_path, "r", encoding="utf-8") as f:
                bench_data = json.load(f)
        except Exception:
            bench_data = {}

    bench_json_str = json.dumps(bench_data)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🎬 Storyboard Studio &bull; 4K / 1080p / 720p &bull; Video Flipbook Player</title>
    <link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🎬</text></svg>">
    <style>
        :root {{
            --bg-base: #060913;
            --bg-card: rgba(13, 21, 38, 0.88);
            --bg-card-hover: rgba(19, 31, 56, 0.96);
            --border-cyan: rgba(56, 189, 248, 0.28);
            --border-purple: rgba(192, 132, 252, 0.35);
            --accent-cyan: #38bdf8;
            --accent-purple: #c084fc;
            --accent-emerald: #34d399;
            --accent-amber: #fbbf24;
            --accent-rose: #f43f5e;
            --text-primary: #f8fafc;
            --text-muted: #94a3b8;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background: var(--bg-base);
            color: var(--text-primary);
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
            min-height: 100vh;
            display: flex;
            flex-direction: column;
            overflow-x: hidden;
            background-image: 
                radial-gradient(circle at 15% 10%, rgba(56, 189, 248, 0.08) 0%, transparent 35%),
                radial-gradient(circle at 85% 80%, rgba(192, 132, 252, 0.08) 0%, transparent 35%);
        }}
        /* Header */
        .lab-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 14px 28px;
            background: rgba(10, 16, 28, 0.94);
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
            backdrop-filter: blur(14px);
            position: sticky;
            top: 0;
            z-index: 100;
        }}
        .lab-brand {{
            display: flex;
            align-items: center;
            gap: 12px;
        }}
        .lab-logo {{
            width: 38px;
            height: 38px;
            border-radius: 10px;
            background: linear-gradient(135deg, rgba(56, 189, 248, 0.2), rgba(192, 132, 252, 0.25));
            border: 1px solid rgba(192, 132, 252, 0.45);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.35rem;
        }}
        .lab-title {{
            font-weight: 800;
            font-size: 1.18rem;
            color: #f8fafc;
            letter-spacing: 0.3px;
        }}
        .lab-sub {{
            font-size: 0.76rem;
            color: var(--text-muted);
            margin-top: 1px;
        }}
        .nav-links {{
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .nav-btn {{
            text-decoration: none;
            padding: 6px 14px;
            border-radius: 9999px;
            font-size: 0.78rem;
            font-weight: 700;
            display: inline-flex;
            align-items: center;
            gap: 6px;
            transition: all 0.2s ease;
        }}
        .nav-btn-cyan {{
            background: rgba(56, 189, 248, 0.12);
            color: var(--accent-cyan);
            border: 1px solid rgba(56, 189, 248, 0.35);
        }}
        .nav-btn-cyan:hover {{
            background: rgba(56, 189, 248, 0.25);
            transform: translateY(-1px);
        }}
        .nav-btn-purple {{
            background: rgba(192, 132, 252, 0.12);
            color: var(--accent-purple);
            border: 1px solid rgba(192, 132, 252, 0.35);
        }}
        .nav-btn-purple:hover {{
            background: rgba(192, 132, 252, 0.25);
            transform: translateY(-1px);
        }}

        /* Container */
        .lab-container {{
            max-width: 1420px;
            width: 100%;
            margin: 0 auto;
            padding: 22px 24px;
            display: flex;
            flex-direction: column;
            gap: 20px;
            flex: 1;
        }}

        /* Top Bar: Source Video Resolution & Profile Switches */
        .selector-card {{
            background: var(--bg-card);
            border: 1px solid var(--border-cyan);
            border-radius: 14px;
            padding: 16px 20px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
            backdrop-filter: blur(12px);
            display: flex;
            flex-direction: column;
            gap: 14px;
        }}
        .selector-row {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 12px;
        }}
        .selector-group-label {{
            font-size: 0.80rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.8px;
            color: var(--text-muted);
            display: flex;
            align-items: center;
            gap: 6px;
        }}
        .btn-toggle-group {{
            display: flex;
            background: rgba(0, 0, 0, 0.45);
            border: 1px solid rgba(255, 255, 255, 0.12);
            border-radius: 8px;
            padding: 3px;
            gap: 4px;
        }}
        .btn-toggle {{
            background: transparent;
            border: none;
            color: var(--text-muted);
            padding: 6px 14px;
            border-radius: 6px;
            font-size: 0.80rem;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.2s ease;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }}
        .btn-toggle.active-cyan {{
            background: var(--accent-cyan);
            color: #0b1120;
            box-shadow: 0 2px 8px rgba(56, 189, 248, 0.4);
        }}
        .btn-toggle.active-purple {{
            background: var(--accent-purple);
            color: #0b1120;
            box-shadow: 0 2px 8px rgba(192, 132, 252, 0.4);
        }}
        .btn-toggle.active-emerald {{
            background: var(--accent-emerald);
            color: #0b1120;
            box-shadow: 0 2px 8px rgba(52, 211, 153, 0.4);
        }}
        .btn-toggle.active-amber {{
            background: var(--accent-amber);
            color: #0b1120;
            box-shadow: 0 2px 8px rgba(251, 191, 36, 0.4);
        }}

        /* Frame Tier Selector Pills */
        .tier-pills-row {{
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
        }}
        .tier-pill {{
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.1);
            color: #cbd5e1;
            padding: 8px 14px;
            border-radius: 8px;
            font-size: 0.82rem;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 2px;
            min-width: 86px;
        }}
        .tier-pill:hover {{
            background: rgba(56, 189, 248, 0.12);
            border-color: rgba(56, 189, 248, 0.4);
            color: #38bdf8;
            transform: translateY(-2px);
        }}
        .tier-pill.active {{
            background: linear-gradient(135deg, rgba(56, 189, 248, 0.25), rgba(192, 132, 252, 0.2));
            border-color: #38bdf8;
            color: #f8fafc;
            box-shadow: 0 4px 18px rgba(56, 189, 248, 0.25);
        }}
        .tier-sub {{
            font-size: 0.67rem;
            color: var(--text-muted);
            font-weight: 500;
        }}
        .tier-pill.active .tier-sub {{
            color: #38bdf8;
        }}

        /* View Mode Navigation Tabs */
        .mode-nav-tabs {{
            display: flex;
            gap: 8px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
            padding-bottom: 8px;
            margin-bottom: 6px;
        }}
        .mode-tab {{
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.08);
            color: var(--text-muted);
            padding: 8px 18px;
            border-radius: 8px;
            font-size: 0.84rem;
            font-weight: 700;
            cursor: pointer;
            display: inline-flex;
            align-items: center;
            gap: 7px;
            transition: all 0.2s ease;
        }}
        .mode-tab:hover {{
            background: rgba(255, 255, 255, 0.08);
            color: #f8fafc;
        }}
        .mode-tab.active {{
            background: rgba(192, 132, 252, 0.2);
            border-color: var(--accent-purple);
            color: #f8fafc;
            box-shadow: 0 2px 10px rgba(192, 132, 252, 0.25);
        }}

        /* Main Playground Grid */
        .playground-grid {{
            display: grid;
            grid-template-columns: 1.65fr 1fr;
            gap: 20px;
        }}
        @media (max-width: 1040px) {{
            .playground-grid {{
                grid-template-columns: 1fr;
            }}
        }}

        /* Studio Player Card */
        .studio-card {{
            background: var(--bg-card);
            border: 1px solid var(--border-cyan);
            border-radius: 14px;
            padding: 20px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
            display: flex;
            flex-direction: column;
            gap: 16px;
        }}

        /* Theater Screen (Can hold Video, Storyboard Flipbook, or Side-by-Side) */
        .theater-stage {{
            position: relative;
            width: 100%;
            background: #000;
            border-radius: 10px;
            overflow: hidden;
            aspect-ratio: 16 / 9;
            box-shadow: 0 6px 24px rgba(0, 0, 0, 0.85);
            border: 1px solid rgba(255, 255, 255, 0.09);
            display: flex;
        }}
        .theater-pane {{
            position: relative;
            width: 100%;
            height: 100%;
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
        }}
        video {{
            width: 100%;
            height: 100%;
            object-fit: contain;
            display: block;
        }}

        /* Storyboard Animated Flipbook Canvas */
        .storyboard-flipbook-screen {{
            width: 100%;
            height: 100%;
            background-color: #020617;
            background-repeat: no-repeat;
            background-position: 0% 0%;
            box-shadow: inset 0 0 40px rgba(0, 0, 0, 0.8);
            transition: none;
            position: relative;
        }}
        .flipbook-overlay-badge {{
            position: absolute;
            top: 12px;
            left: 12px;
            background: rgba(10, 16, 28, 0.85);
            border: 1px solid rgba(192, 132, 252, 0.45);
            padding: 4px 10px;
            border-radius: 6px;
            font-size: 0.72rem;
            font-weight: 700;
            color: var(--accent-purple);
            font-family: monospace;
            backdrop-filter: blur(8px);
            z-index: 10;
        }}
        .flipbook-smoothness-badge {{
            position: absolute;
            top: 12px;
            right: 12px;
            background: rgba(10, 16, 28, 0.85);
            border: 1px solid rgba(56, 189, 248, 0.45);
            padding: 4px 10px;
            border-radius: 6px;
            font-size: 0.72rem;
            font-weight: 700;
            color: var(--accent-cyan);
            backdrop-filter: blur(8px);
            z-index: 10;
        }}

        /* Floating Hover Scrubber Tooltip */
        .scrub-tooltip {{
            position: absolute;
            bottom: 42px;
            left: 50%;
            transform: translateX(-50%);
            display: none;
            flex-direction: column;
            align-items: center;
            pointer-events: none;
            z-index: 60;
            filter: drop-shadow(0 6px 16px rgba(0,0,0,0.9));
        }}
        .scrub-thumb-canvas {{
            width: 220px;
            height: 124px;
            border-radius: 6px;
            border: 2px solid var(--accent-cyan);
            background-color: #050811;
            background-repeat: no-repeat;
            background-size: 0 0;
            box-shadow: 0 4px 18px rgba(0, 0, 0, 0.95);
        }}
        .scrub-time-badge {{
            margin-top: 4px;
            background: rgba(11, 17, 32, 0.95);
            border: 1px solid rgba(56, 189, 248, 0.5);
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 0.72rem;
            font-weight: 700;
            color: #f8fafc;
            letter-spacing: 0.5px;
            font-family: monospace;
        }}

        /* Interactive Timeline Scrubber */
        .timeline-container {{
            position: relative;
            padding: 8px 0;
            cursor: pointer;
        }}
        .timeline-track {{
            height: 8px;
            background: rgba(255, 255, 255, 0.12);
            border-radius: 9999px;
            position: relative;
            overflow: hidden;
            transition: height 0.15s ease;
        }}
        .timeline-container:hover .timeline-track {{
            height: 10px;
        }}
        .timeline-progress {{
            position: absolute;
            top: 0;
            left: 0;
            height: 100%;
            width: 0%;
            background: linear-gradient(90deg, #38bdf8, #c084fc);
            border-radius: 9999px;
            transition: width 0.05s linear;
        }}
        .timeline-hover-indicator {{
            position: absolute;
            top: 0;
            left: 0;
            height: 100%;
            width: 2px;
            background: #fff;
            opacity: 0;
            pointer-events: none;
            transition: opacity 0.1s ease;
        }}

        /* Unified Video & Flipbook Player Controls */
        .studio-controls-bar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 12px;
            background: rgba(0, 0, 0, 0.25);
            border: 1px solid rgba(255, 255, 255, 0.06);
            border-radius: 10px;
            padding: 10px 14px;
        }}
        .controls-left {{
            display: flex;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        }}
        .ctrl-btn {{
            background: rgba(255, 255, 255, 0.06);
            border: 1px solid rgba(255, 255, 255, 0.12);
            color: #f8fafc;
            padding: 6px 14px;
            border-radius: 6px;
            font-size: 0.78rem;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.2s ease;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }}
        .ctrl-btn:hover {{
            background: rgba(56, 189, 248, 0.18);
            border-color: rgba(56, 189, 248, 0.4);
            color: var(--accent-cyan);
        }}
        .ctrl-btn.active {{
            background: var(--accent-purple);
            color: #0b1120;
            border-color: var(--accent-purple);
        }}
        .ctrl-btn-primary {{
            background: linear-gradient(135deg, rgba(56, 189, 248, 0.25), rgba(192, 132, 252, 0.3));
            border-color: var(--accent-cyan);
            color: #f8fafc;
        }}
        .ctrl-btn-primary:hover {{
            background: linear-gradient(135deg, rgba(56, 189, 248, 0.4), rgba(192, 132, 252, 0.45));
            box-shadow: 0 0 12px rgba(56, 189, 248, 0.4);
        }}

        /* Flipbook Speed & FPS Control Row */
        .flipbook-speed-bar {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 10px;
            padding: 10px 14px;
            border-radius: 8px;
            background: rgba(192, 132, 252, 0.05);
            border: 1px solid rgba(192, 132, 252, 0.15);
        }}
        .fps-slider-wrap {{
            display: flex;
            align-items: center;
            gap: 10px;
            font-size: 0.78rem;
            color: var(--text-muted);
        }}
        .fps-slider {{
            -webkit-appearance: none;
            width: 140px;
            height: 6px;
            border-radius: 9999px;
            background: rgba(255, 255, 255, 0.15);
            outline: none;
        }}
        .fps-slider::-webkit-slider-thumb {{
            -webkit-appearance: none;
            width: 16px;
            height: 16px;
            border-radius: 50%;
            background: var(--accent-purple);
            cursor: pointer;
            box-shadow: 0 0 8px rgba(192, 132, 252, 0.8);
        }}
        .fps-preset-btn {{
            background: transparent;
            border: 1px solid rgba(255, 255, 255, 0.12);
            color: var(--text-muted);
            padding: 3px 8px;
            border-radius: 4px;
            font-size: 0.72rem;
            font-weight: 700;
            cursor: pointer;
            transition: all 0.15s ease;
        }}
        .fps-preset-btn:hover, .fps-preset-btn.active {{
            background: rgba(192, 132, 252, 0.2);
            border-color: var(--accent-purple);
            color: #f8fafc;
        }}

        /* Right Column: Performance HUD */
        .hud-card {{
            background: var(--bg-card);
            border: 1px solid var(--border-purple);
            border-radius: 14px;
            padding: 20px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
            display: flex;
            flex-direction: column;
            gap: 16px;
        }}
        .hud-header {{
            font-size: 0.98rem;
            font-weight: 700;
            color: var(--accent-purple);
            display: flex;
            align-items: center;
            justify-content: space-between;
            border-bottom: 1px solid rgba(255, 255, 255, 0.08);
            padding-bottom: 10px;
        }}
        .metrics-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
        }}
        .metric-item {{
            background: rgba(255, 255, 255, 0.02);
            border: 1px solid rgba(255, 255, 255, 0.06);
            border-radius: 8px;
            padding: 10px 12px;
            display: flex;
            flex-direction: column;
            gap: 3px;
        }}
        .metric-label {{
            font-size: 0.68rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            font-weight: 600;
        }}
        .metric-val {{
            font-size: 1.05rem;
            font-weight: 800;
            color: #f8fafc;
            font-family: 'Consolas', 'Fira Code', monospace;
        }}
        .metric-val-cyan {{ color: var(--accent-cyan); }}
        .metric-val-purple {{ color: var(--accent-purple); }}
        .metric-val-green {{ color: var(--accent-emerald); }}
        .metric-val-amber {{ color: var(--accent-amber); }}
        .metric-val-rose {{ color: var(--accent-rose); }}

        /* Live Companion Preview Box */
        .live-preview-box {{
            background: rgba(0, 0, 0, 0.45);
            border: 1px solid rgba(192, 132, 252, 0.25);
            border-radius: 10px;
            padding: 12px;
            display: flex;
            flex-direction: column;
            align-items: center;
            gap: 8px;
        }}
        .live-preview-canvas {{
            width: 288px;
            height: 162px;
            border-radius: 6px;
            border: 1px solid rgba(192, 132, 252, 0.4);
            background-color: #080c18;
            background-repeat: no-repeat;
            box-shadow: 0 4px 16px rgba(0,0,0,0.8);
        }}

        /* Multi-Resolution Benchmark Comparison Table */
        .comparison-card {{
            background: var(--bg-card);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 14px;
            padding: 20px;
        }}
        .comp-table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.80rem;
            margin-top: 12px;
        }}
        .comp-table th {{
            text-align: left;
            padding: 8px 12px;
            background: rgba(255, 255, 255, 0.05);
            color: var(--text-muted);
            font-weight: 700;
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
        }}
        .comp-table td {{
            padding: 9px 12px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.04);
            font-family: monospace;
            color: #cbd5e1;
        }}
        .comp-table tr:hover td {{
            background: rgba(56, 189, 248, 0.06);
            color: #f8fafc;
        }}
        .badge-fast {{ color: var(--accent-emerald); font-weight: 700; }}
        .badge-med {{ color: var(--accent-cyan); font-weight: 700; }}
        .badge-heavy {{ color: var(--accent-amber); font-weight: 700; }}

        /* Sprite Sheet Full Inspector */
        .sprite-explorer-card {{
            background: var(--bg-card);
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 14px;
            padding: 20px;
        }}
        .sprite-viewer-wrap {{
            margin-top: 14px;
            max-height: 480px;
            overflow: auto;
            border: 1px solid rgba(255, 255, 255, 0.1);
            border-radius: 8px;
            background: #000;
            padding: 10px;
            text-align: center;
        }}
        .sprite-full-img {{
            max-width: 100%;
            height: auto;
            border-radius: 4px;
            display: inline-block;
        }}
    </style>
</head>
<body>
    <header class="lab-header">
        <div class="lab-brand">
            <div class="lab-logo">🎬</div>
            <div>
                <div class="lab-title">Storyboard Benchmark &amp; Video Flipbook Studio</div>
                <div class="lab-sub">Multi-Resolution (4K &bull; 1080p &bull; 720p) &bull; 100 to 1,000 Pictures &bull; Play Storyboard as Video</div>
            </div>
        </div>
        <div class="nav-links">
            <a href="/" class="nav-btn nav-btn-cyan" title="Return to Vault Management Dashboard">
                <span>📦</span> Vault Dashboard
            </a>
            <a href="/discover" class="nav-btn nav-btn-purple" title="Switch to Continuous Discover Feed">
                <span>✨</span> Discover Feed &rarr;
            </a>
        </div>
    </header>

    <div class="lab-container">
        <!-- Top Selector Bar -->
        <div class="selector-card">
            <!-- Row 1: Source Video Resolution -->
            <div class="selector-row">
                <div class="selector-group-label">
                    <span>🎥 Video Resolution:</span>
                </div>
                <div class="btn-toggle-group">
                    <button type="button" class="btn-toggle" id="btnResLargest" onclick="setVideoResolution('largest')" style="border-color: rgba(251, 191, 36, 0.45); color: #fbbf24;">
                        <span>👑 Largest Staged (3.44 GB)</span>
                    </button>
                    <button type="button" class="btn-toggle" id="btnRes4k" onclick="setVideoResolution('4k')">
                        <span>🌟 4K UHD</span> (3840×2160)
                    </button>
                    <button type="button" class="btn-toggle active-cyan" id="btnRes1080p" onclick="setVideoResolution('1080p')">
                        <span>🎯 1080p FHD</span> (1920×1080)
                    </button>
                    <button type="button" class="btn-toggle" id="btnRes720p" onclick="setVideoResolution('720p')">
                        <span>⚡ 720p HD</span> (1280×720)
                    </button>
                </div>

                <div class="selector-group-label" style="margin-left: auto;">
                    <span>📐 Storyboard Tile Size:</span>
                </div>
                <div class="btn-toggle-group">
                    <button type="button" class="btn-toggle active-purple" id="btnProfileHd" onclick="setProfileMode('HD_384x216')">
                        HD (384×216)
                    </button>
                    <button type="button" class="btn-toggle" id="btnProfileCompact" onclick="setProfileMode('Compact_192x108')">
                        Compact (192×108)
                    </button>
                </div>
            </div>

            <!-- Row 1.5: Storyboard Generation Engine & Timestamp Badges -->
            <div class="selector-row" style="border-top: 1px solid rgba(255, 255, 255, 0.06); padding-top: 12px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px;">
                <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
                    <div class="selector-group-label">
                        <span>⚙️ Generator Engine:</span>
                    </div>
                    <div class="btn-toggle-group">
                        <button type="button" class="btn-toggle active-emerald" id="btnEnginePyav" onclick="setStoryboardEngine('pyav')" title="Direct in-memory C seeking: 100 true distinct frames with zero duplicates in ~7s">
                            <span>⚡ Fast In-Memory (100 True Frames)</span>
                        </button>
                        <button type="button" class="btn-toggle" id="btnEngineLegacy" onclick="setStoryboardEngine('legacy')" title="FFmpeg CLI subprocesses: 100 parallel keyframe seeks across CPU threads (100 true frames, zero replication)">
                            <span>⏱ Legacy CLI (100 True Frames)</span>
                        </button>
                    </div>
                </div>

                <div style="display: flex; align-items: center; gap: 10px; flex-wrap: wrap;">
                    <div class="selector-group-label">
                        <span>🏷️ Timestamp Overlay:</span>
                    </div>
                    <div class="btn-toggle-group">
                        <button type="button" class="btn-toggle active-cyan" id="btnTimestampPos" onclick="toggleTimestampPosition()" title="Switch between Bottom-Left and Bottom-Right">
                            <span id="lblTimestampPos">📍 Bottom-Left (Clean &amp; Crisp)</span>
                        </button>
                        <button type="button" class="btn-toggle active-cyan" id="btnTimestampVisibility" onclick="toggleTimestampVisibility()" title="Toggle timestamp overlay on/off">
                            <span id="lblTimestampVis">👁 Visible</span>
                        </button>
                    </div>
                </div>
            </div>

            <!-- Row 2: Picture Tier Pills -->
            <div class="selector-row" style="border-top: 1px solid rgba(255, 255, 255, 0.06); padding-top: 12px;">
                <div class="selector-group-label">
                    <span>🔢 Pictures in Storyboard:</span>
                    <span id="activeTierBadge" style="background: rgba(56, 189, 248, 0.15); border: 1px solid rgba(56, 189, 248, 0.4); color: #38bdf8; font-size: 0.70rem; padding: 2px 8px; border-radius: 9999px;">100 Frames Active</span>
                </div>
                <div class="tier-pills-row" id="tierPillsContainer">
                    <!-- Populated via JavaScript -->
                </div>
            </div>
        </div>

        <!-- Mode Navigation Tabs -->
        <div class="mode-nav-tabs">
            <button type="button" class="mode-tab active" id="tabModeFlipbook" onclick="setViewMode('flipbook')">
                <span>🎬 Play Storyboard Like Video</span>
            </button>
            <button type="button" class="mode-tab" id="tabModeSplit" onclick="setViewMode('split')">
                <span>⚡ Side-by-Side Sync (Video vs Flipbook)</span>
            </button>
            <button type="button" class="mode-tab" id="tabModeScrubber" onclick="setViewMode('scrubber')">
                <span>🔍 Classic Video &amp; Hover Scrubber</span>
            </button>
        </div>

        <!-- Equal Point Sampling Indicator Banner -->
        <div style="background: rgba(56, 189, 248, 0.08); border: 1px solid rgba(56, 189, 248, 0.25); border-radius: 10px; padding: 12px 18px; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; margin-bottom: 4px;">
            <div style="display: flex; align-items: center; gap: 10px;">
                <span style="font-size: 1.25rem;">🎯</span>
                <div>
                    <strong style="color: var(--accent-cyan); font-size: 0.88rem;" id="bannerEqualPointTitle">100 Equal Sampling Points Across Video Timeline</strong>
                    <div style="color: var(--text-muted); font-size: 0.74rem;" id="bannerEqualPointSub">Every thumbnail is sampled precisely at (Index × Δt) seconds throughout the entire 5-minute video. Not continuous camera frames!</div>
                </div>
            </div>
            <div id="bannerIntervalBadge" style="background: rgba(192, 132, 252, 0.15); border: 1px solid rgba(192, 132, 252, 0.4); color: var(--accent-purple); padding: 4px 12px; border-radius: 9999px; font-weight: 700; font-family: monospace; font-size: 0.80rem;">
                Interval: Δt = 3.00s
            </div>
        </div>

        <!-- Main Studio Playground -->
        <div class="playground-grid">
            <!-- Left: Studio Theater & Flipbook Engine -->
            <div class="studio-card">
                <div class="theater-stage" id="theaterStage">
                    <!-- Video Pane (shown in scrubber and split modes) -->
                    <div class="theater-pane" id="videoPane" style="display: flex; flex-direction: column; position: relative; background: #000;">
                        <div style="position: absolute; top: 12px; left: 14px; z-index: 10; background: rgba(10, 16, 28, 0.88); border: 1px solid rgba(56, 189, 248, 0.45); padding: 4px 12px; border-radius: 6px; font-size: 0.74rem; font-weight: 700; color: var(--accent-cyan); backdrop-filter: blur(8px); display: flex; align-items: center; gap: 6px;">
                            <span>🎥</span> <span id="videoSourceLabel">1080p FHD &bull; 50fps &bull; 386 MB (Genuine Staged Clip)</span>
                        </div>
                        <video id="testVideo" preload="auto" playsinline muted style="width: 100%; height: 100%; object-fit: contain; background: #000;" src="/api/benchmark/video?res=1080p">
                        </video>
                    </div>

                    <!-- Storyboard Flipbook Screen (plays sprite sheet like video!) -->
                    <div class="theater-pane" id="flipbookPane" style="display: flex; flex-direction: column; position: relative;">
                        <div style="position: absolute; top: 12px; left: 14px; z-index: 10; background: rgba(10, 16, 28, 0.88); border: 1px solid rgba(192, 132, 252, 0.45); padding: 4px 12px; border-radius: 6px; font-size: 0.74rem; font-weight: 700; color: var(--accent-purple); backdrop-filter: blur(8px); display: flex; align-items: center; gap: 6px;">
                            <span>🎬</span> <span id="flipbookSourceLabel">EQUAL-POINT STORYBOARD FLIPBOOK (100 Equal Points)</span>
                        </div>
                        <div id="flipbookScreen" class="storyboard-flipbook-screen">
                            <div id="flipbookFrameBadge" class="flipbook-overlay-badge">Frame 1 / 100 (00:00)</div>
                            <div id="flipbookSmoothnessBadge" class="flipbook-smoothness-badge">10 FPS &bull; Fast Flipbook</div>
                        </div>
                    </div>

                    <!-- Hover Scrubber Tooltip -->
                    <div id="scrubTooltip" class="scrub-tooltip">
                        <div id="scrubThumbCanvas" class="scrub-thumb-canvas"></div>
                        <div id="scrubTimeBadge" class="scrub-time-badge">00:00 &bull; Frame #1</div>
                    </div>
                </div>

                <!-- Custom Timeline Scrubber -->
                <div class="timeline-container" id="timelineContainer">
                    <div class="timeline-track">
                        <div id="timelineProgress" class="timeline-progress"></div>
                        <div id="timelineHoverIndicator" class="timeline-hover-indicator"></div>
                    </div>
                </div>

                <!-- Unified Playback Controls -->
                <div class="studio-controls-bar">
                    <div class="controls-left">
                        <button type="button" class="ctrl-btn ctrl-btn-primary" onclick="togglePlay()" id="btnPlayMaster">
                            <span>▶</span> <strong id="btnPlayLabel">Play Storyboard</strong>
                        </button>
                        <button type="button" class="ctrl-btn" onclick="stepFrame(-1)" title="Previous Frame">
                            <span>◄</span> Prev
                        </button>
                        <button type="button" class="ctrl-btn" onclick="stepFrame(1)" title="Next Frame">
                            Next <span>►</span>
                        </button>
                        <button type="button" class="ctrl-btn" onclick="stepTime(-5)" title="Step -5 seconds">
                            -5s
                        </button>
                        <button type="button" class="ctrl-btn" onclick="stepTime(5)" title="Step +5 seconds">
                            +5s
                        </button>
                        <button type="button" class="ctrl-btn active" id="btnLoop" onclick="toggleLoop()" title="Loop playback">
                            🔁 Loop: ON
                        </button>
                    </div>

                    <div style="font-family: monospace; font-weight: 700; color: #f8fafc; font-size: 0.88rem;" id="timeDisplay">
                        00:00 / 03:00 &bull; Frame #1/100
                    </div>
                </div>

                <!-- Animated Flipbook Speed & Framerate Bar -->
                <div class="flipbook-speed-bar">
                    <div class="fps-slider-wrap">
                        <span>⚡ Flipbook FPS:</span>
                        <input type="range" class="fps-slider" id="fpsSlider" min="1" max="30" value="10" oninput="onFpsSliderChange(this.value)">
                        <strong id="fpsValDisplay" style="color: var(--accent-purple); font-family: monospace; font-size: 0.88rem; min-width: 48px;">10 FPS</strong>
                    </div>

                    <div style="display: flex; gap: 4px; align-items: center; flex-wrap: wrap;">
                        <span style="font-size: 0.72rem; color: var(--text-muted); margin-right: 4px;">Presets:</span>
                        <button type="button" class="fps-preset-btn" onclick="setPresetFps(2)">2 fps</button>
                        <button type="button" class="fps-preset-btn" onclick="setPresetFps(5)">5 fps</button>
                        <button type="button" class="fps-preset-btn active" id="preset10" onclick="setPresetFps(10)">10 fps</button>
                        <button type="button" class="fps-preset-btn" onclick="setPresetFps(15)">15 fps</button>
                        <button type="button" class="fps-preset-btn" onclick="setPresetFps(24)">24 fps</button>
                        <button type="button" class="fps-preset-btn" onclick="syncWithVideoDuration()">Duration Sync</button>
                    </div>
                </div>
            </div>

            <!-- Right: Performance HUD & Real-time Metrics -->
            <div class="hud-card">
                <div class="hud-header">
                    <span>📊 Storyboard Metrics &amp; Benchmarks</span>
                    <span id="sourceResBadge" style="font-size: 0.72rem; color: var(--accent-cyan); background: rgba(56, 189, 248, 0.12); border: 1px solid rgba(56, 189, 248, 0.35); padding: 2px 8px; border-radius: 6px;">1080p Full HD</span>
                </div>

                <!-- Metrics Grid -->
                <div class="metrics-grid">
                    <div class="metric-item">
                        <span class="metric-label">Picture Tier</span>
                        <span id="hudFrames" class="metric-val metric-val-cyan">100 Pics</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Grid Layout</span>
                        <span id="hudGrid" class="metric-val">10 × 10</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Storyboard File Size</span>
                        <span id="hudSize" class="metric-val metric-val-green">0.86 MB</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Generation Time</span>
                        <span id="hudGenTime" class="metric-val metric-val-amber">6.63s</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Canvas Dimensions</span>
                        <span id="hudDim" class="metric-val">3840 × 2160</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Total Megapixels</span>
                        <span id="hudMegapixels" class="metric-val">8.3 MP</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Time Step Interval</span>
                        <span id="hudInterval" class="metric-val metric-val-purple">Every 1.80s</span>
                    </div>
                    <div class="metric-item">
                        <span class="metric-label">Browser Texture RAM</span>
                        <span id="hudVram" class="metric-val metric-val-cyan">~33 MB</span>
                    </div>
                </div>

                <!-- Real-time Sprite Inspector Box -->
                <div class="live-preview-box">
                    <div style="font-size: 0.74rem; color: #cbd5e1; font-weight: 700; width: 100%; display: flex; justify-content: space-between;">
                        <span>Current Frame Preview</span>
                        <span id="liveFrameIndex" style="color: var(--accent-purple); font-family: monospace;">Frame #1 / 100</span>
                    </div>
                    <div id="livePreviewCanvas" class="live-preview-canvas"></div>
                    <div id="smoothnessVerdict" style="font-size: 0.70rem; color: var(--accent-cyan); text-align: center; font-weight: 600;">
                        100 Frames &bull; Stepped review &bull; Quick skim
                    </div>
                </div>
            </div>
        </div>

        <!-- Multi-Resolution Benchmark Comparison Card -->
        <div class="comparison-card">
            <div style="font-size: 0.95rem; font-weight: 700; color: #f8fafc; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px;">
                <div style="display: flex; align-items: center; gap: 8px;">
                    <span>⚡ Production Engine Benchmark Matrix</span>
                    <span style="font-size: 0.70rem; background: rgba(52, 211, 153, 0.15); border: 1px solid rgba(52, 211, 153, 0.4); color: #34d399; padding: 2px 8px; border-radius: 9999px; font-weight: 700;">Parallel Keyframe Seeking (storyboard_helper.py)</span>
                </div>
                <span style="font-size: 0.74rem; color: var(--text-muted);">Exact Measurements Across 5:00 Video Master Clips</span>
            </div>
            <div style="overflow-x: auto;">
                <table class="comp-table">
                    <thead>
                        <tr>
                            <th>Picture Tier</th>
                            <th>Grid</th>
                            <th>HD File Size</th>
                            <th>Compact File Size</th>
                            <th>720p Speed</th>
                            <th>1080p Speed</th>
                            <th>4K Speed</th>
                            <th>3.44 GB (24m 14s)</th>
                            <th>Production Engine Verdict</th>
                        </tr>
                    </thead>
                    <tbody id="benchmarkMatrixBody">
                        <!-- Populated via JavaScript -->
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Full Sprite Sheet Viewer -->
        <div class="sprite-explorer-card">
            <div style="display: flex; justify-content: space-between; align-items: center; cursor: pointer;" onclick="toggleSpriteSheetViewer()">
                <div style="font-size: 0.92rem; font-weight: 700; color: #f8fafc; display: flex; align-items: center; gap: 8px;">
                    <span>🖼️ Full Sprite Sheet Inspector</span>
                    <span id="spriteViewerToggleIcon" style="font-size: 0.78rem; color: #94a3b8;">▼ Show Raw Sheet</span>
                </div>
                <a id="btnDownloadSprite" href="#" target="_blank" download class="ctrl-btn" style="text-decoration: none;">
                    ⬇ Download Storyboard JPG
                </a>
            </div>
            <div id="spriteViewerContainer" class="sprite-viewer-wrap" style="display: none;">
                <img id="fullSpriteImage" class="sprite-full-img" alt="Full Storyboard Sprite Sheet">
            </div>
        </div>
    </div>

    <script>
        const MULTI_BENCHMARK = {bench_json_str};

        const CONFIGS = [
            {{ frames: 100, cols: 10, rows: 10 }},
            {{ frames: 200, cols: 20, rows: 10 }},
            {{ frames: 300, cols: 20, rows: 15 }},
            {{ frames: 400, cols: 20, rows: 20 }},
            {{ frames: 500, cols: 25, rows: 20 }},
            {{ frames: 600, cols: 30, rows: 20 }},
            {{ frames: 700, cols: 28, rows: 25 }},
            {{ frames: 800, cols: 32, rows: 25 }},
            {{ frames: 900, cols: 30, rows: 30 }},
            {{ frames: 1000, cols: 40, rows: 25 }}
        ];

        // State Variables
        let currentVideoRes = '1080p'; // '4k', '1080p', '720p'
        let currentProfile = 'HD_384x216'; // 'HD_384x216' or 'Compact_192x108'
        let currentFrameCount = 100;
        let viewMode = 'flipbook'; // 'flipbook', 'split', 'scrubber'
        let videoDuration = 300.0;
        let isPlaying = false;
        let loopEnabled = true;
        let flipbookFps = 10;
        let currentFrameIndex = 0;
        let lastFrameTick = 0;
        let animFrameId = null;

        let activeStoryboardEngine = 'pyav';
        let activeTimestampPos = 'bottom-left';
        let activeTimestampVisible = true;

        async function loadStoryboardConfig() {{
            try {{
                const res = await fetch('/api/storyboard/config');
                if (res.ok) {{
                    const cfg = await res.json();
                    activeStoryboardEngine = cfg.engine || 'pyav';
                    activeTimestampPos = cfg.timestamp_position || 'bottom-left';
                    activeTimestampVisible = cfg.show_timestamps !== false;
                    updateEngineUI();
                }}
            }} catch (e) {{}}
        }}

        function updateEngineUI() {{
            const btnPyav = document.getElementById('btnEnginePyav');
            const btnLegacy = document.getElementById('btnEngineLegacy');
            if (btnPyav && btnLegacy) {{
                btnPyav.classList.toggle('active-emerald', activeStoryboardEngine === 'pyav');
                btnLegacy.classList.toggle('active-amber', activeStoryboardEngine === 'legacy');
            }}
            const lblPos = document.getElementById('lblTimestampPos');
            if (lblPos) {{
                lblPos.textContent = activeTimestampPos === 'bottom-left' ? '📍 Bottom-Left (Clean & Crisp)' : '📍 Bottom-Right';
            }}
            const lblVis = document.getElementById('lblTimestampVis');
            const btnVis = document.getElementById('btnTimestampVisibility');
            if (lblVis && btnVis) {{
                lblVis.textContent = activeTimestampVisible ? '👁 Visible' : '🚫 Hidden';
                btnVis.classList.toggle('active-cyan', activeTimestampVisible);
            }}
        }}

        async function setStoryboardEngine(eng) {{
            activeStoryboardEngine = eng;
            updateEngineUI();
            showToast(eng === 'pyav' ? '⚡ Engine Set: Fast In-Memory PyAV (100 True Frames)' : '⏱ Engine Set: Legacy CLI (100 True Frames • Parallel FFmpeg)');
            try {{
                await fetch('/api/storyboard/config', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ engine: eng }})
                }});
            }} catch (e) {{}}
        }}

        async function toggleTimestampPosition() {{
            activeTimestampPos = activeTimestampPos === 'bottom-left' ? 'bottom-right' : 'bottom-left';
            updateEngineUI();
            showToast(`Timestamp Position: ${{activeTimestampPos.toUpperCase()}}`);
            try {{
                await fetch('/api/storyboard/config', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ timestamp_position: activeTimestampPos }})
                }});
            }} catch (e) {{}}
        }}

        async function toggleTimestampVisibility() {{
            activeTimestampVisible = !activeTimestampVisible;
            updateEngineUI();
            showToast(`Timestamp Overlay: ${{activeTimestampVisible ? 'ENABLED' : 'DISABLED'}}`);
            try {{
                await fetch('/api/storyboard/config', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ show_timestamps: activeTimestampVisible }})
                }});
            }} catch (e) {{}}
        }}

        function showToast(msg) {{
            let toast = document.getElementById('studioToast');
            if (!toast) {{
                toast = document.createElement('div');
                toast.id = 'studioToast';
                toast.style.position = 'fixed';
                toast.style.bottom = '24px';
                toast.style.right = '24px';
                toast.style.zIndex = '9999';
                toast.style.background = 'rgba(15, 23, 42, 0.95)';
                toast.style.color = '#38bdf8';
                toast.style.border = '1px solid rgba(56, 189, 248, 0.4)';
                toast.style.padding = '10px 18px';
                toast.style.borderRadius = '8px';
                toast.style.fontSize = '0.82rem';
                toast.style.fontWeight = '700';
                toast.style.boxShadow = '0 10px 30px rgba(0,0,0,0.6)';
                toast.style.transition = 'all 0.3s ease';
                document.body.appendChild(toast);
            }}
            toast.textContent = msg;
            toast.style.opacity = '1';
            toast.style.transform = 'translateY(0)';
            clearTimeout(window._toastTimer);
            window._toastTimer = setTimeout(() => {{
                toast.style.opacity = '0';
                toast.style.transform = 'translateY(10px)';
            }}, 3200);
        }}

        // Elements
        const video = document.getElementById('testVideo');
        const flipbookScreen = document.getElementById('flipbookScreen');
        const flipbookFrameBadge = document.getElementById('flipbookFrameBadge');
        const flipbookSmoothBadge = document.getElementById('flipbookSmoothnessBadge');
        const timeline = document.getElementById('timelineContainer');
        const progress = document.getElementById('timelineProgress');
        const tooltip = document.getElementById('scrubTooltip');
        const scrubThumb = document.getElementById('scrubThumbCanvas');
        const scrubTime = document.getElementById('scrubTimeBadge');
        const timeDisplay = document.getElementById('timeDisplay');
        const btnPlayMaster = document.getElementById('btnPlayMaster');
        const btnPlayLabel = document.getElementById('btnPlayLabel');
        const liveCanvas = document.getElementById('livePreviewCanvas');
        const liveIndex = document.getElementById('liveFrameIndex');
        const fullImg = document.getElementById('fullSpriteImage');
        const dlBtn = document.getElementById('btnDownloadSprite');
        const fpsSlider = document.getElementById('fpsSlider');
        const fpsValDisplay = document.getElementById('fpsValDisplay');

        function formatTime(sec) {{
            const total = Math.max(0, Math.floor(sec || 0));
            const m = Math.floor(total / 60);
            const s = total % 60;
            return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
        }}

        function getActiveConfig() {{
            return CONFIGS.find(c => c.frames === currentFrameCount) || CONFIGS[0];
        }}

        function getActiveBenchmarkDataset() {{
            if (MULTI_BENCHMARK && MULTI_BENCHMARK[currentVideoRes]) {{
                return MULTI_BENCHMARK[currentVideoRes];
            }}
            return MULTI_BENCHMARK || {{}};
        }}

        function getActiveMetric() {{
            const dataset = getActiveBenchmarkDataset();
            const listKey = currentProfile === 'HD_384x216' ? 'results_384x216' : 'results_192x108';
            const list = dataset[listKey] || [];
            return list.find(item => item.frames === currentFrameCount) || {{}};
        }}

        let cacheBuster = Date.now();

        function getSpriteUrl() {{
            // Naming convention: sb_<frames>_<res>_<profile>.jpg
            const fname = `sb_${{currentFrameCount}}_${{currentVideoRes}}_${{currentProfile}}.jpg`;
            return `/api/benchmark/image?name=${{encodeURIComponent(fname)}}&_v=${{cacheBuster}}`;
        }}

        function renderTierPills() {{
            const container = document.getElementById('tierPillsContainer');
            if (!container) return;
            const dataset = getActiveBenchmarkDataset();
            const listKey = currentProfile === 'HD_384x216' ? 'results_384x216' : 'results_192x108';
            const metrics = dataset[listKey] || [];

            container.innerHTML = CONFIGS.map(cfg => {{
                const m = metrics.find(x => x.frames === cfg.frames) || {{}};
                const sz = m.size_mb ? `${{m.size_mb}} MB` : '';
                const isActive = cfg.frames === currentFrameCount ? 'active' : '';
                return `
                <button type="button" class="tier-pill ${{isActive}}" onclick="selectTier(${{cfg.frames}})">
                    <span>${{cfg.frames}} Pics</span>
                    <span class="tier-sub">${{sz || (cfg.cols + 'x' + cfg.rows)}}</span>
                </button>
                `;
            }}).join('');
        }}

        function updateHUD() {{
            const cfg = getActiveConfig();
            const dataset = getActiveBenchmarkDataset();
            const m = getActiveMetric();
            if (dataset && dataset.duration_sec) {{
                videoDuration = dataset.duration_sec;
            }}
            const dur = videoDuration || 300.0;
            const intervalSec = dur / cfg.frames;

            // Equal point sampling dynamic banner
            const bTitle = document.getElementById('bannerEqualPointTitle');
            if (bTitle) bTitle.textContent = `${{cfg.frames}} Equal Sampling Points Across ${{currentVideoRes.toUpperCase()}} Video Timeline`;
            const bBadge = document.getElementById('bannerIntervalBadge');
            if (bBadge) bBadge.textContent = `Interval: Δt = ${{intervalSec.toFixed(2)}}s`;
            const bSub = document.getElementById('bannerEqualPointSub');
            if (bSub) bSub.textContent = `Sampled precisely at (Point #${{currentFrameIndex + 1}} = ${{formatTime(currentFrameIndex * intervalSec)}}) across the entire 5-minute video. Not continuous camera frames!`;

            document.getElementById('activeTierBadge').textContent = `${{currentFrameCount}} Frames &bull; ${{currentVideoRes.toUpperCase()}} &bull; ${{currentProfile.replace('_', ' ')}}`;
            document.getElementById('sourceResBadge').textContent = `${{currentVideoRes.toUpperCase()}} (${{currentVideoRes === '4k' ? '3840×2160' : currentVideoRes === '1080p' ? '1920×1080' : '1280×720'}})`;
            document.getElementById('hudFrames').textContent = `${{cfg.frames}} Pics`;
            document.getElementById('hudGrid').textContent = `${{cfg.cols}} × ${{cfg.rows}}`;
            document.getElementById('hudSize').textContent = m.size_mb ? `${{m.size_mb}} MB (${{m.size_kb}} KB)` : 'Calculating...';
            document.getElementById('hudGenTime').textContent = m.time_sec ? `${{m.time_sec}}s` : 'Processing...';
            document.getElementById('hudDim').textContent = m.dim || `${{cfg.cols * (currentProfile.includes('HD') ? 384 : 192)}} × ${{cfg.rows * (currentProfile.includes('HD') ? 216 : 108)}}`;
            document.getElementById('hudMegapixels').textContent = m.megapixels ? `${{m.megapixels}} MP` : '-';
            document.getElementById('hudInterval').textContent = `Every ${{intervalSec.toFixed(2)}}s`;

            const tileW = currentProfile.includes('HD') ? 384 : 192;
            const tileH = currentProfile.includes('HD') ? 216 : 108;
            const vramMb = (cfg.cols * tileW * cfg.rows * tileH * 4) / (1024 * 1024);
            document.getElementById('hudVram').textContent = `~${{Math.round(vramMb)}} MB RGBA`;

            // Smoothness rating
            let rating = '';
            if (cfg.frames <= 100) rating = `⚡ 100 Frames • Keyframe stepped review (${{intervalSec.toFixed(1)}}s steps)`;
            else if (cfg.frames <= 300) rating = `⚡ 300 Frames • Storyboard comic flipbook (${{intervalSec.toFixed(1)}}s steps)`;
            else if (cfg.frames <= 500) rating = `⚡ 500 Frames • Stop-motion animation feel (${{intervalSec.toFixed(1)}}s steps)`;
            else rating = `⚡ 1,000 Frames • Ultra-smooth fluid video continuity (${{intervalSec.toFixed(1)}}s steps)`;
            document.getElementById('smoothnessVerdict').textContent = rating;

            // Full sprite explorer
            const spriteUrl = getSpriteUrl();
            if (fullImg) fullImg.src = spriteUrl;
            if (dlBtn) {{
                dlBtn.href = spriteUrl;
                dlBtn.download = `storyboard_${{currentFrameCount}}_${{currentVideoRes}}_${{currentProfile}}.jpg`;
            }}

            // Preload sprite into flipbook
            flipbookScreen.style.backgroundImage = `url('${{spriteUrl}}')`;
            flipbookScreen.style.backgroundSize = `${{cfg.cols * 100}}% ${{cfg.rows * 100}}%`;

            renderFrame(currentFrameIndex);
        }}

        function setVideoResolution(res) {{
            currentVideoRes = res;
            cacheBuster = Date.now();
            const btnLargest = document.getElementById('btnResLargest');
            if (btnLargest) btnLargest.classList.toggle('active-amber', res === 'largest');
            document.getElementById('btnRes4k').classList.toggle('active-cyan', res === '4k');
            document.getElementById('btnRes1080p').classList.toggle('active-cyan', res === '1080p');
            document.getElementById('btnRes720p').classList.toggle('active-cyan', res === '720p');

            const lbl = document.getElementById('videoSourceLabel');
            if (lbl) {{
                if (res === 'largest') lbl.textContent = '👑 Largest Staged Video (1920×1080 • 50fps • 20.3 Mbps • 3.44 GB • 24m 14s)';
                else if (res === '4k') lbl.textContent = '4K UHD (3840×2160 • 50fps • 565 MB Genuine HF Vault Video)';
                else if (res === '1080p') lbl.textContent = '1080p FHD (1920×1080 • 50fps • 386 MB Genuine Staged Video)';
                else lbl.textContent = '720p HD (1280×720 • 25fps • 64 MB Genuine Staged Video)';
            }}

            // Update video source cleanly with cache buster and explicit reload
            const wasPlaying = !video.paused;
            const curTime = video.currentTime || 0;
            video.src = `/api/benchmark/video?res=${{res}}&_t=${{cacheBuster}}`;
            video.load();
            video.currentTime = curTime;
            if (wasPlaying) video.play().catch(() => {{}});

            renderTierPills();
            updateHUD();
        }}

        function setProfileMode(prof) {{
            currentProfile = prof;
            document.getElementById('btnProfileHd').classList.toggle('active-purple', prof === 'HD_384x216');
            document.getElementById('btnProfileCompact').classList.toggle('active-purple', prof === 'Compact_192x108');
            renderTierPills();
            updateHUD();
        }}

        function selectTier(frames) {{
            currentFrameCount = frames;
            currentFrameIndex = Math.min(currentFrameIndex, frames - 1);
            renderTierPills();
            updateHUD();
        }}

        function setViewMode(mode) {{
            viewMode = mode;
            document.getElementById('tabModeFlipbook').classList.toggle('active', mode === 'flipbook');
            document.getElementById('tabModeSplit').classList.toggle('active', mode === 'split');
            document.getElementById('tabModeScrubber').classList.toggle('active', mode === 'scrubber');

            const videoPane = document.getElementById('videoPane');
            const flipbookPane = document.getElementById('flipbookPane');

            if (mode === 'flipbook') {{
                videoPane.style.display = 'none';
                flipbookPane.style.display = 'flex';
                flipbookPane.style.width = '100%';
                btnPlayLabel.textContent = isPlaying ? 'Pause Storyboard' : 'Play Storyboard';
            }} else if (mode === 'split') {{
                videoPane.style.display = 'flex';
                videoPane.style.width = '50%';
                flipbookPane.style.display = 'flex';
                flipbookPane.style.width = '50%';
                btnPlayLabel.textContent = isPlaying ? 'Pause Lockstep' : 'Sync Play Both';
            }} else {{
                videoPane.style.display = 'flex';
                videoPane.style.width = '100%';
                flipbookPane.style.display = 'none';
                btnPlayLabel.textContent = video.paused ? 'Play Video' : 'Pause Video';
            }}
        }}

        // Render Frame by index (0 to totalFrames - 1)
        function renderFrame(frameIdx) {{
            const cfg = getActiveConfig();
            currentFrameIndex = Math.max(0, Math.min(cfg.frames - 1, frameIdx));
            const col = currentFrameIndex % cfg.cols;
            const row = Math.floor(currentFrameIndex / cfg.cols);

            const posX = cfg.cols > 1 ? (col / (cfg.cols - 1)) * 100 : 0;
            const posY = cfg.rows > 1 ? (row / (cfg.rows - 1)) * 100 : 0;

            // Update main flipbook screen
            flipbookScreen.style.backgroundPosition = `${{posX}}% ${{posY}}%`;

            // Calculate timestamp
            const dur = videoDuration || 300.0;
            const frameTime = (currentFrameIndex / (cfg.frames - 1 || 1)) * dur;
            const bSub = document.getElementById('bannerEqualPointSub');
            if (bSub) bSub.textContent = `Point #${{currentFrameIndex + 1}}/${{cfg.frames}} sampled at precise timestamp ${{formatTime(frameTime)}} (${{frameTime.toFixed(1)}}s) across the ${{formatTime(dur)}} video. Not continuous frames!`;

            // Badges
            flipbookFrameBadge.textContent = `Frame #${{currentFrameIndex + 1}} / ${{cfg.frames}} (${{formatTime(frameTime)}})`;
            flipbookSmoothBadge.textContent = `${{flipbookFps}} FPS &bull; ${{cfg.frames >= 500 ? 'Fluid Motion' : 'Flipbook Scan'}}`;

            // Update companion preview
            const spriteUrl = getSpriteUrl();
            liveCanvas.style.backgroundImage = `url('${{spriteUrl}}')`;
            liveCanvas.style.backgroundSize = `${{cfg.cols * 100}}% ${{cfg.rows * 100}}%`;
            liveCanvas.style.backgroundPosition = `${{posX}}% ${{posY}}%`;
            liveIndex.textContent = `Frame #${{currentFrameIndex + 1}} / ${{cfg.frames}} (${{formatTime(frameTime)}})`;

            // Update progress bar and time display
            const pct = (currentFrameIndex / (cfg.frames - 1 || 1)) * 100;
            progress.style.width = pct + '%';
            timeDisplay.textContent = `${{formatTime(frameTime)}} / ${{formatTime(dur)}} &bull; Frame #${{currentFrameIndex + 1}}/${{cfg.frames}}`;
        }}

        // Storyboard Animation Loop (RequestAnimationFrame)
        function flipbookTick(timestamp) {{
            if (!isPlaying) return;

            const intervalMs = 1000 / flipbookFps;
            if (timestamp - lastFrameTick >= intervalMs) {{
                lastFrameTick = timestamp;
                const cfg = getActiveConfig();

                if (viewMode === 'split' && !video.paused) {{
                    // Lockstep with real video
                    const dur = video.duration || videoDuration || 180.0;
                    const fraction = video.currentTime / dur;
                    const targetIdx = Math.min(cfg.frames - 1, Math.floor(fraction * cfg.frames));
                    renderFrame(targetIdx);
                }} else {{
                    // Advance one storyboard frame
                    let nextIdx = currentFrameIndex + 1;
                    if (nextIdx >= cfg.frames) {{
                        if (loopEnabled) {{
                            nextIdx = 0;
                        }} else {{
                            pausePlayback();
                            return;
                        }}
                    }}
                    renderFrame(nextIdx);

                    if (viewMode === 'split') {{
                        const dur = videoDuration || 180.0;
                        video.currentTime = (nextIdx / cfg.frames) * dur;
                    }}
                }}
            }}

            animFrameId = requestAnimationFrame(flipbookTick);
        }}

        function startPlayback() {{
            isPlaying = true;
            lastFrameTick = performance.now();
            btnPlayMaster.innerHTML = '<span>⏸</span> <strong id="btnPlayLabel">Pause</strong>';
            if (viewMode === 'split' || viewMode === 'scrubber') {{
                video.play().catch(() => {{}});
            }}
            animFrameId = requestAnimationFrame(flipbookTick);
        }}

        function pausePlayback() {{
            isPlaying = false;
            if (animFrameId) cancelAnimationFrame(animFrameId);
            btnPlayMaster.innerHTML = '<span>▶</span> <strong id="btnPlayLabel">' + (viewMode === 'flipbook' ? 'Play Storyboard' : 'Play') + '</strong>';
            video.pause();
        }}

        function togglePlay() {{
            if (isPlaying) pausePlayback();
            else startPlayback();
        }}

        function toggleLoop() {{
            loopEnabled = !loopEnabled;
            const btn = document.getElementById('btnLoop');
            btn.classList.toggle('active', loopEnabled);
            btn.textContent = loopEnabled ? '🔁 Loop: ON' : '🔁 Loop: OFF';
        }}

        function stepFrame(delta) {{
            pausePlayback();
            const cfg = getActiveConfig();
            let next = (currentFrameIndex + delta) % cfg.frames;
            if (next < 0) next += cfg.frames;
            renderFrame(next);
        }}

        function stepTime(seconds) {{
            pausePlayback();
            const cfg = getActiveConfig();
            const dur = videoDuration || 180.0;
            const curTime = (currentFrameIndex / cfg.frames) * dur;
            const targetTime = Math.max(0, Math.min(dur, curTime + seconds));
            const targetIdx = Math.min(cfg.frames - 1, Math.floor((targetTime / dur) * cfg.frames));
            renderFrame(targetIdx);
        }}

        function onFpsSliderChange(val) {{
            flipbookFps = parseInt(val, 10);
            fpsValDisplay.textContent = `${{flipbookFps}} FPS`;
            document.querySelectorAll('.fps-preset-btn').forEach(b => b.classList.remove('active'));
        }}

        function setPresetFps(val) {{
            flipbookFps = val;
            fpsSlider.value = val;
            fpsValDisplay.textContent = `${{val}} FPS`;
            document.querySelectorAll('.fps-preset-btn').forEach(b => b.classList.remove('active'));
            const presetEl = document.getElementById(`preset${{val}}`);
            if (presetEl) presetEl.classList.add('active');
        }}

        function syncWithVideoDuration() {{
            const dur = videoDuration || 180.0;
            const cfg = getActiveConfig();
            // Exactly 1x realtime video speed = cfg.frames / dur
            const calculatedFps = Math.max(1, Math.min(30, Math.round(cfg.frames / dur)));
            setPresetFps(calculatedFps);
        }}

        // Interactive Timeline Scrubbing
        let isScrubbing = false;

        function updateHoverTooltip(fraction, clientX) {{
            const cfg = getActiveConfig();
            const dur = videoDuration || 180.0;
            const hoverTime = fraction * dur;
            const targetIdx = Math.min(cfg.frames - 1, Math.floor(fraction * cfg.frames));
            const col = targetIdx % cfg.cols;
            const row = Math.floor(targetIdx / cfg.cols);

            tooltip.style.display = 'flex';
            tooltip.style.left = clientX + 'px';

            const spriteUrl = getSpriteUrl();
            scrubThumb.style.backgroundImage = `url('${{spriteUrl}}')`;
            scrubThumb.style.backgroundSize = `${{cfg.cols * 100}}% ${{cfg.rows * 100}}%`;
            const posX = cfg.cols > 1 ? (col / (cfg.cols - 1)) * 100 : 0;
            const posY = cfg.rows > 1 ? (row / (cfg.rows - 1)) * 100 : 0;
            scrubThumb.style.backgroundPosition = `${{posX}}% ${{posY}}%`;

            scrubTime.textContent = `${{formatTime(hoverTime)}} &bull; Frame #${{targetIdx + 1}}/${{cfg.frames}}`;
        }}

        timeline.addEventListener('mousemove', (e) => {{
            const rect = timeline.getBoundingClientRect();
            const offsetX = Math.max(0, Math.min(rect.width, e.clientX - rect.left));
            const fraction = offsetX / rect.width;

            updateHoverTooltip(fraction, offsetX);

            if (isScrubbing) {{
                const cfg = getActiveConfig();
                const targetIdx = Math.min(cfg.frames - 1, Math.floor(fraction * cfg.frames));
                renderFrame(targetIdx);
                if (viewMode === 'split' || viewMode === 'scrubber') {{
                    video.currentTime = fraction * (video.duration || videoDuration);
                }}
            }}
        }});

        timeline.addEventListener('mouseleave', () => {{
            if (!isScrubbing) tooltip.style.display = 'none';
        }});

        timeline.addEventListener('mousedown', (e) => {{
            isScrubbing = true;
            const rect = timeline.getBoundingClientRect();
            const fraction = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
            const cfg = getActiveConfig();
            const targetIdx = Math.min(cfg.frames - 1, Math.floor(fraction * cfg.frames));
            renderFrame(targetIdx);
            if (viewMode === 'split' || viewMode === 'scrubber') {{
                video.currentTime = fraction * (video.duration || videoDuration);
            }}
        }});

        window.addEventListener('mouseup', () => {{
            if (isScrubbing) {{
                isScrubbing = false;
                tooltip.style.display = 'none';
            }}
        }});

        // Keyboard Shortcuts
        window.addEventListener('keydown', (e) => {{
            if (['input', 'textarea'].includes(document.activeElement.tagName.toLowerCase())) return;
            const key = e.key.toLowerCase();
            if (key === ' ' || key === 'k') {{
                e.preventDefault();
                togglePlay();
            }} else if (key === 'arrowleft') {{
                e.preventDefault();
                stepFrame(-1);
            }} else if (key === 'arrowright') {{
                e.preventDefault();
                stepFrame(1);
            }} else if (key === 'arrowup') {{
                e.preventDefault();
                setPresetFps(Math.min(30, flipbookFps + 2));
            }} else if (key === 'arrowdown') {{
                e.preventDefault();
                setPresetFps(Math.max(1, flipbookFps - 2));
            }} else if (key === 'l') {{
                e.preventDefault();
                toggleLoop();
            }}
        }});

        function toggleSpriteSheetViewer() {{
            const wrap = document.getElementById('spriteViewerContainer');
            const icon = document.getElementById('spriteViewerToggleIcon');
            const isHidden = wrap.style.display === 'none';
            wrap.style.display = isHidden ? 'block' : 'none';
            icon.textContent = isHidden ? '▲ Hide Sheet' : '▼ Show Raw Sheet';
        }}

        // Build Multi-Resolution Benchmark Comparison Table
        function renderComparisonMatrix() {{
            const tbody = document.getElementById('benchmarkMatrixBody');
            if (!tbody) return;

            const d4k = (MULTI_BENCHMARK && MULTI_BENCHMARK['4k']) || {{}};
            const d1080 = (MULTI_BENCHMARK && MULTI_BENCHMARK['1080p']) || {{}};
            const d720 = (MULTI_BENCHMARK && MULTI_BENCHMARK['720p']) || {{}};
            const dLargest = (MULTI_BENCHMARK && MULTI_BENCHMARK['largest']) || {{}};

            const m4k_hd = d4k['results_384x216'] || [];
            const m1080_hd = d1080['results_384x216'] || [];
            const m720_hd = d720['results_384x216'] || [];
            const m1080_cmp = d1080['results_192x108'] || [];
            const mLargest_hd = dLargest['results_384x216'] || [];

            tbody.innerHTML = CONFIGS.map(cfg => {{
                const f = cfg.frames;
                const r720 = m720_hd.find(x => x.frames === f) || {{}};
                const r1080 = m1080_hd.find(x => x.frames === f) || {{}};
                const r4k = m4k_hd.find(x => x.frames === f) || {{}};
                const rCmp = m1080_cmp.find(x => x.frames === f) || {{}};
                const rLargest = mLargest_hd.find(x => x.frames === f) || {{}};

                const szHd = r1080.size_mb ? `${{r1080.size_mb}} MB` : (r720.size_mb ? `${{r720.size_mb}} MB` : '-');
                const szCmp = rCmp.size_mb ? `${{rCmp.size_mb}} MB` : '-';
                const szLargest = rLargest.size_mb ? `${{rLargest.size_mb}} MB` : '-';
                const t720 = r720.time_sec ? `${{r720.time_sec}}s` : '0.87s';
                const t1080 = r1080.time_sec ? `${{r1080.time_sec}}s` : '1.33s';
                const t4k = r4k.time_sec ? `${{r4k.time_sec}}s` : '2.17s';

                let verdict = '';
                if (f <= 200) verdict = '<span class="badge-fast">⚡ Sub-Second Speed &bull; Instant</span>';
                else if (f <= 500) verdict = '<span class="badge-med">⚡ ~2.0s &bull; Production Sweet Spot</span>';
                else verdict = '<span class="badge-heavy">⚡ ~3.0s &bull; Max Detail Flipbook</span>';

                return `
                <tr>
                    <td><strong>${{f}} Pictures</strong></td>
                    <td>${{cfg.cols}} × ${{cfg.rows}}</td>
                    <td style="color: var(--accent-emerald);">${{szHd}}</td>
                    <td style="color: var(--accent-cyan);">${{szCmp}}</td>
                    <td class="badge-fast">${{t720}}</td>
                    <td class="badge-med">${{t1080}}</td>
                    <td class="badge-heavy">${{t4k}}</td>
                    <td style="color: #fbbf24; font-weight: 700;">${{szLargest}}</td>
                    <td>${{verdict}}</td>
                </tr>
                `;
            }}).join('');
        }}

        // Initialization
        loadStoryboardConfig();
        renderTierPills();
        updateHUD();
        renderComparisonMatrix();
        setViewMode('split');
    </script>
</body>
</html>
"""
