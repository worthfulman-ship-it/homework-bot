"""
drive_journal.py — fast, thread-safe Drive primitives for the event journal.

drive_store.py's calls share ONE googleapiclient service, which isn't
thread-safe, and its upload path does a `list` + `update` (two round trips
of the whole file). The journal needs the opposite: many tiny single-RPC
writes, safe to run in parallel so a slow one can be hedged.

So this module keeps one Drive service PER THREAD (threading.local) and
runs everything on its own small thread pool. It reuses drive_store's
credentials/config and never raises: failures return None / are logged.
"""

from __future__ import annotations

import io
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

import drive_store

PREFIX = "ev-"

_pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="drive-j")
_tl = threading.local()


def _svc():
    svc = getattr(_tl, "svc", None)
    if svc is None:
        import google_auth_httplib2
        import httplib2
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            token=None,
            refresh_token=drive_store.REFRESH_TOKEN,
            client_id=drive_store.CLIENT_ID,
            client_secret=drive_store.CLIENT_SECRET,
            token_uri=drive_store.TOKEN_URI,
            scopes=drive_store.SCOPES,
        )
        http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=10))
        svc = build("drive", "v3", http=http, cache_discovery=False)
        _tl.svc = svc
    return svc


def prewarm() -> None:
    """Build each pool thread's service + fetch its access token now, so a
    hedged request later doesn't pay that cold-start cost."""
    def touch():
        try:
            _svc().files().list(pageSize=1, fields="files(id)").execute()
        except Exception as e:
            print(f"[journal] prewarm error: {e}")
    futs = [_pool.submit(touch) for _ in range(4)]
    wait(futs, timeout=15)


def create(name: str, data: bytes, parent_id: str) -> str | None:
    """ONE request: create a new file. No lookup, no whole-file rewrite.
    Returns the Drive file id, or None on any failure."""
    try:
        from googleapiclient.http import MediaIoBaseUpload
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype="application/json", resumable=False)
        meta = {"name": name, "parents": [parent_id]}
        return _svc().files().create(body=meta, media_body=media, fields="id").execute()["id"]
    except Exception as e:
        print(f"[journal] create error for {name}: {e}")
        return None


def create_hedged(name, data, parent_id, hedge_after=0.6, overall=20.0, on_extra=None) -> str | None:
    """Create with tail-latency hedging: if the first request hasn't finished
    after `hedge_after` seconds (or has failed), send an identical twin and
    take whichever succeeds first. Twins are harmless — the events inside
    carry unique keys and replay dedupes them.

    `on_extra(file_id)` is called for any twin that succeeds later, so the
    caller can track (and eventually delete) the extra file."""
    futs = [_pool.submit(create, name, data, parent_id)]
    t0 = time.monotonic()
    hedged = False

    def track_stragglers(skip=None):
        if on_extra:
            for g in futs:
                if g is not skip:
                    g.add_done_callback(lambda fut: fut.result() and on_extra(fut.result()))

    while True:
        for f in futs:
            if f.done() and f.result():
                track_stragglers(skip=f)
                return f.result()
        pending = [f for f in futs if not f.done()]
        elapsed = time.monotonic() - t0
        if elapsed >= overall or (hedged and not pending):
            track_stragglers()
            return None
        if not hedged and (elapsed >= hedge_after or not pending):
            futs.append(_pool.submit(create, name, data, parent_id))
            hedged = True
            continue
        timeout = (hedge_after - elapsed) if not hedged else (overall - elapsed)
        wait(pending, timeout=max(0.01, timeout), return_when=FIRST_COMPLETED)


def list_events(parent_id: str):
    """[(file_id, name)] of journal files, or None if the listing failed."""
    try:
        svc = _svc()
        out, token = [], None
        q = f"'{parent_id}' in parents and trashed = false and name contains '{PREFIX}'"
        while True:
            resp = svc.files().list(
                q=q, spaces="drive", pageSize=200, pageToken=token,
                fields="nextPageToken, files(id, name)",
            ).execute()
            out += [(f["id"], f["name"]) for f in resp.get("files", []) if f["name"].startswith(PREFIX)]
            token = resp.get("nextPageToken")
            if not token:
                return out
    except Exception as e:
        print(f"[journal] list error: {e}")
        return None


def fetch(file_id: str) -> bytes | None:
    try:
        from googleapiclient.http import MediaIoBaseDownload
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, _svc().files().get_media(fileId=file_id))
        done = False
        while not done:
            _, done = dl.next_chunk()
        return buf.getvalue()
    except Exception as e:
        print(f"[journal] fetch error for {file_id}: {e}")
        return None


def fetch_many(file_ids: list[str]) -> list[bytes | None]:
    return list(_pool.map(fetch, file_ids))


def delete_many(file_ids: list[str]) -> None:
    """Best-effort. A journal file that fails to delete is harmless: its
    events are older than the snapshot's folded_through and get skipped."""
    def rm(fid):
        try:
            _svc().files().delete(fileId=fid).execute()
        except Exception as e:
            print(f"[journal] delete error for {fid}: {e}")
    list(_pool.map(rm, file_ids))


def reinit_after_fork() -> None:
    """Child process after fork: the parent's pool threads and per-thread
    Drive services don't exist here, so start fresh."""
    global _pool, _tl
    _pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="drive-j")
    _tl = threading.local()
