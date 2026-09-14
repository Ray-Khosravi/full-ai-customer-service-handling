"""Technician matching: skill, active, area, weekly schedule, Outlook status,
pending load and ON_CALL emergency fallback. Pure functions except the Outlook
lookup, which is injected as a callable."""

from __future__ import annotations

from datetime import datetime, time
from typing import Callable

from .util import TORONTO_TZ, postal_fsa

DAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def covers_area(technician: dict, postal_or_address: str | None) -> bool:
    """service_areas holds FSA prefixes ("M", "M1", "L1B"). Empty list = everywhere."""
    areas = technician.get("service_areas") or []
    if not areas:
        return True
    fsa = postal_fsa(postal_or_address)
    if not fsa:
        return False  # unknown area: do not assume coverage
    return any(fsa.startswith(prefix.upper()) for prefix in areas)


def has_skill(technician: dict, skill: str) -> bool:
    return skill in (technician.get("skills") or [])


def schedule_windows(technician: dict, day: datetime) -> list[tuple[datetime, datetime]]:
    """Weekly schedule windows for the Toronto-local date of ``day``."""
    local = day.astimezone(TORONTO_TZ)
    slots = (technician.get("weekly_schedule") or {}).get(DAY_KEYS[local.weekday()], [])
    windows = []
    for start_text, end_text in slots:
        sh, sm = (int(x) for x in start_text.split(":"))
        eh, em = (int(x) for x in end_text.split(":"))
        start = datetime.combine(local.date(), time(sh, sm), tzinfo=TORONTO_TZ)
        end = datetime.combine(local.date(), time(eh, em), tzinfo=TORONTO_TZ)
        windows.append((start, end))
    return windows


def scheduled_overlap(technician: dict, window_start: datetime, window_end: datetime) -> tuple[datetime, datetime] | None:
    """Intersection of the technician's weekly schedule with the requested window."""
    for start, end in schedule_windows(technician, window_start):
        lo, hi = max(start, window_start), min(end, window_end)
        if lo < hi:
            return lo, hi
    return None


def rank_candidates(
    technicians: list[dict],
    skill: str,
    postal_or_address: str | None,
    window_start: datetime,
    window_end: datetime,
    outlook_status: Callable[[dict], str],
    pending_load: Callable[[str], int],
    emergency: bool = False,
    exclude_ids: set[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Return (eligible ranked candidates, rejected with reasons).

    Eligible = active, has skill, covers area, not opted out, weekly schedule
    overlaps the window, Outlook status WORKING (or ON_CALL when emergency).
    Never OFF / SICK / VACATION. Ranking: primary skill first, lower pending
    load, larger schedule overlap, WORKING before ON_CALL.
    """
    exclude_ids = exclude_ids or set()
    eligible, rejected = [], []
    for tech in technicians:
        tid = tech.get("technician_id") or tech.get("RowKey")
        reason = None
        if tid in exclude_ids:
            reason = "already_tried"
        elif not tech.get("active", True):
            reason = "inactive"
        elif tech.get("sms_opt_out"):
            reason = "sms_opt_out"
        elif not has_skill(tech, skill):
            reason = "skill"
        elif not covers_area(tech, postal_or_address):
            reason = "area"
        if reason:
            rejected.append({"technician_id": tid, "reason": reason})
            continue
        overlap = scheduled_overlap(tech, window_start, window_end)
        if not overlap:
            rejected.append({"technician_id": tid, "reason": "schedule"})
            continue
        status = outlook_status(tech)
        if status in ("OFF", "SICK", "VACATION"):
            rejected.append({"technician_id": tid, "reason": f"outlook_{status.lower()}"})
            continue
        if status == "CALENDAR_NOT_FOUND":
            rejected.append({"technician_id": tid, "reason": "calendar_not_found"})
            continue
        if status == "ON_CALL" and not emergency:
            rejected.append({"technician_id": tid, "reason": "on_call_non_emergency"})
            continue
        load = pending_load(tid)
        primary = (tech.get("skills") or [None])[0] == skill
        eligible.append({
            "technician_id": tid,
            "name": tech.get("name"),
            "outlook_status": status,
            "pending_load": load,
            "window_start": overlap[0].isoformat(),
            "window_end": overlap[1].isoformat(),
            "primary_skill": primary,
            "_sort": (0 if status == "WORKING" else 1, 0 if primary else 1, load, -(overlap[1] - overlap[0]).total_seconds()),
        })
    eligible.sort(key=lambda c: c["_sort"])
    for c in eligible:
        c.pop("_sort", None)
    return eligible, rejected
