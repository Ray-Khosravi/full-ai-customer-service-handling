"""Safe seed data. Phone numbers are fictional (555) placeholders; the real
technician numbers are entered by the owner (admin seed endpoint / table).
Weekly schedules are Toronto local times; days are mon..sun."""

from __future__ import annotations

from .store import Store
from .util import iso_utc

SKILLS = [
    {"skill_code": "general_plumbing", "name": "General plumbing",
     "keywords": ["leak", "pipe", "faucet", "tap", "toilet", "sink", "valve", "plumbing", "burst", "shutoff"]},
    {"skill_code": "drain_services", "name": "Drain services",
     "keywords": ["drain", "clog", "clogged", "backup", "sewer", "blocked", "snake", "camera"]},
    {"skill_code": "water_heaters", "name": "Water heaters",
     "keywords": ["water heater", "hot water", "tank", "tankless", "boiler", "gas water heater", "no hot water"]},
]

WEEKDAY = ["mon", "tue", "wed", "thu", "fri"]

TECHNICIANS = [
    {
        "technician_id": "tech_john",
        "name": "John",
        "calendar_name": "John - Plumbing",
        "skills": ["general_plumbing", "water_heaters"],
        "service_areas": ["M"],                      # FSA prefixes: all of Toronto
        "phone": "+15555550101",                     # placeholder, replace via admin seed
        "active": True,
        "sms_opt_out": False,
        "weekly_schedule": {**{d: [["08:00", "18:00"]] for d in WEEKDAY}, "sat": [["09:00", "15:00"]], "sun": []},
    },
    {
        "technician_id": "tech_sara",
        "name": "Sara",
        "calendar_name": "Sara - Drain Services",
        "skills": ["drain_services", "general_plumbing"],
        "service_areas": ["M", "L1"],                # Toronto + Pickering/Ajax
        "phone": "+15555550102",
        "active": True,
        "sms_opt_out": False,
        "weekly_schedule": {**{d: [["07:00", "17:00"]] for d in WEEKDAY}, "sat": [["08:00", "14:00"]], "sun": []},
    },
    {
        "technician_id": "tech_michael",
        "name": "Michael",
        "calendar_name": "Michael - Water Heaters",
        "skills": ["water_heaters", "general_plumbing"],
        "service_areas": ["M", "L1"],
        "phone": "+15555550103",
        "active": True,
        "sms_opt_out": False,
        "weekly_schedule": {**{d: [["08:00", "20:00"]] for d in WEEKDAY}, "sat": [["09:00", "17:00"]], "sun": []},
    },
]


def seed(store: Store, overwrite: bool = False, phone_overrides: dict[str, str] | None = None) -> dict:
    """Insert seed rows that do not exist yet (or all rows when overwrite=True)."""
    created = {"skills": 0, "technicians": 0}
    for skill in SKILLS:
        entity = {"PartitionKey": "skill", "RowKey": skill["skill_code"], **skill, "updated_at": iso_utc()}
        if overwrite or store.get("Skills", "skill", skill["skill_code"]) is None:
            store.upsert("Skills", entity)
            created["skills"] += 1
    for tech in TECHNICIANS:
        entity = {"PartitionKey": "technician", "RowKey": tech["technician_id"], **tech, "updated_at": iso_utc()}
        if phone_overrides and tech["technician_id"] in phone_overrides:
            entity["phone"] = phone_overrides[tech["technician_id"]]
        existing = store.get("Technicians", "technician", tech["technician_id"])
        if overwrite or existing is None:
            if existing and not overwrite:
                continue
            if existing and phone_overrides is None:
                entity["phone"] = existing.get("phone", entity["phone"])  # keep owner-entered numbers
            store.upsert("Technicians", entity)
            created["technicians"] += 1
    return created


def ensure_seeded(store: Store) -> None:
    if not store.query("Technicians", "PartitionKey eq 'technician'", limit=1):
        seed(store)
