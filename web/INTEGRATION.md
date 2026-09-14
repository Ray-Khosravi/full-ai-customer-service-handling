# Website integration contract — Nexroza chat widget

The Nexroza website source is not in this repository, so this document is the
contract the website developer implements. `web/test_client/` is a minimal,
runnable reference (backend route + mobile-friendly widget) that follows it.

## Topology (no secrets in the browser)

```
browser widget  --HTTPS-->  website backend route  --Entra token-->  Foundry Responses API (agent)
                              (session, rate limit)                        |
                                                                           v  MCP + x-functions-key (held in Foundry)
                                                                     Azure Functions MCP server
```

* The browser talks **only** to the website backend. It never sees Foundry
  endpoints, model keys, the MCP URL/key, SMS credentials, or internal IDs.
* The backend authenticates to Foundry with a **Microsoft Entra identity**
  (managed identity on Azure hosting, or a service principal / workload
  identity elsewhere) that has the **Azure AI User** role on project
  your Foundry project (see `deploy.env`). No API keys.

## Backend route

`POST /api/chat`  (JSON, same origin, `Content-Type: application/json`)

Request
```json
{ "session_id": "<opaque, server-issued>", "message": "<user text, 1..1000 chars>" }
```
Response
```json
{ "session_id": "…", "reply": "<agent text>", "status": "ok" }
```
Errors: `400` invalid input, `429` rate limited, `503` `{"status":"unavailable","reply":"…friendly message…"}`.

Backend responsibilities
1. **Session**: issue a random `session_id` (>=128-bit) in an HttpOnly cookie
   or in the first response; map it to the Foundry `previous_response_id` so
   the conversation continues (`web/test_client/server.py` keeps an in-memory
   map; use a cache/DB with a 24 h TTL in production).
2. **Date context**: prepend `Current Toronto date: YYYY-MM-DD` (America/Toronto)
   to every user turn — the agent relies on it for "today/tomorrow".
3. **Call Foundry**: `responses.create(input=[…], previous_response_id=…,
   extra_body={"agent_reference": {"name": "Plumbing-Service-Coordinator", "type": "agent_reference"}})`
   with `azure-ai-projects>=2.0.0` (`AIProjectClient(...).get_openai_client()`)
   or the REST equivalent.
4. **Validation**: trim, cap 1000 chars, reject empty/binary, strip control
   characters. Never forward headers, cookies, or IPs to the agent.
5. **Rate limiting**: e.g. 20 messages / 5 minutes per session and per IP;
   respond `429` with a retry hint. Add a honeypot field or CAPTCHA on first
   message for abuse protection.
6. **Errors**: map Foundry/tool failures and content-filter `400`s to a
   friendly `503`/`200` reply ("I couldn't process that — please rephrase or
   call the office"); never leak exception text.
7. **Logging**: log session id, timestamp, latency, error class only. Do not log
   message bodies (they contain names/phones/addresses).

## Widget (browser)

* Mobile-first, fixed bottom-right launcher, full-screen sheet on phones.
* States: idle, sending (spinner, input disabled), error (retry), offline.
* Shows a **privacy/consent notice** before the first message: what is
  collected (name, phone, address, issue), why (to dispatch a technician),
  that SMS may be used to contact technicians, and a link to the privacy policy.
* Renders the **tracking reference** prominently when the agent returns one
  (regex `NX-[A-Z0-9]{8,16}`) with a "check status" shortcut that pre-fills
  "What's the status of NX-…? My phone ends in ____".
* Never renders raw HTML from the model (text only), max 4 KB per reply.

## Customer-facing flow the widget must support

1. Safety questions → 2. details → 3. "request submitted, reference NX-…" →
(later) 4. status check with reference + phone → 5. proposed time → 6. "yes"
→ 7. confirmation text. Steps 3→4 may be minutes to an hour apart; the
widget must not block or poll — the customer returns and asks for status.

## What the website must NOT do

* No Microsoft sign-in for customers, no Entra/MSAL in the browser.
* No direct calls to the MCP endpoint, Foundry, Graph, or SMS provider.
* No storage of customer message content in browser storage beyond the
  current session.
