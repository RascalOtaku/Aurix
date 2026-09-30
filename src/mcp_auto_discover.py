"""MCP discovery helpers.

This module intentionally stays lightweight and does not replace the existing
MCP manager. It adds auto-discovery for environment-driven MCP endpoints and
keeps the data model compatible with Aurix's `McpServer` table.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional


def _clean(value: Optional[str]) -> str:
    return (value or "").strip()


def _coerce_server(value: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None
    url = _clean(value.get("url") or value.get("server_url"))
    name = _clean(value.get("name") or value.get("server_name"))
    transport = _clean(value.get("transport") or value.get("type") or "stdio")
    if not url and not name and not transport:
        return None
    payload = {
        "name": name or (url or "mcp-server"),
        "transport": transport or "stdio",
        "url": url,
        "command": _clean(value.get("command") or value.get("cmd")),
        "args": value.get("args") or value.get("command_args") or [],
        "env": value.get("env") or {},
        "is_enabled": bool(value.get("is_enabled", True)),
        "oauth_config": value.get("oauth_config"),
    }
    return payload


def discover_mcp_servers_from_env() -> List[Dict[str, Any]]:
    """Discover MCP endpoints from environment variables.

    Supported patterns:
      - MCP_SERVERS='[{"name": "...", "url": "https://..."}]'
      - MCP_SERVER_URL=https://example.com/sse
      - MCP_SERVER_NAME=my-server
    """
    servers: List[Dict[str, Any]] = []
    raw = os.getenv("MCP_SERVERS", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                for item in parsed:
                    server = _coerce_server(item)
                    if server:
                        servers.append(server)
        except Exception:
            pass

    for key in ["MCP_SERVER_URL", "MCP_SSE_URL", "MCP_HTTP_URL"]:
        url = _clean(os.getenv(key))
        if url:
            servers.append({
                "name": os.getenv("MCP_SERVER_NAME", "auto-discovered-mcp"),
                "transport": "sse",
                "url": url,
                "command": "",
                "args": [],
                "env": {},
                "is_enabled": True,
                "oauth_config": None,
            })

    # Deduplicate by name+url
    seen = set()
    deduped: List[Dict[str, Any]] = []
    for server in servers:
        key = (server.get("name", ""), server.get("url", ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(server)
    return deduped


def discover_mcp_servers_from_db(db_module: Any = None) -> List[Dict[str, Any]]:
    """Fetch enabled MCP servers from a SQLAlchemy DB module if available."""
    if db_module is None:
        try:
            from src.database import McpServer
            db_module = McpServer
        except Exception:
            return []

    try:
        if hasattr(db_module, "query"):
            rows = db_module.query().all()
        else:
            return []
        return [
            {
                "name": row.name,
                "transport": row.transport,
                "url": row.url,
                "command": row.command,
                "args": row.args or [],
                "env": row.env or {},
                "is_enabled": row.is_enabled,
                "oauth_config": row.oauth_config,
            }
            for row in rows
            if getattr(row, "is_enabled", True)
        ]
    except Exception:
        return []
