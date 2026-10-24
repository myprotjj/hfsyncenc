---
title: HfSync Media Vault
emoji: 🎬
colorFrom: indigo
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---

# HFSYNC: High-Speed Encrypted Cloud Media Vault & Jellyfin Streaming Bridge

An automated system to sync hundreds of gigabytes or terabytes of video content to **public Hugging Face Datasets** with **military-grade authenticated encryption (AES-256-GCM)**, auto-deleting local files one-by-one to save local disk space, while remaining **100% streamable and seekable on Jellyfin** via on-the-fly chunk decryption.

---

## Features & Capabilities

### 1. Interactive Folder Tree Dashboard & Deletion Management
* Open **[http://127.0.0.1:8080/](http://127.0.0.1:8080/)** in any browser.
* **Hierarchical Folder Explorer:** Videos are organized in a clean, collapsible folder tree matching your exact local directory structure (e.g. `📁 SciFi-Vault` &rarr; `📁 2024` &rarr; `📁 Cyberpunk` &rarr; `🎬 movie.mp4`).
* **Instant Real-Time Search:** Filter across all movies and folders as you type.
* **Built-in Web Player:** Stream any video directly in the browser with seeking.
* **Permanent Delete Button:** Delete any video directly from the web interface. This removes the encrypted blob from Hugging Face, removes the `.strm` file in `jellyfin_library/`, and updates the manifest.

### 2. Zero-Delay 1-Millisecond Sparse Media Hashing
* Samples exact file size + first 1 MB (headers) + middle 1 MB + last 1 MB (index).
* Runs in **< 1 millisecond** regardless of file size (even 100+ GB).
* If a file has already been uploaded, it is identified as a duplicate **instantly** and moved to `archive/` in the same folder structure without wasting any upload or encryption time.

### 3. Multi-Threaded Parallel Uploads (Full Internet Saturation)
* The watcher runs a **ThreadPoolExecutor** with 4 concurrent workers.
* Multiple movies encrypt and upload simultaneously over independent TCP streams to Hugging Face, saturating high-bandwidth connections.

### 4. 100% Anonymized Blind Hex Paths on Hugging Face
* Real movie titles, years, and directory paths are **never exposed** on Hugging Face.
* Stored on Hugging Face as random 64-character encrypted hex blobs:
  `blobs/56/56ef31708ddc5b108a4fa1313f84dd535e153c26ed9eb599ed8492394090f175.enc`
* Anyone browsing the public Hugging Face repository sees only encrypted noise.

### 5. Human-Readable Local Jellyfin Library (`.strm`)
* Locally on your PC, [`jellyfin_library/`](file:///C:/Users/opc/ag/hfsyncenc/jellyfin_library) retains the **clean, readable folder & movie names** (e.g. `SciFi-Vault/2024/Cyberpunk/matrix_resurrections.mp4.strm`).
* Jellyfin reads these `.strm` files, downloads official movie posters, actor cast, and IMDb ratings automatically.
* When you press **Play**, the local streaming bridge seamlessly maps the readable name to the obfuscated blob on Hugging Face and decrypts the stream on the fly.

### 6. YouTube-Style On-The-Fly Streaming (Instant Seeking)
* Videos are encrypted in **64 KB independent AES-256-GCM chunks**.
* When seeking or scrubbing in Jellyfin, the bridge downloads **only the requested ~64 KB chunk** from Hugging Face's global CDN, decrypts it in RAM, and feeds it to Jellyfin as HTTP 206 Partial Content.
* **The rest of the movie is NEVER downloaded or stored on disk.**

---

## Folder Layout

```
hfsyncenc/
├── videos_to_sync/    <-- Drop new movies/folders here (processed & deleted automatically)
├── archive/           <-- Duplicate files are automatically moved here in identical structure
├── jellyfin_library/  <-- Add this folder to Jellyfin (contains readable .strm stream pointers)
├── upload_manifest.json
├── env.txt
├── run_watcher.bat    <-- High-speed multi-threaded watcher (4 workers)
├── run_proxy.bat      <-- Streaming bridge server & web UI (port 8080)
└── start_all_services.bat <-- Starts both with 1 click
```

---

## 1-Click Startup

Double click:
```bat
start_all_services.bat
```
* **Interactive Web Dashboard:** [http://127.0.0.1:8080/](http://127.0.0.1:8080/)
* **Auto-Watcher:** Actively monitoring `videos_to_sync/` with 4 concurrent threads.
