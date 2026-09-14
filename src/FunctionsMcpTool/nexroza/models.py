"""Service-request state machine and domain errors."""

from __future__ import annotations


class WorkflowError(Exception):
    """Safe, customer-presentable error with a stable code."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


class InvalidTransition(WorkflowError):
    def __init__(self, current: str, target: str):
        super().__init__("invalid_transition", f"Cannot move a request from {current} to {target}.", 409)
        self.current = current
        self.target = target


# Request states -------------------------------------------------------------
NEW = "new"
MATCHING = "matching"
AWAITING_TECHNICIAN = "awaiting_technician"
TECHNICIAN_DECLINED = "technician_declined"
TECHNICIAN_PROPOSED_TIME = "technician_proposed_time"
AWAITING_CUSTOMER_CONFIRMATION = "awaiting_customer_confirmation"
CONFIRMED = "confirmed"
CANCELLED = "cancelled"
EXPIRED = "expired"
FAILED = "failed"

STATES = {
    NEW, MATCHING, AWAITING_TECHNICIAN, TECHNICIAN_DECLINED, TECHNICIAN_PROPOSED_TIME,
    AWAITING_CUSTOMER_CONFIRMATION, CONFIRMED, CANCELLED, EXPIRED, FAILED,
}
TERMINAL_STATES = {CONFIRMED, CANCELLED, EXPIRED, FAILED}

# Allowed transitions. Anything not listed is rejected.
TRANSITIONS: dict[str, set[str]] = {
    NEW: {MATCHING, CANCELLED, FAILED},
    MATCHING: {AWAITING_TECHNICIAN, MATCHING, CANCELLED, FAILED},
    AWAITING_TECHNICIAN: {TECHNICIAN_PROPOSED_TIME, TECHNICIAN_DECLINED, EXPIRED, CANCELLED, FAILED},
    # a decline or a timeout may re-enter matching/awaiting for the next technician
    TECHNICIAN_DECLINED: {MATCHING, AWAITING_TECHNICIAN, CANCELLED, FAILED, EXPIRED},
    EXPIRED: {MATCHING, AWAITING_TECHNICIAN, CANCELLED, FAILED},
    TECHNICIAN_PROPOSED_TIME: {AWAITING_CUSTOMER_CONFIRMATION, CANCELLED, EXPIRED, FAILED},
    AWAITING_CUSTOMER_CONFIRMATION: {CONFIRMED, CANCELLED, EXPIRED, FAILED, AWAITING_TECHNICIAN},
    CONFIRMED: set(),
    CANCELLED: set(),
    FAILED: set(),
}


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, set())


def assert_transition(current: str, target: str) -> None:
    if not can_transition(current, target):
        raise InvalidTransition(current, target)


# Customer-facing wording (never leaks internals)
CUSTOMER_STATUS_TEXT = {
    NEW: "received",
    MATCHING: "finding a technician",
    AWAITING_TECHNICIAN: "waiting for a technician to confirm",
    TECHNICIAN_DECLINED: "finding another technician",
    EXPIRED: "no technician reply yet - we are trying another technician",
    TECHNICIAN_PROPOSED_TIME: "a technician proposed a time",
    AWAITING_CUSTOMER_CONFIRMATION: "waiting for your confirmation of the proposed time",
    CONFIRMED: "confirmed",
    CANCELLED: "cancelled",
    FAILED: "could not be scheduled automatically - the office will follow up",
}

SERVICE_TYPES = {
    "general_plumbing": "General plumbing",
    "drain_services": "Drain services",
    "water_heaters": "Water heaters",
}
