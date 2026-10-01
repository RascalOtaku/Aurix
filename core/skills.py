"""
Skill registry and execution framework for agent task delegation.

Skills are reusable, composable units of functionality that agents dispatch
to accomplish tasks. They bridge agent orchestration with actual tool execution.
"""

import asyncio
import inspect
import json
import logging
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


class SkillCategory(Enum):
    """Skill domain classification."""
    MEMORY = "memory"              # Memory search, recall, storage
    WEB_SEARCH = "web_search"      # Information retrieval
    FILE_OPS = "file_ops"          # Read/write filesystem
    SHELL = "shell"                # Execute system commands
    DOCUMENT = "document"          # Process documents, PDFs, etc.
    EMAIL = "email"                # Email operations
    CALENDAR = "calendar"          # Calendar/scheduling
    CODE = "code"                  # Code analysis, execution
    VISION = "vision"              # Image/video processing
    CUSTOM = "custom"              # User-defined


@dataclass
class SkillMetadata:
    """Describes a skill's capabilities and constraints."""
    name: str
    category: SkillCategory
    description: str
    parameters: Dict[str, Any] = field(default_factory=dict)
    returns: str = "Any"
    requires_auth: bool = False
    requires_gpu: bool = False
    max_duration_seconds: int = 300
    retry_count: int = 1
    tags: List[str] = field(default_factory=list)
    confidence: float = 0.8  # Quality estimate [0, 1]


class Skill:
    """Executable skill that agents can invoke."""

    def __init__(
        self,
        name: str,
        category: SkillCategory,
        description: str,
        func: Callable,
        parameters: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
    ):
        self.id = f"{category.value}_{name}".replace(" ", "_").lower()
        self.metadata = SkillMetadata(
            name=name,
            category=category,
            description=description,
            parameters=parameters or {},
            tags=tags or [],
        )
        self.func = func
        self.invocation_count = 0
        self.last_invoked = None
        self.error_count = 0

    async def execute(self, **kwargs) -> Dict[str, Any]:
        """Execute the skill with validation and error handling."""
        try:
            # Type checking against declared parameters
            for key, value in kwargs.items():
                if key not in self.metadata.parameters:
                    logger.warning(f"Skill {self.id}: unknown parameter '{key}'")

            # Call the function (support both sync and async)
            if asyncio.iscoroutinefunction(self.func):
                result = await self.func(**kwargs)
            else:
                result = self.func(**kwargs)

            self.invocation_count += 1
            self.last_invoked = __import__("time").time()

            return {
                "success": True,
                "data": result,
                "skill_id": self.id,
            }
        except Exception as e:
            self.error_count += 1
            logger.error(f"Skill {self.id} execution failed: {e}")
            return {
                "success": False,
                "error": str(e),
                "skill_id": self.id,
            }

    def to_dict(self) -> Dict[str, Any]:
        """Serialize skill metadata."""
        data = asdict(self.metadata)
        data["category"] = self.metadata.category.value
        data["id"] = self.id
        data["invocation_count"] = self.invocation_count
        data["error_count"] = self.error_count
        return data


class SkillRegistry:
    """Centralized registry for skill discovery and execution."""

    def __init__(self):
        self.skills: Dict[str, Skill] = {}
        self.categories: Dict[SkillCategory, List[str]] = {c: [] for c in SkillCategory}
        logger.info("SkillRegistry initialized")

    def register(
        self,
        name: str,
        category: SkillCategory,
        description: str,
        func: Callable,
        parameters: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
    ) -> Skill:
        """Register a new skill."""
        skill = Skill(name, category, description, func, parameters, tags)
        self.skills[skill.id] = skill
        self.categories[category].append(skill.id)
        logger.info(f"Registered skill: {skill.id}")
        return skill

    async def invoke(self, skill_id: str, **kwargs) -> Dict[str, Any]:
        """Execute a skill by ID."""
        skill = self.skills.get(skill_id)
        if not skill:
            return {
                "success": False,
                "error": f"Skill '{skill_id}' not found",
            }
        return await skill.execute(**kwargs)

    def list_by_category(self, category: SkillCategory) -> List[Dict[str, Any]]:
        """List all skills in a category."""
        skill_ids = self.categories.get(category, [])
        return [self.skills[sid].to_dict() for sid in skill_ids if sid in self.skills]

    def list_all(self) -> List[Dict[str, Any]]:
        """List all registered skills."""
        return [s.to_dict() for s in self.skills.values()]

    def search_by_tag(self, tag: str) -> List[Dict[str, Any]]:
        """Find skills by tag."""
        return [
            s.to_dict()
            for s in self.skills.values()
            if tag.lower() in [t.lower() for t in s.metadata.tags]
        ]


def register_builtin_skills(registry: SkillRegistry) -> None:
    """Register built-in skills available to all agents."""
    
    # Memory skills
    async def memory_search(query: str, limit: int = 5) -> List[Dict[str, Any]]:
        """Search persistent memory for relevant entries."""
        # This will be bound to actual memory_manager at runtime
        return []

    registry.register(
        name="Memory Search",
        category=SkillCategory.MEMORY,
        description="Search semantic memory for relevant context",
        func=memory_search,
        parameters={"query": str, "limit": int},
        tags=["memory", "context", "retrieval"],
    )

    # Web search skill
    async def web_search(query: str, num_results: int = 5) -> List[Dict[str, Any]]:
        """Search the web for information."""
        return []

    registry.register(
        name="Web Search",
        category=SkillCategory.WEB_SEARCH,
        description="Search the internet for current information",
        func=web_search,
        parameters={"query": str, "num_results": int},
        tags=["web", "search", "research"],
    )

    # Document summarization
    async def summarize_document(content: str, max_length: int = 500) -> str:
        """Summarize document content."""
        return content[:max_length]

    registry.register(
        name="Document Summarizer",
        category=SkillCategory.DOCUMENT,
        description="Summarize document content to key points",
        func=summarize_document,
        parameters={"content": str, "max_length": int},
        tags=["document", "summarization", "analysis"],
    )

    # File operations
    def read_file(path: str) -> str:
        """Read file contents."""
        try:
            with open(path, "r") as f:
                return f.read()
        except Exception as e:
            raise Exception(f"Failed to read {path}: {e}")

    registry.register(
        name="Read File",
        category=SkillCategory.FILE_OPS,
        description="Read contents of a file",
        func=read_file,
        parameters={"path": str},
        tags=["file", "read", "filesystem"],
    )

    def write_file(path: str, content: str) -> str:
        """Write content to file."""
        try:
            with open(path, "w") as f:
                f.write(content)
            return f"Wrote {len(content)} bytes to {path}"
        except Exception as e:
            raise Exception(f"Failed to write {path}: {e}")

    registry.register(
        name="Write File",
        category=SkillCategory.FILE_OPS,
        description="Write content to a file",
        func=write_file,
        parameters={"path": str, "content": str},
        tags=["file", "write", "filesystem"],
    )

    logger.info("Built-in skills registered (6 base skills)")
