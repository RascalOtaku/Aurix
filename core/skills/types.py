"""Skill type definitions."""

from enum import Enum
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from datetime import datetime


class SkillCategory(str, Enum):
    """Skill categories."""
    CODE = "code"              # Code generation/review
    DATA = "data"              # Data processing
    COMMUNICATION = "communication"  # User interaction
    RESEARCH = "research"      # Information gathering
    AUTOMATION = "automation"  # Task automation
    MEDIA = "media"            # Audio/video processing
    INTEGRATION = "integration"  # External service integration
    CUSTOM = "custom"          # User-defined


@dataclass
class SkillExecutionContext:
    """Execution context for skill."""
    skill_name: str
    agent_id: str
    inputs: Dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.now)
    timeout: float = 30.0
    max_retries: int = 3
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillMetadata:
    """Skill metadata descriptor."""
    name: str
    category: SkillCategory
    description: str
    version: str = "1.0.0"
    author: str = ""
    tags: List[str] = field(default_factory=list)
    inputs: Dict[str, Any] = field(default_factory=dict)
    outputs: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    dependencies: List[str] = field(default_factory=list)
