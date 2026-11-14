"""Agents wired into chat: routing, gated execution, providers, web search, chat handler."""

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.agents import AgentConfig, AgentManager  # noqa: E402
from core.agents import execution  # noqa: E402
from core.agents import implementations as impl  # noqa: E402
from core.agents.context import agent_context, current_session_id  # noqa: E402
from core.agents.routing import (  # noqa: E402
    build_default_fleet,
    classify_ask,
    parse_agent_request,
)
from core.agents.types import AgentRole  # noqa: E402
from core.skills import SkillRegistry  # noqa: E402
from core.skills.builtin_skills import WebSearchSkill, register_builtin_skills  # noqa: E402
from core.skills.types import SkillCategory, SkillMetadata  # noqa: E402
from src import approval_gate  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("message", [
    "what's for dinner",
    "can you research this for me",          # no explicit ask for the agents
    "email me at bob@agent.com",
    "my travel agent: booked",               # "agent:" only counts at the start
    "",
])
def test_plain_chat_is_not_routed(message):
    assert parse_agent_request(message) is None


@pytest.mark.parametrize("message,role", [
    ("@agent research the latest Ollama release notes", AgentRole.RESEARCHER),
    ("agent: review this plan for holes", AgentRole.REVIEWER),
    ("@agent fix the bug in this python function", AgentRole.SPECIALIST),
    ("@agent delete the old logs in /var/tmp", AgentRole.EXECUTOR),
    ("@agent plan my week", AgentRole.ORCHESTRATOR),
    ("multi-agent: look into the backups and remove the stale ones", AgentRole.EXECUTOR),
])
def test_keyword_routing(message, role):
    req = parse_agent_request(message)
    assert req is not None and req.role == role and not req.explicit_role


def test_explicit_role_mention_wins_and_is_stripped():
    req = parse_agent_request("@reviewer delete step looks risky?")
    assert req.role == AgentRole.REVIEWER and req.explicit_role
    assert "@reviewer" not in req.task and req.task.startswith("delete step")


def test_executor_beats_research_when_an_ask_mixes_them():
    assert classify_ask("look into the disk and delete what's big") == AgentRole.EXECUTOR


def test_default_fleet_has_one_agent_per_role_and_is_idempotent():
    manager = AgentManager()
    ids = build_default_fleet(manager)
    assert len(ids) == 5
    assert {a.config.role for a in manager.agents.values()} == set(AgentRole) - {AgentRole.COMMUNICATOR}
    assert build_default_fleet(manager) == ids


def test_get_agent_for_role_falls_back_to_orchestrator():
    manager = AgentManager()
    orch = impl.ChatLLMAgent(AgentConfig(name="P", role=AgentRole.ORCHESTRATOR))
    manager.register_agent(orch)
    assert manager.get_agent_for_role(AgentRole.REVIEWER) is orch
    assert AgentManager().get_agent_for_role(AgentRole.REVIEWER) is None


def test_dispatch_to_missing_agent_fails_and_is_recorded():
    manager = AgentManager()
    task_id = run(manager.dispatch_task("x", agent_id="nope"))
    assert manager.tasks[task_id].status == "failed"
    assert manager.workflow_history[-1]["status"] == "failed"


# ---------------------------------------------------------------------------
# Gated execution: no more fake "executed"
# ---------------------------------------------------------------------------

def test_force_gate_gates_every_session_even_when_mode_is_off(monkeypatch):
    monkeypatch.setenv("APPROVAL_GATE_MODE", "off")
    assert not approval_gate.requires_approval("bash", "web-session")
    with approval_gate.force_gate():
        assert approval_gate.requires_approval("bash", "web-session")
        assert approval_gate.requires_approval("send_email", None)
        assert not approval_gate.requires_approval("web_search", None)  # read-only stays ungated
    assert not approval_gate.gate_forced()


def _fake_executor(desc, result, seen=None):
    async def _exec(block, session_id=None, owner=None):
        if seen is not None:
            seen.append({"block": block, "session": session_id, "owner": owner,
                         "forced": approval_gate.gate_forced()})
        return desc, result
    return _exec


def test_gated_action_runs_inside_the_forced_gate():
    seen = []
    out = run(execution.run_gated_action(
        "bash", {"command": "ls /tmp"}, session_id="s1", owner="rascal",
        executor=_fake_executor("bash: ls /tmp", {"output": "a\nb", "exit_code": 0}, seen)))
    assert out["status"] == "executed"
    assert seen[0]["forced"] is True and seen[0]["session"] == "s1" and seen[0]["owner"] == "rascal"
    assert seen[0]["block"].tool_type == "bash" and seen[0]["block"].content == "ls /tmp"
    assert not approval_gate.gate_forced()


@pytest.mark.parametrize("desc,result,status", [
    ("bash: DENIED", {"error": "Denied by owner", "exit_code": 1}, "denied"),
    ("bash: BLOCKED", {"error": "disabled", "exit_code": 1}, "denied"),
    ("bash: false", {"output": "", "exit_code": 2}, "failed"),
    ("write_file: x", {"error": "no such dir"}, "failed"),
])
def test_gated_action_reports_what_really_happened(desc, result, status):
    out = run(execution.run_gated_action("bash", {"content": "x"},
                                         executor=_fake_executor(desc, result)))
    assert out["status"] == status


def test_gated_action_error_when_executor_raises():
    async def boom(block, session_id=None, owner=None):
        raise RuntimeError("gate offline")
    out = run(execution.run_gated_action("bash", {"content": "x"}, executor=boom))
    assert out["status"] == "error" and "gate offline" in out["result"]["error"]


def test_json_tools_get_json_content():
    assert json.loads(execution.action_content("send_email", {"to": "a@b.c"})) == {"to": "a@b.c"}
    assert execution.action_content("python", {"code": "print(1)"}) == "print(1)"


def test_real_execute_path_goes_through_the_gate():
    """execute_tool_block itself is called, and the approval gate's enforce() sees a forced gate."""
    seen = {}

    async def fake_enforce(tool, content, session_id=None, owner=None):
        seen["forced"] = approval_gate.gate_forced()
        seen["gated"] = approval_gate.requires_approval(tool, session_id)
        return "Denied: owner said no."

    with mock.patch.object(approval_gate, "enforce", fake_enforce):
        out = run(execution.run_gated_action("bash", {"command": "rm -rf /tmp/x"}, session_id="web"))
    assert seen == {"forced": True, "gated": True}
    assert out["status"] == "denied"


@pytest.mark.parametrize("cls", [impl.LocalLLMAgent, impl.ClaudeAgent, impl.OpenAIAgent, impl.ChatLLMAgent])
def test_every_agent_executes_through_the_gate(cls, monkeypatch):
    calls = []

    async def fake_gated(action, params, session_id=None, owner=None):
        calls.append((action, session_id, owner))
        return {"action": action, "status": "denied"}

    monkeypatch.setattr(impl, "run_gated_action", fake_gated)
    agent = cls(AgentConfig(name="A", role=AgentRole.EXECUTOR))
    with agent_context(session_id="s9", owner="rascal"):
        out = run(agent.execute("bash", {"command": "ls"}))
    assert out["status"] == "denied"
    assert calls == [("bash", "s9", "rascal")]


# ---------------------------------------------------------------------------
# Thinking
# ---------------------------------------------------------------------------

def test_chat_agent_thinks_with_the_bound_session_model():
    seen = []

    async def session_model(messages):
        seen.append(messages)
        return "  answer  "

    agent = impl.ChatLLMAgent(AgentConfig(name="R", role=AgentRole.RESEARCHER, system_prompt="SYS"))
    with agent_context(llm=session_model):
        assert run(agent.think("q")) == "answer"
    assert seen[0][0] == {"role": "system", "content": "SYS"}
    assert seen[0][1] == {"role": "user", "content": "q"}


def test_chat_agent_without_a_model_errors_instead_of_pretending():
    agent = impl.ChatLLMAgent(AgentConfig(name="R", role=AgentRole.RESEARCHER))
    with pytest.raises(RuntimeError):
        run(agent.think("q"))


class _FakeClaudeClient:
    def __init__(self, stop_reason="end_turn"):
        self.kwargs = None
        self.stop_reason = stop_reason
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(stop_reason=self.stop_reason, content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text="hi there"),
        ])


def test_claude_agent_uses_async_client_and_current_model(monkeypatch):
    monkeypatch.delenv("AURIX_CLAUDE_MODEL", raising=False)
    client = _FakeClaudeClient()
    agent = impl.ClaudeAgent(AgentConfig(name="C", role=AgentRole.SPECIALIST), client=client)
    assert agent.model == impl.DEFAULT_CLAUDE_MODEL  # not the generic "gpt-4" config default
    assert run(agent.think("hello")) == "hi there"
    assert client.kwargs["model"] == impl.DEFAULT_CLAUDE_MODEL
    assert "temperature" not in client.kwargs
    assert client.kwargs["fallbacks"] == "default"


def test_claude_agent_model_override_and_refusal(monkeypatch):
    monkeypatch.setenv("AURIX_CLAUDE_MODEL", "claude-sonnet-5-5")
    agent = impl.ClaudeAgent(AgentConfig(name="C", role=AgentRole.SPECIALIST),
                             client=_FakeClaudeClient(stop_reason="refusal"))
    assert agent.model == "claude-sonnet-5-5"
    with pytest.raises(RuntimeError):
        run(agent.think("hello"))


def test_openai_agent_sends_system_prompt_as_a_message():
    captured = {}

    async def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    agent = impl.OpenAIAgent(AgentConfig(name="O", role=AgentRole.SPECIALIST, system_prompt="S"),
                             client=client)
    assert run(agent.think("t")) == "ok"
    assert "system_prompt" not in captured
    assert captured["messages"][0] == {"role": "system", "content": "S"}


# ---------------------------------------------------------------------------
# Web search skill
# ---------------------------------------------------------------------------

def _web_skill(fn):
    return WebSearchSkill(SkillMetadata(name="web_search", category=SkillCategory.RESEARCH,
                                        description="d"), search_fn=fn)


def test_web_search_skill_returns_real_results():
    skill = _web_skill(lambda q, n: (f"results for {q}", [{"url": "https://x.example", "title": "X"}]))
    out = run(skill.run("a", {"query": "ollama"}))
    assert out["summary"] == "results for ollama"
    assert out["results"] == [{"url": "https://x.example", "title": "X"}]


def test_web_search_skill_reports_failures():
    def broken(q, n):
        raise ConnectionError("searxng down")
    out = run(_web_skill(broken).run("a", {"query": "q"}))
    assert out["results"] == [] and "searxng down" in out["error"]
    assert run(_web_skill(broken).run("a", {"query": " "}))["error"] == "empty query"


# ---------------------------------------------------------------------------
# Chat handler
# ---------------------------------------------------------------------------

def _handler(search=None):
    from src.chat_handler import ChatHandler

    registry = SkillRegistry()
    register_builtin_skills(registry)
    if search is not None:
        registry.unregister("web_search")
        registry.register(_web_skill(search))
    return ChatHandler(None, None, None, None, SimpleNamespace(presets={}), None,
                       agent_manager=AgentManager(), agent_skill_registry=registry)


def _model(reply, seen=None):
    async def _llm(messages):
        if seen is not None:
            seen.append(messages)
        return reply
    return _llm


def test_handler_ignores_plain_chat():
    assert run(_handler().maybe_route_agent_task("hello there", llm=_model("x"))) is None


def test_handler_routes_research_with_web_results_marked_untrusted():
    seen = []
    h = _handler(search=lambda q, n: ("Ollama 0.9 shipped", []))
    reply = run(h.maybe_route_agent_task("@agent research the latest ollama release",
                                         session=SimpleNamespace(id="s1"), llm=_model("It's 0.9.", seen)))
    assert reply.startswith("**Researcher** (researcher agent)") and "It's 0.9." in reply
    user_msg = seen[0][1]["content"]
    assert "Ollama 0.9 shipped" in user_msg and "UNTRUSTED_SOURCE_DATA" in user_msg
    assert len(h.agent_manager.agents) == 5  # fleet built on first use


def test_handler_holds_executor_steps_by_default(monkeypatch):
    monkeypatch.delenv("AURIX_AGENT_EXECUTE", raising=False)
    ran = []

    async def fake_gated(action, params, session_id=None, owner=None):
        ran.append(action)
        return {"status": "executed", "result": {}}

    monkeypatch.setattr(impl, "run_gated_action", fake_gated)
    plan = "Clear old logs:\n```bash\nrm /var/tmp/old.log\n```"
    reply = run(_handler().maybe_route_agent_task("@agent delete the old logs", llm=_model(plan)))
    assert "**Executor**" in reply and "Nothing has run. 1 step(s)" in reply
    assert ran == []


def test_handler_runs_executor_steps_through_the_gate_when_enabled(monkeypatch):
    monkeypatch.setenv("AURIX_AGENT_EXECUTE", "1")
    calls = []

    async def fake_gated(action, params, session_id=None, owner=None):
        calls.append((action, params["content"], session_id, owner))
        status = "executed" if len(calls) == 1 else "denied"
        return {"status": status, "result": {"output": "done"} if status == "executed"
                else {"error": "Denied: owner said no"}}

    monkeypatch.setattr(impl, "run_gated_action", fake_gated)
    plan = "```bash\ndf -h /var\n```\n```bash\nrm /var/tmp/old.log\n```\n```bash\nreboot\n```"
    reply = run(_handler().maybe_route_agent_task(
        "@executor tidy /var/tmp", session=SimpleNamespace(id="s2"), owner="rascal", llm=_model(plan)))
    assert calls[0] == ("bash", "df -h /var", "s2", "rascal")
    assert len(calls) == 2  # stopped after the denial; reboot never asked
    assert "- bash: executed (done)" in reply and "- bash: denied" in reply
    assert "later steps were not run" in reply


def test_handler_reports_agent_failure_plainly():
    async def broken(messages):
        raise TimeoutError("model asleep")
    reply = run(_handler().maybe_route_agent_task("@agent plan my week", llm=broken))
    assert "couldn't finish this" in reply and "model asleep" in reply and "Nothing was run" in reply


def test_agent_sse_matches_what_chat_clients_parse():
    from src.agent_chat import agent_sse

    async def collect():
        return [c async for c in agent_sse("hello", saved_id="m1")]

    chunks = run(collect())
    assert json.loads(chunks[0][6:]) == {"delta": "hello"}
    assert json.loads(chunks[1][6:]) == {"type": "message_saved", "id": "m1"}
    assert chunks[-1] == "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# /api/chat end to end (route wiring), with the DB and model faked
# ---------------------------------------------------------------------------

def test_api_chat_routes_agent_asks_and_saves_the_turn():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routes import chat_routes

    saved = []
    sess = SimpleNamespace(id="s1", endpoint_url="http://model/v1", model="m", headers={},
                           add_message=lambda m: saved.append((m.role, m.content)))
    session_manager = SimpleNamespace(get_session=lambda sid: sess, save_sessions=lambda: None)
    handler = _handler()

    async def fake_llm(url, model, messages, **kw):
        assert (url, model) == ("http://model/v1", "m")
        return "Step 1: breathe."

    async def main_chat_must_not_run(*a, **kw):
        raise AssertionError("plain chat path ran")

    async def no_memory(sess, message):
        return None
    handler.handle_memory_command = no_memory

    patches = [
        mock.patch.object(chat_routes, "_verify_session_owner", lambda r, s: None),
        mock.patch.object(chat_routes, "_clear_orphaned_session_endpoint", lambda s: False),
        mock.patch.object(chat_routes, "_enforce_chat_privileges", lambda r, s: None),
        mock.patch.object(chat_routes, "get_current_user", lambda r: "rascal"),
        # Other test modules may stub core.models; keep this test independent of that.
        mock.patch.object(chat_routes, "ChatMessage",
                          lambda role, content, metadata=None: SimpleNamespace(role=role, content=content)),
        mock.patch.object(chat_routes, "llm_call_async", main_chat_must_not_run),
        mock.patch("src.llm_core.llm_call_async", fake_llm),
        mock.patch("core.database.update_session_last_accessed", lambda sid: None),
    ]
    for p in patches:
        p.start()
    try:
        app = FastAPI()
        app.include_router(chat_routes.setup_chat_routes(session_manager, handler, None, None, None, None))
        res = TestClient(app).post("/api/chat", json={"message": "@agent plan my week", "session": "s1"})
    finally:
        for p in reversed(patches):
            p.stop()
    assert res.status_code == 200, res.text
    assert res.json()["response"].startswith("**Planner** (orchestrator agent)")
    assert saved[0] == ("user", "@agent plan my week") and "Step 1: breathe." in saved[1][1]
