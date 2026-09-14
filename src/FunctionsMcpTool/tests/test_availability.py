"""Unit tests for the pure availability rules in function_app.py (no Azure access)."""
import os
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

os.environ.setdefault("GRAPH_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nexroza import graph as fa  # noqa: E402
from nexroza import util  # noqa: E402

TZ = ZoneInfo("America/Toronto")


def ev(subject, start=None, end=None):
    e = {"subject": subject}
    if start:
        e["start"] = {"dateTime": start, "timeZone": "America/Toronto"}
        e["end"] = {"dateTime": end, "timeZone": "America/Toronto"}
    return e


def test_status_priority_and_keywords():
    assert fa.status_from_events([]) == ("WORKING", [])
    assert fa.status_from_events([ev("Dentist 3pm")]) == ("WORKING", [])
    assert fa.status_from_events([ev("off today")])[0] == "OFF"
    assert fa.status_from_events([ev("Vacation - Cuba")])[0] == "VACATION"
    assert fa.status_from_events([ev("SICK")])[0] == "SICK"
    for s in ("ON CALL", "ON_CALL", "on-call weekend", "ONCALL"):
        assert fa.status_from_events([ev(s)])[0] == "ON_CALL", s
    # Unavailable beats on-call; only status events are echoed back (privacy).
    status, subjects = fa.status_from_events([ev("ON CALL"), ev("Dentist"), ev("OFF - family")])
    assert status == "OFF" and subjects == ["ON CALL", "OFF - family"]
    # Keyword must be a whole word at the start.
    assert fa.status_from_events([ev("Office visit")])[0] == "WORKING"
    assert fa.status_from_events([ev("Sickle cell clinic")])[0] == "WORKING"


def test_local_day_window_dst():
    # Spring forward 2026-03-08: day is 23h. Fall back 2026-11-01: day is 25h.
    utc = ZoneInfo("UTC")
    s, e = util.local_day_window(date(2026, 3, 8))
    assert (e.astimezone(utc) - s.astimezone(utc)).total_seconds() == 23 * 3600
    assert s.isoformat() == "2026-03-08T00:00:00-05:00" and e.isoformat() == "2026-03-09T00:00:00-04:00"
    s, e = util.local_day_window(date(2026, 11, 1))
    assert (e.astimezone(utc) - s.astimezone(utc)).total_seconds() == 25 * 3600
    assert s.isoformat() == "2026-11-01T00:00:00-04:00" and e.isoformat() == "2026-11-02T00:00:00-05:00"
    s, e = util.local_day_window(date(2026, 9, 13))
    assert (e.astimezone(utc) - s.astimezone(utc)).total_seconds() == 24 * 3600


def test_parse_graph_datetime_seven_fraction_digits():
    d = fa.parse_graph_datetime({"dateTime": "2026-09-13T00:00:00.0000000", "timeZone": "America/Toronto"})
    assert d == datetime(2026, 9, 13, tzinfo=TZ)
    d = fa.parse_graph_datetime({"dateTime": "2026-09-13T04:00:00.0000000", "timeZone": "UTC"})
    assert d == datetime(2026, 9, 13, tzinfo=TZ)  # 04:00Z == 00:00 EDT
    d = fa.parse_graph_datetime({"dateTime": "2026-09-13T09:00:00.0000000", "timeZone": "Eastern Standard Time"})
    assert d.tzinfo is TZ  # non-IANA name falls back to Toronto


def test_overlap_excludes_neighbouring_all_day_events():
    s, e = util.local_day_window(date(2026, 9, 13))
    same_day = ev("OFF", "2026-09-13T00:00:00.0000000", "2026-09-14T00:00:00.0000000")
    next_day = ev("OFF", "2026-09-14T00:00:00.0000000", "2026-09-15T00:00:00.0000000")
    prev_day = ev("OFF", "2026-09-12T00:00:00.0000000", "2026-09-13T00:00:00.0000000")
    late = ev("ON CALL", "2026-09-13T23:00:00.0000000", "2026-09-14T02:00:00.0000000")
    assert fa.overlaps(same_day, s, e)
    assert not fa.overlaps(next_day, s, e)
    assert not fa.overlaps(prev_day, s, e)
    assert fa.overlaps(late, s, e)
    assert fa.overlaps(ev("no times"), s, e)  # unknown -> keep


def test_invalid_date_returns_error_json_without_graph():
    import json
    import function_app
    # The MCP decorator wraps the tool in an async shim; unwrap to the original.
    tool = function_app.get_technician_work_status._function.get_user_function().__wrapped__
    out = json.loads(tool("13/09/2026"))
    assert out["error"] == "invalid_date"
