"""Small shared helpers: time, ids, redaction, phone normalisation."""

from __future__ import annotations

import hashlib
import re
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

TORONTO_TZ_NAME = "America/Toronto"
TORONTO_TZ = ZoneInfo(TORONTO_TZ_NAME)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    """UTC ISO-8601 with a trailing Z (what we store)."""
    value = value or now_utc()
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    text = text.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def to_toronto(value: datetime) -> datetime:
    return value.astimezone(TORONTO_TZ)


def toronto_local(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=TORONTO_TZ)


def local_day_window(requested: date) -> tuple[datetime, datetime]:
    """Toronto-local [midnight, next midnight) - 23/25 hours on DST days."""
    start = datetime.combine(requested, datetime.min.time(), tzinfo=TORONTO_TZ)
    end = datetime.combine(requested + timedelta(days=1), datetime.min.time(), tzinfo=TORONTO_TZ)
    return start, end


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def new_tracking_token() -> str:
    """Public, high-entropy reference (~72 bits) the customer keeps."""
    return "NX-" + secrets.token_urlsafe(9).replace("-", "A").replace("_", "B").upper()


def new_reply_code() -> str:
    """Short code technicians put in an SMS reply to disambiguate requests."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(4))


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_PHONE_DIGITS = re.compile(r"\D+")


def normalize_phone(raw: str | None) -> str | None:
    """Return E.164 for North American numbers, else None."""
    if not raw:
        return None
    digits = _PHONE_DIGITS.sub("", raw)
    if len(digits) == 10:
        digits = "1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if 11 < len(digits) <= 15 and raw.strip().startswith("+"):
        return "+" + digits
    return None


def redact_phone(phone: str | None) -> str:
    """+15555550123 -> +1******0123 (never log or return full numbers)."""
    if not phone:
        return ""
    digits = _PHONE_DIGITS.sub("", phone)
    if len(digits) < 4:
        return "***"
    if len(digits) == 10:
        digits = "1" + digits
    return f"+{digits[0]}{'*' * (len(digits) - 5)}{digits[-4:]}"


def mask_id(value: str | None, keep: int = 4) -> str:
    """Provider message ids and similar identifiers: log only a short suffix."""
    text = str(value or "")
    return ("…" + text[-keep:]) if len(text) > keep else text


def phone_last4(phone: str | None) -> str:
    digits = _PHONE_DIGITS.sub("", phone or "")
    return digits[-4:]


_POSTAL = re.compile(r"^([A-Za-z]\d[A-Za-z])\s*(\d[A-Za-z]\d)?$")


def postal_fsa(postal_or_address: str | None) -> str | None:
    """Forward sortation area (first 3 chars) of a Canadian postal code found in the text."""
    if not postal_or_address:
        return None
    match = re.search(r"\b([A-Za-z]\d[A-Za-z])\s*\d[A-Za-z]\d\b", postal_or_address)
    if match:
        return match.group(1).upper()
    match = _POSTAL.match(postal_or_address.strip())
    return match.group(1).upper() if match else None
