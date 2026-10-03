"""Regression tests for src.tool_schemas.function_call_to_tool_block.

This is the bridge between the model's native function calls and Aurix's
text-based tool execution pipeline. A wrong conversion here silently runs the
wrong tool or mangles arguments, so every branch of the converter is pinned.
"""
import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import src.agent_tools  # noqa: F401,E402 - must import first: breaks the agent_tools <-> tool_schemas circular import
from src import tool_schemas as ts  # noqa: E402


def block(name, arguments):
    return ts.function_call_to_tool_block(name, arguments)


class SimpleConversionsTests(unittest.TestCase):
    def test_bash(self):
        b = block("bash", '{"command": "ls -la"}')
        self.assertEqual((b.tool_type, b.content), ("bash", "ls -la"))

    def test_python(self):
        b = block("python", '{"code": "print(1)"}')
        self.assertEqual((b.tool_type, b.content), ("python", "print(1)"))

    def test_web_search(self):
        b = block("web_search", '{"query": "cats"}')
        self.assertEqual((b.tool_type, b.content), ("web_search", "cats"))

    def test_read_file(self):
        b = block("read_file", '{"path": "/tmp/x"}')
        self.assertEqual((b.tool_type, b.content), ("read_file", "/tmp/x"))

    def test_write_file_joins_path_and_content(self):
        b = block("write_file", '{"path": "/tmp/x", "content": "hi"}')
        self.assertEqual((b.tool_type, b.content), ("write_file", "/tmp/x\nhi"))

    def test_chat_with_model(self):
        b = block("chat_with_model", '{"model": "m", "message": "hello"}')
        self.assertEqual((b.tool_type, b.content), ("chat_with_model", "m\nhello"))

    def test_ask_teacher_defaults_model_to_auto(self):
        b = block("ask_teacher", '{"problem": "stuck"}')
        self.assertEqual((b.tool_type, b.content), ("ask_teacher", "auto\nstuck"))

    def test_list_models_uses_filter(self):
        b = block("list_models", '{"filter": "qwen"}')
        self.assertEqual((b.tool_type, b.content), ("list_models", "qwen"))


class DocumentConversionsTests(unittest.TestCase):
    def test_create_document_with_language(self):
        b = block("create_document", '{"title": "T", "language": "en", "content": "C"}')
        self.assertEqual((b.tool_type, b.content), ("create_document", "T\nen\nC"))

    def test_create_document_defaults(self):
        b = block("create_document", '{"content": "C"}')
        self.assertEqual((b.tool_type, b.content), ("create_document", "Untitled\nC"))

    def test_edit_document_block_format(self):
        b = block("edit_document", '{"edits": [{"find": "a", "replace": "b"}]}')
        self.assertEqual(b.tool_type, "edit_document")
        self.assertEqual(b.content, "<<<FIND>>>\na\n<<<REPLACE>>>\nb\n<<<END>>>")

    def test_suggest_document_block_format(self):
        b = block("suggest_document",
                  '{"suggestions": [{"find": "a", "replace": "b", "reason": "why"}]}')
        self.assertEqual(b.tool_type, "suggest_document")
        self.assertIn("<<<SUGGEST>>>\nb\n<<<REASON>>>\nwhy", b.content)

    def test_update_document(self):
        b = block("update_document", '{"content": "new body"}')
        self.assertEqual((b.tool_type, b.content), ("update_document", "new body"))


class RoutingConversionsTests(unittest.TestCase):
    def test_email_tools_route_to_mcp_email(self):
        b = block("list_emails", '{"account": "x"}')
        self.assertEqual(b.tool_type, "mcp__email__list_emails")
        self.assertEqual(json.loads(b.content), {"account": "x"})

    def test_email_tool_with_empty_args(self):
        b = block("send_email", "")
        self.assertEqual(b.tool_type, "mcp__email__send_email")
        self.assertEqual(b.content, "{}")

    def test_mcp_namespaced_tool_passes_through(self):
        b = block("mcp__srv1__mytool", '{"a": 1}')
        self.assertEqual(b.tool_type, "mcp__srv1__mytool")
        self.assertEqual(json.loads(b.content), {"a": 1})

    def test_manage_memory_add_with_category(self):
        b = block("manage_memory", '{"action": "add", "text": "remember this", "category": "prefs"}')
        self.assertEqual((b.tool_type, b.content),
                         ("manage_memory", "add\nremember this\nprefs"))

    def test_manage_memory_search(self):
        b = block("manage_memory", '{"action": "search", "text": "q"}')
        self.assertEqual((b.tool_type, b.content), ("manage_memory", "search\nq"))

    def test_manage_session_list_uses_keyword(self):
        b = block("manage_session", '{"action": "list", "keyword": "foo"}')
        self.assertEqual((b.tool_type, b.content), ("manage_session", "list\nfoo"))

    def test_manage_session_list_drops_current_default(self):
        # Regression: the agent omitting session_id must not produce
        # "No sessions found matching 'current'".
        b = block("manage_session", '{"action": "list", "session_id": "current"}')
        self.assertEqual((b.tool_type, b.content), ("manage_session", "list"))

    def test_manage_session_rename(self):
        b = block("manage_session", '{"action": "rename", "session_id": "abc", "value": "New"}')
        self.assertEqual((b.tool_type, b.content), ("manage_session", "rename\nabc\nNew"))

    def test_pipeline_passes_steps_as_json(self):
        steps = [{"a": 1}]
        b = block("pipeline", json.dumps({"steps": steps}))
        self.assertEqual(b.tool_type, "pipeline")
        self.assertEqual(json.loads(b.content), {"steps": steps})

    def test_ui_control_toggle(self):
        b = block("ui_control", '{"action": "toggle", "name": "x", "value": "y"}')
        self.assertEqual((b.tool_type, b.content), ("ui_control", "toggle x y"))

    def test_fallback_tools_keep_json_args(self):
        b = block("adopt_served_model", '{"session_id": "s1"}')
        self.assertEqual(b.tool_type, "adopt_served_model")
        self.assertEqual(json.loads(b.content), {"session_id": "s1"})


class EdgeCaseTests(unittest.TestCase):
    def test_unknown_tool_returns_none(self):
        self.assertIsNone(block("no_such_tool_xyz", "{}"))

    def test_malformed_json_returns_none(self):
        self.assertIsNone(block("bash", "{oops"))

    def test_empty_string_args(self):
        b = block("bash", "")
        self.assertEqual((b.tool_type, b.content), ("bash", ""))

    def test_whitespace_args(self):
        b = block("bash", "   ")
        self.assertEqual((b.tool_type, b.content), ("bash", ""))

    def test_dict_args_accepted_directly(self):
        b = block("bash", {"command": "ls"})
        self.assertEqual((b.tool_type, b.content), ("bash", "ls"))


class SchemaRegistryTests(unittest.TestCase):
    def test_every_schema_has_required_shape(self):
        self.assertTrue(ts.FUNCTION_TOOL_SCHEMAS)
        for entry in ts.FUNCTION_TOOL_SCHEMAS:
            self.assertEqual(entry["type"], "function")
            fn = entry["function"]
            self.assertIsInstance(fn["name"], str)
            self.assertTrue(fn["name"])
            self.assertIsInstance(fn.get("description", ""), str)
            self.assertIsInstance(fn["parameters"], dict)

    def test_schema_names_are_unique(self):
        names = [e["function"]["name"] for e in ts.FUNCTION_TOOL_SCHEMAS]
        self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
