"""Tests for multi-agent framework."""

import asyncio
import pytest
from core.agents import Agent, AgentConfig, AgentManager
from core.agents.types import AgentRole, AgentStatus


class TestAgent(Agent):
    """Test agent implementation."""

    async def _process_task(self, task: str) -> str:
        return f"Test: {task}"

    async def _execute_action(self, action: str, params):
        return {"result": f"Executed {action}"}


def test_agent_creation():
    config = AgentConfig(
        name="TestAgent",
        role=AgentRole.SPECIALIST,
    )
    agent = TestAgent(config)
    assert agent.config.name == "TestAgent"
    assert agent.status == AgentStatus.IDLE


@pytest.mark.asyncio
async def test_agent_think():
    config = AgentConfig(
        name="TestAgent",
        role=AgentRole.SPECIALIST,
    )
    agent = TestAgent(config)
    result = await agent.think("test task")
    assert result == "Test: test task"
    assert agent.status == AgentStatus.COMPLETE


def test_agent_manager():
    manager = AgentManager()
    config = AgentConfig(
        name="TestAgent",
        role=AgentRole.ORCHESTRATOR,
    )
    agent = TestAgent(config)
    agent_id = manager.register_agent(agent)
    assert agent_id in manager.agents


def test_agent_role_filtering():
    manager = AgentManager()
    
    orch_config = AgentConfig(
        name="Orchestrator",
        role=AgentRole.ORCHESTRATOR,
    )
    spec_config = AgentConfig(
        name="Specialist",
        role=AgentRole.SPECIALIST,
    )
    
    orch = TestAgent(orch_config)
    spec = TestAgent(spec_config)
    
    manager.register_agent(orch)
    manager.register_agent(spec)
    
    orchestrators = manager.get_agents_by_role(AgentRole.ORCHESTRATOR)
    assert len(orchestrators) == 1
    assert orchestrators[0].config.name == "Orchestrator"
