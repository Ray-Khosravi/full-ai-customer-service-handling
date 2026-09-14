"""Hybrid live test: REAL Twilio outbound SMS + SIMULATED carrier->Twilio inbound step.

Real:      create -> match -> send (queue) -> Twilio API send -> Twilio delivery status,
           signed webhook validation, queues, database, state machine, MCP, Foundry agent.
Simulated: only the carrier transport of the technician's reply - we build the exact
           Twilio inbound webhook payload and sign it with the configured auth token.

Prints no secrets, no full phone numbers, no message SIDs, no signatures.
Never confirms a booking. Requires az login.

    python foundry/test_twilio_hybrid.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "FunctionsMcpTool"))
from nexroza.sms import TwilioSmsProvider  # noqa: E402
from nexroza.util import redact_phone  # noqa: E402
from test_mcp_workflow import BASE, Mcp, admin_view, wait_for  # noqa: E402
import deployconf as cfg  # noqa: E402

RG, APP = cfg.RG, cfg.APP
WEBHOOK = f"{BASE}/api/sms/inbound"
SHELL = sys.platform.startswith("win")


def az(*args) -> str:
    return subprocess.run(["az", *args], capture_output=True, text=True, shell=SHELL, check=True).stdout.strip()


def settings() -> dict:
    return {s["name"]: s["value"] for s in json.loads(az("functionapp", "config", "appsettings", "list", "-g", RG, "-n", APP, "-o", "json"))}


def mask_sid(sid: str) -> str:
    return (sid[:2] + "…" + sid[-4:]) if sid else "-"


def main() -> None:
    cfg = settings()
    keys = json.loads(az("functionapp", "keys", "list", "-g", RG, "-n", APP, "-o", "json"))
    mcp, master = Mcp(keys["systemKeys"]["mcp_extension"]), keys["masterKey"]
    sid, token, from_number = cfg["TWILIO_ACCOUNT_SID"], cfg["TWILIO_AUTH_TOKEN"], cfg["TWILIO_FROM_NUMBER"]
    test_to = cfg["SMS_TEST_RECIPIENT_OVERRIDE"]
    assert cfg.get("SMS_PROVIDER") == "twilio", "SMS_PROVIDER must be twilio"
    print(f"provider=twilio  from={redact_phone(from_number)}  override_to={redact_phone(test_to)}  account=Trial")

    customer_phone = "416-555-0181"
    if "--resume" in sys.argv:
        ref = resume_latest(customer_phone)
        print(f"[1-2] resuming with existing request {ref[:5]}… (no new SMS is sent)")
    else:
        ref = create_and_send(mcp, customer_phone)
    run_rest(mcp, master, sid, token, from_number, test_to, ref, customer_phone)


def create_and_send(mcp, customer_phone) -> str:
    day = datetime.now(ZoneInfo("America/Toronto")).date() + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    created = mcp.call("create_service_request", customer_name="Live Test Customer", callback_phone=customer_phone,
                       address="9 Test Crescent, Scarborough", postal_code="M1B 3C4", service_type="water_heaters",
                       issue="gas water heater leaking (live SMS test)", preferred_date=day.isoformat(),
                       preferred_time_from="16:00", preferred_time_to="20:00", sms_consent=True, is_emergency=False,
                       idempotency_key=str(uuid.uuid4()))
    ref = created["tracking_reference"]
    match = mcp.call("find_matching_technicians", tracking_reference=ref, callback_phone=customer_phone)
    print(f"[1] request {ref[:5]}… created for {day}; candidates: {[c['technician'] for c in match['candidates']]}")
    assert match["candidates"] and match["candidates"][0]["technician"] == "Michael"
    sent = mcp.call("send_technician_request", tracking_reference=ref, callback_phone=customer_phone, technician_name="Michael")
    print(f"[2] send_technician_request -> {sent['status']} (technician {sent['technician']}) - returned immediately")
    return ref


def resume_latest(customer_phone: str) -> str:
    """Rotate a fresh tracking token onto the most recent live-test request (tokens are stored hashed)."""
    from azure.data.tables import TableClient, UpdateMode
    from azure.identity import AzureCliCredential
    from nexroza.util import new_tracking_token, normalize_phone, sha256
    client = TableClient(endpoint=cfg.TABLE_ENDPOINT, table_name="ServiceRequests", credential=AzureCliCredential())
    rows = [r for r in client.query_entities("PartitionKey eq 'request'") if r.get("customer_phone") == normalize_phone(customer_phone)]
    row = sorted(rows, key=lambda r: r.get("created_at", ""))[-1]
    token = new_tracking_token()
    row["tracking_hash"] = sha256(token)
    client.update_entity(row, mode=UpdateMode.MERGE)
    return token


def run_rest(mcp, master, sid, token, from_number, test_to, ref, customer_phone) -> None:
    # ---- 2. real Twilio send by the queue handler --------------------------------------
    view = wait_for(lambda: admin_view(master, ref),
                    lambda v: any(s.get("provider") == "twilio" and s.get("provider_message_id") for s in v.get("sms", [])) or v.get("status") == "failed",
                    timeout=120)
    attempt = next((s for s in view["sms"] if s.get("provider") == "twilio"), None)
    assert attempt and attempt.get("provider_message_id"), f"no Twilio send recorded: {[(s.get('status'), s.get('error')) for s in view['sms']]}"
    message_sid = attempt["provider_message_id"]
    print(f"[3] REAL outbound SMS accepted by Twilio: status={attempt['status']} sid={mask_sid(message_sid)} to={redact_phone(test_to)}")

    # ---- 3. real delivery status from Twilio ------------------------------------------
    provider = TwilioSmsProvider(account_sid=sid, auth_token=token, from_number=from_number)
    final = None
    try:
        for _ in range(30):
            st = provider.fetch_status(message_sid)
            if st["status"] in ("delivered", "failed", "undelivered"):
                final = st
                break
            time.sleep(4)
        source = "Twilio Messages API"
    except requests.HTTPError as exc:  # trial accounts cannot read message resources via the API (403)
        source = f"Twilio StatusCallback -> /api/sms/status (signed by Twilio; API read HTTP {exc.response.status_code} on trial)"
    row = wait_for(lambda: admin_view(master, ref),
                   lambda v: any(s.get("provider_message_id") == message_sid and s.get("status") in ("delivered", "failed", "undelivered") for s in v.get("sms", [])),
                   timeout=120)
    stored = next(s for s in row["sms"] if s.get("provider_message_id") == message_sid)
    status_text = (final or {}).get("status") or stored["status"]
    print(f"[4] REAL Twilio delivery status: {status_text} (source: {source}); stored in SmsMessages: {stored['status']} error={stored.get('error') or '-'}")
    assert status_text == "delivered" and stored["status"] == "delivered", f"outbound not delivered: {status_text}/{stored}"

    # ---- 4. simulated carrier step: build Twilio's inbound webhook exactly ---------------
    inbound_sid = "SM" + uuid.uuid4().hex
    params = {"ToCountry": "US", "ToState": "TX", "SmsMessageSid": inbound_sid, "NumMedia": "0", "ToCity": "",
              "FromZip": "", "SmsSid": inbound_sid, "FromState": "ON", "SmsStatus": "received",
              "FromCity": "TORONTO", "Body": "I can do 5:30 PM", "FromCountry": "CA", "To": from_number,
              "MessagingServiceSid": "", "NumSegments": "1", "MessageSid": inbound_sid, "AccountSid": sid,
              "From": test_to, "ApiVersion": "2010-04-01"}
    body = urlencode(params).encode()
    good_sig = provider.compute_signature(token, WEBHOOK, params)

    def post(sig: str | None):
        headers = {"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "TwilioProxy/1.1"}
        if sig is not None:
            headers["X-Twilio-Signature"] = sig
        return requests.post(WEBHOOK, data=body, headers=headers, timeout=60)

    r_missing, r_bad = post(None), post("bad")
    print(f"[5] webhook without signature -> HTTP {r_missing.status_code}; with invalid signature -> HTTP {r_bad.status_code}")
    assert r_missing.status_code == 403 and r_bad.status_code == 403

    r_ok = post(good_sig)
    print(f"[6] SIMULATED carrier delivery of the reply with a VALID signature -> HTTP {r_ok.status_code} body={r_ok.text.strip()[:30]!r}")
    assert r_ok.status_code == 200

    view = wait_for(lambda: admin_view(master, ref), lambda v: v.get("status") in ("technician_proposed_time", "awaiting_customer_confirmation"), timeout=120)
    print(f"[7] REAL queue + database: status={view['status']} proposed_start={view.get('proposed_start')} technician={view.get('assigned_technician_id')}")

    r_dup = post(good_sig)
    time.sleep(15)
    view = admin_view(master, ref)
    inbound_rows = [s for s in view["sms"] if s.get("direction") == "inbound"]
    print(f"[8] duplicate delivery (same MessageSid) -> HTTP {r_dup.status_code}; inbound rows stored={len(inbound_rows)}; proposals recorded={len([e for e in view['events'] if e['kind'] == 'technician_proposed'])}")
    assert len(inbound_rows) == 1 and r_dup.status_code == 200

    status = mcp.call("get_service_request_status", tracking_reference=ref, callback_phone=customer_phone[-4:])
    print(f"[9] MCP get_service_request_status -> {status['status']} proposed={status['proposed_time']['spoken'] if status.get('proposed_time') else None}")
    assert status["status"] == "awaiting_customer_confirmation"

    # ---- 5. Foundry agent reads it (no confirmation) -------------------------------------
    from azure.ai.projects import AIProjectClient
    from azure.identity import AzureCliCredential
    client = AIProjectClient(endpoint=cfg.PROJECT_ENDPOINT, credential=AzureCliCredential()).get_openai_client()
    today = datetime.now(ZoneInfo("America/Toronto")).date().isoformat()
    resp = client.responses.create(
        input=[{"role": "user", "content": f"Current Toronto date: {today}\n\nWhat's the status of my request {ref}? My phone number is {customer_phone}. Don't book anything yet, just tell me."}],
        extra_body={"agent_reference": {"name": cfg.AGENT_NAME, "type": "agent_reference"}})
    calls = [i.name for i in resp.output if i.type == "mcp_call"]
    text = resp.output_text
    print(f"[10] Foundry agent tools={calls}\n     agent: {text.strip()[:400]}")
    assert "5:30" in text and "confirm_booking" not in calls
    assert admin_view(master, ref)["status"] == "awaiting_customer_confirmation"
    print("\nHYBRID TEST OK  (outbound+delivery: REAL | inbound transport: SIMULATED | webhook/queue/db/state/MCP/agent: REAL)")
    print("REF", ref)


if __name__ == "__main__":
    main()
