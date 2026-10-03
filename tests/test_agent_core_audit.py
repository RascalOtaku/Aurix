"""Regression tests for bugs found in the agent-core audit (2026-10-03).

Covers:
- agent_loop._resolve_tool_blocks / _append_tool_results: native tool-call
  results misattributed to the wrong tool_call_id when a conversion fails.
- agent_loop._build_system_prompt: base-prompt cache ignoring the per-server
  MCP disabled-tools map (stale prompt vs fresh schemas).
- tool_implementations._next_run_for_task_edit: editing a task's trigger_type
  not recomputing/clearing next_run (task silently never fires, or misfires).
- tool_implementations._resolve_doc_for_delete: delete with an explicit but
  nonexistent document_id falling back to deleting the most recent document.
- tool_implementations._escape_like: LIKE wildcards in document list search.
"""
import json
import sys
import types
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import src.agent_loop as al  # noqa: E402
from src import tool_implementations as ti  # noqa: E402
from src import llm_core as lc  # noqa: E402
from src.tool_index import ToolIndex  # noqa: E402


# ---------------------------------------------------------------------------
# BUG 1: native tool-call results attached to the wrong tool_call_id
# ---------------------------------------------------------------------------

def _native_calls():
    return [
        {"id": "call_1", "name": "bash", "arguments": json.dumps({"command": "echo A"})},
        {"id": "call_2", "name": "definitely_not_a_real_tool_xyz", "arguments": "{}"},
        {"id": "call_3", "name": "bash", "arguments": json.dumps({"command": "echo C"})},
    ]


class TestResolveToolBlocks:
    def test_failed_conversions_are_tracked(self):
        blocks, used_native, failed = al._resolve_tool_blocks("resp", _native_calls(), 1)
        assert used_native is True
        assert failed == {1}
        assert len(blocks) == 2
        assert all(b.tool_type == "bash" for b in blocks)

    def test_all_ok_no_failures(self):
        calls = [c for i, c in enumerate(_native_calls()) if i != 1]
        blocks, used_native, failed = al._resolve_tool_blocks("resp", calls, 1)
        assert used_native is True
        assert failed == set()
        assert len(blocks) == 2


class TestAppendToolResults:
    def test_results_stay_aligned_with_call_ids(self):
        """The core BUG 1 regression: with calls [ok, fail, ok], the old code
        gave call_2 (failed) call_3's result and call_3 an empty string."""
        messages = []
        al._append_tool_results(
            messages, "resp", _native_calls(),
            ["result-A", "result-C"], ["result-A", "result-C"],
            True, 1, failed_native_idxs={1},
        )
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        assert len(tool_msgs) == 3
        assert tool_msgs[0]["tool_call_id"] == "call_1"
        assert tool_msgs[0]["content"] == "result-A"
        # The failed call gets an explicit error — never another call's result.
        assert tool_msgs[1]["tool_call_id"] == "call_2"
        assert "not executed" in tool_msgs[1]["content"]
        assert "result-C" not in tool_msgs[1]["content"]
        assert tool_msgs[2]["tool_call_id"] == "call_3"
        assert tool_msgs[2]["content"] == "result-C"

    def test_no_failures_behaves_as_before(self):
        calls = [c for i, c in enumerate(_native_calls()) if i != 1]
        messages = []
        al._append_tool_results(
            messages, "resp", calls,
            ["result-A", "result-C"], ["result-A", "result-C"],
            True, 1, failed_native_idxs=set(),
        )
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        assert [m["tool_call_id"] for m in tool_msgs] == ["call_1", "call_3"]
        assert [m["content"] for m in tool_msgs] == ["result-A", "result-C"]

    def test_assistant_message_still_lists_all_calls(self):
        messages = []
        al._append_tool_results(
            messages, "resp", _native_calls(), [], [], True, 1, failed_native_idxs={1},
        )
        assistant = messages[0]
        assert assistant["role"] == "assistant"
        assert [c["id"] for c in assistant["tool_calls"]] == ["call_1", "call_2", "call_3"]


# ---------------------------------------------------------------------------
# BUG 3: prompt cache ignores the MCP disabled-tools map
# ---------------------------------------------------------------------------

class _StubMcpMgr:
    def get_tool_descriptions_for_prompt(self, disabled_map):
        disabled = sorted(disabled_map.get("srv1", set()))
        return f"\n\n[MCP srv1 disabled: {','.join(disabled)}]"

    def get_all_openai_schemas(self, disabled_map):
        return []


class TestPromptCacheMcpMap:
    def test_changing_mcp_disabled_map_busts_cache(self):
        al._cached_base_prompt = None
        al._cached_base_prompt_key = None
        try:
            stub = _StubMcpMgr()
            msgs1, _ = al._build_system_prompt(
                [], "model", None, stub, mcp_disabled_map={"srv1": {"tool_a"}})
            sys_text1 = next(m["content"] for m in msgs1 if m["role"] == "system")
            assert "[MCP srv1 disabled: tool_a]" in sys_text1

            # Same request but tool_a re-enabled — the cached prompt must NOT
            # be reused; the description has to reflect the new map.
            msgs2, _ = al._build_system_prompt(
                [], "model", None, stub, mcp_disabled_map={"srv1": set()})
            sys_text2 = next(m["content"] for m in msgs2 if m["role"] == "system")
            assert "[MCP srv1 disabled: ]" in sys_text2
            assert "[MCP srv1 disabled: tool_a]" not in sys_text2
        finally:
            al._cached_base_prompt = None
            al._cached_base_prompt_key = None

    def test_identical_map_still_hits_cache(self):
        al._cached_base_prompt = None
        al._cached_base_prompt_key = None
        try:
            stub = _StubMcpMgr()
            al._build_system_prompt([], "model", None, stub,
                                    mcp_disabled_map={"srv1": {"tool_a"}})
            cached = al._cached_base_prompt
            assert cached is not None
            al._build_system_prompt([], "model", None, stub,
                                    mcp_disabled_map={"srv1": {"tool_a"}})
            # Cache hit: the exact same object is reused.
            assert al._cached_base_prompt is cached
        finally:
            al._cached_base_prompt = None
            al._cached_base_prompt_key = None


# ---------------------------------------------------------------------------
# Task edit: trigger_type changes must recompute/clear next_run
# ---------------------------------------------------------------------------

def _task(trigger_type="schedule", schedule="daily", scheduled_time="09:00",
          scheduled_day=None, next_run=None):
    return SimpleNamespace(
        trigger_type=trigger_type, schedule=schedule,
        scheduled_time=scheduled_time, scheduled_day=scheduled_day,
        next_run=next_run,
    )


class TestNextRunForTaskEdit:
    def test_event_to_schedule_recomputes_next_run(self):
        """The core regression: switching trigger_type event->schedule left
        next_run=None so the task silently never fired."""
        task = _task(trigger_type="schedule")  # setattr already applied by edit path
        result = ti._next_run_for_task_edit({"trigger_type": "schedule"}, task, False)
        assert result is not ti._NEXT_RUN_UNCHANGED
        assert result is not None

    def test_schedule_to_event_clears_next_run(self):
        """Switching away from schedule mode with a stale next_run misfires."""
        from datetime import datetime
        task = _task(trigger_type="event", next_run=datetime(2030, 1, 1))
        result = ti._next_run_for_task_edit({"trigger_type": "event"}, task, False)
        assert result is None

    def test_schedule_field_change_still_recomputes(self):
        """Pre-existing behavior: editing schedule fields recomputes."""
        task = _task(trigger_type="schedule", scheduled_time="10:00")
        result = ti._next_run_for_task_edit({"scheduled_time": "10:00"}, task, True)
        assert result is not ti._NEXT_RUN_UNCHANGED
        assert result is not None

    def test_unrelated_edit_leaves_next_run_alone(self):
        from datetime import datetime
        sentinel = datetime(2030, 5, 5, 9, 0)
        task = _task(next_run=sentinel)
        result = ti._next_run_for_task_edit({"name": "renamed"}, task, False)
        assert result is ti._NEXT_RUN_UNCHANGED


# ---------------------------------------------------------------------------
# Document delete: explicit-but-wrong id must error, not delete most recent
# ---------------------------------------------------------------------------

def _stub_core_database():
    class _FakeColumn:
        def __eq__(self, value):
            return ("eq", value)

        def desc(self):
            return ("desc",)

    class _FakeDocument:
        id = _FakeColumn()
        is_active = _FakeColumn()
        updated_at = _FakeColumn()

    fake_core = types.ModuleType("core")
    fake_db = types.ModuleType("core.database")
    fake_db.Document = _FakeDocument
    fake_core.database = fake_db
    return {"core": fake_core, "core.database": fake_db}


class _FakeQuery:
    def __init__(self, first_result):
        self._first_result = first_result

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def first(self):
        return self._first_result


def _fake_db(first_result):
    db = mock.MagicMock()
    db.query.return_value = _FakeQuery(first_result)
    return db


class TestResolveDocForDelete:
    def test_explicit_id_miss_is_an_error(self):
        """The core regression: an explicit but nonexistent id fell back to
        deleting the most recently updated document."""
        with mock.patch.dict(sys.modules, _stub_core_database()):
            doc, err = ti._resolve_doc_for_delete(_fake_db(None), "no-such-id", None)
        assert doc is None
        assert err is not None
        assert "no-such-id" in err

    def test_explicit_id_hit_returns_doc(self):
        sentinel = SimpleNamespace(id="abc", title="My Doc")
        with mock.patch.dict(sys.modules, _stub_core_database()):
            doc, err = ti._resolve_doc_for_delete(_fake_db(sentinel), "abc", None)
        assert doc is sentinel
        assert err is None

    def test_no_id_falls_back_to_most_recent(self):
        """Pre-existing behavior preserved: with no id at all, the most
        recently updated doc is still the target."""
        sentinel = SimpleNamespace(id="recent", title="Recent Doc")
        with mock.patch.dict(sys.modules, _stub_core_database()):
            doc, err = ti._resolve_doc_for_delete(_fake_db(sentinel), None, None)
        assert doc is sentinel
        assert err is None

    def test_no_id_no_docs_is_an_error(self):
        with mock.patch.dict(sys.modules, _stub_core_database()):
            doc, err = ti._resolve_doc_for_delete(_fake_db(None), None, None)
        assert doc is None
        assert err == "No document to delete"


# ---------------------------------------------------------------------------
# LIKE wildcard escaping in document search
# ---------------------------------------------------------------------------

class TestEscapeLike:
    def test_wildcards_escaped(self):
        assert ti._escape_like("100%_sure") == "100\\%\\_sure"

    def test_backslash_escaped_first(self):
        assert ti._escape_like("a\\b") == "a\\\\b"

    def test_plain_string_unchanged(self):
        assert ti._escape_like("hello world") == "hello world"


# ---------------------------------------------------------------------------
# B1: MCP tool reindex guard (tool_index.py)
# ---------------------------------------------------------------------------

def _bare_tool_index():
    idx = ToolIndex.__new__(ToolIndex)
    idx._mcp_generation = -1
    idx._collection = mock.MagicMock()
    idx._collection.get.return_value = {"ids": []}
    idx._embed = mock.MagicMock(return_value=[[0.1] * 8])
    return idx


class TestIndexMcpTools:
    def test_reindexes_when_mcp_tools_change(self):
        """The core B1 regression: the old _generation guard never changed,
        so reindexing silently stopped after the first call and RAG kept
        surfacing stale tools."""
        idx = _bare_tool_index()
        mcp_mgr = mock.MagicMock()
        mcp_mgr.get_tool_descriptions_for_prompt.return_value = "**srv:**\n- tool_a: does A"

        idx.index_mcp_tools(mcp_mgr, {})
        assert idx._collection.upsert.called  # first index works

        # Same tools again -> skip the expensive reindex.
        idx._collection.upsert.reset_mock()
        idx.index_mcp_tools(mcp_mgr, {})
        assert not idx._collection.upsert.called

        # Tools changed -> must reindex (old code never did).
        mcp_mgr.get_tool_descriptions_for_prompt.return_value = (
            "**srv:**\n- tool_a: does A\n- tool_b: does B")
        idx.index_mcp_tools(mcp_mgr, {})
        assert idx._collection.upsert.called

    def test_disabled_map_change_reindexes(self):
        idx = _bare_tool_index()
        mcp_mgr = mock.MagicMock()
        # Descriptions reflect the disabled map (disabled tools excluded).
        mcp_mgr.get_tool_descriptions_for_prompt.side_effect = (
            lambda dm: "**srv:**\n- tool_a: does A"
            if dm.get("srv") else "**srv:**\n- tool_a: does A\n- tool_b: does B")

        idx.index_mcp_tools(mcp_mgr, {})
        assert idx._collection.upsert.called
        idx._collection.upsert.reset_mock()
        idx.index_mcp_tools(mcp_mgr, {"srv": {"tool_b"}})
        assert idx._collection.upsert.called


# ---------------------------------------------------------------------------
# B2: _sanitize_llm_messages must keep assistant tool-call turns
# ---------------------------------------------------------------------------

class TestSanitizeLlmMessages:
    def test_keeps_content_none_tool_calls(self):
        """The core B2 regression: the standard OpenAI shape for a pure tool
        call (content=None) was dropped entirely, orphaning tool messages."""
        msgs = [
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "bash", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ]
        out = lc._sanitize_llm_messages(msgs)
        assert len(out) == 2
        assert out[0]["role"] == "assistant"
        assert out[0]["tool_calls"][0]["id"] == "c1"
        assert out[1]["role"] == "tool"

    def test_still_drops_empty_messages(self):
        msgs = [
            {"role": "assistant", "content": None},  # no tool_calls -> dropped
            {"role": "user", "content": "hi"},
        ]
        out = lc._sanitize_llm_messages(msgs)
        assert len(out) == 1
        assert out[0]["role"] == "user"

    def test_strips_disallowed_keys(self):
        msgs = [{"role": "user", "content": "hi", "odysseus_meta": 1}]
        out = lc._sanitize_llm_messages(msgs)
        assert out == [{"role": "user", "content": "hi"}]


# ---------------------------------------------------------------------------
# B4: header normalization shared by sync/async/stream paths
# ---------------------------------------------------------------------------

class TestNormalizeHeaders:
    def test_double_encoded_json_string(self):
        assert lc._normalize_headers('{"Authorization": "Bearer x"}') == {"Authorization": "Bearer x"}

    def test_junk_string_becomes_none(self):
        assert lc._normalize_headers("not json at all") is None

    def test_dict_passes_through(self):
        h = {"Authorization": "Bearer x"}
        assert lc._normalize_headers(h) == h

    def test_none_passes_through(self):
        assert lc._normalize_headers(None) is None

    def test_non_dict_non_string_becomes_none(self):
        assert lc._normalize_headers(["a", "b"]) is None


# ---------------------------------------------------------------------------
# B6: bare local Ollama URL detection
# ---------------------------------------------------------------------------

class TestOllamaUrlDetection:
    def test_bare_localhost_is_ollama(self):
        assert lc._detect_provider("http://localhost:11434") == "ollama"
        assert lc._detect_provider("http://localhost:11434/") == "ollama"
        assert lc._detect_provider("http://127.0.0.1:11434") == "ollama"

    def test_bare_url_normalizes_to_api_chat(self):
        assert lc._normalize_ollama_url("http://localhost:11434") == "http://localhost:11434/api/chat"
        assert lc._normalize_ollama_url("http://localhost:11434/") == "http://localhost:11434/api/chat"

    def test_existing_paths_unchanged(self):
        assert lc._detect_provider("http://localhost:11434/api") == "ollama"
        assert lc._normalize_ollama_url("http://localhost:11434/api/chat") == "http://localhost:11434/api/chat"
        assert lc._detect_provider("https://api.openai.com/v1") == "openai"
        assert lc._is_ollama_native_url("https://ollama.com") is True


# ---------------------------------------------------------------------------
# B7: model-activity key must match between writer and reader
# ---------------------------------------------------------------------------

class TestModelActivityKey:
    def test_raw_url_lookup_hits(self):
        """The core B7 regression: activity was recorded under the normalized
        URL but builtin_actions checks with the raw configured URL -> the
        quiet-window deferral never fired."""
        lc._model_activity.clear()
        try:
            # Simulate what llm_call now does: record under the raw url.
            lc.note_model_activity("https://api.anthropic.com", "claude-x")
            recent = lc.seconds_since_model_activity("https://api.anthropic.com", "claude-x")
            assert recent is not None
            assert recent >= 0
        finally:
            lc._model_activity.clear()

    def test_different_models_tracked_separately(self):
        lc._model_activity.clear()
        try:
            lc.note_model_activity("https://api.anthropic.com", "model-a")
            assert lc.seconds_since_model_activity("https://api.anthropic.com", "model-b") is None
        finally:
            lc._model_activity.clear()
