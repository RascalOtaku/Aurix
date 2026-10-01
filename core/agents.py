"""
Multi-agent orchestration framework for Aurix.

Supports hierarchical agent coordination, skill delegation, and memory-aware
task execution. Agents communicate via a message bus and maintain persistent
state across sessions.
"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class AgentRole(Enum):
    """Agent capability classification."""
    ORCHESTRATOR = "orchestrator"      # Coordinates other agents
    RESEARCHER = "researcher"           # Gathers and analyzes information
    EXECUTOR = "executor"               # Performs tasks (shell, file, API)
    PLANNER = "planner"                 # Decomposes complex tasks
    REVIEWER = "reviewer"               # QA and validation
    MEMORY = "memory"                   # Manages persistent context
    SKILL = "skill"                     # Specialized domain knowledge


class TaskStatus(Enum):
    """Task lifecycle states."""
    PENDING = "pending"
    DISPATCHED = "dispatched"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskContext:
    """Execution context passed between agents."""
    task_id: str
    description: str
    priority: int = 1
    owner: str = "system"
    max_rounds: int = 10
    timeout_seconds: int = 300
    
    # Execution state
    status: TaskStatus = TaskStatus.PENDING
    rounds_completed: int = 0
    messages: List[Dict[str, Any]] = field(default_factory=list)
    memory_refs: Set[str] = field(default_factory=set)
    skill_refs: Set[str] = field(default_factory=set)
    
    # Results
    result: Optional[str] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        data = asdict(self)
        data["status"] = self.status.value
        data["memory_refs"] = list(self.memory_refs)
        data["skill_refs"] = list(self.skill_refs)
        return data


@dataclass
class AgentCapability:
    """Describes what an agent can do."""
    name: str
    role: AgentRole
    description: str
    supports_parallel: bool = True
    skill_ids: List[str] = field(default_factory=list)
    max_concurrent_tasks: int = 3


class Agent:
    """Base agent that can be composed into multi-agent workflows."""

    def __init__(
        self,
        name: str,
        role: AgentRole,
        description: str = "",
        llm_endpoint: Optional[str] = None,
        llm_model: Optional[str] = None,
    ):
        self.id = str(uuid.uuid4())
        self.name = name
        self.role = role
        self.description = description
        self.llm_endpoint = llm_endpoint
        self.llm_model = llm_model
        
        self.capability = AgentCapability(
            name=name,
            role=role,
            description=description,
        )
        
        self.active_tasks: Dict[str, TaskContext] = {}
        self.completed_tasks: List[TaskContext] = []
        self.message_queue: asyncio.Queue = asyncio.Queue()
        
        logger.info(f"Agent initialized: {name} ({role.value})")

    async def execute_task(self, ctx: TaskContext) -> TaskContext:
        """Override to implement agent-specific logic."""
        ctx.status = TaskStatus.COMPLETED
        ctx.result = f"{self.name} executed task (no-op)"
        return ctx

    async def should_delegate(self, ctx: TaskContext) -> bool:
        """Return True if this agent should pass the task to another agent."""
        return False

    async def handle_message(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Process inter-agent messages."""
        return None


class AgentManager:
    """Orchestrates multi-agent workflows."""

    def __init__(self, name: str = "Aurix"):
        self.name = name
        self.agents: Dict[str, Agent] = {}
        self.tasks: Dict[str, TaskContext] = {}
        self.message_bus: asyncio.Queue = asyncio.Queue()
        self.running = False
        
        logger.info(f"AgentManager '{name}' initialized")

    def register_agent(self, agent: Agent) -> None:
        """Register an agent with the orchestrator."""
        self.agents[agent.id] = agent
        logger.info(f"Registered agent: {agent.name} ({agent.role.value})")

    async def dispatch_task(
        self,
        task_description: str,
        priority: int = 1,
        owner: str = "user",
        max_rounds: int = 10,
    ) -> str:
        """Create and dispatch a task to the agent network."""
        task_id = str(uuid.uuid4())
        ctx = TaskContext(
            task_id=task_id,
            description=task_description,
            priority=priority,
            owner=owner,
            max_rounds=max_rounds,
            status=TaskStatus.PENDING,
        )
        
        self.tasks[task_id] = ctx
        logger.info(f"Task dispatched: {task_id} ({task_description[:50]}...)")
        
        # Run execution in background
        asyncio.create_task(self._execute_task_loop(ctx))
        return task_id

    async def _execute_task_loop(self, ctx: TaskContext) -> None:
        """Main task execution loop with round management."""
        ctx.status = TaskStatus.RUNNING
        
        try:
            while ctx.rounds_completed < ctx.max_rounds and ctx.status == TaskStatus.RUNNING:
                ctx.rounds_completed += 1
                logger.info(f"Task {ctx.task_id} round {ctx.rounds_completed}/{ctx.max_rounds}")
                
                # Find best agent for this task
                agent = self._select_agent(ctx)
                if not agent:
                    ctx.error = "No suitable agent found"
                    ctx.status = TaskStatus.FAILED
                    break
                
                # Execute task
                try:
                    ctx = await agent.execute_task(ctx)
                except Exception as e:
                    logger.error(f"Agent {agent.name} failed: {e}")
                    ctx.error = str(e)
                    ctx.status = TaskStatus.FAILED
                    break
                
                # Check if task is complete
                if ctx.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                    break
                
                # Brief pause between rounds
                await asyncio.sleep(0.1)
            
            if ctx.status == TaskStatus.RUNNING:
                ctx.status = TaskStatus.COMPLETED
                
        except Exception as e:
            logger.error(f"Task loop error: {e}")
            ctx.error = str(e)
            ctx.status = TaskStatus.FAILED
        finally:
            ctx.updated_at = time.time()
            logger.info(f"Task {ctx.task_id} finished: {ctx.status.value}")

    def _select_agent(self, ctx: TaskContext) -> Optional[Agent]:
        """Select the best agent for a task."""
        # Simple heuristic: prefer orchestrators, then executors
        available = [
            a for a in self.agents.values()
            if len(a.active_tasks) < a.capability.max_concurrent_tasks
        ]
        
        if not available:
            return None
        
        # Sort by role priority
        role_priority = {
            AgentRole.ORCHESTRATOR: 0,
            AgentRole.PLANNER: 1,
            AgentRole.EXECUTOR: 2,
            AgentRole.RESEARCHER: 3,
            AgentRole.REVIEWER: 4,
            AgentRole.MEMORY: 5,
            AgentRole.SKILL: 6,
        }
        
        return sorted(available, key=lambda a: role_priority.get(a.role, 999))[0]

    def get_task_status(self, task_id: str) -> Optional[TaskContext]:
        """Retrieve task context by ID."""
        return self.tasks.get(task_id)

    def list_agents(self) -> List[Dict[str, Any]]:
        """List all registered agents with their capabilities."""
        return [
            {
                "id": a.id,
                "name": a.name,
                "role": a.role.value,
                "description": a.description,
                "active_tasks": len(a.active_tasks),
                "completed_tasks": len(a.completed_tasks),
            }
            for a in self.agents.values()
        ]

    def list_tasks(self, owner: Optional[str] = None, status: Optional[str] = None) -> List[Dict[str, Any]]:
        """List tasks with optional filtering."""
        tasks = self.tasks.values()
        
        if owner:
            tasks = [t for t in tasks if t.owner == owner]
        if status:
            tasks = [t for t in tasks if t.status.value == status]
        
        return sorted(
            [t.to_dict() for t in tasks],
            key=lambda x: x["created_at"],
            reverse=True,
        )
