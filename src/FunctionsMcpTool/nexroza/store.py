"""Persistence: Azure Table Storage (production) and an in-memory twin (tests).

Tables (all in the Function App's storage account, managed identity only):

| table            | PartitionKey  | RowKey            | holds                                  |
|------------------|---------------|-------------------|----------------------------------------|
| Technicians      | "technician"  | technician_id     | name, skills, service areas, schedule  |
| Skills           | "skill"       | skill_code        | display name, keywords                 |
| ServiceRequests  | "request"     | request_id        | customer details, status, assignment   |
| RequestEvents    | request_id    | ts + seq          | status history / audit                 |
| SmsMessages      | request_id    | message_id        | outbound attempts and inbound replies  |
| Proposals        | request_id    | proposal_id       | technician time proposals              |
| Bookings         | request_id    | booking_id        | Graph event ids                        |
| Idempotency      | "idem"        | sha256(key)       | dedupe for webhooks / tool retries     |

Every timestamp is UTC ISO-8601. Complex fields are JSON strings.
Optimistic concurrency uses the entity ETag (``update(..., etag=...)``).
"""

from __future__ import annotations

import copy
import json
import os
import threading
from typing import Any, Iterable, Protocol

from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError
from azure.core import MatchConditions

from .util import iso_utc, new_id

TABLES = ["Technicians", "Skills", "ServiceRequests", "RequestEvents", "SmsMessages",
          "Proposals", "Bookings", "Idempotency"]

JSON_FIELDS = {"skills", "service_areas", "weekly_schedule", "candidates", "keywords", "payload"}


class ConcurrencyConflict(Exception):
    """Another writer changed the entity since it was read."""


class Store(Protocol):
    def get(self, table: str, pk: str, rk: str) -> dict | None: ...
    def insert(self, table: str, entity: dict) -> dict: ...
    def upsert(self, table: str, entity: dict) -> dict: ...
    def update(self, table: str, entity: dict, etag: str | None = None) -> dict: ...
    def query(self, table: str, filter_: str, limit: int | None = None) -> list[dict]: ...
    def delete(self, table: str, pk: str, rk: str) -> None: ...


def _encode(entity: dict) -> dict:
    out = {}
    for key, value in entity.items():
        if key in ("etag", "_etag"):
            continue
        if key in JSON_FIELDS or isinstance(value, (list, dict)):
            out[key] = json.dumps(value)
        elif value is None:
            continue
        else:
            out[key] = value
    return out


def _decode(entity: dict) -> dict:
    out = {}
    for key, value in entity.items():
        if key in JSON_FIELDS and isinstance(value, str):
            try:
                out[key] = json.loads(value)
            except ValueError:
                out[key] = value
        elif key == "Timestamp":
            continue
        else:
            out[key] = value
    meta = getattr(entity, "metadata", None) or {}
    if meta.get("etag"):
        out["etag"] = meta["etag"]
    return out


# --------------------------------------------------------------------------- #
# Azure Table Storage
# --------------------------------------------------------------------------- #

class TableStore:
    def __init__(self, service_client=None):
        if service_client is None:
            from azure.data.tables import TableServiceClient
            table_uri = os.environ.get("AzureWebJobsStorage__tableServiceUri")
            if table_uri:
                from azure.identity import DefaultAzureCredential
                credential = DefaultAzureCredential(
                    managed_identity_client_id=os.environ.get("AzureWebJobsStorage__clientId"),
                    exclude_interactive_browser_credential=True,
                )
                service_client = TableServiceClient(endpoint=table_uri, credential=credential)
            else:
                conn = os.environ.get("AzureWebJobsStorage")
                if not conn:
                    raise RuntimeError("Table storage configuration is missing.")
                service_client = TableServiceClient.from_connection_string(conn)
        self._service = service_client
        self._clients: dict[str, Any] = {}
        self._lock = threading.Lock()

    def _table(self, name: str):
        with self._lock:
            client = self._clients.get(name)
            if client is None:
                client = self._service.get_table_client(name)
                try:
                    client.create_table()
                except ResourceExistsError:
                    pass
                self._clients[name] = client
            return client

    def get(self, table, pk, rk):
        try:
            return _decode(self._table(table).get_entity(pk, rk))
        except ResourceNotFoundError:
            return None

    def insert(self, table, entity):
        try:
            self._table(table).create_entity(_encode(entity))
        except ResourceExistsError as exc:
            raise ConcurrencyConflict(f"{table}/{entity.get('RowKey')} already exists") from exc
        return self.get(table, entity["PartitionKey"], entity["RowKey"])

    def upsert(self, table, entity):
        self._table(table).upsert_entity(_encode(entity))
        return self.get(table, entity["PartitionKey"], entity["RowKey"])

    def update(self, table, entity, etag=None):
        from azure.data.tables import UpdateMode
        kwargs = {}
        if etag:
            kwargs = {"etag": etag, "match_condition": MatchConditions.IfNotModified}
        try:
            self._table(table).update_entity(_encode(entity), mode=UpdateMode.REPLACE, **kwargs)
        except HttpResponseError as exc:
            if exc.status_code == 412:
                raise ConcurrencyConflict(f"{table}/{entity.get('RowKey')} was modified concurrently") from exc
            raise
        return self.get(table, entity["PartitionKey"], entity["RowKey"])

    def query(self, table, filter_, limit=None):
        rows = self._table(table).query_entities(filter_, results_per_page=limit)
        out = []
        for row in rows:
            out.append(_decode(row))
            if limit and len(out) >= limit:
                break
        return out

    def delete(self, table, pk, rk):
        self._table(table).delete_entity(pk, rk)


# --------------------------------------------------------------------------- #
# In-memory twin for unit tests (supports the small OData subset we use)
# --------------------------------------------------------------------------- #

class MemoryStore:
    def __init__(self):
        self._data: dict[str, dict[tuple[str, str], dict]] = {t: {} for t in TABLES}
        self._etag = 0
        self._lock = threading.Lock()

    def _next_etag(self) -> str:
        self._etag += 1
        return f'W/"{self._etag}"'

    def get(self, table, pk, rk):
        row = self._data[table].get((pk, rk))
        return copy.deepcopy(row) if row else None

    def insert(self, table, entity):
        with self._lock:
            key = (entity["PartitionKey"], entity["RowKey"])
            if key in self._data[table]:
                raise ConcurrencyConflict(f"{table}/{key[1]} already exists")
            row = copy.deepcopy(entity)
            row["etag"] = self._next_etag()
            self._data[table][key] = row
            return copy.deepcopy(row)

    def upsert(self, table, entity):
        with self._lock:
            key = (entity["PartitionKey"], entity["RowKey"])
            row = copy.deepcopy(entity)
            row["etag"] = self._next_etag()
            self._data[table][key] = row
            return copy.deepcopy(row)

    def update(self, table, entity, etag=None):
        with self._lock:
            key = (entity["PartitionKey"], entity["RowKey"])
            current = self._data[table].get(key)
            if current is None:
                raise ResourceNotFoundError(f"{table}/{key[1]} not found")
            if etag and current.get("etag") != etag:
                raise ConcurrencyConflict(f"{table}/{key[1]} was modified concurrently")
            row = copy.deepcopy(entity)
            row["etag"] = self._next_etag()
            self._data[table][key] = row
            return copy.deepcopy(row)

    def query(self, table, filter_, limit=None):
        rows = [copy.deepcopy(r) for r in self._data[table].values() if _match(r, filter_)]
        rows.sort(key=lambda r: (r["PartitionKey"], r["RowKey"]))
        return rows[:limit] if limit else rows

    def delete(self, table, pk, rk):
        self._data[table].pop((pk, rk), None)


def _match(row: dict, filter_: str) -> bool:
    """Evaluate "a eq 'x' and b eq 'y' and c ge 'z'" style filters."""
    if not filter_:
        return True
    for clause in filter_.split(" and "):
        parts = clause.strip().split(" ", 2)
        if len(parts) != 3:
            return False
        field, op, raw = parts
        value = raw.strip("'") if raw.startswith("'") else raw
        actual = row.get(field)
        if actual is None:
            return False
        if isinstance(actual, bool):
            value = value.lower() == "true"
        if op == "eq" and not actual == value:
            return False
        if op == "ne" and not actual != value:
            return False
        if op == "ge" and not str(actual) >= str(value):
            return False
        if op == "gt" and not str(actual) > str(value):
            return False
        if op == "le" and not str(actual) <= str(value):
            return False
        if op == "lt" and not str(actual) < str(value):
            return False
    return True


# --------------------------------------------------------------------------- #
# Convenience: audit events and idempotency
# --------------------------------------------------------------------------- #

_seq_lock = threading.Lock()
_seq = 0


def record_event(store: Store, request_id: str, kind: str, from_status: str | None,
                 to_status: str | None, detail: dict | None = None, actor: str = "system") -> dict:
    global _seq
    with _seq_lock:
        _seq = (_seq + 1) % 10000
        seq = _seq
    entity = {
        "PartitionKey": request_id,
        "RowKey": f"{iso_utc()}-{seq:04d}-{new_id('ev')[-6:]}",
        "kind": kind,
        "from_status": from_status or "",
        "to_status": to_status or "",
        "actor": actor,
        "detail": json.dumps(detail or {}),
        "created_at": iso_utc(),
    }
    return store.insert("RequestEvents", entity)


def claim_idempotency(store: Store, key: str, scope: str) -> bool:
    """True the first time a key is seen; False on any repeat."""
    from .util import sha256
    entity = {"PartitionKey": "idem", "RowKey": sha256(f"{scope}:{key}"), "scope": scope, "created_at": iso_utc()}
    try:
        store.insert("Idempotency", entity)
        return True
    except ConcurrencyConflict:
        return False


def list_all(rows: Iterable[dict]) -> list[dict]:
    return list(rows)
