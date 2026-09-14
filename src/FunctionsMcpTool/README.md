# FunctionsMcpTool — Nexroza calendar MCP server

This folder is the Azure Function App (name in `deploy.env` → `NEXROZA_FUNCTION_APP`).
All project documentation — architecture, owner authorization, Foundry
connection, monitoring, testing, troubleshooting — lives in the
[repository README](../../README.md).

Quick reference:

```powershell
.venv\Scripts\python.exe -m pytest tests -q   # unit tests
azd deploy                                     # deploy this app
```

Files: `function_app.py` (MCP tools, `/api/status`, `outlook_heartbeat` timer),
`host.json` (MCP endpoint requires the `mcp_extension` system key),
`requirements.txt`, `tests/`. `local.settings.json` is git-ignored; create it
locally with `FUNCTIONS_WORKER_RUNTIME=python` and
`AzureWebJobsStorage=UseDevelopmentStorage=true` if you run `func start`.
