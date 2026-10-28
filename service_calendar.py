"""USF academic context evaluated at event time in America/New_York."""

from datetime import datetime, date
from zoneinfo import ZoneInfo
from functools import lru_cache

USF_TIMEZONE = ZoneInfo("America/New_York")

def local_datetime(at=None):
    return datetime.now(USF_TIMEZONE) if at is None else datetime.fromtimestamp(at, USF_TIMEZONE)

def get_period_type(at=None):
    return _period_for_date(local_datetime(at).date())


@lru_cache(maxsize=1024)
def _period_for_date(today):

    # --- HOLIDAYS ---
    holidays = {
        date(2026, 5, 25),   # Memorial Day
        date(2026, 6, 19),   # Juneteenth
        date(2026, 7, 3),    # Independence Day observed
        date(2026, 9, 7),    # Labor Day
        date(2026, 11, 11),  # Veterans Day
        date(2026, 11, 26),  # Thanksgiving
        date(2026, 11, 27),  # Thanksgiving
        date(2026, 12, 25),  # Christmas
        date(2027, 1, 1),    # New Year's Day
        date(2027, 1, 18),   # MLK Day
        date(2027, 5, 31),   # Memorial Day
        date(2027, 6, 19),   # Juneteenth
        date(2027, 7, 5),    # Independence Day observed
        date(2027, 9, 6),    # Labor Day
        date(2027, 11, 11),  # Veterans Day
        date(2027, 11, 25),  # Thanksgiving
        date(2027, 11, 26),  # Thanksgiving
        date(2027, 12, 25),  # Christmas
        date(2028, 1, 1),    # New Year's Day
        date(2028, 1, 17),   # MLK Day
    }
    if today in holidays:
        return "holiday"

    # --- SUMMER 2026 ---
    if date(2026, 5, 18) <= today <= date(2026, 8, 7):
        return "summer"

    # --- FALL 2026 ---
    if date(2026, 8, 24) <= today <= date(2026, 8, 28):
        return "first_week"
    if date(2026, 12, 5) <= today <= date(2026, 12, 10):
        return "finals"
    if date(2026, 8, 24) <= today <= date(2026, 12, 4):
        return "regular"

    # --- WINTER BREAK 2026-2027 ---
    if date(2026, 12, 11) <= today <= date(2027, 1, 10):
        return "break"

    # --- SPRING 2027 ---
    if date(2027, 1, 11) <= today <= date(2027, 1, 17):
        return "first_week"
    if date(2027, 3, 15) <= today <= date(2027, 3, 21):
        return "break"  # Spring break
    if date(2027, 4, 24) <= today <= date(2027, 4, 30):
        return "finals_week_prep"
    if date(2027, 5, 1) <= today <= date(2027, 5, 6):
        return "finals"
    if date(2027, 1, 11) <= today <= date(2027, 4, 30):
        return "regular"

    # --- SUMMER 2027 ---
    if date(2027, 5, 17) <= today <= date(2027, 8, 6):
        return "summer"

    # --- FALL 2027 (estimated) ---
    if date(2027, 8, 23) <= today <= date(2027, 8, 27):
        return "first_week"
    if date(2027, 12, 4) <= today <= date(2027, 12, 10):
        return "finals"
    if date(2027, 8, 23) <= today <= date(2027, 12, 3):
        return "regular"

    # --- WINTER BREAK 2027-2028 ---
    if date(2027, 12, 11) <= today <= date(2028, 1, 9):
        return "break"

    # --- SPRING 2028 (estimated) ---
    if date(2028, 1, 10) <= today <= date(2028, 1, 16):
        return "first_week"
    if date(2028, 3, 10) <= today <= date(2028, 3, 16):
        return "break"
    if date(2028, 4, 28) <= today <= date(2028, 5, 3):
        return "finals"
    if date(2028, 1, 10) <= today <= date(2028, 4, 27):
        return "regular"

    # --- SUMMER 2028 (estimated) ---
    if date(2028, 5, 15) <= today <= date(2028, 8, 7):
        return "summer"

    return "break"

