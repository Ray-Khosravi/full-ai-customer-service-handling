"""Nexroza Plumbing service-request MCP server (Azure Functions, Python).

MCP tools (called by the Microsoft Foundry agent, protected by the
``mcp_extension`` system key):

    list_technician_calendars, get_technician_work_status,
    create_service_request, find_matching_technicians, check_availability,
    send_technician_request, get_service_request_status, confirm_booking

Async plumbing:

    POST /api/sms/inbound      provider webhook (signature-validated) -> queue
    queue sms-outbound         sends the technician SMS (idempotent per attempt)
    queue sms-inbound          interprets/correlates replies (idempotent per message)
    *-poison queues            logged as POISON_MESSAGE and the request is failed
    timer expire_requests      technician timeout -> next technician; hold expiry
    timer outlook_heartbeat    Outlook authorization health

The owner's delegated Graph token is refreshed silently from blob storage
(see nexroza/graph.py); website visitors never sign in to Microsoft.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime

import azure.functions as func

from nexroza import models as m
from nexroza.graph import GraphClient, GraphError, OutlookAuthorizationError, day_status
from nexroza.seed import seed as seed_data
from nexroza.sms import InboundSms, WebhookSignatureError, get_provider
from nexroza.store import TableStore
from nexroza.util import iso_utc, mask_id, redact_phone
from nexroza.workflow import INBOUND_QUEUE, OUTBOUND_QUEUE, Queue, Workflow, azure_queue_sender

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
logger = logging.getLogger("nexroza.app")

_workflow: Workflow | None = None


def workflow() -> Workflow:
    global _workflow
    if _workflow is None:
        _workflow = Workflow(TableStore(), GraphClient(), get_provider(), Queue(azure_queue_sender()))
    return _workflow


def _json(payload: dict | list, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(json.dumps(payload, default=str), status_code=status, mimetype="application/json")


def _tool_result(fn, correlation_id: str):
    """Run a tool body; convert every failure into a safe JSON error."""
    try:
        result = fn()
    except m.WorkflowError as exc:
        return json.dumps({"error": exc.code, "message": str(exc), "correlation_id": correlation_id})
    except OutlookAuthorizationError as exc:
        return json.dumps({"error": exc.code, "message": "Live calendar access is unavailable right now.", "correlation_id": correlation_id})
    except GraphError as exc:
        return json.dumps({"error": "graph_error", "graph_code": exc.code, "correlation_id": correlation_id})
    except Exception:  # noqa: BLE001
        logger.exception("Unexpected failure correlation_id=%s", correlation_id)
        return json.dumps({"error": "internal_error", "message": "The request could not be processed.", "correlation_id": correlation_id})
    return json.dumps(result, indent=2, default=str)


# --------------------------------------------------------------------------- #
# Calendar tools (kept from the first milestone, now database-driven)
# --------------------------------------------------------------------------- #

@app.mcp_tool()
def list_technician_calendars() -> str:
    """List configured technicians, their skills and whether their Outlook calendar exists."""
    correlation_id = str(uuid.uuid4())

    def body():
        wf = workflow()
        calendars = wf.graph.calendar_map(correlation_id)
        return [{"technician": t.get("name"), "calendar": t.get("calendar_name"), "skills": t.get("skills"),
                 "active": bool(t.get("active", True)), "found": t.get("calendar_name") in calendars}
                for t in wf.technicians()]
    return _tool_result(body, correlation_id)


@app.mcp_tool()
@app.mcp_tool_property(arg_name="date", description="Date to check in YYYY-MM-DD format, interpreted in Toronto time.", is_required=True)
def get_technician_work_status(date: str) -> str:
    """Check whether each technician is WORKING, OFF, SICK, VACATION, or ON_CALL on a date (Toronto)."""
    correlation_id = str(uuid.uuid4())

    def body():
        try:
            requested = datetime.strptime(date, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            raise m.WorkflowError("invalid_date", "date must use YYYY-MM-DD format")
        wf = workflow()
        calendars = wf.graph.calendar_map(correlation_id)
        results = []
        for tech in wf.technicians():
            calendar_id = calendars.get(tech.get("calendar_name"))
            item = {"technician": tech.get("name"), "skills": tech.get("skills")}
            if not calendar_id:
                item["status"] = "CALENDAR_NOT_FOUND"
            else:
                status, subjects, _ = day_status(wf.graph, calendar_id, requested, correlation_id)
                item["status"] = status
                item["status_events"] = subjects
            results.append(item)
        return {"date": date, "timezone": "America/Toronto", "technicians": results}
    return _tool_result(body, correlation_id)


# --------------------------------------------------------------------------- #
# Service-request tools
# --------------------------------------------------------------------------- #

@app.mcp_tool()
@app.mcp_tool_property(arg_name="customer_name", description="Customer's name.", is_required=True)
@app.mcp_tool_property(arg_name="callback_phone", description="Customer callback phone number (North American).", is_required=True)
@app.mcp_tool_property(arg_name="address", description="Service address (street, city).", is_required=True)
@app.mcp_tool_property(arg_name="postal_code", description="Canadian postal code, at least the first 3 characters (e.g. M1B).", is_required=False)
@app.mcp_tool_property(arg_name="service_type", description="One of general_plumbing, drain_services, water_heaters.", is_required=True)
@app.mcp_tool_property(arg_name="issue", description="Short description of the problem (no more than a couple of sentences).", is_required=True)
@app.mcp_tool_property(arg_name="preferred_date", description="Preferred service date YYYY-MM-DD (Toronto).", is_required=True)
@app.mcp_tool_property(arg_name="preferred_time_from", description="Earliest acceptable time HH:MM 24h Toronto (default 08:00).", is_required=False)
@app.mcp_tool_property(arg_name="preferred_time_to", description="Latest acceptable time HH:MM 24h Toronto (default 20:00).", is_required=False)
@app.mcp_tool_property(arg_name="sms_consent", description="true if the customer agreed to receive SMS updates.", property_type=func.McpPropertyType.BOOLEAN, is_required=False)
@app.mcp_tool_property(arg_name="is_emergency", description="true when the safety check found an urgent situation (allows on-call technicians).", property_type=func.McpPropertyType.BOOLEAN, is_required=False)
@app.mcp_tool_property(arg_name="idempotency_key", description="Optional unique key so a retried call does not create a duplicate request.", is_required=False)
def create_service_request(customer_name: str, callback_phone: str, address: str, service_type: str, issue: str,
                           preferred_date: str, postal_code: str = "", preferred_time_from: str = "",
                           preferred_time_to: str = "", sms_consent: bool = False, is_emergency: bool = False,
                           idempotency_key: str = "") -> str:
    """Create a new service request (status new) and return the customer's tracking reference. Call after the safety check and after collecting name, phone, address, service type, issue and preferred date/time."""
    correlation_id = str(uuid.uuid4())
    return _tool_result(lambda: workflow().create_service_request(
        customer_name=customer_name, callback_phone=callback_phone, address=address, postal_code=postal_code,
        service_type=service_type, issue=issue, preferred_date=preferred_date,
        preferred_time_from=preferred_time_from or None, preferred_time_to=preferred_time_to or None,
        sms_consent=bool(sms_consent), is_emergency=bool(is_emergency), idempotency_key=idempotency_key or None,
    ), correlation_id)


@app.mcp_tool()
@app.mcp_tool_property(arg_name="tracking_reference", description="The customer's tracking reference (NX-...).", is_required=True)
@app.mcp_tool_property(arg_name="callback_phone", description="The callback phone given for the request (verification).", is_required=True)
def find_matching_technicians(tracking_reference: str, callback_phone: str) -> str:
    """Match qualified, active technicians for a request using skill, service area, weekly schedule and live Outlook availability. Never returns OFF/SICK/VACATION technicians."""
    correlation_id = str(uuid.uuid4())
    return _tool_result(lambda: workflow().find_matching_technicians(tracking_reference, callback_phone, correlation_id), correlation_id)


@app.mcp_tool()
@app.mcp_tool_property(arg_name="service_type", description="One of general_plumbing, drain_services, water_heaters.", is_required=True)
@app.mcp_tool_property(arg_name="date", description="Date YYYY-MM-DD (Toronto).", is_required=True)
@app.mcp_tool_property(arg_name="postal_code", description="Optional postal code / FSA to check service-area coverage.", is_required=False)
@app.mcp_tool_property(arg_name="time_from", description="Optional earliest time HH:MM (24h Toronto).", is_required=False)
@app.mcp_tool_property(arg_name="time_to", description="Optional latest time HH:MM (24h Toronto).", is_required=False)
@app.mcp_tool_property(arg_name="emergency", description="true to include on-call technicians as an emergency fallback.", property_type=func.McpPropertyType.BOOLEAN, is_required=False)
def check_availability(service_type: str, date: str, postal_code: str = "", time_from: str = "", time_to: str = "",
                       emergency: bool = False) -> str:
    """Check which technicians could serve a service type on a date: weekly schedule plus live Outlook status. Use before promising anything about availability."""
    correlation_id = str(uuid.uuid4())
    return _tool_result(lambda: workflow().check_availability(
        service_type, date, postal_code or None, time_from or None, time_to or None, correlation_id, emergency=bool(emergency)), correlation_id)


@app.mcp_tool()
@app.mcp_tool_property(arg_name="tracking_reference", description="The customer's tracking reference (NX-...).", is_required=True)
@app.mcp_tool_property(arg_name="callback_phone", description="The callback phone given for the request (verification).", is_required=True)
@app.mcp_tool_property(arg_name="technician_name", description="Optional technician to contact first (must be one of the matched candidates).", is_required=False)
def send_technician_request(tracking_reference: str, callback_phone: str, technician_name: str = "") -> str:
    """Queue an SMS request to the best eligible technician and return immediately (status awaiting_technician). The technician's reply arrives asynchronously; do not wait for it."""
    correlation_id = str(uuid.uuid4())
    return _tool_result(lambda: workflow().send_technician_request(tracking_reference, callback_phone, technician_name or None), correlation_id)


@app.mcp_tool()
@app.mcp_tool_property(arg_name="tracking_reference", description="The customer's tracking reference (NX-...).", is_required=True)
@app.mcp_tool_property(arg_name="callback_phone", description="The callback phone given for the request, or its last 4 digits (verification).", is_required=True)
def get_service_request_status(tracking_reference: str, callback_phone: str) -> str:
    """Get the current status of a service request, including any time a technician proposed. Requires the tracking reference plus the callback phone for verification."""
    correlation_id = str(uuid.uuid4())
    return _tool_result(lambda: workflow().get_service_request_status(tracking_reference, callback_phone), correlation_id)


@app.mcp_tool()
@app.mcp_tool_property(arg_name="tracking_reference", description="The customer's tracking reference (NX-...).", is_required=True)
@app.mcp_tool_property(arg_name="callback_phone", description="The callback phone given for the request (verification).", is_required=True)
@app.mcp_tool_property(arg_name="customer_accepts", description="true if the customer accepted the proposed time; false to ask the technician for another time.", property_type=func.McpPropertyType.BOOLEAN, is_required=True)
def confirm_booking(tracking_reference: str, callback_phone: str, customer_accepts: bool) -> str:
    """Confirm the proposed time: re-checks Outlook and creates the calendar booking. Only report a booking as confirmed when this returns confirmed=true."""
    correlation_id = str(uuid.uuid4())
    return _tool_result(lambda: workflow().confirm_booking(tracking_reference, callback_phone, bool(customer_accepts), correlation_id), correlation_id)


# --------------------------------------------------------------------------- #
# SMS webhook (provider -> queue) and queue handlers
# --------------------------------------------------------------------------- #

@app.route(route="sms/inbound", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
@app.queue_output(arg_name="outbox", queue_name=INBOUND_QUEUE, connection="AzureWebJobsStorage")
def sms_inbound(req: func.HttpRequest, outbox: func.Out[str]) -> func.HttpResponse:
    """Provider webhook. Validates the provider signature, then enqueues. Anonymous at the
    platform level because providers cannot send function keys; the signature is the auth."""
    provider = get_provider()
    raw = req.get_body()
    # Event Grid subscription validation handshake (ACS)
    if req.headers.get("aeg-event-type") == "SubscriptionValidation":
        try:
            code = json.loads(raw)[0]["data"]["validationCode"]
            return _json({"validationResponse": code})
        except (ValueError, KeyError, IndexError):
            return _json({"error": "bad_validation"}, 400)
    try:
        messages = provider.parse_inbound(dict(req.headers), raw, req.url, dict(req.params))
    except WebhookSignatureError:
        logger.warning("SMS_WEBHOOK_SIGNATURE_INVALID provider=%s", provider.name)
        return func.HttpResponse("forbidden", status_code=403)
    except (ValueError, KeyError) as exc:
        logger.warning("SMS_WEBHOOK_BAD_PAYLOAD provider=%s error=%s", provider.name, type(exc).__name__)
        return func.HttpResponse("bad request", status_code=400)
    for inbound in messages:
        logger.info("SMS_INBOUND provider=%s from=%s provider_message_id=%s", inbound.provider, redact_phone(inbound.from_phone), mask_id(inbound.provider_message_id))
        outbox.set(json.dumps(inbound.__dict__))
    if provider.name == "twilio":
        return func.HttpResponse("<Response></Response>", mimetype="text/xml")
    return _json({"accepted": len(messages)})


@app.route(route="sms/status", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def sms_status(req: func.HttpRequest) -> func.HttpResponse:
    """Provider delivery receipt (Twilio StatusCallback). Signature-validated; updates the stored attempt."""
    provider = get_provider()
    if not hasattr(provider, "parse_status"):
        return func.HttpResponse("not supported", status_code=404)
    try:
        sid, status_text, error_code = provider.parse_status(dict(req.headers), req.get_body(), req.url)
    except WebhookSignatureError:
        logger.warning("SMS_WEBHOOK_SIGNATURE_INVALID provider=%s route=status", provider.name)
        return func.HttpResponse("forbidden", status_code=403)
    except (ValueError, KeyError):
        return func.HttpResponse("bad request", status_code=400)
    if sid:
        wf = workflow()
        for attempt in wf.store.query("SmsMessages", f"provider_message_id eq '{sid}'", limit=1):
            attempt.update(status=status_text or attempt.get("status"), error=str(error_code or ""), updated_at=iso_utc())
            wf.store.update("SmsMessages", attempt)
            level = logger.error if status_text in ("failed", "undelivered") else logger.info
            level("SMS_DELIVERY_STATUS provider=%s status=%s error=%s request=%s", provider.name, status_text, error_code or "", attempt["PartitionKey"][-8:])
            if status_text in ("failed", "undelivered"):
                logger.error("SMS_DELIVERY_FAILURE request=%s technician=%s error=%s", attempt["PartitionKey"][-8:], attempt.get("technician_id"), error_code)
    return func.HttpResponse("", status_code=204)


@app.queue_trigger(arg_name="msg", queue_name=OUTBOUND_QUEUE, connection="AzureWebJobsStorage")
def process_sms_outbound(msg: func.QueueMessage) -> None:
    payload = json.loads(msg.get_body().decode("utf-8"))
    result = workflow().process_outbound(payload)
    logger.info("SMS_OUTBOUND_PROCESSED request=%s dequeue=%s result=%s", payload.get("request_id", "")[-8:], msg.dequeue_count, result)


@app.queue_trigger(arg_name="msg", queue_name=INBOUND_QUEUE, connection="AzureWebJobsStorage")
def process_sms_inbound(msg: func.QueueMessage) -> None:
    payload = json.loads(msg.get_body().decode("utf-8"))
    inbound = InboundSms(**payload)
    result = workflow().handle_inbound(inbound)
    logger.info("SMS_INBOUND_PROCESSED provider_message_id=%s dequeue=%s result=%s", mask_id(inbound.provider_message_id), msg.dequeue_count, result)


def _poison(queue_name: str, msg: func.QueueMessage) -> None:
    body = msg.get_body().decode("utf-8", errors="replace")
    request_id = ""
    try:
        request_id = str(json.loads(body).get("request_id", ""))
    except (ValueError, AttributeError):
        pass
    logger.error("POISON_MESSAGE queue=%s message_id=%s request=%s bytes=%d", queue_name, msg.id, request_id[-8:], len(body))
    if request_id:
        try:
            wf = workflow()
            entity = wf.store.get("ServiceRequests", "request", request_id)
            if entity and entity["status"] not in m.TERMINAL_STATES:
                wf._transition(entity, m.FAILED, "poison_message", {"queue": queue_name, "message_id": msg.id})
                logger.warning("SERVICE_REQUEST_FAILED request=%s reason=poison_message", request_id[-8:])
        except Exception:  # noqa: BLE001
            logger.exception("Poison handling failed for request=%s", request_id[-8:])


@app.queue_trigger(arg_name="msg", queue_name=f"{OUTBOUND_QUEUE}-poison", connection="AzureWebJobsStorage")
def poison_sms_outbound(msg: func.QueueMessage) -> None:
    _poison(f"{OUTBOUND_QUEUE}-poison", msg)


@app.queue_trigger(arg_name="msg", queue_name=f"{INBOUND_QUEUE}-poison", connection="AzureWebJobsStorage")
def poison_sms_inbound(msg: func.QueueMessage) -> None:
    _poison(f"{INBOUND_QUEUE}-poison", msg)


# --------------------------------------------------------------------------- #
# Timers
# --------------------------------------------------------------------------- #

@app.timer_trigger(schedule="0 */5 * * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def expire_requests(timer: func.TimerRequest) -> None:
    result = workflow().expire_requests()
    if result["expired"]:
        logger.info("EXPIRY_SWEEP expired=%d", result["expired"])


@app.timer_trigger(schedule="0 */30 * * * *", arg_name="timer", run_on_startup=False, use_monitor=True)
def outlook_heartbeat(timer: func.TimerRequest) -> None:
    """Every 30 minutes, prove the stored authorization still reaches Graph."""
    correlation_id = str(uuid.uuid4())
    try:
        wf = workflow()
        calendars = wf.graph.calendar_map(correlation_id)
    except OutlookAuthorizationError as exc:
        logger.error("OUTLOOK_HEARTBEAT_FAILED error=%s correlation_id=%s", exc.code, correlation_id)
        return
    except GraphError as exc:
        logger.error("OUTLOOK_HEARTBEAT_FAILED error=graph_%s correlation_id=%s", exc.code, correlation_id)
        return
    except Exception:  # noqa: BLE001
        logger.exception("OUTLOOK_HEARTBEAT_FAILED error=internal correlation_id=%s", correlation_id)
        return
    missing = [t.get("calendar_name") for t in wf.technicians() if t.get("calendar_name") not in calendars]
    if missing:
        logger.warning("OUTLOOK_HEARTBEAT_DEGRADED missing_calendars=%s correlation_id=%s", missing, correlation_id)
    else:
        logger.info("OUTLOOK_HEARTBEAT_OK correlation_id=%s", correlation_id)


# --------------------------------------------------------------------------- #
# Operations endpoints (function key; no customer exposure)
# --------------------------------------------------------------------------- #

@app.route(route="status", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def status(req: func.HttpRequest) -> func.HttpResponse:
    correlation_id = str(uuid.uuid4())
    body: dict = {"correlation_id": correlation_id, "outlook_connected": False, "sms_provider": os.environ.get("SMS_PROVIDER", "mock")}
    try:
        wf = workflow()
        calendars = wf.graph.calendar_map(correlation_id)
        body["outlook_connected"] = True
        body["missing_calendars"] = [t.get("calendar_name") for t in wf.technicians() if t.get("calendar_name") not in calendars]
        body["technicians"] = len(wf.technicians())
        return _json(body, 200)
    except OutlookAuthorizationError as exc:
        body["error"] = exc.code
        return _json(body, 503)
    except GraphError as exc:
        body["error"], body["graph_code"] = "graph_error", exc.code
        return _json(body, 502)
    except Exception:  # noqa: BLE001
        logger.exception("Status check failed correlation_id=%s", correlation_id)
        body["error"] = "internal_error"
        return _json(body, 500)


@app.route(route="ops/seed", methods=["POST"], auth_level=func.AuthLevel.ADMIN)
def ops_seed(req: func.HttpRequest) -> func.HttpResponse:
    """Re-seed skills/technicians (master key). Body: {"overwrite": bool, "phones": {"tech_john": "+1..."}}."""
    try:
        payload = req.get_json() if req.get_body() else {}
    except ValueError:
        payload = {}
    wf = workflow()
    result = seed_data(wf.store, overwrite=bool(payload.get("overwrite")), phone_overrides=payload.get("phones") or None)
    return _json({"seeded": result, "at": iso_utc()})


@app.route(route="ops/requests/{tracking}", methods=["GET"], auth_level=func.AuthLevel.ADMIN)
def ops_request(req: func.HttpRequest) -> func.HttpResponse:
    """Operator view of one request by tracking reference (master key): status, events, sms attempts (redacted)."""
    from nexroza.util import sha256
    token = (req.route_params.get("tracking") or "").upper()
    wf = workflow()
    rows = wf.store.query("ServiceRequests", f"PartitionKey eq 'request' and tracking_hash eq '{sha256(token)}'", limit=1)
    if not rows:
        return _json({"error": "not_found"}, 404)
    entity = rows[0]
    rid = entity["RowKey"]
    view = {k: v for k, v in entity.items() if k not in ("customer_phone", "tracking_hash", "etag", "PartitionKey")}
    view["customer_phone"] = redact_phone(entity.get("customer_phone"))
    view["events"] = [{k: e.get(k) for k in ("RowKey", "kind", "from_status", "to_status", "actor", "detail")}
                      for e in wf.store.query("RequestEvents", f"PartitionKey eq '{rid}'")]
    view["sms"] = [{k: s.get(k) for k in ("RowKey", "direction", "technician_id", "status", "provider", "provider_message_id", "intent", "error", "created_at")}
                   for s in wf.store.query("SmsMessages", f"PartitionKey eq '{rid}'")]
    view["bookings"] = [{k: b.get(k) for k in ("RowKey", "technician_id", "graph_event_id", "start", "end")} for b in wf.store.query("Bookings", f"PartitionKey eq '{rid}'")]
    return _json(view)
