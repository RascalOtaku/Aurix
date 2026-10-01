"""Base Agent class implementing core functionality."""

import asyncio
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Callable
from datetime import datetime

from .types import AgentRole, AgentStatus, AgentCapability, AgentMemory


@dataclass
class AgentConfig:
    """Agent configuration."""
    name: str
    role: AgentRole
    model: str = "gpt-4"
    temperature: float = 0.7
    max_tokens: int = 2000
    description: str = ""
    capabilities: List[AgentCapability] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    system_prompt: str = ""
    context_window: int = 8192


class Agent(ABC):
    """Base agent class (inspired by OpenClaw, ECC patterns)."""

    def __init__(self, config: AgentConfig):
        self.config = config
        self.id = str(uuid.uuid4())
        self.status = AgentStatus.IDLE
        self.memory = AgentMemory(
            short_term={},
            long_term={},
            working_memory={}
        )
        self.created_at = datetime.now()
        self.updated_at = datetime.now()
        self.task_history: List[Dict[str, Any]] = []
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._listeners: List[Callable] = []

    async def think(self, task: str) -> str:
        """Process task and generate response."""
        self.status = AgentStatus.THINKING
        try:
            response = await self._process_task(task)
            self.status = AgentStatus.COMPLETE
            return response
        except Exception as e:
            self.status = AgentStatus.ERROR
            raise
        finally:
            self.updated_at = datetime.now()

    async def execute(self, action: str, params: Dict[str, Any]) -> Any:
        """Execute specific action with parameters."""
        self.status = AgentStatus.WORKING
        try:
            result = await self._execute_action(action, params)
            return result
        finally:
            self.status = AgentStatus.IDLE
            self.updated_at = datetime.now()

    def add_capability(self, capability: AgentCapability) -> None:
        """Register new capability."""
        self.config.capabilities.append(capability)

    def get_capabilities(self) -> List[AgentCapability]:
        """List all capabilities."""
        return [c for c in self.config.capabilities if c.enabled]

    def subscribe(self, listener: Callable) -> None:
        """Subscribe to agent events."""
        self._listeners.append(listener)

    async def _notify(self, event: str, data: Any) -> None:
        """Notify subscribers of events."""
        for listener in self._listeners:
            if asyncio.iscoroutinefunction(listener):
                await listener({"event": event, "data": data})
            else:
                listener({"event": event, "data": data})

    @abstractmethod
    async def _process_task(self, task: str) -> str:
        """Process task (implement in subclass)."""
        pass

    @abstractmethod
    async def _execute_action(self, action: str, params: Dict[str, Any]) -> Any:
        """Execute action (implement in subclass)."""
        pass

    def get_state(self) -> Dict[str, Any]:
        """Get current agent state."""
        return {
            "id": self.id,
            "name": self.config.name,
            "role": self.config.role.value,
            "status": self.status.value,
            "capabilities": len(self.get_capabilities()),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "tasks_completed": len(self.task_history),
        }
