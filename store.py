"""
store.py — task persistence for the homework tracker.

DURABILITY MODEL: snapshot + event journal on Drive
---------------------------------------------------
Every mutation (add / done / severity / alpha / last_ping) is an *event*:
a few hundred bytes with a unique key (ms timestamp, random id). The event
is applied to the in-memory state immediately and appended to Drive as its
own tiny file via ONE request (drive_journal.create_hedged — if that
request is slow, an identical twin is fired and the first to finish wins).

Drive therefore always holds:  snapshot (homework_tasks.json, with a
"folded_through" key)  +  journal files "ev-*.json" newer than that key.
Loading = read snapshot, replay newer events in key order, dedupe by key.
A background compactor folds the journal into a fresh snapshot and deletes
the folded files, so the journal stays short.

Why: the old model re-uploaded the whole JSON (a `list` + an `update`) on
every write, so a hard kill (SIGKILL/OOM, no SIGTERM, no atexit) while
Drive was slow could drop a write. Now the thing that must survive is one
small create request, hedged against tail latency, and commit() reports
honestly whether it landed.

Other guarantees kept from before:
  * handlers never block on Drive — they call commit(deadline) and, if it
    returns False, tell the user the write isn't confirmed yet;
  * a failed or unreadable load raises StorageUnavailable instead of
    starting empty (which would overwrite real history);
  * is_ready()/kick_warm() let handlers answer instantly during cold start.

Snapshot shape (homework_tasks.json):
{
  "users": {"<discord_user_id>": {"alpha": 0.5, "last_ping": "2026-09-10",
                                  "tasks": [ {...calc.Task dict...} ]}},
  "folded_through": [<ms>, "<id>"]     # absent in pre-journal files = (0, "")
}
"""

from __future__ import annotations

import atexit
import functools
import json
import threading
import time
import uuid
from datetime import datetime, timezone

import drive_journal
import drive_store
from calc import Task

FILE_NAME = "homework_tasks.json"
DEFAULT_ALPHA = 0.5
COMPACT_AT = 12          # fold the journal into the snapshot at this many files
WRITER_RETRY_S = 5

LOCK = threading.RLock()          # guards _state, _pending, counters
_load_lock = threading.Lock()     # serializes the Drive load only
_upload_lock = threading.Lock()   # one journal flush at a time (writer or flush())
_snap_lock = threading.Lock()     # one compaction at a time
_cv = threading.Condition()       # signals "flushed generation advanced"
_warm_guard = threading.Lock()

_folder_id: str | None = None
_state: dict | None = None
_gen = 0                          # bumped per mutation
_flushed_gen = 0                  # highest generation durably on Drive
_last_key: tuple = (0, "")        # key of the newest event applied in memory
_pending: list = []               # [(gen, event)] applied but not yet journaled
_journal_files: list = []         # [(max_key, drive_file_id)] not yet folded
_force_compact = False
_warming = False
_dirty = threading.Event()        # wakes the journal writer
_compact_evt = threading.Event()  # wakes the compactor


class StorageUnavailable(Exception):
    pass


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        with LOCK:
            return fn(*a, **kw)
    return wrapper


def _journaling() -> bool:
    return bool(drive_store.DRIVE_ENABLED and _folder_id)


# ---------- events ----------

def _apply(state: dict, ev: dict) -> None:
    """The ONLY place state is mutated. Used for live writes and for replay,
    so a replayed state is identical to the live one. Idempotent per event."""
    rec = state.setdefault("users", {}).setdefault(ev["uid"], {"alpha": DEFAULT_ALPHA, "tasks": []})
    op = ev["op"]
    if op == "add":
        if not any(d["id"] == ev["task"]["id"] for d in rec["tasks"]):
            rec["tasks"].append(ev["task"])
    elif op == "done":
        for d in rec["tasks"]:
            if d["id"] == ev["id"]:
                d["status"] = "done"
                d["completed_at"] = ev["at"]
                break
    elif op == "sev":
        for d in rec["tasks"]:
            if d["id"] == ev["id"]:
                d["severity_override"] = ev["v"]
                break
    elif op == "alpha":
        rec["alpha"] = ev["v"]
    elif op == "ping":
        rec["last_ping"] = ev["day"]


def _record(fields: dict) -> None:
    """Apply a mutation in memory and queue it for the journal. Caller holds LOCK."""
    global _gen, _last_key
    key = (max(int(time.time() * 1000), _last_key[0] + 1), uuid.uuid4().hex[:8])
    ev = {"k": list(key), **fields}
    _apply(_state, ev)
    _last_key = key
    _gen += 1
    if _journaling():
        _pending.append((_gen, ev))
        _dirty.set()
    else:  # Drive not configured: memory-only, nothing to wait for
        global _flushed_gen
        with _cv:
            _flushed_gen = _gen
            _cv.notify_all()


# ---------- loading ----------

def is_ready() -> bool:
    """Lock-free: True once state has been loaded."""
    return _state is not None


def _ensure_loaded() -> dict:
    global _state, _folder_id, _last_key, _journal_files, _force_compact
    if _state is not None:
        return _state
    with _load_lock:
        if _state is not None:
            return _state

        if not drive_store.DRIVE_ENABLED:
            _state = {"users": {}}
            return _state

        if _folder_id is None:
            _folder_id = drive_store.get_subfolder("homework-tracker")
        if not _folder_id:
            raise StorageUnavailable("couldn't reach the Drive folder")

        raw, status = drive_store.download_bytes_checked(FILE_NAME, _folder_id)
        if status == "error":
            raise StorageUnavailable("Drive download failed")
        if status == "not_found":
            loaded = {"users": {}}
        else:
            try:
                loaded = json.loads(raw.decode("utf-8"))
            except Exception as e:
                # Don't silently start fresh: the next snapshot would overwrite it.
                raise StorageUnavailable(f"{FILE_NAME} is unreadable: {e}")
        folded = tuple(loaded.pop("folded_through", None) or (0, ""))

        files = drive_journal.list_events(_folder_id)
        if files is None:
            raise StorageUnavailable("couldn't list the journal")
        blobs = drive_journal.fetch_many([fid for fid, _ in files])

        events, tracked, seen = [], [], set()
        for (fid, name), blob in zip(files, blobs):
            if blob is None:
                raise StorageUnavailable(f"couldn't read journal file {name}")
            try:
                batch = json.loads(blob.decode("utf-8"))["events"]
            except Exception as e:
                raise StorageUnavailable(f"journal file {name} is unreadable: {e}")
            keys = [tuple(e["k"]) for e in batch]
            tracked.append((max(keys, default=(0, "")), fid))
            for k, e in zip(keys, batch):
                if k > folded and k not in seen:  # skip folded events and hedge twins
                    seen.add(k)
                    events.append((k, e))
        events.sort(key=lambda x: x[0])
        for _, e in events:
            _apply(loaded, e)

        _last_key = max([folded] + [k for k, _ in events])
        _journal_files = tracked
        _force_compact = bool(tracked)
        _state = loaded
        if tracked:
            _compact_evt.set()
        if events:
            print(f"[store] replayed {len(events)} journal events")
        return _state


def warm() -> None:
    for attempt in range(5):
        try:
            _ensure_loaded()
            print("[store] state loaded")
            return
        except StorageUnavailable as e:
            print(f"[store] warm-up attempt {attempt + 1} failed: {e}")
            time.sleep(5)


def kick_warm() -> None:
    """Start a background load if needed; returns immediately (handler-safe)."""
    global _warming
    with _warm_guard:
        if _warming or _state is not None:
            return
        _warming = True

    def run():
        global _warming
        try:
            warm()
            if is_ready() and drive_store.DRIVE_ENABLED:
                drive_journal.prewarm()  # build per-thread services before a hedge needs them
        finally:
            _warming = False

    threading.Thread(target=run, daemon=True, name="store-warm").start()


# ---------- durable writes ----------

def _flush_pending() -> bool:
    """Journal everything pending as ONE hedged create. True on success or
    nothing to do."""
    global _flushed_gen
    with _upload_lock:
        with LOCK:
            batch = list(_pending)
        if not batch:
            return True
        first, last = batch[0][1]["k"], batch[-1][1]["k"]
        name = f"ev-{first[0]:013d}-{first[1]}.json"
        payload = json.dumps({"events": [e for _, e in batch]}).encode("utf-8")
        max_key = tuple(last)

        def track_twin(fid):  # a late-finishing hedge twin: remember it for cleanup
            with LOCK:
                _journal_files.append((max_key, fid))

        fid = drive_journal.create_hedged(name, payload, _folder_id, on_extra=track_twin)
        if not fid:
            return False
        with LOCK:
            del _pending[:len(batch)]  # batch is a prefix; new events were appended after it
            _journal_files.append((max_key, fid))
        with _cv:
            _flushed_gen = max(_flushed_gen, batch[-1][0])
            _cv.notify_all()
        return True


def _writer_loop() -> None:
    while True:
        _dirty.wait()
        _dirty.clear()
        try:
            ok = _flush_pending()
        except Exception as e:
            print(f"[store] flush error: {e}")
            ok = False
        if ok:
            _compact_evt.set()
        else:
            print("[store] journal write failed — will retry")
            _dirty.set()
            time.sleep(WRITER_RETRY_S)


def _compact() -> None:
    """Fold the journal into a fresh snapshot, then delete the folded files.
    Runs on its own thread so a slow snapshot upload can never delay a
    journal write."""
    global _force_compact
    with LOCK:
        if _state is None or not _journaling():
            return
        if len(_journal_files) < (1 if _force_compact else COMPACT_AT):
            return
        key, files = _last_key, list(_journal_files)
        snap = json.dumps(
            {"users": _state.get("users", {}), "folded_through": list(key)}, indent=2
        ).encode("utf-8")
        _force_compact = False
    if not _snap_lock.acquire(blocking=False):
        return
    try:
        if not drive_store.upload_bytes(FILE_NAME, snap, _folder_id, mime_type="application/json"):
            print("[store] snapshot upload failed — journal kept")
            return
        ids = [fid for _, fid in files]
        drive_journal.delete_many(ids)
        gone = set(ids)
        with LOCK:
            _journal_files[:] = [x for x in _journal_files if x[1] not in gone]
    finally:
        _snap_lock.release()


def _compactor_loop() -> None:
    while True:
        _compact_evt.wait()
        _compact_evt.clear()
        try:
            _compact()
        except Exception as e:
            print(f"[store] compaction error: {e}")


def commit(timeout: float = 1.8) -> bool:
    """Block until every mutation so far is durably journaled on Drive, or
    `timeout` seconds pass. False means "queued, not yet confirmed" — it is
    still retried in the background, but callers should say so honestly."""
    with LOCK:
        target = _gen
    deadline = time.monotonic() + timeout
    with _cv:
        while _flushed_gen < target:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print("[store] commit deadline hit — write not yet confirmed")
                return False
            _cv.wait(remaining)
    return True


def flush() -> None:
    """Shutdown hook (atexit): waits for any in-flight journal write, then
    journals whatever is still pending. One small request, so it fits easily
    inside the SIGTERM grace period."""
    for _ in range(3):
        try:
            if _flush_pending():
                return
        except Exception as e:
            print(f"[store] flush error: {e}")


threading.Thread(target=_writer_loop, daemon=True, name="store-writer").start()
threading.Thread(target=_compactor_loop, daemon=True, name="store-compactor").start()
atexit.register(flush)


# ---------- accessors ----------

def _user_record(user_id: str) -> dict:
    state = _ensure_loaded()
    return state.setdefault("users", {}).setdefault(user_id, {"alpha": DEFAULT_ALPHA, "tasks": []})


@_locked
def get_alpha(user_id: str) -> float:
    return _user_record(user_id).get("alpha", DEFAULT_ALPHA)


@_locked
def set_alpha(user_id: str, alpha: float) -> None:
    _user_record(user_id)
    _record({"op": "alpha", "uid": user_id, "v": max(0.0, min(1.0, alpha))})


@_locked
def get_tasks(user_id: str, include_done: bool = False) -> list[Task]:
    tasks = [Task.from_dict(d) for d in _user_record(user_id).get("tasks", [])]
    return tasks if include_done else [t for t in tasks if t.status == "open"]


@_locked
def get_done_tasks(user_id: str) -> list[Task]:
    tasks = [Task.from_dict(d) for d in _user_record(user_id).get("tasks", [])]
    return [t for t in tasks if t.status == "done"]


@_locked
def add_task(task: Task) -> None:
    _user_record(task.user_id)
    _record({"op": "add", "uid": task.user_id, "task": task.to_dict()})


@_locked
def find_task(user_id: str, task_id: str) -> Task | None:
    for d in _user_record(user_id).get("tasks", []):
        if d["id"] == task_id or d["id"].startswith(task_id):
            return Task.from_dict(d)
    return None


@_locked
def set_severity_override(user_id: str, task_id: str, value: float | None) -> bool:
    for d in _user_record(user_id).get("tasks", []):
        if d["id"] == task_id or d["id"].startswith(task_id):
            _record({"op": "sev", "uid": user_id, "id": d["id"], "v": value})
            return True
    return False


@_locked
def mark_done(user_id: str, task_id: str) -> bool:
    for d in _user_record(user_id).get("tasks", []):
        if d["status"] == "open" and (d["id"] == task_id or d["id"].startswith(task_id)):
            _record({"op": "done", "uid": user_id, "id": d["id"],
                     "at": datetime.now(timezone.utc).isoformat()})
            return True
    return False


def new_task_id() -> str:
    return uuid.uuid4().hex[:8]


@_locked
def all_user_ids() -> list[str]:
    return list(_ensure_loaded().get("users", {}).keys())


@_locked
def get_last_ping(user_id: str) -> str | None:
    return _user_record(user_id).get("last_ping")


@_locked
def set_last_ping(user_id: str, day_iso: str) -> None:
    _user_record(user_id)
    _record({"op": "ping", "uid": user_id, "day": day_iso})
