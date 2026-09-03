"""
drive_store.py — Google Drive persistence layer for Project Hydra.

Render's free tier wipes local disk on every restart/redeploy, so this
module makes Google Drive the source of truth for anything that needs to
survive that (the keyword discovery cache + virality_log.xlsx). Local disk
is still used as a fast scratch space during a single process's lifetime —
Drive is synced down once on first access and synced up after every write.

WHY OAUTH2 AND NOT A SERVICE ACCOUNT
-------------------------------------
Service accounts have zero Drive storage quota of their own. Sharing a
folder with one doesn't fix this — files it creates still count against
its own (empty) quota, not yours, so every upload fails with
`storageQuotaExceeded`. There's no setting that changes this for a
personal (non-Workspace) Google account; the real fixes (domain-wide
delegation, Shared Drives) require a Workspace org.

So this module authenticates as *you* instead, via OAuth2 with a
long-lived refresh token. Files then get created under your own quota,
exactly as if you'd uploaded them by hand.

ONE-TIME SETUP (done on your own machine, not on Render — Render has no
browser to show the consent screen):
    1. Google Cloud Console -> create an OAuth 2.0 Client ID of type
       "Desktop app". Download it as client_secret.json.
    2. On the OAuth consent screen, set publishing status to
       "In production" (NOT "Testing"). In Testing status, refresh tokens
       expire after 7 days, which would quietly break the bot on the
       server every week. drive.file is a non-sensitive scope, so going
       to production does not require Google's manual review process.
    3. Run get_refresh_token.py (in this same folder) locally with
       client_secret.json next to it. It opens a browser, you log in with
       your own Google account and approve access, and it prints a
       refresh token to your terminal.
    4. Set these three plus the folder id as Render env vars:
           GOOGLE_OAUTH_CLIENT_ID
           GOOGLE_OAUTH_CLIENT_SECRET
           GOOGLE_OAUTH_REFRESH_TOKEN
           DRIVE_FOLDER_ID     (share/create the target folder in your
                                 own Drive, then copy its id from the URL)

If any of these four env vars is missing, DRIVE_ENABLED is False and every
function in this module is a safe no-op (downloads return None, uploads
return False). The same happens if Drive errors at runtime for any reason
— network failure, bad/expired token, API rejection — every public
function here catches broadly and fails soft rather than raising, so a
Drive outage degrades the caller to "local-only for this run," never a
crash. Same fallback philosophy as the Xpoz -> Instaloader adapter switch
elsewhere in this project.
"""

import io
import os

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
TOKEN_URI = "https://oauth2.googleapis.com/token"

CLIENT_ID = os.getenv("GOOGLE_OAUTH_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_OAUTH_CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("GOOGLE_OAUTH_REFRESH_TOKEN")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")

DRIVE_ENABLED = bool(CLIENT_ID and CLIENT_SECRET and REFRESH_TOKEN and DRIVE_FOLDER_ID)

_service = None
_folder_cache: dict[str, str] = {}  # subfolder name -> id, memoized per process


def _get_service():
    global _service
    if _service is None:
        creds = Credentials(
            token=None,
            refresh_token=REFRESH_TOKEN,
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            token_uri=TOKEN_URI,
            scopes=SCOPES,
        )
        print('[Drive] building service')
        _service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _service


def _find_file(name: str, parent_id: str):
    """Return the file id of `name` inside `parent_id`, or None."""
    svc = _get_service()
    safe_name = name.replace("'", "\\'")
    q = f"name = '{safe_name}' and '{parent_id}' in parents and trashed = false"
    resp = svc.files().list(q=q, spaces="drive", fields="files(id, name)", pageSize=1).execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def get_subfolder(name: str) -> str | None:
    """Get-or-create a subfolder under DRIVE_FOLDER_ID, memoized for this process.
    Returns None (never raises) on any failure or if Drive isn't configured."""
    if not DRIVE_ENABLED:
        return None
    if name in _folder_cache:
        return _folder_cache[name]
    try:
        svc = _get_service()
        folder_id = _find_file(name, DRIVE_FOLDER_ID)
        if folder_id is None:
            meta = {
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [DRIVE_FOLDER_ID],
            }
            folder_id = svc.files().create(body=meta, fields="id").execute()["id"]
        _folder_cache[name] = folder_id
        return folder_id
    except Exception as e:
        print(f"[Drive] get_subfolder error for {name}: {e}")
        return None


def download_bytes(name: str, parent_id: str | None) -> bytes | None:
    """Return the raw bytes of `name` from Drive, or None on missing file,
    disabled config, or ANY error (network, auth, API) — never raises."""
    if not DRIVE_ENABLED or not parent_id:
        return None
    try:
        svc = _get_service()
        file_id = _find_file(name, parent_id)
        if file_id is None:
            return None
        request = svc.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue()
    except Exception as e:
        print(f"[Drive] download error for {name}: {e}")
        return None


def download_bytes_checked(name: str, parent_id: str | None) -> tuple[bytes | None, str]:
    """Like download_bytes(), but distinguishes WHY it returned no bytes.

    Returns (data, status) where status is one of:
      'found'     — file existed and downloaded successfully; data is bytes.
      'not_found' — Drive was reachable and confirmed the file genuinely
                    doesn't exist there; data is None.
      'error'     — Drive isn't configured, or the check itself failed
                    (network, auth/expired-token, API error); data is None,
                    and this does NOT mean the file is absent — it means we
                    couldn't determine that.

    Callers that are deciding whether it's safe to create-and-upload a fresh
    replacement file MUST treat 'error' differently from 'not_found' — see
    instagram_metrics.py's _sync_xlsx_from_drive_once() for why collapsing
    them (which is what plain download_bytes() does) caused real history to
    get silently overwritten by a blank file whenever the Drive check failed
    for a reason unrelated to the file actually being missing.
    """
    if not DRIVE_ENABLED or not parent_id:
        return None, "error"
    try:
        svc = _get_service()
        file_id = _find_file(name, parent_id)
        if file_id is None:
            return None, "not_found"
        request = svc.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue(), "found"
    except Exception as e:
        print(f"[Drive] download_checked error for {name}: {e}")
        return None, "error"


def upload_bytes(
    name: str,
    data: bytes,
    parent_id: str | None,
    mime_type: str = "application/octet-stream",
) -> bool:
    """Create or update `name` in `parent_id` with `data`. Returns success;
    never raises — any error (network, auth, quota, API) just returns False."""
    if not DRIVE_ENABLED or not parent_id:
        return False
    try:
        svc = _get_service()
        media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mime_type, resumable=False)
        file_id = _find_file(name, parent_id)
        if file_id:
            svc.files().update(fileId=file_id, media_body=media).execute()
        else:
            meta = {"name": name, "parents": [parent_id]}
            svc.files().create(body=meta, media_body=media, fields="id").execute()
        return True
    except Exception as e:
        print(f"[Drive] upload error for {name}: {e}")
        return False


def list_filenames(parent_id: str | None) -> list[str]:
    """List file names directly inside `parent_id`. Returns [] on any error."""
    if not DRIVE_ENABLED or not parent_id:
        return []
    try:
        svc = _get_service()
        q = f"'{parent_id}' in parents and trashed = false"
        names: list[str] = []
        page_token = None
        while True:
            resp = svc.files().list(
                q=q, spaces="drive", fields="nextPageToken, files(name)", pageToken=page_token
            ).execute()
            names.extend(f["name"] for f in resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return names
    except Exception as e:
        print(f"[Drive] list error: {e}")
        return []
