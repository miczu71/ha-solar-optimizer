"""G12W two-tier tariff calendar and helpers.

Peak hours (Mon-Fri workdays only):
  06:00-13:00 and 15:00-22:00
Off-peak: everything else (weekends, holidays, and the midday window 13-15).

All functions work on datetime objects in the HA local timezone.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

PEAK_PRICE = 1.23    # PLN/kWh
OFFPEAK_PRICE = 0.63  # PLN/kWh


def datetime_to_slot(dt: datetime) -> int:
    """Convert a local datetime to a 30-min slot index (0–47)."""
    return dt.hour * 2 + dt.minute // 30


def is_peak(dt: datetime, workday: bool) -> bool:
    """Return True if dt falls in G12W peak hours."""
    if not workday:
        return False
    h = dt.hour
    return (6 <= h < 13) or (15 <= h < 22)


def price_at(dt: datetime, workday: bool) -> float:
    return PEAK_PRICE if is_peak(dt, workday) else OFFPEAK_PRICE


def peak_vector_48(reference: datetime, workday: bool) -> list[bool]:
    """48-element peak vector for the day starting at midnight of reference."""
    result = []
    for slot in range(48):
        h = slot // 2
        result.append(workday and ((6 <= h < 13) or (15 <= h < 22)))
    return result


def peak_vector_96(reference: datetime, workday_today: bool, workday_tomorrow: bool) -> list[bool]:
    """96-element peak vector covering today (slots 0-47) and tomorrow (slots 48-95)."""
    return peak_vector_48(reference, workday_today) + peak_vector_48(reference, workday_tomorrow)


@dataclass
class OffpeakWindow:
    start: datetime
    end: datetime

    def duration_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600

    def __str__(self) -> str:
        return f"{self.start.strftime('%H:%M')}–{self.end.strftime('%H:%M')}"


def next_offpeak_window(now: datetime, workday_today: bool, workday_tomorrow: bool) -> OffpeakWindow:
    """Return the next off-peak charging window starting from now."""
    h = now.hour

    if h >= 22:
        start = now.replace(hour=22, minute=0, second=0, microsecond=0)
        if h == 22 and now.minute == 0:
            start = now
        end_dt = (now + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
        if not workday_tomorrow:
            end_dt = (now + timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
        return OffpeakWindow(start=start, end=end_dt)

    if h < 6:
        start = now
        end_dt = now.replace(hour=6, minute=0, second=0, microsecond=0)
        return OffpeakWindow(start=start, end=end_dt)

    if workday_today and 13 <= h < 15:
        start = now
        end_dt = now.replace(hour=15, minute=0, second=0, microsecond=0)
        return OffpeakWindow(start=start, end=end_dt)

    if h >= 13 and workday_today:
        start = now.replace(hour=22, minute=0, second=0, microsecond=0)
        end_dt = (now + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
        return OffpeakWindow(start=start, end=end_dt)

    if workday_today and 6 <= h < 13:
        start = now.replace(hour=13, minute=0, second=0, microsecond=0)
        end_dt = now.replace(hour=15, minute=0, second=0, microsecond=0)
        return OffpeakWindow(start=start, end=end_dt)

    if not workday_today:
        end_dt = (now + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
        if not workday_tomorrow:
            end_dt = (now + timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
        return OffpeakWindow(start=now, end=end_dt)

    start = now.replace(hour=22, minute=0, second=0, microsecond=0)
    end_dt = (now + timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
    return OffpeakWindow(start=start, end=end_dt)


def offpeak_hours_remaining_tonight(now: datetime, workday_today: bool, workday_tomorrow: bool) -> float:
    """Hours of off-peak charging available between now and the next peak start."""
    window = next_offpeak_window(now, workday_today, workday_tomorrow)
    if window.start > now:
        return window.duration_hours()
    return max(0.0, (window.end - now).total_seconds() / 3600)
