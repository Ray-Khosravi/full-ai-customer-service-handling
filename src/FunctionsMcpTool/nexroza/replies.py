"""Interpret technician SMS replies: accept / decline / propose time / opt-out.

Examples handled: "Yes", "Y", "OK", "No", "Can't", "I can do 5:30 PM",
"Available after 6", "5pm works", "17:30", "STOP", "R-AB12 yes 5:30".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .util import TORONTO_TZ

OPT_OUT_WORDS = {"stop", "stopall", "unsubscribe", "cancel", "end", "quit"}
DECLINE_PATTERNS = re.compile(
    r"\b(no|nope|can'?t|cannot|can not|unable|not available|unavailable|busy|decline|pass|not today|sorry)\b",
    re.IGNORECASE,
)
ACCEPT_PATTERNS = re.compile(r"\b(yes|yep|yeah|y|ok|okay|sure|confirm|confirmed|accept|available|can do|i can|works|fine|good)\b", re.IGNORECASE)
TIME_PATTERN = re.compile(
    r"\b(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>am|pm|a\.m\.|p\.m\.)?\b(?!\s*(?:min|minutes|hours?|hrs?))",
    re.IGNORECASE,
)
AFTER_PATTERN = re.compile(r"\b(after|from|at|by|around)\s+(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>am|pm)?", re.IGNORECASE)
CODE_PATTERN = re.compile(r"\b(?:R-)?([A-HJ-NP-Z2-9]{4})\b")


@dataclass
class ReplyInterpretation:
    intent: str                      # accept | decline | propose | opt_out | unknown
    proposed_start: datetime | None  # Toronto-aware
    reply_code: str | None
    note: str


def _to_time(h: int, m: int, ampm: str | None, default_pm_from: int = 1) -> time | None:
    ampm = (ampm or "").replace(".", "").lower()
    if ampm == "pm" and h < 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0
    elif not ampm and 1 <= h <= 6 and default_pm_from <= h:
        # a bare "5" or "5:30" from a plumber during the day means afternoon
        h += 12
    if 0 <= h <= 23 and 0 <= m <= 59:
        return time(h, m)
    return None


def interpret_reply(body: str, service_day: date, window_start: datetime | None = None,
                    window_end: datetime | None = None) -> ReplyInterpretation:
    text = (body or "").strip()
    lowered = text.lower()
    code_match = CODE_PATTERN.search(text.upper())
    reply_code = code_match.group(1) if code_match else None

    first_word = re.sub(r"[^a-z]", "", lowered.split()[0]) if lowered.split() else ""
    if first_word in OPT_OUT_WORDS and len(lowered.split()) <= 2:
        return ReplyInterpretation("opt_out", None, reply_code, "opt-out keyword")

    # explicit time?
    proposed: datetime | None = None
    for pattern in (AFTER_PATTERN, TIME_PATTERN):
        for match in pattern.finditer(text):
            if reply_code and match.group(0).strip().upper() in reply_code:
                continue
            h, m = int(match.group("h")), int(match.group("m") or 0)
            candidate = _to_time(h, m, match.group("ampm"))
            if candidate is None:
                continue
            proposed = datetime.combine(service_day, candidate, tzinfo=TORONTO_TZ)
            if pattern is AFTER_PATTERN and match.group(1).lower() in ("after", "from") and window_start:
                # "after 6" -> earliest slot at/after 6 within the window
                proposed = max(proposed, window_start) if window_start.date() == service_day else proposed
            break
        if proposed:
            break

    if DECLINE_PATTERNS.search(text) and not proposed and not ACCEPT_PATTERNS.search(text):
        return ReplyInterpretation("decline", None, reply_code, "decline keyword")
    if proposed:
        if window_end and proposed >= window_end:
            return ReplyInterpretation("propose", proposed, reply_code, "time outside requested window")
        return ReplyInterpretation("propose", proposed, reply_code, "time proposed")
    if ACCEPT_PATTERNS.search(text):
        if len(text.split()) > 8:
            # long free text without a time is not a clear acceptance (also blunts injected instructions)
            return ReplyInterpretation("unknown", None, reply_code, "long message without a clear answer")
        return ReplyInterpretation("accept", window_start, reply_code, "accepted requested window")
    if DECLINE_PATTERNS.search(text):
        return ReplyInterpretation("decline", None, reply_code, "decline keyword")
    return ReplyInterpretation("unknown", None, reply_code, "could not interpret")


def default_duration() -> timedelta:
    return timedelta(hours=2)
