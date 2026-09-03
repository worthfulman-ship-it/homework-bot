"""
store.py — task persistence for the homework tracker.

Uses drive_store.py (unchanged, copied from Hydra) for the actual Drive
I/O. This module owns the JSON shape and the local in-memory cache; Drive
is the source of truth and is synced down on first access, synced up after
every write. If Drive isn't configured (DRIVE_ENABLED is False), state
just lives in memory for the process lifetime — same fail-soft philosophy
as Hydra/attendance-bot.

JSON shape on disk (homework_tasks.json):

{
  "users": {
    "<discord_user_id>": {
      "alpha": 0.5,
      "tasks": [ {...task dict, see calc.Task...}, ... ]
    }
  }
}
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import drive_store
from calc import Task

FILE_NAME = "homework_tasks.json"
DEFAULT_ALPHA = 0.5

_folder_id: str | None = None
_state: dict | None = None  # loaded lazily, cached for process lifetime


def _ensure_loaded() -> dict:
    global _state, _folder_id
    if _state is not None:
        return _state

    if drive_store.DRIVE_ENABLED and _folder_id is None:
        _folder_id = drive_store.get_subfolder("homework-tracker")

    raw = drive_store.download_bytes(FILE_NAME, _folder_id) if _folder_id else None
    if raw:
        try:
            _state = json.loads(raw.decode("utf-8"))
        except Exception as e:
            print(f"[store] failed to parse {FILE_NAME}, starting fresh: {e}")
            _state = {"users": {}}
    else:
        _state = {"users": {}}
    return _state


def _save() -> None:
    global _folder_id
    if _state is None:
        return
    if drive_store.DRIVE_ENABLED and _folder_id is None:
        _folder_id = drive_store.get_subfolder("homework-tracker")
    if not _folder_id:
        return  # Drive not configured — in-memory only for this run
    data = json.dumps(_state, indent=2).encode("utf-8")
    ok = drive_store.upload_bytes(FILE_NAME, data, _folder_id, mime_type="application/json")
    if not ok:
        print("[store] Drive upload failed — changes kept in memory only for now")


def _user_record(user_id: str) -> dict:
    state = _ensure_loaded()
    users = state.setdefault("users", {})
    return users.setdefault(user_id, {"alpha": DEFAULT_ALPHA, "tasks": []})


def get_alpha(user_id: str) -> float:
    return _user_record(user_id).get("alpha", DEFAULT_ALPHA)


def set_alpha(user_id: str, alpha: float) -> None:
    rec = _user_record(user_id)
    rec["alpha"] = max(0.0, min(1.0, alpha))
    _save()


def get_tasks(user_id: str, include_done: bool = False) -> list[Task]:
    rec = _user_record(user_id)
    tasks = [Task.from_dict(d) for d in rec.get("tasks", [])]
    if include_done:
        return tasks
    return [t for t in tasks if t.status == "open"]


def get_done_tasks(user_id: str) -> list[Task]:
    rec = _user_record(user_id)
    tasks = [Task.from_dict(d) for d in rec.get("tasks", [])]
    return [t for t in tasks if t.status == "done"]


def add_task(task: Task) -> None:
    rec = _user_record(task.user_id)
    rec["tasks"].append(task.to_dict())
    _save()


def find_task(user_id: str, task_id: str) -> Task | None:
    rec = _user_record(user_id)
    for d in rec.get("tasks", []):
        if d["id"] == task_id or d["id"].startswith(task_id):
            return Task.from_dict(d)
    return None


def set_severity_override(user_id: str, task_id: str, value: float | None) -> bool:
    rec = _user_record(user_id)
    for d in rec.get("tasks", []):
        if d["id"] == task_id or d["id"].startswith(task_id):
            d["severity_override"] = value
            _save()
            return True
    return False


def mark_done(user_id: str, task_id: str) -> bool:
    rec = _user_record(user_id)
    for d in rec.get("tasks", []):
        if d["id"] == task_id or d["id"].startswith(task_id):
            d["status"] = "done"
            d["completed_at"] = datetime.now(timezone.utc).isoformat()
            _save()
            return True
    return False


def new_task_id() -> str:
    return uuid.uuid4().hex[:8]  # short id — enough to be typed in a slash command


def all_user_ids() -> list[str]:
    """Used by the daily-ping job to know who to DM."""
    return list(_ensure_loaded().get("users", {}).keys())
