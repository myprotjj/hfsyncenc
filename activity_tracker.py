import os
import sys
import time
import json
import threading
from pathlib import Path

ACTIVITY_FILE = "live_activity.json"
ACTIVITY_LOCK = threading.RLock()
MAX_EVENTS = 100


def _load_activity_raw():
    if os.path.exists(ACTIVITY_FILE):
        try:
            with open(ACTIVITY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Ensure monotonic counters exist
                if "total_completed_count" not in data:
                    data["total_completed_count"] = 0
                if "manifest_version" not in data:
                    data["manifest_version"] = 1
                if "batch_total" not in data:
                    data["batch_total"] = 0
                if "batch_completed" not in data:
                    data["batch_completed"] = 0
                return data
        except Exception:
            pass
    return {
        "active_tasks": {},
        "recent_events": [],
        "watcher_status": "idle",
        "batch_total": 0,
        "batch_completed": 0,
        "total_completed_count": 0,
        "manifest_version": 1,
        "last_updated": time.time()
    }


def _save_activity_raw(data):
    try:
        data["last_updated"] = time.time()
        temp_file = f"{ACTIVITY_FILE}.tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(temp_file, ACTIVITY_FILE)
    except Exception as e:
        sys.stderr.write(f"[!] Warning saving activity state: {e}\n")


class ActivityTracker:
    def __init__(self):
        self._lock = ACTIVITY_LOCK

    def set_watcher_status(self, status: str = "active"):
        with self._lock:
            state = _load_activity_raw()
            state["watcher_status"] = status
            _save_activity_raw(state)

    def start_batch(self, total_files: int):
        """Initializes or expands the current sync batch count."""
        with self._lock:
            state = _load_activity_raw()
            if total_files > 0:
                cur_total = state.get("batch_total", 0)
                cur_done = state.get("batch_completed", 0)
                if cur_total == 0 or cur_done >= cur_total:
                    state["batch_total"] = total_files
                    state["batch_completed"] = 0
                else:
                    state["batch_total"] = max(cur_total, cur_done + total_files)
                state["watcher_status"] = "processing"
                _save_activity_raw(state)

    def start_task(self, task_id: str, filename: str, file_size: int, account: str = "", repo_id: str = ""):
        with self._lock:
            state = _load_activity_raw()
            size_mb = file_size / (1024 * 1024)
            size_str = f"{size_mb:.2f} MB" if size_mb < 1024 else f"{size_mb / 1024:.2f} GB"

            state["active_tasks"][task_id] = {
                "id": task_id,
                "filename": filename,
                "file_size": file_size,
                "size_str": size_str,
                "stage": "detected",
                "stage_name": "New Video Detected",
                "percent": 5,
                "speed_str": "",
                "account": account,
                "repo_id": repo_id,
                "started_at": time.time(),
                "updated_at": time.time(),
                "details": "Preparing ingestion pipeline..."
            }
            state["watcher_status"] = "processing"
            self._prune_completed_tasks(state, older_than_sec=60.0)
            _save_activity_raw(state)

    def update_stage(self, task_id: str, stage: str, stage_name: str, percent: int, speed_str: str = "", details: str = "", account: str = None, repo_id: str = None):
        with self._lock:
            state = _load_activity_raw()
            if task_id in state["active_tasks"]:
                task = state["active_tasks"][task_id]
                task["stage"] = stage
                task["stage_name"] = stage_name
                task["percent"] = max(0, min(100, percent))
                if speed_str:
                    task["speed_str"] = speed_str
                if details:
                    task["details"] = details
                if account:
                    task["account"] = account
                if repo_id:
                    task["repo_id"] = repo_id
                task["updated_at"] = time.time()
                task["elapsed_sec"] = round(time.time() - task.get("started_at", time.time()), 1)
            else:
                state["active_tasks"][task_id] = {
                    "id": task_id,
                    "filename": task_id,
                    "stage": stage,
                    "stage_name": stage_name,
                    "percent": percent,
                    "speed_str": speed_str,
                    "details": details,
                    "account": account or "",
                    "repo_id": repo_id or "",
                    "started_at": time.time(),
                    "updated_at": time.time(),
                    "elapsed_sec": 0
                }
            state["watcher_status"] = "processing"
            _save_activity_raw(state)

    def finish_task(self, task_id: str, final_stage: str, message: str, percent: int = 100):
        with self._lock:
            state = _load_activity_raw()
            if task_id in state["active_tasks"]:
                task = state["active_tasks"][task_id]
                task["stage"] = final_stage
                task["stage_name"] = "Completed" if final_stage == "completed" else final_stage.replace("_", " ").title()
                task["percent"] = percent
                task["details"] = message
                task["updated_at"] = time.time()
                task["finished_at"] = time.time()
                task["elapsed_sec"] = round(time.time() - task.get("started_at", time.time()), 1)

            # Monotonic counters
            state["total_completed_count"] = state.get("total_completed_count", 0) + 1
            state["manifest_version"] = state.get("manifest_version", 0) + 1
            if state.get("batch_total", 0) > 0:
                state["batch_completed"] = min(state["batch_total"], state.get("batch_completed", 0) + 1)

            # Check if any tasks are still running
            active_count = sum(1 for t in state["active_tasks"].values() if t.get("stage") not in ("completed", "skipped_duplicate", "error"))
            if active_count == 0:
                state["watcher_status"] = "idle"
                if state.get("batch_total", 0) > 0:
                    state["batch_completed"] = state["batch_total"]

            self._prune_completed_tasks(state, older_than_sec=60.0)
            _save_activity_raw(state)

    def notify_manifest_updated(self):
        with self._lock:
            state = _load_activity_raw()
            state["manifest_version"] = state.get("manifest_version", 0) + 1
            _save_activity_raw(state)

    def add_event(self, event_type: str, title: str, message: str, account: str = "", speed: str = ""):
        with self._lock:
            state = _load_activity_raw()
            ev = {
                "id": f"ev_{int(time.time() * 1000)}",
                "time": time.strftime("%H:%M:%S"),
                "type": event_type,  # "success", "upload", "encrypt", "skip", "error", "info"
                "title": title,
                "message": message,
                "account": account,
                "speed": speed
            }
            state["recent_events"].insert(0, ev)
            if len(state["recent_events"]) > MAX_EVENTS:
                state["recent_events"] = state["recent_events"][:MAX_EVENTS]
            _save_activity_raw(state)

    def clear_events(self):
        with self._lock:
            state = _load_activity_raw()
            state["recent_events"] = []
            _save_activity_raw(state)

    def set_idle(self):
        with self._lock:
            state = _load_activity_raw()
            active_count = sum(1 for t in state["active_tasks"].values() if t.get("stage") not in ("completed", "skipped_duplicate", "error"))
            if active_count == 0:
                state["watcher_status"] = "idle"
                if state.get("batch_total", 0) > 0:
                    state["batch_completed"] = state["batch_total"]
            self._prune_completed_tasks(state, older_than_sec=60.0)
            _save_activity_raw(state)

    def _prune_completed_tasks(self, state, older_than_sec: float = 60.0):
        now = time.time()
        to_del = []
        for tid, t in state.get("active_tasks", {}).items():
            if t.get("stage") in ("completed", "skipped_duplicate", "error"):
                fin = t.get("finished_at", t.get("updated_at", now))
                if now - fin > older_than_sec:
                    to_del.append(tid)
        for tid in to_del:
            del state["active_tasks"][tid]

    def get_state(self):
        with self._lock:
            state = _load_activity_raw()
            self._prune_completed_tasks(state, older_than_sec=60.0)
            return state


tracker = ActivityTracker()
