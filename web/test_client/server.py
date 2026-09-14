"""Minimal standalone test client for the Nexroza chat widget.

Serves index.html and a POST /api/chat route that forwards messages to the
Foundry agent with the caller's Entra identity (az login locally; managed
identity in Azure). Implements the integration contract in web/INTEGRATION.md:
server-issued sessions, input validation, per-session and per-IP rate limits,
Toronto date context, friendly error mapping, no message-body logging.

    python web/test_client/server.py            # http://localhost:8787
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "foundry"))
import deployconf as cfg  # noqa: E402

PROJECT_ENDPOINT = os.environ.get("FOUNDRY_PROJECT_ENDPOINT") or cfg.PROJECT_ENDPOINT
AGENT_NAME = cfg.AGENT_NAME
PORT = int(os.environ.get("PORT", "8787"))
MAX_CHARS = 1000
RATE_LIMIT = (20, 300)  # 20 messages per 5 minutes per session and per IP
HERE = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("nexroza.web")

_client = AIProjectClient(endpoint=PROJECT_ENDPOINT, credential=DefaultAzureCredential()).get_openai_client()
_sessions: dict[str, dict] = {}          # session_id -> {"previous": response_id, "seen": ts}
_hits: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _limited(key: str) -> bool:
    now = time.time()
    with _lock:
        q = _hits[key]
        while q and q[0] < now - RATE_LIMIT[1]:
            q.popleft()
        if len(q) >= RATE_LIMIT[0]:
            return True
        q.append(now)
    return False


def _chat(session_id: str, message: str) -> str:
    today = datetime.now(ZoneInfo("America/Toronto")).date().isoformat()
    session = _sessions.setdefault(session_id, {"previous": None, "seen": time.time()})
    kwargs = {"previous_response_id": session["previous"]} if session["previous"] else {}
    response = _client.responses.create(
        input=[{"role": "user", "content": f"Current Toronto date: {today}\n\n{message}"}],
        extra_body={"agent_reference": {"name": AGENT_NAME, "type": "agent_reference"}},
        **kwargs,
    )
    session["previous"], session["seen"] = response.id, time.time()
    return (response.output_text or "").strip()[:4000]


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        return self._send(404, b'{"error":"not_found"}')

    def do_POST(self):  # noqa: N802
        if self.path != "/api/chat":
            return self._send(404, b'{"error":"not_found"}')
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TypeError):
            return self._send(400, b'{"status":"error","reply":"Invalid request."}')
        message = CONTROL.sub("", str(payload.get("message", ""))).strip()
        if not message or len(message) > MAX_CHARS or payload.get("website"):  # "website" = honeypot field
            return self._send(400, b'{"status":"error","reply":"Please enter a message (up to 1000 characters)."}')
        session_id = str(payload.get("session_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,64}", session_id):
            session_id = secrets.token_urlsafe(32)
        ip = self.client_address[0]
        if _limited(f"s:{session_id}") or _limited(f"ip:{ip}"):
            return self._send(429, json.dumps({"status": "rate_limited", "session_id": session_id,
                                               "reply": "You're sending messages too quickly. Please wait a minute."}).encode())
        started = time.time()
        try:
            reply = _chat(session_id, message)
            status = 200
            body = {"status": "ok", "session_id": session_id, "reply": reply}
        except Exception as exc:  # noqa: BLE001 - never leak details to the browser
            log.warning("chat failed session=%s error=%s", session_id[:8], type(exc).__name__)
            status = 503 if "content_filter" not in str(exc) else 200
            body = {"status": "unavailable" if status == 503 else "ok", "session_id": session_id,
                    "reply": "Sorry, I couldn't process that. Please rephrase, or call our office."}
        log.info("chat session=%s ms=%d status=%s", session_id[:8], int((time.time() - started) * 1000), status)
        return self._send(status, json.dumps(body).encode())

    def log_message(self, *args):  # default access log would include the path only; keep it quiet
        pass


if __name__ == "__main__":
    log.info("Nexroza test client on http://localhost:%d (agent %s)", PORT, AGENT_NAME)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
