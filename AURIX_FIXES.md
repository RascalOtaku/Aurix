# Aurix Fixes & Improvements

This document summarizes the improvements made to Aurix to enhance multi-agent dispatch, model routing, and MCP server discovery.

## New Modules

### 1. `src/model_router.py`
**Purpose:** Lightweight task-aware model selection for different workload types.

**Key Functions:**
- `infer_task_kind(prompt)` — Detects task type (coding, research, utility, vision, default)
- `choose_model_for_task(prompt, available_models, strategy)` — Selects the best model from available options based on task type and cost/quality profile
- `route_model_for_role(role, available_models)` — Routes by role name
- `model_router_payload(prompt, available_models, strategy)` — Returns a routing descriptor for logging/persistence

**Model Profiles:**
- **Coding:** Prefers `gpt-4.1`, `claude-sonnet-4`, `o3` → falls back to `gpt-4o-mini`, `deepseek-chat`
- **Research:** Prefers `o3`, `deepseek-reasoner`, `claude-sonnet-4` → falls back to `gpt-4.1`
- **Utility:** Prefers `gpt-4o-mini`, `claude-haiku-4` → falls back to `gpt-4o`
- **Vision:** Prefers `gpt-4o`, `gemini-2.5-pro`, `claude-sonnet-4` → falls back to `gpt-4o-mini`
- **Default:** Balanced quality/cost mix

**Integration:** Use in `agent_loop.py` or `task_scheduler.py` to route multi-step workflows to appropriate models without centralizing endpoint logic.

---

### 2. `src/mcp_auto_discover.py`
**Purpose:** Auto-discover and register MCP servers from environment variables.

**Key Functions:**
- `discover_mcp_servers_from_env()` — Reads from:
  - `MCP_SERVERS='[{"name": "...", "url": "https://..."}]'`
  - `MCP_SERVER_URL`, `MCP_SSE_URL`, `MCP_HTTP_URL`
  - `MCP_SERVER_NAME` (for single-server mode)
  - Returns normalized list compatible with `McpServer` model
  
- `discover_mcp_servers_from_db(db_module)` — Fetches enabled MCP servers from database if available

**Auto-Deduplication:** Prevents duplicate registrations by (name, url) key.

**Integration:** Call `discover_mcp_servers_from_env()` on startup in `mcp_manager.py` to auto-connect MCP servers without manual database entries.

---

## How to Use

### Model Routing Example
```python
from src.model_router import choose_model_for_task

# In agent_loop.py or a task handler:
prompt = "Fix the login bug in auth.py"
available_models = ["gpt-4o-mini", "gpt-4.1", "deepseek-chat", "o3"]
model = choose_model_for_task(prompt, available_models)
# Returns: "gpt-4.1" (best match for coding task)
```

### MCP Auto-Discovery Example
```python
from src.mcp_auto_discover import discover_mcp_servers_from_env

# On app startup:
servers = discover_mcp_servers_from_env()
# If MCP_SERVER_URL="https://example.com/sse", returns:
# [{"name": "auto-discovered-mcp", "transport": "sse", "url": "...", ...}]

# Then pass to mcp_manager:
for server in servers:
    await mcp_manager.connect_server(
        server_id=server["name"],
        name=server["name"],
        transport=server["transport"],
        url=server["url"],
    )
```

---

## Integration Checklist

- [ ] **Model Router:** Import `choose_model_for_task` in `agent_loop.py` or `task_scheduler.py` for fallback/routing decisions
- [ ] **MCP Discovery:** Call `discover_mcp_servers_from_env()` in app startup (e.g., `main.py` or `__init__.py`)
- [ ] **Settings:** Add routing/MCP config keys to user settings (optional but recommended for UI control)
- [ ] **Testing:** Verify routing with `python -m pytest tests/test_model_router.py`
- [ ] **Environment:** Set `MCP_SERVER_URL` or `MCP_SERVERS` in `.env` for auto-discovery

---

## What These Fixes Address

### Multi-Agent Orchestration
- **Before:** All tasks routed to one fixed model endpoint
- **After:** Tasks intelligently routed based on type (coding → stronger model, utility → faster/cheaper model)

### MCP Server Management
- **Before:** Manual database entries required for each MCP server
- **After:** Servers auto-discovered from environment variables on startup

### Extensibility
- Both modules are lightweight, non-invasive, and work alongside existing systems
- No breaking changes to `agent_loop.py`, `llm_core.py`, or `mcp_manager.py`
- Can be adopted incrementally

---

## Architecture

```
User Request
    ↓
agent_loop.py (existing)
    ├→ model_router.infer_task_kind()
    ├→ model_router.choose_model_for_task()
    └→ route to appropriate endpoint_url + model
    
    ↓ (multi-round execution with tools)
    
mcp_manager.py (existing, enhanced)
    ├→ mcp_auto_discover.discover_mcp_servers_from_env()
    └→ connect servers before first use
```

---

## Files Changed

1. ✅ **Created** `src/model_router.py` — Task-aware model selection
2. ✅ **Created** `src/mcp_auto_discover.py` — Environment-driven MCP discovery

---

## Future Enhancements

- Add user settings UI for model profile customization
- Expose routing metrics (which tasks used which models, success rates)
- Add cost tracking per task type
- Support for dynamic model availability checks before routing
