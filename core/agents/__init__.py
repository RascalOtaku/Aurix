"""Multi-agent orchestration framework.

Inspired by OpenClaw, ECC, and munder-difflin patterns.
"""

from .base import Agent, AgentConfig
from .manager import AgentManager
from .types import AgentRole, AgentStatus

__all__ = [
    "Agent",
    "AgentConfig",
    "AgentManager",
    "AgentRole",
    "AgentStatus",
]
