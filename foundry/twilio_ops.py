"""Twilio operations for the Nexroza SMS integration - no secrets printed.

Credentials are read from the Function App's app settings (TWILIO_ACCOUNT_SID,
TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER) into memory with the Azure CLI; phone
numbers are shown redacted (+1******7277).

    python foundry/twilio_ops.py check                 # account type, sender number, verified caller ids
    python foundry/twilio_ops.py configure-webhook     # point the sender number's inbound SMS + status callbacks at Azure
    python foundry/twilio_ops.py message-status NX-…   # delivery state of the SMS sent for a request
    python foundry/twilio_ops.py inbound NX-…          # inbound replies Twilio received from the test number (redacted)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src", "FunctionsMcpTool"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nexroza.util import redact_phone  # noqa: E402

import deployconf as cfg  # noqa: E402

cfg.require("NEXROZA_RESOURCE_GROUP", "NEXROZA_FUNCTION_APP")
RG, APP, BASE = cfg.RG, cfg.APP, cfg.BASE
API = "https://api.twilio.com/2010-04-01"
SHELL = sys.platform.startswith("win")


def az(*args) -> str:
    return subprocess.run(["az", *args], capture_output=True, text=True, shell=SHELL, check=True).stdout.strip()


def settings() -> dict:
    raw = json.loads(az("functionapp", "config", "appsettings", "list", "-g", RG, "-n", APP, "-o", "json"))
    values = {s["name"]: s["value"] for s in raw}
    missing = [k for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER") if not values.get(k)]
    if missing:
        sys.exit(f"Missing app settings: {missing} - enter them in Azure Portal > Function App > Environment variables.")
    return values


class Twilio:
    def __init__(self, sid: str, token: str, from_number: str):
        self.sid, self.auth, self.from_number = sid, (sid, token), from_number

    def get(self, path: str, **params) -> dict:
        r = requests.get(f"{API}/Accounts/{self.sid}{path}", auth=self.auth, params=params, timeout=30)
        if r.status_code >= 300:
            sys.exit(f"Twilio GET {path} -> HTTP {r.status_code} code={r.json().get('code') if r.content else ''}")
        return r.json()

    def post(self, path: str, data: dict) -> dict:
        r = requests.post(f"{API}/Accounts/{self.sid}{path}", auth=self.auth, data=data, timeout=30)
        if r.status_code >= 300:
            sys.exit(f"Twilio POST {path} -> HTTP {r.status_code} code={r.json().get('code') if r.content else ''}")
        return r.json()


def check(t: Twilio, master: str | None = None) -> None:
    acct = t.get(".json")
    print(f"account: status={acct.get('status')} type={acct.get('type')}  (Trial = free credit, verified recipients only)")
    numbers = t.get("/IncomingPhoneNumbers.json").get("incoming_phone_numbers", [])
    print(f"sender numbers on account: {len(numbers)}")
    for n in numbers:
        caps = n.get("capabilities", {})
        mark = "  <- TWILIO_FROM_NUMBER" if n.get("phone_number") == t.from_number else ""
        print(f"  {redact_phone(n.get('phone_number'))} sms={caps.get('sms')} sms_url={n.get('sms_url') or '(none)'} status_cb={n.get('status_callback') or '(none)'}{mark}")
    if not any(n.get("phone_number") == t.from_number for n in numbers):
        print(f"  !! TWILIO_FROM_NUMBER {redact_phone(t.from_number)} is not a number on this account")
    verified = t.get("/OutgoingCallerIds.json").get("outgoing_caller_ids", [])
    print("verified caller ids (trial accounts can only text these):", [redact_phone(v.get("phone_number")) for v in verified] or "(none)")
    balance = requests.get(f"{API}/Accounts/{t.sid}/Balance.json", auth=t.auth, timeout=30)
    if balance.ok:
        b = balance.json()
        print(f"balance: {b.get('balance')} {b.get('currency')}")


def configure_webhook(t: Twilio) -> None:
    numbers = t.get("/IncomingPhoneNumbers.json", PhoneNumber=t.from_number).get("incoming_phone_numbers", [])
    if not numbers:
        sys.exit("sender number not found on the account")
    updated = t.post(f"/IncomingPhoneNumbers/{numbers[0]['sid']}.json", {
        "SmsUrl": f"{BASE}/api/sms/inbound", "SmsMethod": "POST",
        "StatusCallback": f"{BASE}/api/sms/status", "StatusCallbackMethod": "POST",
    })
    print("inbound SMS webhook:", updated.get("sms_url"), updated.get("sms_method"))
    print("status callback   :", updated.get("status_callback"))


def ops_view(master: str, token: str) -> dict:
    return requests.get(f"{BASE}/api/ops/requests/{token}", headers={"x-functions-key": master}, timeout=60).json()


def message_status(t: Twilio, token: str) -> None:
    master = json.loads(az("functionapp", "keys", "list", "-g", RG, "-n", APP, "-o", "json"))["masterKey"]
    view = ops_view(master, token)
    sids = [s for s in view.get("sms", []) if s.get("provider") == "twilio" and s.get("provider_message_id")]
    if not sids:
        sys.exit("no Twilio message recorded for this request yet")
    for s in sids:
        msg = t.get(f"/Messages/{s['provider_message_id']}.json")
        print(f"attempt {s['RowKey'][-6:]}: stored_status={s.get('status')} twilio_status={msg.get('status')} "
              f"to={redact_phone(msg.get('to'))} segments={msg.get('num_segments')} error={msg.get('error_code') or '-'} price={msg.get('price') or '-'} {msg.get('price_unit') or ''}")


def inbound(t: Twilio, token: str) -> None:
    msgs = t.get("/Messages.json", To=t.from_number, PageSize=10).get("messages", [])
    print(f"last inbound messages to {redact_phone(t.from_number)}: {len(msgs)}")
    for m in msgs:
        print(f"  {m.get('date_sent')} from={redact_phone(m.get('from'))} status={m.get('status')} chars={len(m.get('body') or '')}")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    s = settings()
    t = Twilio(s["TWILIO_ACCOUNT_SID"], s["TWILIO_AUTH_TOKEN"], s["TWILIO_FROM_NUMBER"])
    if cmd == "check":
        check(t)
    elif cmd == "configure-webhook":
        configure_webhook(t)
    elif cmd == "message-status":
        message_status(t, sys.argv[2])
    elif cmd == "inbound":
        inbound(t, sys.argv[2] if len(sys.argv) > 2 else "")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
