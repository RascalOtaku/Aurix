"""Agent type definitions and enums."""

from enum import Enum
from typing import Any, Dict
from dataclasses import dataclass
from datetime import datetime


class AgentRole(str, Enum):
    """Agent role types (inspired by ECC office patterns)."""
    ORCHESTRATOR = "orchestrator"  # Main coordinator
    SPECIALIST = "specialist"      # Domain-specific expert
    REVIEWER = "reviewer"          # Code/output reviewer
    EXECUTOR = "executor"          # Task executor
    RESEARCHER = "researcher"      # Information gathering
    COMMUNICATOR = "communicator"  # User interface


class AgentStatus(str, Enum):
    """Agent lifecycle states."""
    IDLE = "idle"
    THINKING = "thinking"
    WORKING = "working"
    WAITING = "waiting"
    ERROR = "error"
    COMPLETE = "complete"


@dataclass
class AgentCapability:
    """Agent capability descriptor."""
    name: str
    description: str
    enabled: bool = True
    version: str = "1.0.0"
    tags: list[str] = None
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.tags is None:
            self.tags = []
        if self.metadata is None:
            self.metadata = {}


@dataclass
class AgentMemory:
    """Agent memory/context store (context-mode inspired)."""
    short_term: Dict[str, Any]  # Current session context
    long_term: Dict[str, Any]   # Persistent knowledge
    working_memory: Dict[str, Any]  # Active task state
    created_at: datetime = None
    updated_at: datetime = None

    def __post_init__(self):
        if self.created_at is None:
            self.created_at = datetime.now()
        if self.updated_at is None:
            self.updated_at = datetime.now()

    def clear_working_memory(self):
        """Clear working memory between tasks."""
        self.working_memory.clear()
        self.updated_at = datetime.now()
