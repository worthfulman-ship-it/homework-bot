"""
bot.py — Homework tracker Discord bot.

Standalone bot (separate from Hydra and the attendance bot). Slash
commands for manual entry/logging, a daily DM ping showing what's due
today with a button to expand into the full priority-ordered list.

Commands:
  /hw-add <subject> <title> <due_date> <length>
                                  — due_date accepts natural phrases
                                    ("next thursday", "tomorrow", "fri",
                                    "in 5 days") as well as YYYY-MM-DD;
                                    see date_parse.py
  /hw-list                       — full open-task list, priority ordered
  /hw-done <task_id>             — mark complete, moves to log
  /hw-log                        — completed homework history
  /hw-severity <task_id> <level> — override auto severity (low/medium/high/critical)
  /hw-severity <task_id> auto    — clear override, back to auto-by-due-date
  /hw-weight <alpha 0.0-1.0>     — tune urgency-vs-length weighting

Env vars needed (same four as attendance-bot's Drive setup, OAuth flavor —
see drive_store.py's module docstring for the one-time setup):
  DISCORD_BOT_TOKEN
  GOOGLE_OAUTH_CLIENT_ID
  GOOGLE_OAUTH_CLIENT_SECRET
  GOOGLE_OAUTH_REFRESH_TOKEN
  DRIVE_FOLDER_ID
  DAILY_PING_USER_IDS   — comma-separated Discord user IDs to DM (optional;
                           if unset, the ping job just uses whoever has
                           existing task data in the store)
"""

from __future__ import annotations

import asyncio
import os
import threading
from datetime import date, datetime, time as dtime
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

import store
from calc import SEVERITY_PRESETS, Task, order_tasks, due_today, is_overdue, days_overdue, days_until_due
from date_parse import parse_due_date

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
IST = ZoneInfo("Asia/Kolkata")
PING_HOUR_IST = 17  # 5pm IST
PING_MINUTE_IST = 0

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# ---------- formatting helpers ----------

def _task_line(t: Task, today: date) -> str:
    if is_overdue(t, today):
        flag = f"⚠️ {days_overdue(t, today)}d overdue"
    else:
        d = days_until_due(t, today)
        flag = "due today" if d == 0 else f"due in {d}d"
    sev = ""
    if t.severity_override is not None:
        for label, val in SEVERITY_PRESETS.items():
            if val == t.severity_override:
                sev = f", severity: {label}"
                break
    return f"`{t.id}` **{t.subject}** — {t.title} ({t.length} items, {flag}{sev})"


def _build_list_embed(user_id: str, title: str) -> discord.Embed:
    today = date.today()
    tasks_ = store.get_tasks(user_id)
    alpha = store.get_alpha(user_id)
    ordered = order_tasks(tasks_, alpha=alpha, today=today)

    embed = discord.Embed(title=title, color=0x5865F2)
    if not ordered:
        embed.description = "Nothing open. 🎉"
        return embed

    overdue = [t for t in ordered if is_overdue(t, today)]
    upcoming = [t for t in ordered if not is_overdue(t, today)]

    if overdue:
        embed.add_field(name="Overdue", value="\n".join(_task_line(t, today) for t in overdue), inline=False)
    if upcoming:
        embed.add_field(name="Upcoming", value="\n".join(_task_line(t, today) for t in upcoming), inline=False)
    embed.set_footer(text=f"priority weight α={alpha:.2f}  •  /hw-weight to tune")
    return embed


class ExpandView(discord.ui.View):
    """Attached to the daily ping — lets the user pull up the full list."""

    def __init__(self, user_id: str):
        super().__init__(timeout=None)
        self.user_id = user_id

    @discord.ui.button(label="View all open tasks", style=discord.ButtonStyle.secondary)
    async def expand(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = _build_list_embed(self.user_id, "All open homework")
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------- slash commands ----------

@tree.command(name="hw-add", description="Add a homework task")
@app_commands.describe(
    subject="Subject/class",
    title="Short description of the task",
    due_date="e.g. 'next thursday', 'tomorrow', 'fri', 'in 5 days', or 2026-09-10",
    length="Length — number of sub-items or pages",
)
async def hw_add(interaction: discord.Interaction, subject: str, title: str, due_date: str, length: int):
    due = parse_due_date(due_date)
    if due is None:
        await interaction.response.send_message(
            "Couldn't understand that due date. Try things like `next thursday`, "
            "`tomorrow`, `fri`, `in 5 days`, or `2026-09-10`.",
            ephemeral=True,
        )
        return

    task = Task(
        id=store.new_task_id(),
        user_id=str(interaction.user.id),
        subject=subject,
        title=title,
        due_date=due,
        length=length,
        created_at=datetime.utcnow().isoformat(),
    )
    store.add_task(task)
    await interaction.response.send_message(
        f"Added `{task.id}` — **{subject}**: {title} (due {due.isoformat()}, {length} items).",
        ephemeral=True,
    )


@tree.command(name="hw-list", description="Show open homework, ordered by priority")
async def hw_list(interaction: discord.Interaction):
    embed = _build_list_embed(str(interaction.user.id), "Open homework")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="hw-done", description="Mark a homework task as done")
@app_commands.describe(task_id="The task id shown in /hw-list (e.g. a1b2c3d4)")
async def hw_done(interaction: discord.Interaction, task_id: str):
    ok = store.mark_done(str(interaction.user.id), task_id)
    if ok:
        await interaction.response.send_message(f"Marked `{task_id}` done. Nice.", ephemeral=True)
    else:
        await interaction.response.send_message(f"No open task matching `{task_id}`.", ephemeral=True)


@tree.command(name="hw-log", description="Show completed homework history")
async def hw_log(interaction: discord.Interaction):
    done = store.get_done_tasks(str(interaction.user.id))
    if not done:
        await interaction.response.send_message("No completed homework logged yet.", ephemeral=True)
        return
    done.sort(key=lambda t: t.completed_at or "", reverse=True)
    lines = [
        f"`{t.id}` **{t.subject}** — {t.title} (done {t.completed_at[:10] if t.completed_at else '?'})"
        for t in done
    ]
    embed = discord.Embed(title="Completed homework", description="\n".join(lines[:25]), color=0x57F287)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="hw-severity", description="Override a task's severity, or reset it to auto")
@app_commands.describe(task_id="The task id", level="low / medium / high / critical / auto")
@app_commands.choices(
    level=[app_commands.Choice(name=lvl, value=lvl) for lvl in list(SEVERITY_PRESETS.keys()) + ["auto"]]
)
async def hw_severity(interaction: discord.Interaction, task_id: str, level: app_commands.Choice[str]):
    user_id = str(interaction.user.id)
    value = None if level.value == "auto" else SEVERITY_PRESETS[level.value]
    ok = store.set_severity_override(user_id, task_id, value)
    if not ok:
        await interaction.response.send_message(f"No task matching `{task_id}`.", ephemeral=True)
        return
    msg = f"`{task_id}` severity reset to auto (by due date)." if value is None else f"`{task_id}` severity set to **{level.value}**."
    await interaction.response.send_message(msg, ephemeral=True)


@tree.command(name="hw-weight", description="Set how heavily due-date urgency is weighted vs. length (0.0-1.0)")
@app_commands.describe(alpha="0.0 = pure length ordering, 1.0 = pure due-date ordering")
async def hw_weight(interaction: discord.Interaction, alpha: float):
    if not 0.0 <= alpha <= 1.0:
        await interaction.response.send_message("alpha must be between 0.0 and 1.0.", ephemeral=True)
        return
    store.set_alpha(str(interaction.user.id), alpha)
    await interaction.response.send_message(f"Priority weighting set to α={alpha:.2f}.", ephemeral=True)


# ---------- daily ping ----------

@tasks.loop(time=dtime(hour=PING_HOUR_IST, minute=PING_MINUTE_IST, tzinfo=IST))
async def daily_ping():
    today = date.today()
    for user_id in store.all_user_ids():
        try:
            user = await bot.fetch_user(int(user_id))
        except Exception as e:
            print(f"[ping] couldn't fetch user {user_id}: {e}")
            continue

        todays = due_today(store.get_tasks(user_id), today=today)
        embed = discord.Embed(title="Due today", color=0xFEE75C)
        if todays:
            embed.description = "\n".join(_task_line(t, today) for t in todays)
        else:
            embed.description = "Nothing due today."

        overdue_count = sum(1 for t in store.get_tasks(user_id) if is_overdue(t, today))
        if overdue_count:
            embed.set_footer(text=f"⚠️ {overdue_count} task(s) overdue — check /hw-list")

        try:
            await user.send(embed=embed, view=ExpandView(user_id))
        except Exception as e:
            print(f"[ping] couldn't DM {user_id}: {e}")


@daily_ping.before_loop
async def before_daily_ping():
    await bot.wait_until_ready()


# ---------- Render port-scan workaround ----------
# Render's Web Service type expects something bound to $PORT and will kill
# the process if nothing answers. This bot only makes outbound connections
# (Discord gateway + Drive API), so we run a trivial HTTP server on a
# background thread just to satisfy the port scan. UptimeRobot pings this
# same endpoint to prevent cold starts. Same fix as Hydra.

class _HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass  # keep Render logs quiet


def _run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), _HealthCheckHandler)
    print(f"[health] listening on 0.0.0.0:{port}")
    server.serve_forever()


# ---------- lifecycle ----------

@bot.event
async def on_ready():
    await tree.sync()
    if not daily_ping.is_running():
        daily_ping.start()
    print(f"[bot] logged in as {bot.user} — daily ping set for {PING_HOUR_IST:02d}:{PING_MINUTE_IST:02d} IST")


async def _run_with_backoff():
    """Exponential backoff around bot.start(), ported from the fix used on
    Hydra after its Cloudflare 1015 incident."""
    delay = 5
    max_delay = 300
    while True:
        try:
            async with bot:
                await bot.start(TOKEN)
            break  # clean shutdown
        except Exception as e:
            print(f"[bot] crashed: {e} — retrying in {delay}s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN is not set.")
    threading.Thread(target=_run_health_server, daemon=True).start()
    asyncio.run(_run_with_backoff())
