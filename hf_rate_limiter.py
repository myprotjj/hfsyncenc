"""
HFSYNC High-Capacity Rate Limiter & Request Pacer for Hugging Face Hub
Prevents HTTP 429 (Too Many Requests) and Secondary Commit Rate Limits
when synchronizing large batches of files.

Features:
1. Automated Hub Interceptor Hook: Hooks into huggingface_hub's underlying httpx2 session
   to capture all 'RateLimit' ("api";r=...;t=...) and 'Retry-After' response headers automatically.
2. Proactive Fixed-Window Token Bucket: Tracks requests per 5-minute window (w=300s).
   Throttles proactively before hitting the 1,000 request/5min limit so quota NEVER hits 0.
3. Commit Rate Pacer: Enforces minimum spacing (1.5s - 2.0s) between Git commits per repo/account
   to eliminate git-lock contention and commit rate limits.
4. Live Header Feedback: Reads 'RateLimit' and 'RateLimit-Policy' headers. If remaining quota 'r'
   drops below safety margin (<60 requests), automatically slows down requests proportionally.
5. Remote Repository File Cache (RemoteRepoCache): Caches repository file trees in memory (TTL: 180s)
   so checking whether video blobs, storyboards, audio, or subtitles exist on HF takes 0 API calls.
6. Debounced Index Synchronizer (DebouncedIndexSyncer): Batches 'vault_index.enc' updates across
   multiple uploaded files so that 50 files don't trigger 50 separate commits to the index.
"""

import time
import threading
import random
import re
from typing import Dict, Set, Optional, Tuple, Any
from huggingface_hub import HfApi

# Global locks
_RATE_LIMITER_LOCK = threading.Lock()
_CACHE_LOCK = threading.Lock()


def parse_ratelimit_header(header_val: str) -> Dict[str, str]:
    """
    Parses RFC draft / Hugging Face RateLimit header strings, e.g.:
      RateLimit: "api";r=984;t=210
      RateLimit-Policy: "fixed window";"api";q=1000;w=300
    """
    res = {}
    if not header_val:
        return res
    for part in header_val.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            res[k.strip().lower()] = v.strip('" ')
        elif part:
            res["policy_type"] = part.strip('" ')
    return res


class HFRateLimiter:
    """
    Thread-safe proactive rate limiter for Hugging Face Hub operations.
    Guarantees that rapid batch uploads never exceed HF's 1,000 req/5min quota
    or commit burst thresholds.
    """
    _instance = None
    _singleton_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "HFRateLimiter":
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = HFRateLimiter()
            return cls._instance

    def __init__(
        self,
        max_requests_per_window: int = 850,     # Safety buffer under HF's 1,000 limit
        window_seconds: int = 300,              # HF fixed window is 300 seconds (5 min)
        min_commit_spacing_sec: float = 2.5,     # Git write spacing (prevents commit rate limits)
        min_request_spacing_sec: float = 0.05    # Base API request pacing
    ):
        self.max_requests = max_requests_per_window
        self.window_sec = window_seconds
        self.min_commit_spacing = min_commit_spacing_sec
        self.min_request_spacing = min_request_spacing_sec

        self.lock = threading.Lock()
        # token -> dict with window_start, request_count, last_commit_time, remaining_quota, reset_in
        self.token_states: Dict[str, Dict[str, Any]] = {}
        self.throttled_until: Dict[str, float] = {}

    def _get_token_key(self, token: Optional[str]) -> str:
        if not token:
            return "default"
        # Use last 10 characters or whole token if short
        return token[-10:] if len(token) > 10 else token

    def _ensure_token_state(self, key: str) -> Dict[str, Any]:
        now = time.time()
        if key not in self.token_states:
            self.token_states[key] = {
                "window_start": now,
                "request_count": 0,
                "last_commit_time": 0.0,
                "last_request_time": 0.0,
                "remaining_quota": 1000,
                "reset_in": 300,
                "last_header_update": 0.0
            }
        state = self.token_states[key]
        if now - state["window_start"] >= self.window_sec:
            state["window_start"] = now
            state["request_count"] = 0
            state["remaining_quota"] = 1000
        return state

    def update_from_headers(self, token: Optional[str], headers: Any):
        """
        Ingests actual RateLimit and RateLimit-Policy response headers returned by Hugging Face
        to align local counter with server-side reality.
        """
        if not headers:
            return
        # Headers can be dict or httpx.Headers
        rl = headers.get("RateLimit") or headers.get("ratelimit")
        if not rl:
            return

        key = self._get_token_key(token)
        parsed = parse_ratelimit_header(str(rl))
        r_val = parsed.get("r")
        t_val = parsed.get("t")

        with self.lock:
            state = self._ensure_token_state(key)
            if r_val and str(r_val).isdigit():
                state["remaining_quota"] = int(r_val)
            if t_val and str(t_val).isdigit():
                state["reset_in"] = int(t_val)
            state["last_header_update"] = time.time()

    def wait_for_request_slot(self, token: Optional[str] = None):
        """
        Blocks smoothly if the request rate is approaching limits.
        Ensures requests never exceed the 5-minute quota.
        """
        key = self._get_token_key(token)
        sleep_time = 0.0

        with self.lock:
            now = time.time()
            # 1. Check if token is in explicit 429 cooldown
            until = self.throttled_until.get(key, 0.0)
            if now < until:
                sleep_time = max(sleep_time, until - now)

            state = self._ensure_token_state(key)

            # 2. Check live remaining quota from HF headers
            rem = state.get("remaining_quota", 1000)
            reset_t = state.get("reset_in", 300)

            if rem <= 40 and reset_t > 0:
                # Urgent throttle: fewer than 40 requests remaining before window reset!
                # Pace requests evenly across remaining time
                pace_delay = max(1.5, reset_t / max(1, rem))
                sleep_time = max(sleep_time, pace_delay)
            elif rem <= 100 and reset_t > 0:
                # Moderate throttle
                pace_delay = max(0.5, (reset_t / max(1, rem)) * 0.8)
                sleep_time = max(sleep_time, pace_delay)

            # 3. Check local window count
            if state["request_count"] >= self.max_requests:
                time_left_in_window = max(1.0, (state["window_start"] + self.window_sec) - now)
                sleep_time = max(sleep_time, time_left_in_window)

            # 4. Enforce micro-spacing
            elapsed_since_last = now - state["last_request_time"]
            if elapsed_since_last < self.min_request_spacing:
                sleep_time = max(sleep_time, self.min_request_spacing - elapsed_since_last)

            # Optimistically record request
            state["request_count"] += 1
            state["last_request_time"] = now + sleep_time

        if sleep_time > 0:
            if sleep_time >= 5.0:
                print(f"[{time.strftime('%H:%M:%S')}] [HF Rate Limiter] Pacing API requests ({sleep_time:.1f}s) to stay safely under 1,000 req/5min quota...")
            time.sleep(sleep_time)

    def wait_for_commit_slot(self, token: Optional[str] = None):
        """
        Enforces minimum spacing between Git commits to prevent secondary rate limits
        and git-lock collisions.
        """
        # First ensure general request slot is available
        self.wait_for_request_slot(token)

        key = self._get_token_key(token)
        commit_sleep = 0.0

        with self.lock:
            now = time.time()
            state = self._ensure_token_state(key)
            last_commit = state["last_commit_time"]
            elapsed = now - last_commit
            if elapsed < self.min_commit_spacing:
                commit_sleep = (self.min_commit_spacing - elapsed) + random.uniform(0.05, 0.25)
            state["last_commit_time"] = now + commit_sleep

        if commit_sleep > 0:
            time.sleep(commit_sleep)

    def trigger_cooldown(self, token: Optional[str], duration_sec: float, reason: str = ""):
        """
        Registers an explicit cooldown period for a token (e.g. upon encountering an HTTP 429).
        """
        key = self._get_token_key(token)
        with self.lock:
            until = time.time() + duration_sec
            self.throttled_until[key] = max(self.throttled_until.get(key, 0.0), until)
        print(f"[{time.strftime('%H:%M:%S')}] [HF Rate Limiter Cooldown] Pausing account [{key}] for {duration_sec:.1f}s. {reason}")

    def is_throttled(self, token: Optional[str]) -> bool:
        key = self._get_token_key(token)
        with self.lock:
            return time.time() < self.throttled_until.get(key, 0.0)

    def get_remaining_quota(self, token: Optional[str]) -> int:
        key = self._get_token_key(token)
        with self.lock:
            state = self._ensure_token_state(key)
            return state.get("remaining_quota", 1000)


class RemoteRepoCache:
    """
    Caches repository file listings in memory (TTL: 180s) to completely eliminate
    repeated 'api.file_exists()' HTTP requests during batch uploads.
    """
    _instance = None
    _singleton_lock = threading.Lock()

    @classmethod
    def get_instance(cls) -> "RemoteRepoCache":
        with cls._singleton_lock:
            if cls._instance is None:
                cls._instance = RemoteRepoCache()
            return cls._instance

    def __init__(self, ttl_sec: float = 180.0):
        self.ttl = ttl_sec
        self.lock = threading.Lock()
        # repo_id -> (set_of_file_paths, timestamp)
        self.repo_files: Dict[str, Tuple[Set[str], float]] = {}

    def get_repo_files(self, api: HfApi, repo_id: str, force_refresh: bool = False) -> Set[str]:
        now = time.time()
        with self.lock:
            cached = self.repo_files.get(repo_id)
            if not force_refresh and cached and (now - cached[1] < self.ttl):
                return set(cached[0])

        limiter = HFRateLimiter.get_instance()
        limiter.wait_for_request_slot(getattr(api, "token", None))

        try:
            files_list = api.list_repo_files(repo_id=repo_id, repo_type="dataset")
            files_set = set(files_list)
        except Exception:
            # If repo doesn't exist yet or is empty, return empty set
            files_set = set()

        with self.lock:
            self.repo_files[repo_id] = (files_set, time.time())
        return files_set

    def file_exists(self, api: HfApi, repo_id: str, filename: str) -> bool:
        """
        Fast O(1) in-memory check without sending HTTP requests to Hugging Face.
        """
        if not filename:
            return False
        files_set = self.get_repo_files(api, repo_id)
        return filename in files_set

    def record_added_files(self, repo_id: str, filenames: list):
        """
        Updates the in-memory cache when files have been successfully committed.
        """
        if not filenames:
            return
        with self.lock:
            cached = self.repo_files.get(repo_id)
            if cached:
                cached[0].update(filenames)
            else:
                self.repo_files[repo_id] = (set(filenames), time.time())

    def invalidate(self, repo_id: str):
        with self.lock:
            self.repo_files.pop(repo_id, None)


class DebouncedIndexSyncer:
    """
    Batches updates to 'vault_index.enc' across multiple completed video uploads.
    Instead of pushing 50 individual commits to 'vault_index.enc' when 50 files
    are synced, it waits until batch completion or a quiet period (15s) and pushes
    ONE unified master manifest commit.
    """
    def __init__(self, debounce_delay_sec: float = 15.0, max_interval_sec: float = 45.0):
        self.debounce_delay = debounce_delay_sec
        self.max_interval = max_interval_sec
        self.lock = threading.Lock()
        self.pending_manifest = None
        self.sync_func = None
        self.api = None
        self.master_repo = None
        self.password = None
        self.timer: Optional[threading.Timer] = None
        self.first_queued_time = 0.0

    def queue_master_sync(self, api: HfApi, master_repo: str, manifest: dict, password: str, sync_func):
        with self.lock:
            self.api = api
            self.master_repo = master_repo
            self.manifest = manifest
            self.password = password
            self.sync_func = sync_func

            now = time.time()
            if self.first_queued_time == 0.0:
                self.first_queued_time = now

            if self.timer:
                self.timer.cancel()
                self.timer = None

            # Check if max interval has passed
            if now - self.first_queued_time >= self.max_interval:
                # Flush immediately in background
                threading.Thread(target=self._flush, daemon=True).start()
                return

            self.timer = threading.Timer(self.debounce_delay, self._flush)
            self.timer.daemon = True
            self.timer.start()

    def flush_immediate(self):
        """Forces an immediate flush (e.g. when all files in the batch have finished)."""
        with self.lock:
            if self.timer:
                self.timer.cancel()
                self.timer = None
        self._flush()

    def _flush(self):
        with self.lock:
            self.timer = None
            self.first_queued_time = 0.0
            api = self.api
            master_repo = self.master_repo
            manifest = getattr(self, "manifest", None)
            password = self.password
            sync_func = self.sync_func

        if not api or not master_repo or not manifest or not sync_func:
            return

        limiter = HFRateLimiter.get_instance()
        token = getattr(api, "token", None)
        limiter.wait_for_commit_slot(token)

        try:
            print(f"[{time.strftime('%H:%M:%S')}] [Debounced Master Index] Syncing consolidated vault_index.enc to {master_repo}...")
            sync_func(api, master_repo, manifest, password)
        except Exception as e:
            print(f"[!] Warning during debounced master index sync: {e}")


def _install_hf_hub_hooks():
    """
    Hooks into huggingface_hub's httpx2 session event hooks to capture live
    RateLimit headers and 429 status codes automatically.
    """
    try:
        from huggingface_hub.utils import get_session
        session = get_session()
        if hasattr(session, "event_hooks") and isinstance(session.event_hooks.get("response"), list):
            def _hf_response_hook(response):
                try:
                    headers = response.headers
                    req = getattr(response, "request", None)
                    token = None
                    if req and hasattr(req, "headers"):
                        auth = req.headers.get("Authorization") or req.headers.get("authorization")
                        if auth and auth.startswith("Bearer "):
                            token = auth[7:].strip()

                    limiter = HFRateLimiter.get_instance()
                    limiter.update_from_headers(token, headers)

                    if response.status_code == 429:
                        # Extract wait time
                        ra = headers.get("Retry-After") or headers.get("retry-after")
                        rl = headers.get("RateLimit") or headers.get("ratelimit")
                        duration = 60.0
                        if ra and str(ra).strip().isdigit():
                            duration = float(str(ra).strip()) + 5.0
                        elif rl:
                            parsed = parse_ratelimit_header(str(rl))
                            t_val = parsed.get("t")
                            if t_val and str(t_val).isdigit():
                                duration = float(t_val) + 5.0
                        limiter.trigger_cooldown(token, duration, reason=f"HTTP 429 from {getattr(req, 'url', 'HF')}")
                except Exception:
                    pass

            # Only install once
            if _hf_response_hook not in session.event_hooks["response"]:
                session.event_hooks["response"].append(_hf_response_hook)
    except Exception:
        pass


# Install automatic response hooks on module load
_install_hf_hub_hooks()

# Singleton accessors
rate_limiter = HFRateLimiter.get_instance()
repo_cache = RemoteRepoCache.get_instance()
debounced_syncer = DebouncedIndexSyncer()
