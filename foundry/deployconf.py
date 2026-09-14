"""Deployment identifiers for the operator scripts - never hard-coded.

Values come from environment variables, or from a git-ignored ``deploy.env``
file at the repository root (copy ``deploy.env.example``). Nothing here is a
secret, but resource names identify your Azure estate, so they stay out of
the public repository.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / "deploy.env"

KEYS = {
    "AZURE_SUBSCRIPTION_ID": "Azure subscription id",
    "NEXROZA_RESOURCE_GROUP": "resource group of the Function App (azd: rg-<env>)",
    "NEXROZA_FUNCTION_APP": "Function App name (azd: func-api-<token>)",
    "NEXROZA_STORAGE_ACCOUNT": "storage account name (azd: st<token>)",
    "GRAPH_CLIENT_ID": "Entra app (client) id used for delegated Outlook access",
    "FOUNDRY_RESOURCE_GROUP": "resource group of the Foundry account",
    "FOUNDRY_ACCOUNT": "Foundry (AI Services) account name",
    "FOUNDRY_PROJECT": "Foundry project name",
    "FOUNDRY_AGENT_NAME": "agent name (default Plumbing-Service-Coordinator)",
    "LOG_ANALYTICS_WORKSPACE_ID": "optional: workspace id for log queries",
}


def _load_env_file() -> None:
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()


def get(key: str, default: str | None = None, required: bool = True) -> str:
    value = os.environ.get(key, default)
    if required and not value:
        sys.exit(f"Missing {key} ({KEYS.get(key, '')}). Set it in the environment or in {ENV_FILE.name} "
                 f"(see deploy.env.example).")
    return value or ""


SUBSCRIPTION = get("AZURE_SUBSCRIPTION_ID", required=False)
RG = get("NEXROZA_RESOURCE_GROUP", required=False)
APP = get("NEXROZA_FUNCTION_APP", required=False)
STORAGE = get("NEXROZA_STORAGE_ACCOUNT", required=False)
GRAPH_CLIENT_ID = get("GRAPH_CLIENT_ID", required=False)
FOUNDRY_RG = get("FOUNDRY_RESOURCE_GROUP", required=False)
FOUNDRY_ACCOUNT = get("FOUNDRY_ACCOUNT", required=False)
FOUNDRY_PROJECT = get("FOUNDRY_PROJECT", required=False)
AGENT_NAME = get("FOUNDRY_AGENT_NAME", "Plumbing-Service-Coordinator", required=False)

BASE = f"https://{APP}.azurewebsites.net" if APP else ""
MCP_URL = f"{BASE}/runtime/webhooks/mcp" if APP else ""
PROJECT_ENDPOINT = (f"https://{FOUNDRY_ACCOUNT}.services.ai.azure.com/api/projects/{FOUNDRY_PROJECT}"
                    if FOUNDRY_ACCOUNT and FOUNDRY_PROJECT else "")
TABLE_ENDPOINT = f"https://{STORAGE}.table.core.windows.net" if STORAGE else ""
BLOB_ENDPOINT = f"https://{STORAGE}.blob.core.windows.net/" if STORAGE else ""


def require(*names: str) -> None:
    for name in names:
        get(name)
