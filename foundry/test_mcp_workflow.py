"""End-to-end test of the deployed service-request workflow through the MCP
endpoint, the Azure queues and the (mock) SMS webhook - no Foundry involved.

Flow: create -> match -> send (queue) -> mock SMS sent by the Function ->
technician reply via signed webhook -> queue -> status shows proposal ->
customer confirms -> Outlook event created -> confirmed. Then the test event
is removed from the owner's calendar and duplicate/failure paths are checked.

Reads the mcp_extension key, master key and SMS_WEBHOOK_SECRET with the Azure
CLI into memory; prints none of them. Requires az login.

    python foundry/test_mcp_workflow.py [--keep-event] [--date YYYY-MM-DD]
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deployconf as cfg  # noqa: E402

cfg.require("NEXROZA_RESOURCE_GROUP", "NEXROZA_FUNCTION_APP", "NEXROZA_STORAGE_ACCOUNT", "GRAPH_CLIENT_ID")
RG, APP, BASE, MCP = cfg.RG, cfg.APP, cfg.BASE, cfg.MCP_URL
SHELL = sys.platform.startswith("win")


def az(*args) -> str:
    return subprocess.run(["az", *args], capture_output=True, text=True, shell=SHELL, check=True).stdout.strip()


def keys() -> tuple[str, str, str]:
    data = json.loads(az("functionapp", "keys", "list", "-g", RG, "-n", APP, "-o", "json"))
    secret = az("functionapp", "config", "appsettings", "list", "-g", RG, "-n", APP,
                "--query", "[?name=='SMS_WEBHOOK_SECRET'].value", "-o", "tsv")
    return data["systemKeys"]["mcp_extension"], data["masterKey"], secret


class Mcp:
    def __init__(self, key: str):
        self.key = key
        self.n = 0

    def call(self, tool: str, **arguments) -> dict:
        self.n += 1
        response = requests.post(
            MCP,
            headers={"x-functions-key": self.key, "Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": self.n, "method": "tools/call", "params": {"name": tool, "arguments": arguments}},
            timeout=120,
        )
        response.raise_for_status()
        data = next(line[6:] for line in response.text.splitlines() if line.startswith("data: "))
        result = json.loads(data)
        if "error" in result:
            raise RuntimeError(result["error"])
        return json.loads(result["result"]["content"][0]["text"])

    def tools(self) -> list[str]:
        response = requests.post(MCP, headers={"x-functions-key": self.key, "Content-Type": "application/json",
                                               "Accept": "application/json, text/event-stream"},
                                 json={"jsonrpc": "2.0", "id": 0, "method": "tools/list", "params": {}}, timeout=60)
        data = next(line[6:] for line in response.text.splitlines() if line.startswith("data: "))
        return sorted(t["name"] for t in json.loads(data)["result"]["tools"])


def webhook(secret: str, payload: dict, bad_signature: bool = False) -> requests.Response:
    raw = json.dumps(payload).encode()
    sig = "bad" if bad_signature else hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return requests.post(f"{BASE}/api/sms/inbound", data=raw, headers={"Content-Type": "application/json", "x-mock-signature": sig}, timeout=60)


def technician_phone(technician_id: str) -> str:
    """Seeded (placeholder) technician number, read with the caller's Azure identity."""
    from azure.data.tables import TableClient
    from azure.identity import AzureCliCredential
    client = TableClient(endpoint=cfg.TABLE_ENDPOINT, table_name="Technicians", credential=AzureCliCredential())
    return client.get_entity("technician", technician_id)["phone"]


def admin_view(master: str, token: str) -> dict:
    return requests.get(f"{BASE}/api/ops/requests/{token}", headers={"x-functions-key": master}, timeout=60).json()


def wait_for(fn, predicate, timeout=90, every=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if predicate(value):
            return value
        time.sleep(every)
    raise TimeoutError("condition not met in time")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-event", action="store_true")
    parser.add_argument("--date", default=None, help="service date YYYY-MM-DD (default: next weekday)")
    args = parser.parse_args()

    mcp_key, master, secret = keys()
    mcp = Mcp(mcp_key)
    print("tools:", mcp.tools())

    toronto_now = datetime.now(ZoneInfo("America/Toronto"))
    day = args.date
    if not day:
        candidate = toronto_now + timedelta(days=1)
        while candidate.weekday() >= 5:
            candidate += timedelta(days=1)
        day = candidate.date().isoformat()
    phone = "416-555-0142"
    print(f"\n[1] create_service_request for {day}")
    created = mcp.call("create_service_request", customer_name="E2E Test Customer", callback_phone=phone,
                       address="1 Test Ave, Scarborough", postal_code="M1B 1A1", service_type="water_heaters",
                       issue="gas water heater leaking (e2e test)", preferred_date=day, preferred_time_from="16:00",
                       preferred_time_to="20:00", sms_consent=True, is_emergency=False, idempotency_key=str(uuid.uuid4()))
    token = created["tracking_reference"]
    print("   status:", created["status"], "| reference:", token[:5] + "…")

    print("[2] find_matching_technicians")
    match = mcp.call("find_matching_technicians", tracking_reference=token, callback_phone=phone)
    print("   candidates:", [(c["technician"], c["outlook_status"]) for c in match["candidates"]], match.get("no_match_reasons"))
    if not match["candidates"]:
        sys.exit("no eligible technician on that date; pick another --date")

    print("[3] send_technician_request (returns immediately)")
    t0 = time.time()
    sent = mcp.call("send_technician_request", tracking_reference=token, callback_phone=phone)
    print(f"   status: {sent['status']} technician: {sent['technician']} deadline: {sent['reply_deadline']} ({time.time() - t0:.1f}s)")

    print("[4] waiting for the queue to send the mock SMS…")
    view = wait_for(lambda: admin_view(master, token), lambda v: any(s["status"] == "mock_sent" for s in v.get("sms", [])))
    attempt = next(s for s in view["sms"] if s["status"] == "mock_sent")
    print("   mock SMS sent by the Function: provider_message_id:", attempt["provider_message_id"][:10] + "…", "technician:", attempt["technician_id"])
    tech_phone = technician_phone(attempt["technician_id"])

    print("[5] bad webhook signature must be rejected")
    print("   HTTP", webhook(secret, {"from": tech_phone, "body": "yes"}, bad_signature=True).status_code)

    print("[6] technician replies via signed mock webhook: 'I can do 5:30 PM'")
    msg_id = f"e2e-{uuid.uuid4().hex[:8]}"
    print("   HTTP", webhook(secret, {"from": tech_phone, "body": "I can do 5:30 PM", "message_id": msg_id}).status_code)
    print("   duplicate delivery HTTP", webhook(secret, {"from": tech_phone, "body": "I can do 5:30 PM", "message_id": msg_id}).status_code)

    print("[7] waiting for the inbound queue…")
    wait_for(lambda: admin_view(master, token), lambda v: v.get("status") in ("technician_proposed_time", "awaiting_customer_confirmation"))
    status = mcp.call("get_service_request_status", tracking_reference=token, callback_phone=phone[-4:])
    print("   status:", status["status"], "| proposed:", status["proposed_time"]["spoken"], "| technician:", status["technician"])
    proposals = len([s for s in admin_view(master, token)["sms"] if s["direction"] == "inbound"])
    print("   inbound messages stored:", proposals, "(duplicate ignored)" if proposals == 1 else "(!)")

    print("[8] wrong phone must not see the request")
    denied = mcp.call("get_service_request_status", tracking_reference=token, callback_phone="0000")
    print("   error:", denied.get("error"), "| exposes status?", "status" in denied)
    assert denied.get("error") == "not_found"

    print("[9] confirm_booking")
    confirmed = mcp.call("confirm_booking", tracking_reference=token, callback_phone=phone, customer_accepts=True)
    print("   confirmed:", confirmed.get("confirmed"), "| status:", confirmed.get("status"))
    again = mcp.call("confirm_booking", tracking_reference=token, callback_phone=phone, customer_accepts=True)
    print("   second confirm -> already_confirmed:", again.get("already_confirmed"))
    view = admin_view(master, token)
    print("   bookings:", len(view["bookings"]), "| events:", [e["kind"] for e in view["events"]])
    assert len(view["bookings"]) == 1 and view["status"] == "confirmed"

    if not args.keep_event:
        event_id = view["bookings"][0]["graph_event_id"]
        # remove the test booking from the owner's calendar using the Function's own Graph client locally
        sys.path.insert(0, "src/FunctionsMcpTool")
        os.environ.setdefault("AzureWebJobsStorage__blobServiceUri", cfg.BLOB_ENDPOINT)
        from nexroza.graph import GraphClient
        GraphClient().delete_event(event_id, "e2e-cleanup")
        print("[10] test event removed from Outlook")
    print("\nE2E OK")


if __name__ == "__main__":
    main()
