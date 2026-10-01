"""Agent manager for orchestrating multiple agents."""

import asyncio
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass
from datetime import datetime
import json

from .base import Agent, AgentConfig
from .types import AgentRole, AgentStatus


@dataclass
class TaskAssignment:
    """Task assignment to agent."""
    task_id: str
    agent_id: str
    description: str
    priority: int = 0
    dependencies: List[str] = None
    status: str = "pending"
    result: Any = None


class AgentManager:
    """Manages fleet of agents (inspired by munder-difflin, ECC patterns)."""

    def __init__(self, name: str = "Aurix"):
        self.name = name
        self.agents: Dict[str, Agent] = {}
        self.tasks: Dict[str, TaskAssignment] = {}
        self.workflow_history: List[Dict[str, Any]] = []
        self._running = False

    def register_agent(self, agent: Agent) -> str:
        """Register new agent in fleet."""
        self.agents[agent.id] = agent
        return agent.id

    def get_agent(self, agent_id: str) -> Optional[Agent]:
        """Retrieve agent by ID."""
        return self.agents.get(agent_id)

    def get_agents_by_role(self, role: AgentRole) -> List[Agent]:
        """Get all agents with specific role."""
        return [a for a in self.agents.values() if a.config.role == role]

    def get_agents_by_capability(self, capability: str) -> List[Agent]:
        """Get agents with specific capability."""
        result = []
        for agent in self.agents.values():
            if any(c.name == capability for c in agent.get_capabilities()):
                result.append(agent)
        return result

    async def dispatch_task(
        self,
        task_description: str,
        agent_id: Optional[str] = None,
        priority: int = 0,
        dependencies: Optional[List[str]] = None,
    ) -> str:
        """Dispatch task to agent(s)."""
        import uuid
        task_id = str(uuid.uuid4())

        # Auto-select agent if not specified
        if agent_id is None:
            agent_id = await self._select_best_agent(task_description)

        assignment = TaskAssignment(
            task_id=task_id,
            agent_id=agent_id,
            description=task_description,
            priority=priority,
            dependencies=dependencies or [],
        )
        self.tasks[task_id] = assignment

        # Execute task
        agent = self.get_agent(agent_id)
        if agent:
            try:
                result = await agent.think(task_description)
                assignment.status = "completed"
                assignment.result = result
            except Exception as e:
                assignment.status = "failed"
                assignment.result = str(e)

        return task_id

    async def _select_best_agent(self, task_description: str) -> str:
        """Select best agent for task (can be improved with ML)."""
        # Prefer orchestrator role by default
        orchestrators = self.get_agents_by_role(AgentRole.ORCHESTRATOR)
        if orchestrators:
            return orchestrators[0].id

        # Fall back to first available agent
        if self.agents:
            return next(iter(self.agents.values())).id

        raise RuntimeError("No agents registered")

    async def collaborate(
        self,
        task_description: str,
        agent_roles: List[AgentRole],
    ) -> Dict[str, Any]:
        """Coordinate multi-agent collaboration on task."""
        results = {}
        tasks = []

        for role in agent_roles:
            agents = self.get_agents_by_role(role)
            if agents:
                task = self.dispatch_task(
                    f"{task_description} (role: {role.value})",
                    agent_id=agents[0].id,
                )
                tasks.append(task)

        # Wait for all tasks
        task_ids = await asyncio.gather(*tasks)
        for task_id in task_ids:
            if task_id in self.tasks:
                assignment = self.tasks[task_id]
                results[assignment.agent_id] = assignment.result

        return results

    def get_fleet_status(self) -> Dict[str, Any]:
        """Get status of all agents."""
        return {
            "manager": self.name,
            "agents": {
                agent_id: agent.get_state()
                for agent_id, agent in self.agents.items()
            },
            "pending_tasks": len([t for t in self.tasks.values() if t.status == "pending"]),
            "completed_tasks": len([t for t in self.tasks.values() if t.status == "completed"]),
        }

    def export_workflow(self) -> str:
        """Export workflow as JSON."""
        return json.dumps({
            "manager": self.name,
            "agents": len(self.agents),
            "tasks_total": len(self.tasks),
            "history": self.workflow_history[:50],  # Last 50 events
        }, indent=2, default=str)
