"""
calc.py — priority scoring for the homework tracker.

Two-tier model:

  TIER 1 — OVERDUE. Any task whose due_date is before today. Always ranks
  above every non-overdue task, no matter how short. Sorted within the tier
  by how many days overdue (worse first), then by length (bigger first).

  TIER 2 — NOT YET DUE. Ranked by a continuous priority score that blends
  due-date urgency and task length, so a huge task due tomorrow can outrank
  a tiny task due in five days without needing hardcoded due-today/
  due-this-week buckets.

      urgency      = 1 / (days_until_due + 1)        # in (0, 1], decays smoothly
      length_norm  = length / max_length_among_open_tasks
      priority     = alpha * urgency + (1 - alpha) * length_norm

  `urgency` can be replaced per-task with a manual override (see
  SEVERITY_PRESETS) — that's the "overridable" part of "auto by due date,
  overridable." The override never lets a non-overdue task jump into the
  overdue tier; it only changes its score within tier 2.

  `alpha` defaults to 0.5 but is meant to be tuned per-user via /hw-weight;
  it's passed in, not hardcoded here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


# Preset labels for manual severity override, mapped onto the same (0, 1]
# scale as auto-urgency so the two are comparable in the score formula.
SEVERITY_PRESETS: dict[str, float] = {
    "low": 0.25,
    "medium": 0.5,
    "high": 0.75,
    "critical": 1.0,
}


@dataclass
class Task:
    id: str
    user_id: str
    subject: str
    title: str
    due_date: date  # calendar date, no time component
    length: int  # sub-items / pages
    status: str = "open"  # "open" | "done"
    severity_override: float | None = None  # None = auto by due date
    created_at: str = ""
    completed_at: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "user_id": self.user_id,
            "subject": self.subject,
            "title": self.title,
            "due_date": self.due_date.isoformat(),
            "length": self.length,
            "status": self.status,
            "severity_override": self.severity_override,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }

    @staticmethod
    def from_dict(d: dict) -> "Task":
        return Task(
            id=d["id"],
            user_id=d["user_id"],
            subject=d["subject"],
            title=d["title"],
            due_date=date.fromisoformat(d["due_date"]),
            length=int(d["length"]),
            status=d.get("status", "open"),
            severity_override=d.get("severity_override"),
            created_at=d.get("created_at", ""),
            completed_at=d.get("completed_at"),
        )


def is_overdue(task: Task, today: date) -> bool:
    return task.due_date < today


def days_overdue(task: Task, today: date) -> int:
    return (today - task.due_date).days


def days_until_due(task: Task, today: date) -> int:
    return (task.due_date - today).days


def auto_urgency(task: Task, today: date) -> float:
    """1/(days_until_due + 1). Only meaningful for non-overdue tasks
    (days_until_due >= 0), where it's in (0, 1]."""
    return 1.0 / (days_until_due(task, today) + 1)


def priority_score(task: Task, today: date, alpha: float, max_length: int) -> float:
    """Score for a NON-overdue task. Higher = more urgent/should-do-sooner."""
    urgency = task.severity_override if task.severity_override is not None else auto_urgency(task, today)
    length_norm = task.length / max_length if max_length > 0 else 0.0
    return alpha * urgency + (1 - alpha) * length_norm


def order_tasks(tasks: list[Task], alpha: float = 0.5, today: date | None = None) -> list[Task]:
    """Return open tasks ordered: overdue tier first (worst/longest first),
    then non-overdue tier by blended priority score (highest first)."""
    today = today or date.today()
    open_tasks = [t for t in tasks if t.status == "open"]

    overdue = [t for t in open_tasks if is_overdue(t, today)]
    upcoming = [t for t in open_tasks if not is_overdue(t, today)]

    overdue.sort(key=lambda t: (days_overdue(t, today), t.length), reverse=True)

    max_length = max((t.length for t in upcoming), default=1) or 1
    upcoming.sort(key=lambda t: priority_score(t, today, alpha, max_length), reverse=True)

    return overdue + upcoming


def due_today(tasks: list[Task], today: date | None = None) -> list[Task]:
    today = today or date.today()
    return [t for t in tasks if t.status == "open" and t.due_date == today]
