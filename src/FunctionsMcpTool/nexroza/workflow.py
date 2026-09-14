"""Service-request workflow: the business logic behind the MCP tools, the
queue handlers and the expiry timer. Everything here is unit-testable with
``MemoryStore``, a fake Graph client and ``MockSmsProvider``.

Public identifiers: customers only ever see the high-entropy tracking token
(``NX-...``); technicians only see a 4-character reply code. Internal request
ids never leave this module's return values.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date, datetime, timedelta

from . import models as m
from .graph import GraphClient, GraphError, OutlookAuthorizationError, busy_in_window, day_status
from .matching import rank_candidates, scheduled_overlap
from .replies import default_duration, interpret_reply
from .seed import ensure_seeded
from .sms import InboundSms, SmsProvider
from .store import ConcurrencyConflict, Store, claim_idempotency, record_event
from .util import (TORONTO_TZ, iso_utc, mask_id, new_id, new_reply_code, new_tracking_token, normalize_phone,
                   now_utc, parse_iso, phone_last4, postal_fsa, redact_phone, sha256, to_toronto, toronto_local)

logger = logging.getLogger("nexroza.workflow")

OUTBOUND_QUEUE = "sms-outbound"
INBOUND_QUEUE = "sms-inbound"
REPLY_TIMEOUT_MINUTES = int(os.environ.get("TECHNICIAN_REPLY_TIMEOUT_MINUTES", "30"))
MAX_TECHNICIAN_ATTEMPTS = int(os.environ.get("MAX_TECHNICIAN_ATTEMPTS", "3"))
BOOKING_HOLD_HOURS = int(os.environ.get("BOOKING_HOLD_HOURS", "12"))  # customer must confirm within


class Queue:
    """Minimal queue abstraction (Azure Storage Queue or in-memory list)."""

    def __init__(self, sender=None):
        self._sender = sender
        self.sent: list[tuple[str, dict]] = []

    def send(self, queue_name: str, payload: dict) -> None:
        self.sent.append((queue_name, payload))
        if self._sender:
            self._sender(queue_name, json.dumps(payload))


def azure_queue_sender():
    from azure.storage.queue import BinaryBase64EncodePolicy, QueueServiceClient
    from azure.identity import DefaultAzureCredential
    uri = os.environ.get("AzureWebJobsStorage__queueServiceUri")
    if uri:
        credential = DefaultAzureCredential(
            managed_identity_client_id=os.environ.get("AzureWebJobsStorage__clientId"),
            exclude_interactive_browser_credential=True,
        )
        service = QueueServiceClient(uri, credential=credential)
    else:
        service = QueueServiceClient.from_connection_string(os.environ["AzureWebJobsStorage"])
    clients: dict = {}

    def send(queue_name: str, text: str) -> None:
        client = clients.get(queue_name)
        if client is None:
            client = service.get_queue_client(queue_name, message_encode_policy=BinaryBase64EncodePolicy())
            try:
                client.create_queue()
            except Exception:  # noqa: BLE001 - already exists
                pass
            clients[queue_name] = client
        client.send_message(text.encode("utf-8"))

    return send


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #

def _q(value: str) -> str:
    """Escape a value for an OData string literal."""
    return str(value).replace("'", "''")


def _require(value, code: str, message: str):
    if value in (None, "", []):
        raise m.WorkflowError(code, message)
    return value


def _parse_date(text: str) -> date:
    try:
        return datetime.strptime(str(text).strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise m.WorkflowError("invalid_date", "preferred_date must be YYYY-MM-DD (Toronto).") from exc


def _parse_hhmm(text: str | None, default: str) -> tuple[int, int]:
    raw = (text or default).strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", raw)
    if not match or not (0 <= int(match.group(1)) <= 23 and 0 <= int(match.group(2)) <= 59):
        raise m.WorkflowError("invalid_time", "Times must be HH:MM in 24-hour Toronto time.")
    return int(match.group(1)), int(match.group(2))


def _local_str(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = parse_iso(value)
        if value is None:
            return None
    return to_toronto(value).strftime("%Y-%m-%d %H:%M")


def _local_speech(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = parse_iso(value)
    return to_toronto(value).strftime("%A %B %d at %I:%M %p").replace(" 0", " ")


# --------------------------------------------------------------------------- #
# Workflow
# --------------------------------------------------------------------------- #

class Workflow:
    def __init__(self, store: Store, graph: GraphClient | None, sms: SmsProvider, queue: Queue,
                 clock=None):
        self.store = store
        self.graph = graph
        self.sms = sms
        self.queue = queue
        self.clock = clock or now_utc
        ensure_seeded(store)

    # -- lookups --------------------------------------------------------------
    def technicians(self) -> list[dict]:
        return self.store.query("Technicians", "PartitionKey eq 'technician'")

    def technician(self, technician_id: str) -> dict | None:
        return self.store.get("Technicians", "technician", technician_id)

    def technician_by_phone(self, phone: str) -> dict | None:
        for tech in self.technicians():
            if normalize_phone(tech.get("phone")) == phone:
                return tech
        return None

    def _request(self, request_id: str) -> dict:
        entity = self.store.get("ServiceRequests", "request", request_id)
        if entity is None:
            raise m.WorkflowError("not_found", "No such request.", 404)
        return entity

    def _request_by_token(self, tracking_token: str, phone: str | None) -> dict:
        token = (tracking_token or "").strip().upper()
        if not re.fullmatch(r"NX-[A-Z0-9]{8,16}", token):
            raise m.WorkflowError("not_found", "No request matches that reference.", 404)
        rows = self.store.query("ServiceRequests", f"PartitionKey eq 'request' and tracking_hash eq '{sha256(token)}'", limit=1)
        if not rows:
            raise m.WorkflowError("not_found", "No request matches that reference.", 404)
        entity = rows[0]
        # second factor: the callback phone (full number or last 4 digits)
        supplied = (phone or "").strip()
        if not supplied:
            raise m.WorkflowError("verification_required", "Please provide the callback phone number used for the request.", 401)
        ok = normalize_phone(supplied) == entity.get("customer_phone") or (
            re.fullmatch(r"\d{4}", supplied) and supplied == phone_last4(entity.get("customer_phone")))
        if not ok:
            logger.warning("REQUEST_LOOKUP_DENIED reference=%s", token[:6] + "…")
            raise m.WorkflowError("not_found", "No request matches that reference.", 404)
        return entity

    def _transition(self, entity: dict, target: str, kind: str, detail: dict | None = None,
                    actor: str = "system", etag: str | None = None, **fields) -> dict:
        current = entity["status"]
        m.assert_transition(current, target)
        updated = dict(entity)
        updated.update(fields)
        updated["status"] = target
        updated["updated_at"] = iso_utc(self.clock())
        saved = self.store.update("ServiceRequests", updated, etag=etag or entity.get("etag"))
        record_event(self.store, entity["RowKey"], kind, current, target, detail, actor)
        logger.info("REQUEST_TRANSITION request=%s %s -> %s kind=%s", entity["RowKey"][-8:], current, target, kind)
        return saved

    def _window(self, entity: dict) -> tuple[datetime, datetime]:
        return parse_iso(entity["window_start"]), parse_iso(entity["window_end"])

    # -- 1. create --------------------------------------------------------------
    def create_service_request(self, *, customer_name: str, callback_phone: str, address: str,
                               postal_code: str | None, service_type: str, issue: str,
                               preferred_date: str, preferred_time_from: str | None = None,
                               preferred_time_to: str | None = None, sms_consent: bool = False,
                               is_emergency: bool = False, idempotency_key: str | None = None) -> dict:
        _require(customer_name, "missing_name", "Customer name is required.")
        phone = normalize_phone(callback_phone)
        if not phone:
            raise m.WorkflowError("invalid_phone", "A valid North American callback phone number is required.")
        _require(address, "missing_address", "Service address is required.")
        if service_type not in m.SERVICE_TYPES:
            raise m.WorkflowError("invalid_service_type", f"service_type must be one of {sorted(m.SERVICE_TYPES)}.")
        _require(issue, "missing_issue", "A short issue description is required.")
        day = _parse_date(preferred_date)
        fh, fm = _parse_hhmm(preferred_time_from, "08:00")
        th, tm = _parse_hhmm(preferred_time_to, "20:00")
        window_start, window_end = toronto_local(day, fh, fm), toronto_local(day, th, tm)
        if window_end <= window_start:
            raise m.WorkflowError("invalid_window", "preferred_time_to must be after preferred_time_from.")
        if window_end < self.clock():
            raise m.WorkflowError("window_in_past", "The requested time window is already in the past.")
        fsa = postal_fsa(postal_code) or postal_fsa(address)

        if idempotency_key and not claim_idempotency(self.store, idempotency_key, "create_service_request"):
            rows = self.store.query("ServiceRequests", f"PartitionKey eq 'request' and idempotency_key eq '{_q(idempotency_key)}'", limit=1)
            if rows:
                return self.public_view(rows[0], include_token=False) | {"duplicate": True}

        request_id = new_id("req")
        token = new_tracking_token()
        entity = {
            "PartitionKey": "request", "RowKey": request_id,
            "status": m.NEW,
            "tracking_hash": sha256(token),
            "reply_code": new_reply_code(),
            "idempotency_key": idempotency_key or "",
            "customer_name": customer_name.strip()[:80],
            "customer_phone": phone,
            "address": address.strip()[:200],
            "postal_fsa": fsa or "",
            "service_type": service_type,
            "issue": issue.strip()[:500],
            "is_emergency": bool(is_emergency),
            "sms_consent": bool(sms_consent),
            "service_date": day.isoformat(),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "candidates": [],
            "tried_technicians": "",
            "assigned_technician_id": "",
            "proposed_start": "", "proposed_end": "",
            "expires_at": "",
            "confirm_lock": "",
            "created_at": iso_utc(self.clock()), "updated_at": iso_utc(self.clock()),
        }
        saved = self.store.insert("ServiceRequests", entity)
        record_event(self.store, request_id, "created", None, m.NEW, {"service_type": service_type, "fsa": fsa or ""}, "agent")
        logger.info("SERVICE_REQUEST_CREATED request=%s service=%s fsa=%s emergency=%s",
                    request_id[-8:], service_type, fsa or "?", bool(is_emergency))
        view = self.public_view(saved, include_token=False)
        view["tracking_reference"] = token
        return view

    # -- 2. match -----------------------------------------------------------------
    def _outlook_status_for(self, technician: dict, day: date, correlation_id: str, cache: dict) -> str:
        if self.graph is None:
            return "WORKING"
        name = technician.get("calendar_name")
        if "calendars" not in cache:
            cache["calendars"] = self.graph.calendar_map(correlation_id)
        calendar_id = cache["calendars"].get(name)
        if not calendar_id:
            return "CALENDAR_NOT_FOUND"
        key = f"{calendar_id}:{day}"
        if key not in cache:
            status, _, events = day_status(self.graph, calendar_id, day, correlation_id)
            cache[key] = (status, events)
        return cache[key][0]

    def _pending_load(self, technician_id: str) -> int:
        rows = self.store.query("ServiceRequests",
                                f"PartitionKey eq 'request' and assigned_technician_id eq '{technician_id}' and status eq '{m.AWAITING_TECHNICIAN}'")
        return len(rows)

    def find_matching_technicians(self, tracking_token: str, phone: str, correlation_id: str) -> dict:
        entity = self._request_by_token(tracking_token, phone)
        if entity["status"] not in (m.NEW, m.MATCHING, m.TECHNICIAN_DECLINED, m.EXPIRED):
            raise m.WorkflowError("invalid_state", f"Request is {m.CUSTOMER_STATUS_TEXT.get(entity['status'])}.", 409)
        window_start, window_end = self._window(entity)
        day = window_start.date()
        cache: dict = {}
        tried = set(filter(None, entity.get("tried_technicians", "").split(",")))
        eligible, rejected = rank_candidates(
            self.technicians(), entity["service_type"], entity.get("postal_fsa") or entity.get("address"),
            window_start, window_end,
            outlook_status=lambda t: self._outlook_status_for(t, day, correlation_id, cache),
            pending_load=self._pending_load,
            emergency=bool(entity.get("is_emergency")),
            exclude_ids=tried,
        )
        entity = self._transition(entity, m.MATCHING, "matched",
                                  {"eligible": [c["technician_id"] for c in eligible], "rejected": rejected},
                                  "agent", candidates=eligible)
        return {
            "tracking_reference": tracking_token.upper(),
            "status": entity["status"],
            "candidates": [{"technician": c["name"], "outlook_status": c["outlook_status"],
                            "available_from": _local_str(c["window_start"]), "available_until": _local_str(c["window_end"])}
                           for c in eligible],
            "no_match_reasons": sorted({r["reason"] for r in rejected}) if not eligible else [],
        }

    # -- 3. availability (no request needed) -----------------------------------------
    def check_availability(self, service_type: str, date_text: str, postal_code: str | None,
                           time_from: str | None, time_to: str | None, correlation_id: str,
                           emergency: bool = False) -> dict:
        if service_type not in m.SERVICE_TYPES:
            raise m.WorkflowError("invalid_service_type", f"service_type must be one of {sorted(m.SERVICE_TYPES)}.")
        day = _parse_date(date_text)
        fh, fm = _parse_hhmm(time_from, "08:00")
        th, tm = _parse_hhmm(time_to, "20:00")
        window_start, window_end = toronto_local(day, fh, fm), toronto_local(day, th, tm)
        cache: dict = {}
        results = []
        for tech in self.technicians():
            item = {"technician": tech.get("name"), "skill_match": service_type in (tech.get("skills") or []),
                    "area_match": True, "scheduled": None, "outlook_status": None, "available": False}
            if not tech.get("active", True):
                item["reason"] = "inactive"
                results.append(item)
                continue
            if postal_code:
                from .matching import covers_area
                item["area_match"] = covers_area(tech, postal_code)
            overlap = scheduled_overlap(tech, window_start, window_end)
            item["scheduled"] = {"from": _local_str(overlap[0]), "to": _local_str(overlap[1])} if overlap else None
            status = self._outlook_status_for(tech, day, correlation_id, cache)
            item["outlook_status"] = status
            item["available"] = bool(item["skill_match"] and item["area_match"] and overlap
                                     and (status == "WORKING" or (status == "ON_CALL" and emergency)))
            if status == "ON_CALL":
                item["emergency_only"] = True
            results.append(item)
        return {"date": day.isoformat(), "timezone": "America/Toronto", "service_type": service_type,
                "window": {"from": _local_str(window_start), "to": _local_str(window_end)}, "technicians": results}

    # -- 4. send SMS (async) ----------------------------------------------------------
    def send_technician_request(self, tracking_token: str, phone: str, technician_name: str | None = None) -> dict:
        entity = self._request_by_token(tracking_token, phone)
        if entity["status"] not in (m.MATCHING, m.TECHNICIAN_DECLINED, m.EXPIRED):
            raise m.WorkflowError("invalid_state", "Run find_matching_technicians first, or the request is already in progress.", 409)
        return self._dispatch_next(entity, technician_name, actor="agent")

    def _dispatch_next(self, entity: dict, technician_name: str | None = None, actor: str = "system") -> dict:
        tried = [t for t in entity.get("tried_technicians", "").split(",") if t]
        candidates = [c for c in (entity.get("candidates") or []) if c["technician_id"] not in tried]
        if technician_name:
            candidates = [c for c in candidates if (c.get("name") or "").lower() == technician_name.lower()] or []
        if not candidates or len(tried) >= MAX_TECHNICIAN_ATTEMPTS:
            failed = self._transition(entity, m.FAILED, "no_technician_available", {"tried": tried}, actor)
            logger.warning("SERVICE_REQUEST_FAILED request=%s reason=no_eligible_technician tried=%d", entity["RowKey"][-8:], len(tried))
            return self.public_view(failed, include_token=False)
        chosen = candidates[0]
        tech = self.technician(chosen["technician_id"])
        if not tech or not tech.get("phone"):
            raise m.WorkflowError("technician_unreachable", "Technician has no SMS number on file.", 500)
        attempt_id = new_id("sms")
        expires_at = self.clock() + timedelta(minutes=REPLY_TIMEOUT_MINUTES)
        self.store.insert("SmsMessages", {
            "PartitionKey": entity["RowKey"], "RowKey": attempt_id, "direction": "outbound",
            "technician_id": tech["RowKey"], "to_redacted": redact_phone(tech["phone"]),
            "status": "queued", "provider": "", "provider_message_id": "", "error": "",
            "created_at": iso_utc(self.clock()), "updated_at": iso_utc(self.clock()),
        })
        updated = self._transition(
            entity, m.AWAITING_TECHNICIAN, "sms_queued", {"technician_id": tech["RowKey"], "attempt_id": attempt_id}, actor,
            assigned_technician_id=tech["RowKey"],
            tried_technicians=",".join(tried + [tech["RowKey"]]),
            expires_at=iso_utc(expires_at), proposed_start="", proposed_end="",
        )
        self.queue.send(OUTBOUND_QUEUE, {"request_id": entity["RowKey"], "attempt_id": attempt_id})
        view = self.public_view(updated, include_token=False)
        view["technician"] = tech.get("name")
        view["reply_deadline"] = _local_str(expires_at)
        return view

    def sms_body(self, entity: dict, tech: dict) -> str:
        start, end = self._window(entity)
        area = entity.get("postal_fsa") or "Toronto area"
        return (f"Nexroza job {entity['reply_code']}: {m.SERVICE_TYPES[entity['service_type']]} in {area} "
                f"on {to_toronto(start).strftime('%a %b %d')} between {to_toronto(start).strftime('%I:%M %p').lstrip('0')} "
                f"and {to_toronto(end).strftime('%I:%M %p').lstrip('0')}. "
                f"Reply YES, a time (e.g. 5:30 PM), or NO. Reply STOP to opt out.")

    def process_outbound(self, message: dict) -> dict:
        """Queue handler: actually send the SMS. Idempotent per attempt."""
        request_id, attempt_id = message["request_id"], message["attempt_id"]
        attempt = self.store.get("SmsMessages", request_id, attempt_id)
        if attempt is None:
            raise m.WorkflowError("unknown_attempt", "Unknown SMS attempt.", 400)
        if attempt["status"] != "queued":
            return {"skipped": True, "status": attempt["status"]}  # retry after success: no double send
        entity = self._request(request_id)
        tech = self.technician(attempt["technician_id"])
        if entity["status"] != m.AWAITING_TECHNICIAN or entity.get("assigned_technician_id") != tech["RowKey"]:
            attempt.update(status="cancelled", updated_at=iso_utc(self.clock()))
            self.store.update("SmsMessages", attempt)
            return {"skipped": True, "status": "cancelled"}
        to_phone = os.environ.get("SMS_TEST_RECIPIENT_OVERRIDE") or tech["phone"]
        result = self.sms.send(to_phone, self.sms_body(entity, tech), entity["reply_code"])
        attempt.update(status=result.status, provider=result.provider,
                       provider_message_id=result.provider_message_id or "", error=result.error or "",
                       updated_at=iso_utc(self.clock()))
        self.store.update("SmsMessages", attempt)
        record_event(self.store, request_id, "sms_sent" if result.ok else "sms_failed", entity["status"], entity["status"],
                     {"attempt_id": attempt_id, "provider": result.provider, "status": result.status, "error": result.error})
        if not result.ok:
            logger.error("SMS_DELIVERY_FAILURE request=%s technician=%s error=%s", request_id[-8:], tech["RowKey"], result.error)
            declined = self._transition(entity, m.TECHNICIAN_DECLINED, "sms_failed", {"technician_id": tech["RowKey"]})
            self._dispatch_next(declined)
        return {"sent": result.ok, "status": result.status}

    # -- 5. inbound replies -------------------------------------------------------------
    def handle_inbound(self, inbound: InboundSms) -> dict:
        """Queue handler for a technician reply. Idempotent per provider message id."""
        if not claim_idempotency(self.store, f"{inbound.provider}:{inbound.provider_message_id}", "inbound_sms"):
            logger.info("SMS_INBOUND_DUPLICATE provider_message_id=%s", mask_id(inbound.provider_message_id))
            return {"duplicate": True}
        tech = self.technician_by_phone(inbound.from_phone)
        if tech is None:
            logger.warning("SMS_INBOUND_UNKNOWN_SENDER from=%s", redact_phone(inbound.from_phone))
            return {"ignored": "unknown_sender"}

        interpretation = interpret_reply(inbound.body, self.clock().astimezone(TORONTO_TZ).date())
        if interpretation.intent == "opt_out":
            tech.update(sms_opt_out=True, updated_at=iso_utc(self.clock()))
            self.store.update("Technicians", tech)
            logger.warning("SMS_OPT_OUT technician=%s", tech["RowKey"])
            for entity in self._awaiting_for(tech["RowKey"]):
                declined = self._transition(entity, m.TECHNICIAN_DECLINED, "technician_opted_out", {"technician_id": tech["RowKey"]}, "technician")
                self._dispatch_next(declined)
            return {"opt_out": True}

        entity = self._correlate(tech["RowKey"], interpretation.reply_code)
        if entity is None:
            self._store_inbound(None, tech, inbound, "uncorrelated")
            logger.warning("SMS_INBOUND_UNCORRELATED technician=%s", tech["RowKey"])
            return {"ignored": "no_pending_request"}

        window_start, window_end = self._window(entity)
        interpretation = interpret_reply(inbound.body, window_start.date(), window_start, window_end)
        self._store_inbound(entity["RowKey"], tech, inbound, interpretation.intent)

        if interpretation.intent == "decline":
            declined = self._transition(entity, m.TECHNICIAN_DECLINED, "technician_declined",
                                        {"technician_id": tech["RowKey"]}, "technician")
            self._dispatch_next(declined)
            return {"request": entity["RowKey"], "intent": "decline"}

        if interpretation.intent in ("accept", "propose"):
            start = interpretation.proposed_start or window_start
            end = start + default_duration()
            proposal_id = new_id("prop")
            self.store.insert("Proposals", {
                "PartitionKey": entity["RowKey"], "RowKey": proposal_id, "technician_id": tech["RowKey"],
                "proposed_start": start.isoformat(), "proposed_end": end.isoformat(),
                "source_message_id": inbound.provider_message_id, "note": interpretation.note,
                "created_at": iso_utc(self.clock()),
            })
            self._transition(entity, m.TECHNICIAN_PROPOSED_TIME, "technician_proposed",
                             {"technician_id": tech["RowKey"], "proposal_id": proposal_id, "start": start.isoformat()},
                             "technician", proposed_start=start.isoformat(), proposed_end=end.isoformat(),
                             expires_at=iso_utc(self.clock() + timedelta(hours=BOOKING_HOLD_HOURS)))
            return {"request": entity["RowKey"], "intent": interpretation.intent, "proposed_start": start.isoformat()}

        logger.info("SMS_INBOUND_UNINTERPRETED request=%s", entity["RowKey"][-8:])
        return {"request": entity["RowKey"], "intent": "unknown"}

    def _awaiting_for(self, technician_id: str) -> list[dict]:
        rows = self.store.query("ServiceRequests",
                                f"PartitionKey eq 'request' and assigned_technician_id eq '{technician_id}' and status eq '{m.AWAITING_TECHNICIAN}'")
        return sorted(rows, key=lambda r: r.get("updated_at", ""), reverse=True)

    def _correlate(self, technician_id: str, reply_code: str | None) -> dict | None:
        awaiting = self._awaiting_for(technician_id)
        if reply_code:
            for entity in awaiting:
                if entity.get("reply_code") == reply_code:
                    return entity
        return awaiting[0] if awaiting else None

    def _store_inbound(self, request_id: str | None, tech: dict, inbound: InboundSms, intent: str) -> None:
        self.store.insert("SmsMessages", {
            "PartitionKey": request_id or "uncorrelated", "RowKey": new_id("in"), "direction": "inbound",
            "technician_id": tech["RowKey"], "from_redacted": redact_phone(inbound.from_phone),
            "provider": inbound.provider, "provider_message_id": inbound.provider_message_id,
            "body": inbound.body[:320], "intent": intent, "status": "received",
            "created_at": inbound.received_at, "updated_at": iso_utc(self.clock()),
        })

    # -- 6. expiry timer --------------------------------------------------------------------
    def expire_requests(self) -> dict:
        now = iso_utc(self.clock())
        expired = 0
        for status in (m.AWAITING_TECHNICIAN, m.TECHNICIAN_PROPOSED_TIME, m.AWAITING_CUSTOMER_CONFIRMATION):
            rows = self.store.query("ServiceRequests", f"PartitionKey eq 'request' and status eq '{status}'")
            for entity in rows:
                if not entity.get("expires_at") or entity["expires_at"] > now:
                    continue
                try:
                    if status == m.AWAITING_TECHNICIAN:
                        logger.warning("REQUEST_EXPIRED request=%s technician=%s", entity["RowKey"][-8:], entity.get("assigned_technician_id"))
                        moved = self._transition(entity, m.EXPIRED, "technician_timeout", {"technician_id": entity.get("assigned_technician_id")})
                        self._dispatch_next(moved)
                    else:
                        logger.warning("REQUEST_EXPIRED request=%s stage=%s", entity["RowKey"][-8:], status)
                        self._transition(entity, m.EXPIRED, "customer_timeout", {})
                    expired += 1
                except ConcurrencyConflict:
                    continue
        return {"expired": expired}

    # -- 7. status ----------------------------------------------------------------------------
    def get_service_request_status(self, tracking_token: str, phone: str) -> dict:
        entity = self._request_by_token(tracking_token, phone)
        if entity["status"] == m.TECHNICIAN_PROPOSED_TIME:
            entity = self._transition(entity, m.AWAITING_CUSTOMER_CONFIRMATION, "proposal_presented", {}, "agent")
        return self.public_view(entity, include_token=True, token=tracking_token.upper())

    def public_view(self, entity: dict, include_token: bool, token: str | None = None) -> dict:
        tech = self.technician(entity["assigned_technician_id"]) if entity.get("assigned_technician_id") else None
        view = {
            "status": entity["status"],
            "status_text": m.CUSTOMER_STATUS_TEXT.get(entity["status"], entity["status"]),
            "service_type": m.SERVICE_TYPES.get(entity["service_type"], entity["service_type"]),
            "service_date": entity.get("service_date"),
            "requested_window": {"from": _local_str(entity.get("window_start")), "to": _local_str(entity.get("window_end"))},
            "technician": (tech or {}).get("name") if entity["status"] in (
                m.AWAITING_TECHNICIAN, m.TECHNICIAN_PROPOSED_TIME, m.AWAITING_CUSTOMER_CONFIRMATION, m.CONFIRMED) else None,
            "proposed_time": None,
            "next_step": None,
        }
        if include_token and token:
            view["tracking_reference"] = token
        if entity["status"] in (m.AWAITING_CUSTOMER_CONFIRMATION, m.TECHNICIAN_PROPOSED_TIME, m.CONFIRMED) and entity.get("proposed_start"):
            view["proposed_time"] = {"start": _local_str(entity["proposed_start"]), "end": _local_str(entity["proposed_end"]),
                                     "spoken": _local_speech(entity["proposed_start"])}
        view["next_step"] = {
            m.NEW: "Find matching technicians.",
            m.MATCHING: "Send the request to a technician.",
            m.AWAITING_TECHNICIAN: "Wait for the technician's SMS reply; check back later with the tracking reference.",
            m.TECHNICIAN_DECLINED: "Another technician is being contacted.",
            m.EXPIRED: "Another technician is being contacted." if not entity.get("proposed_start") else "The hold expired; a new request is needed.",
            m.TECHNICIAN_PROPOSED_TIME: "Ask the customer to confirm the proposed time.",
            m.AWAITING_CUSTOMER_CONFIRMATION: "Ask the customer to confirm the proposed time, then call confirm_booking.",
            m.CONFIRMED: "Booked. The technician will arrive in the confirmed window.",
            m.CANCELLED: "Nothing further.",
            m.FAILED: "The office will call the customer back.",
        }.get(entity["status"])
        return view

    # -- 8. confirm ------------------------------------------------------------------------------
    def confirm_booking(self, tracking_token: str, phone: str, customer_accepts: bool, correlation_id: str) -> dict:
        entity = self._request_by_token(tracking_token, phone)
        if entity["status"] == m.CONFIRMED:
            return self.public_view(entity, include_token=True, token=tracking_token.upper()) | {"confirmed": True, "already_confirmed": True}
        if entity["status"] != m.AWAITING_CUSTOMER_CONFIRMATION:
            raise m.WorkflowError("invalid_state", "There is no proposed time waiting for confirmation.", 409)
        tech = self.technician(entity.get("assigned_technician_id") or "")
        start, end = parse_iso(entity.get("proposed_start")), parse_iso(entity.get("proposed_end"))
        if not tech or not start or not end:
            raise m.WorkflowError("invalid_proposal", "The proposal is incomplete; please contact the office.", 409)

        if not customer_accepts:
            moved = self._transition(entity, m.AWAITING_TECHNICIAN, "customer_rejected_time", {"technician_id": tech["RowKey"]}, "customer",
                                     proposed_start="", proposed_end="",
                                     expires_at=iso_utc(self.clock() + timedelta(minutes=REPLY_TIMEOUT_MINUTES)))
            # ask the same technician for another time via a fresh SMS attempt
            attempt_id = new_id("sms")
            self.store.insert("SmsMessages", {"PartitionKey": entity["RowKey"], "RowKey": attempt_id, "direction": "outbound",
                                              "technician_id": tech["RowKey"], "to_redacted": redact_phone(tech["phone"]),
                                              "status": "queued", "provider": "", "provider_message_id": "", "error": "",
                                              "created_at": iso_utc(self.clock()), "updated_at": iso_utc(self.clock())})
            self.queue.send(OUTBOUND_QUEUE, {"request_id": entity["RowKey"], "attempt_id": attempt_id})
            return self.public_view(moved, include_token=True, token=tracking_token.upper()) | {"confirmed": False}

        # 1. take the confirmation lock (only one caller wins the ETag race)
        lock = new_id("lock")
        try:
            locked = self.store.update("ServiceRequests", {**entity, "confirm_lock": lock, "updated_at": iso_utc(self.clock())},
                                       etag=entity.get("etag"))
        except ConcurrencyConflict:
            raise m.WorkflowError("confirmation_in_progress", "This booking is already being confirmed.", 409)

        def release(reason: str):
            try:
                self.store.update("ServiceRequests", {**locked, "confirm_lock": ""}, etag=locked.get("etag"))
            except ConcurrencyConflict:
                pass
            logger.error("BOOKING_FAILED request=%s reason=%s", entity["RowKey"][-8:], reason)
            record_event(self.store, entity["RowKey"], "booking_failed", entity["status"], entity["status"], {"reason": reason}, "system")

        if self.graph is None:
            release("graph_unavailable")
            raise m.WorkflowError("booking_failed", "Calendar service is unavailable; the booking is not confirmed.", 503)

        # 2. re-check Outlook right now
        try:
            calendars = self.graph.calendar_map(correlation_id)
            calendar_id = calendars.get(tech.get("calendar_name"))
            if not calendar_id:
                release("calendar_not_found")
                raise m.WorkflowError("booking_failed", "The technician's calendar is unavailable; the booking is not confirmed.", 503)
            status, _, events = day_status(self.graph, calendar_id, start.date(), correlation_id)
            if status in ("OFF", "SICK", "VACATION"):
                release(f"technician_{status.lower()}")
                raise m.WorkflowError("technician_unavailable", "The technician is no longer available that day; the booking is not confirmed.", 409)
            if busy_in_window(events, start, end):
                release("slot_taken")
                raise m.WorkflowError("slot_taken", "That time was just taken on the technician's calendar; the booking is not confirmed.", 409)
            # 3. create the event
            subject = f"[NX] {m.SERVICE_TYPES[entity['service_type']]} - {entity['customer_name'].split()[0]} ({entity.get('postal_fsa') or 'Toronto'})"
            body = (f"Nexroza job {entity['reply_code']}\nCustomer: {entity['customer_name']}\nPhone: {entity['customer_phone']}\n"
                    f"Address: {entity['address']}\nIssue: {entity['issue']}\nTracking: {tracking_token.upper()}")
            event = self.graph.create_event(calendar_id, subject, body, start, end, correlation_id, location=entity["address"])
        except (OutlookAuthorizationError, GraphError) as exc:
            release(type(exc).__name__)
            raise m.WorkflowError("booking_failed", "The calendar could not be updated; the booking is not confirmed.", 503) from exc

        event_id = event.get("id", "")
        booking_id = new_id("bk")
        self.store.insert("Bookings", {"PartitionKey": entity["RowKey"], "RowKey": booking_id, "technician_id": tech["RowKey"],
                                       "calendar_id": calendar_id, "graph_event_id": event_id,
                                       "start": start.isoformat(), "end": end.isoformat(), "created_at": iso_utc(self.clock())})
        # 4. confirm (still under our lock / etag)
        confirmed = self._transition(locked, m.CONFIRMED, "booked", {"booking_id": booking_id, "graph_event_id": event_id[:12] + "…"},
                                     "customer", etag=locked.get("etag"), confirm_lock="", booking_id=booking_id, graph_event_id=event_id,
                                     expires_at="")
        logger.info("BOOKING_CONFIRMED request=%s technician=%s", entity["RowKey"][-8:], tech["RowKey"])
        return self.public_view(confirmed, include_token=True, token=tracking_token.upper()) | {"confirmed": True}

    def cancel_request(self, tracking_token: str, phone: str) -> dict:
        entity = self._request_by_token(tracking_token, phone)
        if entity["status"] in m.TERMINAL_STATES:
            raise m.WorkflowError("invalid_state", "This request can no longer be cancelled.", 409)
        moved = self._transition(entity, m.CANCELLED, "customer_cancelled", {}, "customer", expires_at="")
        return self.public_view(moved, include_token=True, token=tracking_token.upper())
