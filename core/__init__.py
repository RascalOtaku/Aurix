"""Aurix core multi-agent framework.

Inspired by:
- OpenClaw: AI that does things
- ECC: Agent harness performance optimization
- munder-difflin: Multi-agent orchestration
- context-mode: Context window optimization
"""

from .agents import Agent, AgentConfig, AgentManager
from .skills import Skill, SkillRegistry
from .memory import MemoryStore

__version__ = "2.0.0-alpha"

__all__ = [
    "Agent",
    "AgentConfig",
    "AgentManager",
    "Skill",
    "SkillRegistry",
    "MemoryStore",
]
