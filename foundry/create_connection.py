"""Create or rotate the Foundry project connection that holds the MCP key.

Reads the Function App's ``mcp_extension`` system key through the Azure CLI
and stores it in the project connection ``nexroza-calendar-mcp`` as the
``x-functions-key`` header (category RemoteTool, auth CustomKeys). The key is
never printed. Re-run after rotating the Function key.

Requires: az login with rights on both resource groups.
    python foundry/create_connection.py
"""

from __future__ import annotations

import json
import subprocess
import sys

import os
import requests
from azure.identity import AzureCliCredential

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import deployconf as cfg  # noqa: E402

cfg.require("AZURE_SUBSCRIPTION_ID", "FOUNDRY_RESOURCE_GROUP", "FOUNDRY_ACCOUNT", "FOUNDRY_PROJECT",
            "NEXROZA_RESOURCE_GROUP", "NEXROZA_FUNCTION_APP")
SUBSCRIPTION, FOUNDRY_RG, FOUNDRY_ACCOUNT, FOUNDRY_PROJECT = cfg.SUBSCRIPTION, cfg.FOUNDRY_RG, cfg.FOUNDRY_ACCOUNT, cfg.FOUNDRY_PROJECT
CONNECTION_NAME = "nexroza-calendar-mcp"
FUNCTION_RG, FUNCTION_APP, MCP_URL = cfg.RG, cfg.APP, cfg.MCP_URL

ARM = (
    f"https://management.azure.com/subscriptions/{SUBSCRIPTION}/resourceGroups/{FOUNDRY_RG}"
    f"/providers/Microsoft.CognitiveServices/accounts/{FOUNDRY_ACCOUNT}/projects/{FOUNDRY_PROJECT}"
    f"/connections/{CONNECTION_NAME}?api-version=2025-06-01"
)


def main() -> None:
    key = subprocess.run(
        ["az", "functionapp", "keys", "list", "-g", FUNCTION_RG, "-n", FUNCTION_APP,
         "--query", "systemKeys.mcp_extension", "-o", "tsv"],
        capture_output=True, text=True, shell=sys.platform.startswith("win"), check=True,
    ).stdout.strip()
    if len(key) < 30:
        sys.exit("Could not read the mcp_extension system key.")

    token = AzureCliCredential().get_token("https://management.azure.com/.default").token
    body = {
        "properties": {
            "category": "RemoteTool",
            "target": MCP_URL,
            "authType": "CustomKeys",
            "credentials": {"keys": {"x-functions-key": key}},
            "isSharedToAll": False,
            "metadata": {"ApiType": "Azure", "description": "Nexroza technician calendar MCP server"},
        }
    }
    response = requests.put(ARM, json=body, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    if not response.ok:
        sys.exit(f"ARM PUT failed: HTTP {response.status_code} {response.text[:300].replace(key, '<redacted>')}")
    props = response.json().get("properties", {})
    print(json.dumps({"connection": CONNECTION_NAME, "category": props.get("category"),
                      "authType": props.get("authType"), "target": props.get("target")}, indent=2))
    print("Key stored in the Foundry connection (not shown).")


if __name__ == "__main__":
    main()
