"""SMS providers behind one interface.

* ``MockSmsProvider`` - default. Records the message, returns a fake provider
  id, and accepts inbound webhooks signed with ``SMS_WEBHOOK_SECRET`` (HMAC).
* ``TwilioSmsProvider`` - REST send + ``X-Twilio-Signature`` validation.
* ``AcsSmsProvider`` - Azure Communication Services send + Event Grid
  ``SMSReceived`` events (validation handshake + shared secret query key).

Selection: ``SMS_PROVIDER`` = ``mock`` (default) | ``twilio`` | ``acs``.
No provider credential is ever logged. Phone numbers are redacted in logs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass
from typing import Protocol

from .util import iso_utc, mask_id, new_id, normalize_phone, redact_phone

logger = logging.getLogger("nexroza.sms")


@dataclass
class SmsSendResult:
    ok: bool
    provider: str
    provider_message_id: str | None
    status: str          # queued | sent | failed | mock_sent
    error: str | None = None


@dataclass
class InboundSms:
    provider: str
    provider_message_id: str
    from_phone: str
    to_phone: str | None
    body: str
    received_at: str


class SmsProvider(Protocol):
    name: str

    def send(self, to_phone: str, body: str, reference: str) -> SmsSendResult: ...
    def parse_inbound(self, headers: dict, raw_body: bytes, url: str, query: dict) -> list[InboundSms]: ...


class WebhookSignatureError(Exception):
    pass


def public_url(path: str, request_url: str | None = None) -> str:
    """Canonical public URL of a webhook route as the provider sees it.

    Behind the Functions host the request URL may differ (scheme/port), so
    signature checks are done against PUBLIC_BASE_URL (or WEBSITE_HOSTNAME).
    """
    base = os.environ.get("PUBLIC_BASE_URL") or (
        f"https://{os.environ['WEBSITE_HOSTNAME']}" if os.environ.get("WEBSITE_HOSTNAME") else None)
    if base:
        return base.rstrip("/") + path
    return request_url or path


def _url_variants(url: str) -> list[str]:
    """Twilio may sign with or without the default port; accept both."""
    variants = {url}
    if url.startswith("https://"):
        host_and_rest = url[len("https://"):]
        host, _, rest = host_and_rest.partition("/")
        if ":" not in host:
            variants.add(f"https://{host}:443/{rest}")
        elif host.endswith(":443"):
            variants.add(f"https://{host[:-4]}/{rest}")
    return sorted(variants)


# --------------------------------------------------------------------------- #
# Mock provider
# --------------------------------------------------------------------------- #

class MockSmsProvider:
    name = "mock"

    def __init__(self, webhook_secret: str | None = None, fail_numbers: set[str] | None = None):
        self.webhook_secret = webhook_secret or os.environ.get("SMS_WEBHOOK_SECRET", "")
        self.fail_numbers = fail_numbers or set(
            n for n in os.environ.get("SMS_MOCK_FAIL_NUMBERS", "").split(",") if n
        )
        self.sent: list[dict] = []

    def send(self, to_phone, body, reference):
        if to_phone in self.fail_numbers:
            logger.warning("SMS_SEND_FAILED provider=mock to=%s reference=%s reason=simulated",
                           redact_phone(to_phone), reference)
            return SmsSendResult(False, self.name, None, "failed", "simulated_failure")
        message_id = new_id("mock")
        self.sent.append({"to": to_phone, "body": body, "reference": reference, "id": message_id})
        logger.info("SMS_SENT provider=mock to=%s reference=%s provider_message_id=%s chars=%d",
                    redact_phone(to_phone), reference, mask_id(message_id), len(body))
        return SmsSendResult(True, self.name, message_id, "mock_sent")

    @staticmethod
    def sign(secret: str, raw_body: bytes) -> str:
        return hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()

    def parse_inbound(self, headers, raw_body, url, query):
        signature = headers.get("x-mock-signature") or headers.get("X-Mock-Signature") or ""
        if not self.webhook_secret or not hmac.compare_digest(signature, self.sign(self.webhook_secret, raw_body)):
            raise WebhookSignatureError("mock signature invalid")
        payload = json.loads(raw_body.decode("utf-8"))
        from_phone = normalize_phone(payload.get("from"))
        if not from_phone:
            raise ValueError("from is required")
        return [InboundSms(
            provider=self.name,
            provider_message_id=str(payload.get("message_id") or new_id("mockin")),
            from_phone=from_phone,
            to_phone=normalize_phone(payload.get("to")),
            body=str(payload.get("body") or ""),
            received_at=iso_utc(),
        )]


# --------------------------------------------------------------------------- #
# Twilio
# --------------------------------------------------------------------------- #

class TwilioSmsProvider:
    name = "twilio"

    def __init__(self, account_sid=None, auth_token=None, from_number=None, session=None):
        import requests
        self.account_sid = account_sid or os.environ.get("TWILIO_ACCOUNT_SID", "")
        self.auth_token = auth_token or os.environ.get("TWILIO_AUTH_TOKEN", "")
        self.from_number = from_number or os.environ.get("TWILIO_FROM_NUMBER", "")
        self.session = session or requests.Session()
        if not (self.account_sid and self.auth_token and self.from_number):
            raise RuntimeError("Twilio settings TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN/TWILIO_FROM_NUMBER are missing.")

    def send(self, to_phone, body, reference):
        data = {"To": to_phone, "From": self.from_number, "Body": body}
        trial_template = os.environ.get("TWILIO_TRIAL_TEMPLATE", "").strip()
        if trial_template:
            # Twilio *trial* accounts reject custom bodies (error 572006): only a
            # predefined template name may be sent, from the console's trial number.
            # Opt-in for connectivity tests only; never set in production.
            logger.warning("SMS_TRIAL_TEMPLATE_MODE template=%s reference=%s (dispatch text NOT sent)", trial_template, reference)
            data = {"To": to_phone, "From": self.from_number, "Body": trial_template}
        status_url = public_url("/api/sms/status")
        if status_url.startswith("https://"):
            data["StatusCallback"] = status_url  # delivery receipts -> sms_status route (signed)
        response = self.session.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json",
            data=data,
            auth=(self.account_sid, self.auth_token),
            timeout=30,
        )
        if response.status_code >= 300:
            code = None
            try:
                code = response.json().get("code")
            except ValueError:
                pass
            logger.error("SMS_SEND_FAILED provider=twilio to=%s reference=%s http=%s code=%s",
                         redact_phone(to_phone), reference, response.status_code, code)
            return SmsSendResult(False, self.name, None, "failed", f"twilio_{response.status_code}_{code}")
        data = response.json()
        logger.info("SMS_SENT provider=twilio to=%s reference=%s provider_message_id=%s status=%s",
                    redact_phone(to_phone), reference, mask_id(data.get("sid")), data.get("status"))
        return SmsSendResult(True, self.name, data.get("sid"), data.get("status", "queued"))

    def _verified_params(self, headers, raw_body, url) -> dict:
        from urllib.parse import parse_qs
        signature = headers.get("x-twilio-signature") or headers.get("X-Twilio-Signature") or ""
        params = {k: v[0] for k, v in parse_qs(raw_body.decode("utf-8"), keep_blank_values=True).items()}
        candidates = _url_variants(url)
        if url.endswith("/api/sms/inbound") or url.endswith("/api/sms/status"):
            path = "/api/sms/" + url.rsplit("/", 1)[1]
            candidates = sorted(set(candidates) | set(_url_variants(public_url(path, url))))
        for candidate in candidates:
            if hmac.compare_digest(signature, self.compute_signature(self.auth_token, candidate, params)):
                return params
        raise WebhookSignatureError("twilio signature invalid")

    def parse_status(self, headers, raw_body, url) -> tuple[str, str, str | None]:
        """Delivery receipt -> (message sid, status, error code)."""
        params = self._verified_params(headers, raw_body, url)
        return params.get("MessageSid") or params.get("SmsSid") or "", params.get("MessageStatus", ""), params.get("ErrorCode")

    def fetch_status(self, message_sid: str) -> dict:
        """Poll Twilio for a message's current status (no body/number printed)."""
        response = self.session.get(
            f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages/{message_sid}.json",
            auth=(self.account_sid, self.auth_token), timeout=30)
        response.raise_for_status()
        data = response.json()
        return {"status": data.get("status"), "error_code": data.get("error_code"), "error_message": data.get("error_message"),
                "date_sent": data.get("date_sent"), "num_segments": data.get("num_segments"), "price": data.get("price")}

    def parse_inbound(self, headers, raw_body, url, query):
        params = self._verified_params(headers, raw_body, url)
        from_phone = normalize_phone(params.get("From"))
        if not from_phone:
            raise ValueError("From is required")
        return [InboundSms(self.name, params.get("MessageSid") or new_id("twin"), from_phone,
                           normalize_phone(params.get("To")), params.get("Body", ""), iso_utc())]

    @staticmethod
    def compute_signature(auth_token: str, url: str, params: dict) -> str:
        data = url + "".join(k + params[k] for k in sorted(params))
        digest = hmac.new(auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1).digest()
        return base64.b64encode(digest).decode("ascii")


# --------------------------------------------------------------------------- #
# Azure Communication Services
# --------------------------------------------------------------------------- #

class AcsSmsProvider:
    name = "acs"

    def __init__(self, endpoint=None, from_number=None, webhook_key=None):
        self.endpoint = endpoint or os.environ.get("ACS_ENDPOINT", "")
        self.from_number = from_number or os.environ.get("ACS_FROM_NUMBER", "")
        self.webhook_key = webhook_key or os.environ.get("SMS_WEBHOOK_SECRET", "")
        if not (self.endpoint and self.from_number):
            raise RuntimeError("ACS settings ACS_ENDPOINT/ACS_FROM_NUMBER are missing.")

    def _client(self):
        from azure.communication.sms import SmsClient  # optional dependency
        from azure.identity import DefaultAzureCredential
        return SmsClient(self.endpoint, DefaultAzureCredential(
            managed_identity_client_id=os.environ.get("AzureWebJobsStorage__clientId")))

    def send(self, to_phone, body, reference):
        try:
            result = self._client().send(from_=self.from_number, to=[to_phone], message=body, enable_delivery_report=True, tag=reference)[0]
        except Exception as exc:  # noqa: BLE001
            logger.error("SMS_SEND_FAILED provider=acs to=%s reference=%s error=%s", redact_phone(to_phone), reference, type(exc).__name__)
            return SmsSendResult(False, self.name, None, "failed", type(exc).__name__)
        if not result.successful:
            logger.error("SMS_SEND_FAILED provider=acs to=%s reference=%s http=%s", redact_phone(to_phone), reference, result.http_status_code)
            return SmsSendResult(False, self.name, result.message_id, "failed", f"acs_{result.http_status_code}")
        logger.info("SMS_SENT provider=acs to=%s reference=%s provider_message_id=%s", redact_phone(to_phone), reference, mask_id(result.message_id))
        return SmsSendResult(True, self.name, result.message_id, "sent")

    def parse_inbound(self, headers, raw_body, url, query):
        # Event Grid webhook secured with a shared key in the subscription URL (?key=...)
        if not self.webhook_key or not hmac.compare_digest(str(query.get("key", "")), self.webhook_key):
            raise WebhookSignatureError("acs webhook key invalid")
        events = json.loads(raw_body.decode("utf-8"))
        out = []
        for event in events if isinstance(events, list) else [events]:
            if event.get("eventType") != "Microsoft.Communication.SMSReceived":
                continue
            data = event.get("data", {})
            from_phone = normalize_phone(data.get("from"))
            if not from_phone:
                continue
            out.append(InboundSms(self.name, data.get("messageId") or event.get("id"), from_phone,
                                  normalize_phone(data.get("to")), data.get("message", ""), iso_utc()))
        return out


def get_provider() -> SmsProvider:
    name = os.environ.get("SMS_PROVIDER", "mock").lower()
    if name == "twilio":
        return TwilioSmsProvider()
    if name == "acs":
        return AcsSmsProvider()
    return MockSmsProvider()
