"""
app.py — Homework tracker, HTTP-interactions edition (no gateway login).

Why: Render's free tier shares outbound IPs and Cloudflare sometimes bans
them in front of discord.com (Error 1015). A gateway bot must log in on
every process start, so a restart during a ban meant hours of downtime.
Here Discord POSTs slash commands and button taps to /interactions and the
reply is the HTTP response, so there is no gateway and no login. The only
outbound Discord calls are the daily DMs, which are retried with a
cooldown.

Routes:
  GET  /                 health check (UptimeRobot monitor #1)
  POST /interactions     Discord interactions endpoint (signature-verified)
  GET  /cron/ping?key=.. daily DM job (UptimeRobot monitor #2, every 5 min).
                         Idempotent: sends only after 17:00 IST and only
                         once per user per IST day. Returns 200 always
                         (except bad key / unknown job).

Env vars:
  DISCORD_BOT_TOKEN, DISCORD_PUBLIC_KEY, CRON_KEY (alphanumeric only),
  GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET,
  GOOGLE_OAUTH_REFRESH_TOKEN, DRIVE_FOLDER_ID

Run (one worker — state lives in memory):
  gunicorn -w 1 --threads 8 --graceful-timeout 25 -b 0.0.0.0:$PORT app:app
"""

from __future__ import annotations

import hmac
import os
import sys
import threading
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

import store
from calc import SEVERITY_PRESETS, Task, days_overdue, days_until_due, due_today, is_overdue, order_tasks
from date_parse import parse_due_date

# Under gunicorn stdout is a pipe, so print() is block-buffered and log lines
# can be delayed or never appear. Flush every line.
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
PUBLIC_KEY = os.getenv("DISCORD_PUBLIC_KEY")
CRON_KEY = os.getenv("CRON_KEY")
IST = ZoneInfo("Asia/Kolkata")
PING_HOUR_IST = 17  # 5pm IST

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (https://github.com/homework-bot, 1.0)"

# Discord error code: "Cannot send messages to this user" (DMs closed, no
# shared server, or the user blocked the bot). Permanent, so don't retry.
CANNOT_DM_USER = 50007

app = Flask(__name__)
_verify_key = VerifyKey(bytes.fromhex(PUBLIC_KEY)) if PUBLIC_KEY else None


def today_ist() -> date:
    # Render runs in UTC; "tomorrow" must mean tomorrow in IST.
    return datetime.now(IST).date()


# ---------- formatting helpers ----------

def _fit(lines: list[str], limit: int = 1000) -> str:
    """Embed field values cap at 1024 chars."""
    out, used = [], 0
    for i, line in enumerate(lines):
        if used + len(line) + 1 > limit:
            out.append(f"…and {len(lines) - i} more")
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def _task_line(t: Task, today: date, show_flag: bool = True) -> str:
    sev = ""
    if t.severity_override is not None:
        for label, val in SEVERITY_PRESETS.items():
            if val == t.severity_override:
                sev = f", severity: {label}"
                break

    if not show_flag:
        return f"`{t.id}` **{t.subject}** — {t.title} ({t.length} items{sev})"

    if is_overdue(t, today):
        flag = f"⚠️ {days_overdue(t, today)}d overdue"
    else:
        d = days_until_due(t, today)
        flag = "due today" if d == 0 else f"due in {d}d"
    return f"`{t.id}` **{t.subject}** — {t.title} ({t.length} items, {flag}{sev})"


def _list_embed(user_id: str, title: str, today: date) -> dict:
    alpha = store.get_alpha(user_id)
    ordered = order_tasks(store.get_tasks(user_id), alpha=alpha, today=today)

    embed: dict = {"title": title, "color": 0x5865F2}
    if not ordered:
        embed["description"] = "Nothing open. 🎉"
        return embed

    overdue = [t for t in ordered if is_overdue(t, today)]
    upcoming = [t for t in ordered if not is_overdue(t, today)]
    fields = []
    if overdue:
        fields.append({"name": "Overdue", "value": _fit([_task_line(t, today) for t in overdue]), "inline": False})
    if upcoming:
        fields.append({"name": "Upcoming", "value": _fit([_task_line(t, today) for t in upcoming]), "inline": False})
    embed["fields"] = fields
    embed["footer"] = {"text": f"priority weight α={alpha:.2f}  •  /hw-weight to tune"}
    return embed


def _msg(content: str | None = None, embeds: list | None = None) -> dict:
    """Interaction response (type 4), always ephemeral (flag 64)."""
    data: dict = {"flags": 64}
    if content:
        data["content"] = content
    if embeds:
        data["embeds"] = embeds
    return {"type": 4, "data": data}


# ---------- command handlers (all in-memory fast; Drive writes are async) ----------

def cmd_add(uid: str, o: dict, today: date) -> dict:
    due = parse_due_date(o["due_date"], today=today)
    if due is None:
        return _msg(
            "Couldn't understand that due date. Try things like `next thursday`, "
            "`tomorrow`, `fri`, `in 5 days`, or `2026-09-10`."
        )
    task = Task(
        id=store.new_task_id(),
        user_id=uid,
        subject=o["subject"],
        title=o["title"],
        due_date=due,
        length=int(o["length"]),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    store.add_task(task)
    return _msg(f"Added `{task.id}` — **{task.subject}**: {task.title} (due {due.isoformat()}, {task.length} items).")


def cmd_list(uid: str, o: dict, today: date) -> dict:
    return _msg(embeds=[_list_embed(uid, "Open homework", today)])


def cmd_done(uid: str, o: dict, today: date) -> dict:
    task_id = o["task_id"]
    if store.mark_done(uid, task_id):
        return _msg(f"Marked `{task_id}` done. Nice.")
    return _msg(f"No open task matching `{task_id}`.")


def cmd_log(uid: str, o: dict, today: date) -> dict:
    done = store.get_done_tasks(uid)
    if not done:
        return _msg("No completed homework logged yet.")
    done.sort(key=lambda t: t.completed_at or "", reverse=True)
    lines = [
        f"`{t.id}` **{t.subject}** — {t.title} (done {t.completed_at[:10] if t.completed_at else '?'})"
        for t in done
    ]
    return _msg(embeds=[{"title": "Completed homework", "description": "\n".join(lines[:25]), "color": 0x57F287}])


def cmd_severity(uid: str, o: dict, today: date) -> dict:
    task_id, level = o["task_id"], o["level"]
    value = None if level == "auto" else SEVERITY_PRESETS.get(level)
    if level != "auto" and value is None:
        return _msg(f"Unknown level `{level}`.")
    if not store.set_severity_override(uid, task_id, value):
        return _msg(f"No task matching `{task_id}`.")
    if value is None:
        return _msg(f"`{task_id}` severity reset to auto (by due date).")
    return _msg(f"`{task_id}` severity set to **{level}**.")


def cmd_weight(uid: str, o: dict, today: date) -> dict:
    alpha = float(o["alpha"])
    if not 0.0 <= alpha <= 1.0:
        return _msg("alpha must be between 0.0 and 1.0.")
    store.set_alpha(uid, alpha)
    return _msg(f"Priority weighting set to α={alpha:.2f}.")


# Commands that change data. The reply waits (up to COMMIT_DEADLINE seconds,
# inside Discord's 3s window) until the change is durably on Drive, so a
# redeploy right after "Added ..." can't lose it.
MUTATING = {"hw-add", "hw-done", "hw-severity", "hw-weight"}
COMMIT_DEADLINE = 1.8

COMMANDS = {
    "hw-add": cmd_add,
    "hw-list": cmd_list,
    "hw-done": cmd_done,
    "hw-log": cmd_log,
    "hw-severity": cmd_severity,
    "hw-weight": cmd_weight,
}


# ---------- Discord entry point ----------

def _signature_ok() -> bool:
    if _verify_key is None:
        return False
    sig = request.headers.get("X-Signature-Ed25519")
    ts = request.headers.get("X-Signature-Timestamp")
    if not sig or not ts:
        return False
    try:
        _verify_key.verify(ts.encode() + request.get_data(), bytes.fromhex(sig))
        return True
    except (BadSignatureError, ValueError):
        return False


@app.post("/interactions")
def interactions():
    if not _signature_ok():
        return "invalid request signature", 401

    body = request.get_json(silent=True) or {}
    itype = body.get("type")

    if itype == 1:  # PING
        return jsonify({"type": 1})

    user = (body.get("member") or {}).get("user") or body.get("user") or {}
    uid = str(user.get("id", ""))
    data = body.get("data") or {}

    if itype in (2, 3) and not store.is_ready():
        # Cold start: state is still loading from Drive. Answer instantly
        # instead of blocking past Discord's 3s window.
        store.kick_warm()
        err = store.last_error()
        if err:
            return jsonify(_msg(f"Storage isn't loading: {err}. Check the Render logs for [store]/[journal]/[Drive] lines."))
        return jsonify(_msg("Waking up — give it a few seconds and try again."))

    try:
        today = today_ist()
        if itype == 2:  # slash command
            name = data.get("name")
            fn = COMMANDS.get(name)
            if fn is None:
                return jsonify(_msg("Unknown command."))
            opts = {x["name"]: x["value"] for x in data.get("options", [])}
            resp = fn(uid, opts, today)
            if name in MUTATING and not store.commit(COMMIT_DEADLINE):
                # Honest ack: don't claim "saved" when Drive hasn't confirmed.
                # The write is queued and keeps retrying in the background.
                d = resp["data"]
                d["content"] = (d.get("content") or "") + (
                    "\n⚠️ Drive hasn't confirmed this yet — it's queued and retrying. "
                    "Re-check `/hw-list` in a minute."
                )
            return jsonify(resp)
        if itype == 3:
            # The only button this app ever sends is "View all open tasks",
            # so accept ANY custom_id. That keeps buttons on DMs sent by the
            # old gateway version (random auto-generated ids) working too.
            return jsonify(_msg(embeds=[_list_embed(uid, "All open homework", today)]))
    except store.StorageUnavailable as e:
        print(f"[interactions] storage unavailable: {e}")
        return jsonify(_msg("Storage is temporarily unavailable — try again in a moment."))
    except Exception as e:
        print(f"[interactions] handler error: {e!r}")
        return jsonify(_msg("Something went wrong on my end."))

    return jsonify(_msg("Unsupported interaction."))


# ---------- daily ping (REST, outbound) ----------

_ping_lock = threading.Lock()
_cooldown_until = 0.0
_dm_channels: dict[str, str] = {}
# uid -> IST day (iso) on which Discord said we can't DM them. In memory only:
# a restart gives that user one more attempt, which is harmless.
_undeliverable: dict[str, str] = {}


class DiscordError(Exception):
    def __init__(self, msg: str, code: int | None = None):
        super().__init__(msg)
        self.code = code  # Discord's JSON error code, e.g. 50007


def _discord(method: str, path: str, **kw):
    """Outbound REST call. A non-JSON error response is Cloudflare's block
    page (Error 1015), so back off for an hour; a JSON 429 is a normal rate
    limit and backs off for its retry_after."""
    global _cooldown_until
    r = requests.request(
        method,
        API + path,
        headers={"Authorization": f"Bot {TOKEN}", "User-Agent": USER_AGENT},
        timeout=10,
        **kw,
    )
    is_json = "json" in r.headers.get("content-type", "")
    if r.status_code >= 400 and not is_json:
        _cooldown_until = time.time() + 3600
        raise DiscordError(f"blocked/non-JSON {r.status_code} — cooling down 1h")
    if r.status_code == 429:
        _cooldown_until = time.time() + max(5.0, float(r.json().get("retry_after", 5)))
        raise DiscordError("rate limited")
    if r.status_code >= 400:
        raise DiscordError(f"{r.status_code}: {r.text[:200]}", r.json().get("code"))
    return r.json()


def _ping_payload(uid: str, today: date) -> dict:
    tomorrow = today + timedelta(days=1)
    tasks_ = store.get_tasks(uid)
    tomorrows = due_today(tasks_, today=tomorrow)
    embed: dict = {"title": "Due tomorrow", "color": 0xFEE75C}
    if tomorrows:
        embed["description"] = _fit([_task_line(t, today, show_flag=False) for t in tomorrows], 4000)
    else:
        embed["description"] = "Nothing due tomorrow."
    overdue_count = sum(1 for t in tasks_ if is_overdue(t, today))
    if overdue_count:
        embed["footer"] = {"text": f"⚠️ {overdue_count} task(s) overdue — check /hw-list"}
    button = {"type": 2, "style": 2, "label": "View all open tasks", "custom_id": "expand"}
    return {"embeds": [embed], "components": [{"type": 1, "components": [button]}]}


def _send_ping(uid: str, today: date) -> None:
    payload = _ping_payload(uid, today)
    channel = _dm_channels.get(uid)
    if channel is None:
        channel = _discord("POST", "/users/@me/channels", json={"recipient_id": uid})["id"]
        _dm_channels[uid] = channel
    _discord("POST", f"/channels/{channel}/messages", json=payload)


def run_ping_job() -> str:
    now = datetime.now(IST)
    if now.hour < PING_HOUR_IST:
        return "too early"
    if time.time() < _cooldown_until:
        return "cooling down"
    if not store.is_ready():
        store.kick_warm()
        return "warming"
    if not _ping_lock.acquire(blocking=False):
        return "busy"
    try:
        today = now.date()
        sent = 0
        for uid in store.all_user_ids():
            if store.get_last_ping(uid) == today.isoformat():
                continue
            if _undeliverable.get(uid) == today.isoformat():
                continue  # Discord already told us we can't DM them today
            try:
                _send_ping(uid, today)
            except Exception as e:
                if getattr(e, "code", None) == CANNOT_DM_USER:
                    # Permanent (DMs closed / no shared server / blocked).
                    # Stop retrying every 5 minutes; try again tomorrow.
                    print(f"[ping] {uid} has DMs closed, skipping for today")
                    _undeliverable[uid] = today.isoformat()
                    continue
                # Not recorded as sent, so the next 5-minute tick retries it.
                print(f"[ping] {uid} failed: {e}")
                if time.time() < _cooldown_until:
                    break
                continue
            store.set_last_ping(uid, today.isoformat())
            # Make "already pinged today" durable before moving on, so a
            # restart can't cause a duplicate DM.
            if not store.commit(10.0):
                print(f"[ping] last_ping for {uid} not yet durable")
            sent += 1
        return f"sent {sent}"
    finally:
        _ping_lock.release()


@app.get("/cron/<job>")
def cron(job: str):
    if not CRON_KEY or not hmac.compare_digest(request.args.get("key", ""), CRON_KEY):
        return "forbidden", 403
    if job != "ping":
        return "unknown job", 404
    try:
        return run_ping_job(), 200
    except Exception as e:  # always 200 so the monitor doesn't flap
        print(f"[cron] ping job error: {e!r}")
        return "error (logged)", 200


START = time.time()


@app.get("/")
def health():
    return (
        f"ok pid={os.getpid()} up={int(time.time() - START)}s "
        f"ready={store.is_ready()} warming={store._warming} err={store.last_error()!r}"
    )


# TEMPORARY diagnostic: remove once everything works.
@app.get("/debug/stacks")
def debug_stacks():
    if not CRON_KEY or not hmac.compare_digest(request.args.get("key", ""), CRON_KEY):
        return "forbidden", 403
    names = {t.ident: t.name for t in threading.enumerate()}
    out = []
    for tid, frame in sys._current_frames().items():
        out.append(f"--- {names.get(tid, tid)} ---")
        out.append("".join(traceback.format_stack(frame)))
    return "\n".join(out), 200, {"Content-Type": "text/plain"}


store.kick_warm()
