"""src/mcp_presets.py - opt-in MCP servers from projects the owner follows.

Nothing here starts on its own. List the ones you want in AURIX_MCP_PRESETS (comma-separated) and they are
connected at startup next to the built-ins (src/builtin_mcp.py -> src/mcp_auto_discover.py):

    AURIX_MCP_PRESETS=blender,context-mode

Each preset records its upstream, license and the safety defaults Aurix applies. The env block is the
*default*; a variable already set in the host environment wins (so BLENDER_MCP_SAFE_MODE=0 turns safe mode off
deliberately, never by accident).
"""
from __future__ import annotations

import os
from typing import Any, Dict, List

MCP_PRESETS: Dict[str, Dict[str, Any]] = {
    # Blender over MCP. The Blender add-on listens on a raw, unauthenticated socket and the server exposes
    # `execute_blender_code` (arbitrary Python inside Blender), so safe mode is ON by default here and the
    # socket stays on localhost unless the owner points BLENDER_HOST elsewhere.
    "blender": {
        "name": "Blender (mcp-for-blender)",
        "upstream": "https://github.com/ahujasid/mcp-for-blender",
        "license": "MIT",
        "transport": "stdio",
        "command": "uvx",
        "args": ["mcp-for-blender"],
        "env": {
            "BLENDER_HOST": "localhost",
            "BLENDER_PORT": "9876",
            "BLENDER_MCP_SAFE_MODE": "1",
            "DISABLE_TELEMETRY": "true",
        },
        "needs": "Blender running with the mcp-for-blender add-on enabled, and `uv` on PATH.",
    },
    # context-mode: sandboxes bulky tool output and indexes it (SQLite FTS5 / BM25) so only the relevant
    # slice reaches the model. Useful for long agent runs on small local context windows.
    "context-mode": {
        "name": "context-mode",
        "upstream": "https://github.com/mksglu/context-mode",
        "license": "ELv2 (self-hosting is fine; do not resell it as a hosted service)",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "context-mode"],
        "env": {},
        "needs": "Node.js; run `npx -y context-mode --version` once so the package is cached.",
    },
}


def selected_presets(raw: str | None = None) -> List[str]:
    raw = os.environ.get("AURIX_MCP_PRESETS", "") if raw is None else raw
    out: List[str] = []
    for name in (raw or "").split(","):
        name = name.strip().lower()
        if name and name in MCP_PRESETS and name not in out:
            out.append(name)
    return out


def preset_servers(raw: str | None = None) -> List[Dict[str, Any]]:
    """Server dicts (same shape as src/mcp_auto_discover.py) for the enabled presets."""
    servers = []
    for key in selected_presets(raw):
        spec = MCP_PRESETS[key]
        env = {k: os.environ.get(k, v) for k, v in spec["env"].items()}
        servers.append({
            "id": f"builtin_preset_{key.replace('-', '_')}",
            "name": spec["name"],
            "transport": spec["transport"],
            "url": "",
            "command": spec["command"],
            "args": list(spec["args"]),
            "env": env,
            "is_enabled": True,
            "oauth_config": None,
        })
    return servers
