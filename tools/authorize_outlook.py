"""One-time Outlook authorization for the Nexroza calendar MCP server.

Run this on an administrator's machine (needs ``az login`` with the Azure
account that owns the deployment). It:

1. Opens a *private* browser window so the *business owner* signs in fresh
   with the Microsoft account that owns the technician calendars
   (auth-code + PKCE, public client, no secret). A private window matters:
   a stale signed-in Microsoft-account session makes the consumer login
   endpoint fail with an opaque ``server_error``.
2. Verifies the delegated token against Microsoft Graph (/me, /me/calendars)
   and reports which expected technician calendars are missing.
3. Uploads the serialized MSAL token cache (with refresh token) to the
   Function App's private blob ``mcp-auth/msal-token-cache.json`` using your
   Azure identity - no bootstrap endpoint or setup key is exposed publicly.
4. Proves that a *silent* refresh from the uploaded cache works, which is
   exactly what the Function App does at runtime.

Nothing secret is printed. Requires the packages in
``src/FunctionsMcpTool/requirements.txt`` (msal, requests, azure-identity,
azure-storage-blob).

Usage (defaults match the deployed environment):

    python tools/authorize_outlook.py
    python tools/authorize_outlook.py --verify-only   # check the stored cache
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import msal
import requests
from azure.core.exceptions import ResourceNotFoundError
from azure.identity import AzureCliCredential, AzureDeveloperCliCredential, ChainedTokenCredential
from azure.storage.blob import BlobServiceClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "foundry"))
import deployconf as _cfg  # noqa: E402  (reads deploy.env / environment)

DEFAULT_CLIENT_ID = _cfg.GRAPH_CLIENT_ID
DEFAULT_STORAGE_ACCOUNT = _cfg.STORAGE
DEFAULT_CONTAINER = "mcp-auth"
DEFAULT_BLOB = "msal-token-cache.json"
DEFAULT_AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES = ["User.Read", "Calendars.ReadWrite"]
GRAPH = "https://graph.microsoft.com/v1.0"

TECHNICIANS = [
    "John - Plumbing",
    "Sara - Drain Services",
    "Michael - Water Heaters",
]


# Browsers that can open a *private* window. A stale "Signed in" Microsoft
# account session in the normal profile makes login.microsoftonline.com/consumers
# answer with a bare `server_error`; a fresh private-window login avoids it.
PRIVATE_BROWSERS = [
    (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe", ["--inprivate", "--new-window"]),
    (r"C:\Program Files\Microsoft\Edge\Application\msedge.exe", ["--inprivate", "--new-window"]),
    (r"C:\Program Files\Google\Chrome\Application\chrome.exe", ["--incognito", "--new-window"]),
    (r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe", ["--incognito", "--new-window"]),
]


def _open_private_browser(url: str) -> bool:
    for exe, flags in PRIVATE_BROWSERS:
        if os.path.exists(exe):
            subprocess.Popen([exe, *flags, url])
            return True
    for name in ("msedge", "chrome", "google-chrome"):
        exe = shutil.which(name)
        if exe:
            subprocess.Popen([exe, "--inprivate" if "edge" in name else "--incognito", url])
            return True
    return False


def _acquire_token_fresh_login(app: msal.PublicClientApplication, timeout: int = 600) -> dict:
    """Auth-code + PKCE with prompt=login in a private browser window."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    redirect_uri = f"http://localhost:{port}"

    flow = app.initiate_auth_code_flow(
        SCOPES, redirect_uri=redirect_uri, prompt="login", response_mode="form_post"
    )
    captured: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def _finish(self, params: dict) -> None:
            captured.update(params)
            body = ("<html><body><h2>Outlook connected. You can close this window and return to "
                    "the terminal.</h2></body></html>" if "code" in params else
                    "<html><body><h2>Authorization failed - see the terminal.</h2></body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body.encode())

        def do_GET(self):  # noqa: N802
            self._finish({k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()})

        def do_POST(self):  # noqa: N802
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode()
            self._finish({k: v[0] for k, v in parse_qs(raw).items()})

        def log_message(self, *args):  # silence request logging
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = timeout
    if _open_private_browser(flow["auth_uri"]):
        print("Opened a private browser window for a fresh Microsoft sign-in.")
    else:
        webbrowser.open(flow["auth_uri"])
        print("No Edge/Chrome found; opened the default browser. If you get server_error, use a private window.")
    server.handle_request()
    server.server_close()
    if not captured:
        return {"error": "timeout", "error_description": "No sign-in completed within the time limit."}
    return app.acquire_token_by_auth_code_flow(flow, captured)


def _fail(message: str, code: int = 1) -> None:
    print(f"\nERROR: {message}", file=sys.stderr)
    sys.exit(code)


def _blob_client(args: argparse.Namespace):
    credential = ChainedTokenCredential(AzureCliCredential(), AzureDeveloperCliCredential())
    service = BlobServiceClient(f"https://{args.storage_account}.blob.core.windows.net", credential=credential)
    container = service.get_container_client(args.container)
    if not container.exists():
        container.create_container()
    return container.get_blob_client(args.blob)


def _graph_get(token: str, path: str, params: dict | None = None) -> dict:
    response = requests.get(
        f"{GRAPH}{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        params=params,
        timeout=30,
    )
    if not response.ok:
        try:
            err = response.json().get("error", {})
            detail = f"{err.get('code')}: {err.get('message')}"
        except ValueError:
            detail = response.text[:300]
        raise RuntimeError(f"Graph {path} -> HTTP {response.status_code} {detail}")
    return response.json()


def _describe_account(token: str) -> None:
    me = _graph_get(token, "/me", {"$select": "displayName,userPrincipalName,mail"})
    print(f"  Signed in as: {me.get('displayName')} <{me.get('mail') or me.get('userPrincipalName')}>")

    calendars = _graph_get(token, "/me/calendars", {"$select": "name", "$top": "100"})
    names = [c["name"] for c in calendars.get("value", [])]
    print(f"  Calendars visible ({len(names)}): {', '.join(names) if names else '(none)'}")
    missing = [n for n in TECHNICIANS if n not in names]
    if missing:
        print("  WARNING - expected technician calendars not found:")
        for name in missing:
            print(f"    - {name}")
        print("  Create them in Outlook with exactly these names (the server matches by name).")
    else:
        print("  All three technician calendars found.")


def _silent_check(serialized_cache: str, args: argparse.Namespace) -> None:
    """Simulate the Function App: cache -> silent token (forced refresh)."""
    cache = msal.SerializableTokenCache()
    cache.deserialize(serialized_cache)
    app = msal.PublicClientApplication(args.client_id, authority=args.authority, token_cache=cache)
    accounts = app.get_accounts()
    if not accounts:
        _fail("Stored cache contains no account; run without --verify-only to authorize.")
    result = app.acquire_token_silent(SCOPES, account=accounts[0], force_refresh=True)
    if not result or "access_token" not in result:
        _fail(f"Silent refresh failed: {(result or {}).get('error')} - {(result or {}).get('error_description')}")
    print("  Silent refresh with the stored refresh token: OK")
    _describe_account(result["access_token"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client-id", default=DEFAULT_CLIENT_ID)
    parser.add_argument("--authority", default=DEFAULT_AUTHORITY,
                        help="Use .../consumers to force a personal account, or a tenant ID for Microsoft 365.")
    parser.add_argument("--storage-account", default=DEFAULT_STORAGE_ACCOUNT)
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--blob", default=DEFAULT_BLOB)
    parser.add_argument("--verify-only", action="store_true", help="Only test the cache already in blob storage.")
    parser.add_argument("--no-upload", action="store_true", help="Authorize and verify but do not upload.")
    args = parser.parse_args()

    blob = None
    if not args.no_upload or args.verify_only:
        print("Connecting to Azure Storage with your az login identity...")
        blob = _blob_client(args)

    if args.verify_only:
        try:
            serialized = blob.download_blob().readall().decode("utf-8")
        except ResourceNotFoundError:
            _fail("No token cache is stored yet; run without --verify-only.")
        print("Verifying stored token cache:")
        _silent_check(serialized, args)
        return

    cache = msal.SerializableTokenCache()
    app = msal.PublicClientApplication(args.client_id, authority=args.authority, token_cache=cache)

    print("\nA browser window will open. Sign in with the Microsoft account that OWNS the technician calendars.")
    print("(Do not sign in with a customer or personal account by mistake.)\n")
    result = _acquire_token_fresh_login(app)
    if "access_token" not in result:
        print(json.dumps({k: result.get(k) for k in ("error", "error_description", "correlation_id")}, indent=2))
        _fail("Interactive authorization failed (details above). A bare `server_error` from a personal "
              "Microsoft account means a stale browser session; make sure the private window was used and "
              "that you typed the password (fresh login).")

    print("Authorization succeeded. Checking Microsoft Graph access:")
    try:
        _describe_account(result["access_token"])
    except RuntimeError as exc:
        _fail(str(exc))

    serialized = cache.serialize()
    if args.no_upload:
        print("\n--no-upload given; token cache NOT stored.")
        return

    blob.upload_blob(serialized, overwrite=True)
    print(f"\nUploaded token cache to {args.storage_account}/{args.container}/{args.blob}")

    print("Re-reading the uploaded cache and forcing a silent refresh (what the Function App does):")
    _silent_check(blob.download_blob().readall().decode("utf-8"), args)
    print("\nDone. The MCP server can now read the calendars without any further sign-in.")


if __name__ == "__main__":
    main()
