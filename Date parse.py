"""
date_parse.py — flexible due-date parsing for /hw-add.

Accepts, in order of attempt:
  1. ISO format:            "2026-09-10"
  2. Relative words:        "today", "tomorrow"
  3. Weekday phrases:       "thursday", "this thursday", "next thursday",
                             "thu", "tues", "weds" (any unambiguous prefix
                             of a weekday name, >=3 chars)
  4. Relative offsets:      "in 5 days", "in 1 day"

Weekday semantics (the common colloquial reading, not the calendar-app
one):
  - bare weekday / "this <weekday>"  -> the closest upcoming occurrence,
    INCLUDING today if today is that weekday. ("thursday" said on a
    Thursday means today.)
  - "next <weekday>"                 -> skips the closest occurrence and
    lands on the one in the following week. ("next thursday" said on a
    Monday means 10 days out, not 3.)

Returns None (never raises) if the text can't be parsed, so callers can
show a friendly error with examples.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

_WEEKDAYS = [
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
]

_RELATIVE_WORDS = {
    "today": 0,
    "tomorrow": 1,
    "tmrw": 1,
    "tmr": 1,
}

_WEEKDAY_RE = re.compile(r"^(next|this|coming)?\s*([a-z]+)$")
_IN_DAYS_RE = re.compile(r"^in\s+(\d+)\s*days?$")


def _match_weekday(token: str) -> int | None:
    """Match a token against a weekday name via unambiguous prefix
    (min 3 chars, so 'thu' -> thursday, but 'tu' is rejected)."""
    if len(token) < 3:
        return None
    matches = [i for i, wd in enumerate(_WEEKDAYS) if wd.startswith(token)]
    return matches[0] if len(matches) == 1 else None


def parse_due_date(text: str, today: date | None = None) -> date | None:
    today = today or date.today()
    cleaned = text.strip().lower()
    if not cleaned:
        return None

    # 1. ISO format — unchanged behavior for anyone still typing it directly.
    try:
        return date.fromisoformat(cleaned)
    except ValueError:
        pass

    # 2. Relative words.
    if cleaned in _RELATIVE_WORDS:
        return today + timedelta(days=_RELATIVE_WORDS[cleaned])

    # 3. Weekday phrases, with optional "next"/"this"/"coming" modifier.
    m = _WEEKDAY_RE.match(cleaned)
    if m:
        modifier, token = m.groups()
        weekday_idx = _match_weekday(token)
        if weekday_idx is not None:
            diff = (weekday_idx - today.weekday()) % 7
            if modifier == "next":
                diff += 7
            return today + timedelta(days=diff)

    # 4. "in N days".
    m = _IN_DAYS_RE.match(cleaned)
    if m:
        return today + timedelta(days=int(m.group(1)))

    return None
