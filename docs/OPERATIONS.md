# Operations guide

Detailed runbook for the Nexroza AI dispatch platform. Placeholders like `<function-app>` are your own resource names; put them in `deploy.env` (git-ignored) so the scripts in `foundry/` and `tools/` pick them up.


A public website chatbot (Microsoft Foundry agent) runs a safety check,
collects the customer's details, creates a **service request**, matches a
qualified technician against the database and the owner's **live Outlook
calendars**, asks the technician by **SMS**, and — after the technician
proposes a time and the customer accepts — books the job in Outlook. The
whole technician round-trip is **asynchronous** (queues + webhook); no
conversation or HTTP request ever waits for a reply. The owner authorizes
Outlook once; customers never sign in to Microsoft.

Built on the [Azure-Samples remote-mcp-functions-python](https://github.com/Azure-Samples/remote-mcp-functions-python)
template (its other sample projects remain under `src/` untouched).

## Architecture

```
 website          website backend         Microsoft Foundry                Azure Function App <function-app> (Flex, py3.13)
┌────────┐ HTTPS ┌──────────────┐ Entra  ┌───────────────────────┐ MCP+key ┌──────────────────────────────────────────────┐
│ widget │──────►│ /api/chat    │───────►│ agent Plumbing-Service │────────►│ 8 MCP tools ─┐                               │
│(no login)◄─────│ (session,    │◄───────│ -Coordinator (gpt-5-   │◄────────│              ▼                               │
└────────┘       │ rate limit)  │        │ mini) + MCP tool       │         │  nexroza.workflow ── Table Storage (8 tables)│
                 └──────────────┘        └───────────────────────┘         │       │  ▲                                   │
                                                                            │       ▼  │ queue sms-outbound ─► SMS provider │
   technician phone ◄──── SMS (mock | Twilio | ACS) ◄───────────────────────│  process_sms_outbound      (mock today)      │
   technician reply ────► POST /api/sms/inbound (signature) ─► queue sms-inbound ─► process_sms_inbound ─► proposal stored│
                                                                            │  timers: expire_requests (5 min), heartbeat  │
                                                                            │  Graph (owner token, blob cache) ◄───────────┤
                                                                            └───────────────┬──────────────────────────────┘
                                                                                            ▼  calendarView / create event
                                                                                    owner's Outlook calendars
```

| Component | Value |
|-----------|-------|
| Subscription / RG | `<subscription>` / `rg-nexroza-calendar-mcp` (Canada Central) — real names live in the git-ignored `deploy.env` (see `deploy.env.example`) |
| Function App | `<function-app>` — MCP endpoint `https://<function-app>.azurewebsites.net/runtime/webhooks/mcp` |
| Storage | `<storage-account>`: blob `mcp-auth` (token cache), **Table Storage** (service-request database), **Storage Queues** `sms-outbound`, `sms-inbound` (+ `-poison`); managed identity only |
| SMS | `SMS_PROVIDER=mock` today (fully tested); `twilio` / `acs` implementations ready, awaiting provider approval |
| Entra app (Graph delegated) | `Nexroza Plumbing Calendar MCP`, client id `<graph-client-id>`, public client + PKCE, **no client secret**, permissions `User.Read`, `Calendars.ReadWrite` |
| Foundry | account `<foundry-account>` (westus3), project `<foundry-project>`, agent `Plumbing-Service-Coordinator` (v4: 8 tools), connection `nexroza-calendar-mcp` |
| Monitoring | App Insights `appi-<token>`, action group `ag-nexroza-calendar`, 4 alert rules (`infra/monitoring.bicep`) |

## Calendar naming and technician status rules

The owner's Outlook must contain calendars with **exactly** these names
(matched by name, case-sensitive):

| Calendar | Technician | Skill code |
|----------|-----------|-----------|
| `John - Plumbing` | John | `general_plumbing` |
| `Sara - Drain Services` | Sara | `drain_services` |
| `Michael - Water Heaters` | Michael | `water_heaters` |

For a requested date (Toronto local midnight → next midnight, 23/25 h on DST
days), the status is derived from event **subjects** that *start* with a
keyword (case-insensitive):

| Subject starts with | Status | Meaning |
|---------------------|--------|---------|
| `OFF`, `VACATION`, `SICK` | that word | unavailable (wins over everything) |
| `ON CALL`, `ON_CALL`, `ON-CALL` | `ON_CALL` | emergency fallback only |
| nothing matching | `WORKING` | available |
| calendar missing | `CALENDAR_NOT_FOUND` | treat as "cannot check" |

Only status events are returned to the agent; other appointments stay
private, and the agent is instructed to say just available / unavailable /
emergency-only.

## Service-request workflow

1. **Safety check** (agent): gas smell → leave + gas utility/911; flooding → shut main valve, emergency; electrical hazard → breaker off, emergency.
2. **Collect** name, callback phone, address + postal code, service type, short issue, preferred date/time window, SMS consent. Nothing else.
3. `create_service_request` → status `new`, returns the **tracking reference** `NX-…` (≈72-bit random, stored hashed).
4. `find_matching_technicians` → skill ∧ active ∧ not opted-out ∧ service area (postal FSA prefix) ∧ weekly schedule overlap ∧ Outlook `WORKING` (`ON_CALL` only when `is_emergency`), never `OFF/SICK/VACATION`; ranked by primary skill, pending load, overlap.
5. `send_technician_request` → queues an SMS and returns immediately (`awaiting_technician`, reply deadline = 30 min). The customer gets the reference and "confirmation follows".
6. Queue `sms-outbound` → provider sends *"Nexroza job ABCD: Water heaters in M1B on Mon Sep 14 between 4:00 PM and 8:00 PM. Reply YES, a time (e.g. 5:30 PM), or NO. Reply STOP to opt out."* (no customer PII).
7. Technician replies → provider webhook `POST /api/sms/inbound` (signature validated) → queue `sms-inbound` → interpreted (`yes` / `5:30 PM` / `after 6` / `no` / `STOP`), correlated by reply code or the technician's pending request, stored → `technician_proposed_time`.
8. Customer returns: `get_service_request_status(reference, phone)` → `awaiting_customer_confirmation` with the proposed time.
9. Customer accepts → `confirm_booking(customer_accepts=true)`: ETag lock → re-check Outlook (day status + slot conflict) → create Outlook event → store Graph event id → `confirmed`. Any Graph failure releases the lock and returns `booking_failed`; the agent never says "confirmed" unless the tool returns `confirmed: true`.
10. Timer `expire_requests` (every 5 min): technician timeout → `expired` → next candidate; `MAX_TECHNICIAN_ATTEMPTS` exhausted → `failed` (office follows up). Customer hold expiry after `BOOKING_HOLD_HOURS`.

### MCP tools

| Tool | Purpose |
|------|---------|
| `list_technician_calendars` | technicians, skills, calendar presence |
| `get_technician_work_status(date)` | WORKING / OFF / SICK / VACATION / ON_CALL per technician |
| `check_availability(service_type, date, postal_code?, time_from?, time_to?, emergency?)` | schedule + Outlook per technician, no request needed |
| `create_service_request(customer_name, callback_phone, address, postal_code?, service_type, issue, preferred_date, preferred_time_from?, preferred_time_to?, sms_consent?, is_emergency?, idempotency_key?)` | returns `tracking_reference` |
| `find_matching_technicians(tracking_reference, callback_phone)` | ranked candidates + no-match reasons |
| `send_technician_request(tracking_reference, callback_phone, technician_name?)` | queues SMS, returns at once |
| `get_service_request_status(tracking_reference, callback_phone)` | customer view incl. proposed time |
| `confirm_booking(tracking_reference, callback_phone, customer_accepts)` | books in Outlook or asks the technician for another time |

Every tool that touches a request requires the tracking reference **and** the
callback phone (full or last 4 digits); internal ids are never returned.
Operators can inspect a request with `GET /api/ops/requests/{reference}`
(master key; phone redacted).

### Database (Azure Table Storage, `nexroza/store.py`)

| Table | PK / RK | Contents |
|-------|---------|----------|
| `Technicians` | `technician` / `tech_*` | name, calendar_name, skills[], service_areas[] (FSA prefixes), phone, active, sms_opt_out, weekly_schedule{mon..sun: [[HH:MM,HH:MM]]} |
| `Skills` | `skill` / code | name, keywords[] |
| `ServiceRequests` | `request` / `req_*` | customer fields, service_date, window_start/end, status, tracking_hash, reply_code, candidates[], tried_technicians, assigned_technician_id, proposed_start/end, expires_at, confirm_lock, booking_id, graph_event_id, idempotency_key, created_at/updated_at (UTC) |
| `RequestEvents` | request_id / ts-seq | kind, from_status, to_status, actor, detail (status history) |
| `SmsMessages` | request_id / id | direction, technician_id, redacted number, provider, provider_message_id, status, error, body (inbound only), intent |
| `Proposals` | request_id / id | technician_id, proposed_start/end, source_message_id |
| `Bookings` | request_id / id | technician_id, calendar_id, graph_event_id, start/end |
| `Idempotency` | `idem` / sha256(scope:key) | create retries and inbound provider message ids |

Seed data (`nexroza/seed.py`): John (general_plumbing, water_heaters), Sara
(drain_services, general_plumbing), Michael (water_heaters, general_plumbing);
Toronto FSA prefix `M` (+ `L1` for Sara/Michael); Mon–Sat schedules; phones
are fictional `+1555…` placeholders. Set real numbers with
`POST /api/ops/seed` (master key) body `{"phones": {"tech_john": "+1…"}}`.

### State machine (`nexroza/models.py`)

```
new → matching → awaiting_technician → technician_proposed_time → awaiting_customer_confirmation → confirmed
                       │  ├─ technician_declined ─┐                    │  (customer declines) ──► awaiting_technician
                       │  └─ expired ─────────────┴─► matching / awaiting_technician (next technician) / failed
                       └─ cancelled / failed at any non-terminal point;  awaiting_customer_confirmation ─(hold expiry)─► expired
```
Invalid transitions raise `invalid_transition` and are covered by tests.

### SMS providers (`nexroza/sms.py`)

| Provider | Send | Inbound webhook auth | Status |
|----------|------|----------------------|--------|
| `mock` (default) | records + fake id | HMAC-SHA256 header `x-mock-signature` with `SMS_WEBHOOK_SECRET` | fully tested (unit, deployed E2E, Foundry E2E) |
| `twilio` | REST Messages API | `X-Twilio-Signature` (HMAC-SHA1 over URL+params) | implemented, unit-tested signature; needs account/number |
| `acs` | Azure Communication Services SDK | Event Grid `SMSReceived` + `?key=` shared secret, validation handshake handled | implemented; needs ACS resource + toll-free number verification |

Twilio **trial** accounts (new "free units" model): the API rejects custom
bodies (error 572006) and only sends one of Twilio's predefined templates from
the console's shared trial number, only to the verified signup number, and
message resources cannot be read back (403); real numbers require an upgrade.
`TWILIO_TRIAL_TEMPLATE=sms_appointment_reminders` is an explicit, test-only
switch that sends the template instead of the dispatch text (logged as
`SMS_TRIAL_TEMPLATE_MODE`); never set it in production. Production needs a
Pay-as-you-go Twilio account with an owned Canadian number (~US$1.15/month)
and `SMS_PROVIDER=twilio`; a Twilio failure is then reported as a failure -
the code never falls back to the mock provider at runtime.

Rules: request id (reply code), service type, approximate area (FSA) and
window only — no customer name/address/phone in SMS; delivery status and
provider message id stored; every inbound message is idempotent by provider
message id; `STOP` sets `sms_opt_out` and re-dispatches pending requests.
Technician consent: technicians are onboarded by the owner and agree to
receive dispatch SMS; customers are not texted in this MVP (`sms_consent` is
stored for future use). For the one controlled live test, set
`SMS_TEST_RECIPIENT_OVERRIDE` (app setting, never in code) so every outbound
SMS goes to the owner-controlled test number, then remove it.

### Async processing

* `send_technician_request` writes an `SmsMessages` attempt (`queued`) and a
  base64 queue message `{request_id, attempt_id}`; the trigger sends once per
  attempt (retries after success are skipped).
* Storage-queue retries: 5 dequeues → `*-poison` queue → `poison_sms_*`
  logs `POISON_MESSAGE` and marks the related request `failed` (tested live).
* Timeouts: `TECHNICIAN_REPLY_TIMEOUT_MINUTES` (30), `MAX_TECHNICIAN_ATTEMPTS`
  (3), `BOOKING_HOLD_HOURS` (12).
* Confirmation race: ETag-guarded `confirm_lock`; only one caller can create
  the Outlook event (tested with 4 concurrent threads).

## Repository layout

```
src/FunctionsMcpTool/        Function App: function_app.py (triggers), nexroza/ (workflow, store, graph, sms, matching, replies, seed), tests/
tools/authorize_outlook.py   one-time owner authorization + cache upload + verification
foundry/                     agent instructions/version, create_connection.py, test_agent.py, test_mcp_workflow.py, test_e2e_conversation.py
web/INTEGRATION.md           website integration contract; web/test_client/ minimal backend route + mobile widget
infra/                       azd Bicep (main.bicep, app/*.bicep) + monitoring.bicep
```

## Local development

```powershell
cd src\FunctionsMcpTool
py -3.14 -m venv .venv                     # Function App runs Python 3.13; 3.13+ locally
.venv\Scripts\python.exe -m pip install -r requirements.txt pytest pyflakes
.venv\Scripts\python.exe -m pytest tests -q   # pure unit tests, no Azure access
.venv\Scripts\python.exe -m pyflakes function_app.py
```

`local.settings.json` is git-ignored. To run tools locally against the real
token cache (needs `az login` and Storage Blob Data Owner on the storage
account, which `infra/app/rbac.bicep` grants to the deployer):

```powershell
$env:GRAPH_CLIENT_ID="<graph-client-id>"
$env:AzureWebJobsStorage__blobServiceUri="https://<storage-account>.blob.core.windows.net/"
.venv\Scripts\python.exe -c "import function_app as f; print(f.status._function.get_user_function()(None).get_body().decode())"
```

## Azure deployment

```powershell
cd src\FunctionsMcpTool
azd deploy            # code only — the normal path
azd provision --preview   # what-if; run `azd provision` only when infra/*.bicep changed
```

azd environment `nexroza-calendar-mcp` holds `GRAPH_CLIENT_ID` and
`UNAUTHENTICATED_CLIENT_ACTION=AllowAnonymous`, which `infra/main.parameters.json`
feeds into Bicep so provisioning keeps the working configuration.
Monitoring is a separate resource-group deployment (see [Monitoring](#monitoring)).

## One-time Outlook owner authorization

```powershell
src\FunctionsMcpTool\.venv\Scripts\python.exe tools\authorize_outlook.py
```

1. A **private** Edge/Chrome window opens with a forced fresh login. Sign in
   with the Microsoft account whose Outlook holds the technician calendars and
   accept the Calendar consent.
2. The script verifies `/me` and `/me/calendars`, warns about missing
   calendars, uploads the MSAL cache to `mcp-auth/msal-token-cache.json` with
   your `az login` identity, and forces a silent refresh to prove the Function
   App can renew tokens on its own.

Why a private window: a stale "Signed in" Microsoft-account session in the
normal browser profile makes `login.microsoftonline.com/consumers` return an
opaque `server_error` (and the device-code page "temporary problem",
errcode 10868). That was the root cause of the original failures.

## Outlook reauthorization procedure

Symptoms: alert *Nexroza: Outlook authorization or Graph failure*, or
`GET /api/status` → `503 {"error":"reauthorization_required"|"not_authorized"}`,
or the chatbot says live availability cannot be checked. Causes: password
change, revoked consent, ~90 days without use (personal accounts), account
security event.

1. Run `tools\authorize_outlook.py` again (same steps as above). Nothing else
   changes; no redeploy.
2. Confirm: `tools\authorize_outlook.py --verify-only` and `GET /api/status`
   → `200 {"outlook_connected": true, "missing_calendars": []}`.

## Microsoft Foundry MCP connection

Everything is in the existing project — no duplicate resources.

| Item | Value |
|------|-------|
| Project endpoint | `https://<foundry-account>.services.ai.azure.com/api/projects/<foundry-project>` |
| Agent | `Plumbing-Service-Coordinator` (latest version carries the instructions in `foundry/agent-instructions.md` and the MCP tool) |
| Project connection | `nexroza-calendar-mcp` — category `RemoteTool`, auth `CustomKeys`, stores the header `x-functions-key` = Function `mcp_extension` system key |
| MCP tool | `server_label: nexroza_calendar`, `project_connection_id: nexroza-calendar-mcp`, `require_approval: never`, `allowed_tools: [list_technician_calendars, get_technician_work_status]` |

Re-create / update without the portal:

```powershell
# (re)create the connection / rotate the key — reads the key from Azure, never prints it:
src\FunctionsMcpTool\.venv\Scripts\python.exe foundry\create_connection.py
# publish a new agent version from the tracked definition:
az rest --method post --resource https://ai.azure.com `
  --url "https://<foundry-account>.services.ai.azure.com/api/projects/<foundry-project>/agents/Plumbing-Service-Coordinator/versions?api-version=2025-11-15-preview" `
  --headers "Content-Type=application/json" --body @foundry/agent-version.json
```

`foundry/agent-version.json` is generated from `foundry/agent-instructions.md`
(no secrets; the key lives only in the project connection).

### Website integration (for the web developer)

Full contract: [web/INTEGRATION.md](web/INTEGRATION.md). Runnable reference: `python web/test_client/server.py` (backend route `/api/chat` with sessions, validation, rate limiting, honeypot; mobile-friendly widget with consent notice, loading/error states and a tracking-reference shortcut).

The website **backend** (never the browser) calls the Foundry Responses API
with the agent reference. Minimal Python (`pip install "azure-ai-projects>=2.0.0" openai`):

```python
from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential   # managed identity / service principal with "Azure AI User" on the project

project = AIProjectClient(endpoint="https://<foundry-account>.services.ai.azure.com/api/projects/<foundry-project>",
                          credential=DefaultAzureCredential())
client = project.get_openai_client()
resp = client.responses.create(
    input=[{"role": "user", "content": f"Current Toronto date: {today_yyyy_mm_dd}\n\n{customer_message}"}],
    previous_response_id=previous_id_or_None,          # keep the conversation
    extra_body={"agent_reference": {"name": "Plumbing-Service-Coordinator", "type": "agent_reference"}},
)
answer = resp.output_text
```

Requirements for the site:
* Prepend `Current Toronto date: YYYY-MM-DD` (America/Toronto) to each turn so
  "tomorrow"/"next Monday" resolve correctly; the agent asks for an exact date
  if it is missing.
* Authenticate the backend to Foundry with an Entra identity that has the
  **Azure AI User** role on the project. Do not put Foundry or Function keys in
  browser code.
* Customers are never asked to sign in; the MCP key never leaves Foundry.
* `foundry/test_agent.py` is a ready-made regression test of the whole path.

## Required settings and secret-handling rules

| Where | Name | Purpose | Secret? |
|-------|------|---------|---------|
| Function App setting | `GRAPH_CLIENT_ID` | Entra client id | no |
| Function App setting | `GRAPH_AUTHORITY` (optional) | default `https://login.microsoftonline.com/common` | no |
| Function App setting | `SMS_PROVIDER` | `mock` / `twilio` / `acs` | no |
| Function App setting | `SMS_WEBHOOK_SECRET` | HMAC / Event Grid key for `/api/sms/inbound` | **yes** (generated; app settings + git-ignored azd env only) |
| Function App settings | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM_NUMBER` or `ACS_ENDPOINT`, `ACS_FROM_NUMBER` | real provider (when approved) | **yes** |
| Function App setting | `SMS_TEST_RECIPIENT_OVERRIDE` | one-off live-test recipient; remove after the test | yes (phone) |
| Function App setting | `TWILIO_TRIAL_TEMPLATE` | trial-account template mode for connectivity tests only; remove after the test | no |
| Function App setting | `PUBLIC_BASE_URL` | public https base used to validate provider webhook signatures | no |
| Function App settings | `TECHNICIAN_REPLY_TIMEOUT_MINUTES`, `MAX_TECHNICIAN_ATTEMPTS`, `BOOKING_HOLD_HOURS` | workflow tuning | no |
| Function App setting | `AzureWebJobsStorage__tableServiceUri` | table endpoint (managed identity) | no |
| Function App settings | `AzureWebJobsStorage__*`, `APPLICATIONINSIGHTS_*`, `WEBSITE_AUTH_*`, `OVERRIDE_USE_MI_FIC_ASSERTION_CLIENTID` | platform / managed identity — do not edit | no |
| Function App | `mcp_extension` system key | authenticates MCP callers | **yes** — lives in Function keys and the Foundry connection only |
| Function App | `default` function key | `/api/status` health endpoint | yes |
| Blob `mcp-auth/msal-token-cache.json` | Outlook refresh token | delegated Graph access | **yes** — private container, managed identity, never downloaded into the repo |

Rules: no client secret exists or is needed (public client + PKCE); keys are
read with `az functionapp keys list` into variables, never typed or printed;
`.gitignore` blocks `local.settings.json`, `msal-token-cache*.json`, `.env*`;
`detect-secrets`/pattern scans run before commits.

## MCP authentication

* Endpoint requires the `mcp_extension` **system key** in `x-functions-key`
  (or `?code=`); missing/wrong key → `401`. `host.json` has no anonymous
  override.
* App Service Authentication stays enabled with `AllowAnonymous` so keys, not
  Entra sign-in, gate the endpoint (`infra/app/api.bicep`).
* Rotate: `az functionapp keys set -g rg-nexroza-calendar-mcp -n <function-app> --key-type systemKeys --key-name mcp_extension --key-value <new>` then update the Foundry connection `nexroza-calendar-mcp`.

## Testing

| Layer | Command | Expect |
|-------|---------|--------|
| Unit | `pytest tests -q` (in `src/FunctionsMcpTool`) | 39 passed: validation, transitions, matching/area/schedule, OFF/SICK/VACATION exclusion, ON_CALL fallback, DST, duplicate webhooks, decline, timeout, fallback, confirmation, Graph failure, concurrency, secure lookup, injection, SMS failure, provider signatures |
| Twilio hybrid live test | `foundry/test_twilio_hybrid.py [--resume]` | one real outbound SMS + Twilio-signed delivery callback; simulated carrier step with a correctly signed inbound webhook; 403 on missing/invalid signature; duplicate ignored; MCP + agent read the proposal |
| Deployed workflow | `foundry\test_mcp_workflow.py` | create → match → send → queue → signed webhook (403 on bad signature, duplicate ignored) → status → confirm → Outlook event → cleanup |
| Poison queue | enqueue a malformed message on `sms-outbound` | `POISON_MESSAGE` trace and `poison_sms_outbound` invocation |
| Web client | `python web/test_client/server.py` | `/api/chat` 200 / 400 / 429 behaviour |
| Function health | `curl -H "x-functions-key: $FKEY" https://<function-app>.azurewebsites.net/api/status` | `200 … "outlook_connected": true` |
| MCP auth | POST `/runtime/webhooks/mcp` without key | `401` |
| MCP tools | POST `tools/list`, `tools/call` with `x-functions-key` | both tools; JSON statuses |
| Stored authorization | `tools\authorize_outlook.py --verify-only` | silent refresh OK, 3 calendars found |
| Foundry availability | `foundry\test_agent.py` (or with a quoted question) | tool call logged for every availability answer |
| Foundry full conversation | `foundry\test_e2e_conversation.py` | safety check → details → reference → technician reply (webhook) → status → confirm → `confirmed` |

## Monitoring

`infra/monitoring.bicep` (deploy: `az deployment group create -g rg-nexroza-calendar-mcp -f infra/monitoring.bicep -p alertEmail=<owner>`):

| Alert | Signal | Severity |
|-------|--------|----------|
| Nexroza: Outlook authorization or Graph failure | `OUTLOOK_HEARTBEAT_FAILED`, silent refresh failures, Graph errors | 1 |
| Nexroza: MCP server errors | tool exceptions, blob cache access failures, `OUTLOOK_HEARTBEAT_DEGRADED` (missing calendar), worker crashes | 2 |
| Nexroza: Function App HTTP 5xx | `requests` with 5xx | 2 |
| Nexroza: service-request workflow failures | `SERVICE_REQUEST_FAILED`, `SMS_SEND_FAILED` / `SMS_DELIVERY_FAILURE`, `SMS_WEBHOOK_SIGNATURE_INVALID`, `POISON_MESSAGE`, `REQUEST_EXPIRED`, `BOOKING_FAILED` | 2 |

The Function's `outlook_heartbeat` timer (every 30 min) checks Graph even with
no traffic. Emails go to action group `ag-nexroza-calendar`; alerts carry markers and redacted ids only, never customer data. Cost ≈ US$2/month.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `server_error` during owner sign-in, no description | stale Microsoft-account browser session | the script already uses a private window; make sure you typed the password (fresh login) |
| `/api/status` → 503 `not_authorized` | token cache missing | run `tools/authorize_outlook.py` |
| `/api/status` → 503 `reauthorization_required` | refresh token revoked/expired | run `tools/authorize_outlook.py` |
| `CALENDAR_NOT_FOUND` / `missing_calendars` | calendar renamed/deleted in Outlook | recreate with the exact name |
| MCP `401` from Foundry | key rotated | update connection `nexroza-calendar-mcp` |
| Agent answers without a tool call | instructions changed | re-publish from `foundry/agent-version.json`; run `foundry/test_agent.py` |
| `TypeError … correlation_id` in logs | `msal.acquire_token_silent` does not accept that kwarg | keep the call as in `function_app.py` |
| Logs: App Insights via Log Analytics `log-<token>` | | `AppTraces | where Message has "correlation_id=<id>"` |

## Cost (approximate, monthly)

Flex Consumption Function App (timers ≈ 10k executions/month, tools, queues) ≈ US$0–1;
storage (blob + tables + queues, tiny volume) < US$1; Log Analytics (30-day
retention) ≈ US$1–3; alert rules ≈ US$2; Foundry `gpt-5-mini` pay-per-token
(a few cents per hundred conversations). Real SMS (not yet enabled): Twilio
Canadian local number ≈ US$1.15/month + ≈ US$0.0083/segment; ACS toll-free
≈ US$2/month + ≈ US$0.0075/message. No new Azure resources were created for
the MVP beyond the alert rules.
