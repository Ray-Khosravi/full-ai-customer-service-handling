"""Microsoft Graph access with the owner's delegated token (silent refresh only).

The MSAL token cache lives in blob ``mcp-auth/msal-token-cache.json`` and is
written once by ``tools/authorize_outlook.py``; this module only refreshes it.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import msal
import requests
from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient, ContainerClient

from .util import TORONTO_TZ, TORONTO_TZ_NAME, local_day_window

logger = logging.getLogger("nexroza.graph")

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"
SCOPES = ["User.Read", "Calendars.ReadWrite"]
AUTH_CONTAINER = os.environ.get("AUTH_CONTAINER", "mcp-auth")
TOKEN_CACHE_BLOB = os.environ.get("TOKEN_CACHE_BLOB", "msal-token-cache.json")

STATUS_PATTERN = re.compile(r"^\s*(OFF|VACATION|SICK|ON[ _-]?CALL)\b", re.IGNORECASE)
UNAVAILABLE_STATUSES = ("OFF", "VACATION", "SICK")
NEXROZA_EVENT_MARKER = "[NX]"  # subject prefix for events this system creates


class OutlookAuthorizationError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class GraphError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(f"Microsoft Graph {status_code} {code}: {message}")
        self.status_code = status_code
        self.code = code


# --------------------------------------------------------------------------- #
# Token cache in blob storage
# --------------------------------------------------------------------------- #

_container: ContainerClient | None = None


def _blob_service_client() -> BlobServiceClient:
    blob_uri = os.environ.get("AzureWebJobsStorage__blobServiceUri")
    if blob_uri:
        credential = DefaultAzureCredential(
            managed_identity_client_id=os.environ.get("AzureWebJobsStorage__clientId"),
            exclude_interactive_browser_credential=True,
        )
        return BlobServiceClient(blob_uri, credential=credential)
    connection_string = os.environ.get("AzureWebJobsStorage")
    if not connection_string:
        raise RuntimeError("Azure Storage configuration is missing.")
    return BlobServiceClient.from_connection_string(connection_string)


def _container_client() -> ContainerClient:
    global _container
    if _container is None:
        container = _blob_service_client().get_container_client(AUTH_CONTAINER)
        try:
            container.create_container()
        except ResourceExistsError:
            pass
        _container = container
    return _container


def read_blob(name: str) -> str | None:
    try:
        return _container_client().get_blob_client(name).download_blob().readall().decode("utf-8")
    except ResourceNotFoundError:
        return None
    except HttpResponseError as exc:
        logger.error("Blob read failed for %s: status=%s code=%s", name, exc.status_code, exc.error_code)
        raise


def write_blob(name: str, value: str) -> None:
    try:
        _container_client().get_blob_client(name).upload_blob(value, overwrite=True)
    except HttpResponseError as exc:
        logger.error("Blob write failed for %s: status=%s code=%s", name, exc.status_code, exc.error_code)
        raise


class GraphClient:
    """Thin Graph wrapper; ``session`` is injectable for tests."""

    def __init__(self, client_id: str | None = None, authority: str | None = None, session=None):
        self.client_id = client_id or os.environ["GRAPH_CLIENT_ID"]
        self.authority = authority or os.environ.get("GRAPH_AUTHORITY", "https://login.microsoftonline.com/common")
        self.session = session or requests.Session()

    # -- tokens ---------------------------------------------------------------
    def access_token(self) -> str:
        cache = msal.SerializableTokenCache()
        serialized = read_blob(TOKEN_CACHE_BLOB)
        if serialized:
            cache.deserialize(serialized)
        client = msal.PublicClientApplication(self.client_id, authority=self.authority, token_cache=cache)
        accounts = client.get_accounts()
        if not accounts:
            raise OutlookAuthorizationError(
                "not_authorized",
                "Outlook is not connected. The business owner must run tools/authorize_outlook.py once.",
            )
        result = client.acquire_token_silent(SCOPES, account=accounts[0])
        if cache.has_state_changed:
            write_blob(TOKEN_CACHE_BLOB, cache.serialize())
            logger.info("Token cache refreshed and persisted")
        if not result or "access_token" not in result:
            error = (result or {}).get("error", "silent_auth_failed")
            logger.error("Silent token acquisition failed: error=%s suberror=%s msal_correlation_id=%s",
                         error, (result or {}).get("suberror"), (result or {}).get("correlation_id"))
            raise OutlookAuthorizationError(
                "reauthorization_required",
                f"Outlook authorization must be renewed by the business owner ({error}).",
            )
        return result["access_token"]

    # -- requests -------------------------------------------------------------
    def _request(self, method: str, path: str, correlation_id: str, params=None, json_body=None) -> dict:
        response = self.session.request(
            method,
            f"{GRAPH_BASE_URL}{path}",
            headers={
                "Authorization": f"Bearer {self.access_token()}",
                "Accept": "application/json",
                "client-request-id": correlation_id,
                "Prefer": f'outlook.timezone="{TORONTO_TZ_NAME}"',
            },
            params=params,
            json=json_body,
            timeout=30,
        )
        if response.ok:
            return response.json() if response.content else {}
        try:
            error = response.json().get("error", {})
            code = str(error.get("code", "unknown"))
            message = str(error.get("message", ""))[:300]
        except ValueError:
            code, message = "unknown", response.text[:300]
        logger.error("Graph request failed: method=%s path=%s status=%s code=%s request-id=%s",
                     method, path, response.status_code, code, response.headers.get("request-id"))
        if response.status_code == 401:
            raise OutlookAuthorizationError("token_rejected", "Microsoft Graph rejected the stored authorization.")
        raise GraphError(response.status_code, code, message)

    def get(self, path: str, correlation_id: str, params: dict | None = None) -> dict:
        return self._request("GET", path, correlation_id, params=params)

    def post(self, path: str, correlation_id: str, json_body: dict) -> dict:
        return self._request("POST", path, correlation_id, json_body=json_body)

    def delete_event(self, event_id: str, correlation_id: str) -> None:
        self._request("DELETE", f"/me/events/{quote(event_id, safe='')}", correlation_id)

    # -- calendars --------------------------------------------------------------
    def calendar_map(self, correlation_id: str) -> dict[str, str]:
        data = self.get("/me/calendars", correlation_id, {"$select": "id,name", "$top": "100"})
        return {item["name"]: item["id"] for item in data.get("value", [])}

    def calendar_view(self, calendar_id: str, start: datetime, end: datetime, correlation_id: str) -> list[dict]:
        data = self.get(
            f"/me/calendars/{quote(calendar_id, safe='')}/calendarView",
            correlation_id,
            {
                "startDateTime": start.isoformat(),
                "endDateTime": end.isoformat(),
                "$select": "id,subject,start,end,isAllDay,showAs",
                "$top": "100",
            },
        )
        return [e for e in data.get("value", []) if overlaps(e, start, end)]

    def create_event(self, calendar_id: str, subject: str, body_text: str, start: datetime, end: datetime,
                     correlation_id: str, location: str | None = None) -> dict:
        payload = {
            "subject": subject,
            "body": {"contentType": "text", "content": body_text},
            "start": {"dateTime": start.astimezone(TORONTO_TZ).replace(tzinfo=None).isoformat(), "timeZone": TORONTO_TZ_NAME},
            "end": {"dateTime": end.astimezone(TORONTO_TZ).replace(tzinfo=None).isoformat(), "timeZone": TORONTO_TZ_NAME},
            "showAs": "busy",
            "isReminderOn": True,
            "reminderMinutesBeforeStart": 60,
        }
        if location:
            payload["location"] = {"displayName": location}
        return self.post(f"/me/calendars/{quote(calendar_id, safe='')}/events", correlation_id, payload)


# --------------------------------------------------------------------------- #
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------- #

def parse_graph_datetime(value: dict | None) -> datetime | None:
    if not value or not value.get("dateTime"):
        return None
    text = re.sub(r"(\.\d{6})\d+", r"\1", value["dateTime"])
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed
    try:
        tz = ZoneInfo(value.get("timeZone") or TORONTO_TZ_NAME)
    except (KeyError, ValueError):
        tz = TORONTO_TZ
    return parsed.replace(tzinfo=tz)


def overlaps(event: dict, start: datetime, end: datetime) -> bool:
    ev_start = parse_graph_datetime(event.get("start"))
    ev_end = parse_graph_datetime(event.get("end"))
    if ev_start is None or ev_end is None:
        return True
    return ev_start < end and ev_end > start


def status_from_events(events: list[dict]) -> tuple[str, list[str]]:
    """(WORKING|OFF|SICK|VACATION|ON_CALL, matching status subjects)."""
    statuses: list[str] = []
    subjects: list[str] = []
    for event in events:
        subject = str(event.get("subject") or "").strip()
        match = STATUS_PATTERN.match(subject)
        if not match:
            continue
        keyword = match.group(1).upper()
        statuses.append("ON_CALL" if keyword.startswith("ON") else keyword)
        subjects.append(subject)
    for unavailable in UNAVAILABLE_STATUSES:
        if unavailable in statuses:
            return unavailable, subjects
    if "ON_CALL" in statuses:
        return "ON_CALL", subjects
    return "WORKING", subjects


def busy_in_window(events: list[dict], start: datetime, end: datetime) -> bool:
    """True when a timed (non status, non all-day) event overlaps the window."""
    for event in events:
        if event.get("isAllDay"):
            continue
        if STATUS_PATTERN.match(str(event.get("subject") or "")):
            continue
        if str(event.get("showAs", "busy")).lower() == "free":
            continue
        if overlaps(event, start, end):
            return True
    return False


def day_status(graph: GraphClient, calendar_id: str, day, correlation_id: str) -> tuple[str, list[str], list[dict]]:
    start, end = local_day_window(day)
    events = graph.calendar_view(calendar_id, start, end, correlation_id)
    status, subjects = status_from_events(events)
    return status, subjects, events
