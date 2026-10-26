import os
import sys
import time
import json
import threading
from pathlib import Path

ACTIVITY_FILE = "live_activity.json"
ACTIVITY_LOCK = threading.RLock()
MAX_EVENTS = 10000


def _load_activity_raw():
    if os.path.exists(ACTIVITY_FILE):
        try:
            with open(ACTIVITY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, dict):
                    data = {}
                if "active_tasks" not in data or not isinstance(data.get("active_tasks"), dict):
                    data["active_tasks"] = {}
                if "recent_events" not in data or not isinstance(data.get("recent_events"), list):
                    data["recent_events"] = []
                if "watcher_status" not in data:
                    data["watcher_status"] = "idle"
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
        temp_file = f"{ACTIVITY_FILE}.tmp.{os.getpid()}_{int(time.time()*1000)}"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        # Robust replacement on Windows to survive concurrent readers
        replaced = False
        for _ in range(12):
            try:
                os.replace(temp_file, ACTIVITY_FILE)
                replaced = True
                break
            except (PermissionError, OSError):
                time.sleep(0.04)
        if not replaced:
            import shutil
            shutil.copyfile(temp_file, ACTIVITY_FILE)
            try: os.remove(temp_file)
            except Exception: pass
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
        """Initializes or updates the current sync batch count safely."""
        with self._lock:
            state = _load_activity_raw()
            if total_files > 0:
                cur_total = state.get("batch_total", 0)
                cur_done = state.get("batch_completed", 0)
                if cur_total == 0 or cur_done >= cur_total or total_files != cur_total:
                    state["batch_total"] = total_files
                    state["batch_completed"] = 0
                elif total_files > cur_total:
                    state["batch_total"] = total_files
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
                hist = task.setdefault("stage_history", [])
                if not hist or hist[-1].get("stage") != stage or hist[-1].get("details") != details:
                    hist.append({
                        "stage": stage,
                        "stage_name": stage_name,
                        "percent": percent,
                        "details": details,
                        "speed": speed_str,
                        "time": time.strftime("%H:%M:%S")
                    })
                    if len(hist) > 25:
                        hist.pop(0)
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

            # Append completion step to task history
            if task_id in state["active_tasks"]:
                hist = state["active_tasks"][task_id].setdefault("stage_history", [])
                hist.append({
                    "stage": final_stage,
                    "stage_name": state["active_tasks"][task_id].get("stage_name", "Completed"),
                    "percent": percent,
                    "details": message,
                    "speed": "",
                    "time": time.strftime("%H:%M:%S")
                })

            # Retain completed tasks in memory so user can inspect all finished tasks
            self._prune_completed_tasks(state, older_than_sec=7200.0)
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

    def reset(self):
        with self._lock:
            state = {
                "active_tasks": {},
                "recent_events": [],
                "watcher_status": "idle",
                "batch_total": 0,
                "batch_completed": 0,
                "total_completed_count": 0,
                "manifest_version": 1,
                "last_updated": time.time()
            }
            _save_activity_raw(state)

    def set_idle(self):
        with self._lock:
            state = _load_activity_raw()
            active_count = sum(1 for t in state["active_tasks"].values() if t.get("stage") not in ("completed", "staged", "skipped_duplicate", "error"))
            if active_count == 0:
                state["watcher_status"] = "idle"
            self._prune_completed_tasks(state, older_than_sec=60.0)
            _save_activity_raw(state)

    def _prune_completed_tasks(self, state, older_than_sec: float = 7200.0):
        now = time.time()
        to_del = []
        for tid, t in list(state.get("active_tasks", {}).items()):
            stage = t.get("stage")
            updated = t.get("updated_at", now)
            # Prune completed or errored tasks after older_than_sec (2 hours)
            if stage in ("completed", "staged", "skipped_duplicate", "error"):
                fin = t.get("finished_at", updated)
                if now - fin > older_than_sec:
                    to_del.append(tid)
            # Prune abandoned tasks
            elif now - updated > 180.0:
                to_del.append(tid)

        # Cap memory to maximum 500 completed tasks
        completed_keys = [tid for tid, t in state.get("active_tasks", {}).items() if t.get("stage") in ("completed", "staged", "skipped_duplicate")]
        if len(completed_keys) > 500:
            for tid in completed_keys[:-500]:
                to_del.append(tid)

        for tid in to_del:
            del state["active_tasks"][tid]

    def get_state(self):
        with self._lock:
            state = _load_activity_raw()
            self._prune_completed_tasks(state, older_than_sec=7200.0)

            # Enrich with staged files from staging manifest so finished tasks from this batch are always visible with full metadata
            stg_path = os.path.join("staging", "staging_manifest.json")
            if os.path.exists(stg_path):
                try:
                    with open(stg_path, "r", encoding="utf-8") as sf:
                        stg_data = json.load(sf)
                    all_repos = stg_data.get("repositories", {})
                    # Isolate the current active staging batch (avoid counting previous already-staged folders)
                    active_rf_keys = [rf for rf, rd in all_repos.items() if rd.get("status") == "staging"]
                    if not active_rf_keys:
                        # If no repository has status 'staging', pick ONLY the single most recently active ready_to_push folder
                        ready_keys = [rf for rf, rd in all_repos.items() if rd.get("status") == "ready_to_push"]
                        if ready_keys:
                            active_rf_keys = [max(ready_keys, key=lambda k: all_repos[k].get("updated_at", 0))]

                    ready_repos_summary = []
                    for rf, rd in all_repos.items():
                        if rd.get("status") == "ready_to_push":
                            flen = len(rd.get("files", []))
                            osz = rd.get("total_orig_size", 0)
                            mb = osz / (1024 * 1024)
                            sz_str = f"{mb:.2f} MB" if mb < 1024 else f"{mb / 1024:.2f} GB"
                            thumbs = [
                                f"/api/screens/thumb?id={f.get('safe_id')}"
                                for f in rd.get("files", [])[:4]
                                if f.get("safe_id")
                            ]
                            ready_repos_summary.append({
                                "repo_folder": rf,
                                "repo_id": rd.get("repo_id", ""),
                                "account": rd.get("account", ""),
                                "file_count": flen,
                                "size_formatted": sz_str,
                                "status": "ready_to_push",
                                "preview_thumbs": thumbs
                            })
                    state["ready_repos_summary"] = ready_repos_summary

                    target_repos = {rf: all_repos[rf] for rf in active_rf_keys if rf in all_repos}
                    valid_task_ids = set()

                    for rf, rdata in target_repos.items():
                        for f in rdata.get("files", []):
                            fk = f.get("full_key")
                            if fk:
                                valid_task_ids.add(fk)
                                rec = f.get("record", {})
                                sz = f.get("orig_size", 0)
                                mb = sz / (1024 * 1024)
                                sz_str = f"{mb:.2f} MB" if mb < 1024 else f"{mb / 1024:.2f} GB"
                                if fk in state.get("active_tasks", {}):
                                    t = state["active_tasks"][fk]
                                    if not t.get("media_info"):
                                        t["media_info"] = rec.get("media_info", {})
                                    if not t.get("audio_info"):
                                        t["audio_info"] = rec.get("audio", {})
                                    if not t.get("storyboard_info"):
                                        t["storyboard_info"] = rec.get("storyboard", {})
                                    if not t.get("verifier"):
                                        t["verifier"] = rec.get("verifier", "")
                                    if not t.get("blob_path"):
                                        t["blob_path"] = f.get("blob_path", rec.get("blob_path", ""))
                                    if not t.get("account"):
                                        t["account"] = rec.get("account", rdata.get("account", ""))
                                    if not t.get("repo_id"):
                                        t["repo_id"] = rec.get("repo_id", rdata.get("repo_id", ""))
                                else:
                                    state["active_tasks"][fk] = {
                                        "id": fk,
                                        "filename": f.get("orig_filename") or os.path.basename(fk),
                                        "file_size": sz,
                                        "size_str": sz_str,
                                        "stage": "staged",
                                        "stage_name": "Staged",
                                        "percent": 100,
                                        "details": "Pre-encrypted & staged! Ready for batch push",
                                        "speed_str": "",
                                        "account": rec.get("account", rdata.get("account", "")),
                                        "repo_id": rec.get("repo_id", rdata.get("repo_id", "")),
                                        "blob_path": f.get("blob_path", rec.get("blob_path", "")),
                                        "media_info": rec.get("media_info", {}),
                                        "audio_info": rec.get("audio", {}),
                                        "storyboard_info": rec.get("storyboard", {}),
                                        "verifier": rec.get("verifier", ""),
                                        "started_at": rec.get("updated_at", time.time()),
                                        "finished_at": rec.get("updated_at", time.time()),
                                        "elapsed_sec": 0
                                    }

                    # Prune older completed tasks from other repositories to prevent over-counting
                    if target_repos:
                        keys_to_remove = []
                        for tid, t in list(state.get("active_tasks", {}).items()):
                            if t.get("stage") in ("completed", "staged", "skipped_duplicate"):
                                if tid not in valid_task_ids and not any(tid.startswith(rf) for rf in target_repos):
                                    keys_to_remove.append(tid)
                        for tid in keys_to_remove:
                            del state["active_tasks"][tid]

                    # Synchronize full event log for every staged file in this session
                    existing_event_titles = {e.get("title", "") for e in state.get("recent_events", [])}
                    for tid, t in state.get("active_tasks", {}).items():
                        if t.get("stage") in ("staged", "completed"):
                            fname = t.get("filename", "")
                            base_name = os.path.basename(fname)
                            evt_title = f"Staged: {base_name}"
                            if evt_title not in existing_event_titles and fname not in existing_event_titles:
                                t_time = time.strftime("%H:%M:%S", time.localtime(t.get("finished_at", time.time())))
                                state["recent_events"].append({
                                    "id": f"ev_{int(time.time() * 1000)}_{len(state['recent_events'])}",
                                    "time": t_time,
                                    "type": "success",
                                    "title": evt_title,
                                    "message": t.get("details") or "Pre-encrypted & staged! Ready for batch push",
                                    "account": t.get("account", ""),
                                    "speed": t.get("speed_str", "")
                                })
                                existing_event_titles.add(evt_title)
                except Exception:
                    pass

            # Calculate mathematically consistent batch metrics
            active_tasks = state.get("active_tasks", {})
            in_flight = [t for t in active_tasks.values() if t.get("stage") not in ("completed", "staged", "skipped_duplicate", "error")]
            in_flight_count = len(in_flight)
            finished_count = sum(1 for t in active_tasks.values() if t.get("stage") in ("completed", "staged", "skipped_duplicate"))

            # Precalculate total initially and dynamically based on content on main root tree folder
            precalculated_total = 0
            if target_repos:
                for rf, rdata in target_repos.items():
                    init_scan = rdata.get("initial_scan") or {}
                    v_disc = init_scan.get("video_files_discovered", 0)
                    if v_disc > 0:
                        precalculated_total += v_disc
                    else:
                        watch_folder = os.path.join("videos_to_sync", rf)
                        if os.path.exists(watch_folder):
                            disk_vids = sum(1 for root, _, fs in os.walk(watch_folder) for f in fs if not f.endswith(('.tmp.enc', '.enc', '.tmp', '.companion.m4a', '.nfo', '.strm')) and not f.startswith('tmp_'))
                            staged_in_repo = len(rdata.get("files", []))
                            precalculated_total += (disk_vids + staged_in_repo)

            if precalculated_total > 0:
                batch_total = max(precalculated_total, finished_count + in_flight_count)
            elif target_repos and len(valid_task_ids) > 0:
                batch_total = max(len(valid_task_ids), finished_count + in_flight_count)
            else:
                batch_total = finished_count + in_flight_count
            if target_repos:
                actual_finished = sum(len(rdata.get("files", [])) for rdata in target_repos.values())
                for tid, t in active_tasks.items():
                    if t.get("stage") in ("completed", "staged", "skipped_duplicate"):
                        if tid not in valid_task_ids and any(tid.startswith(rf) for rf in target_repos):
                            actual_finished += 1
            else:
                actual_finished = finished_count

            if in_flight_count > 0:
                # While tasks are actively running, completed count MUST be strictly less than total
                batch_completed = min(batch_total - in_flight_count, actual_finished)
                if batch_completed < 0:
                    batch_completed = 0
                watcher_status = "processing"
            else:
                batch_completed = min(batch_total, actual_finished)
                watcher_status = state.get("watcher_status", "idle")

            state["batch_total"] = batch_total
            state["batch_completed"] = batch_completed
            state["in_flight_count"] = in_flight_count
            state["watcher_status"] = watcher_status

            return state


tracker = ActivityTracker()
