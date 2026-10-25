"""
encrypted_stream_cache.py - Dynamic Sparse Encrypted Chunk Cache for HFSYNC
----------------------------------------------------------------------------
Implements an on-the-fly, chunk-by-chunk local cache for encrypted media streams:
- Preserves the EXACT AES-256-GCM ciphertext format as stored on Hugging Face (.enc).
- Never leaks plaintext video data to disk.
- Sparse caching: only stores chunks that have actually been buffered or requested.
- Fast cache lookup: reads previously buffered chunks from disk at local SSD speeds.
- Grouping: batches contiguous cached or missing chunks into spans to minimize cloud I/O.
- Automatic verification: ensures GCM authentication tags are valid before indexing chunks.
- "Make Offline Available" support: can download all remaining chunks in the background and pin forever.
- 24-hour auto-expiration: unpinned videos are automatically deleted 24h after last access.
- Persistent index (.idx bitmask) tracking exact cached chunks, pins, and timestamps across proxy restarts.
"""

import os
import time
import struct
import threading
from typing import Dict, List, Optional, Tuple, Set
import crypto_engine

CACHE_DIR = "encrypted_cache"
INDEX_MAGIC = b"HFCACHE2"
INDEX_HEADER_FMT = ">8sHBBQIQII"  # 40 bytes: magic(8), ver(2), hdr_cached(1), pinned(1), last_accessed(8), chunk_size(4), total_size(8), total_chunks(4), cached_count(4)
INDEX_HEADER_SIZE = struct.calcsize(INDEX_HEADER_FMT)  # 40 bytes

DEFAULT_CACHE_MAX_AGE_SEC = 24 * 3600  # 24 Hours


def format_bytes(num_bytes: int) -> str:
    """Format byte size into human readable string (KB, MB, GB)."""
    if not isinstance(num_bytes, (int, float)) or num_bytes <= 0:
        return "0 B"
    if num_bytes >= 1024 * 1024 * 1024:
        return f"{num_bytes / (1024 * 1024 * 1024):.1f} GB"
    elif num_bytes >= 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.1f} MB"
    elif num_bytes >= 1024:
        return f"{num_bytes / 1024:.0f} KB"
    else:
        return f"{int(num_bytes)} B"


class VideoChunkIndex:
    """Represents a bitmask index of cached chunks for a single video."""

    def __init__(self, cache_id: str, total_size: int, chunk_size: int):
        self.cache_id = cache_id
        self.total_size = total_size
        self.chunk_size = chunk_size
        self.total_chunks = (total_size + chunk_size - 1) // chunk_size if total_size > 0 else 0
        self.enc_chunk_payload = chunk_size + crypto_engine.TAG_SIZE
        
        self.header_cached = False
        self.pinned_offline = False  # Keep offline forever!
        self.last_accessed = int(time.time())  # Unix timestamp for 24-hour auto-expiration
        self.cached_chunks_count = 0
        num_mask_bytes = (self.total_chunks + 7) // 8 if self.total_chunks > 0 else 0
        self.bitmask = bytearray(num_mask_bytes)
        self.dirty = False

    def touch(self):
        """Update last accessed timestamp (resets 24h expiration timer)."""
        self.last_accessed = int(time.time())
        self.dirty = True

    def set_pinned_offline(self, pinned: bool):
        """Pin offline forever (True) or restore 24h auto-expiration (False)."""
        if self.pinned_offline != pinned:
            self.pinned_offline = pinned
            self.touch()

    def is_header_cached(self) -> bool:
        return self.header_cached

    def mark_header_cached(self):
        if not self.header_cached:
            self.header_cached = True
            self.touch()

    def is_chunk_cached(self, c_idx: int) -> bool:
        if c_idx < 0 or c_idx >= self.total_chunks:
            return False
        byte_pos = c_idx >> 3
        bit_pos = c_idx & 7
        if byte_pos >= len(self.bitmask):
            return False
        return bool(self.bitmask[byte_pos] & (1 << bit_pos))

    def mark_chunk_cached(self, c_idx: int):
        if c_idx < 0 or c_idx >= self.total_chunks:
            return
        byte_pos = c_idx >> 3
        bit_pos = c_idx & 7
        if byte_pos >= len(self.bitmask):
            return
        if not (self.bitmask[byte_pos] & (1 << bit_pos)):
            self.bitmask[byte_pos] |= (1 << bit_pos)
            self.cached_chunks_count += 1
            self.touch()

    def get_cached_percentage(self) -> float:
        if self.total_chunks <= 0:
            return 0.0
        return (self.cached_chunks_count / self.total_chunks) * 100.0

    def get_cached_bytes(self) -> int:
        if self.cached_chunks_count <= 0:
            return 0
        return crypto_engine.HEADER_SIZE + (self.cached_chunks_count * self.enc_chunk_payload)

    def get_chunk_spans(self, first_chunk: int, last_chunk: int) -> List[Tuple[bool, int, int]]:
        """
        Divides the requested chunk range into contiguous spans of cached vs missing chunks.
        Returns a list of tuples: (is_cached: bool, span_start: int, span_end: int).
        """
        if first_chunk > last_chunk:
            return []
        first_chunk = max(0, first_chunk)
        last_chunk = min(self.total_chunks - 1, last_chunk)

        spans: List[Tuple[bool, int, int]] = []
        curr_cached: Optional[bool] = None
        curr_start = first_chunk

        for c_idx in range(first_chunk, last_chunk + 1):
            cached = self.is_chunk_cached(c_idx)
            if curr_cached is None:
                curr_cached = cached
                curr_start = c_idx
            elif cached != curr_cached:
                spans.append((curr_cached, curr_start, c_idx - 1))
                curr_cached = cached
                curr_start = c_idx

        if curr_cached is not None:
            spans.append((curr_cached, curr_start, last_chunk))

        return spans

    def serialize(self) -> bytes:
        hdr = struct.pack(
            INDEX_HEADER_FMT,
            INDEX_MAGIC,
            2,  # version 2
            1 if self.header_cached else 0,
            1 if self.pinned_offline else 0,
            self.last_accessed,
            self.chunk_size,
            self.total_size,
            self.total_chunks,
            self.cached_chunks_count
        )
        return hdr + bytes(self.bitmask)

    @classmethod
    def deserialize(cls, cache_id: str, data: bytes) -> Optional['VideoChunkIndex']:
        if len(data) < 31:
            return None
        magic = data[:8]
        if magic == b"HFCACHE2":
            if len(data) < INDEX_HEADER_SIZE:
                return None
            _, version, hdr_cached, pinned, last_accessed, chunk_size, total_size, total_chunks, cached_count = struct.unpack(
                INDEX_HEADER_FMT, data[:INDEX_HEADER_SIZE]
            )
            idx = cls(cache_id, total_size, chunk_size)
            idx.header_cached = bool(hdr_cached)
            idx.pinned_offline = bool(pinned)
            idx.last_accessed = last_accessed
            idx.cached_chunks_count = cached_count
            mask_bytes = data[INDEX_HEADER_SIZE:]
            if len(mask_bytes) >= len(idx.bitmask):
                idx.bitmask = bytearray(mask_bytes[:len(idx.bitmask)])
            else:
                idx.bitmask[:len(mask_bytes)] = mask_bytes
            idx.dirty = False
            return idx
        elif magic == b"HFCACHE1":
            # Backward compatibility with version 1
            INDEX_HEADER_FMT_V1 = ">8sHBIQII"
            V1_SIZE = struct.calcsize(INDEX_HEADER_FMT_V1)
            if len(data) < V1_SIZE:
                return None
            _, version, hdr_cached, chunk_size, total_size, total_chunks, cached_count = struct.unpack(
                INDEX_HEADER_FMT_V1, data[:V1_SIZE]
            )
            idx = cls(cache_id, total_size, chunk_size)
            idx.header_cached = bool(hdr_cached)
            idx.pinned_offline = False
            idx.last_accessed = int(time.time())
            idx.cached_chunks_count = cached_count
            mask_bytes = data[V1_SIZE:]
            if len(mask_bytes) >= len(idx.bitmask):
                idx.bitmask = bytearray(mask_bytes[:len(idx.bitmask)])
            else:
                idx.bitmask[:len(mask_bytes)] = mask_bytes
            idx.dirty = True
            return idx
        return None


class EncryptedStreamCache:
    """High-performance thread-safe manager for sparse encrypted video chunk caching and offline downloads."""

    def __init__(self, cache_dir: str = CACHE_DIR, max_age_seconds: int = DEFAULT_CACHE_MAX_AGE_SEC):
        self.cache_dir = cache_dir
        self.max_age_seconds = max_age_seconds
        os.makedirs(self.cache_dir, exist_ok=True)
        self._indices: Dict[str, VideoChunkIndex] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._active_downloads: Dict[str, threading.Thread] = {}
        self._stop_downloads: Set[str] = set()
        self._global_lock = threading.Lock()

        # Purge any unpinned cache on startup (only pinned offline videos remain on disk)
        self.purge_unpinned_cache()

        # Start background cleaner thread
        self._cleaner_thread = threading.Thread(target=self._expiration_cleaner_loop, daemon=True)
        self._cleaner_thread.start()

    def _get_lock(self, cache_id: str) -> threading.Lock:
        with self._global_lock:
            if cache_id not in self._locks:
                self._locks[cache_id] = threading.Lock()
            return self._locks[cache_id]

    def _get_enc_path(self, cache_id: str) -> str:
        safe_name = "".join(c if (c.isalnum() or c in "._-") else "_" for c in cache_id)
        return os.path.join(self.cache_dir, f"{safe_name}.enc")

    def _get_idx_path(self, cache_id: str) -> str:
        safe_name = "".join(c if (c.isalnum() or c in "._-") else "_" for c in cache_id)
        return os.path.join(self.cache_dir, f"{safe_name}.idx")

    def get_or_load_index(self, cache_id: str, total_size: int = 0, chunk_size: int = 0) -> VideoChunkIndex:
        with self._get_lock(cache_id):
            if cache_id in self._indices:
                idx = self._indices[cache_id]
                if (total_size <= 0 or idx.total_size == total_size) and (chunk_size <= 0 or idx.chunk_size == chunk_size):
                    idx.touch()
                    return idx

            idx_path = self._get_idx_path(cache_id)
            if os.path.exists(idx_path):
                try:
                    with open(idx_path, "rb") as f:
                        loaded = VideoChunkIndex.deserialize(cache_id, f.read())
                    if loaded and (total_size <= 0 or loaded.total_size == total_size or loaded.total_chunks > 0) and (chunk_size <= 0 or loaded.chunk_size == chunk_size or loaded.chunk_size > 0):
                        loaded.touch()
                        self._indices[cache_id] = loaded
                        return loaded
                except Exception:
                    pass

            idx = VideoChunkIndex(cache_id, total_size, chunk_size)
            self._indices[cache_id] = idx
            return idx

    def save_index(self, cache_id: str):
        with self._get_lock(cache_id):
            idx = self._indices.get(cache_id)
            if not idx or not idx.dirty:
                return
            idx_path = self._get_idx_path(cache_id)
            tmp_path = idx_path + ".tmp"
            try:
                with open(tmp_path, "wb") as f:
                    f.write(idx.serialize())
                os.replace(tmp_path, idx_path)
                idx.dirty = False
            except Exception:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

    def has_cached_header(self, cache_id: str) -> bool:
        idx_path = self._get_idx_path(cache_id)
        enc_path = self._get_enc_path(cache_id)
        if not os.path.exists(idx_path) or not os.path.exists(enc_path):
            return False
        with self._get_lock(cache_id):
            if cache_id in self._indices:
                return self._indices[cache_id].is_header_cached()
            try:
                with open(idx_path, "rb") as f:
                    hdr_data = f.read(INDEX_HEADER_SIZE)
                if len(hdr_data) >= 31:
                    idx = VideoChunkIndex.deserialize(cache_id, hdr_data)
                    if idx:
                        return idx.is_header_cached()
            except Exception:
                pass
        return False

    def read_header_bytes(self, cache_id: str) -> Optional[bytes]:
        enc_path = self._get_enc_path(cache_id)
        if not os.path.exists(enc_path) or os.path.getsize(enc_path) < crypto_engine.HEADER_SIZE:
            return None
        with self._get_lock(cache_id):
            try:
                with open(enc_path, "rb") as f:
                    data = f.read(crypto_engine.HEADER_SIZE)
                if cache_id in self._indices:
                    self._indices[cache_id].touch()
                return data
            except Exception:
                return None

    def write_header_bytes(self, cache_id: str, header_bytes: bytes, total_size: int, chunk_size: int):
        if len(header_bytes) < crypto_engine.HEADER_SIZE:
            return
        enc_path = self._get_enc_path(cache_id)
        idx = self.get_or_load_index(cache_id, total_size, chunk_size)
        with self._get_lock(cache_id):
            try:
                mode = "r+b" if os.path.exists(enc_path) else "w+b"
                with open(enc_path, mode) as f:
                    f.seek(0)
                    f.write(header_bytes[:crypto_engine.HEADER_SIZE])
                idx.mark_header_cached()
            except Exception:
                return
        self.save_index(cache_id)

    def get_chunk_cipher_length(self, total_size: int, chunk_size: int, c_idx: int) -> int:
        total_chunks = (total_size + chunk_size - 1) // chunk_size if total_size > 0 else 0
        if c_idx == total_chunks - 1:
            return (total_size - c_idx * chunk_size) + crypto_engine.TAG_SIZE
        return chunk_size + crypto_engine.TAG_SIZE

    def read_chunk_ciphertext(self, cache_id: str, c_idx: int, total_size: int, chunk_size: int) -> Optional[bytes]:
        idx = self.get_or_load_index(cache_id, total_size, chunk_size)
        if not idx.is_chunk_cached(c_idx):
            return None
        enc_path = self._get_enc_path(cache_id)
        if not os.path.exists(enc_path):
            return None
        
        enc_chunk_payload = chunk_size + crypto_engine.TAG_SIZE
        offset = crypto_engine.HEADER_SIZE + (c_idx * enc_chunk_payload)
        c_len = self.get_chunk_cipher_length(total_size, chunk_size, c_idx)

        with self._get_lock(cache_id):
            try:
                with open(enc_path, "rb") as f:
                    f.seek(offset)
                    data = f.read(c_len)
                    if len(data) == c_len:
                        idx.touch()
                        return data
            except Exception:
                pass
        return None

    def write_chunk_ciphertext(self, cache_id: str, c_idx: int, chunk_cipher: bytes, total_size: int, chunk_size: int):
        c_len = self.get_chunk_cipher_length(total_size, chunk_size, c_idx)
        if len(chunk_cipher) != c_len:
            return
        
        idx = self.get_or_load_index(cache_id, total_size, chunk_size)
        enc_path = self._get_enc_path(cache_id)
        enc_chunk_payload = chunk_size + crypto_engine.TAG_SIZE
        offset = crypto_engine.HEADER_SIZE + (c_idx * enc_chunk_payload)

        with self._get_lock(cache_id):
            try:
                mode = "r+b" if os.path.exists(enc_path) else "w+b"
                with open(enc_path, mode) as f:
                    f.seek(offset)
                    f.write(chunk_cipher)
                idx.mark_chunk_cached(c_idx)
            except Exception:
                return

        self.save_index(cache_id)

    def get_chunk_spans(self, cache_id: str, first_chunk: int, last_chunk: int, total_size: int, chunk_size: int) -> List[Tuple[bool, int, int]]:
        idx = self.get_or_load_index(cache_id, total_size, chunk_size)
        idx.touch()
        return idx.get_chunk_spans(first_chunk, last_chunk)

    def is_pinned_offline(self, cache_id: str) -> bool:
        """Checks if a video is explicitly pinned for offline playback."""
        idx = self._indices.get(cache_id)
        if idx:
            return bool(idx.pinned_offline)
        idx_path = self._get_idx_path(cache_id)
        if not os.path.exists(idx_path):
            return False
        try:
            with open(idx_path, "rb") as f:
                loaded = VideoChunkIndex.deserialize(cache_id, f.read())
            if loaded:
                self._indices[cache_id] = loaded
                return bool(loaded.pinned_offline)
        except Exception:
            pass
        return False

    def is_downloading_offline(self, cache_id: str) -> bool:
        """Checks if a video is currently being downloaded offline in background."""
        with self._global_lock:
            return bool(cache_id in self._active_downloads)

    def set_pinned_offline(self, cache_id: str, pinned: bool, total_size: int = 0, chunk_size: int = 0):
        """Toggle pinned offline status for a video. If unpinned, immediately clears offline cache."""
        if not pinned:
            # When unpinned, immediately clear cache so no 24h unpinned cache remains on disk
            self.clear_cache(cache_id=cache_id)
            return
        idx = self.get_or_load_index(cache_id, total_size, chunk_size)
        if idx:
            idx.set_pinned_offline(pinned)
            self._indices[cache_id] = idx
            self.save_index(cache_id)

    def start_offline_download(
        self,
        cache_id: str,
        hf_url: str,
        repo_token: Optional[str],
        total_size: int,
        chunk_size: int,
        base_nonce: bytes,
        aesgcm,
        session
    ) -> bool:
        """
        Marks video pinned_offline = True and launches a background thread to fetch
        any missing chunks until the full video is 100% cached locally.
        """
        idx = self.get_or_load_index(cache_id, total_size, chunk_size)
        idx.set_pinned_offline(True)
        self.save_index(cache_id)

        if idx.cached_chunks_count >= idx.total_chunks:
            return True  # Already 100% complete!

        with self._global_lock:
            if cache_id in self._active_downloads:
                return True
            self._stop_downloads.discard(cache_id)
            th = threading.Thread(
                target=self._offline_download_worker,
                args=(cache_id, hf_url, repo_token, total_size, chunk_size, base_nonce, aesgcm, session),
                daemon=True,
                name=f"OfflineDownloader-{cache_id[:8]}"
            )
            self._active_downloads[cache_id] = th
            th.start()
            return True

    def stop_offline_download(self, cache_id: str):
        """Requests graceful stopping of an in-flight background download."""
        with self._global_lock:
            self._stop_downloads.add(cache_id)

    def _offline_download_worker(
        self,
        cache_id: str,
        hf_url: str,
        repo_token: Optional[str],
        total_size: int,
        chunk_size: int,
        base_nonce: bytes,
        aesgcm,
        session
    ):
        try:
            idx = self.get_or_load_index(cache_id, total_size, chunk_size)
            total_chunks = idx.total_chunks

            def read_exact_bytes(raw_stream, num_bytes):
                buf = bytearray()
                while len(buf) < num_bytes:
                    part = raw_stream.read(num_bytes - len(buf))
                    if not part:
                        break
                    buf.extend(part)
                return bytes(buf)

            c_idx = 0
            while c_idx < total_chunks:
                if cache_id in self._stop_downloads:
                    break

                if idx.is_chunk_cached(c_idx):
                    c_idx += 1
                    continue

                # Batch up to 32 missing chunks (~2 MB per batch for optimal download speed)
                batch_start = c_idx
                batch_end = min(total_chunks - 1, batch_start + 31)
                while batch_end > batch_start and idx.is_chunk_cached(batch_end):
                    batch_end -= 1

                span_range = crypto_engine.calculate_byte_range_chunks(
                    total_size, chunk_size, batch_start * chunk_size, min(total_size - 1, (batch_end + 1) * chunk_size - 1)
                )

                hf_headers = {}
                if repo_token:
                    hf_headers["Authorization"] = f"Bearer {repo_token}"
                hf_headers["Range"] = f"bytes={span_range['enc_start']}-{span_range['enc_end']}"

                try:
                    resp = session.get(hf_url, headers=hf_headers, stream=True, timeout=30)
                    if resp.status_code not in (200, 206):
                        time.sleep(2)
                        continue
                    stream_raw = resp.raw

                    for curr_c in range(batch_start, batch_end + 1):
                        if cache_id in self._stop_downloads:
                            break
                        c_len = self.get_chunk_cipher_length(total_size, chunk_size, curr_c)
                        chunk_cipher = read_exact_bytes(stream_raw, c_len)
                        if not chunk_cipher or len(chunk_cipher) < c_len:
                            break

                        nonce = crypto_engine.get_chunk_nonce(base_nonce, curr_c)
                        # Verify integrity by decrypting tag
                        aesgcm.decrypt(nonce, chunk_cipher, None)

                        # Write to sparse file
                        self.write_chunk_ciphertext(cache_id, curr_c, chunk_cipher, total_size, chunk_size)

                    c_idx = batch_end + 1
                    time.sleep(0.01)  # Yield CPU briefly between batches

                except Exception:
                    time.sleep(2)
                    c_idx = batch_start  # Retry

        finally:
            with self._global_lock:
                self._active_downloads.pop(cache_id, None)
                self._stop_downloads.discard(cache_id)

    def get_cache_info(self, cache_id: str, total_size: int = 0, chunk_size: int = 0) -> dict:
        idx_path = self._get_idx_path(cache_id)
        enc_path = self._get_enc_path(cache_id)
        is_downloading = bool(cache_id in self._active_downloads)

        if not os.path.exists(idx_path) or not os.path.exists(enc_path):
            return {
                "cached": False,
                "cached_chunks": 0,
                "total_chunks": 0,
                "percentage": 0.0,
                "cached_bytes": 0,
                "cached_size_str": "0 B",
                "file_size": 0,
                "pinned_offline": False,
                "is_downloading": is_downloading,
                "last_accessed": 0,
                "expires_in_sec": None
            }
        
        idx = self._indices.get(cache_id)
        if not idx:
            try:
                with open(idx_path, "rb") as f:
                    idx = VideoChunkIndex.deserialize(cache_id, f.read())
                    if idx:
                        self._indices[cache_id] = idx
            except Exception:
                pass

        if not idx:
            return {
                "cached": False,
                "cached_chunks": 0,
                "total_chunks": 0,
                "percentage": 0.0,
                "cached_bytes": 0,
                "cached_size_str": "0 B",
                "file_size": 0,
                "pinned_offline": False,
                "is_downloading": is_downloading,
                "last_accessed": 0,
                "expires_in_sec": None
            }

        file_sz = os.path.getsize(enc_path) if os.path.exists(enc_path) else 0
        cached_b = idx.get_cached_bytes()
        expires_sec = None
        if not idx.pinned_offline:
            expires_sec = max(0, self.max_age_seconds - (int(time.time()) - idx.last_accessed))

        return {
            "cached": idx.cached_chunks_count > 0,
            "cached_chunks": idx.cached_chunks_count,
            "total_chunks": idx.total_chunks,
            "percentage": round(idx.get_cached_percentage(), 1),
            "cached_bytes": cached_b,
            "cached_size_str": format_bytes(cached_b),
            "file_size": file_sz,
            "pinned_offline": idx.pinned_offline,
            "is_downloading": is_downloading,
            "last_accessed": idx.last_accessed,
            "expires_in_sec": expires_sec
        }

    def get_overall_stats(self) -> dict:
        total_cached_bytes = 0
        total_cached_files = 0
        total_pinned_files = 0
        file_stats = {}

        if not os.path.exists(self.cache_dir):
            return {
                "total_cached_bytes": 0,
                "total_cached_size_str": "0 B",
                "total_cached_files": 0,
                "total_pinned_files": 0,
                "files": {}
            }

        for fname in os.listdir(self.cache_dir):
            if fname.endswith(".idx"):
                cache_id = fname[:-4]
                info = self.get_cache_info(cache_id)
                if info["cached"]:
                    total_cached_files += 1
                    total_cached_bytes += info["cached_bytes"]
                    if info["pinned_offline"]:
                        total_pinned_files += 1
                    file_stats[cache_id] = info

        return {
            "total_cached_bytes": total_cached_bytes,
            "total_cached_size_str": format_bytes(total_cached_bytes),
            "total_cached_files": total_cached_files,
            "total_pinned_files": total_pinned_files,
            "files": file_stats
        }

    def purge_unpinned_cache(self) -> int:
        """
        Deletes all cached video files that are NOT pinned offline.
        Ensures videos are only stored locally when offline is explicitly selected.
        Returns number of purged files.
        """
        purged_count = 0
        with self._global_lock:
            if not os.path.exists(self.cache_dir):
                return 0

            for fname in os.listdir(self.cache_dir):
                if not fname.endswith(".idx"):
                    continue
                cache_id = fname[:-4]
                if cache_id in self._active_downloads:
                    continue
                idx_path = self._get_idx_path(cache_id)
                enc_path = self._get_enc_path(cache_id)

                try:
                    idx = self._indices.get(cache_id)
                    if not idx and os.path.exists(idx_path):
                        with open(idx_path, "rb") as f:
                            idx = VideoChunkIndex.deserialize(cache_id, f.read())
                    if idx and not idx.pinned_offline:
                        self._indices.pop(cache_id, None)
                        self._locks.pop(cache_id, None)
                        if os.path.exists(enc_path):
                            try:
                                os.remove(enc_path)
                            except Exception:
                                pass
                        if os.path.exists(idx_path):
                            try:
                                os.remove(idx_path)
                            except Exception:
                                pass
                        purged_count += 1
                except Exception:
                    pass

        return purged_count

    def purge_expired_cache(self, max_age_seconds: Optional[int] = None) -> int:
        """Purges any unpinned cache files."""
        return self.purge_unpinned_cache()

    def _expiration_cleaner_loop(self):
        """Background cleaner that runs every 15 minutes to purge any unpinned cache files."""
        while True:
            try:
                time.sleep(900)  # 15 minutes
                self.purge_unpinned_cache()
            except Exception:
                pass

    def clear_cache(self, cache_id: Optional[str] = None) -> bool:
        """Clears cache for a specific video or all videos."""
        with self._global_lock:
            if cache_id:
                self.stop_offline_download(cache_id)
                idx_path = self._get_idx_path(cache_id)
                enc_path = self._get_enc_path(cache_id)
                self._indices.pop(cache_id, None)
                self._locks.pop(cache_id, None)
                for p in (idx_path, enc_path):
                    if os.path.exists(p):
                        try:
                            os.remove(p)
                        except Exception:
                            pass
                return True
            else:
                for cid in list(self._active_downloads.keys()):
                    self.stop_offline_download(cid)
                self._indices.clear()
                self._locks.clear()
                if os.path.exists(self.cache_dir):
                    for fname in os.listdir(self.cache_dir):
                        p = os.path.join(self.cache_dir, fname)
                        if os.path.isfile(p):
                            try:
                                os.remove(p)
                            except Exception:
                                pass
                return True


# Global Singleton Instance
stream_cache = EncryptedStreamCache()
