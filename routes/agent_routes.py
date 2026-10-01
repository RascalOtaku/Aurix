"""Integration of multi-agent framework with FastAPI routes."""

from fastapi import APIRouter, Request, HTTPException
from typing import Dict, List, Any
import logging

from core.agents import Agent, AgentConfig, AgentManager
from core.agents.types import AgentRole, AgentStatus, AgentCapability
from core.skills import SkillRegistry
from core.memory import MemoryStore
from core.memory.types import MemoryType

logger = logging.getLogger(__name__)

# Global agent manager instance
_agent_manager: AgentManager = None
_skill_registry: SkillRegistry = None
_memory_store: MemoryStore = None


def setup_agent_routes(app_state) -> APIRouter:
    """Setup multi-agent orchestration routes."""
    global _agent_manager, _skill_registry, _memory_store
    
    router = APIRouter(prefix="/api/agents", tags=["agents"])
    
    # Initialize singletons
    if _agent_manager is None:
        _agent_manager = AgentManager(name="Aurix")
        _skill_registry = SkillRegistry()
        _memory_store = MemoryStore(max_size=10000)
        
        # Register default agents
        from core.agents.base import Agent as BaseAgent
        
        class DefaultAgent(BaseAgent):
            async def _process_task(self, task: str) -> str:
                # Delegate to LLM or existing processor
                return f"Processed: {task}"
            
            async def _execute_action(self, action: str, params):
                return {"action": action, "status": "executed"}
        
        # Create orchestrator
        orch_config = AgentConfig(
            name="Aurix-Orchestrator",
            role=AgentRole.ORCHESTRATOR,
            model="gpt-4",
            description="Main Aurix orchestrator for multi-agent workflows",
            capabilities=[
                AgentCapability(
                    name="task_delegation",
                    description="Delegate tasks to specialist agents",
                    tags=["coordination", "orchestration"]
                ),
                AgentCapability(
                    name="memory_management",
                    description="Manage agent memory and context",
                    tags=["memory"]
                )
            ]
        )
        orch = DefaultAgent(orch_config)
        _agent_manager.register_agent(orch)
        
        logger.info("Agent manager initialized with default orchestrator")
    
    @router.get("/status")
    async def get_agent_status(request: Request):
        """Get status of all agents."""
        return _agent_manager.get_fleet_status()
    
    @router.get("/list")
    async def list_agents(request: Request):
        """List all registered agents."""
        agents = []
        for agent in _agent_manager.agents.values():
            agents.append(agent.get_state())
        return {"agents": agents}
    
    @router.post("/task/dispatch")
    async def dispatch_task(
        request: Request,
        task_description: str,
        agent_id: str = None,
        priority: int = 0
    ):
        """Dispatch a task to agent(s)."""
        try:
            task_id = await _agent_manager.dispatch_task(
                task_description=task_description,
                agent_id=agent_id,
                priority=priority
            )
            return {"task_id": task_id, "status": "dispatched"}
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
    
    @router.get("/task/{task_id}")
    async def get_task_status(request: Request, task_id: str):
        """Get task status and result."""
        if task_id not in _agent_manager.tasks:
            raise HTTPException(status_code=404, detail="Task not found")
        
        task = _agent_manager.tasks[task_id]
        return {
            "task_id": task_id,
            "agent_id": task.agent_id,
            "status": task.status,
            "result": task.result,
        }
    
    @router.get("/skills")
    async def list_skills(request: Request):
        """List all registered skills."""
        return {"skills": _skill_registry.list_all()}
    
    @router.get("/memory/search")
    async def search_memory(
        request: Request,
        query: str,
        agent_id: str = None,
        memory_type: str = None
    ):
        """Search memory store."""
        mtype = MemoryType[memory_type.upper()] if memory_type else None
        results = _memory_store.search(query, agent_id=agent_id, memory_type=mtype)
        return {
            "query": query,
            "results": [
                {
                    "key": r.key,
                    "value": str(r.value)[:200],  # Truncate for API
                    "type": r.memory_type.value,
                    "agent_id": r.agent_id,
                    "tags": r.tags,
                }
                for r in results
            ]
        }
    
    @router.get("/memory/stats")
    async def memory_stats(request: Request):
        """Get memory store statistics."""
        return _memory_store.get_stats()
    
    # Store instances in app state for reuse
    app_state.agent_manager = _agent_manager
    app_state.skill_registry = _skill_registry
    app_state.memory_store = _memory_store
    
    return router
