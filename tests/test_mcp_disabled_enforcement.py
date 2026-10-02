"""Regression tests: disabled MCP tools are execution-blocked, not just hidden.

Disabling a tool in settings removes it from the listings handed to the model
(get_all_openai_schemas / get_tool_descriptions_for_prompt), but
McpManager.call_tool must ALSO refuse to execute it — otherwise anything that
invokes the qualified name directly (agent loop, scheduler, a sneaky prompt)
still runs the tool. These tests pin that execution gate.
"""
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.exceptions import McpToolDisabledError  # noqa: E402
from src.mcp_manager import McpManager  # noqa: E402


def _fake_mcp_result(text="ok"):
    """Minimal stand-in for an MCP SDK CallToolResult."""
    return SimpleNamespace(content=[SimpleNamespace(text=text)], isError=False)


def _manager_with_session(server_id="srv1"):
    m = McpManager()
    session = AsyncMock()
    session.call_tool.return_value = _fake_mcp_result()
    m._sessions[server_id] = session
    m._tools[server_id] = [{"name": "secret_tool", "description": "d", "input_schema": {}}]
    return m, session


def _run(coro):
    return asyncio.run(coro)


class DisabledToolEnforcementTests(unittest.TestCase):
    def test_disabled_tool_raises_and_session_never_invoked(self):
        m, session = _manager_with_session()
        m.set_disabled_map({"srv1": {"secret_tool"}})
        with self.assertRaises(McpToolDisabledError) as ctx:
            _run(m.call_tool("mcp__srv1__secret_tool", {"k": "v"}))
        self.assertEqual(ctx.exception.tool_name, "secret_tool")
        self.assertEqual(ctx.exception.server_id, "srv1")
        self.assertIn("disabled", str(ctx.exception))
        session.call_tool.assert_not_called()

    def test_enabled_tool_executes_normally(self):
        m, session = _manager_with_session()
        m.set_disabled_map({"srv1": {"other_tool"}})  # unrelated tool disabled
        result = _run(m.call_tool("mcp__srv1__secret_tool", {"k": "v"}))
        session.call_tool.assert_called_once_with("secret_tool", {"k": "v"})
        self.assertEqual(result.get("exit_code"), 0)
        self.assertEqual(result.get("stdout"), "ok")

    def test_reenabling_restores_execution(self):
        m, session = _manager_with_session()
        m.set_disabled_map({"srv1": {"secret_tool"}})
        with self.assertRaises(McpToolDisabledError):
            _run(m.call_tool("mcp__srv1__secret_tool", {}))
        session.call_tool.assert_not_called()

        # User re-enables the tool in settings
        m.set_disabled_map({})
        result = _run(m.call_tool("mcp__srv1__secret_tool", {}))
        session.call_tool.assert_called_once()
        self.assertEqual(result.get("exit_code"), 0)

    def test_disabled_check_scoped_to_server(self):
        m, session = _manager_with_session(server_id="srv1")
        session2 = AsyncMock()
        session2.call_tool.return_value = _fake_mcp_result("srv2-ok")
        m._sessions["srv2"] = session2
        # Same tool name disabled on srv1 only — srv2 still runs.
        m.set_disabled_map({"srv1": {"secret_tool"}})
        result = _run(m.call_tool("mcp__srv2__secret_tool", {}))
        session2.call_tool.assert_called_once()
        self.assertEqual(result.get("stdout"), "srv2-ok")
        with self.assertRaises(McpToolDisabledError):
            _run(m.call_tool("mcp__srv1__secret_tool", {}))

    def test_malformed_name_still_returns_error_dict(self):
        m, _ = _manager_with_session()
        m.set_disabled_map({})
        result = _run(m.call_tool("not_a_qualified_name", {}))
        self.assertEqual(result.get("exit_code"), 1)
        self.assertIn("Invalid MCP tool name", result.get("error", ""))


class DispatcherHandlingTests(unittest.TestCase):
    def test_mcp_dispatch_converts_disabled_error_to_tidy_result(self):
        """execute_tool_block must surface a clean tool result, never a traceback."""
        from src import tool_execution

        m, session = _manager_with_session()
        m.set_disabled_map({"srv1": {"secret_tool"}})
        block = SimpleNamespace(tool_type="mcp__srv1__secret_tool", content='{"k": "v"}')
        with patch.object(tool_execution, "get_mcp_manager", return_value=m), \
             patch.object(tool_execution, "_owner_is_admin", return_value=True):
            desc, result = _run(
                tool_execution.execute_tool_block(block, session_id=None, owner=None)
            )
        session.call_tool.assert_not_called()
        self.assertEqual(result.get("exit_code"), 1)
        self.assertIn("disabled", result.get("error", "").lower())
        self.assertIn("mcp", desc)

    def test_mcp_dispatch_enabled_tool_runs(self):
        from src import tool_execution

        m, session = _manager_with_session()
        m.set_disabled_map({})
        block = SimpleNamespace(tool_type="mcp__srv1__secret_tool", content='{}')
        with patch.object(tool_execution, "get_mcp_manager", return_value=m), \
             patch.object(tool_execution, "_owner_is_admin", return_value=True):
            desc, result = _run(
                tool_execution.execute_tool_block(block, session_id=None, owner=None)
            )
        session.call_tool.assert_called_once()
        self.assertEqual(result.get("exit_code"), 0)


if __name__ == "__main__":
    unittest.main()
